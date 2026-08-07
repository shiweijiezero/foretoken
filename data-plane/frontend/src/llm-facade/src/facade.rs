// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//! Prefill/decode and encoder/prefill/decode model-server orchestration.

use async_trait::async_trait;
use futures::StreamExt;
use vllm_llm::{FinishReason, GenerateRequest};

use crate::http::{
    HttpFacade, MODEL_SERVER_REQUEST_START_TIMEOUT, bootstrap_engine_id, validate_endpoint,
};
use crate::{LlmFacade, LlmFacadeError, TokenStream};

/// Executes requests through one static prefill/decode pair or E/P/D triplet.
#[derive(Clone)]
pub struct VllmFacade {
    encoder: Option<HttpFacade>,
    prefill: HttpFacade,
    decode: HttpFacade,
    bootstrap_endpoint: String,
    bootstrap_client: reqwest::Client,
}

impl VllmFacade {
    pub fn prefill_decode(
        prefill_endpoint: String,
        decode_endpoint: String,
        bootstrap_endpoint: String,
    ) -> Result<Self, LlmFacadeError> {
        Self::new(None, prefill_endpoint, decode_endpoint, bootstrap_endpoint)
    }

    /// Builds the execution facade for one statically bound encoder/prefill/decode triplet.
    pub fn encoder_prefill_decode(
        encoder_endpoint: String,
        prefill_endpoint: String,
        decode_endpoint: String,
        bootstrap_endpoint: String,
    ) -> Result<Self, LlmFacadeError> {
        Self::new(
            Some(encoder_endpoint),
            prefill_endpoint,
            decode_endpoint,
            bootstrap_endpoint,
        )
    }

    fn new(
        encoder_endpoint: Option<String>,
        prefill_endpoint: String,
        decode_endpoint: String,
        bootstrap_endpoint: String,
    ) -> Result<Self, LlmFacadeError> {
        let bootstrap_endpoint = validate_endpoint(bootstrap_endpoint)?;
        Ok(Self {
            encoder: encoder_endpoint.map(HttpFacade::new).transpose()?,
            prefill: HttpFacade::new(prefill_endpoint)?,
            decode: HttpFacade::new(decode_endpoint)?,
            bootstrap_endpoint,
            bootstrap_client: reqwest::Client::builder()
                .timeout(MODEL_SERVER_REQUEST_START_TIMEOUT)
                .build()
                .expect("static bootstrap client configuration must be valid"),
        })
    }

    async fn generate_pd(&self, request: GenerateRequest) -> Result<TokenStream, LlmFacadeError> {
        let engine_id =
            bootstrap_engine_id(&self.bootstrap_client, &self.bootstrap_endpoint).await?;
        let (prefill_request, decode_request) =
            pd_requests(request, &self.bootstrap_endpoint, engine_id)?;
        let prefill_id = prefill_request.request_id.clone();
        let decode_id = decode_request.request_id.clone();
        let prefill_stream = self.prefill.generate(prefill_request).await?;
        if let Err(error) = consume_prefill(prefill_stream).await {
            let _ = self.prefill.abort(&[prefill_id]).await;
            return Err(error);
        }
        match self.decode.generate(decode_request).await {
            Ok(stream) => Ok(stream),
            Err(error) => {
                let abort_error = self.decode.abort(&[decode_id]).await.err();
                Err(preferred_error(Some(error), None, None, abort_error))
            }
        }
    }

