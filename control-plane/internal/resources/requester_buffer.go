// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Validates container memory allocations reserved for Mooncake requesters.

package resources

import (
	"fmt"
	"math/big"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// ValidateRequesterBufferBudget checks that all per-worker Mooncake buffers fit
// within one compiled model Pod's memory request and limit, leaving room for execution.
func ValidateRequesterBufferBudget(resources inferencev1alpha1.ModelResources, requesterBufferBytes int64) error {
	if requesterBufferBytes < 1 {
		return fmt.Errorf("requester buffer must be a positive exact integer byte quantity")
	}
	requestBytes, err := ParsePositiveBytes("resources.requests.memory", string(resources.Requests.Memory))
	if err != nil {
		return err
	}
	// Compiled topology assigns one worker per requested GPU. Each worker owns
	// its full buffer; use exact multiplication even when their sum exceeds int64.
	workers := int64(resources.Requests.GPU.Count)
	totalBytes := big.NewInt(requesterBufferBytes)
	totalBytes.Mul(totalBytes, big.NewInt(workers))
	if totalBytes.Cmp(big.NewInt(requestBytes)) >= 0 {
		return fmt.Errorf("total requester buffers for %d workers must be strictly less than resources.requests.memory", workers)
	}
	if resources.Limits != nil && resources.Limits.Memory != nil {
		limitBytes, err := ParsePositiveBytes("resources.limits.memory", string(*resources.Limits.Memory))
		if err != nil {
			return err
		}
		if totalBytes.Cmp(big.NewInt(limitBytes)) >= 0 {
			return fmt.Errorf("total requester buffers for %d workers must be strictly less than resources.limits.memory", workers)
		}
	}
	return nil
}
