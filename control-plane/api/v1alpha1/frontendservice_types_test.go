// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package v1alpha1

import (
	"encoding/json"
	"testing"
)

func TestFrontendServiceReplicasPreservesExplicitZero(t *testing.T) {
	zero := int32(0)
	frontend := FrontendService{Spec: FrontendServiceSpec{Replicas: &zero}}

	encoded, err := json.Marshal(frontend)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	spec := decoded["spec"].(map[string]any)
	if replicas, exists := spec["replicas"]; !exists || replicas != float64(0) {
		t.Fatalf("serialized FrontendService replicas = %#v", replicas)
	}
}
