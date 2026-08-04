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

func TestLoadGeneratedCRDs(t *testing.T) {
	crds, err := loadCRDs(filepath.Join(moduleRoot(t), "config", "crd", "bases"))
	if err != nil {
		t.Fatal(err)
	}
	if len(crds) != 2 {
		t.Fatalf("loaded %d CRDs, want 2", len(crds))
	}
	for _, crd := range crds {
		if crd.Name == "" || len(crd.Spec.Versions) == 0 {
			t.Fatalf("loaded incomplete CRD: %#v", crd)
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
