// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Owns frontend workloads, routing configuration, and service readiness.

package frontend

import (
	"context"
	"errors"
	"fmt"
	"time"

	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/observability"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	ctrlbuilder "sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"
)

const frontendServiceFieldOwner = "foretoken-frontendservice-controller"

// GatewayParent identifies the platform-owned Gateway that accepts frontend traffic.
type GatewayParent struct {
	Name        string
	Namespace   string
	SectionName string
}

// FrontendRuntimeProfile contains platform-owned frontend settings and an optional production Gateway.
type FrontendRuntimeProfile struct {
	ApplicationFiles     runtimeconfig.ApplicationFiles
	ApplicationURL       string
	SourceMode           bool
	SourceRevision       string
	Image                string
	WorkerImage          string
	Port                 int32
	ImagePullSecrets     []corev1.LocalObjectReference
	RuntimeCache         *inferencev1alpha1.RuntimeCacheBinding
	VideoStorageIdentity *inferencev1alpha1.StorageIdentity
	HuggingFaceAccess    *inferencev1alpha1.HuggingFaceAccess
	AdmissionStore       *inferencev1alpha1.RedisConnection
	ResponseStore        *inferencev1alpha1.ResponseStoreConfig
	Gateway              *GatewayParent
}

// Reconciler owns the frontend workload and its optional HTTPRoute.
type Reconciler struct {
	client.Client
	APIReader      client.Reader
	RuntimeProfile FrontendRuntimeProfile
	CacheProfile   runtimeconfig.RuntimeCacheProfile
	Alerts         *observability.ServiceAlerts
}

// SetupWithManager watches each resource whose state contributes to frontend readiness.
func (reconciler *Reconciler) SetupWithManager(manager ctrl.Manager) error {
	builder := ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.FrontendService{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&rbacv1.Role{}).
		Owns(&rbacv1.RoleBinding{})
	builder = builder.Watches(&corev1.PersistentVolumeClaim{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsForClaim))
	if reconciler.RuntimeProfile.Gateway != nil {
		builder = builder.Owns(&gatewayv1.HTTPRoute{})
	}
	if reconciler.Alerts != nil && reconciler.Alerts.WatchRules {
		builder = builder.Owns(&monitoringv1.PrometheusRule{})
	}
	workloadPod := func(object client.Object) bool {
		return object.GetLabels()[workload.FrontendServiceLabel] != "" || object.GetLabels()[workload.ModelGroupLabel] != ""
	}
	return builder.
		Watches(&corev1.Pod{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsForPod), ctrlbuilder.WithPredicates(predicate.Funcs{
			CreateFunc: func(event event.CreateEvent) bool { return workloadPod(event.Object) },
			UpdateFunc: func(event event.UpdateEvent) bool {
				before, after := event.ObjectOld.(*corev1.Pod), event.ObjectNew.(*corev1.Pod)
				return workloadPod(before) || workloadPod(after) || before.Status.Phase != after.Status.Phase && (after.Status.Phase == corev1.PodSucceeded || after.Status.Phase == corev1.PodFailed)
			},
			DeleteFunc:  func(event.DeleteEvent) bool { return true },
			GenericFunc: func(event event.GenericEvent) bool { return workloadPod(event.Object) },
		})).
		Watches(&inferencev1alpha1.ModelService{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsInNamespace)).
		Watches(&inferencev1alpha1.ModelPool{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsInNamespace)).
		Watches(&inferencev1alpha1.ModelGroup{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsInNamespace)).
		Watches(&inferencev1alpha1.RuntimeCache{}, handler.EnqueueRequestsFromMapFunc(reconciler.frontendsInNamespace)).
		Complete(reconciler)
}

