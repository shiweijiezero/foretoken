// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package controllers

import (
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestValidateGroupRuntimeBackends(t *testing.T) {
	cases := []struct {
		name    string
		backend string
		role    inferencev1alpha1.ModelRole
		wantErr bool
	}{
		{"vllm aggregate", "vllm", inferencev1alpha1.ModelRoleAggregate, false},
		{"vllm decode", "vllm", inferencev1alpha1.ModelRoleDecode, false},
		{"sglang aggregate", "sglang", inferencev1alpha1.ModelRoleAggregate, false},
		{"sglang decode", "sglang", inferencev1alpha1.ModelRoleDecode, true},
		{"sglang prefill", "sglang", inferencev1alpha1.ModelRolePrefill, true},
		{"unknown backend", "tensorrt", inferencev1alpha1.ModelRoleAggregate, true},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			group := testGroup()
			group.Spec.Runtime.Backend = test.backend
			group.Spec.Role = test.role
			err := validateGroupRuntime(&group)
			if (err != nil) != test.wantErr {
				t.Fatalf("validateGroupRuntime(%q/%q) err = %v, wantErr = %v", test.backend, test.role, err, test.wantErr)
			}
		})
	}
}

func TestValidateGroupRuntimeRejectsMultiMember(t *testing.T) {
	group := testGroup()
	group.Spec.MemberCount = 2
	if err := validateGroupRuntime(&group); err == nil {
		t.Fatal("multi-member group was accepted")
	}
}

func testGroup() inferencev1alpha1.ModelGroup {
	return inferencev1alpha1.ModelGroup{
		ObjectMeta: metav1.ObjectMeta{Name: "group", Namespace: "default"},
		Spec: inferencev1alpha1.ModelGroupSpec{
			Role:        inferencev1alpha1.ModelRoleAggregate,
			NodeCount:   1,
			MemberCount: 1,
			Runtime:     inferencev1alpha1.ModelGroupRuntime{Backend: "vllm"},
		},
	}
}
