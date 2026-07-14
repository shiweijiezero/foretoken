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

	orchestrationv1alpha1 "github.com/foretoken/foretoken/control-plane/api/orchestration/v1alpha1"
)

// nolint:unused
// log is for logging in this package.
var inferencepoollog = logf.Log.WithName("inferencepool-resource")

// SetupInferencePoolWebhookWithManager registers the webhook for InferencePool in the manager.
func SetupInferencePoolWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr, &orchestrationv1alpha1.InferencePool{}).
		WithValidator(&InferencePoolCustomValidator{}).
		WithDefaulter(&InferencePoolCustomDefaulter{}).
		Complete()
}

// TODO(user): EDIT THIS FILE!  THIS IS SCAFFOLDING FOR YOU TO OWN!

// +kubebuilder:webhook:path=/mutate-orchestration-foretoken-ai-v1alpha1-inferencepool,mutating=true,failurePolicy=fail,sideEffects=None,groups=orchestration.foretoken.ai,resources=inferencepools,verbs=create;update,versions=v1alpha1,name=minferencepool-v1alpha1.kb.io,admissionReviewVersions=v1

// InferencePoolCustomDefaulter struct is responsible for setting default values on the custom resource of the
// Kind InferencePool when those are created or updated.
//
// NOTE: The +kubebuilder:object:generate=false marker prevents controller-gen from generating DeepCopy methods,
// as it is used only for temporary operations and does not need to be deeply copied.
type InferencePoolCustomDefaulter struct {
	// TODO(user): Add more fields as needed for defaulting
}

// Default implements webhook.CustomDefaulter so a webhook will be registered for the Kind InferencePool.
func (d *InferencePoolCustomDefaulter) Default(_ context.Context, obj *orchestrationv1alpha1.InferencePool) error {
	inferencepoollog.Info("Defaulting for InferencePool", "name", obj.GetName())

	// TODO(user): fill in your defaulting logic.

	return nil
}

// TODO(user): change verbs to "verbs=create;update;delete" if you want to enable deletion validation.
// NOTE: If you want to customise the 'path', use the flags '--defaulting-path' or '--validation-path'.
// +kubebuilder:webhook:path=/validate-orchestration-foretoken-ai-v1alpha1-inferencepool,mutating=false,failurePolicy=fail,sideEffects=None,groups=orchestration.foretoken.ai,resources=inferencepools,verbs=create;update,versions=v1alpha1,name=vinferencepool-v1alpha1.kb.io,admissionReviewVersions=v1

// InferencePoolCustomValidator struct is responsible for validating the InferencePool resource
// when it is created, updated, or deleted.
//
// NOTE: The +kubebuilder:object:generate=false marker prevents controller-gen from generating DeepCopy methods,
// as this struct is used only for temporary operations and does not need to be deeply copied.
type InferencePoolCustomValidator struct {
	// TODO(user): Add more fields as needed for validation
}

// ValidateCreate implements webhook.CustomValidator so a webhook will be registered for the type InferencePool.
func (v *InferencePoolCustomValidator) ValidateCreate(_ context.Context, obj *orchestrationv1alpha1.InferencePool) (admission.Warnings, error) {
	inferencepoollog.Info("Validation for InferencePool upon creation", "name", obj.GetName())

	// TODO(user): fill in your validation logic upon object creation.

	return nil, nil
}

// ValidateUpdate implements webhook.CustomValidator so a webhook will be registered for the type InferencePool.
func (v *InferencePoolCustomValidator) ValidateUpdate(_ context.Context, oldObj, newObj *orchestrationv1alpha1.InferencePool) (admission.Warnings, error) {
	inferencepoollog.Info("Validation for InferencePool upon update", "name", newObj.GetName())

	// TODO(user): fill in your validation logic upon object update.

	return nil, nil
}

// ValidateDelete implements webhook.CustomValidator so a webhook will be registered for the type InferencePool.
func (v *InferencePoolCustomValidator) ValidateDelete(_ context.Context, obj *orchestrationv1alpha1.InferencePool) (admission.Warnings, error) {
	inferencepoollog.Info("Validation for InferencePool upon deletion", "name", obj.GetName())

	// TODO(user): fill in your validation logic upon object deletion.

	return nil, nil
}
