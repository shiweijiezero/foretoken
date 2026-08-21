// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Validates container memory allocations reserved for Mooncake requesters.

package resources

import (
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"k8s.io/apimachinery/pkg/api/resource"
)

// ValidateRequesterBufferBudget ensures a per-rank Mooncake requester buffer is
// a positive exact byte allocation and leaves memory for the model process.
func ValidateRequesterBufferBudget(resources inferencev1alpha1.ModelResources, requesterBufferBytes int64) error {
	if requesterBufferBytes < 1 {
		return fmt.Errorf("requester buffer must be a positive exact integer byte quantity")
	}
	requestBytes, err := parsePositiveExactBytes("resources.requests.memory", string(resources.Requests.Memory))
	if err != nil {
		return err
	}
	if requesterBufferBytes >= requestBytes {
		return fmt.Errorf("requester buffer must be strictly less than resources.requests.memory")
	}
	if resources.Limits != nil && resources.Limits.Memory != nil {
		limitBytes, err := parsePositiveExactBytes("resources.limits.memory", string(*resources.Limits.Memory))
		if err != nil {
			return err
		}
		if requesterBufferBytes >= limitBytes {
			return fmt.Errorf("requester buffer must be strictly less than resources.limits.memory")
		}
	}
	return nil
}

func parsePositiveExactBytes(field, value string) (int64, error) {
	quantity, err := resource.ParseQuantity(value)
	if err != nil {
		return 0, fmt.Errorf("parse %s: %w", field, err)
	}
	bytes, exact := quantity.AsInt64()
	if !exact || bytes < 1 {
		return 0, fmt.Errorf("%s must be a positive exact integer byte quantity", field)
	}
	return bytes, nil
}
