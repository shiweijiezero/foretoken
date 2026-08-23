// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Starts the Foretoken control-plane manager and health endpoints.

package main

import (
	"errors"
	"flag"
	"os"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
)

func main() {
	var metricsAddress string
	var probeAddress string
	var leaderElection bool
	var metricsScrapeNamespace string
	var frontendEnabled bool
	var frontendImage string
	var frontendPort int
	var frontendGatewayName string
	var frontendGatewayNamespace string
	var frontendGatewaySectionName string
	var vllmImage string
	var vllmModelServerPort int
	var vllmAcceleratorType string
	var vllmDeviceResourceName string
	var vllmRuntimeClassName string
	var vllmNodeSelectorKey string
	var vllmNodeSelectorValue string
	var vllmECProfileName string
	var vllmECProfileRevision string
	var vllmECConnector string
	var vllmECSharedStorageClaim string
	var vllmECSharedStoragePath string
	var vllmPDProfileName string
	var vllmPDProfileRevision string
	var vllmPDProtocol string
	var vllmPDBootstrapPort int
	var vllmPDAbortRequestTimeoutSeconds int
	var vllmPDRDMADeviceName string
	var vllmPDRDMAResourceName string
	var vllmPDRDMAResourceCount int
	var vllmMooncakeStoreProfileName string
	var vllmMooncakeStoreProfileRevision string
	var vllmMooncakeStoreConfigMapName string
	var vllmMooncakeStoreConfigMapKey string
	var vllmMooncakeStorePythonHashSeed string

	// Metrics stay disabled until the chart exposes a secured endpoint.
	flag.StringVar(&metricsAddress, "metrics-bind-address", "0", "Metrics endpoint bind address; 0 disables metrics.")
	flag.StringVar(&probeAddress, "health-probe-bind-address", ":8081", "Health probe bind address.")
	flag.BoolVar(&leaderElection, "leader-elect", false, "Enable leader election.")
	flag.StringVar(&metricsScrapeNamespace, "metrics-scrape-namespace", "", "Namespace allowed to scrape model-server metrics; empty disables scrape access.")
	flag.BoolVar(&frontendEnabled, "frontend-enabled", false, "Enable FrontendService workload and HTTPRoute reconciliation.")
	flag.StringVar(&frontendImage, "frontend-image", "", "Frontend runtime image.")
	flag.IntVar(&frontendPort, "frontend-port", 8080, "Frontend runtime HTTP port.")
	flag.StringVar(&frontendGatewayName, "frontend-gateway-name", "", "Platform Gateway name used by frontend HTTPRoutes.")
	flag.StringVar(&frontendGatewayNamespace, "frontend-gateway-namespace", "", "Platform Gateway namespace; defaults to the FrontendService namespace.")
	flag.StringVar(&frontendGatewaySectionName, "frontend-gateway-section-name", "", "Platform Gateway listener section name.")
	flag.StringVar(&vllmImage, "vllm-image", "", "vLLM runtime image.")
	flag.IntVar(&vllmModelServerPort, "vllm-model-server-port", 9000, "Internal Foretoken model-server port.")
	flag.StringVar(&vllmAcceleratorType, "vllm-accelerator-type", "", "Concrete accelerator type served by the vLLM profile.")
	flag.StringVar(&vllmDeviceResourceName, "vllm-device-resource-name", "", "Kubernetes extended resource for the vLLM accelerator.")
	flag.StringVar(&vllmRuntimeClassName, "vllm-runtime-class-name", "", "Optional Kubernetes RuntimeClass for vLLM Pods.")
	flag.StringVar(&vllmNodeSelectorKey, "vllm-node-selector-key", "", "Node label key for the vLLM accelerator profile.")
	flag.StringVar(&vllmNodeSelectorValue, "vllm-node-selector-value", "", "Node label value for the vLLM accelerator profile.")
	flag.StringVar(&vllmECProfileName, "vllm-ec-profile-name", "", "Platform-owned EC profile name; empty disables E/P/D.")
	flag.StringVar(&vllmECProfileRevision, "vllm-ec-profile-revision", "", "Platform-owned EC profile revision.")
	flag.StringVar(&vllmECConnector, "vllm-ec-connector", "", "Fixed pinned-vLLM EC connector identity.")
	flag.StringVar(&vllmECSharedStorageClaim, "vllm-ec-shared-storage-claim", "", "ReadWriteMany PVC used by the pinned EC connector.")
	flag.StringVar(&vllmECSharedStoragePath, "vllm-ec-shared-storage-path", "/var/lib/foretoken/ec", "In-container shared EC storage path.")
	flag.StringVar(&vllmPDProfileName, "vllm-pd-profile-name", "", "Opaque platform-owned Mooncake P/D profile name; empty disables P/D.")
	flag.StringVar(&vllmPDProfileRevision, "vllm-pd-profile-revision", "", "Opaque platform-owned Mooncake P/D profile revision.")
	flag.StringVar(&vllmPDProtocol, "vllm-pd-protocol", "", "Mooncake P/D protocol; only rdma is supported.")
	flag.IntVar(&vllmPDBootstrapPort, "vllm-pd-bootstrap-port", 0, "Mooncake bootstrap port.")
	flag.IntVar(&vllmPDAbortRequestTimeoutSeconds, "vllm-pd-abort-request-timeout-seconds", 0, "Mooncake abort request timeout in seconds.")
	flag.StringVar(&vllmPDRDMADeviceName, "vllm-pd-rdma-device-name", "", "Platform-verified Mooncake RDMA HCA name.")
	flag.StringVar(&vllmPDRDMAResourceName, "vllm-pd-rdma-resource-name", "", "Kubernetes extended resource that injects Mooncake RDMA devices.")
	flag.IntVar(&vllmPDRDMAResourceCount, "vllm-pd-rdma-resource-count", 0, "Mooncake RDMA extended resources requested by each P/D Pod.")
	flag.StringVar(&vllmMooncakeStoreProfileName, "vllm-mooncake-store-profile-name", "", "Opaque platform-owned Mooncake Store profile name; empty disables external Store.")
	flag.StringVar(&vllmMooncakeStoreProfileRevision, "vllm-mooncake-store-profile-revision", "", "Opaque platform-owned Mooncake Store profile revision.")
	flag.StringVar(&vllmMooncakeStoreConfigMapName, "vllm-mooncake-store-config-map-name", "", "Mooncake Store ConfigMap name.")
	flag.StringVar(&vllmMooncakeStoreConfigMapKey, "vllm-mooncake-store-config-map-key", "", "Mooncake Store ConfigMap key.")
	flag.StringVar(&vllmMooncakeStorePythonHashSeed, "vllm-mooncake-store-python-hash-seed", "", "PYTHONHASHSEED for Mooncake Store.")

	logOptions := zap.Options{Development: false}
	logOptions.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&logOptions)))
	var mooncakePD *resolver.MooncakePDProfile
	var ec *resolver.ECProfile
	var mooncakeStore *resolver.MooncakeStoreProfile
	if vllmECProfileName != "" {
		ec = &resolver.ECProfile{
			Name: vllmECProfileName, Revision: vllmECProfileRevision,
			Connector:          vllmECConnector,
			SharedStorageClaim: vllmECSharedStorageClaim,
			SharedStoragePath:  vllmECSharedStoragePath,
		}
	}
	if vllmMooncakeStoreProfileName != "" {
		mooncakeStore = &resolver.MooncakeStoreProfile{Name: vllmMooncakeStoreProfileName, Revision: vllmMooncakeStoreProfileRevision, ConfigMapName: vllmMooncakeStoreConfigMapName, ConfigMapKey: vllmMooncakeStoreConfigMapKey, PythonHashSeed: vllmMooncakeStorePythonHashSeed}
	}
	if vllmPDProfileName != "" {
		mooncakePD = &resolver.MooncakePDProfile{
			Name:                       vllmPDProfileName,
			Revision:                   vllmPDProfileRevision,
			Protocol:                   vllmPDProtocol,
			BootstrapPort:              int32(vllmPDBootstrapPort),
			AbortRequestTimeoutSeconds: int32(vllmPDAbortRequestTimeoutSeconds),
			RDMADeviceName:             vllmPDRDMADeviceName,
			RDMAResourceName:           vllmPDRDMAResourceName,
			RDMAResourceCount:          int32(vllmPDRDMAResourceCount),
		}
	}

	// Register built-in and Foretoken APIs before controllers are attached to the manager.
	scheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(inferencev1alpha1.AddToScheme(scheme))
	utilruntime.Must(gatewayv1.Install(scheme))

	restConfig := ctrl.GetConfigOrDie()
	if frontendEnabled {
		discoveryClient, err := discovery.NewDiscoveryClientForConfig(restConfig)
		if err != nil {
			ctrl.Log.Error(err, "unable to create Gateway API discovery client")
			os.Exit(1)
		}
		resources, err := discoveryClient.ServerResourcesForGroupVersion(gatewayv1.GroupVersion.String())
		if err != nil {
			ctrl.Log.Error(err, "frontend requires the Gateway API HTTPRoute CRD")
			os.Exit(1)
		}
		httpRouteAvailable := false
		for _, resource := range resources.APIResources {
			if resource.Name == "httproutes" {
				httpRouteAvailable = true
				break
			}
		}
		if !httpRouteAvailable {
			ctrl.Log.Error(errors.New("HTTPRoute resource was not discovered"), "frontend requires the Gateway API HTTPRoute CRD")
			os.Exit(1)
		}
	}

	manager, err := ctrl.NewManager(restConfig, ctrl.Options{
		Scheme: scheme,
		Client: client.Options{Cache: &client.CacheOptions{
			// The shared credential is read by its fixed name; avoid a cluster-wide Secret informer.
			DisableFor: []client.Object{&corev1.Secret{}},
		}},
		Metrics:                metricsserver.Options{BindAddress: metricsAddress},
		HealthProbeBindAddress: probeAddress,
		LeaderElection:         leaderElection,
		LeaderElectionID:       "inference.foretoken.io",
	})
	if err != nil {
		ctrl.Log.Error(err, "unable to create manager")
		os.Exit(1)
	}

	// Controllers are registered explicitly so each resource keeps one lifecycle owner.
	if frontendEnabled {
		frontendReconciler := &controllers.FrontendServiceReconciler{
			Client: manager.GetClient(),
			RuntimeProfile: controllers.FrontendRuntimeProfile{
				Image: frontendImage,
				Port:  int32(frontendPort),
				Gateway: controllers.GatewayParent{
					Name:        frontendGatewayName,
					Namespace:   frontendGatewayNamespace,
					SectionName: frontendGatewaySectionName,
				},
			},
		}
		if err := frontendReconciler.SetupWithManager(manager); err != nil {
			ctrl.Log.Error(err, "unable to register FrontendService controller")
			os.Exit(1)
		}
	}
	if err := (&controllers.ModelServiceReconciler{
		Client:              manager.GetClient(),
		PoolMetricsProvider: controllers.NewHTTPPoolMetricsProvider(manager.GetClient()),
	}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ModelService controller")
		os.Exit(1)
	}
	if err := (&controllers.KVServiceReconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVService controller")
		os.Exit(1)
	}
	if err := (&controllers.KVPoolReconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVPool controller")
		os.Exit(1)
	}
	if err := (&controllers.KVGroupReconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVGroup controller")
		os.Exit(1)
	}
	if err := (&controllers.ModelPoolReconciler{
		Client: manager.GetClient(),
		TemplateResolver: resolver.StaticModelPoolResolver{RuntimeProfile: resolver.RuntimeProfile{
			Image:              vllmImage,
			ModelServerPort:    int32(vllmModelServerPort),
			AcceleratorType:    vllmAcceleratorType,
			DeviceResourceName: vllmDeviceResourceName,
			RuntimeClassName:   vllmRuntimeClassName,
			NodeSelectorKey:    vllmNodeSelectorKey,
			NodeSelectorValue:  vllmNodeSelectorValue,
			MooncakePD:         mooncakePD,
			EC:                 ec,
			MooncakeStore:      mooncakeStore,
		}},
	}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ModelPool controller")
		os.Exit(1)
	}
	controlPlaneNamespace := os.Getenv("POD_NAMESPACE")
	if controlPlaneNamespace == "" {
		ctrl.Log.Error(errors.New("POD_NAMESPACE is required"), "unable to configure ModelGroup drain networking")
		os.Exit(1)
	}
	if err := (&controllers.ModelGroupReconciler{
		Client:                 manager.GetClient(),
		ControlPlaneNamespace:  controlPlaneNamespace,
		MetricsScrapeNamespace: metricsScrapeNamespace,
	}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ModelGroup controller")
		os.Exit(1)
	}

	// These endpoints are consumed by the liveness and readiness probes in the Helm chart.
	if err := manager.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		ctrl.Log.Error(err, "unable to register health check")
		os.Exit(1)
	}
	if err := manager.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		ctrl.Log.Error(err, "unable to register readiness check")
		os.Exit(1)
	}

	ctrl.Log.Info("starting manager")
	if err := manager.Start(ctrl.SetupSignalHandler()); err != nil {
		ctrl.Log.Error(err, "manager stopped with an error")
		os.Exit(1)
	}
}
