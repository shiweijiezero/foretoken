// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Configures and starts the deployment-scoped serving controller manager.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"math"
	"net/http"
	"os"
	"strings"
	"time"

	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/frontend"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/kv"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/model"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/profiling"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/storage/runtimecache"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/video"
	"github.com/shiweijiezero/foretoken/control-plane/internal/deployments"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/observability"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

const (
	frontendModeLocal   = "local"
	frontendModeGateway = "gateway"
)

// servingOptions contains values parsed from the platform deployment's manager arguments.
type servingOptions struct {
	deploymentUID                         string
	metricsAddress                        string
	probeAddress                          string
	leaderElection                        bool
	sourceMode                            bool
	frontendEnabled                       bool
	frontendMode                          string
	frontendImage                         string
	frontendApplicationURL                string
	modelServerApplicationURL             string
	modelPreparationImage                 string
	videoWorkerImage                      string
	videoWorkerApplicationURL             string
	applicationFilesJSON                  string
	frontendPort                          int
	frontendGatewayName                   string
	frontendGatewayNamespace              string
	frontendGatewaySectionName            string
	inferenceEngineImage                  string
	omniInferenceEngineImage              string
	nsightImage                           string
	modelServerPort                       int
	gpuResourceName                       string
	runtimeClassName                      string
	gpuNodeSelectorKey                    string
	gpuNodeSelectorValue                  string
	profiles                              vllmconfig.RuntimeProfiles
	rdmaResourceName                      string
	rdmaResourceCount                     int
	rdmaNetworks                          []string
	rdmaBindings                          []resolver.RDMANetworkBinding
	rdmaBindingsSet                       bool
	legacyRDMASet                         bool
	autoscalingTelemetryConfigurationPath string
	workloadImagePullSecretNames          []string
	cacheClaimName                        string
	cacheMountPath                        string
	cacheDirectoryBindings                runtimeconfig.RuntimeCacheDirectoryBindings
	leaderWorkerScheduling                *inferencev1alpha1.LeaderWorkerScheduling
	admissionStoreURLSecretName           string
	admissionStoreURLSecretKey            string
	responseStoreURLSecretName            string
	responseStoreURLSecretKey             string
	responseRetention                     string
	responseReplayRetention               string
	modelSourceEndpoint                   string
	modelSourceTokenSecretName            string
	modelSourceTokenSecretKey             string
	modelDownloadProxyURL                 string
	observabilityPrometheus               string
	observabilityLabelsJSON               string
	logOptions                            zap.Options
}

// servingConfiguration is the resolved startup contract shared by manager construction and controller registration.
type servingConfiguration struct {
	deploymentUID                         string
	controlPlaneNamespace                 string
	metricsAddress                        string
	probeAddress                          string
	leaderElection                        bool
	sourceMode                            bool
	frontendEnabled                       bool
	frontendMode                          string
	frontendImage                         string
	frontendApplicationURL                string
	modelServerApplicationURL             string
	modelPreparationImage                 string
	videoWorkerImage                      string
	videoWorkerApplicationURL             string
	frontendPort                          int
	frontendGatewayName                   string
	frontendGatewayNamespace              string
	frontendGatewaySectionName            string
	autoscalingTelemetryConfigurationPath string
	workloadImagePullSecrets              []corev1.LocalObjectReference
	cacheProfile                          runtimeconfig.RuntimeCacheProfile
	huggingFaceAccessProfile              runtimeconfig.HuggingFaceAccessProfile
	modelDistributionProfile              runtimeconfig.ModelDistributionProfile
	applicationFiles                      runtimeconfig.ApplicationFiles
	admissionStore                        *inferencev1alpha1.RedisConnection
	responseStore                         *inferencev1alpha1.ResponseStoreConfig
	observabilityPrometheus               string
	observabilityLabelsJSON               string
	runtimeProfile                        resolver.RuntimeProfile
}

