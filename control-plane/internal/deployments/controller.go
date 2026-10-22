// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Selects instance controllers, repairs their resources, and publishes their readiness.
package deployments

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"reflect"
	"slices"
	"sync"
	"time"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
)

const (
	deploymentFinalizer      = "inference.foretoken.io/deployment-protection"
	deploymentNameAnnotation = "inference.foretoken.io/deployment-name"
	controllerComponent      = "deployment-controller"
	selectionKey             = "controller-selection.json"
	platformUIDLabel         = "inference.foretoken.io/platform-uid"
)

// Reconciler materializes independently selected controllers from the installed platform template.
// It keeps each controller alive until its serving resources and model references have retired.
type Reconciler struct {
	client.Client
	APIReader     client.Reader
	Configuration client.ObjectKey
	leaders       sync.Map
}

// controllerSelection retains the actual template chosen by an explicit deployment request.
// Reconciliation and recovery do not silently adopt later platform defaults.
type controllerSelection struct {
	UpdateID                 string                            `json:"updateID"`
	ControllerSourceRevision string                            `json:"controllerSourceRevision,omitempty"`
	PlatformUID              types.UID                         `json:"platformUID"`
	Template                 Template                          `json:"template"`
	Resources                []api.DeploymentResourceReference `json:"resources,omitempty"`
}

// SetupWithManager observes deployment intent and loss or rollout of its controller resources.
func (r *Reconciler) SetupWithManager(manager ctrl.Manager) error {
	r.APIReader = manager.GetAPIReader()
	return ctrl.NewControllerManagedBy(manager).
		For(&api.InferenceDeployment{}).
		Watches(&appsv1.Deployment{}, handler.EnqueueRequestsFromMapFunc(r.deploymentRequests)).
		Watches(&corev1.ConfigMap{}, handler.EnqueueRequestsFromMapFunc(r.deploymentRequests)).
		Complete(r)
}

func (r *Reconciler) deploymentRequests(_ context.Context, object client.Object) []ctrl.Request {
	name := object.GetAnnotations()[deploymentNameAnnotation]
	if name == "" || object.GetLabels()["app.kubernetes.io/component"] != controllerComponent {
		return nil
	}
	return []ctrl.Request{{NamespacedName: client.ObjectKey{Namespace: object.GetNamespace(), Name: name}}}
}

