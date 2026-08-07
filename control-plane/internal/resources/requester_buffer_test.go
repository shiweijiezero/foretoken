// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package resources

import (
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

func TestValidateRequesterBufferBudget(t *testing.T) {
	oneGiB := int64(1024 * 1024 * 1024)
	resources := inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{
		ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"},
	}}

	for _, bufferBytes := range []int64{0, -1, oneGiB} {
		if err := ValidateRequesterBufferBudget(resources, bufferBytes); err == nil {
			t.Fatalf("buffer %d was accepted for a 1Gi request", bufferBytes)
		}
	}
	if err := ValidateRequesterBufferBudget(resources, oneGiB-1); err != nil {
		t.Fatalf("buffer below request was rejected: %v", err)
	}

	limit := inferencev1alpha1.ResourceQuantity("512Mi")
	resources.Limits = &inferencev1alpha1.ComputeResourceLimits{Memory: &limit}
	if err := ValidateRequesterBufferBudget(resources, 512*1024*1024); err == nil {
		t.Fatal("buffer equal to memory limit was accepted")
	}

	resources.Requests.Memory = "1.5"
	if err := ValidateRequesterBufferBudget(resources, 1); err == nil {
		t.Fatal("fractional memory request was accepted")
	}
	resources.Requests.Memory = "1Gi"
	limit = "1.5"
	if err := ValidateRequesterBufferBudget(resources, 1); err == nil {
		t.Fatal("fractional memory limit was accepted")
	}
}
