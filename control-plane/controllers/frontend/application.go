// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Selects and persists frontend applications independently of changing platform defaults.
package frontend

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"strconv"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	appsv1 "k8s.io/api/apps/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// reconcileApplication returns the selected runtime profile after persisting its application and protocol.
// A nil profile without an error means a rejected target was reported without replacing the running application.
func (reconciler *Reconciler) reconcileApplication(ctx context.Context, frontend *inferencev1alpha1.FrontendService, services []inferencev1alpha1.ModelService) (*FrontendRuntimeProfile, error) {
	if err := reconciler.RuntimeProfile.validate(); err != nil {
		return nil, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "RuntimeProfileIncomplete", FailureMessage: err.Error()})
	}
	sourceAllowed, err := reconciler.sourceSelectionAllowed(ctx, frontend)
	if err != nil {
		return nil, err
	}
	sourceRevision, err := runtimeconfig.SourceRevision(frontend.Annotations, sourceAllowed)
	if err != nil {
		return nil, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "InvalidIntent", FailureMessage: err.Error()})
	}
	profile := reconciler.RuntimeProfile
	profile.SourceRevision = sourceRevision
	deploymentRevision := frontend.Spec.DeploymentRevision
	selection := frontend.Status.Application
	deploymentRequired := frontendState{
		FailureReason:  "FrontendDeploymentRequired",
		FailureMessage: "Redeploy the frontend with the current platform application before applying its serving configuration",
	}
	if selection == nil {
		current := new(appsv1.Deployment)
		if err := reconciler.Get(ctx, client.ObjectKeyFromObject(frontend), current); err == nil {
			if !metav1.IsControlledBy(current, frontend) {
				return nil, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "InvalidIntent", FailureMessage: "The existing frontend Deployment belongs to another owner"})
			}
			// Adopting observed state must not consume an explicit request for a new application.
			if deploymentRevision == "" {
				responseStore, err := runtimeconfig.ResponseStoreFromEnv(current.Spec.Template.Spec.Containers[0].Env)
				if err != nil {
					return nil, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "InvalidIntent", FailureMessage: err.Error()})
				}
				selection = &inferencev1alpha1.FrontendApplicationSelection{
					ApplicationSelection: inferencev1alpha1.ApplicationSelection{
						Image:              current.Spec.Template.Spec.Containers[0].Image,
						ApplicationURL:     current.Spec.Template.Annotations["inference.foretoken.io/application-url"],
						SourceRevision:     current.Spec.Template.Annotations[runtimeconfig.SourceRevisionAnnotation],
						DeploymentRevision: deploymentRevision,
					},
					ResponseStore: responseStore,
				}
				selection.AdmissionStore, err = runtimeconfig.RedisConnectionFromEnv(current.Spec.Template.Spec.Containers[0].Env, runtimeconfig.AdmissionStoreURLEnv)
				if err != nil {
					return nil, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "InvalidIntent", FailureMessage: err.Error()})
				}
				base := frontend.DeepCopy()
				frontend.Status.Application = selection
				frontend.Status.ServingConfigVersion = 0
				if version, err := strconv.ParseUint(current.Spec.Template.Annotations[servingconfig.ProtocolAnnotation], 10, 32); err == nil && version >= uint64(servingconfig.AdmissionProtocol) && version <= uint64(servingconfig.CurrentProtocol) {
					frontend.Status.ServingConfigVersion = uint32(version)
				}
				if err := reconciler.Status().Patch(ctx, frontend, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
					return nil, fmt.Errorf("retain frontend application configuration: %w", err)
				}
			}
		} else if !apierrors.IsNotFound(err) {
			return nil, err
		}
	}
	retained := selection != nil && selection.DeploymentRevision == deploymentRevision && (selection.SourceRevision == sourceRevision || (selection.SourceRevision == "" && sourceRevision != "" && selection.ApplicationURL == profile.ApplicationFiles.Ref("frontend", sourceRevision)))
	if !retained {
		selection = &inferencev1alpha1.FrontendApplicationSelection{
			ApplicationSelection: inferencev1alpha1.ApplicationSelection{
				Image: profile.Image, ApplicationURL: profile.ApplicationURL, SourceRevision: sourceRevision,
				DeploymentRevision: deploymentRevision, AdmissionStore: profile.AdmissionStore.DeepCopy(),
			},
			ResponseStore: profile.ResponseStore.DeepCopy(),
		}
		if sourceRevision != "" {
			image, err := profile.ApplicationFiles.ResolveImage(ctx, "frontend", sourceRevision, profile.Image)
			if err != nil {
				statusErr := reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "ApplicationResolutionFailed", FailureMessage: err.Error()})
				return nil, errors.Join(err, statusErr)
			}
			selection.Image = image
			selection.ApplicationURL = profile.ApplicationFiles.Ref("frontend", sourceRevision)
		}
	} else {
		selection = selection.DeepCopy()
		selection.SourceRevision = sourceRevision
	}
	// Application selection and its serving protocol advance together. A platform update
	// leaves an explicitly retained application and its existing configuration untouched.
	requiresModelRouting := slices.ContainsFunc(services, func(service inferencev1alpha1.ModelService) bool {
		return service.DeletionTimestamp.IsZero() && (service.Spec.Backend == "vllm" || service.Spec.Backend == "vllm-omni") && service.Spec.RouterPipeline != nil
	})
	if retained && (frontend.Status.ServingConfigVersion < servingconfig.AdmissionProtocol || requiresModelRouting && frontend.Status.ServingConfigVersion < servingconfig.ModelRoutingProtocol) {
		_, membershipErr := reconciler.reconcileServingSnapshot(ctx, frontend, nil, true)
		statusErr := reconciler.updateStatus(ctx, frontend, deploymentRequired)
		return nil, errors.Join(membershipErr, statusErr)
	}
	selectedVersion := frontend.Status.ServingConfigVersion
	if !retained {
		selectedVersion = servingconfig.CurrentProtocol
	}
	if !reflect.DeepEqual(frontend.Status.Application, selection) || frontend.Status.ServingConfigVersion != selectedVersion {
		base := frontend.DeepCopy()
		frontend.Status.Application = selection
		frontend.Status.ServingConfigVersion = selectedVersion
		if err := reconciler.Status().Patch(ctx, frontend, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return nil, fmt.Errorf("persist frontend application selection: %w", err)
		}
	}
	profile.Image, profile.ApplicationURL = selection.Image, selection.ApplicationURL
	profile.AdmissionStore = selection.AdmissionStore.DeepCopy()
	profile.ResponseStore = selection.ResponseStore.DeepCopy()
	return &profile, nil
}

