// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package tests

import (
	"context"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"
)

// TestFrontendRoutingSnapshotAndReadinessContract protects Gateway routing publication and readiness as one frontend lifecycle.
func TestFrontendRoutingSnapshotAndReadinessContract(t *testing.T) {
	ctx := context.Background()
	frontend := &inferencev1alpha1.FrontendService{
		TypeMeta:   metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "FrontendService"},
		ObjectMeta: metav1.ObjectMeta{Name: "chat", Namespace: "default", UID: "chat-uid", Generation: 1},
		Spec: inferencev1alpha1.FrontendServiceSpec{
			Replicas: pointer(int32(1)),
			Hostname: "chat.example.com",
			Resources: inferencev1alpha1.FrontendResources{
				Requests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"},
			},
			Timeouts: inferencev1alpha1.FrontendTimeouts{Request: "10m", StreamIdle: "5m"},
		},
	}
	c := controllerClient(t, frontend)
	r := &controllers.FrontendServiceReconciler{Client: c, RuntimeProfile: controllers.FrontendRuntimeProfile{Image: "frontend:test", Port: 8080, ImagePullSecrets: []corev1.LocalObjectReference{{Name: "registry-auth"}}, Gateway: &controllers.GatewayParent{Name: "public", Namespace: "gateway-system", SectionName: "https"}}}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(frontend)}
	for range 2 {
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	frontendService := get(t, ctx, c, request.NamespacedName, new(corev1.Service))
	if frontendService.Labels["inference.foretoken.io/frontend-service"] != frontend.Name || len(frontendService.Spec.Ports) != 1 || frontendService.Spec.Ports[0].Name != "http" {
		t.Fatalf("frontend service discovery contract = %#v", frontendService)
	}
	var deployments appsv1.DeploymentList
	if err := c.List(ctx, &deployments, client.InNamespace(frontend.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(deployments.Items) != 1 {
		current := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.FrontendService))
		t.Fatalf("frontend deployments = %#v; status = %#v", deployments.Items, current.Status)
	}
	deployment := &deployments.Items[0]
	if got := deployment.Spec.Template.Spec.ImagePullSecrets; len(got) != 1 || got[0].Name != "registry-auth" {
		t.Fatalf("frontend image pull secrets = %#v", got)
	}
	if grace := deployment.Spec.Template.Spec.TerminationGracePeriodSeconds; grace == nil || *grace != 605 {
		t.Fatalf("frontend termination grace = %v", grace)
	}
	frontendEnv := map[string]string{}
	for _, item := range deployment.Spec.Template.Spec.Containers[0].Env {
		frontendEnv[item.Name] = item.Value
	}
	if frontendEnv["FORETOKEN_REQUEST_TIMEOUT_SECONDS"] != "600" {
		t.Fatalf("frontend request timeout env = %#v", frontendEnv)
	}
	deployment.Status.ObservedGeneration = deployment.Generation
	deployment.Status.AvailableReplicas = 1
	deployment.Status.Conditions = []appsv1.DeploymentCondition{{Type: appsv1.DeploymentAvailable, Status: corev1.ConditionTrue}}
	if err := c.Status().Update(ctx, deployment); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	var snapshots corev1.ConfigMapList
	if err := c.List(ctx, &snapshots, client.InNamespace("default")); err != nil {
		t.Fatal(err)
	}
	if len(snapshots.Items) == 0 {
		t.Fatal("frontend did not publish a serving snapshot")
	}
	foundRoute := false
	for _, config := range snapshots.Items {
		if config.Data["serving.json"] != "" {
			foundRoute = true
		}
	}
	if !foundRoute {
		t.Fatalf("snapshot config maps = %#v", snapshots.Items)
	}
	current := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.FrontendService))
	if condition := meta.FindStatusCondition(current.Status.Conditions, readyCondition); condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("frontend status before gateway readiness = %#v", current.Status)
	}
}

// TestFrontendLocalModeNeedsNoGateway protects local LoadBalancer readiness from depending on Gateway resources.
func TestFrontendLocalModeNeedsNoGateway(t *testing.T) {
	ctx := context.Background()
	frontend := &inferencev1alpha1.FrontendService{
		TypeMeta:   metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "FrontendService"},
		ObjectMeta: metav1.ObjectMeta{Name: "local", Namespace: "default", UID: "local-uid", Generation: 1},
		Spec: inferencev1alpha1.FrontendServiceSpec{
			Replicas:  pointer(int32(1)),
			Resources: inferencev1alpha1.FrontendResources{Requests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"}},
			Timeouts:  inferencev1alpha1.FrontendTimeouts{Request: "10m", StreamIdle: "5m"},
		},
	}
	staleRoute := &gatewayv1.HTTPRoute{
		TypeMeta: metav1.TypeMeta{APIVersion: gatewayv1.GroupVersion.String(), Kind: "HTTPRoute"},
		ObjectMeta: metav1.ObjectMeta{Name: frontend.Name, Namespace: frontend.Namespace, OwnerReferences: []metav1.OwnerReference{{
			APIVersion: frontend.APIVersion, Kind: frontend.Kind, Name: frontend.Name, UID: frontend.UID, Controller: pointer(true),
		}}},
	}
	model := modelService("local-model", 1)
	pool := modelPool(model, "local-model-default", 1)
	group := modelGroup(pool, "local-model-r1-0", 0)
	markPoolRoutingReady(pool, "r1")
	markServiceRoutingReady(model, pool)
	markGroupReady(group)
	c := controllerClient(t, frontend, staleRoute, model, pool, group)
	r := &controllers.FrontendServiceReconciler{
		Client: c, APIReader: c,
		RuntimeProfile: controllers.FrontendRuntimeProfile{Image: "frontend:test", Port: 8080},
	}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(frontend)}
	for range 2 {
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(staleRoute), new(gatewayv1.HTTPRoute)); !apierrors.IsNotFound(err) {
		t.Fatalf("stale local HTTPRoute lookup error = %v", err)
	}
	service := get(t, ctx, c, request.NamespacedName, new(corev1.Service))
	if service.Spec.Type != corev1.ServiceTypeLoadBalancer {
		t.Fatalf("local frontend Service type = %q", service.Spec.Type)
	}
	deployment := get(t, ctx, c, request.NamespacedName, new(appsv1.Deployment))
	deployment.Status.ObservedGeneration = deployment.Generation
	deployment.Status.AvailableReplicas = 1
	deployment.Status.Conditions = []appsv1.DeploymentCondition{{Type: appsv1.DeploymentAvailable, Status: corev1.ConditionTrue}}
	if err := c.Status().Update(ctx, deployment); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	current := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.FrontendService))
	if condition := meta.FindStatusCondition(current.Status.Conditions, readyCondition); condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("local frontend readiness = %#v", current.Status)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, "RouteAccepted"); condition == nil || condition.Status != metav1.ConditionTrue || condition.Reason != "NotRequired" {
		t.Fatalf("local frontend route condition = %#v", current.Status)
	}
}
