/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package v1alpha1

import (
	"context"

	ctrl "sigs.k8s.io/controller-runtime"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	autoscalingv1alpha1 "github.com/foretoken/foretoken/control-plane/api/autoscaling/v1alpha1"
)

// nolint:unused
// log is for logging in this package.
var inferenceautoscalerlog = logf.Log.WithName("inferenceautoscaler-resource")

// SetupInferenceAutoscalerWebhookWithManager registers the webhook for InferenceAutoscaler in the manager.
func SetupInferenceAutoscalerWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr, &autoscalingv1alpha1.InferenceAutoscaler{}).
		WithValidator(&InferenceAutoscalerCustomValidator{}).
		WithDefaulter(&InferenceAutoscalerCustomDefaulter{}).
		Complete()
}

// TODO(user): EDIT THIS FILE!  THIS IS SCAFFOLDING FOR YOU TO OWN!

// +kubebuilder:webhook:path=/mutate-autoscaling-foretoken-ai-v1alpha1-inferenceautoscaler,mutating=true,failurePolicy=fail,sideEffects=None,groups=autoscaling.foretoken.ai,resources=inferenceautoscalers,verbs=create;update,versions=v1alpha1,name=minferenceautoscaler-v1alpha1.kb.io,admissionReviewVersions=v1

// InferenceAutoscalerCustomDefaulter struct is responsible for setting default values on the custom resource of the
// Kind InferenceAutoscaler when those are created or updated.
//
// NOTE: The +kubebuilder:object:generate=false marker prevents controller-gen from generating DeepCopy methods,
// as it is used only for temporary operations and does not need to be deeply copied.
type InferenceAutoscalerCustomDefaulter struct {
	// TODO(user): Add more fields as needed for defaulting
}

// Default implements webhook.CustomDefaulter so a webhook will be registered for the Kind InferenceAutoscaler.
func (d *InferenceAutoscalerCustomDefaulter) Default(_ context.Context, obj *autoscalingv1alpha1.InferenceAutoscaler) error {
	inferenceautoscalerlog.Info("Defaulting for InferenceAutoscaler", "name", obj.GetName())

	// TODO(user): fill in your defaulting logic.

	return nil
}

// TODO(user): change verbs to "verbs=create;update;delete" if you want to enable deletion validation.
// NOTE: If you want to customise the 'path', use the flags '--defaulting-path' or '--validation-path'.
// +kubebuilder:webhook:path=/validate-autoscaling-foretoken-ai-v1alpha1-inferenceautoscaler,mutating=false,failurePolicy=fail,sideEffects=None,groups=autoscaling.foretoken.ai,resources=inferenceautoscalers,verbs=create;update,versions=v1alpha1,name=vinferenceautoscaler-v1alpha1.kb.io,admissionReviewVersions=v1

// InferenceAutoscalerCustomValidator struct is responsible for validating the InferenceAutoscaler resource
// when it is created, updated, or deleted.
//
// NOTE: The +kubebuilder:object:generate=false marker prevents controller-gen from generating DeepCopy methods,
// as this struct is used only for temporary operations and does not need to be deeply copied.
type InferenceAutoscalerCustomValidator struct {
	// TODO(user): Add more fields as needed for validation
}

// ValidateCreate implements webhook.CustomValidator so a webhook will be registered for the type InferenceAutoscaler.
func (v *InferenceAutoscalerCustomValidator) ValidateCreate(_ context.Context, obj *autoscalingv1alpha1.InferenceAutoscaler) (admission.Warnings, error) {
	inferenceautoscalerlog.Info("Validation for InferenceAutoscaler upon creation", "name", obj.GetName())

	// TODO(user): fill in your validation logic upon object creation.

	return nil, nil
}

// ValidateUpdate implements webhook.CustomValidator so a webhook will be registered for the type InferenceAutoscaler.
func (v *InferenceAutoscalerCustomValidator) ValidateUpdate(_ context.Context, oldObj, newObj *autoscalingv1alpha1.InferenceAutoscaler) (admission.Warnings, error) {
	inferenceautoscalerlog.Info("Validation for InferenceAutoscaler upon update", "name", newObj.GetName())

	// TODO(user): fill in your validation logic upon object update.

	return nil, nil
}

// ValidateDelete implements webhook.CustomValidator so a webhook will be registered for the type InferenceAutoscaler.
func (v *InferenceAutoscalerCustomValidator) ValidateDelete(_ context.Context, obj *autoscalingv1alpha1.InferenceAutoscaler) (admission.Warnings, error) {
	inferenceautoscalerlog.Info("Validation for InferenceAutoscaler upon deletion", "name", obj.GetName())

	// TODO(user): fill in your validation logic upon object deletion.

	return nil, nil
}
