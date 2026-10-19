// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Retains resolved source versions independently of the runtime cache's storage lifecycle.

package model

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const (
	preparationNamespaceEnv      = "FORETOKEN_MODEL_PREPARATION_NAMESPACE"
	preparationRecordEnv         = "FORETOKEN_MODEL_PREPARATION_RECORD"
	preparationRecordDirectory   = "/etc/foretoken/model-source"
	preparationHFEndpointKey     = "huggingface-endpoint"
	preparationSourceReadyReason = "SourcePrepared"
)

// Missing publication or authority does not establish that source inputs changed.
type preparationSourceInputMatch uint8

const (
	preparationSourceInputsUnknown preparationSourceInputMatch = iota
	preparationSourceInputsMatch
	preparationSourceInputsChanged
)

// reconcilePreparationRecord returns the cohort's source record with its volume and endpoint pinned.
// Pool updates do not replace existing bindings.
func reconcilePreparationRecord(ctx context.Context, kubeClient client.Client, reader client.Reader, pool *inferencev1alpha1.ModelPool, spec inferencev1alpha1.ModelGroupSpec) (*corev1.ConfigMap, error) {
	name := runtimeconfig.PreparationSourceName(spec)
	record := &corev1.ConfigMap{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: pool.Namespace, Labels: map[string]string{runtimeconfig.ModelCacheSourceLabel: string(pool.UID)}},
	}
	if reader == nil {
		reader = kubeClient
	}
	err := reader.Get(ctx, client.ObjectKeyFromObject(record), record)
	creating := apierrors.IsNotFound(err)
	if err != nil && !creating {
		return nil, fmt.Errorf("read model source record: %w", err)
	}
	if creating {
		if err := controllerutil.SetControllerReference(pool, record, kubeClient.Scheme()); err != nil {
			return nil, err
		}
	} else if !metav1.IsControlledBy(record, pool) {
		return nil, fmt.Errorf("model source ConfigMap %q belongs to another owner", name)
	}
	if !record.DeletionTimestamp.IsZero() {
		return nil, fmt.Errorf("model source ConfigMap %q is being deleted", name)
	}
	base := record.DeepCopy()
	if spec.Artifacts.Source == inferencev1alpha1.ModelSourceHF {
		endpoint := preparationHuggingFaceEndpoint(spec.Artifacts.HuggingFaceAccess)
		if recorded, known := record.Data[preparationHFEndpointKey]; known {
			if recorded != endpoint {
				return nil, fmt.Errorf("model source ConfigMap %q has a different Hugging Face endpoint from its cohort", name)
			}
		} else {
			confirmed := creating
			if !creating {
				// Only a surviving cohort Group can establish a missing endpoint; new Pool intent cannot.
				groups, err := ownedModelGroups(ctx, reader, pool)
				if err != nil {
					return nil, err
				}
				for _, group := range groups {
					if runtimeconfig.PreparationSourceName(group.Spec) == name && group.Spec.Artifacts.Source == inferencev1alpha1.ModelSourceHF && group.Spec.Runtime.PreparationVersion > 0 {
						endpoint = preparationHuggingFaceEndpoint(group.Spec.Artifacts.HuggingFaceAccess)
						confirmed = true
						break
					}
				}
			}
			if confirmed {
				if record.Data == nil {
					record.Data = make(map[string]string)
				}
				record.Data[preparationHFEndpointKey] = endpoint
			}
		}
	}
	if cache := spec.Artifacts.Cache; cache != nil && spec.Runtime.PreparationVersion >= runtimeconfig.SharedModelCachePreparationVersion {
		if encoded, bound := record.Data[runtimeconfig.ModelCacheReferenceKey]; bound {
			var reference runtimeconfig.ModelCacheReference
			if err := json.Unmarshal([]byte(encoded), &reference); err != nil {
				return nil, fmt.Errorf("decode model source cache reference %q: %w", name, err)
			}
			if reference.Directory != cache.Directory || reference.ClaimName != cache.ClaimName || reference.MountPath != cache.MountPath {
				return nil, fmt.Errorf("model source ConfigMap %q has a different cache binding from its cohort", name)
			}
		} else {
			claim := new(corev1.PersistentVolumeClaim)
			if err := reader.Get(ctx, client.ObjectKey{Namespace: pool.Namespace, Name: cache.ClaimName}, claim); err != nil {
				return nil, fmt.Errorf("read model source cache claim: %w", err)
			}
			if claim.UID == "" {
				return nil, fmt.Errorf("model source cache claim %q has no UID", claim.Name)
			}
			reference := runtimeconfig.ModelCacheReference{
				Directory: cache.Directory, ClaimName: claim.Name, ClaimUID: claim.UID,
				MountPath: cache.MountPath, DirectoryOwner: cache.DirectoryOwner.DeepCopy(),
			}
			encoded, err := json.Marshal(reference)
			if err != nil {
				return nil, fmt.Errorf("encode model source cache reference: %w", err)
			}
			if record.Data == nil {
				record.Data = make(map[string]string)
			}
			record.Data[runtimeconfig.ModelCacheReferenceKey] = string(encoded)
		}
		controllerutil.AddFinalizer(record, runtimeconfig.ModelCacheReferenceFinalizer)
	}
	if creating {
		if err := kubeClient.Create(ctx, record); err != nil {
			return nil, fmt.Errorf("create model source ConfigMap: %w", err)
		}
	} else if !reflect.DeepEqual(base.Data, record.Data) || !reflect.DeepEqual(base.Finalizers, record.Finalizers) {
		if err := kubeClient.Patch(ctx, record, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return nil, fmt.Errorf("retain model source record: %w", err)
		}
	}
	return record, nil
}

