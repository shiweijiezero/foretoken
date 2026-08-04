// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Package v1alpha1 contains API schema definitions for inference.foretoken.io/v1alpha1.
// +kubebuilder:object:generate=true
// +groupName=inference.foretoken.io
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

var (
	GroupVersion  = schema.GroupVersion{Group: "inference.foretoken.io", Version: "v1alpha1"}
	SchemeBuilder = runtime.NewSchemeBuilder(func(scheme *runtime.Scheme) error {
		metav1.AddToGroupVersion(scheme, GroupVersion)
		return nil
	})
	AddToScheme = SchemeBuilder.AddToScheme
)