// runServingController executes the serving manager's parse, resolve, manager, registration, and start stages.
func runServingController() {
	options := parseServingOptions()
	configuration := resolveServingConfiguration(options)
	manager := newServingManager(&configuration)
	registerServingControllers(manager, configuration)
	startServingManager(manager)
}

// parseServingOptions registers the manager's platform-owned flags and parses the process arguments.
func parseServingOptions() servingOptions {
	options := servingOptions{}
	flag.StringVar(&options.deploymentUID, "deployment-uid", "", "Immutable deployment UID selecting this controller's resources.")
	// Metrics stay disabled until the chart exposes a secured endpoint.
	flag.StringVar(&options.metricsAddress, "metrics-bind-address", "0", "Metrics endpoint bind address; 0 disables metrics.")
	flag.StringVar(&options.probeAddress, "health-probe-bind-address", "0", "Health probe bind address; 0 disables probes.")
	flag.BoolVar(&options.leaderElection, "leader-elect", false, "Enable leader election.")
	flag.BoolVar(&options.sourceMode, "source-mode", false, "Enable service source bundles from the platform application origin.")
	flag.StringVar(&options.observabilityPrometheus, "observability-prometheus", "", "Prometheus NAMESPACE/NAME selected for service alert rules.")
	flag.StringVar(&options.observabilityLabelsJSON, "observability-labels", "{}", "JSON labels used to select controller-owned alert rules.")
	flag.StringVar(&options.autoscalingTelemetryConfigurationPath, "autoscaling-telemetry-config", "", "Platform configuration file supplying autoscaling telemetry collection budgets.")
	flag.BoolVar(&options.frontendEnabled, "frontend-enabled", false, "Enable FrontendService workload reconciliation.")
	flag.StringVar(&options.frontendMode, "frontend-mode", frontendModeLocal, "Frontend access mode: local or gateway.")
	flag.StringVar(&options.frontendImage, "frontend-image", "", "Frontend runtime image.")
	flag.StringVar(&options.frontendApplicationURL, "frontend-application-url", "", "Trusted platform frontend publication selected on deployment.")
	flag.StringVar(&options.modelServerApplicationURL, "model-server-application-url", "", "Trusted platform model-server publication selected on deployment.")
	flag.StringVar(&options.modelPreparationImage, "model-preparation-image", "", "CPU environment for published model preparation applications.")
	flag.StringVar(&options.videoWorkerImage, "video-worker-image", "", "Platform runtime image for video workers.")
	flag.StringVar(&options.videoWorkerApplicationURL, "video-worker-application-url", "", "Published application directory containing the video worker.")
	flag.StringVar(&options.applicationFilesJSON, "application-files", "{}", "JSON configuration for platform application download tools.")
	flag.IntVar(&options.frontendPort, "frontend-port", 0, "Frontend runtime HTTP port supplied by the platform.")
	flag.StringVar(&options.frontendGatewayName, "frontend-gateway-name", "", "Platform Gateway name used by frontend HTTPRoutes.")
	flag.StringVar(&options.frontendGatewayNamespace, "frontend-gateway-namespace", "", "Platform Gateway namespace; defaults to the FrontendService namespace.")
	flag.StringVar(&options.frontendGatewaySectionName, "frontend-gateway-section-name", "", "Platform Gateway listener section name.")
	flag.Func("workload-image-pull-secret", "Namespace-local image pull Secret used by frontend and model-server Pods; repeat for multiple Secrets.", func(value string) error {
		if value == "" {
			return errors.New("workload image pull Secret name must be nonempty")
		}
		options.workloadImagePullSecretNames = append(options.workloadImagePullSecretNames, value)
		return nil
	})
	flag.StringVar(&options.cacheClaimName, "cache-claim", "", "Existing namespace-local PVC shared by runtime workloads.")
	flag.StringVar(&options.cacheMountPath, "cache-mount-path", "", "Absolute runtime cache root supplied by the platform.")
	flag.Func("cache-directory-bindings", "JSON mapping of authorized data directories to prepared namespace claims; an empty object authorizes no directories.", func(value string) error {
		if err := json.Unmarshal([]byte(value), &options.cacheDirectoryBindings); err != nil {
			return err
		}
		if options.cacheDirectoryBindings == nil {
			return errors.New("cache-directory-bindings must be a JSON object")
		}
		for directory, claim := range options.cacheDirectoryBindings {
			if directory == "" || claim == "" {
				return errors.New("cache-directory-bindings requires nonempty directories and claim names")
			}
		}
		return nil
	})
	flag.Func("leader-worker-scheduling", "JSON selection of native placement and quota admission queues for distributed model groups.", func(value string) error {
		var selection *inferencev1alpha1.LeaderWorkerScheduling
		if err := json.Unmarshal([]byte(value), &selection); err != nil {
			return err
		}
		var err error
		options.leaderWorkerScheduling, err = runtimeconfig.ResolveLeaderWorkerScheduling(selection)
		return err
	})
	flag.StringVar(&options.admissionStoreURLSecretName, "admission-store-url-secret-name", "", "Namespace-local Secret containing the shared admission ledger URL.")
	flag.StringVar(&options.admissionStoreURLSecretKey, "admission-store-url-secret-key", "", "Key in the admission ledger URL Secret.")
	flag.StringVar(&options.responseStoreURLSecretName, "response-store-url-secret-name", "", "Namespace-local Secret containing the shared API cache URL.")
	flag.StringVar(&options.responseStoreURLSecretKey, "response-store-url-secret-key", "", "Key in the shared API cache URL Secret.")
	flag.StringVar(&options.responseRetention, "response-retention", "", "Retention for stored responses and chat completions, such as 30d.")
	flag.StringVar(&options.responseReplayRetention, "response-replay-retention", "", "Retention for response stream replay and temporary background results, such as 10m.")
	flag.StringVar(&options.modelSourceEndpoint, "model-source-endpoint", "", "Optional Hugging Face-compatible Hub endpoint.")
	flag.StringVar(&options.modelSourceTokenSecretName, "model-source-token-secret-name", "", "Namespace-local Secret containing the Hugging Face credential.")
	flag.StringVar(&options.modelSourceTokenSecretKey, "model-source-token-secret-key", "", "Key in the model source credential Secret.")
	flag.StringVar(&options.modelDownloadProxyURL, "model-distribution-download-proxy", "", "Optional internal HTTP proxy for model preparation.")
	flag.StringVar(&options.inferenceEngineImage, "inference-engine-image", "", "Inference engine image containing the Foretoken model-server adapter.")
	flag.StringVar(&options.omniInferenceEngineImage, "omni-inference-engine-image", "", "Optional vLLM-Omni image containing the Foretoken Omni adapter.")
	flag.StringVar(&options.nsightImage, "nsight-image", "", "Optional NVIDIA model-server image prepared for Nsight Systems.")
	flag.IntVar(&options.modelServerPort, "model-server-port", 0, "Internal model-server HTTP port supplied by the platform.")
	flag.StringVar(&options.gpuResourceName, "gpu-resource-name", "", "Kubernetes extended resource selected by the platform for accelerator devices.")
	flag.StringVar(&options.runtimeClassName, "runtime-class-name", "", "Optional RuntimeClass for inference engine Pods.")
	flag.StringVar(&options.gpuNodeSelectorKey, "gpu-node-selector-key", "", "Optional GPU node label key.")
	flag.StringVar(&options.gpuNodeSelectorValue, "gpu-node-selector-value", "", "Optional GPU node label value.")
	flag.StringVar(&options.profiles.EC.Name, "vllm-ec-profile-name", "", "Platform-owned EC profile name; empty disables E/P/D.")
	flag.StringVar(&options.profiles.EC.Revision, "vllm-ec-profile-revision", "", "Platform-owned EC profile revision.")
	flag.StringVar(&options.profiles.EC.Connector, "vllm-ec-connector", "", "Fixed EC connector identity for the configured vLLM adapter.")
	flag.StringVar(&options.profiles.EC.SharedStorageClaim, "vllm-ec-shared-storage-claim", "", "ReadWriteMany PVC used by the configured EC connector.")
	flag.StringVar(&options.profiles.EC.SharedStoragePath, "vllm-ec-shared-storage-path", "", "In-container shared EC storage path supplied by the platform.")
	flag.Func("rdma-bindings", "JSON array of native RDMA allocations and their permitted CNI namespaces.", func(value string) error {
		if err := json.Unmarshal([]byte(value), &options.rdmaBindings); err != nil {
			return err
		}
		if options.rdmaBindings == nil {
			return errors.New("rdma-bindings must be a JSON array")
		}
		options.rdmaBindingsSet = true
		return nil
	})
	flag.StringVar(&options.rdmaResourceName, "rdma-resource-name", "", "Retained single-resource controller template; new templates use rdma-bindings.")
	flag.IntVar(&options.rdmaResourceCount, "rdma-resource-count", 0, "RDMA device-plugin allocation units requested by each distributed runtime Pod.")
	flag.Func("rdma-network", "Platform-prepared Multus network attachment; repeat for multiple RoCE interfaces.", func(value string) error {
		if value == "" {
			return errors.New("RDMA network attachment must be nonempty")
		}
		options.rdmaNetworks = append(options.rdmaNetworks, value)
		return nil
	})
	flag.StringVar(&options.profiles.MooncakeStore.Name, "vllm-mooncake-store-profile-name", "", "Opaque platform-owned Mooncake Store profile name; empty disables external Store.")
	flag.StringVar(&options.profiles.MooncakeStore.Revision, "vllm-mooncake-store-profile-revision", "", "Opaque platform-owned Mooncake Store profile revision.")
	flag.StringVar(&options.profiles.MooncakeStore.ConfigMapName, "vllm-mooncake-store-config-map-name", "", "Mooncake Store ConfigMap name.")
	flag.StringVar(&options.profiles.MooncakeStore.ConfigMapKey, "vllm-mooncake-store-config-map-key", "", "Mooncake Store ConfigMap key.")
	flag.StringVar(&options.profiles.MooncakeStore.PythonHashSeed, "vllm-mooncake-store-python-hash-seed", "", "PYTHONHASHSEED for Mooncake Store.")

	options.logOptions = zap.Options{Development: false}
	options.logOptions.BindFlags(flag.CommandLine)
	flag.Parse()
	flag.Visit(func(value *flag.Flag) {
		switch value.Name {
		case "rdma-resource-name", "rdma-resource-count", "rdma-network":
			options.legacyRDMASet = true
		}
	})
	return options
}

