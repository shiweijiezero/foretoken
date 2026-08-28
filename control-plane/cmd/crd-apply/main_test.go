// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Tests CRD loading, server-side apply, timeout, and establishment behavior.

package main

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

// TestLoadGeneratedCRDs protects the packaged CRD set from missing or unreadable generated manifests.
func TestLoadGeneratedCRDs(t *testing.T) {
	crds, err := loadCRDs(filepath.Join(moduleRoot(t), "config", "crd", "bases"))
	if err != nil {
		t.Fatal(err)
	}
	want := []string{
		"frontendservices.inference.foretoken.io",
		"kvgroups.inference.foretoken.io",
		"kvpools.inference.foretoken.io",
		"kvservices.inference.foretoken.io",
		"modelgroups.inference.foretoken.io",
		"modelpools.inference.foretoken.io",
		"modelservices.inference.foretoken.io",
	}
	if len(crds) != len(want) {
		t.Fatalf("loaded %d CRDs, want %d", len(crds), len(want))
	}
	loaded := make(map[string]struct{}, len(crds))
	for _, crd := range crds {
		loaded[crd.Name] = struct{}{}
		if len(crd.Spec.Versions) == 0 {
			t.Fatalf("loaded CRD %q without versions", crd.Name)
		}
	}
	for _, name := range want {
		if _, ok := loaded[name]; !ok {
			t.Fatalf("CRD %q was not loaded", name)
		}
	}
}

// moduleRoot walks to go.mod so tests do not depend on package depth or process cwd.
func moduleRoot(t *testing.T) string {
	t.Helper()

	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("locate test source")
	}
	for directory := filepath.Dir(source); ; directory = filepath.Dir(directory) {
		if _, err := os.Stat(filepath.Join(directory, "go.mod")); err == nil {
			return directory
		} else if !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("inspect module root: %v", err)
		}
		if parent := filepath.Dir(directory); parent == directory {
			t.Fatal("locate module root")
		}
	}
}

// TestGeneratedCRDContracts protects the API validation rules shipped in the generated CRD schemas.
func TestGeneratedCRDContracts(t *testing.T) {
	crds, err := loadCRDs(filepath.Join(moduleRoot(t), "config", "crd", "bases"))
	if err != nil {
		t.Fatal(err)
	}
	byName := make(map[string]*apiextensionsv1.CustomResourceDefinition, len(crds))
	for _, crd := range crds {
		byName[crd.Name] = crd
	}

	modelServiceSpec := crdSpecSchema(t, byName["modelservices.inference.foretoken.io"])
	modelPools := schemaProperty(t, modelServiceSpec, "modelPools")
	if modelPools.XListType == nil || *modelPools.XListType != "map" {
		t.Fatalf("modelPools list type = %v, want map", modelPools.XListType)
	}
	if len(modelPools.XListMapKeys) != 1 || modelPools.XListMapKeys[0] != "name" {
		t.Fatalf("modelPools map keys = %v, want [name]", modelPools.XListMapKeys)
	}

	modelGroupSpec := crdSpecSchema(t, byName["modelgroups.inference.foretoken.io"])
	if !hasValidationRule(modelGroupSpec, "self == oldSelf") {
		t.Fatal("ModelGroup spec does not enforce immutability")
	}
	if !hasValidationRule(modelGroupSpec, "self.memberCount == self.nodeCount") {
		t.Fatal("ModelGroup spec does not enforce one member per node")
	}
	if !hasValidationRule(modelGroupSpec, "self.nodeCount * self.resources.requests.gpu.count == self.parallelism.pp * self.parallelism.tp * self.parallelism.pcp * self.parallelism.dp") {
		t.Fatal("ModelGroup spec does not enforce compiled rank capacity")
	}
	accelerator := schemaProperty(t, modelGroupSpec, "accelerator")
	for _, required := range accelerator.Required {
		if required == "nodeSelector" {
			t.Fatal("ModelGroup accelerator requires a node selector even though the default GPU profile omits one")
		}
	}
}

func crdSpecSchema(t *testing.T, crd *apiextensionsv1.CustomResourceDefinition) *apiextensionsv1.JSONSchemaProps {
	t.Helper()
	if crd == nil || len(crd.Spec.Versions) != 1 || crd.Spec.Versions[0].Schema == nil || crd.Spec.Versions[0].Schema.OpenAPIV3Schema == nil {
		t.Fatalf("CRD has no single structural schema: %#v", crd)
	}
	return schemaProperty(t, crd.Spec.Versions[0].Schema.OpenAPIV3Schema, "spec")
}

