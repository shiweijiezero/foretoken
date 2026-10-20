// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Runs one managed log-storage expansion pass for the platform's scheduled Job.

package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"time"

	"github.com/shiweijiezero/foretoken/control-plane/controllers/storage/logstorage"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/client-go/kubernetes"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
)

// maintainLogStorage observes and expands the selected platform log claims once.
// Kubernetes schedules subsequent observations and owns Job retries and cleanup.
func maintainLogStorage(arguments []string) error {
	flags := flag.NewFlagSet("maintain-log-storage", flag.ContinueOnError)
	namespace := flags.String("namespace", "", "Platform namespace containing the managed log store.")
	statefulSet := flags.String("statefulset", "", "Managed Loki StatefulSet name.")
	maxSize := flags.String("max-size", "", "Maximum capacity of each managed log volume.")
	timeout := flags.Duration("timeout", 30*time.Second, "Maximum duration of one maintenance pass.")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected maintenance arguments: %v", flags.Args())
	}
	if *namespace == "" || *statefulSet == "" {
		return errors.New("log storage maintenance requires --namespace and --statefulset")
	}
	maximum, err := resource.ParseQuantity(*maxSize)
	if err != nil || maximum.Sign() <= 0 || maximum.CmpInt64(maximum.Value()) != 0 {
		return errors.New("--max-size must be a positive whole-byte quantity")
	}
	if *timeout <= 0 {
		return errors.New("--timeout must be positive")
	}

	ctrl.SetLogger(zap.New())
	configuration, err := ctrl.GetConfig()
	if err != nil {
		return fmt.Errorf("configure log storage maintenance: %w", err)
	}
	reader, err := client.New(configuration, client.Options{Scheme: clientgoscheme.Scheme})
	if err != nil {
		return fmt.Errorf("configure log storage resources: %w", err)
	}
	kubeClient, err := kubernetes.NewForConfig(configuration)
	if err != nil {
		return fmt.Errorf("configure log volume observations: %w", err)
	}
	key := client.ObjectKey{Namespace: *namespace, Name: *statefulSet}
	reconciler := logstorage.Reconciler{
		Client: reader, Kubernetes: kubeClient, MaxSize: maximum,
	}
	ctx, cancel := context.WithTimeout(ctrl.SetupSignalHandler(), *timeout)
	defer cancel()
	_, err = reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: key})
	return err
}
