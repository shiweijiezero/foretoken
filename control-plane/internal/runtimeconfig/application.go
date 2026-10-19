// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects platform-selected application files into workload startup.
package runtimeconfig

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"net/http"
	"net/url"
	"path"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
)

// ApplicationFiles contains the platform's file origin, download tools and mount layout.
// Controllers use it to prepare immutable application versions before starting a workload.
type ApplicationFiles struct {
	Origin    string `json:"origin"`
	Image     string `json:"image"`
	Script    string `json:"script"`
	MountPath string `json:"mountPath"`
}

// Configure prepares the selected files in a Pod-local volume and starts its executable.
// The Pod owns the application volume; persistent model storage remains independent.
func (files ApplicationFiles) Configure(template *corev1.PodTemplateSpec, container *corev1.Container, reference, executable string) {
	if reference == "" {
		return
	}
	template.Labels = maps.Clone(template.Labels)
	if template.Labels == nil {
		template.Labels = make(map[string]string)
	}
	template.Labels["foretoken.io/application-files"] = "consumer"
	template.Annotations = maps.Clone(template.Annotations)
	if template.Annotations == nil {
		template.Annotations = make(map[string]string)
	}
	template.Annotations["inference.foretoken.io/application-url"] = reference
	pod := &template.Spec
	// Storage projection supplies the same Pod identity to the downloader and executable.
	noEscalation, readOnly := false, true
	pod.Volumes = append(pod.Volumes, corev1.Volume{
		Name: "application", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
	})
	pod.InitContainers = append(pod.InitContainers, corev1.Container{
		Name: "application-files", Image: files.Image, ImagePullPolicy: corev1.PullIfNotPresent,
		Command:      []string{"python", "-c", files.Script, reference, files.MountPath},
		VolumeMounts: []corev1.VolumeMount{{Name: "application", MountPath: files.MountPath}},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: &noEscalation, ReadOnlyRootFilesystem: &readOnly,
			Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	})
	container.VolumeMounts = append(container.VolumeMounts, corev1.VolumeMount{
		Name: "application", MountPath: files.MountPath, ReadOnly: true,
	})
	command := path.Join(files.Directory(), "bin", executable)
	if len(container.Command) == 0 {
		container.Command = []string{command}
	} else {
		container.Command[0] = command
	}
}

// ResolveImage reads a newly selected source publication's image for service controllers.
// Only legacy manifests without an image field use the platform image; redirects are refused.
func (files ApplicationFiles) ResolveImage(ctx context.Context, component, revision, platformImage string) (string, error) {
	manifestURL := files.Ref(component, revision) + "/manifest.json"
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, manifestURL, nil)
	if err != nil {
		return "", fmt.Errorf("create %s application manifest request: %w", component, err)
	}
	client := &http.Client{
		Timeout: 5 * time.Second,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	response, err := client.Do(request)
	if err != nil {
		return "", fmt.Errorf("fetch %s application manifest: %w", component, err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("fetch %s application manifest: HTTP %d", component, response.StatusCode)
	}
	payload, err := io.ReadAll(response.Body)
	if err != nil {
		return "", fmt.Errorf("read %s application manifest: %w", component, err)
	}
	var manifest map[string]json.RawMessage
	if err := json.Unmarshal(payload, &manifest); err != nil {
		return "", fmt.Errorf("decode %s application manifest: %w", component, err)
	}
	if manifest == nil {
		return "", fmt.Errorf("%s application manifest must be an object", component)
	}
	rawImage, present := manifest["image"]
	if !present {
		return platformImage, nil
	}
	var image string
	if err := json.Unmarshal(rawImage, &image); err != nil || strings.TrimSpace(image) == "" {
		return "", fmt.Errorf("%s application manifest image must be a nonempty string", component)
	}
	return image, nil
}

// Ref resolves a service's selected component version at the trusted platform origin.
// An absent revision produces no source override.
func (files ApplicationFiles) Ref(component, revision string) string {
	if revision == "" {
		return ""
	}
	return strings.TrimRight(files.Origin, "/") + "/" + url.PathEscape(component) + "/" + url.PathEscape(revision)
}

// Directory returns the completed Pod-local application directory consumed at startup.
func (files ApplicationFiles) Directory() string {
	return path.Join(files.MountPath, "current")
}
