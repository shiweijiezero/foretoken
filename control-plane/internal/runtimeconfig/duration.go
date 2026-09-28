// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package runtimeconfig

import (
	"fmt"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// PositiveDurationSeconds rounds up a runtime budget and bounds it for Kubernetes int32 fields.
func PositiveDurationSeconds(value inferencev1alpha1.Duration, name string) (int64, error) {
	duration, err := time.ParseDuration(string(value))
	if err != nil || duration <= 0 {
		return 0, fmt.Errorf("%s timeout must be a positive duration", name)
	}
	seconds := int64(duration / time.Second)
	if duration%time.Second != 0 {
		seconds++
	}
	const maxSeconds = int64(1<<31 - 1)
	if seconds > maxSeconds {
		return 0, fmt.Errorf("%s timeout must not exceed %d seconds", name, maxSeconds)
	}
	return seconds, nil
}
