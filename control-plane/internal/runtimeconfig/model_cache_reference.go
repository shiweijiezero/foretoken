// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Shares persistent model-source identities between acquisition and platform cache collection.

package runtimeconfig

import (
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"k8s.io/apimachinery/pkg/types"
)

const (
	// PreparationRecordKey stores the provider's fixed model and tokenizer versions.
	PreparationRecordKey = "source.json"
	// PreparationConfigMapEnv selects the source record published by an acquisition worker.
	PreparationConfigMapEnv = "FORETOKEN_MODEL_PREPARATION_CONFIGMAP"
	// ModelCacheSourceLabel identifies a source record by its owning ModelPool UID.
	ModelCacheSourceLabel = "inference.foretoken.io/preparation-pool"
	// ModelCacheReferenceFinalizer retains a source until platform collection completes.
	ModelCacheReferenceFinalizer = "inference.foretoken.io/model-cache-reference"
	// ModelCacheReferenceKey stores the controller-confirmed cache binding.
	ModelCacheReferenceKey = "cache.json"
)

// ModelCacheReference pins a source record's storage binding for platform collection.
// The source namespace comes from ConfigMap metadata, never from this payload.
type ModelCacheReference struct {
	Directory      string                             `json:"directory"`
	ClaimName      string                             `json:"claimName"`
	ClaimUID       types.UID                          `json:"claimUID"`
	MountPath      string                             `json:"mountPath"`
	DirectoryOwner *inferencev1alpha1.StorageIdentity `json:"directoryOwner,omitempty"`
}

// PreparationSourceName gives model acquisition and collection the same Pool-cohort record name.
func PreparationSourceName(spec inferencev1alpha1.ModelGroupSpec) string {
	return "model-source-" + spec.ModelPoolRef.UID + "-" + spec.Revision
}
