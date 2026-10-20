// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Dispatches the control-plane binary's platform and maintenance modes.
package main

import (
	"errors"
	"flag"
	"log"
	"os"
)

// main selects a maintenance command or starts the deployment-scoped serving controller.
func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "maintain-log-storage":
			if err := maintainLogStorage(os.Args[2:]); err != nil && !errors.Is(err, flag.ErrHelp) {
				log.Fatal(err)
			}
			return
		case "maintain-model-cache":
			if err := maintainModelCache(os.Args[2:]); err != nil && !errors.Is(err, flag.ErrHelp) {
				log.Fatal(err)
			}
			return
		case "manage-deployments":
			if err := manageDeployments(os.Args[2:]); err != nil && !errors.Is(err, flag.ErrHelp) {
				log.Fatal(err)
			}
			return
		}
	}
	runServingController()
}
