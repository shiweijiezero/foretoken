// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines model storage paths and source selection shared by runtime workload projection.
package runtimeconfig

import (
	"fmt"
	"net/url"
	"path"
	"strings"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

const (
	// ModelRootEnv identifies the persistent model root consumed by both data-plane processes.
	ModelRootEnv = "FORETOKEN_MODEL_ROOT"
	// ModelCacheRootEnv selects provider-owned downloads shared by cache consumers.
	ModelCacheRootEnv = "FORETOKEN_MODEL_CACHE_ROOT"
	// PreparationRootEnv separates deployment acquisition state from shared model files.
	PreparationRootEnv = "FORETOKEN_MODEL_PREPARATION_ROOT"
	// TemporaryModelRootEnv is consumed by foretoken-artifacts in the frontend process.
	TemporaryModelRootEnv = "FORETOKEN_TEMPORARY_MODEL_ROOT"
	// RuntimeCacheBindingEnv carries namespace/claim identity for prepared tokenizer reuse.
	RuntimeCacheBindingEnv = "FORETOKEN_RUNTIME_CACHE_BINDING"
	// DeploymentUIDEnv binds runtime consumers to the deployment's private state directories.
	DeploymentUIDEnv = "FORETOKEN_DEPLOYMENT_UID"
	// ProfileRootEnv records the serving workload's actual capture storage directory.
	ProfileRootEnv = "FORETOKEN_PROFILE_ROOT"
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

// EncoderCacheDirectory gives volume projection and engine configuration the same service-scoped path.
func EncoderCacheDirectory(ec *api.ModelGroupECRuntimeConfig) string {
	return path.Join(ec.ServiceUID, fmt.Sprint(ec.Generation), "profile="+url.PathEscape(ec.ProfileRevision))
}

// ModelServiceReferencesKV identifies model consumers for dependency watches and KVService cleanup.
func ModelServiceReferencesKV(service *api.ModelService, name string) bool {
	if store := service.Spec.KVCache; store != nil && store.MooncakeStore != nil && store.MooncakeStore.KVServiceRef != nil && store.MooncakeStore.KVServiceRef.Name == name {
		return true
	}
	for _, pool := range service.Spec.ModelPools {
		if store := pool.KVCache; store != nil && store.MooncakeStore != nil && store.MooncakeStore.KVServiceRef != nil && store.MooncakeStore.KVServiceRef.Name == name {
			return true
		}
	}
	return false
}

// ModelDirectory locates user-provided model files, outside managed download-cache cleanup.
func ModelDirectory(dataRoot string) string {
	return path.Join(dataRoot, "models")
}

// ModelCacheDirectory locates provider-owned downloads shared across namespace bindings.
func ModelCacheDirectory(dataRoot string) string {
	return path.Join(dataRoot, "shared", "models")
}

// RuntimeCacheIdentity permits direct tokenizer reuse only between consumers of the same claim and layout.
func RuntimeCacheIdentity(namespace, claim string, preparationVersion int32) string {
	binding := namespace + "/" + claim
	if preparationVersion >= SharedModelCachePreparationVersion {
		binding += "/shared-models"
	}
	return binding
}

// DeploymentCacheDirectory isolates mutable runtime state for one deployment across Pod replacement.
func DeploymentCacheDirectory(dataRoot, deploymentUID string) string {
	return path.Join(dataRoot, "deployments", deploymentUID)
}
