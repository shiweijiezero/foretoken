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
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	corev1 "k8s.io/api/core/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// resolveModelTransports selects advertised RDMA allocations and fills omitted P/D protocols.
// The scheduler owns device allocation and free capacity; active workload occupancy does not change this selection.
func (reconciler *ServiceReconciler) resolveModelTransports(ctx context.Context, service *inferencev1alpha1.ModelService, pools []compiler.ModelPool, previous map[string]*inferencev1alpha1.ModelPool) error {
	retained, err := reconciler.retainModelTransports(ctx, service, pools, previous)
	if err != nil || retained {
		return err
	}
	profile := reconciler.RuntimeProfile
	requestsRDMA := make([]bool, len(pools))
	if profile.RDMA != nil {
		for index, pool := range pools {
			if pool.Template.Backend != "vllm" {
				continue
			}
			effective, err := vllmconfig.Compile(pool.Template)
			if err != nil {
				return fmt.Errorf("resolve Pool %q RDMA requirements: %w", pool.Name, err)
			}
			requestsRDMA[index] = effective.RequestsRDMA(pool.Template, profile.DeviceResourceName)
		}
	}
	var nodes corev1.NodeList
	if slices.Contains(requestsRDMA, true) {
		reader := reconciler.APIReader
		if reader == nil {
			reader = reconciler.Client
		}
		if err := reader.List(ctx, &nodes); err != nil {
			return fmt.Errorf("discover runtime RDMA allocation: %w", err)
		}
		// Every Pool inherits the same service cache; existing consumers constrain device placement too.
		var err error
		nodes.Items, err = runtimeconfig.RuntimeCacheNodes(ctx, reader, service.Namespace, pools[0].Template.RuntimeCache, nodes.Items)
		if err != nil {
			return err
		}
	}

	protocol := "rdma"
	for index := range pools {
		template := &pools[index].Template
		template.RDMA = nil
		if allocation := profile.RDMA; requestsRDMA[index] {
			selector := labels.SelectorFromSet(template.NodeSelector)
			requests := modelResourceRequirements(template.Resources, profile.DeviceResourceName, allocation).Requests
			var eligible int32
			for _, node := range nodes.Items {
				if !selector.Matches(labels.Set(node.Labels)) || profile.NodeSelectorKey != "" && node.Labels[profile.NodeSelectorKey] != profile.NodeSelectorValue {
					continue
				}
				if modelNodeFitsRequests(node, profile.DeviceResourceName, requests) {
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

// retainModelTransports keeps an accepted execution choice independent of transient node health.
// A new service generation or an unresolved target still discovers current capacity as one cohort.
func (reconciler *ServiceReconciler) retainModelTransports(ctx context.Context, service *inferencev1alpha1.ModelService, pools []compiler.ModelPool, previous map[string]*inferencev1alpha1.ModelPool) (bool, error) {
	if service.Status.ServingGeneration != service.Generation || len(service.Status.ServingPoolRevisions) == 0 {
		return false, nil
	}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reader.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
		return false, fmt.Errorf("read selected model transports: %w", err)
	}
	selections := make([]inferencev1alpha1.NormalizedPoolTemplate, len(pools))
	protocol := ""
	for index, pool := range pools {
		old := previous[pool.Name]
		if old == nil || !old.DeletionTimestamp.IsZero() || !apiequality.Semantic.DeepEqual(old.Spec.Template.RuntimeCache, pool.Template.RuntimeCache) {
			return false, nil
		}
		selections[index] = old.Spec.Template
		if pool.DesiredGroups > 0 {
			selected := slices.IndexFunc(service.Status.ServingPoolRevisions, func(revision inferencev1alpha1.ServingPoolRevision) bool {
				return revision.PoolName == pool.Name && revision.PoolUID == string(old.UID)
			})
			if selected < 0 {
				return false, nil
			}
			revision := service.Status.ServingPoolRevisions[selected]
			groupIndex := slices.IndexFunc(groups.Items, func(group inferencev1alpha1.ModelGroup) bool {
				return group.Spec.ModelPoolRef.UID == revision.PoolUID && group.Spec.Revision == revision.Revision && metav1.IsControlledBy(&group, old)
			})
			if groupIndex < 0 {
				return false, nil
			}
			group := &groups.Items[groupIndex]
			if !apiequality.Semantic.DeepEqual(group.Spec.Artifacts.Cache, pool.Template.RuntimeCache) {
				return false, nil
			}
			selections[index].RDMA = group.Spec.RDMA
			if pool.Template.KVTransfer != nil {
				if group.Spec.PDRuntime == nil {
					return false, nil
				}
				selections[index].KVTransfer = pool.Template.KVTransfer.DeepCopy()
				selections[index].KVTransfer.Protocol = group.Spec.PDRuntime.Protocol
			}
		}
		if transfer := pool.Template.KVTransfer; transfer != nil {
			selected := selections[index].KVTransfer
			if selected == nil || selected.Protocol == "" || transfer.Protocol != "" && transfer.Protocol != selected.Protocol {
				return false, nil
			}
			if pool.DesiredGroups > 0 {
				if protocol != "" && protocol != selected.Protocol {
					return false, nil
				}
				protocol = selected.Protocol
			}
		}
	}
	// Publish both P/D choices together; a partial match must not mix retained and newly selected protocols.
	for index := range pools {
		pools[index].Template.RDMA = selections[index].RDMA.DeepCopy()
		if transfer := pools[index].Template.KVTransfer; transfer != nil {
			transfer.Protocol = selections[index].KVTransfer.Protocol
			if pools[index].DesiredGroups == 0 && protocol != "" {
				transfer.Protocol = protocol
			}
		}
	}
	return true, nil
}

// modelNodeFitsRequests checks static serving requests against advertised node capacity.
// Transport selection and CPU-only preparation share this placement constraint; the scheduler owns occupancy.
func modelNodeFitsRequests(node corev1.Node, accelerator string, requests corev1.ResourceList) bool {
	if !node.DeletionTimestamp.IsZero() || node.Spec.Unschedulable {
		return false
	}
	for name, request := range requests {
		capacity := node.Status.Allocatable[name]
		if capacity.Cmp(request) < 0 {
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
	return modelNodeReady(node)
}

func modelNodeReady(node corev1.Node) bool {
	for _, condition := range node.Status.Conditions {
		if condition.Type == corev1.NodeReady {
			return condition.Status == corev1.ConditionTrue
		}
	}
	return false
}
