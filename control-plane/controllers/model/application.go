// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Retains executable choices and source admission across Pool replacement and platform updates.
package model

import (
	"context"
	"fmt"
	"reflect"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// sourceSelectionAllowed admits new source intent only in source mode, while preserving
// controller-owned selections after that mode is disabled.
func (reconciler *ServiceReconciler) sourceSelectionAllowed(ctx context.Context, service *inferencev1alpha1.ModelService) (bool, error) {
	if reconciler.SourceMode {
		return true, nil
	}
	revision := service.Annotations[runtimeconfig.SourceRevisionAnnotation]
	if revision == "" {
		return false, nil
	}
	if len(service.Status.PoolApplications) > 0 {
		for _, selected := range service.Status.PoolApplications {
			if selected.SourceRevision != revision || selected.DeploymentRevision != service.Spec.DeploymentRevision {
				return false, nil
			}
		}
		return true, nil
	}
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return false, err
	}
	if len(pools) == 0 {
		return false, nil
	}
	for _, pool := range pools {
		if pool.Spec.Template.SourceRevision != revision {
			return false, nil
		}
		selected := pool.Spec.Template.Application
		if selected == nil {
			if service.Spec.DeploymentRevision != "" {
				return false, nil
			}
		} else if selected.DeploymentRevision != service.Spec.DeploymentRevision {
			return false, nil
		}
	}
	return true, nil
}

// selectPoolApplications binds compiled templates to executable choices and persists them before Pool writes.
func (reconciler *ServiceReconciler) selectPoolApplications(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool, byPoolName map[string]*inferencev1alpha1.ModelPool) error {
	// Cache identity follows encoder content, not replica counts or service alert settings.
	cacheGeneration := service.Generation
	for _, compiled := range compiledPools {
		if compiled.Template.Role != inferencev1alpha1.ModelRoleEncoder {
			continue
		}
		if previous := byPoolName[compiled.Name]; previous != nil {
			before, after := previous.Spec.Template, compiled.Template
			if before.EncoderCacheGeneration > 0 && before.Model == after.Model && before.Source == after.Source && before.ModelRevision == after.ModelRevision && before.Backend == after.Backend && before.ECProfile == after.ECProfile && reflect.DeepEqual(before.EngineArgs, after.EngineArgs) && reflect.DeepEqual(before.Env, after.Env) {
				cacheGeneration = before.EncoderCacheGeneration
			}
		}
	}
	base := service.DeepCopy()
	applications := make(map[string]inferencev1alpha1.ApplicationSelection, len(compiledPools))
	for index := range compiledPools {
		template := &compiledPools[index].Template
		previous := byPoolName[compiledPools[index].Name]
		selection, err := reconciler.selectPoolApplication(ctx, service, compiledPools[index], previous)
		if err != nil {
			return err
		}
		if template.Backend == "vllm" && selection != nil && selection.InstanceAdmissionProtocol == 0 {
			startupAdmission, err := reconciler.legacyInstanceAdmission(ctx, service, previous, compiledPools[index].Name)
			if err != nil {
				return err
			}
			if !reflect.DeepEqual(startupAdmission, compiledPools[index].InstanceAdmission) {
				return &instanceAdmissionDeploymentRequiredError{poolName: compiledPools[index].Name}
			}
			template.InstanceAdmission = startupAdmission
		}
		template.Application = selection
		if selection != nil {
			applications[compiledPools[index].Name] = *selection
		}
		if compiledPools[index].Template.ECProfile != "" {
			compiledPools[index].Template.EncoderCacheGeneration = cacheGeneration
		}
	}

	if len(applications) == 0 {
		applications = nil
	}
	if !reflect.DeepEqual(service.Status.PoolApplications, applications) {
		service.Status.PoolApplications = applications
		if err := reconciler.Status().Patch(ctx, service, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return fmt.Errorf("persist Pool application selections: %w", err)
		}
	}

	return nil
}

