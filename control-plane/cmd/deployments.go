// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package main

import (
	"errors"
	"flag"
	"fmt"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/deployments"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"
)

// manageDeployments runs the platform owner independently of the serving controllers it creates.
func manageDeployments(arguments []string) error {
	flags := flag.NewFlagSet("manage-deployments", flag.ContinueOnError)
	namespace := flags.String("namespace", "", "Platform namespace containing the trusted deployment template.")
	configuration := flags.String("configuration", "", "Platform ConfigMap containing the compiled deployment template.")
	probeAddress := flags.String("health-probe-bind-address", ":8081", "Health probe bind address.")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected deployment manager arguments: %v", flags.Args())
	}
	if *namespace == "" || *configuration == "" {
		return errors.New("manage-deployments requires --namespace and --configuration")
	}
	ctrl.SetLogger(zap.New())
	scheme := runtime.NewScheme()
	for _, register := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, api.AddToScheme, gatewayv1.Install} {
		if err := register(scheme); err != nil {
			return err
		}
	}
	config, err := ctrl.GetConfig()
	if err != nil {
		return err
	}
	managed := labels.SelectorFromSet(map[string]string{"app.kubernetes.io/component": "deployment-controller"})
	manager, err := ctrl.NewManager(config, ctrl.Options{
		Scheme: scheme,
		Cache: cache.Options{
			ReaderFailOnMissingInformer: true,
			// Custom API watches use the default cache so discovery can wait for CRD installation.
			ByObject: map[client.Object]cache.ByObject{
				&appsv1.Deployment{}: {Label: managed},
				&corev1.ConfigMap{}:  {Label: managed},
			},
		},
		Metrics:                       metricsserver.Options{BindAddress: "0"},
		HealthProbeBindAddress:        *probeAddress,
		LeaderElection:                true,
		LeaderElectionID:              "inference.foretoken.io.deployments",
		LeaderElectionNamespace:       *namespace,
		LeaderElectionReleaseOnCancel: true,
	})
	if err != nil {
		return err
	}
	if err := (&deployments.Reconciler{Client: manager.GetClient(), Configuration: client.ObjectKey{Namespace: *namespace, Name: *configuration}}).SetupWithManager(manager); err != nil {
		return err
	}
	if err := manager.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		return err
	}
	if err := manager.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		return err
	}
	return manager.Start(ctrl.SetupSignalHandler())
}
