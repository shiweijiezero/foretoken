// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Bridges BuildKit sessions to a node's containerd content store without exposing its runtime socket to build executors.
package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	containerd "github.com/containerd/containerd/v2/client"
	"github.com/containerd/containerd/v2/core/content"
	"github.com/containerd/containerd/v2/core/content/proxy"
	"github.com/containerd/containerd/v2/core/leases"
	"github.com/containerd/containerd/v2/pkg/namespaces"
	"github.com/containerd/continuity"
	"github.com/containerd/errdefs"
	"github.com/containerd/platforms"
	"github.com/docker/cli/cli/config"
	bkclient "github.com/moby/buildkit/client"
	"github.com/moby/buildkit/exporter/containerimage/exptypes"
	"github.com/moby/buildkit/session"
	"github.com/moby/buildkit/session/auth/authprovider"
	"github.com/moby/buildkit/session/secrets/secretsprovider"
	"github.com/moby/buildkit/util/contentutil"
	"github.com/moby/buildkit/util/progress/progresswriter"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"github.com/tonistiigi/fsutil"
)

// request is the private, explicit projection of the Python build owner's selected operation.
type request struct {
	ContainerdAddress   string                        `json:"containerd_address"`
	ContainerdNamespace string                        `json:"containerd_namespace"`
	LeaseID             string                        `json:"lease_id"`
	LeaseLabels         map[string]string             `json:"lease_labels"`
	Platform            string                        `json:"platform"`
	BuildkitAddress     string                        `json:"buildkit_address,omitempty"`
	FrontendAttrs       map[string]string             `json:"frontend_attrs,omitempty"`
	LocalDirs           map[string]string             `json:"local_dirs,omitempty"`
	Inputs              map[string]ocispec.Descriptor `json:"inputs,omitempty"`
	Output              output                        `json:"output,omitempty"`
	MetadataFile        string                        `json:"metadata_file,omitempty"`
	CacheImports        []string                      `json:"cache_imports,omitempty"`
	Image               string                        `json:"image,omitempty"`
	Descriptor          *ocispec.Descriptor           `json:"descriptor,omitempty"`
	ImageLabels         map[string]string             `json:"image_labels,omitempty"`
}

type output struct {
	Type  string            `json:"type"`
	Attrs map[string]string `json:"attrs"`
}

