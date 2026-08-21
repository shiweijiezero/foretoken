// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles FrontendService intent into a frontend workload and optional platform route.

package controllers

import (
	"context"
	"errors"
	"fmt"
	"reflect"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"
)

const (
	frontendServiceFieldOwner = "foretoken-frontendservice-controller"
	frontendServiceLabel      = "inference.foretoken.io/frontend-service"

	frontendConditionMaterialized = "WorkloadMaterialized"
	frontendConditionAvailable    = "WorkloadAvailable"
	frontendConditionRouteReady   = "RouteAccepted"
	frontendConditionRoutingReady = "RoutingReady"

	frontendResourcesAppliedMessage         = "Frontend resources were applied"
	frontendResourcesNotMaterializedMessage = "Frontend resources were not materialized"
	frontendDeploymentAvailableMessage      = "The frontend Deployment is available"
	frontendDeploymentUnavailableMessage    = "The frontend Deployment is not available"
	frontendRouteAcceptedMessage            = "The HTTPRoute is accepted and resolved"
	frontendRoutePendingMessage             = "The HTTPRoute is not accepted and resolved"
	frontendRouteNotRequiredMessage         = "Local mode exposes the frontend through its LoadBalancer Service"
	frontendRoutingInstalledMessage         = "A routable backend snapshot is installed"
	frontendRoutingNotInstalledMessage      = "No routable backend snapshot is installed"
)

var frontendConditionTypes = [...]string{
	frontendConditionMaterialized,
	frontendConditionAvailable,
	frontendConditionRouteReady,
	frontendConditionRoutingReady,
	conditionReady,
}

// GatewayParent identifies the platform-owned Gateway that accepts frontend traffic.
type GatewayParent struct {
	Name        string
	Namespace   string
	SectionName string
}

// FrontendRuntimeProfile contains platform-owned frontend settings and an optional production Gateway.
type FrontendRuntimeProfile struct {
	Image            string
	Port             int32
	ImagePullSecrets []corev1.LocalObjectReference
	Gateway          *GatewayParent
}

// FrontendServiceReconciler owns the frontend workload and its optional HTTPRoute.
type FrontendServiceReconciler struct {
	client.Client
	APIReader      client.Reader
	RuntimeProfile FrontendRuntimeProfile
}

// SetupWithManager watches each resource whose state contributes to frontend readiness.
func (reconciler *FrontendServiceReconciler) SetupWithManager(manager ctrl.Manager) error {
	builder := ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.FrontendService{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Owns(&corev1.ConfigMap{})
	if reconciler.RuntimeProfile.Gateway != nil {
		builder = builder.Owns(&gatewayv1.HTTPRoute{})
	}
	return builder.
		Watches(&inferencev1alpha1.ModelService{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsInNamespace)).
		Watches(&inferencev1alpha1.ModelPool{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsInNamespace)).
		Watches(&inferencev1alpha1.ModelGroup{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsInNamespace)).
		Complete(reconciler)
}

func (reconciler *FrontendServiceReconciler) frontendsInNamespace(ctx context.Context, object client.Object) []reconcile.Request {
	var frontends inferencev1alpha1.FrontendServiceList
	if err := reconciler.List(ctx, &frontends, client.InNamespace(object.GetNamespace())); err != nil {
		return nil
	}
	requests := make([]reconcile.Request, 0, len(frontends.Items))
	for index := range frontends.Items {
		requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&frontends.Items[index])})
	}
	return requests
}

