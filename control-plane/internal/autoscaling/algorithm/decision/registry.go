// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Lists the capacity-recommendation algorithms compiled into the controller.
package decision

import "github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"

// Descriptors returns the decision algorithms compiled into the controller.
func Descriptors() []core.DecisionDescriptor {
	return []core.DecisionDescriptor{
		aimdDescriptor,
		manualDescriptor,
		queueDescriptor,
		queueThresholdDescriptor,
		tokenLoadDescriptor,
	}
}