// resolveServingConfiguration converts platform flags into validated runtime and workload contracts.
func resolveServingConfiguration(options servingOptions) servingConfiguration {
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&options.logOptions)))
	workloadImagePullSecrets := make([]corev1.LocalObjectReference, len(options.workloadImagePullSecretNames))
	for index, name := range options.workloadImagePullSecretNames {
		workloadImagePullSecrets[index] = corev1.LocalObjectReference{Name: name}
	}
	cacheProfile := runtimeconfig.RuntimeCacheProfile{ClaimName: options.cacheClaimName, MountPath: options.cacheMountPath, DirectoryBindings: options.cacheDirectoryBindings}
	huggingFaceAccessProfile := runtimeconfig.HuggingFaceAccessProfile{Endpoint: options.modelSourceEndpoint, TokenSecretName: options.modelSourceTokenSecretName, TokenSecretKey: options.modelSourceTokenSecretKey}
	modelDistributionProfile := runtimeconfig.ModelDistributionProfile{ProxyURL: options.modelDownloadProxyURL}
	admissionStore := &inferencev1alpha1.RedisConnection{URL: os.Getenv(runtimeconfig.AdmissionStoreURLEnv)}
	if options.admissionStoreURLSecretName != "" || options.admissionStoreURLSecretKey != "" {
		admissionStore.URLSecretRef = &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: options.admissionStoreURLSecretName},
			Key:                  options.admissionStoreURLSecretKey,
		}
	}
	if err := runtimeconfig.ValidateRedisConnection(admissionStore); err != nil {
		ctrl.Log.Error(err, "invalid admission store configuration")
		os.Exit(1)
	}
	var responseStore *inferencev1alpha1.ResponseStoreConfig
	if options.frontendEnabled {
		connection := inferencev1alpha1.RedisConnection{URL: os.Getenv(runtimeconfig.ResponseStoreURLEnv)}
		if options.responseStoreURLSecretName != "" || options.responseStoreURLSecretKey != "" {
			connection.URLSecretRef = &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: options.responseStoreURLSecretName},
				Key:                  options.responseStoreURLSecretKey,
			}
		}
		var err error
		responseStore, err = runtimeconfig.ResponseStoreConfiguration(connection, options.responseRetention, options.responseReplayRetention)
		if err != nil {
			ctrl.Log.Error(err, "invalid response store configuration")
			os.Exit(1)
		}
	}
	var applicationFiles runtimeconfig.ApplicationFiles
	if err := json.Unmarshal([]byte(options.applicationFilesJSON), &applicationFiles); err != nil {
		ctrl.Log.Error(err, "invalid application file configuration")
		os.Exit(1)
	}
	if applicationFiles.Image == "" {
		ctrl.Log.Error(errors.New("application-files image must be nonempty"), "invalid platform tools configuration")
		os.Exit(1)
	}
	if (options.sourceMode || options.videoWorkerApplicationURL != "" || options.frontendApplicationURL != "" || options.modelServerApplicationURL != "") && (applicationFiles.Script == "" || applicationFiles.MountPath == "" || (options.sourceMode && applicationFiles.Origin == "")) {
		ctrl.Log.Error(errors.New("source-mode and video-worker-application-url require application-files; source-mode also requires its origin"), "invalid application file configuration")
		os.Exit(1)
	}
	if options.inferenceEngineImage == "" {
		ctrl.Log.Error(errors.New("inference-engine-image must be nonempty"), "invalid inference engine profile")
		os.Exit(1)
	}
	if err := cacheProfile.Validate(); err != nil {
		ctrl.Log.Error(err, "invalid runtime cache profile")
		os.Exit(1)
	}
	if err := huggingFaceAccessProfile.Validate(); err != nil {
		ctrl.Log.Error(err, "invalid Hugging Face access profile")
		os.Exit(1)
	}
	if err := modelDistributionProfile.Validate(); err != nil {
		ctrl.Log.Error(err, "invalid model distribution profile")
		os.Exit(1)
	}
	if options.modelServerPort < 1 || options.modelServerPort > 65535 {
		ctrl.Log.Error(errors.New("model-server-port must be between 1 and 65535"), "invalid inference engine profile")
		os.Exit(1)
	}
	if options.frontendMode != frontendModeLocal && options.frontendMode != frontendModeGateway {
		ctrl.Log.Error(errors.New("frontend-mode must be local or gateway"), "invalid frontend profile")
		os.Exit(1)
	}
	if options.rdmaBindingsSet && options.legacyRDMASet {
		ctrl.Log.Error(errors.New("rdma-bindings cannot be combined with retained single-resource flags"), "invalid RDMA bindings")
		os.Exit(1)
	}
	rdmaBindings := options.rdmaBindings
	if options.legacyRDMASet {
		if options.rdmaResourceName == "" || options.rdmaResourceCount < 1 || int64(options.rdmaResourceCount) > math.MaxInt32 {
			ctrl.Log.Error(errors.New("retained RDMA flags require a resource name and positive int32 count"), "invalid RDMA allocation")
			os.Exit(1)
		}
		allocation := &inferencev1alpha1.RDMAAllocation{
			ResourceName: options.rdmaResourceName, ResourceCount: int32(options.rdmaResourceCount), Networks: options.rdmaNetworks,
		}
		rdmaBindings = []resolver.RDMANetworkBinding{{Allocation: *allocation.Canonical(), AllowExplicitNetwork: true}}
	}
	for index := range rdmaBindings {
		binding := &rdmaBindings[index]
		if binding.Namespace != "" && len(validation.IsDNS1123Label(binding.Namespace)) != 0 {
			ctrl.Log.Error(errors.New("RDMA binding namespace must be a Kubernetes namespace name"), "invalid RDMA binding", "index", index)
			os.Exit(1)
		}
		if !binding.AllowExplicitNetwork {
			for _, reference := range binding.Allocation.Networks {
				namespace, name, qualified := strings.Cut(reference, "/")
				if !qualified || len(validation.IsDNS1123Label(namespace)) != 0 || len(validation.IsDNS1123Subdomain(name)) != 0 {
					ctrl.Log.Error(errors.New("RDMA binding networks must be namespace/name attachment references"), "invalid RDMA binding", "index", index)
					os.Exit(1)
				}
			}
		}
		if binding.Allocation.ResourceName != "" || binding.Allocation.ResourceCount != 0 {
			ctrl.Log.Error(errors.New("rdma-bindings requires native resources, not resourceName/resourceCount"), "invalid RDMA binding", "index", index)
			os.Exit(1)
		}
		if err := runtimeconfig.ValidateRDMAAllocation(&binding.Allocation, options.gpuResourceName); err != nil {
			ctrl.Log.Error(err, "invalid RDMA binding", "index", index)
			os.Exit(1)
		}
		binding.Allocation = *binding.Allocation.Canonical()
	}
	if err := options.profiles.Validate(); err != nil {
		ctrl.Log.Error(err, "invalid vLLM runtime profile")
		os.Exit(1)
	}
	var ec *vllmconfig.ECProfile
	var mooncakeStore *vllmconfig.MooncakeStoreProfile
	if options.profiles.EC.Name != "" {
		ec = &options.profiles.EC
	}
	if options.profiles.MooncakeStore.Name != "" {
		mooncakeStore = &options.profiles.MooncakeStore
	}
	return servingConfiguration{
		deploymentUID:                         options.deploymentUID,
		metricsAddress:                        options.metricsAddress,
		probeAddress:                          options.probeAddress,
		leaderElection:                        options.leaderElection,
		sourceMode:                            options.sourceMode,
		frontendEnabled:                       options.frontendEnabled,
		frontendMode:                          options.frontendMode,
		frontendImage:                         options.frontendImage,
		frontendApplicationURL:                options.frontendApplicationURL,
		modelServerApplicationURL:             options.modelServerApplicationURL,
		modelPreparationImage:                 options.modelPreparationImage,
		videoWorkerImage:                      options.videoWorkerImage,
		videoWorkerApplicationURL:             options.videoWorkerApplicationURL,
		frontendPort:                          options.frontendPort,
		frontendGatewayName:                   options.frontendGatewayName,
		frontendGatewayNamespace:              options.frontendGatewayNamespace,
		frontendGatewaySectionName:            options.frontendGatewaySectionName,
		autoscalingTelemetryConfigurationPath: options.autoscalingTelemetryConfigurationPath,
		workloadImagePullSecrets:              workloadImagePullSecrets,
		cacheProfile:                          cacheProfile,
		huggingFaceAccessProfile:              huggingFaceAccessProfile,
		modelDistributionProfile:              modelDistributionProfile,
		applicationFiles:                      applicationFiles,
		admissionStore:                        admissionStore,
		responseStore:                         responseStore,
		observabilityPrometheus:               options.observabilityPrometheus,
		observabilityLabelsJSON:               options.observabilityLabelsJSON,
		runtimeProfile: resolver.RuntimeProfile{
			Image:                  options.inferenceEngineImage,
			LeaderWorkerScheduling: options.leaderWorkerScheduling,
			OmniImage:              options.omniInferenceEngineImage,
			NsightImage:            options.nsightImage,
			AdmissionStore:         admissionStore,
			ModelServerPort:        int32(options.modelServerPort),
			DeviceResourceName:     options.gpuResourceName,
			RuntimeClassName:       options.runtimeClassName,
			NodeSelectorKey:        options.gpuNodeSelectorKey,
			NodeSelectorValue:      options.gpuNodeSelectorValue,
			RDMABindings:           rdmaBindings,
			EC:                     ec,
			MooncakeStore:          mooncakeStore,
		},
	}
}

