// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Validates container memory allocations reserved for Mooncake requesters.

package resources

import (
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// ValidateRequesterBufferBudget ensures a per-rank Mooncake requester buffer is
// a positive exact byte allocation and leaves memory for the model process.
func ValidateRequesterBufferBudget(resources inferencev1alpha1.ModelResources, requesterBufferBytes int64) error {
	if requesterBufferBytes < 1 {
		return fmt.Errorf("requester buffer must be a positive exact integer byte quantity")
	}
	requestBytes, err := ParsePositiveBytes("resources.requests.memory", string(resources.Requests.Memory))
	if err != nil {
		return err
	}
	if requesterBufferBytes >= requestBytes {
		return fmt.Errorf("requester buffer must be strictly less than resources.requests.memory")
	}
	if resources.Limits != nil && resources.Limits.Memory != nil {
		limitBytes, err := ParsePositiveBytes("resources.limits.memory", string(*resources.Limits.Memory))
		if err != nil {
			return err
		}
		if requesterBufferBytes >= limitBytes {
			return fmt.Errorf("requester buffer must be strictly less than resources.limits.memory")
		}
	}
	return nil
}
