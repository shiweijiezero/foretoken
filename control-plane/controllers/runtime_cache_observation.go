// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package controllers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const runtimeCachePollInterval = 5 * time.Second

// runtimeCacheObservation is a fresh measurement of one workload's mounted filesystem.
type runtimeCacheObservation struct {
	Version        int    `json:"version"`
	PodUID         string `json:"pod_uid"`
	CapacityBytes  int64  `json:"capacity_bytes"`
	AvailableBytes int64  `json:"available_bytes"`
}

// observeRuntimeCache reads active Pod mounts so PVC growth follows filesystem availability.
func observeRuntimeCache(ctx context.Context, kubeClient client.Client, cache *inferencev1alpha1.RuntimeCache, claimName string) ([]runtimeCacheObservation, error) {
	var groups inferencev1alpha1.ModelGroupList
	if err := kubeClient.List(ctx, &groups, client.InNamespace(cache.Namespace)); err != nil {
		return nil, err
	}
	var observations []runtimeCacheObservation
	var failures []error
	for index := range groups.Items {
		group := &groups.Items[index]
		binding := group.Spec.Artifacts.Cache
		if binding == nil || binding.ClaimName != claimName {
			continue
		}
		groupObservations, err := observeGroupCache(ctx, kubeClient, group)
		observations = append(observations, groupObservations...)
		if err != nil {
			failures = append(failures, err)
		}
	}
	return observations, errors.Join(failures...)
}

// observeGroupCache reads Pod IPs rather than Service endpoints so startup is observable.
func observeGroupCache(ctx context.Context, kubeClient client.Client, group *inferencev1alpha1.ModelGroup) ([]runtimeCacheObservation, error) {
	var pods corev1.PodList
	if err := kubeClient.List(ctx, &pods, client.InNamespace(group.Namespace), client.MatchingLabels(modelGroupLabels(group))); err != nil {
		return nil, err
	}
	var observations []runtimeCacheObservation
	var failures []error
	for index := range pods.Items {
		pod := &pods.Items[index]
		if !pod.DeletionTimestamp.IsZero() || pod.Status.PodIP == "" || pod.Status.Phase != corev1.PodRunning {
			continue
		}
		endpoint := "http://" + net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(int(runtimeCacheObservationPort(group.Spec.Runtime.Port))))
		observation, err := readRuntimeCacheObservation(ctx, endpoint, string(pod.UID))
		if err != nil {
			failures = append(failures, fmt.Errorf("cache observation for Pod %s: %w", pod.Name, err))
			continue
		}
		observations = append(observations, observation)
	}
	return observations, errors.Join(failures...)
}

// readRuntimeCacheObservation bounds one HTTP read and verifies the reporting Pod identity.
func readRuntimeCacheObservation(ctx context.Context, endpoint, podUID string) (runtimeCacheObservation, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"/v1/internal/cache", nil)
	if err != nil {
		return runtimeCacheObservation{}, err
	}
	response, err := (&http.Client{Timeout: 2 * time.Second}).Do(request)
	if err != nil {
		return runtimeCacheObservation{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return runtimeCacheObservation{}, fmt.Errorf("HTTP %d", response.StatusCode)
	}
	var observation runtimeCacheObservation
	if err := json.NewDecoder(response.Body).Decode(&observation); err != nil {
		return observation, err
	}
	if observation.Version != 1 || observation.PodUID != podUID || observation.CapacityBytes <= 0 || observation.AvailableBytes < 0 || observation.AvailableBytes > observation.CapacityBytes {
		return observation, fmt.Errorf("invalid cache filesystem observation")
	}
	return observation, nil
}
