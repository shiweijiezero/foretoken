// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles FrontendService intent into a frontend workload and optional platform route.

package controllers

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"slices"

	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
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
	ApplicationFiles  runtimeconfig.ApplicationFiles
	ApplicationURL    string
	SourceMode        bool
	SourceRevision    string
	Image             string
	WorkerImage       string
	Port              int32
	ImagePullSecrets  []corev1.LocalObjectReference
	RuntimeCache      *inferencev1alpha1.RuntimeCacheBinding
	HuggingFaceAccess *inferencev1alpha1.HuggingFaceAccess
	Gateway           *GatewayParent
}

// FrontendServiceReconciler owns the frontend workload and its optional HTTPRoute.
type FrontendServiceReconciler struct {
	client.Client
	APIReader      client.Reader
	RuntimeProfile FrontendRuntimeProfile
	CacheProfile   RuntimeCacheProfile
	Alerts         *ServiceAlerts
}

// SetupWithManager watches each resource whose state contributes to frontend readiness.
func (reconciler *FrontendServiceReconciler) SetupWithManager(manager ctrl.Manager) error {
	builder := ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.FrontendService{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&rbacv1.Role{}).
		Owns(&rbacv1.RoleBinding{})
	if reconciler.RuntimeProfile.Gateway != nil {
		builder = builder.Owns(&gatewayv1.HTTPRoute{})
	}
	if reconciler.Alerts != nil && reconciler.Alerts.watchRules {
		builder = builder.Owns(&monitoringv1.PrometheusRule{})
	}
	return builder.
		Watches(&inferencev1alpha1.ModelService{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsInNamespace)).
		Watches(&inferencev1alpha1.ModelPool{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsInNamespace)).
		Watches(&inferencev1alpha1.ModelGroup{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsInNamespace)).
		Watches(&inferencev1alpha1.RuntimeCache{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsInNamespace)).
		Complete(reconciler)
}

// frontendsInNamespace maps model lifecycle changes to every frontend in the same namespace.
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

// servingCacheReady lets frontends share a cache only after a serving workload has bound it.
// Every selected cohort must use that cache before the frontend changes its mount.
func (reconciler *FrontendServiceReconciler) servingCacheReady(ctx context.Context, namespace string, cache *inferencev1alpha1.RuntimeCacheBinding, services []inferencev1alpha1.ModelService) (bool, error) {
	var pools inferencev1alpha1.ModelPoolList
	if err := reconciler.List(ctx, &pools, client.InNamespace(namespace)); err != nil {
		return false, fmt.Errorf("list ModelPools for frontend runtime cache: %w", err)
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(namespace)); err != nil {
		return false, fmt.Errorf("list ModelGroups for frontend runtime cache: %w", err)
	}
	selectedRevision := false
	for serviceIndex := range services {
		service := &services[serviceIndex]
		if !service.DeletionTimestamp.IsZero() {
			continue
		}
		for _, selected := range service.Status.ServingPoolRevisions {
			selectedRevision = true
			var pool *inferencev1alpha1.ModelPool
			for poolIndex := range pools.Items {
				candidate := &pools.Items[poolIndex]
				if candidate.Spec.PoolName == selected.PoolName && string(candidate.UID) == selected.PoolUID && routingPoolOwnedBy(candidate, service) {
					pool = candidate
					break
				}
			}
			if pool == nil {
				return false, nil
			}
			matched := false
			for groupIndex := range groups.Items {
				group := &groups.Items[groupIndex]
				if routingGroupOwnedBy(group, pool) && group.Spec.Revision == selected.Revision {
					matched = true
					if !routingGroupReady(group) || !reflect.DeepEqual(group.Spec.Artifacts.Cache, cache) {
						return false, nil
					}
				}
			}
			if !matched {
				return false, nil
			}
		}
	}
	return cache == nil || selectedRevision, nil
}

