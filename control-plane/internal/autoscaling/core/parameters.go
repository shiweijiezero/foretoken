// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package core

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// DecodeParameters decodes explicitly declared stage fields, preserving algorithm-owned defaults when omitted.
func DecodeParameters(raw json.RawMessage, fields map[string]any) error {
	if len(raw) == 0 {
		return nil
	}
	var parameters map[string]json.RawMessage
	if err := json.Unmarshal(raw, &parameters); err != nil {
		return fmt.Errorf("autoscaling parameters must be an object: %w", err)
	}
	if parameters == nil {
		return fmt.Errorf("autoscaling parameters must be an object")
	}
	for name, value := range parameters {
		field, ok := fields[name]
		if !ok {
			return fmt.Errorf("unknown autoscaling parameter %q", name)
		}
		if bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return fmt.Errorf("autoscaling parameter %q must not be null", name)
		}
		if err := json.Unmarshal(value, field); err != nil {
			return fmt.Errorf("autoscaling parameter %q: %w", name, err)
		}
	}
	return nil
}
