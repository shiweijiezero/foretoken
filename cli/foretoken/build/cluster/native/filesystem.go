// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Restores compiler-cache files through containerd's OCI layer implementation.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"

	"github.com/containerd/containerd/v2/core/content"
	"github.com/containerd/containerd/v2/core/images"
	"github.com/containerd/containerd/v2/pkg/archive"
	"github.com/containerd/containerd/v2/pkg/archive/compression"
	"github.com/containerd/platforms"
	digest "github.com/opencontainers/go-digest"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
)

// extractFilesystem restores the selected image into the builder-owned cache directory.
// The caller exposes it to compilers only after every layer and its native diff ID is verified.
func extractFilesystem(ctx context.Context, store content.Store, descriptor ocispec.Descriptor, matcher platforms.MatchComparer, destination string) error {
	manifest, err := images.Manifest(ctx, store, descriptor, matcher)
	if err != nil {
		return err
	}
	diffIDs, err := images.RootFS(ctx, store, manifest.Config)
	if err != nil {
		return err
	}
	if len(diffIDs) != len(manifest.Layers) {
		return errors.New("image layer count differs from its root filesystem")
	}
	if err := os.MkdirAll(destination, 0755); err != nil {
		return err
	}
	for index, layer := range manifest.Layers {
		if err := applyFilesystemLayer(ctx, store, layer, diffIDs[index], destination); err != nil {
			return fmt.Errorf("extract image layer %s: %w", layer.Digest, err)
		}
	}
	return nil
}

// applyFilesystemLayer preserves OCI metadata and whiteouts while mapping ownership to the builder.
func applyFilesystemLayer(ctx context.Context, store content.Store, layer ocispec.Descriptor, diffID digest.Digest, destination string) (result error) {
	if err := diffID.Validate(); err != nil {
		return err
	}
	blob, err := store.ReaderAt(ctx, layer)
	if err != nil {
		return err
	}
	defer func() { result = errors.Join(result, blob.Close()) }()
	decompressed, err := compression.DecompressStream(io.NewSectionReader(blob, 0, blob.Size()))
	if err != nil {
		return err
	}
	defer func() { result = errors.Join(result, decompressed.Close()) }()
	verifier := diffID.Verifier()
	stream := io.TeeReader(decompressed, verifier)
	if _, err := archive.Apply(ctx, destination, stream, archive.WithNoSameOwner()); err != nil {
		return err
	}
	// Tar EOF can precede stream EOF. Include padding and check the decompressor's
	// final result before accepting the image's existing uncompressed layer digest.
	if _, err := io.Copy(io.Discard, stream); err != nil {
		return err
	}
	if !verifier.Verified() {
		return fmt.Errorf("uncompressed layer does not match %s", diffID)
	}
	return nil
}