// reconcilePreparationSource grants preparation workers scoped source publication permissions.
func (reconciler *GroupReconciler) reconcilePreparationSource(ctx context.Context, group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool) error {
	record, err := reconcilePreparationRecord(ctx, reconciler.Client, reconciler.APIReader, pool, group.Spec)
	if err != nil {
		return err
	}
	name := record.Name
	metadata := metav1.ObjectMeta{Name: name, Namespace: group.Namespace, Labels: map[string]string{runtimeconfig.ModelCacheSourceLabel: string(pool.UID)}}
	automount := false
	objects := []client.Object{
		&corev1.ServiceAccount{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"}, ObjectMeta: metadata, AutomountServiceAccountToken: &automount},
		&rbacv1.Role{
			TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "Role"}, ObjectMeta: metadata,
			Rules: []rbacv1.PolicyRule{{APIGroups: []string{""}, Resources: []string{"configmaps"}, ResourceNames: []string{name}, Verbs: []string{"get", "patch"}}},
		},
		&rbacv1.RoleBinding{
			TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "RoleBinding"}, ObjectMeta: metadata,
			RoleRef:  rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: name},
			Subjects: []rbacv1.Subject{{Kind: "ServiceAccount", Name: name, Namespace: group.Namespace}},
		},
	}
	for _, desired := range objects {
		if err := controllerutil.SetControllerReference(record, desired, reconciler.Scheme()); err != nil {
			return err
		}
		current := desired.DeepCopyObject().(client.Object)
		if err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current); apierrors.IsNotFound(err) {
			if err := reconciler.Create(ctx, desired); err != nil {
				return fmt.Errorf("create model source %s: %w", desired.GetObjectKind().GroupVersionKind().Kind, err)
			}
			continue
		} else if err != nil {
			return err
		}
		if !metav1.IsControlledBy(current, record) {
			return fmt.Errorf("model source %s %q belongs to another owner", desired.GetObjectKind().GroupVersionKind().Kind, name)
		}
		if err := reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner(modelGroupFieldOwner), client.ForceOwnership); err != nil {
			return fmt.Errorf("apply model source permissions: %w", err)
		}
	}
	return nil
}

