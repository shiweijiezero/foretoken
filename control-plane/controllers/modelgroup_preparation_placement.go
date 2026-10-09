// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package controllers

import (
	"context"
	"errors"
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var errNoPreparationPlacement = errors.New("no eligible accelerator node for model preparation")

// preparationNodeAffinity restricts a new CPU preparation Job to nodes that can
// also host its serving Pod. The scheduler still decides placement and PVC binding.
func (reconciler *ModelGroupReconciler) preparationNodeAffinity(ctx context.Context, group *inferencev1alpha1.ModelGroup) (*corev1.NodeAffinity, error) {
	var nodes corev1.NodeList
	if err := reconciler.List(ctx, &nodes, client.MatchingLabels(group.Spec.Accelerator.NodeSelector)); err != nil {
		return nil, fmt.Errorf("list accelerator nodes for model preparation: %w", err)
	}
	resourceName := corev1.ResourceName(group.Spec.Accelerator.DeviceResourceName)
	var terms []corev1.NodeSelectorTerm
	for _, node := range nodes.Items {
		allocatable := node.Status.Allocatable[resourceName]
		if node.Spec.Unschedulable || allocatable.Value() < int64(group.Spec.Resources.Requests.GPU.Count) {
			continue
		}
		ready := false
		for _, condition := range node.Status.Conditions {
			if condition.Type == corev1.NodeReady && condition.Status == corev1.ConditionTrue {
				ready = true
				break
			}
		}
		if !ready || !preparationNodeTaintsSupported(node.Spec.Taints, string(resourceName)) {
			continue
		}
		// Node field selectors accept one value; separate terms express eligible alternatives.
		terms = append(terms, corev1.NodeSelectorTerm{MatchFields: []corev1.NodeSelectorRequirement{{
			Key: "metadata.name", Operator: corev1.NodeSelectorOpIn, Values: []string{node.Name},
		}}})
	}
	if len(terms) == 0 {
		return nil, errNoPreparationPlacement
	}
	return &corev1.NodeAffinity{RequiredDuringSchedulingIgnoredDuringExecution: &corev1.NodeSelector{
		NodeSelectorTerms: terms,
	}}, nil
}

// acceleratorTolerations applies the same accelerator-node admission to both
// serving Pods and CPU preparation Jobs, without allocating a device to the Job.
func acceleratorTolerations(resourceName string) []corev1.Toleration {
	return []corev1.Toleration{{Key: resourceName, Operator: corev1.TolerationOpExists, Effect: corev1.TaintEffectNoSchedule}}
}

// preparationNodeTaintsSupported excludes nodes neither Pod can use.
func preparationNodeTaintsSupported(taints []corev1.Taint, accelerator string) bool {
	for _, taint := range taints {
		if taint.Effect != corev1.TaintEffectNoSchedule && taint.Effect != corev1.TaintEffectNoExecute {
			continue
		}
		if taint.Key != accelerator || taint.Effect != corev1.TaintEffectNoSchedule {
			return false
		}
	}
	return true
}
