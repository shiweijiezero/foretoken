// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::Body;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use futures::StreamExt;
use tokio::net::TcpListener;
use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
use vllm_llm::GenerateRequest;

use super::{HttpFacade, LlmFacade, LlmFacadeError, VllmFacade, abort_on_drop};

type Requests = Arc<Mutex<Vec<serde_json::Value>>>;
type Aborts = Arc<Mutex<Vec<Vec<String>>>>;

fn request(id: &str) -> GenerateRequest {
    GenerateRequest {
        request_id: id.into(),
        prompt_token_ids: vec![1, 2],
        sampling_params: EngineCoreSamplingParams::default(),
        mm_features: None,
        arrival_time: None,
        cache_salt: None,
        trace_headers: None,
        priority: 0,
        data_parallel_rank: None,
        reasoning_parser_kwargs: None,
        lora_request: None,
    }
}

#[test]
fn rejects_an_invalid_model_server_endpoint() {
    assert!(HttpFacade::new("not an endpoint".into()).is_err());
}

#[test]
fn facade_is_backend_neutral_execution_port() {
    fn accepts(_: &dyn LlmFacade) {}
    let facade = HttpFacade::new("http://127.0.0.1:30000".into()).unwrap();
    accepts(&facade);
}

#[tokio::test]
async fn aggregate_forwards_ndjson_and_abort() {
    let requests = Arc::new(Mutex::new(Vec::new()));
    let aborts = Arc::new(Mutex::new(Vec::new()));
    let (endpoint, task) = server("aggregate", requests.clone(), aborts.clone()).await;
    let facade = HttpFacade::new(endpoint).unwrap();

    let output = facade
        .generate(request("request-1"))
        .await
        .unwrap()
        .next()
        .await
        .unwrap()
        .unwrap();
    assert_eq!(output.token_ids, vec![42]);
    facade.abort(&["request-1".into()]).await.unwrap();
    assert_eq!(requests.lock().unwrap()[0]["request_id"], "request-1");
    assert_eq!(aborts.lock().unwrap().as_slice(), [["request-1"]]);
    task.abort();
}

