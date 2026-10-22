// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Restricts the content capabilities exposed through a BuildKit session and binds writes to their native lease.
package main

import (
	"context"

	"github.com/containerd/containerd/v2/core/content"
	"github.com/containerd/containerd/v2/core/leases"
	"github.com/containerd/containerd/v2/pkg/namespaces"
	"github.com/containerd/errdefs"
	digest "github.com/opencontainers/go-digest"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
)

// sessionStore exposes either one selected input graph or an output ingester, never general node-store mutation.
type sessionStore struct {
	store        content.Store
	leaseManager leases.Manager
	namespace    string
	lease        string
	readable     map[digest.Digest]struct{}
	writable     bool
}

func (s *sessionStore) scope(ctx context.Context) context.Context {
	return leases.WithLease(namespaces.WithNamespace(ctx, s.namespace), s.lease)
}

// Info permits native output deduplication or metadata from the explicitly selected input graph.
func (s *sessionStore) Info(ctx context.Context, key digest.Digest) (content.Info, error) {
	if _, allowed := s.readable[key]; !allowed && !s.writable {
		return content.Info{}, errdefs.ErrNotFound
	}
	if s.writable {
		if err := s.retainOutput(ctx, key); err != nil {
			return content.Info{}, err
		}
	}
	return s.store.Info(s.scope(ctx), key)
}

// ReaderAt serves selected input blobs; output sessions cannot use an expected digest to retrieve node data.
func (s *sessionStore) ReaderAt(ctx context.Context, descriptor ocispec.Descriptor) (content.ReaderAt, error) {
	if _, allowed := s.readable[descriptor.Digest]; !allowed {
		return nil, errdefs.ErrNotFound
	}
	return s.store.ReaderAt(s.scope(ctx), descriptor)
}

// Writer scopes partial ingestion names and keeps the node's AlreadyExists and native integrity behavior.
func (s *sessionStore) Writer(ctx context.Context, options ...content.WriterOpt) (content.Writer, error) {
	if !s.writable {
		return nil, errdefs.ErrPermissionDenied
	}
	var selected content.WriterOpts
	for _, option := range options {
		if err := option(&selected); err != nil {
			return nil, err
		}
	}
	if selected.Ref == "" {
		return nil, errdefs.ErrInvalidArgument
	}
	if err := s.retainOutput(ctx, selected.Desc.Digest); err != nil {
		return nil, err
	}
	writer, err := s.store.Writer(s.scope(ctx), content.WithDescriptor(selected.Desc), content.WithRef(s.lease+":"+selected.Ref))
	if err != nil {
		return nil, err
	}
	return &sessionWriter{Writer: writer, store: s, ref: selected.Ref}, nil
}

// retainOutput protects the expected blob before either content server can take its AlreadyExists shortcut.
func (s *sessionStore) retainOutput(ctx context.Context, key digest.Digest) error {
	if key == "" {
		return nil
	}
	return s.leaseManager.AddResource(s.scope(ctx), leases.Lease{ID: s.lease}, leases.Resource{Type: "content", ID: key.String()})
}

// Status observes only an ingestion inside this output operation.
func (s *sessionStore) Status(ctx context.Context, ref string) (content.Status, error) {
	if !s.writable {
		return content.Status{}, errdefs.ErrPermissionDenied
	}
	status, err := s.store.Status(s.scope(ctx), s.lease+":"+ref)
	status.Ref = ref
	return status, err
}

// Abort releases only an unfinished ingestion in this output operation.
func (s *sessionStore) Abort(ctx context.Context, ref string) error {
	if !s.writable {
		return errdefs.ErrPermissionDenied
	}
	return s.store.Abort(s.scope(ctx), s.lease+":"+ref)
}

// Update keeps native graph labels with deliverImage, outside the build session's capabilities.
func (s *sessionStore) Update(context.Context, content.Info, ...string) (content.Info, error) {
	return content.Info{}, errdefs.ErrPermissionDenied
}

// Delete leaves blob reclamation with containerd's native garbage collector.
func (s *sessionStore) Delete(context.Context, digest.Digest) error {
	return errdefs.ErrPermissionDenied
}

// Walk denies enumeration outside the selected graph.
func (s *sessionStore) Walk(context.Context, content.WalkFunc, ...string) error {
	return errdefs.ErrPermissionDenied
}

// ListStatuses keeps other node operations out of the build session.
func (s *sessionStore) ListStatuses(context.Context, ...string) ([]content.Status, error) {
	return nil, errdefs.ErrPermissionDenied
}

// sessionWriter preserves the lease through commit while keeping internal ingestion names out of the session protocol.
type sessionWriter struct {
	content.Writer
	store *sessionStore
	ref   string
}

// Commit retains the finalized digest before native integrity validation and deduplication.
func (w *sessionWriter) Commit(ctx context.Context, size int64, expected digest.Digest, options ...content.Opt) error {
	if err := w.store.retainOutput(ctx, expected); err != nil {
		return err
	}
	return w.Writer.Commit(w.store.scope(ctx), size, expected, options...)
}

// Status presents the ingestion name supplied by the session rather than its node-local lease prefix.
func (w *sessionWriter) Status() (content.Status, error) {
	status, err := w.Writer.Status()
	status.Ref = w.ref
	return status, err
}
