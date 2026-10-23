// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the shared model-download service binding consumed by preparation workers.

package runtimeconfig

import (
	"errors"
	"net/url"
)

const ModelDownloadProxyEnv = "FORETOKEN_MODEL_DOWNLOAD_PROXY"

// ModelDistributionProfile identifies the optional internal model-download service.
type ModelDistributionProfile struct {
	ProxyURL string
}

// Validate checks the internal endpoint before preparation workers receive it.
func (profile ModelDistributionProfile) Validate() error {
	if profile.ProxyURL == "" {
		return nil
	}
	endpoint, err := url.Parse(profile.ProxyURL)
	if err != nil || endpoint.Scheme != "http" || endpoint.Hostname() == "" || endpoint.User != nil ||
		(endpoint.Path != "" && endpoint.Path != "/") || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return errors.New("model download proxy must be a credential-free internal HTTP endpoint")
	}
	return nil
}
