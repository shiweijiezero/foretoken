// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects immutable placement and quota choices into native LeaderWorkerSet interfaces.
package model

import (
	"context"
	"errors"
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

var errKueueAdmissionInactive = errors.New("native Kueue LeaderWorkerSet admission is not enabled")

const (
	volcanoQueueAnnotation = "scheduling.volcano.sh/queue-name"
	kueueQueueLabel        = "kueue.x-k8s.io/queue-name"
)

// configureLeaderWorkerScheduling composes native placement and quota admission without owning their resources.
// The selection is already fixed in the Group; CPU preparation and single-node Deployments do not use it.
func configureLeaderWorkerScheduling(workload *lwsv1.LeaderWorkerSet, selection *inferencev1alpha1.LeaderWorkerScheduling) {
	if selection == nil {
		return
	}
	if selection.VolcanoQueueName != "" {
		if workload.Annotations == nil {
			workload.Annotations = make(map[string]string)
		}
		workload.Annotations[volcanoQueueAnnotation] = selection.VolcanoQueueName
		workload.Spec.LeaderWorkerTemplate.WorkerTemplate.Spec.SchedulerName = "volcano"
		if leader := workload.Spec.LeaderWorkerTemplate.LeaderTemplate; leader != nil {
			leader.Spec.SchedulerName = "volcano"
		}
	}
	if selection.KueueLocalQueueName != "" {
		if workload.Labels == nil {
			workload.Labels = make(map[string]string)
		}
		workload.Labels[kueueQueueLabel] = selection.KueueLocalQueueName
	}
}

// requireLeaderWorkerQueueAdmission checks native Kueue creation admission before allocating or repairing a workload.
// Existing objects can retain injected labels after the integration is disabled; a dry-run creation observes the current handler.
func (reconciler *GroupReconciler) requireLeaderWorkerQueueAdmission(ctx context.Context, workload *lwsv1.LeaderWorkerSet, selection *inferencev1alpha1.LeaderWorkerScheduling) error {
	if selection == nil || selection.KueueLocalQueueName == "" {
		return nil
	}
	admitted := workload.DeepCopy()
	admitted.GenerateName, admitted.Name = admitted.Name+"-", ""
	if err := reconciler.Create(ctx, admitted, client.FieldOwner(modelGroupFieldOwner), client.DryRunAll); err != nil {
		return fmt.Errorf("resolve native Kueue admission: %w", err)
	}
	template := admitted.Spec.LeaderWorkerTemplate
	if template.WorkerTemplate.Labels[kueueQueueLabel] != selection.KueueLocalQueueName ||
		template.LeaderTemplate != nil && template.LeaderTemplate.Labels[kueueQueueLabel] != selection.KueueLocalQueueName {
		return fmt.Errorf("%w for namespace %q", errKueueAdmissionInactive, workload.Namespace)
	}
	return nil
}
