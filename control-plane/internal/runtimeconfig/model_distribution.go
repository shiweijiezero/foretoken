// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the platform-selected file distribution endpoint used by preparation workers.

package runtimeconfig

import (
	"errors"
	"path"
)

const DragonflySocketEnv = "FORETOKEN_DRAGONFLY_SOCKET"

// ModelDistributionProfile selects an optional node-local Dragonfly download service.
// The platform installer resolves the host socket; preparation Pods mount its parent directory.
type ModelDistributionProfile struct {
	DragonflySocketPath string
}

// Validate checks the optional host socket before the controller projects it into workloads.
func (profile ModelDistributionProfile) Validate() error {
	if socket := profile.DragonflySocketPath; socket != "" && (!path.IsAbs(socket) || path.Clean(socket) != socket || path.Dir(socket) == "/") {
		return errors.New("Dragonfly socket must be an absolute file path below a socket directory")
	}
	return nil
}
