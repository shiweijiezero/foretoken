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
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

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
	r := &controllers.FrontendServiceReconciler{Client: c, RuntimeProfile: controllers.FrontendRuntimeProfile{Image: "frontend:test", Port: 8080, Gateway: controllers.GatewayParent{Name: "public", Namespace: "gateway-system", SectionName: "https"}}}
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
