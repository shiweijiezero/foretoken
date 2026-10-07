// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines model storage paths and source selection shared by runtime workload projection.
package runtimeconfig

import (
	"fmt"
	"path"
	"strings"
)

const (
	// ModelRootEnv identifies the persistent model root consumed by both data-plane processes.
	ModelRootEnv = "FORETOKEN_MODEL_ROOT"
	// TemporaryModelRootEnv is consumed by foretoken-artifacts in the frontend process.
	TemporaryModelRootEnv = "FORETOKEN_TEMPORARY_MODEL_ROOT"
	// RuntimeCacheBindingEnv carries namespace/claim identity for prepared tokenizer reuse.
	RuntimeCacheBindingEnv = "FORETOKEN_RUNTIME_CACHE_BINDING"
	// ModelPreparationScopeEnv selects the Pool revision's immutable acquisition publication.
	ModelPreparationScopeEnv = "FORETOKEN_MODEL_PREPARATION_SCOPE"
	// SourceRevisionAnnotation selects a CLI-published bundle for service reconciliation.
	SourceRevisionAnnotation = "inference.foretoken.io/source-revision"
	// SourceDirectoryEnv selects the complete source bundle consumed at data-plane startup.
	SourceDirectoryEnv = "FORETOKEN_SOURCE_DIRECTORY"
)

// SourceRevision resolves new service source selections according to the platform source mode.
// Absence requests no source override; a present annotation identifies one directory segment.
func SourceRevision(annotations map[string]string, sourceMode bool) (string, error) {
	revision, present := annotations[SourceRevisionAnnotation]
	if !present {
		return "", nil
	}
	if revision == "" {
		return "", fmt.Errorf("source revision annotation must be nonempty")
	}
	if !sourceMode {
		return "", fmt.Errorf("new source selection requires a source-installed platform (--source-mode)")
	}
	return revision, ValidateSourceRevision(revision)
}

// ValidateSourceRevision checks directory identity in controller-owned Pool and Group contracts.
// Execution of a persisted selection is independent of whether new source selections are enabled.
func ValidateSourceRevision(revision string) error {
	if revision == "." || revision == ".." || strings.ContainsAny(revision, "/\\\x00") {
		return fmt.Errorf("source revision must be a single directory segment")
	}
	return nil
}

// ModelDirectory returns the stable model area of a workload's persistent data root.
// Model providers keep their own cache layouts beneath this directory.
func ModelDirectory(dataRoot string) string {
	return path.Join(dataRoot, "models")
}
