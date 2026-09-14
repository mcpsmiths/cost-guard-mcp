from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

Engine = Literal["bigquery", "snowflake", "databricks"]


class AccuracyTier(str, Enum):
    PRECISE = "PRECISE"
    UPPER_BOUND = "UPPER_BOUND"
    HEURISTIC = "HEURISTIC"


@dataclass(frozen=True)
class HistoricalRuntimeSignal:
    """A warehouse-history-derived runtime signal for an exact-text SQL match, used to
    calibrate estimate_query_cost's runtime assumption in place of the coarse byte-size-tier
    heuristic. Engine-agnostic and intentionally tiny so every engine's lookup function
    returns the same shape."""

    avg_runtime_hours: float
    sample_count: int


class CostEstimate(BaseModel):
    engine: Engine
    accuracy_tier: AccuracyTier
    estimated_bytes: int | None = None
    estimated_cost_usd: float | None = None
    currency: str = "USD"
    caveats: list[str] = Field(default_factory=list)


class EngineCapabilities(BaseModel):
    engine: Engine
    supports_precise_bytes: bool
    supports_dollar_estimate: bool
    default_accuracy_tier: AccuracyTier
    known_gaps: list[str] = Field(default_factory=list)


class RefusalReason(str, Enum):
    COST_CAP_EXCEEDED = "cost_cap_exceeded"
    BYTE_CAP_EXCEEDED = "byte_cap_exceeded"
    ROW_CAP_EXCEEDED = "row_cap_exceeded"


class BoundedQueryResult(BaseModel):
    status: Literal["ok", "refused"]
    reason: RefusalReason | None = None
    estimate: CostEstimate | None = None
    hint: str | None = None
    rows: list[dict] | None = None
    row_count: int | None = None


class CredentialCheckResult(BaseModel):
    engine: Engine
    ok: bool
    detail: str
