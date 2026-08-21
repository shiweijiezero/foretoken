// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package controllers

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	kvIndexerSecretName = "foretoken-kv-indexer"
	kvIndexerSecretKey  = "key"
	kvIndexerKeyPath    = "/etc/foretoken/kv-indexer/key"
)

// ensureKVIndexerSecret creates the namespace credential once. It deliberately
// has no owner reference: workloads can be recreated independently.
func ensureKVIndexerSecret(ctx context.Context, c client.Client, namespace string) error {
	secret := new(corev1.Secret)
	key := client.ObjectKey{Namespace: namespace, Name: kvIndexerSecretName}
	if err := c.Get(ctx, key, secret); err == nil {
		if len(secret.Data[kvIndexerSecretKey]) != 32 {
			return fmt.Errorf("KV index Secret %q has no 32-byte key", kvIndexerSecretName)
		}
		return nil
	} else if !apierrors.IsNotFound(err) {
		return fmt.Errorf("get KV index Secret: %w", err)
	}
	value := make([]byte, 32)
	if _, err := rand.Read(value); err != nil {
		return fmt.Errorf("generate KV index key: %w", err)
	}
	secret = &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: kvIndexerSecretName, Namespace: namespace}, Type: corev1.SecretTypeOpaque, Data: map[string][]byte{kvIndexerSecretKey: value}}
	if err := c.Create(ctx, secret); err == nil {
		return nil
	} else if !apierrors.IsAlreadyExists(err) {
		return fmt.Errorf("create KV index Secret: %w", err)
	}
	// A concurrent controller won. Never overwrite it; re-read and validate it.
	winner := new(corev1.Secret)
	if err := c.Get(ctx, key, winner); err != nil {
		return fmt.Errorf("get concurrently-created KV index Secret: %w", err)
	}
	if len(winner.Data[kvIndexerSecretKey]) != 32 {
		return fmt.Errorf("KV index Secret %q has no 32-byte key", kvIndexerSecretName)
	}
	return nil
}

// kvScopeID is intentionally independent of a Group UID, endpoint, and role so
// compatible aggregate and P/D components calculate exactly the same namespace.
func kvScopeID(group *inferencev1alpha1.ModelGroup) string {
	payload := struct {
		Model, Revision, Tokenizer, TokenizerRevision string
		Parallelism                                   inferencev1alpha1.CompiledParallelism
		RuntimeArgs                                   []inferencev1alpha1.BackendArg
		KVRuntime                                     *inferencev1alpha1.ModelGroupKVRuntimeConfig
	}{group.Spec.Artifacts.Model, group.Spec.Artifacts.ModelRevision, group.Spec.Artifacts.Tokenizer, group.Spec.Artifacts.TokenizerRevision, group.Spec.Parallelism, group.Spec.Runtime.Args, group.Spec.KVRuntime}
	encoded, _ := json.Marshal(payload)
	sum := sha256.Sum256(encoded)
	return hex.EncodeToString(sum[:])
}

// pdPipelineScopeID scopes dynamic Mooncake side-channel ingress to compatible P/D Groups.
func pdPipelineScopeID(group *inferencev1alpha1.ModelGroup) string {
	encoded, _ := json.Marshal(struct {
		KVScope   string
		PDRuntime *inferencev1alpha1.ModelGroupPDRuntimeConfig
	}{kvScopeID(group), group.Spec.PDRuntime})
	sum := sha256.Sum256(encoded)
	return hex.EncodeToString(sum[:16])
}