// Reconcile applies frontend resources and keeps readiness fail-closed until a serving snapshot is installed.
func (reconciler *FrontendServiceReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	frontend := new(inferencev1alpha1.FrontendService)
	if err := reconciler.Get(ctx, request.NamespacedName, frontend); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !frontend.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}
	if err := reconciler.RuntimeProfile.validate(); err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "RuntimeProfileIncomplete", FailureMessage: err.Error()})
	}
	if err := ensureKVIndexerSecret(ctx, reconciler.Client, frontend.Namespace); err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "KVIndexerSecretFailed", FailureMessage: err.Error()})
	}
	servingSnapshotInstalled, err := reconciler.reconcileServingSnapshot(ctx, frontend)
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "ServingSnapshotProjectionFailed", FailureMessage: err.Error()})
	}

	deployment, service, route, err := frontendDesiredResources(frontend, reconciler.RuntimeProfile)
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "InvalidIntent", FailureMessage: err.Error()})
	}
	objects := []client.Object{deployment, service}
	if route != nil {
		objects = append(objects, route)
	}
	for _, object := range objects {
		if err := controllerutil.SetControllerReference(frontend, object, reconciler.Scheme()); err != nil {
			return ctrl.Result{}, fmt.Errorf("set %s owner: %w", object.GetObjectKind().GroupVersionKind().Kind, err)
		}
		if err := reconciler.applyOwned(ctx, frontend, object); err != nil {
			statusErr := reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "ApplyFailed", FailureMessage: err.Error()})
			return ctrl.Result{}, errors.Join(err, statusErr)
		}
	}
	if route == nil {
		if err := reconciler.deleteOwnedHTTPRoute(ctx, frontend); err != nil {
			statusErr := reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "RouteCleanupFailed", FailureMessage: err.Error()})
			return ctrl.Result{}, errors.Join(err, statusErr)
		}
	}

	// Status is calculated from persisted workload and optional Gateway observations.
	currentDeployment := new(appsv1.Deployment)
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(deployment), currentDeployment); err != nil {
		return ctrl.Result{}, fmt.Errorf("get frontend Deployment: %w", err)
	}
	routeRequired := route != nil
	routeReady := !routeRequired
	if routeRequired {
		currentRoute := new(gatewayv1.HTTPRoute)
		if err := reconciler.Get(ctx, client.ObjectKeyFromObject(route), currentRoute); err != nil {
			return ctrl.Result{}, fmt.Errorf("get frontend HTTPRoute: %w", err)
		}
		routeReady = httpRouteAccepted(currentRoute, *reconciler.RuntimeProfile.Gateway, frontend.Namespace)
	}
	available := frontendDeploymentAvailable(currentDeployment)
	state := frontendState{
		Materialized:  true,
		Available:     available,
		RouteRequired: routeRequired,
		RouteReady:    routeReady,
		RoutingReady:  available && servingSnapshotInstalled,
	}
	return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, state)
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

func (reconciler *FrontendServiceReconciler) applyOwned(ctx context.Context, owner *inferencev1alpha1.FrontendService, desired client.Object) error {
	current := desired.DeepCopyObject().(client.Object)
	err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current)
	if err == nil && !metav1.IsControlledBy(current, owner) {
		return fmt.Errorf("%s %q is not controlled by FrontendService", desired.GetObjectKind().GroupVersionKind().Kind, desired.GetName())
	}
	if err != nil && !apierrors.IsNotFound(err) {
		return fmt.Errorf("get %s %q: %w", desired.GetObjectKind().GroupVersionKind().Kind, desired.GetName(), err)
	}
	if err := reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner(frontendServiceFieldOwner), client.ForceOwnership); err != nil {
		return fmt.Errorf("apply %s %q: %w", desired.GetObjectKind().GroupVersionKind().Kind, desired.GetName(), err)
	}
	return nil
}

func (reconciler *FrontendServiceReconciler) deleteOwnedHTTPRoute(ctx context.Context, frontend *inferencev1alpha1.FrontendService) error {
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	route := &gatewayv1.HTTPRoute{}
	err := reader.Get(ctx, client.ObjectKeyFromObject(frontend), route)
	if apierrors.IsNotFound(err) || meta.IsNoMatchError(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("get frontend HTTPRoute for local mode: %w", err)
	}
	if !metav1.IsControlledBy(route, frontend) {
		return nil
	}
	if err := reconciler.Delete(ctx, route); err != nil && !apierrors.IsNotFound(err) {
		return fmt.Errorf("delete frontend HTTPRoute for local mode: %w", err)
	}
	return nil
}