// placeFrontendCache shares the cache placement contract and permits its accelerator-node taint.
func (reconciler *FrontendServiceReconciler) placeFrontendCache(ctx context.Context, namespace string, cache *inferencev1alpha1.RuntimeCacheBinding, pod *corev1.PodTemplateSpec) error {
	if err := placeRuntimeCache(ctx, reconciler.Client, namespace, cache, pod); err != nil {
		return err
	}
	if pod.Spec.Affinity == nil {
		return nil
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(namespace)); err != nil {
		return err
	}
	for _, group := range groups.Items {
		if binding := group.Spec.Artifacts.Cache; binding != nil && binding.ClaimName == cache.ClaimName {
			for _, toleration := range acceleratorTolerations(group.Spec.Accelerator.DeviceResourceName) {
				if !slices.Contains(pod.Spec.Tolerations, toleration) {
					pod.Spec.Tolerations = append(pod.Spec.Tolerations, toleration)
				}
			}
		}
	}
	return nil
}

// Reconcile applies frontend resources and keeps readiness fail-closed until a serving snapshot is installed.
func (reconciler *FrontendServiceReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	frontend := new(inferencev1alpha1.FrontendService)
	if err := reconciler.Get(ctx, request.NamespacedName, frontend); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	result, err := reconciler.reconcileFrontend(ctx, frontend)
	return result, errors.Join(err, reconciler.reconcileAlerts(ctx, frontend))
}

// sourceSelectionAllowed preserves an already-selected source publication without
// reopening source admission when the platform disables new source selections.
func (reconciler *FrontendServiceReconciler) sourceSelectionAllowed(ctx context.Context, frontend *inferencev1alpha1.FrontendService) (bool, error) {
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
	deployment := new(appsv1.Deployment)
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(frontend), deployment); err != nil {
		return false, client.IgnoreNotFound(err)
	}
	return metav1.IsControlledBy(deployment, frontend) && deployment.Spec.Template.Annotations[runtimeconfig.SourceRevisionAnnotation] == revision && deployment.Spec.Template.Annotations["inference.foretoken.io/application-url"] == profile.ApplicationFiles.Ref("frontend", revision), nil
}

// Serving configuration protocol selected with the frontend application.
const frontendServingConfigVersion uint32 = 1
const frontendServingConfigAnnotation = "inference.foretoken.io/serving-config-protocol"