    async fn generate_epd(&self, request: GenerateRequest) -> Result<TokenStream, LlmFacadeError> {
        let encoder = self.encoder.as_ref().ok_or(LlmFacadeError::Configuration)?;
        let engine_id =
            bootstrap_engine_id(&self.bootstrap_client, &self.bootstrap_endpoint).await?;
        let (encoder_request, mut prefill_request, decode_request) =
            epd_requests(request, &self.bootstrap_endpoint, engine_id)?;
        let encoder_id = encoder_request.request_id.clone();
        let prefill_id = prefill_request.request_id.clone();
        let decode_id = decode_request.request_id.clone();
        let mut started = StartedStages::new(
            self.clone(),
            encoder_id.clone(),
            prefill_id.clone(),
            decode_id,
        );

        // A transport error can arrive after the model-server accepts work, so guard each
        // dispatch before awaiting its response.
        started.encoder = true;
        let encoder_stream = match encoder.generate(encoder_request).await {
            Ok(stream) => stream,
            Err(error) => return Err(error),
        };
        let descriptor = consume_encoder(encoder_stream).await?;
        inject_ec_transfer_params(&mut prefill_request, descriptor);

        started.prefill = true;
        let prefill_stream = match self.prefill.generate(prefill_request).await {
            Ok(stream) => stream,
            Err(error) => return Err(error),
        };
        consume_prefill(prefill_stream).await?;

        started.decode = true;
        let decode_stream = match self.decode.generate(decode_request).await {
            Ok(stream) => stream,
            Err(error) => return Err(error),
        };
        started.disarm();
        Ok(decode_stream)
    }
}

#[async_trait]
impl LlmFacade for VllmFacade {
    async fn generate(&self, request: GenerateRequest) -> Result<TokenStream, LlmFacadeError> {
        reject_client_transfer_params(&request)?;
        if self.encoder.is_some() {
            self.generate_epd(request).await
        } else {
            self.generate_pd(request).await
        }
    }

    async fn abort(&self, request_ids: &[String]) -> Result<(), LlmFacadeError> {
        let encoder_ids = request_ids
            .iter()
            .map(|id| format!("{id}/encoder"))
            .collect::<Vec<_>>();
        let prefill_ids = request_ids
            .iter()
            .map(|id| format!("{id}/prefill"))
            .collect::<Vec<_>>();
        let decode_ids = request_ids
            .iter()
            .map(|id| format!("{id}/decode"))
            .collect::<Vec<_>>();
        let encoder_abort = async {
            match &self.encoder {
                Some(encoder) => encoder.abort(&encoder_ids).await,
                None => Ok(()),
            }
        };
        let (encoder_error, prefill_error, decode_error) = tokio::join!(
            encoder_abort,
            self.prefill.abort(&prefill_ids),
            self.decode.abort(&decode_ids)
        );
        match (encoder_error, prefill_error, decode_error) {
            (Ok(()), Ok(()), Ok(())) => Ok(()),
            (encoder, prefill, decode) => Err(preferred_error(
                encoder.err(),
                prefill.err(),
                decode.err(),
                None,
            )),
        }
    }
}

/// Aborts stages that have started if the orchestration future is cancelled or fails.
struct StartedStages {
    facade: Option<VllmFacade>,
    request_id: String,
    encoder: bool,
    prefill: bool,
    decode: bool,
}

impl StartedStages {
    fn new(facade: VllmFacade, encoder_id: String, prefill_id: String, decode_id: String) -> Self {
        let request_id = encoder_id
            .strip_suffix("/encoder")
            .expect("server-generated encoder ID")
            .to_owned();
        debug_assert_eq!(prefill_id, format!("{request_id}/prefill"));
        debug_assert_eq!(decode_id, format!("{request_id}/decode"));
        Self {
            facade: Some(facade),
            request_id,
            encoder: false,
            prefill: false,
            decode: false,
        }
    }

    fn disarm(&mut self) {
        self.facade = None;
    }
}

impl Drop for StartedStages {
    fn drop(&mut self) {
        let Some(facade) = self.facade.take() else {
            return;
        };
        let request_id = self.request_id.clone();
        let encoder = self.encoder;
        let prefill = self.prefill;
        let decode = self.decode;
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                if encoder && let Some(stage) = facade.encoder {
                    let _ = stage.abort(&[format!("{request_id}/encoder")]).await;
                }
                if prefill {
                    let _ = facade
                        .prefill
                        .abort(&[format!("{request_id}/prefill")])
                        .await;
                }
                if decode {
                    let _ = facade.decode.abort(&[format!("{request_id}/decode")]).await;
                }
            });
        }
    }
}

