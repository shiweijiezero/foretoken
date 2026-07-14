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

	modelv1alpha1 "github.com/foretoken/foretoken/control-plane/api/model/v1alpha1"
)

// nolint:unused
// log is for logging in this package.
var modelservicelog = logf.Log.WithName("modelservice-resource")

// SetupModelServiceWebhookWithManager registers the webhook for ModelService in the manager.
func SetupModelServiceWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr, &modelv1alpha1.ModelService{}).
		WithValidator(&ModelServiceCustomValidator{}).
		WithDefaulter(&ModelServiceCustomDefaulter{}).
		Complete()
}

// TODO(user): EDIT THIS FILE!  THIS IS SCAFFOLDING FOR YOU TO OWN!

// +kubebuilder:webhook:path=/mutate-model-foretoken-ai-v1alpha1-modelservice,mutating=true,failurePolicy=fail,sideEffects=None,groups=model.foretoken.ai,resources=modelservices,verbs=create;update,versions=v1alpha1,name=mmodelservice-v1alpha1.kb.io,admissionReviewVersions=v1

// ModelServiceCustomDefaulter struct is responsible for setting default values on the custom resource of the
// Kind ModelService when those are created or updated.
//
// NOTE: The +kubebuilder:object:generate=false marker prevents controller-gen from generating DeepCopy methods,
// as it is used only for temporary operations and does not need to be deeply copied.
type ModelServiceCustomDefaulter struct {
	// TODO(user): Add more fields as needed for defaulting
}

// Default implements webhook.CustomDefaulter so a webhook will be registered for the Kind ModelService.
func (d *ModelServiceCustomDefaulter) Default(_ context.Context, obj *modelv1alpha1.ModelService) error {
	modelservicelog.Info("Defaulting for ModelService", "name", obj.GetName())

	// TODO(user): fill in your defaulting logic.

	return nil
}

// TODO(user): change verbs to "verbs=create;update;delete" if you want to enable deletion validation.
// NOTE: If you want to customise the 'path', use the flags '--defaulting-path' or '--validation-path'.
// +kubebuilder:webhook:path=/validate-model-foretoken-ai-v1alpha1-modelservice,mutating=false,failurePolicy=fail,sideEffects=None,groups=model.foretoken.ai,resources=modelservices,verbs=create;update,versions=v1alpha1,name=vmodelservice-v1alpha1.kb.io,admissionReviewVersions=v1

// ModelServiceCustomValidator struct is responsible for validating the ModelService resource
// when it is created, updated, or deleted.
//
// NOTE: The +kubebuilder:object:generate=false marker prevents controller-gen from generating DeepCopy methods,
// as this struct is used only for temporary operations and does not need to be deeply copied.
type ModelServiceCustomValidator struct {
	// TODO(user): Add more fields as needed for validation
}

// ValidateCreate implements webhook.CustomValidator so a webhook will be registered for the type ModelService.
func (v *ModelServiceCustomValidator) ValidateCreate(_ context.Context, obj *modelv1alpha1.ModelService) (admission.Warnings, error) {
	modelservicelog.Info("Validation for ModelService upon creation", "name", obj.GetName())

	// TODO(user): fill in your validation logic upon object creation.

	return nil, nil
}

// ValidateUpdate implements webhook.CustomValidator so a webhook will be registered for the type ModelService.
func (v *ModelServiceCustomValidator) ValidateUpdate(_ context.Context, oldObj, newObj *modelv1alpha1.ModelService) (admission.Warnings, error) {
	modelservicelog.Info("Validation for ModelService upon update", "name", newObj.GetName())

	// TODO(user): fill in your validation logic upon object update.

	return nil, nil
}

// ValidateDelete implements webhook.CustomValidator so a webhook will be registered for the type ModelService.
func (v *ModelServiceCustomValidator) ValidateDelete(_ context.Context, obj *modelv1alpha1.ModelService) (admission.Warnings, error) {
	modelservicelog.Info("Validation for ModelService upon deletion", "name", obj.GetName())

	// TODO(user): fill in your validation logic upon object deletion.

	return nil, nil
}