// main consumes one operation from stdin; progress stays on stderr and only requested metadata goes to stdout.
func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: foretoken-build-client inspect|solve|deliver")
		os.Exit(2)
	}
	var input request
	if err := json.NewDecoder(os.Stdin).Decode(&input); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	result, err := execute(ctx, os.Args[1], input)
	if err == nil && result != nil {
		err = json.NewEncoder(os.Stdout).Encode(result)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// execute borrows the node client under the Python owner's known lease, which remains until that owner joins all writers.
func execute(ctx context.Context, action string, input request) (any, error) {
	if action != "inspect" && action != "solve" && action != "deliver" {
		return nil, fmt.Errorf("unknown node image operation %q", action)
	}
	if input.ContainerdNamespace == "" || input.LeaseID == "" || len(input.LeaseLabels) == 0 {
		return nil, errors.New("source build namespace and lease ownership are required")
	}
	platform, err := platforms.Parse(input.Platform)
	if err != nil {
		return nil, err
	}
	node, err := containerd.New(input.ContainerdAddress)
	if err != nil {
		return nil, err
	}
	defer node.Close()
	nodeContext := namespaces.WithNamespace(ctx, input.ContainerdNamespace)
	if err := ensureLease(nodeContext, node.LeasesService(), input); err != nil {
		return nil, err
	}
	nodeContext = leases.WithLease(nodeContext, input.LeaseID)
	matcher := platforms.OnlyStrict(platform)
	switch action {
	case "inspect":
		return inspectImage(nodeContext, node, input, matcher)
	case "deliver":
		return nil, deliverImage(nodeContext, node, input, matcher)
	default:
		// BuildKit owns different leases; node-store RPC headers must not reach its daemon.
		return nil, solve(ctx, node, input, matcher)
	}
}

// ensureLease joins concurrent solves only when the existing native lease has this operation's recorded owner.
func ensureLease(ctx context.Context, manager leases.Manager, input request) error {
	_, err := manager.Create(ctx, leases.WithID(input.LeaseID), leases.WithLabels(input.LeaseLabels))
	if !errdefs.IsAlreadyExists(err) {
		return err
	}
	current, err := manager.List(ctx)
	if err != nil {
		return err
	}
	for _, lease := range current {
		if lease.ID != input.LeaseID {
			continue
		}
		for key, value := range input.LeaseLabels {
			if lease.Labels[key] != value {
				return fmt.Errorf("source build lease %q has another owner", lease.ID)
			}
		}
		return nil
	}
	return fmt.Errorf("source build lease %q disappeared", input.LeaseID)
}

// solve retains the existing Dockerfile, filesync, credentials and export semantics while native CAS skips already stored blobs.
func solve(ctx context.Context, node *containerd.Client, input request, matcher platforms.MatchComparer) error {
	client, err := bkclient.New(ctx, input.BuildkitAddress)
	if err != nil {
		return err
	}
	defer client.Close()
	secretValues := map[string][]byte{
		"foretoken-build-jobs": []byte(input.FrontendAttrs["build-arg:BUILD_JOBS"]),
	}
	for _, name := range []string{"FORETOKEN_GITHUB_MIRROR", "GOPROXY"} {
		if value, present := input.FrontendAttrs["build-arg:"+name]; present {
			secretValues[name] = []byte(value)
		}
	}
	options := bkclient.SolveOpt{
		Frontend: "dockerfile.v0", FrontendAttrs: make(map[string]string),
		LocalMounts: make(map[string]fsutil.FS), OCIStores: make(map[string]content.Store),
		Session: []session.Attachable{
			authprovider.NewDockerAuthProvider(authprovider.DockerAuthProviderConfig{
				AuthConfigProvider: authprovider.LoadAuthConfig(config.LoadDefaultConfigFile(os.Stderr)),
			}),
			secretsprovider.FromMap(secretValues),
		},
	}
	for _, reference := range input.CacheImports {
		options.CacheImports = append(options.CacheImports, bkclient.CacheOptionsEntry{
			Type: "registry", Attrs: map[string]string{"ref": reference},
		})
	}
	maps.Copy(options.FrontendAttrs, input.FrontendAttrs)
	for name, directory := range input.LocalDirs {
		mount, err := fsutil.NewFS(directory)
		if err != nil {
			return err
		}
		options.LocalMounts[name] = mount
	}
	for alias, descriptor := range input.Inputs {
		store := &sessionStore{store: node.ContentStore(), namespace: input.ContainerdNamespace, lease: input.LeaseID}
		allowed, err := retainGraph(store.scope(ctx), node, descriptor, matcher)
		if err != nil {
			return err
		}
		store.readable = allowed
		options.OCIStores[alias] = store
		options.FrontendAttrs["context:"+alias] = "oci-layout://" + alias + "@" + descriptor.Digest.String()
	}
	export := bkclient.ExportEntry{Type: input.Output.Type, Attrs: input.Output.Attrs}
	var destination *sessionStore
	if export.Type == bkclient.ExporterOCI {
		destination = &sessionStore{
			store: node.ContentStore(), leaseManager: node.LeasesService(), namespace: input.ContainerdNamespace, lease: input.LeaseID, writable: true,
		}
		// OCI export materializes every cached layer before it checks the destination.
		// Resolve the image first so native ingestion can reuse the node's existing blobs.
		attributes := make(map[string]string, len(export.Attrs)+2)
		maps.Copy(attributes, export.Attrs)
		delete(attributes, "tar")
		attributes["store"] = "false"
		attributes["push"] = "false"
		export = bkclient.ExportEntry{Type: bkclient.ExporterImage, Attrs: attributes}
	}
	options.Exports = []bkclient.ExportEntry{export}
	response, err := solveWithProgress(ctx, client, options)
	if err != nil {
		return err
	}
	if destination != nil {
		encoded, err := base64.StdEncoding.DecodeString(response.ExporterResponse[exptypes.ExporterImageDescriptorKey])
		if err != nil {
			return fmt.Errorf("decode solved image descriptor: %w", err)
		}
		var descriptor ocispec.Descriptor
		if err := json.Unmarshal(encoded, &descriptor); err != nil {
			return fmt.Errorf("read solved image descriptor: %w", err)
		}
		err = contentutil.CopyChain(ctx, destination, proxy.NewContentStore(client.ContentClient()), descriptor)
		if errdefs.IsNotFound(err) {
			// Registry caches can leave layers absent from both local stores. The
			// original exporter owns fetching that content from its lazy providers.
			fmt.Fprintln(os.Stderr, "Materializing image content not yet available on the node")
			options.Exports = []bkclient.ExportEntry{{Type: input.Output.Type, Attrs: input.Output.Attrs, OutputStore: destination}}
			response, err = solveWithProgress(ctx, client, options)
		}
		if err != nil {
			return err
		}
	}
	return writeMetadata(input.MetadataFile, response.ExporterResponse)
}

// solveWithProgress drains native diagnostics even after cancellation, preserving the solve's original failure.
func solveWithProgress(ctx context.Context, client *bkclient.Client, options bkclient.SolveOpt) (*bkclient.SolveResponse, error) {
	progress, err := progresswriter.NewPrinter(context.WithoutCancel(ctx), os.Stderr, "plain")
	if err != nil {
		return nil, err
	}
	response, solveErr := client.Solve(ctx, nil, options, progress.Status())
	<-progress.Done()
	return response, errors.Join(solveErr, progress.Err())
}

// writeMetadata preserves buildctl's JSON/base64 response representation for existing Python consumers.
func writeMetadata(filename string, response map[string]string) error {
	metadata := make(map[string]any, len(response))
	for key, value := range response {
		metadata[key] = value
		decoded, err := base64.StdEncoding.DecodeString(value)
		if err != nil {
			continue
		}
		var object map[string]any
		if json.Unmarshal(decoded, &object) == nil && len(object) > 0 {
			metadata[key] = json.RawMessage(decoded)
		}
	}
	data, err := json.Marshal(metadata)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(filename), 0755); err != nil {
		return err
	}
	return continuity.AtomicWriteFile(filename, data, 0644)
}
