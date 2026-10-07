# Qwen Decode Optimization

English | [简体中文](qwen-decode_zh.md)

Improve single-request decode speed for Qwen3.5-35B-A3B BF16 while preserving answer quality. The scope is open: investigate the inference engine, scheduling, kernels, communication, or serving configuration based on measurements.

## Prepare the comparison

Prepare a working Qwen3.5-35B-A3B Kustomize configuration using the [source deployment guide](../../docs/custom-deployment.md). Use BF16 weights. Record the GPUs, parallelism, model version, thinking mode, and generation settings, and keep them consistent across comparisons. If a condition is itself the subject of the experiment, identify the change explicitly.

Replace `MODEL_CONFIG` below with the actual configuration directory. `baseline` names the reference measurement before changes; use an approach-specific name for subsequent iterations.

```bash
MODEL_CONFIG=path/to/qwen35-deployment
EXPERIMENT=results/decode-speed/qwen35-bf16
ITERATION=baseline

foretoken perf "$MODEL_CONFIG" --dataset random \
  --min-prompt-length 128 --max-prompt-length 128 \
  --min-output-length 512 --max-output-length 512 \
  --max-concurrency 1 --num-prompts 8 --warmup-requests 2 \
  --random-seed 0 --temperature 0 \
  --output experiment --output-dir "$EXPERIMENT" \
  --iteration "$ITERATION"
```

The workload uses 128 input tokens, 512 output tokens, and one concurrent request to measure decode performance. [Fixed output lengths](../../benchmarks/docs/perf/random.md) require the service to support `min_tokens` and `ignore_eos` and report output usage. Adjust the input length for the target scenario, keeping the workload identical within a comparison.

## Analyze and design

Start with time per output token (TPOT), request latency, actual output length, and successful request count. To locate a bottleneck, run a separate [profile](../../benchmarks/docs/profile/README.md) and examine computation, communication, and waiting time. Profiling adds overhead; compare speed using runs without profiling.

Form a hypothesis from the evidence, such as whether reducing a communication wait could lower TPOT. Design the change and its evaluation without assuming that the engine or router must be modified. Redeploy the change, set `ITERATION` to the approach name, and repeat the same workload above.

## Check answer quality

Use random fixed-length requests for speed comparisons and real tasks for answer quality. Run GSM8K before and after the change with the same model and thinking settings:

```bash
foretoken eval "$MODEL_CONFIG" --tasks gsm8k --limit 20 --log_samples \
  --output experiment --output-dir "$EXPERIMENT" \
  --iteration "$ITERATION"
```

Allow sufficient generation budget for the model's thinking; see [quality evaluation](../../benchmarks/docs/eval/README.md) for parameters. Inspect final answers and finish reasons, distinguishing budget exhaustion, answer-format mismatches, and incorrect answers. A small sample helps find problems quickly; expand the evaluation when stronger evidence is needed.

## After each run

Find the run under `$EXPERIMENT/iterations/$ITERATION/runs/` and follow the [result inspection order](../templates/experiments.md#inspect-the-results) to review its status, configuration, metrics, and raw outputs.

The developer or agent edits that iteration's `notes/iteration.md` with the hypothesis, actual changes, TPOT comparison, quality results, and conclusion, linking the run evidence. If faster responses also have shorter outputs or different thinking settings, explain those differences rather than attributing the gain directly to the implementation.

At the end of the iteration, update `$EXPERIMENT/notes/experiment.md` and decide whether to retain, refine, or revert the change. If differences remain unexplained, identify the next measurement needed. See [experiment records](../templates/experiments.md#at-the-end-of-each-iteration) for recording and resource cleanup.