// reconcilePreparationSources retains sources before execution or drain and releases unused records.
// target affects retention only, not cohort selection or provider revision resolution.
// It reports pending record deletion, which Pool deletion must await.
func (reconciler *PoolReconciler) reconcilePreparationSources(ctx context.Context, pool *inferencev1alpha1.ModelPool, service *inferencev1alpha1.ModelService, target *inferencev1alpha1.ModelGroupSpec) (bool, error) {
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	var records corev1.ConfigMapList
	if err := reader.List(ctx, &records, client.InNamespace(pool.Namespace), client.MatchingLabels{runtimeconfig.ModelCacheSourceLabel: string(pool.UID)}); err != nil {
		return false, fmt.Errorf("list model source records: %w", err)
	}
	// The Pool pins target records before creating Groups; other records follow their Group.
	// Read records first so a concurrent Group creation cannot look like a completed cohort.
	groups, err := ownedModelGroups(ctx, reader, pool)
	if err != nil {
		return false, err
	}
	owned := make(map[string]*corev1.ConfigMap, len(records.Items))
	for index := range records.Items {
		record := &records.Items[index]
		if metav1.IsControlledBy(record, pool) {
			owned[record.Name] = record
		}
	}
	live := make(map[string]bool, len(groups))
	for _, group := range groups {
		name := runtimeconfig.PreparationSourceName(group.Spec)
		live[name] = true
		if record := owned[name]; record != nil && record.DeletionTimestamp.IsZero() && group.Spec.Artifacts.Source == inferencev1alpha1.ModelSourceHF && group.Spec.Runtime.PreparationVersion > 0 {
			if _, known := record.Data[preparationHFEndpointKey]; !known {
				updated, err := reconcilePreparationRecord(ctx, reconciler.Client, reader, pool, group.Spec)
				if err != nil {
					return false, err
				}
				*record = *updated
			}
		}
	}

	var selected, reserved *corev1.ConfigMap
	selectedMatch := preparationSourceInputsUnknown
	retainIntent := pool.DeletionTimestamp.IsZero() && preparationServiceIntentMatches(pool, service)
	if retainIntent {
		if reference := pool.Status.ArtifactSource; reference != nil {
			record := owned[reference.Name]
			if record == nil || string(record.UID) != reference.UID {
				return false, fmt.Errorf("ModelPool artifact source %q no longer has its selected UID", reference.Name)
			}
			match, err := comparePreparationSourceInputs(record, pool.Spec.Template)
			if err != nil {
				return false, err
			}
			if record.Data[runtimeconfig.PreparationRecordKey] == "" {
				for _, group := range groups {
					if runtimeconfig.PreparationSourceName(group.Spec) == record.Name && !preparationGroupMatches(group.Spec, pool.Spec.Template) {
						match = preparationSourceInputsChanged
						break
					}
				}
			}
			if match != preparationSourceInputsChanged && record.DeletionTimestamp.IsZero() {
				selected, selectedMatch = record, match
			}
		}

		var candidate *inferencev1alpha1.ModelGroupSpec
		if target != nil && preparationGroupMatches(*target, pool.Spec.Template) {
			candidate = target
		} else if selected == nil {
			for _, revision := range []string{pool.Status.PreparedRevision, servingconfig.ServingRevision(service, pool)} {
				if revision == "" {
					continue
				}
				for index := range groups {
					group := &groups[index]
					if group.Spec.Revision == revision && preparationGroupMatches(group.Spec, pool.Spec.Template) {
						candidate = &group.Spec
						break
					}
				}
				if candidate != nil {
					break
				}
				spec := inferencev1alpha1.ModelGroupSpec{ModelPoolRef: inferencev1alpha1.LocalObjectReference{UID: string(pool.UID)}, Revision: revision}
				if record := owned[runtimeconfig.PreparationSourceName(spec)]; record != nil && record.DeletionTimestamp.IsZero() && record.Data[runtimeconfig.PreparationRecordKey] != "" {
					match, err := comparePreparationSourceInputs(record, pool.Spec.Template)
					if err != nil {
						return false, err
					}
					if match == preparationSourceInputsMatch {
						selected, selectedMatch = record, match
						break
					}
				}
			}
			// Drain completion order does not establish source intent.
			if candidate == nil && selected == nil {
				for index := range groups {
					group := &groups[index]
					if !group.DeletionTimestamp.IsZero() || !preparationGroupMatches(group.Spec, pool.Spec.Template) {
						continue
					}
					if candidate != nil && candidate.Revision != group.Spec.Revision {
						candidate = nil
						break
					}
					candidate = &group.Spec
				}
			}
		}
		if candidate != nil {
			record, err := reconcilePreparationRecord(ctx, reconciler.Client, reader, pool, *candidate)
			if err != nil {
				return false, err
			}
			match, err := comparePreparationSourceInputs(record, pool.Spec.Template)
			if err != nil {
				return false, err
			}
			if match == preparationSourceInputsChanged {
				return false, fmt.Errorf("model source ConfigMap %q does not match its cohort inputs", record.Name)
			}
			reserved = record
			// Keep confirmed matching snapshots until the target publishes; unknown authority is not a match.
			if selected == nil || selectedMatch != preparationSourceInputsMatch || match == preparationSourceInputsMatch {
				selected, selectedMatch = record, match
			}
		}
	}
	var reference *inferencev1alpha1.LocalObjectReference
	if selected != nil {
		reference = &inferencev1alpha1.LocalObjectReference{Name: selected.Name, UID: string(selected.UID)}
	}
	if !reflect.DeepEqual(pool.Status.ArtifactSource, reference) {
		base := pool.DeepCopy()
		pool.Status.ArtifactSource = reference
		if err := reconciler.Status().Patch(ctx, pool, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return false, fmt.Errorf("retain ModelPool artifact source: %w", err)
		}
	}

	pending := false
	for index := range records.Items {
		record := &records.Items[index]
		retained := reference != nil && reference.Name == record.Name && reference.UID == string(record.UID)
		retained = retained || reserved != nil && reserved.Name == record.Name && reserved.UID == record.UID
		if !metav1.IsControlledBy(record, pool) || live[record.Name] || retained {
			continue
		}
		// Retain matching or uncertain publications until a source is explicitly selected or deleted.
		// Unreferenced empty records can still be released.
		if retainIntent && reference == nil && reserved == nil && record.DeletionTimestamp.IsZero() && record.Data[runtimeconfig.PreparationRecordKey] != "" {
			match, err := comparePreparationSourceInputs(record, pool.Spec.Template)
			if err != nil {
				return false, err
			}
			if match != preparationSourceInputsChanged {
				continue
			}
		}
		pending = true
		if record.DeletionTimestamp.IsZero() {
			// Concurrent publication requires a fresh source selection before deletion.
			if err := client.IgnoreNotFound(reconciler.Delete(ctx, record, client.Preconditions{UID: &record.UID, ResourceVersion: &record.ResourceVersion})); err != nil {
				return false, fmt.Errorf("release model source record: %w", err)
			}
		}
	}
	return pending, nil
}

