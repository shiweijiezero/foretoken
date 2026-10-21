// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Lists the observation triggers compiled into the controller.
package trigger

import "github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"

// Descriptors returns the trigger algorithms compiled into the controller.
func Descriptors() []core.TriggerDescriptor {
	return []core.TriggerDescriptor{periodicDescriptor}
}
