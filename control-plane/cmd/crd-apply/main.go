// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Applies packaged CRDs and waits for their APIs to become established.

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"

	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	apiextensionsclient "k8s.io/apiextensions-apiserver/pkg/client/clientset/clientset/typed/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/apimachinery/pkg/util/yaml"
	"k8s.io/client-go/rest"
)

const defaultFieldManager = "foretoken-crd-apply"

type crdClient interface {
	Get(context.Context, string, metav1.GetOptions) (*apiextensionsv1.CustomResourceDefinition, error)
	Patch(context.Context, string, types.PatchType, []byte, metav1.PatchOptions, ...string) (*apiextensionsv1.CustomResourceDefinition, error)
}

// crdApplyManifest excludes status and other server-owned top-level fields from SSA.
type crdApplyManifest struct {
	APIVersion string                                       `json:"apiVersion"`
	Kind       string                                       `json:"kind"`
	Metadata   metav1.ObjectMeta                            `json:"metadata"`
	Spec       apiextensionsv1.CustomResourceDefinitionSpec `json:"spec"`
}

func main() {
	var directory string
	var fieldManager string
	var timeout time.Duration
	flag.StringVar(&directory, "crds-dir", "/opt/foretoken/crds", "Directory containing generated CRD manifests.")
	flag.StringVar(&fieldManager, "field-manager", defaultFieldManager, "Server-Side Apply field manager.")
	flag.DurationVar(&timeout, "timeout", 2*time.Minute, "Maximum time to establish each CRD.")
	flag.Parse()

	if fieldManager == "" {
		log.Fatal("field-manager must not be empty")
	}
	if timeout <= 0 {
		log.Fatal("timeout must be positive")
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	crds, err := loadCRDs(directory)
	if err != nil {
		log.Fatal(err)
	}

	config, err := rest.InClusterConfig()
	if err != nil {
		log.Fatalf("load in-cluster Kubernetes config: %v", err)
	}
	client, err := apiextensionsclient.NewForConfig(config)
	if err != nil {
		log.Fatalf("create CRD client: %v", err)
	}

	// Apply serially so a failure identifies the exact API that blocked startup.
	for _, crd := range crds {
		if err := applyCRD(ctx, client.CustomResourceDefinitions(), crd, fieldManager, timeout); err != nil {
			log.Fatal(err)
		}
		log.Printf("CRD %s is established", crd.Name)
	}
}

// loadCRDs validates the complete input set before any CRD is changed.
func loadCRDs(directory string) ([]*apiextensionsv1.CustomResourceDefinition, error) {
	entries, err := os.ReadDir(directory)
	if err != nil {
		return nil, fmt.Errorf("read CRD directory %q: %w", directory, err)
	}

	var crds []*apiextensionsv1.CustomResourceDefinition
	seen := make(map[string]string)
	for _, entry := range entries {
		if filepath.Ext(entry.Name()) != ".yaml" {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			return nil, fmt.Errorf("inspect CRD manifest %q: %w", entry.Name(), err)
		}
		if !info.Mode().IsRegular() {
			return nil, fmt.Errorf("CRD manifest %q is not a regular file", entry.Name())
		}

		path := filepath.Join(directory, entry.Name())
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read CRD manifest %q: %w", path, err)
		}
		decoder := yaml.NewYAMLOrJSONDecoder(bytes.NewReader(data), 4096)
		var crd *apiextensionsv1.CustomResourceDefinition
		for {
			var raw json.RawMessage
			err := decoder.Decode(&raw)
			if errors.Is(err, io.EOF) {
				break
			}
			if err != nil {
				return nil, fmt.Errorf("decode CRD manifest %q: %w", path, err)
			}
			if len(bytes.TrimSpace(raw)) == 0 {
				continue
			}
			if crd != nil {
				return nil, fmt.Errorf("CRD manifest %q contains multiple YAML documents", path)
			}
			crd = new(apiextensionsv1.CustomResourceDefinition)
			if err := json.Unmarshal(raw, crd); err != nil {
				return nil, fmt.Errorf("decode CRD manifest %q: %w", path, err)
			}
		}
		if crd == nil {
			return nil, fmt.Errorf("CRD manifest %q is empty", path)
		}
		if crd.APIVersion != apiextensionsv1.SchemeGroupVersion.String() || crd.Kind != "CustomResourceDefinition" || crd.Name == "" || crd.Namespace != "" {
			return nil, fmt.Errorf("manifest %q is not a cluster-scoped apiextensions.k8s.io/v1 CustomResourceDefinition", path)
		}
		if previous, ok := seen[crd.Name]; ok {
			return nil, fmt.Errorf("CRD %q is defined by both %q and %q", crd.Name, previous, path)
		}
		seen[crd.Name] = path

		// Keep only desired metadata in the apply payload.
		crd.ResourceVersion = ""
		crd.UID = ""
		crd.Generation = 0
		crd.CreationTimestamp = metav1.Time{}
		crd.DeletionTimestamp = nil
		crd.DeletionGracePeriodSeconds = nil
		crd.ManagedFields = nil
		crd.Finalizers = nil
		crd.Status = apiextensionsv1.CustomResourceDefinitionStatus{}
		crds = append(crds, crd)
	}
	if len(crds) == 0 {
		return nil, fmt.Errorf("no CRD manifests found in %q", directory)
	}

	// Apply parent APIs before their controller-owned Pool and Group descendants.
	sort.SliceStable(crds, func(i, j int) bool {
		return crdHierarchyLevel(crds[i]) < crdHierarchyLevel(crds[j])
	})
	return crds, nil
}

