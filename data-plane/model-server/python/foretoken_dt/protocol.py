# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Control messages for native Draft/Target tensor transfers and retained turns."""

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

PEER_PROTOCOL_VERSION = 6
TransferProtocol = Literal["tcp", "rdma", "nccl"]


class Message(BaseModel):
    """Validate control messages at the private peer boundary."""

    model_config = ConfigDict(extra="forbid")


class Settings(Message):
    """Select one role and its transport from the resolved engine configuration."""

    role: Literal["draft", "target"]
    protocol: TransferProtocol
    _endpoint: str | None = PrivateAttr(default=None)

    @property
    def endpoint(self) -> str | None:
        """Read the launcher's peer binding without including it in graph settings."""
        return self._endpoint

    @classmethod
    def from_engine(cls, config):
        """Combine graph settings with the launcher's process-local peer binding."""
        value = config.additional_config.get("foretoken_dt")
        if value is None:
            raise ValueError("the DT worker requires additional_config.foretoken_dt")
        result = cls.model_validate(value)
        if result.role == "target":
            result._endpoint = os.environ.get("FORETOKEN_DT_DRAFT_ENDPOINT")
            if not result._endpoint:
                raise ValueError(
                    "the DT Target requires its launcher-provided Draft endpoint"
                )
        return result


class TensorRegion(Message):
    """Describe a registered allocation retained by the sending peer."""

    session: str
    address: int = Field(gt=0)
    shape: list[int]
    dtype: str


class OpenPeer(Message):
    """Bind one Target worker's input and output capacities to a Draft peer."""

    protocol_version: int
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
    protocol: TransferProtocol
    return_probabilities: bool
    logits: TensorRegion | None = None
    nccl_ids: tuple[str, str] | None = None


class InitialDeviceRequest(Message):
    """Supply a complete known prompt before its first Target-produced anchor exists."""

    prompt_token_ids: list[int] = Field(min_length=1)
    cache_salt: str | None
    temperature: float = Field(ge=0)
    seed: int


class DeviceRequest(Message):
    """Offer a complete logical query for native resource reservation."""

    request_id: str
    query_tokens: int = Field(gt=0)
    seq_len_upper_bound: int = Field(gt=0)
    initial: InitialDeviceRequest | None = None


class PrepareBatch(Message):
    """Announce transfer shapes before Target execution produces their contents."""

    sequence: int = Field(ge=0)
    feature_rows: int = Field(ge=0)
    num_requests: int = Field(gt=0)
    requests: list[DeviceRequest] = Field(default_factory=list)


class ExecuteBatch(Message):
    """Join the native batch already admitted by a successful device-input reservation."""

    sequence: int = Field(ge=0)


class ExecuteConfirmation(Message):
    """Carry one Draft-confirmed device turn back without a second Target readback."""

    sequence: int = Field(ge=0)
    confirmed_length: int = Field(ge=0)
    context_length: int = Field(ge=0)
    token_ids_delta: list[int]
    anchor: int = Field(ge=0)


class ExecuteResult(Message):
    """Return authoritative confirmations in the same row order as ExecuteBatch."""

    sequence: int = Field(ge=0)
    confirmed: list[ExecuteConfirmation]


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
