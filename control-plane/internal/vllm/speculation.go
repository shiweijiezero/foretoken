// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package vllm

import (
	"encoding/json"
	"fmt"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
)

// CompileDraftSpeculation resolves the native neural profile for the ModelService compiler.
// It derives proposal weights from the Draft model without mutating arguments; nil selects independent drafting.
func CompileDraftSpeculation(targetModel, draftModel string, args inferencev1alpha1.EngineArguments) (*apiextensionsv1.JSON, error) {
	var profile *apiextensionsv1.JSON
	for name, value := range args {
		if strings.ReplaceAll(name, "_", "-") == "speculative-config" {
			if profile != nil {
				return nil, fmt.Errorf("Draft pool repeats speculative-config with different spellings")
			}
			profile = value.DeepCopy()
		}
	}
	if profile == nil {
		return nil, nil
	}
	var config map[string]json.RawMessage
	if err := json.Unmarshal(profile.Raw, &config); err != nil || config == nil {
		return nil, fmt.Errorf("Draft pool engineArgs.speculative-config must be an object")
	}
	var method, model string
	if value, exists := config["model"]; exists {
		if err := json.Unmarshal(value, &model); err != nil {
			return nil, fmt.Errorf("Draft pool speculative-config.model must be a string")
		}
	}
	if model != "" && model != draftModel {
		return nil, fmt.Errorf("Draft pool speculative-config.model must match its model")
	}
	if value, exists := config["method"]; exists {
		if err := json.Unmarshal(value, &method); err != nil {
			return nil, fmt.Errorf("Draft pool speculative-config.method must be a string")
		}
	}
	// Native MTP infers its proposal architecture from the Target checkpoint when no model is supplied.
	if model != "" || method != "mtp" || draftModel != targetModel {
		config["model"], _ = json.Marshal(draftModel)
	}
	raw, err := json.Marshal(config)
	if err != nil {
		return nil, err
	}
	return &apiextensionsv1.JSON{Raw: raw}, nil
}
