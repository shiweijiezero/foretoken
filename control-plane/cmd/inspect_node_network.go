// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reads the running container runtime's CNI paths for short-lived installer Pods.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"

	"github.com/pelletier/go-toml/v2"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	runtimeapi "k8s.io/cri-api/pkg/apis/runtime/v1"
)

// nodeCNIPaths contains only the effective directories needed by the CNI installer.
// Full runtime configuration, including registry credentials, never leaves this process.
type nodeCNIPaths struct {
	Configuration string   `json:"confDir"`
	Binaries      []string `json:"binDirs"`
}

// inspectNodeNetwork reads one selected runtime socket and emits its active CNI directories.
// The caller owns the inspection Pod, socket mount, and operation deadline.
func inspectNodeNetwork(arguments []string) error {
	flags := flag.NewFlagSet("inspect-node-network", flag.ContinueOnError)
	endpoint := flags.String("endpoint", "", "Mounted container runtime Unix socket.")
	kind := flags.String("runtime", "", "Runtime implementation: containerd or cri-o.")
	handler := flags.String("handler", "", "RuntimeClass handler used by the model workload.")
	budget := flags.Duration("timeout", 0, "Remaining installer operation time.")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	if flags.NArg() != 0 || !filepath.IsAbs(*endpoint) || *budget <= 0 {
		return errors.New("inspect-node-network requires an absolute socket path and positive timeout")
	}
	ctx, cancel := context.WithTimeout(context.Background(), *budget)
	defer cancel()
	var paths nodeCNIPaths
	var err error
	switch *kind {
	case "containerd":
		paths, err = containerdCNIPaths(ctx, *endpoint, *handler)
	case "cri-o":
		paths, err = crioCNIPaths(ctx, *endpoint)
	default:
		return fmt.Errorf("CNI discovery is not available for runtime %q", *kind)
	}
	if err != nil {
		return err
	}
	if !filepath.IsAbs(paths.Configuration) || len(paths.Binaries) == 0 {
		return errors.New("the running container runtime did not report its CNI directories")
	}
	for _, directory := range paths.Binaries {
		if !filepath.IsAbs(directory) {
			return errors.New("the running container runtime reported a non-absolute CNI binary directory")
		}
	}
	return json.NewEncoder(os.Stdout).Encode(paths)
}

// containerdCNIPaths consumes CRI Status from the running runtime, after its imports and overrides.
func containerdCNIPaths(ctx context.Context, endpoint, handler string) (nodeCNIPaths, error) {
	connection, err := grpc.NewClient("unix://"+endpoint, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nodeCNIPaths{}, err
	}
	defer connection.Close()
	response, err := runtimeapi.NewRuntimeServiceClient(connection).Status(ctx, &runtimeapi.StatusRequest{Verbose: true})
	if err != nil {
		return nodeCNIPaths{}, fmt.Errorf("read containerd CNI configuration: %w", err)
	}
	var network struct {
		PluginConfDir string
		PluginDirs    []string
	}
	if raw := response.Info["cniconfig"]; raw != "" {
		if err := json.Unmarshal([]byte(raw), &network); err != nil {
			return nodeCNIPaths{}, fmt.Errorf("decode containerd CNI paths: %w", err)
		}
	}
	var configuration struct {
		CNI struct {
			ConfDir string   `json:"confDir"`
			BinDir  string   `json:"binDir"`
			BinDirs []string `json:"binDirs"`
		} `json:"cni"`
		Containerd struct {
			DefaultRuntimeName string `json:"defaultRuntimeName"`
			Runtimes           map[string]struct {
				CNIConfDir string `json:"cniConfDir"`
			} `json:"runtimes"`
		} `json:"containerd"`
	}
	if err := json.Unmarshal([]byte(response.Info["config"]), &configuration); err != nil {
		return nodeCNIPaths{}, fmt.Errorf("decode effective containerd configuration: %w", err)
	}
	paths := nodeCNIPaths{Configuration: network.PluginConfDir, Binaries: network.PluginDirs}
	if paths.Configuration == "" {
		paths.Configuration = configuration.CNI.ConfDir
	}
	if len(paths.Binaries) == 0 {
		paths.Binaries = configuration.CNI.BinDirs
		if len(paths.Binaries) == 0 && configuration.CNI.BinDir != "" {
			paths.Binaries = []string{configuration.CNI.BinDir}
		}
	}
	if handler == "" {
		handler = configuration.Containerd.DefaultRuntimeName
	} else if _, exists := configuration.Containerd.Runtimes[handler]; !exists {
		return nodeCNIPaths{}, fmt.Errorf("containerd has no runtime handler %q", handler)
	}
	if override := configuration.Containerd.Runtimes[handler].CNIConfDir; override != "" {
		paths.Configuration = override
	}
	return paths, nil
}

// crioCNIPaths reads CRI-O's native effective-config endpoint on the selected runtime socket.
func crioCNIPaths(ctx context.Context, endpoint string) (nodeCNIPaths, error) {
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", endpoint)
	}}
	defer transport.CloseIdleConnections()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://localhost/config", nil)
	if err != nil {
		return nodeCNIPaths{}, err
	}
	response, err := (&http.Client{Transport: transport}).Do(request)
	if err != nil {
		return nodeCNIPaths{}, fmt.Errorf("read CRI-O CNI configuration: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nodeCNIPaths{}, fmt.Errorf("CRI-O configuration endpoint returned %s", response.Status)
	}
	var configuration struct {
		CRIO struct {
			Network struct {
				Directory string   `toml:"network_dir"`
				Plugins   []string `toml:"plugin_dirs"`
			} `toml:"network"`
		} `toml:"crio"`
	}
	data, err := io.ReadAll(response.Body)
	if err != nil {
		return nodeCNIPaths{}, fmt.Errorf("read CRI-O configuration response: %w", err)
	}
	if err := toml.Unmarshal(data, &configuration); err != nil {
		return nodeCNIPaths{}, errors.New("CRI-O returned invalid configuration")
	}
	return nodeCNIPaths{Configuration: configuration.CRIO.Network.Directory, Binaries: configuration.CRIO.Network.Plugins}, nil
}