// Reconcile selects only on explicit requests, repairs materialized resources, and publishes controller readiness.
func (r *Reconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	deployment := new(api.InferenceDeployment)
	if err := r.APIReader.Get(ctx, request.NamespacedName, deployment); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !deployment.DeletionTimestamp.IsZero() {
		return r.retire(ctx, deployment)
	}
	platform := new(corev1.ConfigMap)
	if err := r.APIReader.Get(ctx, r.Configuration, platform); apierrors.IsNotFound(err) {
		return ctrl.Result{}, r.publishStatus(ctx, deployment, true, metav1.ConditionFalse, "", "PlatformUnavailable", "The Foretoken platform is not installed")
	} else if err != nil {
		return ctrl.Result{}, err
	}
	if platform.Annotations["inference.foretoken.io/retiring"] == "true" {
		return ctrl.Result{}, r.publishStatus(ctx, deployment, true, metav1.ConditionFalse, "", "PlatformRetiring", "The Foretoken platform is being uninstalled")
	}
	legacy, err := r.legacyController(ctx, deployment.Namespace)
	if err != nil {
		return ctrl.Result{}, err
	}
	if legacy != "" {
		return ctrl.Result{}, r.publishStatus(ctx, deployment, true, metav1.ConditionFalse, "", "LegacyControllerPresent", fmt.Sprintf("Controller %s manages the whole namespace; retire its services and controller before creating deployment instances", legacy))
	}
	if !controllerutil.ContainsFinalizer(deployment, deploymentFinalizer) {
		base := deployment.DeepCopy()
		controllerutil.AddFinalizer(deployment, deploymentFinalizer)
		return ctrl.Result{Requeue: true}, r.Patch(ctx, deployment, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	}
	if deployment.Spec.UpdateID == "" {
		return ctrl.Result{}, r.publishStatus(ctx, deployment, false, metav1.ConditionUnknown, "", "AwaitingSubmission", "Waiting for the deployment's applications to be published")
	}
	selection, configuration, err := r.selectController(ctx, deployment, platform)
	if err != nil {
		return ctrl.Result{}, errors.Join(err, r.publishStatus(ctx, deployment, false, metav1.ConditionUnknown, "", "SelectionPending", err.Error()))
	}
	controller, err := r.materialize(ctx, deployment, selection, configuration)
	if err != nil {
		return ctrl.Result{}, errors.Join(err, r.publishStatus(ctx, deployment, true, metav1.ConditionUnknown, "", "ControllerPending", err.Error()))
	}
	if !controllerReady(controller) {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, r.publishStatus(ctx, deployment, true, metav1.ConditionUnknown, "", "ControllerPending", "Waiting for the selected controller to become ready")
	}
	leading, err := r.leaderReady(ctx, deployment, controller)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !leading {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, r.publishStatus(ctx, deployment, true, metav1.ConditionUnknown, "", "ControllerPending", "Waiting for the selected controller to acquire leadership")
	}
	revision := fmt.Sprintf("%s:%d", controller.UID, controller.Generation)
	return ctrl.Result{}, r.publishStatus(ctx, deployment, true, metav1.ConditionTrue, revision, "Ready", "The selected deployment controller is ready")
}

// selectController records a completed selection before changing its workload or access resources.
func (r *Reconciler) selectController(ctx context.Context, deployment *api.InferenceDeployment, platform *corev1.ConfigMap) (*controllerSelection, *corev1.ConfigMap, error) {
	configuration := new(corev1.ConfigMap)
	key := client.ObjectKey{Namespace: deployment.Namespace, Name: ResourceName(string(deployment.UID))}
	var previous *controllerSelection
	err := r.APIReader.Get(ctx, key, configuration)
	if err != nil && !apierrors.IsNotFound(err) {
		return nil, nil, err
	}
	if err == nil {
		if configuration.Labels[api.DeploymentUIDLabel] != string(deployment.UID) {
			return nil, nil, fmt.Errorf("controller configuration %s belongs to another deployment", key)
		}
		selection := new(controllerSelection)
		if err := json.Unmarshal([]byte(configuration.Data[selectionKey]), selection); err != nil {
			return nil, nil, fmt.Errorf("read selected controller: %w", err)
		}
		previous = selection
		if selection.UpdateID == deployment.Spec.UpdateID && selection.ControllerSourceRevision == deployment.Spec.ControllerSourceRevision {
			return selection, configuration, nil
		}
	} else if deployment.Status.ObservedGeneration == deployment.Generation && deployment.Status.ControllerRevision != "" {
		return nil, nil, fmt.Errorf("controller selection was removed; redeploy %q to select the installed defaults", deployment.Name)
	}

	selection := &controllerSelection{
		UpdateID:                 deployment.Spec.UpdateID,
		ControllerSourceRevision: deployment.Spec.ControllerSourceRevision,
		PlatformUID:              platform.UID,
	}
	if err := json.Unmarshal([]byte(platform.Data[TemplateKey]), &selection.Template); err != nil {
		return nil, nil, fmt.Errorf("decode platform deployment template: %w", err)
	}
	if err := r.configureController(ctx, deployment, selection, previous); err != nil {
		return nil, nil, err
	}
	// Apply keeps earlier declarations alive until explicitly deleted. Retain their
	// identities so later edits cannot make instance deletion forget those resources.
	if previous != nil {
		selection.Resources = append(selection.Resources, previous.Resources...)
	}
	for _, reference := range deployment.Spec.Resources {
		if !slices.Contains(selection.Resources, reference) {
			selection.Resources = append(selection.Resources, reference)
		}
	}
	access := configuration.Data[AccessKey]
	configuration = selection.Template.Configuration.DeepCopy()
	r.bind(deployment, configuration)
	if configuration.Data == nil {
		configuration.Data = make(map[string]string)
	}
	encoded, err := json.Marshal(selection)
	if err != nil {
		return nil, nil, err
	}
	configuration.Data[selectionKey] = string(encoded)
	if access != "" {
		configuration.Data[AccessKey] = access
	}
	if err := r.apply(ctx, deployment, configuration); err != nil {
		return nil, nil, fmt.Errorf("persist controller selection: %w", err)
	}
	return selection, configuration, nil
}

// bind labels explicitly retired platform resources without making the controller a cascade dependent.
func (r *Reconciler) bind(deployment *api.InferenceDeployment, object client.Object) {
	object.SetName(ResourceName(string(deployment.UID)))
	object.SetNamespace(deployment.Namespace)
	object.SetOwnerReferences(nil)
	labels := maps.Clone(object.GetLabels())
	if labels == nil {
		labels = make(map[string]string)
	}
	labels[api.DeploymentUIDLabel] = string(deployment.UID)
	labels["app.kubernetes.io/component"] = controllerComponent
	labels["app.kubernetes.io/managed-by"] = "foretoken"
	object.SetLabels(labels)
	annotations := maps.Clone(object.GetAnnotations())
	if annotations == nil {
		annotations = make(map[string]string)
	}
	delete(annotations, "meta.helm.sh/release-name")
	delete(annotations, "meta.helm.sh/release-namespace")
	annotations[deploymentNameAnnotation] = deployment.Name
	object.SetAnnotations(annotations)
}

// apply uses native optimistic preconditions to repair only this deployment's materialized resources.
func (r *Reconciler) apply(ctx context.Context, deployment *api.InferenceDeployment, desired client.Object) error {
	current := desired.DeepCopyObject().(client.Object)
	if err := r.APIReader.Get(ctx, client.ObjectKeyFromObject(desired), current); apierrors.IsNotFound(err) {
		return r.Create(ctx, desired, client.FieldOwner("foretoken-deployment-manager"))
	} else if err != nil {
		return err
	} else {
		if current.GetLabels()[api.DeploymentUIDLabel] != string(deployment.UID) {
			return fmt.Errorf("resource %s/%s belongs to another deployment", current.GetNamespace(), current.GetName())
		}
		desired.SetUID(current.GetUID())
		desired.SetResourceVersion(current.GetResourceVersion())
	}
	// Typed API responses omit GVK; SSA still requires it in the submitted document.
	kind, err := r.GroupVersionKindFor(desired)
	if err != nil {
		return err
	}
	desired.GetObjectKind().SetGroupVersionKind(kind)
	desired.SetManagedFields(nil)
	return r.Patch(ctx, desired, client.Apply, client.FieldOwner("foretoken-deployment-manager"), client.ForceOwnership)
}

func controllerReady(controller *appsv1.Deployment) bool {
	if controller.Spec.Replicas == nil || *controller.Spec.Replicas == 0 || controller.Status.ObservedGeneration < controller.Generation || controller.Status.UpdatedReplicas != *controller.Spec.Replicas || controller.Status.AvailableReplicas != *controller.Spec.Replicas || controller.Status.Replicas != *controller.Spec.Replicas {
		return false
	}
	for _, condition := range controller.Status.Conditions {
		if condition.Type == appsv1.DeploymentAvailable && condition.Status == corev1.ConditionTrue {
			return true
		}
	}
	return false
}

// publishStatus distinguishes an accepted selection from a running controller; pending work remains Unknown.
func (r *Reconciler) publishStatus(ctx context.Context, deployment *api.InferenceDeployment, observed bool, status metav1.ConditionStatus, revision, reason, message string) error {
	base := deployment.DeepCopy()
	if observed {
		deployment.Status.ObservedGeneration = deployment.Generation
	}
	if revision != "" {
		deployment.Status.ControllerRevision = revision
	}
	meta.SetStatusCondition(&deployment.Status.Conditions, metav1.Condition{
		Type: "Ready", Status: status, Reason: reason, Message: message,
		ObservedGeneration: deployment.Generation,
	})
	if reflect.DeepEqual(base.Status, deployment.Status) {
		return nil
	}
	return r.Status().Patch(ctx, deployment, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
}