// selectPoolApplication retains execution choices independently of changing platform defaults.
// Existing image-only Pools adopt their actual cohort, never a newly configured file publication.
func (reconciler *ServiceReconciler) selectPoolApplication(ctx context.Context, service *inferencev1alpha1.ModelService, compiled compiler.ModelPool, previous *inferencev1alpha1.ModelPool) (*inferencev1alpha1.ApplicationSelection, error) {
	template := compiled.Template
	deployment := service.Spec.DeploymentRevision
	image := reconciler.RuntimeProfile.Image
	imageProfile := template.Backend
	if template.Backend == "vllm-omni" {
		image = reconciler.RuntimeProfile.OmniImage
	} else if template.Profiling != nil && template.Profiling.Engine == "nsight" {
		image = reconciler.RuntimeProfile.NsightImage
		imageProfile = "nsight"
	}
	matches := func(selected inferencev1alpha1.ApplicationSelection) bool {
		return selected.DeploymentRevision == deployment && selected.SourceRevision == template.SourceRevision && selected.ImageProfile == imageProfile
	}
	if selected, exists := service.Status.PoolApplications[compiled.Name]; exists && matches(selected) {
		return selected.DeepCopy(), nil
	}
	if previous == nil && template.SourceRevision != "" && !reconciler.SourceMode {
		for _, selected := range service.Status.PoolApplications {
			if matches(selected) {
				return selected.DeepCopy(), nil
			}
		}
		pools, err := reconciler.ownedPools(ctx, service)
		if err != nil {
			return nil, err
		}
		for index := range pools {
			if pools[index].Spec.Template.SourceRevision == template.SourceRevision && pools[index].Spec.Template.Backend == template.Backend {
				previous = &pools[index]
				break
			}
		}
	}
	selection := &inferencev1alpha1.ApplicationSelection{Image: image, ImageProfile: imageProfile, SourceRevision: template.SourceRevision, DeploymentRevision: deployment, AdmissionStore: reconciler.RuntimeProfile.AdmissionStore.DeepCopy()}
	if template.Backend == "vllm" {
		selection.InstanceAdmissionProtocol = inferencev1alpha1.InstanceAdmissionFileProtocol
	}
	if previous != nil && previous.Spec.Template.Backend == template.Backend && previous.Spec.Template.SourceRevision == template.SourceRevision {
		previousProfile := previous.Spec.Template.Backend
		if previous.Spec.Template.Profiling != nil && previous.Spec.Template.Profiling.Engine == "nsight" {
			previousProfile = "nsight"
		}
		if selected := previous.Spec.Template.Application; selected != nil && selected.DeploymentRevision == deployment && previousProfile == imageProfile {
			retained := selected.DeepCopy()
			retained.SourceRevision, retained.ImageProfile = template.SourceRevision, imageProfile
			return retained, nil
		}
		if previous.Spec.Template.Application == nil && deployment == "" && previousProfile == imageProfile {
			recovered, err := reconciler.recoverPoolApplication(ctx, service, previous, selection)
			if err != nil {
				return nil, err
			}
			if recovered {
				return selection, nil
			}
			// An idle legacy Pool with no execution history has nothing to recover.
			// Keep it unselected until its first demand for capacity chooses the current pair.
			if compiled.DesiredGroups == 0 {
				return nil, nil
			}
		}
	}
	if template.Backend == "vllm" {
		selection.ApplicationURL = reconciler.ApplicationURL
		if template.SourceRevision != "" {
			publishedImage, err := reconciler.ApplicationFiles.ResolveImage(ctx, "model-server", template.SourceRevision, image)
			if err != nil {
				return nil, err
			}
			// Source files also run under Nsight, but their ordinary runtime image
			// cannot replace the platform's diagnostic tools and launch environment.
			if imageProfile != "nsight" {
				selection.Image = publishedImage
			}
			selection.ApplicationURL = reconciler.ApplicationFiles.Ref("model-server", template.SourceRevision)
		}
	}
	return selection, nil
}

// recoverPoolApplication fills a legacy selection from its actual execution cohort.
// It reports whether a Group provided an executable choice, leaving idle Pools unselected otherwise.
func (reconciler *ServiceReconciler) recoverPoolApplication(ctx context.Context, service *inferencev1alpha1.ModelService, pool *inferencev1alpha1.ModelPool, selection *inferencev1alpha1.ApplicationSelection) (bool, error) {
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	groups, err := ownedModelGroups(ctx, reader, pool)
	if err != nil {
		return false, err
	}
	for _, group := range groups {
		if len(groups) == 1 || group.Spec.Revision == pool.Status.PreparedRevision || group.Spec.Revision == servingconfig.ServingRevision(service, pool) {
			selection.Image = group.Spec.Runtime.Image
			selection.ApplicationURL = group.Spec.Runtime.ApplicationURL
			selection.AdmissionStore = group.Spec.Runtime.AdmissionStore.DeepCopy()
			selection.InstanceAdmissionProtocol = group.Spec.Runtime.InstanceAdmissionProtocol
			if selection.ApplicationURL == "" {
				selection.ApplicationURL = reconciler.ApplicationFiles.Ref("model-server", group.Spec.Runtime.SourceRevision)
			}
			return true, nil
		}
	}
	return false, nil
}

type instanceAdmissionDeploymentRequiredError struct {
	poolName string
}

// Error identifies instance intent that the selected executable cannot apply without redeployment.
func (err *instanceAdmissionDeploymentRequiredError) Error() string {
	return fmt.Sprintf("ModelPool %q requires an explicit deployment of a compatible model-server application for instance admission", err.poolName)
}

// legacyInstanceAdmission recovers a retained application's startup limit without taking new intent as its value.
// The selected actual cohort is authoritative; an idle Pool retains its immutable template limit.
func (reconciler *ServiceReconciler) legacyInstanceAdmission(ctx context.Context, service *inferencev1alpha1.ModelService, pool *inferencev1alpha1.ModelPool, poolName string) (*inferencev1alpha1.InstanceAdmissionConfig, error) {
	if pool == nil {
		return nil, &instanceAdmissionDeploymentRequiredError{poolName: poolName}
	}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	groups, err := ownedModelGroups(ctx, reader, pool)
	if err != nil {
		return nil, err
	}
	selected := servingconfig.ServingRevision(service, pool)
	if selected == "" {
		selected = pool.Status.PreparedRevision
	}
	startupAdmission := pool.Spec.Template.InstanceAdmission.DeepCopy()
	found := false
	for index := range groups {
		group := &groups[index]
		if selected != "" && group.Spec.Revision != selected || group.Spec.Runtime.InstanceAdmissionProtocol != 0 {
			continue
		}
		if found && !reflect.DeepEqual(startupAdmission, group.Spec.Runtime.InstanceAdmission) {
			return nil, &instanceAdmissionDeploymentRequiredError{poolName: poolName}
		}
		startupAdmission = group.Spec.Runtime.InstanceAdmission.DeepCopy()
		found = true
	}
	if !found && pool.Spec.Template.Application != nil && pool.Spec.Template.Application.InstanceAdmissionProtocol != 0 {
		return nil, &instanceAdmissionDeploymentRequiredError{poolName: poolName}
	}
	return startupAdmission, nil
}