#[tokio::test]
async fn dropping_an_unfinished_stream_aborts_backend_work() {
    let requests = Arc::new(Mutex::new(Vec::new()));
    let aborts = Arc::new(Mutex::new(Vec::new()));
    let (endpoint, task) = server("aggregate", requests, aborts.clone()).await;
    let facade: Arc<dyn LlmFacade> = Arc::new(HttpFacade::new(endpoint).unwrap());
    let stream = facade.generate(request("request-1")).await.unwrap();

    drop(abort_on_drop(facade, "request-1".into(), stream));

    tokio::time::timeout(Duration::from_secs(1), async {
        while aborts.lock().unwrap().is_empty() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(aborts.lock().unwrap().as_slice(), [["request-1"]]);
    task.abort();
}

#[tokio::test]
async fn terminal_output_disarms_backend_abort() {
    let requests = Arc::new(Mutex::new(Vec::new()));
    let aborts = Arc::new(Mutex::new(Vec::new()));
    let (endpoint, task) = server("aggregate", requests, aborts.clone()).await;
    let facade: Arc<dyn LlmFacade> = Arc::new(HttpFacade::new(endpoint).unwrap());
    let stream = facade.generate(request("request-1")).await.unwrap();
    let mut stream = abort_on_drop(facade, "request-1".into(), stream);

    assert!(
        stream
            .next()
            .await
            .unwrap()
            .unwrap()
            .finish_reason
            .is_some()
    );
    drop(stream);
    tokio::task::yield_now().await;

    assert!(aborts.lock().unwrap().is_empty());
    task.abort();
}

#[tokio::test]
async fn prefill_decode_uses_child_ids_and_streams_only_decode() {
    let prefill_requests = Arc::new(Mutex::new(Vec::new()));
    let decode_requests = Arc::new(Mutex::new(Vec::new()));
    let prefill_aborts = Arc::new(Mutex::new(Vec::new()));
    let decode_aborts = Arc::new(Mutex::new(Vec::new()));
    let (prefill, prefill_task) =
        server("prefill", prefill_requests.clone(), prefill_aborts.clone()).await;
    let (decode, decode_task) =
        server("decode", decode_requests.clone(), decode_aborts.clone()).await;
    let facade = VllmFacade::prefill_decode(prefill.clone(), decode, prefill).unwrap();

    let output = facade
        .generate(request("request-1"))
        .await
        .unwrap()
        .next()
        .await
        .unwrap()
        .unwrap();
    assert_eq!(output.request_id, "request-1/decode");
    assert_eq!(
        prefill_requests.lock().unwrap()[0]["request_id"],
        "request-1/prefill"
    );
    assert_eq!(
        decode_requests.lock().unwrap()[0]["request_id"],
        "request-1/decode"
    );
    facade.abort(&["request-1".into()]).await.unwrap();
    assert_eq!(
        prefill_aborts.lock().unwrap().as_slice(),
        [["request-1/prefill"]]
    );
    assert_eq!(
        decode_aborts.lock().unwrap().as_slice(),
        [["request-1/decode"]]
    );
    prefill_task.abort();
    decode_task.abort();
}

#[tokio::test]
async fn prefill_failure_never_submits_decode() {
    let prefill_requests = Arc::new(Mutex::new(Vec::new()));
    let decode_requests = Arc::new(Mutex::new(Vec::new()));
    let prefill_aborts = Arc::new(Mutex::new(Vec::new()));
    let decode_aborts = Arc::new(Mutex::new(Vec::new()));
    let (prefill, prefill_task) = server("failing-prefill", prefill_requests, prefill_aborts).await;
    let (decode, decode_task) =
        server("decode", decode_requests.clone(), decode_aborts.clone()).await;
    let facade = VllmFacade::prefill_decode(prefill.clone(), decode, prefill).unwrap();

    assert!(matches!(
        facade.generate(request("request-1")).await,
        Err(LlmFacadeError::Unavailable)
    ));
    assert!(decode_requests.lock().unwrap().is_empty());
    assert!(decode_aborts.lock().unwrap().is_empty());
    prefill_task.abort();
    decode_task.abort();
}

#[tokio::test]
async fn encoder_prefill_decode_forwards_opaque_descriptor_and_streams_only_decode() {
    let encoder_requests = Arc::new(Mutex::new(Vec::new()));
    let prefill_requests = Arc::new(Mutex::new(Vec::new()));
    let decode_requests = Arc::new(Mutex::new(Vec::new()));
    let (encoder, encoder_task) = server(
        "encoder",
        encoder_requests.clone(),
        Arc::new(Mutex::new(Vec::new())),
    )
    .await;
    let (prefill, prefill_task) = server(
        "prefill",
        prefill_requests.clone(),
        Arc::new(Mutex::new(Vec::new())),
    )
    .await;
    let (decode, decode_task) = server(
        "decode",
        decode_requests.clone(),
        Arc::new(Mutex::new(Vec::new())),
    )
    .await;
    let facade =
        VllmFacade::encoder_prefill_decode(encoder, prefill.clone(), decode, prefill).unwrap();
    let mut input = request("request-1");
    input.sampling_params.extra_args =
        Some([("unchanged".to_owned(), serde_json::json!({"value": 7}))].into());

    let output = facade
        .generate(input)
        .await
        .unwrap()
        .next()
        .await
        .unwrap()
        .unwrap();

    assert_eq!(output.request_id, "request-1/decode");
    assert_eq!(
        encoder_requests.lock().unwrap()[0]["request_id"],
        "request-1/encoder"
    );
    let prefill = &prefill_requests.lock().unwrap()[0];
    assert_eq!(prefill["request_id"], "request-1/prefill");
    assert_eq!(
        prefill["sampling_params"]["extra_args"]["unchanged"],
        serde_json::json!({"value": 7})
    );
    assert_eq!(
        prefill["sampling_params"]["extra_args"]["ec_transfer_params"],
        serde_json::json!({"opaque": ["do-not-parse"]})
    );
    assert_eq!(
        decode_requests.lock().unwrap()[0]["request_id"],
        "request-1/decode"
    );
    encoder_task.abort();
    prefill_task.abort();
    decode_task.abort();
}

#[tokio::test]
async fn missing_encoder_descriptor_never_submits_prefill_or_decode() {
    let encoder_aborts = Arc::new(Mutex::new(Vec::new()));
    let prefill_requests = Arc::new(Mutex::new(Vec::new()));
    let decode_requests = Arc::new(Mutex::new(Vec::new()));
    let (encoder, encoder_task) = server(
        "missing-encoder",
        Arc::new(Mutex::new(Vec::new())),
        encoder_aborts.clone(),
    )
    .await;
    let (prefill, prefill_task) = server(
        "prefill",
        prefill_requests.clone(),
        Arc::new(Mutex::new(Vec::new())),
    )
    .await;
    let (decode, decode_task) = server(
        "decode",
        decode_requests.clone(),
        Arc::new(Mutex::new(Vec::new())),
    )
    .await;
    let facade =
        VllmFacade::encoder_prefill_decode(encoder, prefill.clone(), decode, prefill).unwrap();

    assert!(matches!(
        facade.generate(request("request-1")).await,
        Err(LlmFacadeError::Protocol)
    ));
    assert!(prefill_requests.lock().unwrap().is_empty());
    assert!(decode_requests.lock().unwrap().is_empty());
    tokio::time::timeout(Duration::from_secs(1), async {
        while encoder_aborts.lock().unwrap().is_empty() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        encoder_aborts.lock().unwrap().as_slice(),
        [["request-1/encoder"]]
    );
    encoder_task.abort();
    prefill_task.abort();
    decode_task.abort();
}

#[tokio::test]
async fn epd_prefill_failure_never_submits_decode_and_aborts_started_stages() {
    let encoder_aborts = Arc::new(Mutex::new(Vec::new()));
    let prefill_aborts = Arc::new(Mutex::new(Vec::new()));
    let decode_requests = Arc::new(Mutex::new(Vec::new()));
    let (encoder, encoder_task) = server(
        "encoder",
        Arc::new(Mutex::new(Vec::new())),
        encoder_aborts.clone(),
    )
    .await;
    let (prefill, prefill_task) = server(
        "failing-prefill",
        Arc::new(Mutex::new(Vec::new())),
        prefill_aborts.clone(),
    )
    .await;
    let (decode, decode_task) = server(
        "decode",
        decode_requests.clone(),
        Arc::new(Mutex::new(Vec::new())),
    )
    .await;
    let facade =
        VllmFacade::encoder_prefill_decode(encoder, prefill.clone(), decode, prefill).unwrap();

    assert!(matches!(
        facade.generate(request("request-1")).await,
        Err(LlmFacadeError::Unavailable)
    ));
    assert!(decode_requests.lock().unwrap().is_empty());
    tokio::time::timeout(Duration::from_secs(1), async {
        while encoder_aborts.lock().unwrap().is_empty() || prefill_aborts.lock().unwrap().is_empty()
        {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        encoder_aborts.lock().unwrap().as_slice(),
        [["request-1/encoder"]]
    );
    assert_eq!(
        prefill_aborts.lock().unwrap().as_slice(),
        [["request-1/prefill"]]
    );
    encoder_task.abort();
    prefill_task.abort();
    decode_task.abort();
}

#[tokio::test]
async fn cancelling_epd_orchestration_aborts_the_started_encoder() {
    let encoder_requests = Arc::new(Mutex::new(Vec::new()));
    let encoder_aborts = Arc::new(Mutex::new(Vec::new()));
    let (encoder, encoder_task) = server(
        "hanging-encoder",
        encoder_requests.clone(),
        encoder_aborts.clone(),
    )
    .await;
    let (prefill, prefill_task) = server(
        "prefill",
        Arc::new(Mutex::new(Vec::new())),
        Arc::new(Mutex::new(Vec::new())),
    )
    .await;
    let (decode, decode_task) = server(
        "decode",
        Arc::new(Mutex::new(Vec::new())),
        Arc::new(Mutex::new(Vec::new())),
    )
    .await;
    let facade = Arc::new(
        VllmFacade::encoder_prefill_decode(encoder, prefill.clone(), decode, prefill).unwrap(),
    );

    let task = tokio::spawn({
        let facade = facade.clone();
        async move { facade.generate(request("request-1")).await }
    });
    tokio::time::timeout(Duration::from_secs(1), async {
        while encoder_requests.lock().unwrap().is_empty() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    task.abort();
    let _ = task.await;
    tokio::time::timeout(Duration::from_secs(1), async {
        while encoder_aborts.lock().unwrap().is_empty() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        encoder_aborts.lock().unwrap().as_slice(),
        [["request-1/encoder"]]
    );
    encoder_task.abort();
    prefill_task.abort();
    decode_task.abort();
}

#[tokio::test]
async fn epd_abort_covers_every_server_generated_stage_id() {
    let encoder_aborts = Arc::new(Mutex::new(Vec::new()));
    let prefill_aborts = Arc::new(Mutex::new(Vec::new()));
    let decode_aborts = Arc::new(Mutex::new(Vec::new()));
    let (encoder, encoder_task) = server(
        "encoder",
        Arc::new(Mutex::new(Vec::new())),
        encoder_aborts.clone(),
    )
    .await;
    let (prefill, prefill_task) = server(
        "prefill",
        Arc::new(Mutex::new(Vec::new())),
        prefill_aborts.clone(),
    )
    .await;
    let (decode, decode_task) = server(
        "decode",
        Arc::new(Mutex::new(Vec::new())),
        decode_aborts.clone(),
    )
    .await;
    let facade =
        VllmFacade::encoder_prefill_decode(encoder, prefill.clone(), decode, prefill).unwrap();

    facade.abort(&["request-1".into()]).await.unwrap();

    assert_eq!(
        encoder_aborts.lock().unwrap().as_slice(),
        [["request-1/encoder"]]
    );
    assert_eq!(
        prefill_aborts.lock().unwrap().as_slice(),
        [["request-1/prefill"]]
    );
    assert_eq!(
        decode_aborts.lock().unwrap().as_slice(),
        [["request-1/decode"]]
    );
    encoder_task.abort();
    prefill_task.abort();
    decode_task.abort();
}

#[tokio::test]
async fn transfer_parameters_supplied_by_a_client_fail_closed() {
    let (endpoint, task) = server(
        "aggregate",
        Arc::new(Mutex::new(Vec::new())),
        Arc::new(Mutex::new(Vec::new())),
    )
    .await;
    let facade = VllmFacade::prefill_decode(endpoint.clone(), endpoint.clone(), endpoint).unwrap();
    for key in ["ec_transfer_params", "kv_transfer_params"] {
        let mut input = request("request-1");
        input.sampling_params.extra_args = Some([(key.to_owned(), serde_json::json!({}))].into());
        assert!(matches!(
            facade.generate(input).await,
            Err(LlmFacadeError::Configuration)
        ));
    }
    task.abort();
}

#[tokio::test]
async fn prefill_must_finish_by_length_before_decode_is_submitted() {
    let prefill_requests = Arc::new(Mutex::new(Vec::new()));
    let decode_requests = Arc::new(Mutex::new(Vec::new()));
    let (prefill, prefill_task) = server(
        "stopped-prefill",
        prefill_requests,
        Arc::new(Mutex::new(Vec::new())),
    )
    .await;
    let (decode, decode_task) = server(
        "decode",
        decode_requests.clone(),
        Arc::new(Mutex::new(Vec::new())),
    )
    .await;
    let facade = VllmFacade::prefill_decode(prefill.clone(), decode, prefill).unwrap();

    assert!(matches!(
        facade.generate(request("request-1")).await,
        Err(LlmFacadeError::RequestFailed)
    ));
    assert!(decode_requests.lock().unwrap().is_empty());
    prefill_task.abort();
    decode_task.abort();
}

async fn server(
    role: &'static str,
    requests: Requests,
    aborts: Aborts,
) -> (String, tokio::task::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let app = axum::Router::new()
        .route(
            "/query",
            get(|| async { axum::Json(serde_json::json!({"0":{"engine_id":"engine-0"}})) }),
        )
        .route("/v1/internal/generate", post(generate))
        .route("/v1/internal/abort", post(abort))
        .with_state((role, requests, aborts));
    (
        format!("http://{address}"),
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() }),
    )
}

async fn generate(
    State((role, requests, _)): State<(&'static str, Requests, Aborts)>,
    body: String,
) -> axum::response::Response {
    let request: serde_json::Value = serde_json::from_str(&body).unwrap();
    let request_id = request["request_id"].as_str().unwrap().to_owned();
    requests.lock().unwrap().push(request);
    if role == "failing-prefill" {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    if role == "hanging-encoder" {
        return (
            StatusCode::OK,
            [("content-type", "application/x-ndjson")],
            Body::from_stream(async_stream::stream! {
                futures::future::pending::<()>().await;
                yield Ok::<_, std::io::Error>(axum::body::Bytes::new());
            }),
        )
            .into_response();
    }
    let finish_reason = if role == "prefill" {
        r#""Length""#
    } else {
        r#"{"Stop":null}"#
    };
    let ec_transfer_params = if role == "encoder" {
        ",\"ec_transfer_params\":{\"opaque\":[\"do-not-parse\"]}"
    } else {
        ""
    };
    (
        StatusCode::OK,
        [("content-type", "application/x-ndjson")],
        Body::from(format!(
            "{{\"type\":\"token\",\"request_id\":\"{request_id}\",\"prompt_token_ids\":null,\"prompt_logprobs\":null,\"token_ids\":[42],\"logprobs\":null,\"cached_token_count\":0,\"finish_reason\":{finish_reason}{ec_transfer_params}}}\n"
        )),
    )
        .into_response()
}

async fn abort(
    State((_, _, aborts)): State<(&'static str, Requests, Aborts)>,
    body: String,
) -> StatusCode {
    let request: serde_json::Value = serde_json::from_str(&body).unwrap();
    aborts.lock().unwrap().push(
        request["request_ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|id| id.as_str().unwrap().to_owned())
            .collect(),
    );
    StatusCode::NO_CONTENT
}
