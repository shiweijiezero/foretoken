// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Executes aggregate, P/D, and E/P/D generation while retaining cross-stage cleanup ownership.

use foretoken_admission::AdmissionPermit;
use foretoken_llm_facade::{
    LlmFacadeResolver, MultiStageCleanup, RouteStage, TokenStream, consume_encoder,
    consume_prefill, encoder_stage_request, inject_ec_transfer_params, pd_stage_requests,
};
use foretoken_model_protocol::ModelServerRole;
use foretoken_router::RouteDecision;

use super::{GenerationError, before_waiting_deadline};

/// Executes one selected workflow, transferring the outer reservation to each stage.
/// Only explicit nonacceptance of the initial stage may return `BackendBusy` to Admission;
/// once any stage is accepted, failure cancels that workflow rather than replaying its input.
pub(crate) async fn execute_workflow(
    resolver: &dyn LlmFacadeResolver,
    session: &mut dyn foretoken_router::RouteSession,
    initial: RouteDecision,
    request: vllm_llm::GenerateRequest,
    admission: &AdmissionPermit,
    role_priority: Option<i32>,
) -> Result<(RouteDecision, TokenStream), GenerationError> {
    match initial.role {
        ModelServerRole::Aggregate => {
            execute_aggregate(resolver, initial, request, admission, role_priority).await
        }
        ModelServerRole::Prefill => {
            execute_pd(
                resolver,
                session,
                initial,
                request,
                None,
                admission,
                role_priority,
            )
            .await
        }
        ModelServerRole::Encoder => {
            let (descriptor, cleanup) =
                execute_encoder(resolver, initial, request.clone(), admission, role_priority)
                    .await?;
            session.stage_complete();
            let prefill = session
                .select_prefill()
                .map_err(GenerationError::from)
                .map_err(after_acceptance)?;
            execute_pd(
                resolver,
                session,
                prefill,
                request,
                Some((descriptor, cleanup)),
                admission,
                role_priority,
            )
            .await
            .map_err(after_acceptance)
        }
        ModelServerRole::Decode => Err(GenerationError::Internal),
    }
}

// Accepted stage ownership is never reset by a later busy response. The caller cancels the
// complete workflow on error; the model server reports confirmed termination to the ledger.
fn after_acceptance(error: GenerationError) -> GenerationError {
    match error {
        GenerationError::BackendBusy => GenerationError::Unavailable,
        error => error,
    }
}

/// Sends one stage and advances local caller rotation only after successful backend headers.
async fn admitted_generate(
    facade: std::sync::Arc<dyn foretoken_llm_facade::LlmFacade>,
    mut request: vllm_llm::GenerateRequest,
    decision: &RouteDecision,
    admission: &AdmissionPermit,
    role_priority: Option<i32>,
    initial_stage: bool,
    cleanup: &mut MultiStageCleanup,
) -> Result<TokenStream, GenerationError> {
    request.data_parallel_rank = Some(decision.data_parallel_rank);
    let reservation = admission.reference().map(|mut reference| {
        reference.final_stage = matches!(
            decision.role,
            ModelServerRole::Aggregate | ModelServerRole::Decode
        );
        reference
    });
    let request_id = request.request_id.clone();
    // Cleanup is armed before awaiting an unresolved submission, but an explicit Busy
    // response disarms this stage so its request ID can be retried without a stale abort.
    cleanup.register(facade.clone(), request_id.clone());
    let submission = async {
        facade
            .generate(request, reservation, role_priority)
            .await
            .map_err(GenerationError::from)
    };
    let stream = if initial_stage {
        before_waiting_deadline(admission.waiting_deadline(), submission).await
    } else {
        submission.await
    };
    if matches!(stream, Err(GenerationError::BackendBusy)) {
        cleanup.rejected(&request_id);
    }
    let stream = if initial_stage {
        stream?
    } else {
        stream.map_err(after_acceptance)?
    };
    admission.accepted().await;
    Ok(stream)
}