// sourceSelectionAllowed preserves an already-selected source publication without
// reopening source admission when the platform disables new source selections.
func (reconciler *Reconciler) sourceSelectionAllowed(ctx context.Context, frontend *inferencev1alpha1.FrontendService) (bool, error) {
	profile := reconciler.RuntimeProfile
	if profile.SourceMode {
		return true, nil
	}
	revision := frontend.Annotations[runtimeconfig.SourceRevisionAnnotation]
	if revision == "" {
		return false, nil
	}
	if selected := frontend.Status.Application; selected != nil {
		return selected.DeploymentRevision == frontend.Spec.DeploymentRevision && (selected.SourceRevision == revision || (selected.SourceRevision == "" && selected.ApplicationURL == profile.ApplicationFiles.Ref("frontend", revision))), nil
	}
	if frontend.Spec.DeploymentRevision != "" {
		return false, nil
	}
	deployment := new(appsv1.Deployment)
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(frontend), deployment); err != nil {
		return false, client.IgnoreNotFound(err)
	}
	return metav1.IsControlledBy(deployment, frontend) && deployment.Spec.Template.Annotations[runtimeconfig.SourceRevisionAnnotation] == revision && deployment.Spec.Template.Annotations["inference.foretoken.io/application-url"] == profile.ApplicationFiles.Ref("frontend", revision), nil
}

func (profile FrontendRuntimeProfile) validate() error {
	if profile.Image == "" {
		return fmt.Errorf("frontend runtime image is not configured")
	}
	if profile.Port < 1 || profile.Port > 65535 {
		return fmt.Errorf("frontend runtime port must be between 1 and 65535")
	}
	if profile.Gateway != nil && profile.Gateway.Name == "" {
		return fmt.Errorf("Gateway parent name is not configured")
	}
	return nil
}