// preparationServiceIntentMatches detects input changes and removed Pools while suspension retains old templates.
// Revisions remain compiler-owned; stopping does not resolve startup settings or provider defaults.
func preparationServiceIntentMatches(pool *inferencev1alpha1.ModelPool, service *inferencev1alpha1.ModelService) bool {
	if service == nil || !service.DeletionTimestamp.IsZero() {
		return false
	}
	selected := len(service.Spec.ModelPools) == 0 && pool.Spec.PoolName == "default"
	for _, entry := range service.Spec.ModelPools {
		selected = selected || entry.Name == pool.Spec.PoolName
	}
	source := service.Spec.Source
	if source == "" {
		source = inferencev1alpha1.ModelSourceHF
	}
	tokenizer := service.Spec.Tokenizer
	if tokenizer == "" {
		tokenizer = service.Spec.Model
	}
	artifacts := runtimeconfig.ResolveModelArtifacts(pool.Spec.Template)
	return selected && source != inferencev1alpha1.ModelSourceLocal && artifacts.Model == service.Spec.Model && artifacts.Source == source && artifacts.Tokenizer == tokenizer
}

// preparationHuggingFaceEndpoint preserves the explicit setting without inferring a provider address.
func preparationHuggingFaceEndpoint(access *inferencev1alpha1.HuggingFaceAccess) string {
	if access == nil {
		return ""
	}
	return access.Endpoint
}

// preparationGroupMatches matches source-enabled Groups to current model and storage inputs.
func preparationGroupMatches(spec inferencev1alpha1.ModelGroupSpec, template inferencev1alpha1.NormalizedPoolTemplate) bool {
	artifacts := spec.Artifacts
	inputs := runtimeconfig.ProjectModelArtifacts(artifacts)
	if spec.Runtime.PreparationVersion == 0 || artifacts.Source == inferencev1alpha1.ModelSourceLocal || inputs != runtimeconfig.ResolveModelArtifacts(template) {
		return false
	}
	if artifacts.Source == inferencev1alpha1.ModelSourceHF && preparationHuggingFaceEndpoint(artifacts.HuggingFaceAccess) != preparationHuggingFaceEndpoint(template.HuggingFaceAccess) {
		return false
	}
	if artifacts.Cache == nil || template.RuntimeCache == nil {
		return artifacts.Cache == nil && template.RuntimeCache == nil
	}
	return artifacts.Cache.Directory == template.RuntimeCache.Directory && artifacts.Cache.ClaimName == template.RuntimeCache.ClaimName && artifacts.Cache.MountPath == template.RuntimeCache.MountPath
}

