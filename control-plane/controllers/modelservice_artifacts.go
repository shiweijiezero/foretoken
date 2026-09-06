// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Materializes one ModelService Hugging Face snapshot before execution Pools are changed.

package controllers

import (
	"context"
	"fmt"
	"reflect"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const (
	conditionArtifactsReady = "ArtifactsReady"
	modelCacheVolumeName    = "model-cache"
	modelCacheHomeEnv       = "HF_HOME"
	modelCacheOfflineEnv    = "HF_HUB_OFFLINE"
)

// ModelArtifactProfile configures an existing namespace-local cache used by preparation Jobs and serving Pods.
type ModelArtifactProfile struct {
	Image            string
	ClaimName        string
	MountPath        string
	Offline          bool
	Endpoint         string
	TokenSecretName  string
	TokenSecretKey   string
	ImagePullSecrets []corev1.LocalObjectReference
}

// Enabled reports whether the platform configured managed model snapshot preparation.
func (profile ModelArtifactProfile) Enabled() bool {
	return profile.ClaimName != ""
}

// ServingCache returns the persistent cache contract installed into prepared Pool templates.
func (profile ModelArtifactProfile) ServingCache() *inferencev1alpha1.ModelArtifactCache {
	if !profile.Enabled() {
		return nil
	}
	return &inferencev1alpha1.ModelArtifactCache{ClaimName: profile.ClaimName, MountPath: profile.MountPath}
}

// Validate rejects incomplete platform-owned cache and Hugging Face settings.
func (profile ModelArtifactProfile) Validate() error {
	if !profile.Enabled() {
		if profile.Offline || profile.Endpoint != "" || profile.TokenSecretName != "" || profile.TokenSecretKey != "" {
			return fmt.Errorf("model cache claim is required when model artifact settings are configured")
		}
		return nil
	}
	if profile.Image == "" {
		return fmt.Errorf("model artifact preparation image is required")
	}
	if profile.MountPath == "" || !strings.HasPrefix(profile.MountPath, "/") {
		return fmt.Errorf("model cache mount path must be absolute")
	}
	if (profile.TokenSecretName == "") != (profile.TokenSecretKey == "") {
		return fmt.Errorf("Hugging Face token Secret name and key must be configured together")
	}
	return nil
}

type modelArtifactState struct {
	Ready   bool
	Reason  string
	Message string
}

func modelArtifactJobName(service *inferencev1alpha1.ModelService) string {
	uid := strings.ReplaceAll(string(service.UID), "-", "")
	return "model-artifacts-" + uid
}

// reconcileModelArtifacts creates or observes the one preparation Job shared by all Pools of a ModelService.
func (reconciler *ModelServiceReconciler) reconcileModelArtifacts(ctx context.Context, service *inferencev1alpha1.ModelService, pools []compiler.ModelPool) (modelArtifactState, error) {
	profile := reconciler.ArtifactProfile
	if err := profile.Validate(); err != nil {
		return modelArtifactState{}, err
	}
	if !profile.Enabled() {
		return modelArtifactState{Ready: true, Reason: "NotRequired", Message: "Managed model artifact preparation is disabled"}, nil
	}
	if len(pools) == 0 {
		return modelArtifactState{Ready: true, Reason: "NotRequired", Message: "ModelService has no execution Pools"}, nil
	}
	requested := false
	for _, pool := range pools {
		requested = requested || pool.DesiredGroups > 0
	}
	if !requested {
		return modelArtifactState{Ready: true, Reason: "NotRequired", Message: "ModelService has no requested serving capacity"}, nil
	}
	desired, err := desiredModelArtifactJob(service, pools[0].Template, profile)
	if err != nil {
		return modelArtifactState{}, err
	}
	if err := controllerutil.SetControllerReference(service, desired, reconciler.Scheme()); err != nil {
		return modelArtifactState{}, fmt.Errorf("set model artifact Job owner: %w", err)
	}
	current := new(batchv1.Job)
	key := client.ObjectKeyFromObject(desired)
	if err := reconciler.Get(ctx, key, current); apierrors.IsNotFound(err) {
		if err := reconciler.Create(ctx, desired); err != nil {
			return modelArtifactState{}, fmt.Errorf("create model artifact Job: %w", err)
		}
		return modelArtifactState{Reason: "Preparing", Message: "Model artifacts are being prepared"}, nil
	} else if err != nil {
		return modelArtifactState{}, fmt.Errorf("get model artifact Job: %w", err)
	}
	if !metav1.IsControlledBy(current, service) {
		return modelArtifactState{}, fmt.Errorf("model artifact Job %q is not controlled by ModelService", current.Name)
	}
	if !modelArtifactJobMatches(current, desired) {
		if err := reconciler.Delete(ctx, current); err != nil {
			return modelArtifactState{}, fmt.Errorf("replace model artifact Job: %w", err)
		}
		return modelArtifactState{Reason: "Preparing", Message: "Model artifact inputs changed; replacing the preparation Job"}, nil
	}
	for _, condition := range current.Status.Conditions {
		if condition.Status != corev1.ConditionTrue {
			continue
		}
		switch condition.Type {
		case batchv1.JobComplete:
			return modelArtifactState{Ready: true, Reason: "Prepared", Message: "Model artifacts are available in the shared cache"}, nil
		case batchv1.JobFailed:
			return modelArtifactState{Reason: "PreparationFailed", Message: "Model artifact preparation failed"}, nil
		}
	}
	return modelArtifactState{Reason: "Preparing", Message: "Model artifacts are being prepared"}, nil
}

func modelArtifactJobMatches(current, desired *batchv1.Job) bool {
	currentPod, desiredPod := current.Spec.Template.Spec, desired.Spec.Template.Spec
	return reflect.DeepEqual(currentPod.Containers, desiredPod.Containers) &&
		reflect.DeepEqual(currentPod.Volumes, desiredPod.Volumes) &&
		reflect.DeepEqual(currentPod.ImagePullSecrets, desiredPod.ImagePullSecrets)
}

func desiredModelArtifactJob(service *inferencev1alpha1.ModelService, template inferencev1alpha1.NormalizedPoolTemplate, profile ModelArtifactProfile) (*batchv1.Job, error) {
	if template.Model == "" || template.ModelRevision == "" || template.Tokenizer == "" || template.TokenizerRevision == "" {
		return nil, fmt.Errorf("model artifact identity is incomplete")
	}
	automountToken := false
	allowPrivilegeEscalation := false
	args := []string{
		"--model", template.Model,
		"--model-revision", template.ModelRevision,
		"--tokenizer", template.Tokenizer,
		"--tokenizer-revision", template.TokenizerRevision,
	}
	if profile.Offline {
		args = append(args, "--offline")
	}
	env := []corev1.EnvVar{{Name: modelCacheHomeEnv, Value: profile.MountPath}}
	if profile.Endpoint != "" {
		env = append(env, corev1.EnvVar{Name: "HF_ENDPOINT", Value: profile.Endpoint})
	}
	if profile.TokenSecretName != "" {
		env = append(env, corev1.EnvVar{Name: "HF_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: profile.TokenSecretName},
			Key:                  profile.TokenSecretKey,
		}}})
	}
	labels := map[string]string{"inference.foretoken.io/model-service": service.Name}
	return &batchv1.Job{
		TypeMeta:   metav1.TypeMeta{APIVersion: batchv1.SchemeGroupVersion.String(), Kind: "Job"},
		ObjectMeta: metav1.ObjectMeta{Name: modelArtifactJobName(service), Namespace: service.Namespace, Labels: labels},
		Spec: batchv1.JobSpec{Template: corev1.PodTemplateSpec{
			ObjectMeta: metav1.ObjectMeta{Labels: labels},
			Spec: corev1.PodSpec{
				AutomountServiceAccountToken: &automountToken,
				RestartPolicy:                corev1.RestartPolicyNever,
				SecurityContext: &corev1.PodSecurityContext{
					SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
				},
				ImagePullSecrets: append([]corev1.LocalObjectReference(nil), profile.ImagePullSecrets...),
				Volumes:          []corev1.Volume{{Name: modelCacheVolumeName, VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: profile.ClaimName}}}},
				Containers: []corev1.Container{{
					Name:                     "prepare",
					Image:                    profile.Image,
					ImagePullPolicy:          corev1.PullIfNotPresent,
					Command:                  []string{"foretoken-prepare-hf-snapshot"},
					Args:                     args,
					Env:                      env,
					VolumeMounts:             []corev1.VolumeMount{{Name: modelCacheVolumeName, MountPath: profile.MountPath}},
					TerminationMessagePath:   corev1.TerminationMessagePathDefault,
					TerminationMessagePolicy: corev1.TerminationMessageReadFile,
					SecurityContext: &corev1.SecurityContext{
						AllowPrivilegeEscalation: &allowPrivilegeEscalation,
						Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
					},
				}},
			},
		}},
	}, nil
}

// servingStateWhileArtifactsPrepare rechecks the selected generation instead of preserving stale readiness.
func (reconciler *ModelServiceReconciler) servingStateWhileArtifactsPrepare(ctx context.Context, service *inferencev1alpha1.ModelService, pools []compiler.ModelPool) (conditionState, error) {
	ready, reason, message, err := reconciler.serviceReadiness(ctx, service, pools)
	if err != nil || !ready {
		return conditionState{conditionStatus(ready), reason, message}, err
	}
	return conditionState{metav1.ConditionTrue, "ServingPreviousGeneration", "The previous complete ModelService generation remains ready while model artifacts are preparing"}, nil
}