async fn consume_encoder(stream: TokenStream) -> Result<serde_json::Value, LlmFacadeError> {
    let mut stream = Box::pin(stream);
    while let Some(event) = stream.next().await {
        let output = event?;
        if output.finish_reason.is_some() {
            return output.ec_transfer_params.ok_or(LlmFacadeError::Protocol);
        }
    }
    Err(LlmFacadeError::Protocol)
}

async fn consume_prefill(stream: TokenStream) -> Result<(), LlmFacadeError> {
    let mut stream = Box::pin(stream);
    while let Some(event) = stream.next().await {
        match event?.finish_reason {
            Some(FinishReason::Length) => return Ok(()),
            Some(_) => return Err(LlmFacadeError::RequestFailed),
            None => {}
        }
    }
    Err(LlmFacadeError::Protocol)
}

fn preferred_error(
    primary: Option<LlmFacadeError>,
    secondary: Option<LlmFacadeError>,
    first_abort: Option<LlmFacadeError>,
    second_abort: Option<LlmFacadeError>,
) -> LlmFacadeError {
    primary
        .or(secondary)
        .or(first_abort)
        .or(second_abort)
        .unwrap_or(LlmFacadeError::RequestFailed)
}

fn reject_client_transfer_params(request: &GenerateRequest) -> Result<(), LlmFacadeError> {
    if request
        .sampling_params
        .extra_args
        .as_ref()
        .is_some_and(|args| {
            args.contains_key("kv_transfer_params") || args.contains_key("ec_transfer_params")
        })
    {
        Err(LlmFacadeError::Configuration)
    } else {
        Ok(())
    }
}

fn pd_requests(
    request: GenerateRequest,
    bootstrap_endpoint: &str,
    engine_id: String,
) -> Result<(GenerateRequest, GenerateRequest), LlmFacadeError> {
    reject_client_transfer_params(&request)?;
    let transfer_id = format!("xfer-{}", request.request_id);
    let mut prefill = request.clone();
    prefill.request_id = format!("{}/prefill", request.request_id);
    prefill.sampling_params.max_tokens = 1;
    prefill.sampling_params.min_tokens = 1;
    let mut prefill_args = prefill
        .sampling_params
        .extra_args
        .take()
        .unwrap_or_default();
    prefill_args.insert("kv_transfer_params".into(), serde_json::json!({"do_remote_decode": true, "do_remote_prefill": false, "transfer_id": transfer_id}));
    prefill.sampling_params.extra_args = Some(prefill_args);
    let mut decode = request;
    decode.request_id = format!("{}/decode", decode.request_id);
    let mut decode_args = decode.sampling_params.extra_args.take().unwrap_or_default();
    decode_args.insert("kv_transfer_params".into(), serde_json::json!({"do_remote_decode": false, "do_remote_prefill": true, "remote_bootstrap_addr": bootstrap_endpoint, "remote_engine_id": engine_id, "transfer_id": transfer_id}));
    decode.sampling_params.extra_args = Some(decode_args);
    Ok((prefill, decode))
}

fn epd_requests(
    request: GenerateRequest,
    bootstrap_endpoint: &str,
    engine_id: String,
) -> Result<(GenerateRequest, GenerateRequest, GenerateRequest), LlmFacadeError> {
    reject_client_transfer_params(&request)?;
    let mut encoder = request.clone();
    encoder.request_id = format!("{}/encoder", request.request_id);
    // EC connectors produce their descriptor at the encoder's bounded terminal output.
    encoder.sampling_params.max_tokens = 1;
    encoder.sampling_params.min_tokens = 1;
    let (prefill, decode) = pd_requests(request, bootstrap_endpoint, engine_id)?;
    Ok((encoder, prefill, decode))
}

fn inject_ec_transfer_params(request: &mut GenerateRequest, descriptor: serde_json::Value) {
    request
        .sampling_params
        .extra_args
        .get_or_insert_default()
        .insert("ec_transfer_params".into(), descriptor);
}
