// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves platform-selected queue identities before execution contracts are published.
package runtimeconfig

import (
	"fmt"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"k8s.io/apimachinery/pkg/util/validation"
)

// ResolveLeaderWorkerScheduling validates native queue names and normalizes an empty selection to nil.
// Queue existence and admission remain with Volcano and Kueue in the workload's cluster and namespace.
func ResolveLeaderWorkerScheduling(selection *inferencev1alpha1.LeaderWorkerScheduling) (*inferencev1alpha1.LeaderWorkerScheduling, error) {
	if selection == nil || *selection == (inferencev1alpha1.LeaderWorkerScheduling{}) {
		return nil, nil
	}
	for _, queue := range []struct{ name, value string }{
		{"volcanoQueueName", selection.VolcanoQueueName},
		{"kueueLocalQueueName", selection.KueueLocalQueueName},
	} {
		if queue.value == "" {
			continue
		}
		if errors := validation.IsDNS1123Subdomain(queue.value); len(errors) != 0 {
			return nil, fmt.Errorf("leader-worker scheduling %s: %s", queue.name, strings.Join(errors, "; "))
		}
	}
	if errors := validation.IsValidLabelValue(selection.KueueLocalQueueName); len(errors) != 0 {
		return nil, fmt.Errorf("leader-worker scheduling kueueLocalQueueName: %s", strings.Join(errors, "; "))
	}
	return selection, nil
}
