// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Pins selected OCI graphs for a build and publishes only the image references selected for delivery.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	containerd "github.com/containerd/containerd/v2/client"
	"github.com/containerd/containerd/v2/core/content"
	"github.com/containerd/containerd/v2/core/images"
	"github.com/containerd/containerd/v2/core/leases"
	"github.com/containerd/errdefs"
	"github.com/containerd/platforms"
	digest "github.com/opencontainers/go-digest"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
)

// retainGraph leases the manifest graph selected by containerd and BuildKit, excluding unused index alternatives.
func retainGraph(ctx context.Context, node *containerd.Client, descriptor ocispec.Descriptor, matcher platforms.MatchComparer) (map[digest.Digest]struct{}, error) {
	leaseID, _ := leases.FromContext(ctx)
	allowed := make(map[digest.Digest]struct{})
	children := images.LimitManifests(images.FilterPlatforms(images.ChildrenHandler(node.ContentStore()), matcher), matcher, 1)
	handler := images.HandlerFunc(func(ctx context.Context, current ocispec.Descriptor) ([]ocispec.Descriptor, error) {
		if _, exists := allowed[current.Digest]; exists {
			return nil, images.ErrSkipDesc
		}
		if err := node.LeasesService().AddResource(ctx, leases.Lease{ID: leaseID}, leases.Resource{Type: "content", ID: current.Digest.String()}); err != nil {
			return nil, err
		}
		allowed[current.Digest] = struct{}{}
		return children(ctx, current)
	})
	if err := images.Walk(ctx, handler, descriptor); err != nil {
		return nil, err
	}
	return allowed, nil
}

// inspectImage reads image metadata without exporting layers and retains the selected graph before the caller builds from it.
func inspectImage(ctx context.Context, node *containerd.Client, input request, matcher platforms.MatchComparer) (any, error) {
	var descriptor ocispec.Descriptor
	if input.Descriptor != nil {
		descriptor = *input.Descriptor
	} else {
		image, err := node.ImageService().Get(ctx, input.Image)
		if errdefs.IsNotFound(err) {
			return map[string]bool{"found": false}, nil
		}
		if err != nil {
			return nil, err
		}
		descriptor = image.Target
	}
	if _, err := retainGraph(ctx, node, descriptor, matcher); err != nil {
		return nil, err
	}
	store := node.ContentStore()
	// Unpacked node images can outlive their compressed blobs. Only complete
	// inputs can bypass BuildKit's registry resolver; generated outputs must remain complete.
	available, _, _, missing, err := images.Check(ctx, store, descriptor, matcher)
	if err != nil {
		return nil, err
	}
	if !available || len(missing) != 0 {
		if input.Descriptor == nil {
			return map[string]bool{"found": false}, nil
		}
		return nil, fmt.Errorf("generated image %s has incomplete content: %w", descriptor.Digest, errdefs.ErrNotFound)
	}
	if images.IsIndexType(descriptor.MediaType) {
		children, err := images.Children(ctx, store, descriptor)
		if err != nil {
			return nil, err
		}
		if len(children) == 1 && images.IsManifestType(children[0].MediaType) && children[0].Annotations["vnd.docker.reference.type"] != "attestation-manifest" {
			manifest, err := images.Manifest(ctx, store, descriptor, matcher)
			if err != nil {
				return nil, err
			}
			if manifest.Subject == nil && manifest.ArtifactType == "" && (manifest.Config.MediaType == ocispec.MediaTypeImageConfig || manifest.Config.MediaType == images.MediaTypeDockerSchema2Config) {
				descriptor = children[0]
			}
		}
	}
	image := containerd.NewImageWithPlatform(node, images.Image{Target: descriptor}, matcher)
	configuration, err := image.Config(ctx)
	if err != nil {
		return nil, err
	}
	data, err := content.ReadBlob(ctx, store, configuration)
	if err != nil {
		return nil, err
	}
	var parsed ocispec.Image
	if err := json.Unmarshal(data, &parsed); err != nil {
		return nil, err
	}
	user := parsed.Config.User
	if user == "" {
		user = "0"
	}
	return struct {
		Found      bool               `json:"found"`
		Descriptor ocispec.Descriptor `json:"descriptor"`
		User       string             `json:"user"`
	}{true, descriptor, user}, nil
}

// deliverImage establishes native GC links, the selected pinned reference and unpacked snapshots after dependency validation succeeds.
func deliverImage(ctx context.Context, node *containerd.Client, input request, matcher platforms.MatchComparer) error {
	if input.Descriptor == nil || input.Image == "" || len(input.ImageLabels) == 0 {
		return errors.New("image delivery requires a descriptor, reference and ownership labels")
	}
	store := node.ImageService()
	previous, err := store.Get(ctx, input.Image)
	if err != nil && !errdefs.IsNotFound(err) {
		return err
	}
	exists := err == nil
	if exists {
		// Borrowed references keep their original owner.
		for key, value := range input.ImageLabels {
			if previous.Labels[key] != value {
				return fmt.Errorf("source image reference %q belongs to another owner", input.Image)
			}
		}
	}
	contentStore := node.ContentStore()
	if err := images.Walk(ctx, images.SetChildrenLabels(contentStore, images.ChildrenHandler(contentStore)), *input.Descriptor); err != nil {
		return err
	}
	image := images.Image{Name: input.Image, Target: *input.Descriptor, Labels: input.ImageLabels}
	if exists {
		fields := []string{"target"}
		for key := range input.ImageLabels {
			fields = append(fields, "labels."+key)
		}
		_, err = store.Update(ctx, image, fields...)
	} else {
		_, err = store.Create(ctx, image)
	}
	if err != nil {
		return err
	}
	return containerd.NewImageWithPlatform(node, image, matcher).Unpack(ctx, "")
}