type frontendState struct {
	Materialized   bool
	Available      bool
	RouteRequired  bool
	RouteReady     bool
	RoutingReady   bool
	FailureReason  string
	FailureMessage string
}

func (reconciler *FrontendServiceReconciler) updateStatus(ctx context.Context, frontend *inferencev1alpha1.FrontendService, state frontendState) error {
	base := frontend.DeepCopy()
	frontend.Status.ObservedGeneration = frontend.Generation
	if state.FailureReason != "" {
		for _, conditionType := range frontendConditionTypes {
			setFrontendCondition(frontend, conditionType, metav1.ConditionFalse, state.FailureReason, state.FailureMessage)
		}
	} else {
		setFrontendCondition(
			frontend,
			frontendConditionMaterialized,
			conditionStatus(state.Materialized),
			frontendBooleanReason(state.Materialized, "Applied", "NotMaterialized"),
			frontendBooleanMessage(state.Materialized, frontendResourcesAppliedMessage, frontendResourcesNotMaterializedMessage),
		)
		setFrontendCondition(
			frontend,
			frontendConditionAvailable,
			conditionStatus(state.Available),
			frontendBooleanReason(state.Available, "Available", "Unavailable"),
			frontendBooleanMessage(state.Available, frontendDeploymentAvailableMessage, frontendDeploymentUnavailableMessage),
		)
		if state.RouteRequired {
			setFrontendCondition(
				frontend,
				frontendConditionRouteReady,
				conditionStatus(state.RouteReady),
				frontendBooleanReason(state.RouteReady, "Accepted", "Pending"),
				frontendBooleanMessage(state.RouteReady, frontendRouteAcceptedMessage, frontendRoutePendingMessage),
			)
		} else {
			setFrontendCondition(frontend, frontendConditionRouteReady, metav1.ConditionTrue, "NotRequired", frontendRouteNotRequiredMessage)
		}
		setFrontendCondition(
			frontend,
			frontendConditionRoutingReady,
			conditionStatus(state.RoutingReady),
			frontendBooleanReason(state.RoutingReady, "Installed", "NotInstalled"),
			frontendBooleanMessage(state.RoutingReady, frontendRoutingInstalledMessage, frontendRoutingNotInstalledMessage),
		)
		reason, message := frontendReadyFailure(state)
		setFrontendCondition(frontend, conditionReady, conditionStatus(reason == "Ready"), reason, message)
	}
	if reflect.DeepEqual(base.Status, frontend.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, frontend, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update FrontendService status: %w", err)
	}
	return nil
}

func setFrontendCondition(frontend *inferencev1alpha1.FrontendService, conditionType string, status metav1.ConditionStatus, reason, message string) {
	meta.SetStatusCondition(&frontend.Status.Conditions, metav1.Condition{Type: conditionType, Status: status, Reason: reason, Message: message, ObservedGeneration: frontend.Generation})
}

func frontendReadyFailure(state frontendState) (string, string) {
	switch {
	case !state.Materialized:
		return "NotMaterialized", "Frontend resources are not materialized"
	case !state.Available:
		return "WorkloadUnavailable", "The frontend Deployment is not available"
	case state.RouteRequired && !state.RouteReady:
		return "RouteNotAccepted", "The HTTPRoute is not accepted and resolved by its Gateway"
	case !state.RoutingReady:
		return "RoutingNotReady", frontendRoutingNotInstalledMessage
	default:
		return "Ready", "The frontend workload and backend routing are ready"
	}
}

func frontendBooleanReason(value bool, trueReason, falseReason string) string {
	if value {
		return trueReason
	}
	return falseReason
}

func frontendBooleanMessage(value bool, trueMessage, falseMessage string) string {
	if value {
		return trueMessage
	}
	return falseMessage
}