// newServingManager registers APIs, validates Gateway discovery, and applies the deployment scope to clients.
func newServingManager(configuration *servingConfiguration) ctrl.Manager {
	// Register built-in and Foretoken APIs before controllers are attached to the manager.
	scheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(inferencev1alpha1.AddToScheme(scheme))
	utilruntime.Must(monitoringv1.AddToScheme(scheme))
	utilruntime.Must(gatewayv1.Install(scheme))
	utilruntime.Must(lwsv1.AddToScheme(scheme))

	restConfig := ctrl.GetConfigOrDie()
	if configuration.frontendEnabled && configuration.frontendMode == frontendModeGateway {
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

	controlPlaneNamespace := os.Getenv("POD_NAMESPACE")
	if controlPlaneNamespace == "" || configuration.deploymentUID == "" {
		ctrl.Log.Error(errors.New("POD_NAMESPACE and --deployment-uid are required"), "unable to configure deployment controller scope")
		os.Exit(1)
	}
	configuration.controlPlaneNamespace = controlPlaneNamespace
	scope := deployments.Scope{Namespace: controlPlaneNamespace, UID: configuration.deploymentUID}
	// Instance membership applies to watches and both client paths. Shared claims
	// remain observable so binding or expansion can wake their instance consumers.
	manager, err := ctrl.NewManager(restConfig, ctrl.Options{
		Scheme: scheme,
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{
				controlPlaneNamespace: {LabelSelector: scope.Selector()},
			},
			ByObject: map[client.Object]cache.ByObject{
				&corev1.PersistentVolumeClaim{}: {Namespaces: map[string]cache.Config{
					controlPlaneNamespace: {LabelSelector: labels.Everything()},
				}},
			},
		},
		Client: client.Options{Cache: &client.CacheOptions{
			// Namespace credentials are read by fixed names without a Secret informer.
			DisableFor: []client.Object{&corev1.Secret{}},
		}},
		Metrics:                 metricsserver.Options{BindAddress: configuration.metricsAddress},
		HealthProbeBindAddress:  configuration.probeAddress,
		LeaderElection:          configuration.leaderElection,
		LeaderElectionID:        deployments.ResourceName(configuration.deploymentUID),
		LeaderElectionNamespace: controlPlaneNamespace,
		// Main exits when the manager stops, so a normal shutdown can release the lease.
		LeaderElectionReleaseOnCancel: true,
	})
	if err != nil {
		ctrl.Log.Error(err, "unable to create manager")
		os.Exit(1)
	}
	return scope.Manager(manager)
}

