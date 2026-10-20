// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Binds advertised devices and a common P/D transport before publishing Pool templates.
package model

import (
	"context"
	"fmt"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/labels"
	ctrl "sigs.k8s.io/controller-runtime"
)

// resolveModelTransports selects advertised RDMA allocations and fills omitted P/D protocols.
// The scheduler owns device allocation and free capacity; active workload occupancy does not change this selection.
func (reconciler *ServiceReconciler) resolveModelTransports(ctx context.Context, namespace string, pools []compiler.ModelPool) error {
	profile := reconciler.RuntimeProfile
	var nodes corev1.NodeList
	if profile.RDMA != nil {
		reader := reconciler.APIReader
		if reader == nil {
			reader = reconciler.Client
		}
		if err := reader.List(ctx, &nodes); err != nil {
			return fmt.Errorf("discover runtime RDMA allocation: %w", err)
		}
		// Every Pool inherits the same service cache; existing consumers constrain device placement too.
		var err error
		nodes.Items, err = runtimeconfig.RuntimeCacheNodes(ctx, reader, namespace, pools[0].Template.RuntimeCache, nodes.Items)
		if err != nil {
			return err
		}
	}

	protocol := "rdma"
	for index := range pools {
		template := &pools[index].Template
		template.RDMA = nil
		if allocation := profile.RDMA; allocation != nil && template.Backend == "vllm" {
			selector := labels.SelectorFromSet(template.NodeSelector)
			var eligible int32
			for _, node := range nodes.Items {
				if !selector.Matches(labels.Set(node.Labels)) || profile.NodeSelectorKey != "" && node.Labels[profile.NodeSelectorKey] != profile.NodeSelectorValue {
					continue
				}
				if modelNodeSupportsDevices(node, profile.DeviceResourceName, template.Resources.Requests.GPU.Count, allocation) {
					eligible++
				}
			}
			if eligible >= template.NodeCount {
				template.RDMA = allocation.DeepCopy()
			}
		}
		// Every P/D member must use the same network family, including across different node selectors.
		if template.KVTransfer != nil && template.RDMA == nil {
			protocol = "tcp"
		}
	}
	for index := range pools {
		if transfer := pools[index].Template.KVTransfer; transfer != nil && transfer.Protocol == "" {
			transfer.Protocol = protocol
		}
	}
	return nil
}

// modelNodeSupportsDevices keeps transport selection and cache preparation on nodes usable by serving Pods.
// Preparation consumes no devices, but must not bind a local volume where the later device requests cannot fit.
func modelNodeSupportsDevices(node corev1.Node, accelerator string, gpuCount int32, rdma *inferencev1alpha1.RDMAAllocation) bool {
	if !node.DeletionTimestamp.IsZero() || node.Spec.Unschedulable {
		return false
	}
	gpu := node.Status.Allocatable[corev1.ResourceName(accelerator)]
	if gpu.CmpInt64(int64(gpuCount)) < 0 {
		return false
	}
	if rdma != nil {
		capacity := node.Status.Allocatable[corev1.ResourceName(rdma.ResourceName)]
		if capacity.CmpInt64(int64(rdma.ResourceCount)) < 0 {
			return false
		}
	}
	tolerations := workload.AcceleratorTolerations(accelerator)
	for _, taint := range node.Spec.Taints {
		if (taint.Effect == corev1.TaintEffectNoSchedule || taint.Effect == corev1.TaintEffectNoExecute) && !slices.ContainsFunc(tolerations, func(toleration corev1.Toleration) bool {
			return toleration.ToleratesTaint(ctrl.Log, &taint, false)
		}) {
			return false
		}
	}
	for _, condition := range node.Status.Conditions {
		if condition.Type == corev1.NodeReady {
			return condition.Status == corev1.ConditionTrue
		}
	}
	return false
}