// frontendsInNamespace maps model lifecycle changes to every frontend in the same namespace.
func (reconciler *Reconciler) frontendsInNamespace(ctx context.Context, object client.Object) []reconcile.Request {
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
func (reconciler *Reconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	frontend := new(inferencev1alpha1.FrontendService)
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	if err := reader.Get(ctx, request.NamespacedName, frontend); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	result, err := reconciler.reconcileFrontend(ctx, frontend)
	return result, errors.Join(err, reconciler.reconcileAlerts(ctx, frontend))
}

// reconcileFrontend keeps serving state independent of optional alert configuration failures.
func (reconciler *Reconciler) reconcileFrontend(ctx context.Context, frontend *inferencev1alpha1.FrontendService) (ctrl.Result, error) {
	if !frontend.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}
	current, err := reconciler.retireFrontendReplicas(ctx, frontend)
	if err != nil {
		statusErr := reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "ReplicaRetirementFailed", FailureMessage: err.Error()})
		return ctrl.Result{}, errors.Join(err, statusErr)
	}
	if frontend.Spec.Suspend || frontend.Spec.Replicas != nil && *frontend.Spec.Replicas == 0 {
		return reconciler.reconcileStoppedFrontend(ctx, frontend, current)
	}
	var services inferencev1alpha1.ModelServiceList
	if err := reconciler.List(ctx, &services, client.InNamespace(frontend.Namespace)); err != nil {
		return ctrl.Result{}, fmt.Errorf("list ModelServices for frontend: %w", err)
	}
	_, admissionErr := effectiveModelAdmission(frontend.Spec, inferencev1alpha1.ModelServiceSpec{})
	if admissionErr == nil {
		for _, service := range services.Items {
			if (service.Spec.Backend != "vllm" && service.Spec.Backend != "vllm-omni") || !service.DeletionTimestamp.IsZero() {
				continue
			}
			if _, err := effectiveModelAdmission(frontend.Spec, service.Spec); err != nil {
				admissionErr = fmt.Errorf("ModelService %q admission: %w", service.Name, err)
				break
			}
		}
	}
	if admissionErr != nil {
		_, membershipErr := reconciler.reconcileServingSnapshot(ctx, frontend, nil, true)
		statusErr := reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "InvalidIntent", FailureMessage: admissionErr.Error()})
		return ctrl.Result{}, errors.Join(membershipErr, statusErr)
	}
	profile, err := reconciler.reconcileApplication(ctx, frontend, services.Items)
	if err != nil || profile == nil {
		return ctrl.Result{}, err
	}
	if err := servingconfig.EnsureKVIndexerSecret(ctx, reconciler.Client, frontend.Namespace); err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "KVIndexerSecretFailed", FailureMessage: err.Error()})
	}
	published, projectionErr := reconciler.reconcileServingSnapshot(ctx, frontend, services.Items, false)
	if !published && projectionErr != nil {
		// Rejected serving intent must not prevent retirement of terminated execution members.
		_, membershipErr := reconciler.reconcileServingSnapshot(ctx, frontend, nil, true)
		projectionErr = errors.Join(projectionErr, membershipErr)
	}

	runtimeCache, cacheReady, err := reconciler.CacheProfile.Resolve(ctx, reconciler.Client, frontend.Namespace)
	if err == nil && cacheReady {
		cacheReady, err = reconciler.servingCacheReady(ctx, frontend.Namespace, runtimeCache, services.Items)
	}
	if err != nil {
		statusErr := reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "RuntimeCacheProjectionFailed", FailureMessage: err.Error()})
		return ctrl.Result{}, errors.Join(err, statusErr)
	}
	// Start with the selected cache instead of creating a temporary frontend that
	// must be replaced once model placement completes. Existing replicas stay live.
	profile.RuntimeCache = runtimeCache
	applyDeployment := cacheReady

	if storage := frontend.Spec.VideoTasks; storage != nil {
		identity, err := runtimeconfig.VideoOutputIdentity(ctx, reconciler.APIReader, frontend.Namespace, storage.ClaimName)
		if err != nil {
			statusErr := reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "VideoStorageProjectionFailed", FailureMessage: err.Error()})
			return ctrl.Result{}, errors.Join(err, statusErr)
		}
		profile.VideoStorageIdentity = &identity
	}
	deployment, service, route, err := frontendDesiredResources(frontend, *profile)
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
		objects = append(objects, desiredVideoTaskAccess(frontend)...)
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
		currentDeployment = current
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
	// Availability can describe retained replicas; completion requires the selected
	// template and cache, followed by every routing consumer's acknowledgement.
	available := workload.DeploymentAvailable(currentDeployment)
	targetReplicas := *deployment.Spec.Replicas
	executionReady := applyDeployment && cacheReady && available &&
		currentDeployment.Spec.Replicas != nil && *currentDeployment.Spec.Replicas == targetReplicas &&
		currentDeployment.Status.UpdatedReplicas == targetReplicas &&
		currentDeployment.Status.Replicas == targetReplicas &&
		currentDeployment.Spec.Template.Spec.Containers[0].Image == profile.Image &&
		currentDeployment.Spec.Template.Annotations["inference.foretoken.io/application-url"] == profile.ApplicationURL
	configuration := reconciler.observeConfiguration(ctx, frontend, currentDeployment)
	if projectionErr != nil {
		configuration = metav1.Condition{Status: metav1.ConditionFalse, Reason: "ServingSnapshotProjectionFailed", Message: projectionErr.Error()}
		var backendRequired *backendDeploymentRequiredError
		if errors.As(projectionErr, &backendRequired) {
			configuration.Reason = "BackendDeploymentRequired"
		}
		if errors.Is(projectionErr, errFrontendCacheIsolationUnsupported) {
			configuration.Reason = "FrontendDeploymentRequired"
		}
	}
	state := frontendState{
		Configuration:  configuration,
		Materialized:   currentDeployment != nil,
		CachePending:   !cacheReady,
		Available:      available,
		ExecutionReady: executionReady,
		RouteRequired:  routeRequired,
		RouteReady:     routeReady,
	}
	result := ctrl.Result{}
	if configuration.Status != metav1.ConditionTrue {
		result.RequeueAfter = time.Second
	}
	return result, reconciler.updateStatus(ctx, frontend, state)
}