// comparePreparationSourceInputs classifies inputs without treating missing authority as a match.
func comparePreparationSourceInputs(record *corev1.ConfigMap, template inferencev1alpha1.NormalizedPoolTemplate) (preparationSourceInputMatch, error) {
	match := preparationSourceInputsUnknown
	if encoded := record.Data[runtimeconfig.PreparationRecordKey]; encoded != "" {
		var source struct {
			Version           int                          `json:"version"`
			Artifacts         runtimeconfig.ModelArtifacts `json:"artifacts"`
			ModelRevision     string                       `json:"model_revision"`
			TokenizerRevision string                       `json:"tokenizer_revision"`
		}
		if err := json.Unmarshal([]byte(encoded), &source); err != nil {
			return match, fmt.Errorf("decode model source record %q: %w", record.Name, err)
		}
		if source.Version != 1 || source.ModelRevision == "" || source.TokenizerRevision == "" {
			return match, fmt.Errorf("model source record %q requires version 1 and fixed model and tokenizer revisions", record.Name)
		}
		if source.Artifacts != runtimeconfig.ResolveModelArtifacts(template) {
			return preparationSourceInputsChanged, nil
		}
		match = preparationSourceInputsMatch
	}
	if template.Source == inferencev1alpha1.ModelSourceHF {
		if endpoint, known := record.Data[preparationHFEndpointKey]; known {
			if endpoint != preparationHuggingFaceEndpoint(template.HuggingFaceAccess) {
				return preparationSourceInputsChanged, nil
			}
		} else {
			match = preparationSourceInputsUnknown
		}
	}
	if encoded, bound := record.Data[runtimeconfig.ModelCacheReferenceKey]; bound {
		var reference runtimeconfig.ModelCacheReference
		if err := json.Unmarshal([]byte(encoded), &reference); err != nil {
			return match, fmt.Errorf("decode model source cache reference %q: %w", record.Name, err)
		}
		cache := template.RuntimeCache
		if cache == nil || cache.Directory != reference.Directory || cache.ClaimName != reference.ClaimName || cache.MountPath != reference.MountPath {
			return preparationSourceInputsChanged, nil
		}
	} else if template.RuntimeCache != nil && runtimeconfig.ModelPreparationVersion(template.Application, template.Backend) >= runtimeconfig.SharedModelCachePreparationVersion {
		return preparationSourceInputsChanged, nil
	}
	return match, nil
}

// mountPreparationSource projects the durable provider identity without API credentials.
func mountPreparationSource(group *inferencev1alpha1.ModelGroup, pod *corev1.PodTemplateSpec) {
	spec := group.Spec
	if spec.Runtime.PreparationVersion == 0 || spec.Artifacts.Source == inferencev1alpha1.ModelSourceLocal {
		return
	}
	items := []corev1.KeyToPath{{Key: runtimeconfig.PreparationRecordKey, Path: runtimeconfig.PreparationRecordKey}}
	if spec.Artifacts.Cache != nil && spec.Runtime.PreparationVersion >= runtimeconfig.SharedModelCachePreparationVersion {
		items = append(items, corev1.KeyToPath{Key: "cache-root", Path: "cache-root"})
	}
	pod.Spec.Volumes = append(pod.Spec.Volumes, corev1.Volume{
		Name: "model-source", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: runtimeconfig.PreparationSourceName(spec)},
			Items:                items,
		}},
	})
	container := &pod.Spec.Containers[0]
	container.VolumeMounts = append(container.VolumeMounts, corev1.VolumeMount{Name: "model-source", MountPath: preparationRecordDirectory, ReadOnly: true})
	container.Env = append(container.Env, corev1.EnvVar{Name: preparationRecordEnv, Value: preparationRecordDirectory + "/" + runtimeconfig.PreparationRecordKey})
}
