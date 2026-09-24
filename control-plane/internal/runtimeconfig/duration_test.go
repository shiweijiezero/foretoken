// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package runtimeconfig

import (
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

func TestPositiveDurationSeconds(t *testing.T) {
	tests := []struct {
		value inferencev1alpha1.Duration
		want  int64
		valid bool
	}{
		{value: "1ms", want: 1, valid: true},
		{value: "1500ms", want: 2, valid: true},
		{value: "2147483647s", want: 2147483647, valid: true},
		{value: "0s"},
		{value: "2147483648s"},
	}
	for _, test := range tests {
		t.Run(string(test.value), func(t *testing.T) {
			got, err := PositiveDurationSeconds(test.value, "test")
			if (err == nil) != test.valid {
				t.Fatalf("PositiveDurationSeconds(%q) error = %v", test.value, err)
			}
			if test.valid && got != test.want {
				t.Fatalf("PositiveDurationSeconds(%q) = %d, want %d", test.value, got, test.want)
			}
		})
	}
}