// retireFrontendReplicas applies capacity reductions without resolving a replacement Pod template.
// Increasing capacity remains part of normal application and storage reconciliation.
func (reconciler *Reconciler) retireFrontendReplicas(ctx context.Context, frontend *inferencev1alpha1.FrontendService) (*appsv1.Deployment, error) {
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	deployment := new(appsv1.Deployment)
	if err := reader.Get(ctx, client.ObjectKeyFromObject(frontend), deployment); apierrors.IsNotFound(err) {
		return nil, nil
	} else if err != nil {
		return nil, fmt.Errorf("read frontend Deployment for replica retirement: %w", err)
	}
	if !metav1.IsControlledBy(deployment, frontend) {
		return nil, fmt.Errorf("Deployment %q is not controlled by FrontendService", deployment.Name)
	}
	currentReplicas := int32(1)
	if deployment.Spec.Replicas != nil {
		currentReplicas = *deployment.Spec.Replicas
	}
	target := frontend.Spec.Replicas
	if frontend.Spec.Suspend {
		zero := int32(0)
		target = &zero
	}
	if target != nil && *target < currentReplicas {
		base := deployment.DeepCopy()
		deployment.Spec.Replicas = target
		if err := reconciler.Patch(ctx, deployment, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}), client.FieldOwner(frontendServiceFieldOwner)); err != nil {
			return nil, fmt.Errorf("reduce frontend replicas: %w", err)
		}
	}
	return deployment, nil
}

// reconcileStoppedFrontend retains configuration and storage while waiting for every old replica to exit.
// ScaledToZero confirms retirement, not serving readiness or application activation.
func (reconciler *Reconciler) reconcileStoppedFrontend(ctx context.Context, frontend *inferencev1alpha1.FrontendService, deployment *appsv1.Deployment) (ctrl.Result, error) {
	if _, err := reconciler.reconcileServingSnapshot(ctx, frontend, nil, true); err != nil {
		return ctrl.Result{}, err
	}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	// Published members remain authoritative when their Deployment or ReplicaSet has already gone.
	var previous []string
	for _, version := range servingconfig.ProtocolVersions(frontend) {
		configuration := new(corev1.ConfigMap)
		key := client.ObjectKey{Namespace: frontend.Namespace, Name: servingconfig.ConfigMapName(frontend, version)}
		if err := reader.Get(ctx, key, configuration); apierrors.IsNotFound(err) {
			continue
		} else if err != nil {
			return ctrl.Result{}, err
		}
		if !metav1.IsControlledBy(configuration, frontend) {
			return ctrl.Result{}, fmt.Errorf("ConfigMap %q is not controlled by FrontendService", configuration.Name)
		}
		snapshot, _, err := readServingPublication([]byte(configuration.Data[servingconfig.SnapshotKey]))
		if err != nil {
			return ctrl.Result{}, err
		}
		previous = append(previous, snapshot.FrontendInstances...)
	}
	pods, err := reconciler.frontendInstancePods(ctx, frontend, previous)
	if err != nil {
		return ctrl.Result{}, err
	}
	if len(pods) != 0 || deployment != nil && (deployment.Status.ObservedGeneration < deployment.Generation || deployment.Status.Replicas != 0) {
		return ctrl.Result{RequeueAfter: time.Second}, reconciler.updateStatus(ctx, frontend, frontendState{FailureReason: "ScalingDown", FailureMessage: "Waiting for frontend replicas to stop"})
	}
	return ctrl.Result{}, reconciler.updateStatus(ctx, frontend, frontendState{Stopped: true})
}

// applyOwned server-side applies a FrontendService-owned resource after checking ownership.
func (reconciler *Reconciler) applyOwned(ctx context.Context, owner *inferencev1alpha1.FrontendService, desired client.Object) error {
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
func (reconciler *Reconciler) deleteOwnedHTTPRoute(ctx context.Context, frontend *inferencev1alpha1.FrontendService) error {
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
func (reconciler *Reconciler) deleteOwnedVideoTaskAccess(ctx context.Context, frontend *inferencev1alpha1.FrontendService) error {
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

// reconcileAlerts keeps HTTP alert scope on the frontend rather than assigning shared failures to models.
func (reconciler *Reconciler) reconcileAlerts(ctx context.Context, frontend *inferencev1alpha1.FrontendService) error {
	var selected []string
	var admission *inferencev1alpha1.AdmissionAlertThresholds
	if frontend.Spec.Observability != nil && frontend.Spec.Observability.Alerts != nil {
		settings := frontend.Spec.Observability.Alerts
		selected = settings.Rules
		if settings.Thresholds != nil {
			admission = settings.Thresholds.Admission
		}
	}
	err := reconciler.Alerts.Reconcile(ctx, frontend, selected, observability.Scope{Namespace: frontend.Namespace, Frontend: frontend.Name}, nil, admission)
	return errors.Join(err, observability.UpdateServiceCondition(ctx, reconciler.Client, frontend, len(selected) != 0, err))
}
