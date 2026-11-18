"""Request/response schemas.

All timestamps are UTC. Inputs may use ``Z`` or any explicit offset; offsets are
normalised to UTC. Naive timestamps are interpreted as UTC for convenience.
Responses always use ``Z``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UTC = ZoneInfo("UTC")

#: A transfer may leave a site no later than 24 hours after contaminated
#: material arrived there.
TRANSFER_WINDOW = timedelta(hours=24)

EXAMPLE_BATCH = {
    "batch_no": "B-1001",
    "origin": "Collection-Lab",
    "destination": "Hub-North",
    "departed_at": "2026-01-01T08:00:00Z",
    "arrived_at": "2026-01-01T10:00:00Z",
}
EXAMPLE_INTAKE = {
    "source_sample_ids": ["S-001"],
    "samples": [
        {"sample_id": "S-001", "batch_nos": ["B-1001", "B-1002"]},
        {"sample_id": "S-002", "batch_nos": ["B-1003"]},
        {"sample_id": "S-003", "batch_nos": []},
    ],
    "batches": [
        EXAMPLE_BATCH,
        {
            "batch_no": "B-1002",
            "origin": "Hub-North",
            "destination": "Reference-Lab",
            "departed_at": "2026-01-01T11:00:00Z",
            "arrived_at": "2026-01-01T15:00:00Z",
        },
        {
            "batch_no": "B-1003",
            "origin": "Reference-Lab",
            "destination": "Archive",
            "departed_at": "2026-01-01T16:00:00Z",
            "arrived_at": "2026-01-01T18:00:00Z",
        },
    ],
}


def _to_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        # The API contract is UTC; a naive value is taken at face value.
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clean(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be blank")
    return cleaned


class BatchIn(BaseModel):
    """A transfer batch: one physical shipment between two sites."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"example": EXAMPLE_BATCH}
    )

    batch_no: str = Field(max_length=128)
    origin: str = Field(max_length=256)
    destination: str = Field(max_length=256)
    departed_at: datetime = Field(
        description="Departure (out of storage) time from the origin."
    )
    arrived_at: datetime = Field(
        description="Arrival (into storage) time at the destination."
    )

    _normalise_strings = field_validator("batch_no", "origin", "destination")(
        lambda v, info: _clean(v, info.field_name)
    )

    @field_validator("departed_at", "arrived_at")
    @classmethod
    def _aware(cls, v: datetime, info) -> datetime:
        return _to_utc(v, info.field_name)

    @model_validator(mode="after")
    def _order(self) -> "BatchIn":
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.departed_at >= self.arrived_at:
            raise ValueError("departed_at must be earlier than arrived_at")
        return self

    def signature(self) -> tuple[str, str, datetime, datetime]:
        return (self.origin, self.destination, self.departed_at, self.arrived_at)


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(max_length=128)
    contaminated: bool = Field(
        default=False,
        description="Contamination source. May also be set via source_sample_ids.",
    )
    batch_nos: list[str] = Field(
        default_factory=list,
        description="Batch numbers (global/hoisted batches) this sample rode on.",
    )
    batches: list[BatchIn] = Field(
        default_factory=list,
        description="Inline batch definitions, an alternative to top-level batches.",
    )

    _normalise_id = field_validator("sample_id")(
        lambda v, info: _clean(v, info.field_name)
    )

    @model_validator(mode="after")
    def _no_duplicates(self) -> "SampleIn":
        if len(set(self.batch_nos)) != len(self.batch_nos):
            raise ValueError(
                f"sample {self.sample_id!r} references a batch more than once"
            )
        inline = [b.batch_no for b in self.batches]
        if len(set(inline)) != len(inline):
            raise ValueError(
                f"sample {self.sample_id!r} defines a batch more than once"
            )
        overlap = sorted(set(inline) & set(self.batch_nos))
        if overlap:
            raise ValueError(
                f"sample {self.sample_id!r} defines and references batch(es)"
                f" {', '.join(overlap)}"
            )
        return self


