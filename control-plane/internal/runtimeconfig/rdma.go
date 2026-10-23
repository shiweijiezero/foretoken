// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Normalizes network references and validates native RDMA allocations.
package runtimeconfig

import (
	"fmt"
	"slices"
	"strings"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/component-helpers/scheduling/corev1/nodeaffinity"
)

// RDMANetworks resolves native attachment references for candidate matching and
// execution comparison, preserving the explicit network precedence of retained Groups.
func RDMANetworks(namespace string, allocation *api.RDMAAllocation, network string) []string {
	var networks []string
	if network != "" {
		networks = []string{network}
	} else if allocation != nil {
		networks = slices.Clone(allocation.Networks)
	}
	for index, reference := range networks {
		if !strings.Contains(reference, "/") {
			networks[index] = namespace + "/" + reference
		}
	}
	slices.Sort(networks)
	networks = slices.Compact(networks)
	if len(networks) == 0 {
		return nil
	}
	return networks
}

// ValidateRDMAAllocation checks native device quantities and placement before callers
// project them into workloads; accelerator requests remain owned by the model resources.
func ValidateRDMAAllocation(allocation *api.RDMAAllocation, accelerator string) error {
	if allocation == nil {
		return nil
	}
	if allocation.Resources != nil && (allocation.ResourceName != "" || allocation.ResourceCount != 0) {
		return fmt.Errorf("RDMA resources cannot be combined with retained resourceName/resourceCount")
	}
	resolved := allocation.Canonical()
	if len(resolved.Resources) == 0 {
		return fmt.Errorf("RDMA allocation requires device resources")
	}
	for name, quantity := range resolved.Resources {
		key := string(name)
		if !strings.Contains(key, "/") || strings.Contains(key, corev1.ResourceDefaultNamespacePrefix) || strings.HasPrefix(key, corev1.DefaultResourceRequestsPrefix) || len(validation.IsQualifiedName(corev1.DefaultResourceRequestsPrefix+key)) != 0 {
			return fmt.Errorf("RDMA resource %q must be a Kubernetes extended resource", name)
		}
		if key == accelerator {
			return fmt.Errorf("RDMA resources must not replace model accelerator resource %q", name)
		}
		if quantity.Sign() <= 0 || quantity.CmpInt64(quantity.Value()) != 0 {
			return fmt.Errorf("RDMA resource %q must request a positive whole allocation", name)
		}
	}
	if resolved.RequiredNodeAffinity != nil {
		if _, err := nodeaffinity.NewNodeSelector(resolved.RequiredNodeAffinity); err != nil {
			return fmt.Errorf("RDMA required node affinity: %w", err)
		}
	}
	return nil
}
