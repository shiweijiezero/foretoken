// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines model-directory layout shared by frontend and engine workload projection.
package runtimeconfig

import "path"

// ModelRootEnv identifies the internal model root consumed by both data-plane processes.
const ModelRootEnv = "FORETOKEN_MODEL_ROOT"

// ModelDirectory returns the stable model area of a workload's persistent data root.
// Model providers keep their own cache layouts beneath this directory.
func ModelDirectory(dataRoot string) string {
	return path.Join(dataRoot, "models")
}
