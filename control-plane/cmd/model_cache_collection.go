// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Runs the platform cache collector independently of deployment instances.

package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/storage/modelcache"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/selection"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
)

// maintainModelCache starts the platform collector with an election separate from deployment-instance controllers.
func maintainModelCache(arguments []string) error {
	flags := flag.NewFlagSet("maintain-model-cache", flag.ContinueOnError)
	namespace := flags.String("namespace", "", "Platform namespace owning cache collection Jobs.")
	serviceAccount := flags.String("service-account", "", "Platform-local read-only ServiceAccount used by collection workers.")
	workerImage := flags.String("worker-image", "", "Platform model-server image used by CPU-only cache collection workers.")
	workerApplicationURL := flags.String("worker-application-url", "", "Platform-selected model-server application publication.")
	deviceResourceName := flags.String("device-resource-name", "", "Platform accelerator resource whose node taint CPU collection workers tolerate.")
	applicationJSON := flags.String("application-files", "{}", "JSON configuration for platform application download tools.")
	probeAddress := flags.String("health-probe-bind-address", ":8081", "Health probe bind address.")
	var imagePullSecrets []corev1.LocalObjectReference
	flags.Func("workload-image-pull-secret", "Platform-local image pull Secret; repeat for multiple Secrets.", func(name string) error {
		if name == "" {
			return errors.New("workload image pull Secret name must be nonempty")
		}
		imagePullSecrets = append(imagePullSecrets, corev1.LocalObjectReference{Name: name})
		return nil
	})
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected model cache collection arguments: %v", flags.Args())
	}
	if *namespace == "" || *serviceAccount == "" || *workerImage == "" {
		return errors.New("model cache collection requires --namespace, --service-account and --worker-image")
	}
	var files runtimeconfig.ApplicationFiles
	if err := json.Unmarshal([]byte(*applicationJSON), &files); err != nil {
		return fmt.Errorf("decode model cache application files: %w", err)
	}
	if files.Image == "" || *workerApplicationURL != "" && (files.Script == "" || files.MountPath == "") {
		return errors.New("model cache collection requires application download tools and a mount path for application publications")
	}

	ctrl.SetLogger(zap.New())
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	if err := inferencev1alpha1.AddToScheme(scheme); err != nil {
		return err
	}
	configuration, err := ctrl.GetConfig()
	if err != nil {
		return fmt.Errorf("configure model cache collection: %w", err)
	}
	sourceLabel, err := labels.NewRequirement(runtimeconfig.ModelCacheSourceLabel, selection.Exists, nil)
	if err != nil {
		return err
	}
	jobLabel, err := labels.NewRequirement(modelcache.CollectionLabel, selection.Exists, nil)
	if err != nil {
		return err
	}
	manager, err := ctrl.NewManager(configuration, ctrl.Options{
		Scheme: scheme,
		Cache: cache.Options{
			ReaderFailOnMissingInformer: true,
			ByObject: map[client.Object]cache.ByObject{
				&corev1.ConfigMap{}:             {Label: labels.NewSelector().Add(*sourceLabel)},
				&batchv1.Job{}:                  {Namespaces: map[string]cache.Config{*namespace: {}}, Label: labels.NewSelector().Add(*jobLabel)},
				&corev1.PersistentVolumeClaim{}: {Namespaces: map[string]cache.Config{*namespace: {}}, Label: labels.NewSelector().Add(*jobLabel)},
				&corev1.PersistentVolume{}:      {Label: labels.NewSelector().Add(*jobLabel)},
			},
		},
		Metrics:                       metricsserver.Options{BindAddress: "0"},
		HealthProbeBindAddress:        *probeAddress,
		LeaderElection:                true,
		LeaderElectionID:              "inference.foretoken.io.model-cache-collection",
		LeaderElectionNamespace:       *namespace,
		LeaderElectionReleaseOnCancel: true,
	})
	if err != nil {
		return fmt.Errorf("create model cache collection manager: %w", err)
	}
	if err := (&modelcache.Reconciler{
		Client: manager.GetClient(), Namespace: *namespace, ServiceAccount: *serviceAccount,
		WorkerImage: *workerImage, WorkerApplicationURL: *workerApplicationURL,
		DeviceResourceName: *deviceResourceName, ApplicationFiles: files, ImagePullSecrets: imagePullSecrets,
	}).SetupWithManager(manager); err != nil {
		return fmt.Errorf("register model cache collection controller: %w", err)
	}
	if err := manager.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		return err
	}
	if err := manager.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		return err
	}
	return manager.Start(ctrl.SetupSignalHandler())
}