// registerServingControllers attaches every instance controller in the established lifecycle order.
func registerServingControllers(manager ctrl.Manager, configuration servingConfiguration) {
	var observabilityLabels map[string]string
	if err := json.Unmarshal([]byte(configuration.observabilityLabelsJSON), &observabilityLabels); err != nil {
		ctrl.Log.Error(err, "invalid observability labels")
		os.Exit(1)
	}
	serviceAlerts, err := observability.NewServiceAlerts(manager, configuration.observabilityPrometheus, observabilityLabels)
	if err != nil {
		ctrl.Log.Error(err, "invalid observability configuration")
		os.Exit(1)
	}

	// Controllers are registered explicitly so each resource keeps one lifecycle owner.
	if err := (&profiling.Reconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ProfileRun controller")
		os.Exit(1)
	}
	if err := (&video.Reconciler{Client: manager.GetClient(), WorkerImage: configuration.videoWorkerImage, WorkerApplicationURL: configuration.videoWorkerApplicationURL, ApplicationFiles: configuration.applicationFiles, FrontendPort: int32(configuration.frontendPort), ImagePullSecrets: configuration.workloadImagePullSecrets}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register VideoTask controller")
		os.Exit(1)
	}
	if err := (&runtimecache.Reconciler{Client: manager.GetClient(), Profile: configuration.cacheProfile}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register RuntimeCache controller")
		os.Exit(1)
	}
	if configuration.frontendEnabled {
		var gateway *frontend.GatewayParent
		if configuration.frontendMode == frontendModeGateway {
			gateway = &frontend.GatewayParent{
				Name:        configuration.frontendGatewayName,
				Namespace:   configuration.frontendGatewayNamespace,
				SectionName: configuration.frontendGatewaySectionName,
			}
		}
		frontendReconciler := &frontend.Reconciler{
			Client:       manager.GetClient(),
			APIReader:    manager.GetAPIReader(),
			CacheProfile: configuration.cacheProfile,
			Alerts:       serviceAlerts,
			RuntimeProfile: frontend.FrontendRuntimeProfile{
				SourceMode:        configuration.sourceMode,
				ApplicationFiles:  configuration.applicationFiles,
				Image:             configuration.frontendImage,
				ApplicationURL:    configuration.frontendApplicationURL,
				WorkerImage:       configuration.videoWorkerImage,
				Port:              int32(configuration.frontendPort),
				ImagePullSecrets:  configuration.workloadImagePullSecrets,
				HuggingFaceAccess: configuration.huggingFaceAccessProfile.Access(),
				AdmissionStore:    configuration.admissionStore,
				ResponseStore:     configuration.responseStore,
				Gateway:           gateway,
			},
		}
		if err := frontendReconciler.SetupWithManager(manager); err != nil {
			ctrl.Log.Error(err, "unable to register FrontendService controller")
			os.Exit(1)
		}
	}
	metricsProvider, err := model.NewHTTPScalingMetricsProvider(manager.GetClient(), configuration.autoscalingTelemetryConfigurationPath)
	if err != nil {
		ctrl.Log.Error(err, "invalid autoscaling telemetry configuration")
		os.Exit(1)
	}
	if err := (&model.ServiceReconciler{
		Client:                   manager.GetClient(),
		CacheProfile:             configuration.cacheProfile,
		SourceMode:               configuration.sourceMode,
		RuntimeProfile:           configuration.runtimeProfile,
		ApplicationFiles:         configuration.applicationFiles,
		ApplicationURL:           configuration.modelServerApplicationURL,
		HuggingFaceAccessProfile: configuration.huggingFaceAccessProfile,
		Alerts:                   serviceAlerts,
		MetricsProvider:          metricsProvider,
	}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ModelService controller")
		os.Exit(1)
	}
	if err := (&kv.ServiceReconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVService controller")
		os.Exit(1)
	}
	if err := (&kv.PoolReconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVPool controller")
		os.Exit(1)
	}
	if err := (&kv.GroupReconciler{Client: manager.GetClient(), HTTPClient: &http.Client{Timeout: 2 * time.Second}, ControlPlaneNamespace: configuration.controlPlaneNamespace}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVGroup controller")
		os.Exit(1)
	}
	if err := (&model.PoolReconciler{Client: manager.GetClient(), RuntimeProfile: configuration.runtimeProfile}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ModelPool controller")
		os.Exit(1)
	}
	if err := (&model.GroupReconciler{Client: manager.GetClient(), ControlPlaneNamespace: configuration.controlPlaneNamespace, ImagePullSecrets: configuration.workloadImagePullSecrets, ModelDistribution: configuration.modelDistributionProfile, ApplicationFiles: configuration.applicationFiles, PreparationImage: configuration.modelPreparationImage}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ModelGroup controller")
		os.Exit(1)
	}
}

// startServingManager registers probes and starts the already configured manager.
func startServingManager(manager ctrl.Manager) {
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
