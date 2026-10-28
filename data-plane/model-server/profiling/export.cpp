// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Retain Kineto's trace format while allowing Python worker control during export.
#include <torch/csrc/autograd/profiler_kineto.h>
#include <torch/csrc/utils/pybind.h>
#include <pybind11/pybind11.h>

PYBIND11_MODULE(foretoken_profile_native, module) {
  module.def(
      "save",
      [](pybind11::object value, const std::string& path) {
        auto& profile = value.cast<torch::autograd::profiler::ProfilerResult&>();
        // value keeps the completed result alive until the native writer returns.
        pybind11::gil_scoped_release release;
        profile.save(path);
      },
      "Export one completed Kineto result while its caller retains capture ownership.");
}
