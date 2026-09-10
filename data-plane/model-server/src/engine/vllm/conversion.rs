// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! vLLM adapter conversions between vLLM engine-native types and the Foretoken
//! shared vocabulary.

use foretoken_model_protocol::ModelDtype;

/// Translates a vLLM [`ModelDtype`] into the shared [`ModelDtype`].
pub fn to_neutral_model_dtype(
    dtype: vllm_engine_core_client::protocol::dtype::ModelDtype,
) -> ModelDtype {
    match dtype {
        vllm_engine_core_client::protocol::dtype::ModelDtype::Float16 => ModelDtype::Float16,
        vllm_engine_core_client::protocol::dtype::ModelDtype::BFloat16 => ModelDtype::BFloat16,
        vllm_engine_core_client::protocol::dtype::ModelDtype::Float32 => ModelDtype::Float32,
    }
}
