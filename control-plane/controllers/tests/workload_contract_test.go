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
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevalidation "k8s.io/apimachinery/pkg/util/validation"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestModelGroupWorkloadContract(t *testing.T) {
	ctx := context.Background()
	t.Run("aggregate workload owns isolated serving resources", func(t *testing.T) {
		service := modelService("model", 1)
		pool := modelPool(service, "model-default", 1)
		group := modelGroup(pool, "model-r1-0", 0)
		group.Spec.Accelerator.RuntimeClassName = "nvidia"
		c := controllerClient(t, service, pool, group)
		r := &controllers.ModelGroupReconciler{Client: c, ControlPlaneNamespace: "foretoken-system", ImagePullSecrets: []corev1.LocalObjectReference{{Name: "registry-auth"}}}
		request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
		for range 2 {
			if _, err := r.Reconcile(ctx, request); err != nil {
				t.Fatal(err)
			}
		}
		deployment := get(t, ctx, c, request.NamespacedName, new(appsv1.Deployment))
		pod := deployment.Spec.Template.Spec
		if deployment.Spec.Replicas == nil || *deployment.Spec.Replicas != 1 || pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken || pod.RuntimeClassName == nil || *pod.RuntimeClassName != "nvidia" || pod.Containers[0].Image != "vllm:test" || pod.Containers[0].ReadinessProbe == nil {
			t.Fatalf("deployment contract = %#v", deployment.Spec)
		}
		if len(pod.ImagePullSecrets) != 1 || pod.ImagePullSecrets[0].Name != "registry-auth" {
			t.Fatalf("model-server image pull secrets = %#v", pod.ImagePullSecrets)
		}
		serviceObject := get(t, ctx, c, request.NamespacedName, new(corev1.Service))
		if !metav1.IsControlledBy(serviceObject, group) || serviceObject.Spec.Selector["inference.foretoken.io/model-group"] != group.Name {
			t.Fatalf("service contract = %#v", serviceObject)
		}
		policy := get(t, ctx, c, request.NamespacedName, new(networkingv1.NetworkPolicy))
		if !metav1.IsControlledBy(policy, group) || len(policy.Spec.Ingress) != 1 || len(policy.Spec.Ingress[0].From) != 2 {
			t.Fatalf("network policy = %#v", policy.Spec)
		}
		current := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.ModelGroup))
		if condition := meta.FindStatusCondition(current.Status.Conditions, "WorkloadMaterialized"); condition == nil || condition.Status != metav1.ConditionTrue {
			t.Fatalf("group status = %#v", current.Status)
		}
	})
	t.Run("service name accepts model group names containing dots", func(t *testing.T) {
		service := modelService("quickstart-qwen3-0.6b", 1)
		pool := modelPool(service, "quickstart-qwen3-0.6b-default", 1)
		group := modelGroup(pool, "quickstart-qwen3-0.6b-default-revision-1-default-0", 0)
		c := controllerClient(t, service, pool, group)
		r := &controllers.ModelGroupReconciler{Client: c, ControlPlaneNamespace: "foretoken-system"}
		request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
		for range 2 {
			if _, err := r.Reconcile(ctx, request); err != nil {
				t.Fatal(err)
			}
		}

		deployment := get(t, ctx, c, request.NamespacedName, new(appsv1.Deployment))
		if deployment.Name != group.Name {
			t.Fatalf("deployment name = %q, want %q", deployment.Name, group.Name)
		}
		var services corev1.ServiceList
		if err := c.List(ctx, &services, client.InNamespace(group.Namespace), client.MatchingLabels{"inference.foretoken.io/model-group": group.Name}); err != nil {
			t.Fatal(err)
		}
		if len(services.Items) != 1 {
			t.Fatalf("services = %#v", services.Items)
		}
		serviceName := services.Items[0].Name
		if errors := kubevalidation.IsDNS1035Label(serviceName); len(errors) != 0 || serviceName == group.Name {
			t.Fatalf("service name %q is not a DNS-1035 fallback: %v", serviceName, errors)
		}
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
		var current corev1.ServiceList
		if err := c.List(ctx, &current, client.InNamespace(group.Namespace), client.MatchingLabels{"inference.foretoken.io/model-group": group.Name}); err != nil {
			t.Fatal(err)
		}
		if len(current.Items) != 1 || current.Items[0].Name != serviceName {
			t.Fatalf("service name changed after reconcile: %#v", current.Items)
		}
	})
	t.Run("prefill workload includes PD runtime network and RDMA contract", func(t *testing.T) {
		service := modelService("pd", 1)
		pool := modelPool(service, "pd-prefill", 1)
		group := modelGroup(pool, "pd-r1-0", 0)
		group.Spec.Role = inferencev1alpha1.ModelRolePrefill
		group.Spec.PDRuntime = &inferencev1alpha1.ModelGroupPDRuntimeConfig{
			ProfileName:                "pd",
			ProfileRevision:            "r1",
			Connector:                  "MooncakeConnector",
			Protocol:                   "rdma",
			BootstrapPort:              29001,
			AbortRequestTimeoutSeconds: 30,
			RDMADeviceName:             "mlx5_1",
			RDMAResourceName:           "rdma/ib",
			RDMAResourceCount:          1,
		}
		group.Spec.Network = "rdma-net"
		c := controllerClient(t, service, pool, group)
		r := &controllers.ModelGroupReconciler{Client: c, ControlPlaneNamespace: "foretoken-system", ImagePullSecrets: []corev1.LocalObjectReference{{Name: "registry-auth"}}}
		request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
		for range 2 {
			if _, err := r.Reconcile(ctx, request); err != nil {
				t.Fatal(err)
			}
		}
		deployment := get(t, ctx, c, request.NamespacedName, new(appsv1.Deployment))
		container := deployment.Spec.Template.Spec.Containers[0]
		rdma := container.Resources.Limits[corev1.ResourceName("rdma/ib")]
		if deployment.Spec.Template.Annotations["k8s.v1.cni.cncf.io/networks"] != "rdma-net" || len(container.Ports) != 2 || rdma.Value() != 1 {
			t.Fatalf("PD deployment contract = %#v", deployment.Spec)
		}
		policy := get(t, ctx, c, request.NamespacedName, new(networkingv1.NetworkPolicy))
		if len(policy.Spec.Ingress) != 3 {
			t.Fatalf("PD network policy = %#v", policy.Spec)
		}
	})
}