// reconcileFrontend keeps serving state independent of optional alert configuration failures.
func (reconciler *FrontendServiceReconciler) reconcileFrontend(ctx context.Context, frontend *inferencev1alpha1.FrontendService) (ctrl.Result, error) {
	if !frontend.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}
	if err := reconciler.RuntimeProfile.validate(); err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "RuntimeProfileIncomplete", FailureMessage: err.Error()})
	}
	sourceAllowed, err := reconciler.sourceSelectionAllowed(ctx, frontend)
	if err != nil {
		return ctrl.Result{}, err
	}
	sourceRevision, err := runtimeconfig.SourceRevision(frontend.Annotations, sourceAllowed)
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "InvalidIntent", FailureMessage: err.Error()})
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
				return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "InvalidIntent", FailureMessage: "The existing frontend Deployment belongs to another owner"})
			}
			selection = &inferencev1alpha1.ApplicationSelection{
				Image:              current.Spec.Template.Spec.Containers[0].Image,
				ApplicationURL:     current.Spec.Template.Annotations["inference.foretoken.io/application-url"],
				SourceRevision:     current.Spec.Template.Annotations[runtimeconfig.SourceRevisionAnnotation],
				DeploymentRevision: deploymentRevision,
			}
			base := frontend.DeepCopy()
			frontend.Status.Application = selection
			frontend.Status.ServingConfigVersion = 0
			if current.Spec.Template.Annotations[frontendServingConfigAnnotation] == fmt.Sprint(frontendServingConfigVersion) {
				frontend.Status.ServingConfigVersion = frontendServingConfigVersion
			}
			if err := reconciler.Status().Patch(ctx, frontend, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
				return ctrl.Result{}, fmt.Errorf("retain frontend application configuration: %w", err)
			}
			if frontend.Status.ServingConfigVersion != frontendServingConfigVersion {
				return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, deploymentRequired)
			}
		} else if !apierrors.IsNotFound(err) {
			return ctrl.Result{}, err
		}
	}
	retained := selection != nil && selection.DeploymentRevision == deploymentRevision && (selection.SourceRevision == sourceRevision || (selection.SourceRevision == "" && sourceRevision != "" && selection.ApplicationURL == profile.ApplicationFiles.Ref("frontend", sourceRevision)))
	if !retained {
		selection = &inferencev1alpha1.ApplicationSelection{Image: profile.Image, ApplicationURL: profile.ApplicationURL, SourceRevision: sourceRevision, DeploymentRevision: deploymentRevision}
		if sourceRevision != "" {
			selection.ApplicationURL = profile.ApplicationFiles.Ref("frontend", sourceRevision)
		}
	} else {
		selection = selection.DeepCopy()
		selection.SourceRevision = sourceRevision
	}
	// Application selection and its serving protocol advance together. A platform update
	// leaves an explicitly retained application and its existing configuration untouched.
	if frontend.Status.Application != nil && frontend.Status.ServingConfigVersion != frontendServingConfigVersion {
		previous := frontend.Status.Application
		sameApplication := previous.ApplicationURL == selection.ApplicationURL && (selection.ApplicationURL != "" || previous.Image == selection.Image)
		if retained || sameApplication {
			return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, deploymentRequired)
		}
	}
	if !reflect.DeepEqual(frontend.Status.Application, selection) || frontend.Status.ServingConfigVersion != frontendServingConfigVersion {
		base := frontend.DeepCopy()
		frontend.Status.Application = selection
		frontend.Status.ServingConfigVersion = frontendServingConfigVersion
		if err := reconciler.Status().Patch(ctx, frontend, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return ctrl.Result{}, fmt.Errorf("persist frontend application selection: %w", err)
		}
	}
	profile.Image, profile.ApplicationURL = selection.Image, selection.ApplicationURL
	var services inferencev1alpha1.ModelServiceList
	if err := reconciler.List(ctx, &services, client.InNamespace(frontend.Namespace)); err != nil {
		return ctrl.Result{}, fmt.Errorf("list ModelServices for frontend: %w", err)
	}
	if err := ensureKVIndexerSecret(ctx, reconciler.Client, frontend.Namespace); err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "KVIndexerSecretFailed", FailureMessage: err.Error()})
	}
	servingSnapshotInstalled, err := reconciler.reconcileServingSnapshot(ctx, frontend, services.Items)
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "ServingSnapshotProjectionFailed", FailureMessage: err.Error()})
	}
	runtimeCache, cacheReady, err := reconciler.CacheProfile.Resolve(ctx, reconciler.Client, frontend.Namespace)
	if err != nil {
		statusErr := reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "RuntimeCacheProjectionFailed", FailureMessage: err.Error()})
		return ctrl.Result{}, errors.Join(err, statusErr)
	}
	if cacheReady {
		cacheReady, err = reconciler.servingCacheReady(ctx, frontend.Namespace, runtimeCache, services.Items)
		if err != nil {
			statusErr := reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "RuntimeCacheProjectionFailed", FailureMessage: err.Error()})
			return ctrl.Result{}, errors.Join(err, statusErr)
		}
	}
	profile.RuntimeCache = runtimeCache
	applyDeployment := true
	if !cacheReady {
		current := new(appsv1.Deployment)
		if err := reconciler.Get(ctx, client.ObjectKeyFromObject(frontend), current); err == nil {
			applyDeployment = false
		} else if apierrors.IsNotFound(err) {
			profile.RuntimeCache = nil
		} else {
			return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "RuntimeCacheProjectionFailed", FailureMessage: err.Error()})
		}
	}

	deployment, service, route, err := frontendDesiredResources(frontend, profile)
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "InvalidIntent", FailureMessage: err.Error()})
	}
	if applyDeployment && profile.RuntimeCache != nil {
		if err := reconciler.placeFrontendCache(ctx, frontend.Namespace, profile.RuntimeCache, &deployment.Spec.Template); err != nil {
			return ctrl.Result{}, err
		}
	}
	objects := []client.Object{service}
	if frontend.Spec.VideoTasks != nil {
		labels := map[string]string{frontendServiceLabel: frontend.Name}
		objects = append(objects,
			&corev1.ServiceAccount{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"}, ObjectMeta: metav1.ObjectMeta{Name: frontend.Name, Namespace: frontend.Namespace, Labels: labels}},
			&rbacv1.Role{TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "Role"}, ObjectMeta: metav1.ObjectMeta{Name: frontend.Name, Namespace: frontend.Namespace, Labels: labels}, Rules: []rbacv1.PolicyRule{{APIGroups: []string{"inference.foretoken.io"}, Resources: []string{"videotasks"}, Verbs: []string{"get", "create", "patch", "delete"}}}},
			&rbacv1.RoleBinding{TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "RoleBinding"}, ObjectMeta: metav1.ObjectMeta{Name: frontend.Name, Namespace: frontend.Namespace, Labels: labels}, RoleRef: rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: frontend.Name}, Subjects: []rbacv1.Subject{{Kind: "ServiceAccount", Name: frontend.Name, Namespace: frontend.Namespace}}},
		)
	}
	if applyDeployment {
		objects = append(objects, deployment)
	}
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

	// Apply returns the persisted objects; an informer read can still contain the previous template.
	currentDeployment := deployment
	if !applyDeployment {
		currentDeployment = new(appsv1.Deployment)
		if err := reconciler.Get(ctx, client.ObjectKeyFromObject(deployment), currentDeployment); err != nil {
			return ctrl.Result{}, fmt.Errorf("get frontend Deployment: %w", err)
		}
	}
	if frontend.Spec.VideoTasks == nil && applyDeployment &&
		currentDeployment.Status.ObservedGeneration >= currentDeployment.Generation &&
		currentDeployment.Status.Replicas == *deployment.Spec.Replicas &&
		currentDeployment.Status.UpdatedReplicas == *deployment.Spec.Replicas &&
		currentDeployment.Status.AvailableReplicas == *deployment.Spec.Replicas {
		if err := reconciler.deleteOwnedVideoTaskAccess(ctx, frontend); err != nil {
			return ctrl.Result{}, err
		}
	}
	routeRequired := route != nil
	routeReady := !routeRequired
	if routeRequired {
		routeReady = httpRouteAccepted(route, *reconciler.RuntimeProfile.Gateway, frontend.Namespace)
	}
	// An old admission-only Pod may be available while the cache-backed replacement starts.
	// Deployment submission completes only when the requested frontend template is ready.
	available := frontendDeploymentAvailable(currentDeployment)
	targetReplicas := *deployment.Spec.Replicas
	executionReady := applyDeployment && cacheReady && available &&
		currentDeployment.Spec.Replicas != nil && *currentDeployment.Spec.Replicas == targetReplicas &&
		currentDeployment.Status.UpdatedReplicas == targetReplicas &&
		currentDeployment.Status.Replicas == targetReplicas &&
		currentDeployment.Spec.Template.Spec.Containers[0].Image == selection.Image &&
		currentDeployment.Spec.Template.Annotations["inference.foretoken.io/application-url"] == selection.ApplicationURL
	state := frontendState{
		Materialized:   applyDeployment,
		Available:      available,
		ExecutionReady: executionReady,
		RouteRequired:  routeRequired,
		RouteReady:     routeReady,
		RoutingReady:   available && servingSnapshotInstalled,
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

// applyOwned server-side applies a FrontendService-owned resource after checking ownership.
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

// deleteOwnedHTTPRoute removes the FrontendService route when local exposure no longer needs it.
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

// deleteOwnedVideoTaskAccess removes delegated access when asynchronous tasks are disabled.
func (reconciler *FrontendServiceReconciler) deleteOwnedVideoTaskAccess(ctx context.Context, frontend *inferencev1alpha1.FrontendService) error {
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	for _, object := range []client.Object{&rbacv1.RoleBinding{}, &rbacv1.Role{}, &corev1.ServiceAccount{}} {
		if err := reader.Get(ctx, client.ObjectKeyFromObject(frontend), object); apierrors.IsNotFound(err) {
			continue
		} else if err != nil {
			return err
		}
		if !metav1.IsControlledBy(object, frontend) {
			continue
		}
		if err := reconciler.Delete(ctx, object); err != nil && !apierrors.IsNotFound(err) {
			return err
		}
	}
	return nil
}

type frontendState struct {
	// ExecutionReady concerns the selected application and target capacity; Available
	// still reports a previous Deployment that is serving during deferred replacement.
	ExecutionReady bool
	Materialized   bool
	Available      bool
	RouteRequired  bool
	RouteReady     bool
	RoutingReady   bool
	FailureReason  string
	FailureMessage string
}

// updateStatus publishes workload, route, and serving-snapshot readiness for the frontend.
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
	case !state.ExecutionReady:
		return "ExecutionPending", "The selected frontend application and target replicas are not ready"
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
