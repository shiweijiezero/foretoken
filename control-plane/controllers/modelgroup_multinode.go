// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package controllers

import (
	"context"
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

// modelGroupLeaderWorkerSetName reserves a separate headless Service name for LWS
// and leaves room for worker ordinals and ControllerRevision label suffixes.
func modelGroupLeaderWorkerSetName(group *inferencev1alpha1.ModelGroup) string {
	return "mg-" + string(group.UID)
}

// reconcileWorkload keeps complete Group availability independent of the Pod orchestration backend.
func (reconciler *ModelGroupReconciler) reconcileWorkload(ctx context.Context, group *inferencev1alpha1.ModelGroup) (bool, error) {
	if group.Spec.NodeCount == 1 {
		deployment, err := reconciler.reconcileDeployment(ctx, group)
		if err != nil {
			return false, err
		}
		return modelGroupDeploymentAvailable(deployment), nil
	}
	workloadName := modelGroupLeaderWorkerSetName(group)
	if workloadName != group.Name {
		// Foreground deletion keeps the old group from holding GPUs when its
		// replacement starts, including when LWS never created every member.
		previous := new(lwsv1.LeaderWorkerSet)
		err := reconciler.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: group.Name}, previous)
		if err == nil {
			if !metav1.IsControlledBy(previous, group) {
				return false, fmt.Errorf("LeaderWorkerSet %q is not controlled by ModelGroup", previous.Name)
			}
			if previous.DeletionTimestamp.IsZero() {
				if err := reconciler.Delete(ctx, previous, client.PropagationPolicy(metav1.DeletePropagationForeground), client.Preconditions{UID: &previous.UID}); client.IgnoreNotFound(err) != nil {
					return false, fmt.Errorf("delete superseded LeaderWorkerSet: %w", err)
				}
			}
			return false, nil
		}
		if !apierrors.IsNotFound(err) {
			return false, fmt.Errorf("get superseded LeaderWorkerSet: %w", err)
		}
	}
	deployment, err := desiredDeployment(group, reconciler.ImagePullSecrets)
	if err != nil {
		return false, err
	}
	member := deployment.Spec.Template
	member.Spec.Affinity = &corev1.Affinity{PodAntiAffinity: &corev1.PodAntiAffinity{
		RequiredDuringSchedulingIgnoredDuringExecution: []corev1.PodAffinityTerm{{
			LabelSelector: &metav1.LabelSelector{MatchLabels: modelGroupLabels(group)},
			TopologyKey:   corev1.LabelHostname,
		}},
	}}
	member.Spec.Containers[0].Env = append(member.Spec.Containers[0].Env,
		corev1.EnvVar{Name: "FORETOKEN_MEMBER_IP", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "status.podIP"}}},
	)
	one := int32(1)
	desired := &lwsv1.LeaderWorkerSet{
		TypeMeta:   metav1.TypeMeta{APIVersion: lwsv1.GroupVersion.String(), Kind: "LeaderWorkerSet"},
		ObjectMeta: metav1.ObjectMeta{Name: workloadName, Namespace: group.Namespace, Labels: modelGroupLabels(group)},
		Spec: lwsv1.LeaderWorkerSetSpec{
			Replicas:      &one,
			StartupPolicy: lwsv1.LeaderCreatedStartupPolicy,
			LeaderWorkerTemplate: lwsv1.LeaderWorkerTemplate{
				Size:           &group.Spec.MemberCount,
				LeaderTemplate: member.DeepCopy(),
				WorkerTemplate: member,
				RestartPolicy:  lwsv1.RecreateGroupOnPodRestart,
			},
		},
	}
	if err := controllerutil.SetControllerReference(group, desired, reconciler.Scheme()); err != nil {
		return false, fmt.Errorf("set LeaderWorkerSet owner: %w", err)
	}
	current := new(lwsv1.LeaderWorkerSet)
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current); err == nil {
		if !metav1.IsControlledBy(current, group) {
			return false, fmt.Errorf("LeaderWorkerSet %q is not controlled by ModelGroup", current.Name)
		}
	} else if !apierrors.IsNotFound(err) {
		return false, fmt.Errorf("get LeaderWorkerSet: %w", err)
	}
	if err := reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner(modelGroupFieldOwner), client.ForceOwnership); err != nil {
		return false, fmt.Errorf("apply LeaderWorkerSet: %w", err)
	}
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current); err != nil {
		return false, fmt.Errorf("get applied LeaderWorkerSet: %w", err)
	}
	if current.Status.ObservedGeneration != current.Generation || current.Status.ReadyReplicas != 1 ||
		!meta.IsStatusConditionTrue(current.Status.Conditions, string(lwsv1.LeaderWorkerSetAvailable)) {
		return false, nil
	}
	// Pod events can arrive before LWS has withdrawn its Available condition.
	var pods corev1.PodList
	if err := reconciler.List(ctx, &pods, client.InNamespace(group.Namespace), client.MatchingLabels(modelGroupLabels(group))); err != nil {
		return false, fmt.Errorf("list Group members: %w", err)
	}
	if len(pods.Items) != int(group.Spec.MemberCount) {
		return false, nil
	}
	for index := range pods.Items {
		if !pods.Items[index].DeletionTimestamp.IsZero() || !podReady(&pods.Items[index]) {
			return false, nil
		}
	}
	return true, nil
}
