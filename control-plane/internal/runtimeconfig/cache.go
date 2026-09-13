// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines model-directory layout shared by frontend and engine workload projection.
package runtimeconfig

import "path"

const (
	// ModelRootEnv identifies the persistent model root consumed by both data-plane processes.
	ModelRootEnv = "FORETOKEN_MODEL_ROOT"
	// TemporaryModelRootEnv is consumed by foretoken-artifacts in the frontend process.
	TemporaryModelRootEnv = "FORETOKEN_TEMPORARY_MODEL_ROOT"
)

// ModelDirectory returns the stable model area of a workload's persistent data root.
// Model providers keep their own cache layouts beneath this directory.
func ModelDirectory(dataRoot string) string {
	return path.Join(dataRoot, "models")
}
