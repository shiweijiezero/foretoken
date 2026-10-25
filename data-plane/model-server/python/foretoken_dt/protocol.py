# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Control messages for draft peers; tensor bytes remain in Mooncake buffers."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    """Validate control messages at the private peer boundary."""

    model_config = ConfigDict(extra="forbid")


class Settings(Message):
    """Select one role and its transport from the resolved engine configuration."""

    role: Literal["draft", "target"]
    endpoint: str | None = None
    protocol: Literal["tcp", "rdma"] = "tcp"

    @classmethod
    def from_engine(cls, config):
        """Read the controller-selected DT configuration for a native worker."""
        value = config.additional_config.get("foretoken_dt")
        if value is None:
            raise ValueError("the DT worker requires additional_config.foretoken_dt")
        result = cls.model_validate(value)
        if result.role == "target" and result.endpoint is None:
            raise ValueError("the DT Target requires a Draft endpoint")
        return result


class TensorRegion(Message):
    """Describe a registered allocation retained by the sending peer."""

    session: str
    address: int = Field(gt=0)
    shape: list[int]
    dtype: str


class OpenPeer(Message):
    """Bind one Target worker's input and output capacities to a Draft peer."""

    peer_id: str
    method: str
    vocab_size: int = Field(gt=0)
    hidden_size: int = Field(gt=0)
    num_features: int = Field(ge=0)
    max_tokens: int = Field(gt=0)
    max_model_len: int = Field(gt=0)
    max_requests: int = Field(gt=0)
    num_candidates: int = Field(gt=0)
    feature_dtype: Literal["float16", "bfloat16", "float32"]
    protocol: Literal["tcp", "rdma"]
    logits: TensorRegion | None = None


class ProposalRequest(Message):
    """Supply one request's confirmed prefix and new feature interval."""

    request_id: str
    cache_salt: str | None
    generation: int = Field(ge=0)
    base_length: int = Field(ge=0)
    token_ids: list[int]
    context_start: int = Field(ge=0)
    feature_offset: int = Field(ge=0)
    feature_count: int = Field(ge=0)
    temperature: float = Field(ge=0)
    seed: int
    generate: bool


class ProposeBatch(Message):
    """Consume one completed input-buffer write and return one native proposal batch."""

    sequence: int = Field(ge=0)
    requests: list[ProposalRequest]


class FinishRequests(Message):
    """Close the specified request sessions while retaining the worker connection."""

    request_ids: list[str]
