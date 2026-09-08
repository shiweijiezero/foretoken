# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""解析性能负载使用的 Hugging Face 数据集选择器和文件 URI。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterator, Optional

if TYPE_CHECKING:
    from datasets import Dataset

_HF_DATASETS_PREFIX = "hf://datasets/"
_HF_FILE_URI_FORMAT = "hf://datasets/<org>/<repo>[@<revision>]/<path>"


def is_hf_file_uri(source: str) -> bool:
    """返回该来源是否为规范的 ``hf://datasets/...`` 文件 URI。"""
    return source.startswith(_HF_DATASETS_PREFIX)


def parse_hf_file_uri(uri: str) -> tuple[str, Optional[str], str]:
    """解析 ``hf://datasets/{repo_id}[@{revision}]/{path}``。

    返回 ``(repo_id, revision_or_none, filename)``。
    """
    if not uri.startswith(_HF_DATASETS_PREFIX):
        raise ValueError(
            f"Invalid HF file URI {uri!r}. Use {_HF_FILE_URI_FORMAT}."
        )

    remainder = uri[len(_HF_DATASETS_PREFIX) :]
    if not remainder or remainder.startswith("/"):
        raise ValueError(
            f"Invalid HF file URI {uri!r}. "
            f"Use {_HF_FILE_URI_FORMAT}."
        )

    if "@" in remainder:
        repo_id, after_at = remainder.split("@", 1)
        if not repo_id or "/" not in after_at:
            raise ValueError(
                f"Invalid HF file URI {uri!r}. "
                f"Use {_HF_FILE_URI_FORMAT}."
            )
        revision, filename = after_at.split("/", 1)
        if not revision or not filename:
            raise ValueError(
                f"Invalid HF file URI {uri!r}. "
                f"Use {_HF_FILE_URI_FORMAT}."
            )
        return repo_id, revision, filename

    parts = remainder.split("/")
    if len(parts) >= 3 and all(parts):
        repo_id = f"{parts[0]}/{parts[1]}"
        filename = "/".join(parts[2:])
        return repo_id, None, filename
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], None, parts[1]
    raise ValueError(
        f"Invalid HF file URI {uri!r}. "
        f"Use {_HF_FILE_URI_FORMAT}."
    )


def resolve_hf_file_uri(uri: str) -> str:
    """缓存 Hugging Face 数据集仓库文件并返回本地路径。"""
    from huggingface_hub import hf_hub_download

    repo_id, revision, filename = parse_hf_file_uri(uri)
    return hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        repo_type="dataset",
        revision=revision,
    )

_DEFAULT_SELECTORS = {
    "KrisQ/StudyChat": "train",
    "valeriol29/mooncake-traces": "conversation",
}


def parse_hf_dataset_spec(spec: str) -> tuple[str, str]:
    """把支持的数据集选择器解析为 ``(dataset_id, selector)``。

    selector 可以是 split 或 builder config。已知轨迹数据集具有默认值，
    其他数据集必须使用 ``org/name:split``。
    """
    if ":" not in spec:
        selector = _DEFAULT_SELECTORS.get(spec)
        if selector is not None:
            return spec, selector
        raise ValueError(
            f"Invalid Hugging Face dataset spec {spec!r}. "
            "Use 'org/name:split' (split is required)."
        )
    dataset_id, split = spec.rsplit(":", 1)
    if not dataset_id or not split:
        raise ValueError(
            f"Invalid Hugging Face dataset spec {spec!r}. "
            "Use 'org/name:split'."
        )
    return dataset_id, split


def is_hf_dataset_spec(spec: str) -> bool:
    """返回 ``spec`` 是否为支持的 Hugging Face 数据集选择器。"""
    try:
        parse_hf_dataset_spec(spec)
    except ValueError:
        return False
    return True


def same_dataset_selector(left: str, right: str) -> bool:
    """返回两个选择器是否解析到同一数据集来源。"""
    if left == right:
        return True
    try:
        return parse_hf_dataset_spec(left) == parse_hf_dataset_spec(right)
    except ValueError:
        return False


def _load_hf_data(dataset_id: str, split: str) -> Any:
    """流式读取 ``dataset_id`` 和 ``split`` 指定的数据。

    部分数据集把选择器作为 builder config，且该配置只包含 ``train`` split；
    此时 CLI 选择器表示 config 名称。
    """
    from datasets import get_dataset_config_names, get_dataset_split_names, load_dataset

    configs = get_dataset_config_names(dataset_id)
    if split in configs:
        data_splits = get_dataset_split_names(dataset_id, split)
        if len(data_splits) != 1:
            raise ValueError(
                f"Hugging Face dataset {dataset_id!r} config {split!r} has "
                f"multiple data splits {data_splits}; expected exactly one."
            )
        return load_dataset(
            dataset_id,
            name=split,
            split=data_splits[0],
            streaming=True,
        )
    return load_dataset(dataset_id, split=split, streaming=True)


def iter_hf_rows(spec: str) -> Iterator[tuple[int, Any]]:
    """按 Hugging Face 数据集选择器逐行返回索引和值。"""
    dataset_id, split = parse_hf_dataset_spec(spec)
    data = _load_hf_data(dataset_id, split)
    for row_index, row in enumerate(data):
        yield row_index, dict(row)
