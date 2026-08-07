// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package v1alpha1

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestFrontendServiceReplicasPreservesExplicitZero(t *testing.T) {
	zero := int32(0)
	frontend := FrontendService{Spec: FrontendServiceSpec{Replicas: &zero}}

	encoded, err := json.Marshal(frontend)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(encoded), `"replicas":0`) {
		t.Fatalf("serialized FrontendService omitted explicit zero replicas: %s", encoded)
	}
}