func crdHierarchyLevel(crd *apiextensionsv1.CustomResourceDefinition) int {
	switch plural := crd.Spec.Names.Plural; {
	case strings.HasSuffix(plural, "services"):
		return 0
	case strings.HasSuffix(plural, "pools"):
		return 1
	case strings.HasSuffix(plural, "groups"):
		return 2
	default:
		return 3
	}
}

// applyCRD updates desired fields and blocks until the API becomes usable.
func applyCRD(ctx context.Context, client crdClient, crd *apiextensionsv1.CustomResourceDefinition, fieldManager string, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	data, err := json.Marshal(crdApplyManifest{
		APIVersion: crd.APIVersion,
		Kind:       crd.Kind,
		Metadata:   crd.ObjectMeta,
		Spec:       crd.Spec,
	})
	if err != nil {
		return fmt.Errorf("encode CRD %q: %w", crd.Name, err)
	}
	if _, err := client.Patch(ctx, crd.Name, types.ApplyPatchType, data, metav1.PatchOptions{FieldManager: fieldManager}); err != nil {
		return fmt.Errorf("apply CRD %q: %w", crd.Name, err)
	}

	if err := wait.PollUntilContextCancel(ctx, 500*time.Millisecond, true, func(ctx context.Context) (bool, error) {
		current, err := client.Get(ctx, crd.Name, metav1.GetOptions{})
		if err != nil {
			return false, err
		}
		return established(current)
	}); err != nil {
		return fmt.Errorf("wait for CRD %q to become established: %w", crd.Name, err)
	}
	return nil
}

// established rejects naming conflicts even if a previous generation was established.
func established(crd *apiextensionsv1.CustomResourceDefinition) (bool, error) {
	established := false
	for _, condition := range crd.Status.Conditions {
		switch condition.Type {
		case apiextensionsv1.NamesAccepted:
			if condition.Status == apiextensionsv1.ConditionFalse {
				return false, fmt.Errorf("CRD %q names were rejected: %s: %s", crd.Name, condition.Reason, condition.Message)
			}
		case apiextensionsv1.Established:
			established = condition.Status == apiextensionsv1.ConditionTrue
		}
	}
	return established, nil
}
