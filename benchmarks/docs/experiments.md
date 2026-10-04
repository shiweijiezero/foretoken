<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Iterative experiments

English | [简体中文](experiments_zh.md)

Add `experiment` to `--output` to keep related measurements, code changes, and notes together. Choose an output directory for the optimization goal and the question being investigated, then name the iteration after the approach being tested.

## Measure an iteration

Run from the checkout containing the changes. These commands use the deployment prepared by the [Quick Start](../../README.md#quick-start):

```bash
foretoken perf examples/quickstart --num-prompts 20 \
  --output experiment --output-dir results/reduce-ttft/queue-aware-routing \
  --iteration baseline

foretoken eval examples/quickstart --tasks gsm8k --limit 20 \
  --output experiment --output-dir results/reduce-ttft/queue-aware-routing \
  --iteration baseline
```

Both commands belong to `baseline`, with a separate run directory for each invocation:

```text
results/reduce-ttft/queue-aware-routing/
├── experiment.md
└── iterations/
    └── baseline/
        ├── iteration.md
        └── runs/
            ├── perf-000001/
            │   ├── context.json
            │   ├── changes/
            │   └── results/
            └── eval-000001/
```

The command prints its experiment run directory. `results/` holds the usual benchmark output, including any sweep points, compared models, and captures from `perf --profile`. Existing plotting and evaluation resume commands use the benchmark result directory printed by the runner.

Repeat the command to add another run without overwriting earlier results. Use a different `--iteration` name for another approach. Omit the name to create a new numbered iteration for each command. Concurrent commands reserve different run directories. A sweep or multi-model comparison stays within one command's run.

`experiment` keeps results locally; `local` is optional. Add `wandb` or `plot` as usual. Add `quiet` to retain preparation and execution output in `run.log` while suppressing progress in the terminal. The experiment notes and checkout snapshot stay local rather than being uploaded with benchmark artifacts.

## Read and extend the record

`context.json` records the command, start and finish times, elapsed time, exit status, and checkout identity. Credential-bearing options and opaque evaluator model arguments are omitted from the saved command. Failed and interrupted executions keep their records. Source-capture time is recorded separately from the whole command; benchmark metrics retain their own measurement-window durations.

Fill in `experiment.md` with the goal and comparison method. In `iteration.md`, explain the hypothesis, the changes made, the findings, and the next decision. Both notes are created once and preserved on subsequent runs. Link the relevant result directories rather than copying their metrics.

Choose only the [experiment recipes](recipes.md) needed to answer the current question. Record time spent finding references, editing, deploying, and analyzing results; label estimates. Explain what the measurements establish and whether changed workloads, precision, caching, or hardware could account for the difference.

## Restore the checkout state

Each command records the current repository's `HEAD` and copies modified and untracked, non-ignored files into `changes/`, preserving repository-relative paths. The checkout entries in `context.json` also identify deletions, symlink targets, file modes, and initialized submodule revisions and changes. Committing or pushing between runs is fine: each run has its own revision, rather than a fixed experiment-wide base.

To reconstruct a run, check out its recorded commit in a separate checkout, apply the recorded deletions and symlinks, and overlay the saved files with their modes. The snapshot restores working-tree contents, not staging decisions. Ignored files and external engine checkouts are not copied. Keep downloaded weights and caches outside the checkout or covered by Git ignore rules.

The snapshot describes the checkout where the command was invoked, not necessarily the code serving requests. Benchmark `environment.json` separately records the client environment and, for Kubernetes targets, the observed service configuration and runtime images. Note any separately built runtime or engine checkout used by the experiment. Run outside Git and the context records that source capture is unavailable.
