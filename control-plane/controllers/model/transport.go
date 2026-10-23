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
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	corev1 "k8s.io/api/core/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/component-helpers/scheduling/corev1/nodeaffinity"
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
	configs := make([]vllmconfig.EffectiveConfig, len(pools))
	if len(profile.RDMABindings) != 0 {
		for index, pool := range pools {
			if pool.DesiredGroups == 0 || pool.Template.Backend != "vllm" {
				continue
			}
			effective, err := vllmconfig.Compile(pool.Template)
			if err != nil {
				return fmt.Errorf("resolve Pool %q RDMA requirements: %w", pool.Name, err)
			}
			configs[index] = effective
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

	candidates := make([][]*inferencev1alpha1.RDMAAllocation, len(pools))
	selected := make([]*inferencev1alpha1.RDMAAllocation, len(pools))
	var pdPools []int
	for index, pool := range pools {
		if requestsRDMA[index] {
			var err error
			candidates[index], err = modelRDMACandidates(service.Namespace, pool.Template, profile, nodes.Items)
			if err != nil {
				return fmt.Errorf("resolve Pool %q RDMA binding: %w", pool.Name, err)
			}
			if len(candidates[index]) > 0 {
				selected[index] = candidates[index][0]
			} else if pool.Template.Network != "" {
				return fmt.Errorf("Pool %q has no available RDMA binding for network %s/%s", pool.Name, service.Namespace, pool.Template.Network)
			}
		}
		if transfer := pool.Template.KVTransfer; pool.DesiredGroups > 0 && transfer != nil && transfer.Protocol != "tcp" {
			pdPools = append(pdPools, index)
		}
	}

	// Choose one attachment domain for all P/D stages, not an arbitrary collection of NICs.
	// An empty attachment set only preserves the same explicit device allocation across stages.
	protocol := "tcp"
	if len(pdPools) > 0 {
		for _, network := range candidates[pdPools[0]] {
			choice := slices.Clone(selected)
			matched := true
			for _, index := range pdPools {
				candidate := slices.IndexFunc(candidates[index], func(allocation *inferencev1alpha1.RDMAAllocation) bool {
					return slices.Equal(network.Networks, allocation.Networks) &&
						(len(network.Networks) > 0 || apiequality.Semantic.DeepEqual(network.Resources, allocation.Resources))
				})
				if candidate < 0 {
					matched = false
					break
				}
				choice[index] = candidates[index][candidate]
			}
			if matched {
				selected, protocol = choice, "rdma"
				break
			}
		}
		if protocol != "rdma" {
			for _, index := range pdPools {
				if pools[index].Template.KVTransfer.Protocol == "rdma" || requestsRDMA[index] && pools[index].Template.Network != "" {
					return fmt.Errorf("P/D RDMA requires an available common network binding across all Pools")
				}
			}
		}
	}
	// Stopped Pools inherit the common protocol but acquire devices only when capacity is enabled.
	for index := range pools {
		template := &pools[index].Template
		if transfer := template.KVTransfer; transfer != nil && transfer.Protocol == "" {
			transfer.Protocol = protocol
		}
		if requestsRDMA[index] && !configs[index].RequestsRDMA(*template, profile.DeviceResourceName) {
			selected[index] = nil
		}
		template.RDMA = selected[index]
	}
	return nil
}

// modelRDMACandidates returns complete installer bindings that can place every member of a Pool.
// Namespace restrictions come from the installer; Kubernetes still owns device allocation and scheduling.
func modelRDMACandidates(namespace string, template inferencev1alpha1.NormalizedPoolTemplate, profile resolver.RuntimeProfile, nodes []corev1.Node) ([]*inferencev1alpha1.RDMAAllocation, error) {
	var candidates []*inferencev1alpha1.RDMAAllocation
	selector := labels.SelectorFromSet(template.NodeSelector)
	for _, binding := range profile.RDMABindings {
		if binding.Namespace != "" && binding.Namespace != namespace {
			continue
		}
		allocation := binding.Allocation.Canonical()
		allocation.Networks = runtimeconfig.RDMANetworks(namespace, allocation, "")
		if template.Network != "" {
			explicit := runtimeconfig.RDMANetworks(namespace, nil, template.Network)
			if binding.AllowExplicitNetwork {
				allocation.Networks = explicit
			} else if !slices.Equal(allocation.Networks, explicit) {
				continue
			}
		}
		var placement *nodeaffinity.NodeSelector
		if allocation.RequiredNodeAffinity != nil {
			var err error
			placement, err = nodeaffinity.NewNodeSelector(allocation.RequiredNodeAffinity)
			if err != nil {
				return nil, err
			}
		}
		requests := modelResourceRequirements(template.Resources, profile.DeviceResourceName, allocation).Requests
		var eligible int32
		for _, node := range nodes {
			if !selector.Matches(labels.Set(node.Labels)) || profile.NodeSelectorKey != "" && node.Labels[profile.NodeSelectorKey] != profile.NodeSelectorValue {
				continue
			}
			if modelNodeFitsRequests(node, profile.DeviceResourceName, requests, placement) {
				eligible++
			}
		}
		if eligible >= template.NodeCount {
			candidates = append(candidates, allocation)
		}
	}
	return candidates, nil
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
			selections[index].RDMA = group.Spec.RDMA.Canonical()
			if selections[index].RDMA != nil {
				selections[index].RDMA.Networks = runtimeconfig.RDMANetworks(service.Namespace, group.Spec.RDMA, group.Spec.Network)
			}
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
		pools[index].Template.RDMA = selections[index].RDMA.Canonical()
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
func modelNodeFitsRequests(node corev1.Node, accelerator string, requests corev1.ResourceList, placement *nodeaffinity.NodeSelector) bool {
	if !node.DeletionTimestamp.IsZero() || node.Spec.Unschedulable || placement != nil && !placement.Match(&node) {
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
