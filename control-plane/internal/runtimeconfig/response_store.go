// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects the frontend's shared API cache connection and retention without resolving credentials.

package runtimeconfig

import (
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/prometheus/common/model"
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

const (
	ResponseStoreURLEnv               = "FORETOKEN_RESPONSE_STORE_URL"
	ResponseStoreNamespaceEnv         = "FORETOKEN_RESPONSE_STORE_NAMESPACE"
	ResponseRetentionSecondsEnv       = "FORETOKEN_RESPONSE_RETENTION_SECONDS"
	ResponseReplayRetentionSecondsEnv = "FORETOKEN_RESPONSE_REPLAY_RETENTION_SECONDS"
)

// ResponseStoreConfiguration resolves platform duration strings into the frontend's second-based selection.
func ResponseStoreConfiguration(connection inferencev1alpha1.RedisConnection, retention, replayRetention string) (*inferencev1alpha1.ResponseStoreConfig, error) {
	configuration := &inferencev1alpha1.ResponseStoreConfig{RedisConnection: *connection.DeepCopy()}
	for _, setting := range []struct {
		name    string
		value   string
		seconds *int64
	}{
		{"retention", retention, &configuration.RetentionSeconds},
		{"replayRetention", replayRetention, &configuration.ReplayRetentionSeconds},
	} {
		duration, err := model.ParseDuration(setting.value)
		if err != nil || duration <= 0 || time.Duration(duration)%time.Second != 0 {
			return nil, fmt.Errorf("%s requires a positive whole-second duration, such as 30d or 10m", setting.name)
		}
		*setting.seconds = int64(time.Duration(duration) / time.Second)
	}
	if err := ValidateResponseStore(configuration); err != nil {
		return nil, err
	}
	return configuration, nil
}

// ValidateResponseStore checks platform-owned connection and retention settings at selection.
func ValidateResponseStore(configuration *inferencev1alpha1.ResponseStoreConfig) error {
	if configuration == nil {
		return errors.New("response store requires a Redis connection")
	}
	if err := ValidateRedisConnection(&configuration.RedisConnection); err != nil {
		return err
	}
	if configuration.RetentionSeconds <= 0 || configuration.ReplayRetentionSeconds <= 0 {
		return errors.New("response and replay retention must be positive seconds")
	}
	return nil
}

// ResponseStoreEnv gives frontend replicas one stable service partition; kubelet reads the URL Secret.
func ResponseStoreEnv(configuration *inferencev1alpha1.ResponseStoreConfig, service string) []corev1.EnvVar {
	if configuration == nil {
		return nil
	}
	return append(RedisConnectionEnv(&configuration.RedisConnection, ResponseStoreURLEnv),
		corev1.EnvVar{Name: ResponseStoreNamespaceEnv, Value: service},
		corev1.EnvVar{Name: ResponseRetentionSecondsEnv, Value: strconv.FormatInt(configuration.RetentionSeconds, 10)},
		corev1.EnvVar{Name: ResponseReplayRetentionSecondsEnv, Value: strconv.FormatInt(configuration.ReplayRetentionSeconds, 10)},
	)
}

// ResponseStoreFromEnv recovers an observed application's selection without reading its Secret.
func ResponseStoreFromEnv(environment []corev1.EnvVar) (*inferencev1alpha1.ResponseStoreConfig, error) {
	connection, err := RedisConnectionFromEnv(environment, ResponseStoreURLEnv)
	if err != nil || connection == nil {
		return nil, err
	}
	configuration := &inferencev1alpha1.ResponseStoreConfig{RedisConnection: *connection}
	for _, env := range environment {
		var destination *int64
		switch env.Name {
		case ResponseRetentionSecondsEnv:
			destination = &configuration.RetentionSeconds
		case ResponseReplayRetentionSecondsEnv:
			destination = &configuration.ReplayRetentionSeconds
		default:
			continue
		}
		value, err := strconv.ParseInt(env.Value, 10, 64)
		if err != nil {
			return nil, errors.New("response and replay retention must be positive seconds")
		}
		*destination = value
	}
	if err := ValidateResponseStore(configuration); err != nil {
		return nil, err
	}
	return configuration, nil
}