async fn execute_aggregate(
    resolver: &dyn LlmFacadeResolver,
    decision: RouteDecision,
    request: vllm_llm::GenerateRequest,
    admission: &AdmissionPermit,
    role_priority: Option<i32>,
) -> Result<(RouteDecision, TokenStream), GenerationError> {
    let facade = resolver
        .resolve_stage(&decision, RouteStage::Aggregate)
        .ok_or(GenerationError::Internal)?;
    let mut cleanup = MultiStageCleanup::new();
    let stream = admitted_generate(
        facade,
        request,
        &decision,
        admission,
        role_priority,
        true,
        &mut cleanup,
    )
    .await?;
    Ok((decision, cleanup.with_stream(stream)))
}

// Encoder is a completion barrier. Its guard survives descriptor extraction so a failed
// downstream handoff still cleans up backend-owned intermediate resources.
async fn execute_encoder(
    resolver: &dyn LlmFacadeResolver,
    decision: RouteDecision,
    request: vllm_llm::GenerateRequest,
    admission: &AdmissionPermit,
    role_priority: Option<i32>,
) -> Result<(serde_json::Value, MultiStageCleanup), GenerationError> {
    let facade = resolver
        .resolve_stage(&decision, RouteStage::Encoder)
        .ok_or(GenerationError::Internal)?;
    let request = encoder_stage_request(request).map_err(GenerationError::from)?;
    let mut cleanup = MultiStageCleanup::new();
    let stream = admitted_generate(
        facade,
        request,
        &decision,
        admission,
        role_priority,
        true,
        &mut cleanup,
    )
    .await?;
    let descriptor = consume_encoder(stream)
        .await
        .map_err(GenerationError::from)?;
    Ok((descriptor, cleanup))
}

// Prefill completes before fresh Decode selection. All stages share one caller reservation;
// intermediate terminal output does not release that reservation before the final stage.
async fn execute_pd(
    resolver: &dyn LlmFacadeResolver,
    session: &mut dyn foretoken_router::RouteSession,
    prefill_decision: RouteDecision,
    request: vllm_llm::GenerateRequest,
    encoder: Option<(serde_json::Value, MultiStageCleanup)>,
    admission: &AdmissionPermit,
    role_priority: Option<i32>,
) -> Result<(RouteDecision, TokenStream), GenerationError> {
    let facade = resolver
        .resolve_stage(&prefill_decision, RouteStage::Prefill)
        .ok_or(GenerationError::Internal)?;
    let bootstrap = resolver
        .bootstrap_endpoint(&prefill_decision)
        .ok_or(GenerationError::Internal)?;
    let initial_stage = encoder.is_none();
    let (descriptor, mut cleanup) = match encoder {
        Some((descriptor, cleanup)) => (Some(descriptor), cleanup),
        None => (None, MultiStageCleanup::new()),
    };
    let shape = async {
        pd_stage_requests(request, &bootstrap, prefill_decision.data_parallel_rank)
            .await
            .map_err(GenerationError::from)
    };
    let (mut prefill_request, decode_request) = if initial_stage {
        before_waiting_deadline(admission.waiting_deadline(), shape).await?
    } else {
        shape.await?
    };
    if let Some(descriptor) = descriptor {
        inject_ec_transfer_params(&mut prefill_request, descriptor);
    }
    let stream = admitted_generate(
        facade,
        prefill_request,
        &prefill_decision,
        admission,
        role_priority,
        initial_stage,
        &mut cleanup,
    )
    .await?;
    consume_prefill(stream)
        .await
        .map_err(GenerationError::from)?;

    // Routing load is stage-local and ends at the barrier, unlike the outer caller capacity.
    session.stage_complete();
    let decode = session
        .select_decode()
        .map_err(GenerationError::from)
        .map_err(after_acceptance)?;
    let decode_facade = resolver
        .resolve_stage(&decode, RouteStage::Decode)
        .ok_or(GenerationError::Internal)?;
    let stream = admitted_generate(
        decode_facade,
        decode_request,
        &decode,
        admission,
        role_priority,
        false,
        &mut cleanup,
    )
    .await?;
    Ok((decode, cleanup.with_stream(stream)))
}