class IntakeRequest(BaseModel):
    """Register samples and transfer batches. The update is atomic and additive.

    Re-sending an identical record is idempotent. Re-sending a batch with
    different values is rejected with 409: use the correction endpoint instead.
    """

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"example": EXAMPLE_INTAKE}
    )

    samples: list[SampleIn] = Field(min_length=1)
    batches: list[BatchIn] = Field(default_factory=list)
    source_sample_ids: list[str] = Field(
        default_factory=list,
        description="Samples that are contamination sources for this intake.",
    )

    @field_validator("source_sample_ids")
    @classmethod
    def _source_ids(cls, v: list[str]) -> list[str]:
        cleaned = [_clean(item, "source_sample_ids") for item in v]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("duplicate source_sample_ids")
        return cleaned

    @model_validator(mode="after")
    def _cross_checks(self) -> "IntakeRequest":
        sample_ids = [s.sample_id for s in self.samples]
        if len(set(sample_ids)) != len(sample_ids):
            raise ValueError("duplicate sample_id in samples")

        declared: dict[str, BatchIn] = {}
        for batch in self.batches:
            if batch.batch_no in declared:
                raise ValueError(f"duplicate batch_no {batch.batch_no!r}")
            declared[batch.batch_no] = batch
        for sample in self.samples:
            for batch in sample.batches:
                existing = declared.get(batch.batch_no)
                if existing is None:
                    declared[batch.batch_no] = batch
                elif existing.signature() != batch.signature():
                    raise ValueError(
                        f"batch {batch.batch_no!r} is defined inconsistently"
                        " across samples"
                    )
        unknown_sources = sorted(set(self.source_sample_ids) - set(sample_ids))
        if unknown_sources:
            raise ValueError(
                "source_sample_ids references unknown sample(s):"
                f" {', '.join(unknown_sources)}"
            )
        return self

    def normalised(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """Flatten inline batches and merge source flags."""
        batches: dict[str, BatchIn] = {}
        for batch in self.batches:
            batches[batch.batch_no] = batch
        for sample in self.samples:
            for batch in sample.batches:
                batches[batch.batch_no] = batch

        explicit_sources = set(self.source_sample_ids)
        samples = []
        for sample in self.samples:
            samples.append(
                {
                    "sample_id": sample.sample_id,
                    "contaminated": sample.contaminated
                    or sample.sample_id in explicit_sources,
                    "batch_nos": list(sample.batch_nos)
                    + [b.batch_no for b in sample.batches],
                }
            )
        return (
            samples,
            [
                {
                    "batch_no": batch.batch_no,
                    "origin": batch.origin,
                    "destination": batch.destination,
                    "departed_at": batch.departed_at,
                    "arrived_at": batch.arrived_at,
                }
                for batch in batches.values()
            ],
            sorted(explicit_sources),
        )


class BatchCorrection(BaseModel):
    """Replacement values for a batch. ``seen_version`` is the optimistic lock."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "seen_version": 1,
                "origin": "Collection-Lab",
                "destination": "Hub-South",
                "departed_at": "2026-01-01T08:00:00Z",
                "arrived_at": "2026-01-01T09:30:00Z",
            }
        },
    )

    seen_version: int = Field(
        ge=1,
        description="Version of the batch record the client read. Must match.",
    )
    origin: str = Field(max_length=256)
    destination: str = Field(max_length=256)
    departed_at: datetime
    arrived_at: datetime

    _normalise_strings = field_validator("origin", "destination")(
        lambda v, info: _clean(v, info.field_name)
    )

    @field_validator("departed_at", "arrived_at")
    @classmethod
    def _aware(cls, v: datetime, info) -> datetime:
        return _to_utc(v, info.field_name)

    @model_validator(mode="after")
    def _order(self) -> "BatchCorrection":
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.departed_at >= self.arrived_at:
            raise ValueError("departed_at must be earlier than arrived_at")
        return self


class TraceRequest(BaseModel):
    """Optional trace parameters. Sources default to samples flagged at intake."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"source_sample_ids": ["S-001"]}},
    )

    source_sample_ids: list[str] | None = Field(
        default=None,
        description=(
            "Override contamination sources for this trace. Omit to use every"
            " sample flagged contaminated by the intake."
        ),
    )

    @field_validator("source_sample_ids")
    @classmethod
    def _ids(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned = [_clean(item, "source_sample_ids") for item in v]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("duplicate source_sample_ids")
        return cleaned


class PathLeg(BaseModel):
    """One shipment in the contamination path, pinned to its version."""

    batch_no: str
    origin: str
    destination: str
    departed_at: datetime
    arrived_at: datetime
    version: int


class AffectedSample(BaseModel):
    sample_id: str
    first_arrival_at: datetime = Field(
        description="Earliest arrival of contaminated material with this sample."
    )
    quarantine_until: datetime = Field(
        description="Isolation deadline: first_arrival_at + 24 hours."
    )
    path: list[PathLeg] = Field(
        description="Complete batch chain from a source-carrying batch."
    )


class BatchOut(BaseModel):
    """A live batch record including its current version."""

    batch_no: str
    version: int
    origin: str
    destination: str
    departed_at: datetime
    arrived_at: datetime

    @field_validator("departed_at", "arrived_at", mode="before")
    @classmethod
    def _utc(cls, v: Any, info) -> Any:
        return _to_utc(v, info.field_name) if isinstance(v, datetime) else v


class SampleOut(BaseModel):
    sample_id: str
    contaminated: bool
    batch_nos: list[str]


class SnapshotInfo(BaseModel):
    intake_id: int
    accepted_at: datetime
    batch_versions: dict[str, int] = Field(
        description="Batch numbers and versions fixed by this trace."
    )
    batches: list[BatchOut] = Field(
        default_factory=list,
        description="The exact batch records used to compute this trace.",
    )


class TraceResponse(BaseModel):
    trace_id: int
    status: str
    has_sources: bool
    source_sample_ids: list[str]
    affected_samples: list[AffectedSample]
    affected_count: int
    snapshot: SnapshotInfo
    error: str | None = None


class InvalidationInfo(BaseModel):
    batch_no: str
    version: int
    invalidated_at: datetime
    reason: str


class TraceSummary(BaseModel):
    trace_id: int
    status: str
    accepted_at: datetime
    completed_at: datetime | None = None
    source_sample_ids: list[str]
    affected_count: int
    invalidated: bool


class TraceListResponse(BaseModel):
    traces: list[TraceSummary]


class IntakeResponse(BaseModel):
    intake_id: int
    created_at: datetime
    sample_count: int
    batch_count: int
    source_sample_ids: list[str]
    inserted_batches: int
    inserted_samples: int


class CorrectionResponse(BaseModel):
    batch_no: str
    previous_version: int
    version: int
    origin: str
    destination: str
    departed_at: datetime
    arrived_at: datetime
    invalidated_trace_ids: list[int]
    invalidated_inventory_query_ids: list[int] = Field(
        default_factory=list,
        description="Inventory checks invalidated by this correction.",
    )


class DatasetResponse(BaseModel):
    intake_id: int
    created_at: datetime
    samples: list[SampleOut]
    batches: list[BatchOut]
    source_sample_ids: list[str]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


# ------------------------------------------------------- as-of inventory ----


class InventoryRequest(BaseModel):
    """Which station to check, and at which UTC instant."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "site": "Reference-Lab",
                "as_of": "2026-01-02T11:00:00Z",
            }
        },
    )

    site: str = Field(
        max_length=256,
        description="Station name, matched exactly against batch origins/destinations.",
    )
    as_of: datetime = Field(
        description=(
            "UTC instant to reconstruct. Offsets are normalised to UTC; a naive"
            " value is interpreted as UTC."
        )
    )

    _normalise_site = field_validator("site")(
        lambda v, info: _clean(v, info.field_name)
    )

    @field_validator("as_of")
    @classmethod
    def _aware(cls, v: datetime, info) -> datetime:
        return _to_utc(v, info.field_name)


class InventoryBatchRef(BaseModel):
    """A batch at the version pinned by an inventory query."""

    batch_no: str
    version: int
    origin: str
    destination: str
    departed_at: datetime
    arrived_at: datetime


class SupportingBatch(InventoryBatchRef):
    role: str = Field(
        description=(
            "'arrival' is the batch that brought the sample into storage;"
            " 'next_departure' is the batch that will take it out again."
        )
    )


class BatchConflict(BaseModel):
    code: str = Field(
        description=(
            "'overlapping_transits' (the later transfer departs before the"
            " earlier one arrives) or 'disconnected_route' (consecutive"
            " transfers do not share a station)."
        )
    )
    message: str
    batch_nos: list[str]
    batches: list[InventoryBatchRef]


class InStockSample(BaseModel):
    sample_id: str
    in_stock_since: datetime = Field(
        description="Arrival at this site that started the current storage stay."
    )
    in_stock_until: datetime | None = Field(
        description=(
            "Departure of the next transfer, or null when no further transfer"
            " is recorded and the sample remains here."
        )
    )
    supporting_batches: list[SupportingBatch]


class ConflictingSample(BaseModel):
    sample_id: str
    conflicts: list[BatchConflict]


class InventorySnapshotInfo(BaseModel):
    intake_id: int
    accepted_at: datetime
    associations: dict[str, list[str]] = Field(
        description="Pinned sample -> batch associations used by this query."
    )
    batch_versions: dict[str, int] = Field(
        description="Versions of every batch referenced by the pinned associations."
    )
    batches: list[InventoryBatchRef] = Field(
        default_factory=list,
        description="The exact batch records used to compute this query.",
    )


class InventoryResponse(BaseModel):
    query_id: int
    site: str
    as_of: datetime
    status: str
    in_stock_samples: list[InStockSample]
    conflicting_samples: list[ConflictingSample]
    in_stock_count: int
    conflict_count: int
    snapshot: InventorySnapshotInfo
    error: str | None = None


class InventorySummary(BaseModel):
    query_id: int
    site: str
    as_of: datetime
    status: str
    accepted_at: datetime
    completed_at: datetime | None = None
    in_stock_count: int
    conflict_count: int
    invalidated: bool


class InventoryListResponse(BaseModel):
    queries: list[InventorySummary]
