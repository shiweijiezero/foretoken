# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright 2024 DistServe Authors
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare DistServe's sampled ShareGPT workload as tokenized Completions requests.

Adapted from LLMServe/DistServe evaluation/2-benchmark-serving/0-prepare-dataset.py
and 2-benchmark-serving.py. This adapter preserves prefix selection, tokenization,
filtering, and sampling, and writes JSONL instead of the upstream binary format.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from huggingface_hub import hf_hub_download
from transformers import AutoTokenizer


def main() -> None:
    """Download or read ShareGPT, apply the artifact protocol, and write sampled requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="facebook/opt-13b", help="Served model name; also the default tokenizer repository")
    parser.add_argument("--tokenizer", help="Tokenizer repository or local directory, if different from --model")
    parser.add_argument("--dataset-path", type=Path, help="Local ShareGPT_V3_unfiltered_cleaned_split.json; downloaded when omitted")
    parser.add_argument("--num-prompts", type=int, default=300)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path, default=Path("results/datasets/distserve-sharegpt.jsonl"))
    args = parser.parse_args()
    if args.num_prompts <= 0:
        parser.error("--num-prompts must be positive")

    source = args.dataset_path or Path(hf_hub_download(
        repo_id="anon8231489123/ShareGPT_Vicuna_unfiltered",
        filename="ShareGPT_V3_unfiltered_cleaned_split.json",
        repo_type="dataset",
    ))
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer or args.model)
    conversations = json.loads(source.read_text(encoding="utf-8"))
    generator = random.Random(args.seed)
    candidates = []
    for row in conversations:
        messages = row["conversations"]
        if len(messages) < 3:
            continue
        prefix_length = generator.randint(1, min(len(messages) - 1, 1000))
        prompt = "\n".join(message["value"] for message in messages[:prefix_length])
        prompt_ids = tokenizer(prompt).input_ids
        output_length = len(tokenizer(messages[prefix_length]["value"]).input_ids)
        # Preserve the artifact's AND predicate and OPT context bound exactly.
        if len(prompt_ids) < 4 and output_length < 4:
            continue
        if len(prompt_ids) + output_length >= 2048:
            continue
        candidates.append({
            "prompt": prompt_ids,
            "output_length": output_length,
            "model": args.model,
        })

    # The upstream client resets its seed independently from dataset preparation.
    requests = random.Random(args.seed).sample(candidates, args.num_prompts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output:
        for request in requests:
            output.write(json.dumps(request, separators=(",", ":")) + "\n")
    print(f"Prepared {len(requests)} requests from {len(candidates)} eligible samples: {args.output}")


if __name__ == "__main__":
    main()
