// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines persistent model and source layouts shared by runtime workload projection.
package runtimeconfig

import (
	"fmt"
	"path"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
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

// SourceRevision resolves service metadata without accepting source execution in release mode.
// Absence selects the image runtime; a present annotation must identify one directory segment.
func SourceRevision(annotations map[string]string, sourceMode bool) (string, error) {
	revision, present := annotations[SourceRevisionAnnotation]
	if !present {
		return "", nil
	}
	if revision == "" {
		return "", fmt.Errorf("source revision annotation must be nonempty")
	}
	return revision, validateSourceRevision(revision, sourceMode)
}

// ValidateSourceRuntime checks the persisted Pool or Group source contract before workload creation.
// Source execution requires an enabled platform and a persistent cache binding.
func ValidateSourceRuntime(revision string, sourceMode bool, cache *inferencev1alpha1.RuntimeCacheBinding) error {
	if revision == "" {
		return nil
	}
	if err := validateSourceRevision(revision, sourceMode); err != nil {
		return err
	}
	if cache == nil || cache.ClaimName == "" || cache.MountPath == "" {
		return fmt.Errorf("source execution requires a persistent RuntimeCache")
	}
	return nil
}

func validateSourceRevision(revision string, sourceMode bool) error {
	if !sourceMode {
		return fmt.Errorf("source revision requires a source-installed platform (--source-mode)")
	}
	if revision == "." || revision == ".." || strings.ContainsAny(revision, "/\\\x00") {
		return fmt.Errorf("source revision must be a single directory segment")
	}
	return nil
}

// SourceDirectory returns the immutable bundle location beneath a workload's resolved cache.
func SourceDirectory(dataRoot, revision string) string {
	return path.Join(dataRoot, "source", revision)
}

// ModelDirectory returns the stable model area of a workload's persistent data root.
// Model providers keep their own cache layouts beneath this directory.
func ModelDirectory(dataRoot string) string {
	return path.Join(dataRoot, "models")
}
