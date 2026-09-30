// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Retains resolved source versions independently of the runtime cache's storage lifecycle.

package controllers

import (
	"context"
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const (
	preparationConfigMapEnv      = "FORETOKEN_MODEL_PREPARATION_CONFIGMAP"
	preparationNamespaceEnv      = "FORETOKEN_MODEL_PREPARATION_NAMESPACE"
	preparationRecordEnv         = "FORETOKEN_MODEL_PREPARATION_RECORD"
	preparationRecordDirectory   = "/etc/foretoken/model-source"
	preparationRecordKey         = "source.json"
	preparationPoolLabel         = "inference.foretoken.io/preparation-pool"
	preparationSourceReadyReason = "SourcePrepared"
)

func preparationSourceName(spec inferencev1alpha1.ModelGroupSpec) string {
	return "model-source-" + spec.ModelPoolRef.UID + "-" + spec.Revision
}

// reconcilePreparationSource gives acquisition workers access to one Pool revision's source record.
// Workers publish resolved provider versions with a resourceVersion precondition; reconciliation
// never rewrites that data. Pool ownership preserves the record across replica and PVC replacement.
func (reconciler *ModelGroupReconciler) reconcilePreparationSource(ctx context.Context, group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool) error {
	name := preparationSourceName(group.Spec)
	metadata := metav1.ObjectMeta{Name: name, Namespace: group.Namespace, Labels: map[string]string{preparationPoolLabel: string(pool.UID)}}
	automount := false
	objects := []client.Object{
		&corev1.ConfigMap{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"}, ObjectMeta: metadata},
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
	var owner client.Object = pool
	for _, desired := range objects {
		if err := controllerutil.SetControllerReference(owner, desired, reconciler.Scheme()); err != nil {
			return err
		}
		current := desired.DeepCopyObject().(client.Object)
		if err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current); apierrors.IsNotFound(err) {
			if err := reconciler.Create(ctx, desired); err != nil {
				return fmt.Errorf("create model source %s: %w", desired.GetObjectKind().GroupVersionKind().Kind, err)
			}
			if _, record := desired.(*corev1.ConfigMap); record {
				owner = desired
			}
			continue
		} else if err != nil {
			return err
		}
		if !metav1.IsControlledBy(current, owner) {
			return fmt.Errorf("model source %s %q belongs to another owner", desired.GetObjectKind().GroupVersionKind().Kind, name)
		}
		if _, record := desired.(*corev1.ConfigMap); record {
			owner = current
			continue
		}
		if err := reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner(modelGroupFieldOwner), client.ForceOwnership); err != nil {
			return fmt.Errorf("apply model source permissions: %w", err)
		}
	}
	return nil
}

// cleanupPreparationSources releases records and their dependent permissions after the last cohort is gone.
func (reconciler *ModelPoolReconciler) cleanupPreparationSources(ctx context.Context, pool *inferencev1alpha1.ModelPool) error {
	groups, err := ownedModelGroups(ctx, reconciler.APIReader, pool)
	if err != nil {
		return err
	}
	live := make(map[string]bool, len(groups))
	for _, group := range groups {
		live[preparationSourceName(group.Spec)] = true
	}
	var records corev1.ConfigMapList
	if err := reconciler.List(ctx, &records, client.InNamespace(pool.Namespace), client.MatchingLabels{preparationPoolLabel: string(pool.UID)}); err != nil {
		return err
	}
	for index := range records.Items {
		record := &records.Items[index]
		if live[record.Name] || !metav1.IsControlledBy(record, pool) {
			continue
		}
		if err := client.IgnoreNotFound(reconciler.Delete(ctx, record, client.Preconditions{UID: &record.UID})); err != nil {
			return err
		}
	}
	return nil
}

// mountPreparationSource projects the durable provider identity without API credentials.
func mountPreparationSource(group *inferencev1alpha1.ModelGroup, pod *corev1.PodTemplateSpec) {
	spec := group.Spec
	if spec.Runtime.PreparationVersion == 0 || spec.Artifacts.Source == inferencev1alpha1.ModelSourceLocal {
		return
	}
	pod.Spec.Volumes = append(pod.Spec.Volumes, corev1.Volume{
		Name: "model-source", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: preparationSourceName(spec)},
			Items:                []corev1.KeyToPath{{Key: preparationRecordKey, Path: preparationRecordKey}},
		}},
	})
	container := &pod.Spec.Containers[0]
	container.VolumeMounts = append(container.VolumeMounts, corev1.VolumeMount{Name: "model-source", MountPath: preparationRecordDirectory, ReadOnly: true})
	container.Env = append(container.Env, corev1.EnvVar{Name: preparationRecordEnv, Value: preparationRecordDirectory + "/" + preparationRecordKey})
}
