// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Lists the replica-adjustment algorithms compiled into the controller.
package adjustment

import "github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"

// Descriptors returns the adjustment algorithms compiled into the controller.
func Descriptors() []core.AdjustmentDescriptor {
	return []core.AdjustmentDescriptor{directDescriptor, stepDescriptor}
}