func schemaProperty(t *testing.T, schema *apiextensionsv1.JSONSchemaProps, name string) *apiextensionsv1.JSONSchemaProps {
	t.Helper()
	property, ok := schema.Properties[name]
	if !ok {
		t.Fatalf("schema property %q is missing", name)
	}
	return &property
}

func hasValidationRule(schema *apiextensionsv1.JSONSchemaProps, rule string) bool {
	for _, validation := range schema.XValidations {
		if validation.Rule == rule {
			return true
		}
	}
	return false
}

// TestEstablishedRejectsInvalidNames protects installation from accepting a CRD whose names were rejected by the API server.
func TestEstablishedRejectsInvalidNames(t *testing.T) {
	crd := &apiextensionsv1.CustomResourceDefinition{
		ObjectMeta: metav1.ObjectMeta{Name: "examples.inference.foretoken.io"},
		Status: apiextensionsv1.CustomResourceDefinitionStatus{Conditions: []apiextensionsv1.CustomResourceDefinitionCondition{
			{Type: apiextensionsv1.Established, Status: apiextensionsv1.ConditionTrue},
			{Type: apiextensionsv1.NamesAccepted, Status: apiextensionsv1.ConditionFalse, Reason: "NameConflict", Message: "name is already in use"},
		}},
	}

	ready, err := established(crd)
	if err == nil || ready {
		t.Fatalf("established() = (%v, %v), want false and an error", ready, err)
	}
}

// TestApplyCRDTimeoutCoversPatch protects the apply timeout from expiring before a slow server-side patch returns.
func TestApplyCRDTimeoutCoversPatch(t *testing.T) {
	crd := &apiextensionsv1.CustomResourceDefinition{
		TypeMeta:   metav1.TypeMeta{APIVersion: apiextensionsv1.SchemeGroupVersion.String(), Kind: "CustomResourceDefinition"},
		ObjectMeta: metav1.ObjectMeta{Name: "examples.inference.foretoken.io"},
	}

	err := applyCRD(context.Background(), blockingClient{}, crd, defaultFieldManager, 10*time.Millisecond)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("applyCRD() error = %v, want context deadline exceeded", err)
	}
}

// TestApplyCRDUsesServerSideApply protects the field manager, patch type, and status-free CRD apply contract.
func TestApplyCRDUsesServerSideApply(t *testing.T) {
	client := &recordingClient{}
	crd := &apiextensionsv1.CustomResourceDefinition{
		TypeMeta:   metav1.TypeMeta{APIVersion: apiextensionsv1.SchemeGroupVersion.String(), Kind: "CustomResourceDefinition"},
		ObjectMeta: metav1.ObjectMeta{Name: "examples.inference.foretoken.io"},
	}

	if err := applyCRD(context.Background(), client, crd, defaultFieldManager, time.Second); err != nil {
		t.Fatal(err)
	}
	if client.patchType != types.ApplyPatchType {
		t.Fatalf("patch type = %q, want %q", client.patchType, types.ApplyPatchType)
	}
	if client.options.FieldManager != defaultFieldManager || client.options.Force != nil {
		t.Fatalf("patch options = %#v, want field manager %q without force", client.options, defaultFieldManager)
	}
	if bytes.Contains(client.data, []byte(`"status"`)) {
		t.Fatalf("apply payload contains status: %s", client.data)
	}
}

type blockingClient struct{}

func (blockingClient) Patch(ctx context.Context, _ string, _ types.PatchType, _ []byte, _ metav1.PatchOptions, _ ...string) (*apiextensionsv1.CustomResourceDefinition, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}

func (blockingClient) Get(context.Context, string, metav1.GetOptions) (*apiextensionsv1.CustomResourceDefinition, error) {
	panic("Get must not be called when Patch times out")
}

type recordingClient struct {
	patchType types.PatchType
	options   metav1.PatchOptions
	data      []byte
}

func (client *recordingClient) Patch(_ context.Context, _ string, patchType types.PatchType, data []byte, options metav1.PatchOptions, _ ...string) (*apiextensionsv1.CustomResourceDefinition, error) {
	client.patchType = patchType
	client.options = options
	client.data = data
	return &apiextensionsv1.CustomResourceDefinition{}, nil
}

func (*recordingClient) Get(_ context.Context, name string, _ metav1.GetOptions) (*apiextensionsv1.CustomResourceDefinition, error) {
	return &apiextensionsv1.CustomResourceDefinition{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Status: apiextensionsv1.CustomResourceDefinitionStatus{Conditions: []apiextensionsv1.CustomResourceDefinitionCondition{
			{Type: apiextensionsv1.NamesAccepted, Status: apiextensionsv1.ConditionTrue},
			{Type: apiextensionsv1.Established, Status: apiextensionsv1.ConditionTrue},
		}},
	}, nil
}
