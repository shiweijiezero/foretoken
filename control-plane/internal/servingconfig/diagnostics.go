// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reads the serving generation actually applied by a frontend process.

package servingconfig

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
)

type Diagnostics struct {
	ActiveGeneration   *uint64 `json:"active_generation"`
	TargetGeneration   *uint64 `json:"target_generation"`
	ConfigurationError *string `json:"configuration_error"`
}

// ReadDiagnostics is shared by configuration acknowledgement and route withdrawal.
func ReadDiagnostics(ctx context.Context, transport *http.Client, endpoint string) (Diagnostics, error) {
	var diagnostics Diagnostics
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"/statusz", nil)
	if err != nil {
		return diagnostics, err
	}
	response, err := transport.Do(request)
	if err != nil {
		return diagnostics, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return diagnostics, fmt.Errorf("frontend status returned HTTP %d", response.StatusCode)
	}
	if err := json.NewDecoder(response.Body).Decode(&diagnostics); err != nil {
		return diagnostics, fmt.Errorf("decode frontend status: %w", err)
	}
	return diagnostics, nil
}
