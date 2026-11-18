"""As-of on-site inventory reconstruction.

A sample's position at an instant is reconstructed from the transfer batches it
rode, in chronological order.  The model is deliberately conservative:

* before the first batch departs, no position is inferred at all;
* from ``departed_at`` (inclusive) until ``arrived_at`` (exclusive) the sample
  is in transit and therefore **not** in any station's storage;
* from ``arrived_at`` (inclusive) until the next ``departed_at`` (exclusive) the
  sample is in storage at that batch's destination;
* after the last ``arrived_at`` the sample remains in storage at the last
  destination.

A sample whose timeline is internally inconsistent cannot be located safely.
Two inconsistencies are reported as conflicts:

``overlapping_transits``
    Consecutive transfers overlap in time (the later one departs before the
    earlier one arrives).

``disconnected_route``
    Consecutive transfers do not share a station (the later batch's origin is
    not the earlier batch's destination).

A conflicting sample is never included in the in-stock roster, even if the
instant happens to fall in a seemingly usable part of its timeline.

All comparisons use timezone-aware UTC datetimes; the caller supplies one
consistent snapshot of sample associations and batch versions.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping


class InventoryFailure(Exception):
    """The snapshot is malformed and a trustworthy roster is impossible."""


def _parse(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _batch_ref(leg: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "batch_no": leg["batch_no"],
        "version": int(leg["version"]),
        "origin": leg["origin"],
        "destination": leg["destination"],
        "departed_at": leg["departed_at"],
        "arrived_at": leg["arrived_at"],
    }


def _conflict(
    code: str,
    message: str,
    earlier: Mapping[str, Any],
    later: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "batch_nos": [earlier["batch_no"], later["batch_no"]],
        "batches": [_batch_ref(earlier), _batch_ref(later)],
    }


def _prepare_batch(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        departed = _parse(row["departed_at"])
        arrived = _parse(row["arrived_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InventoryFailure(
            f"batch {row.get('batch_no')!r} has an invalid timestamp"
        ) from exc
    if departed >= arrived:
        raise InventoryFailure(
            f"batch {row.get('batch_no')!r} does not arrive after departure"
        )
    return {
        "batch_no": row["batch_no"],
        "version": int(row["version"]),
        "origin": row["origin"],
        "destination": row["destination"],
        "departed_at": departed,
        "arrived_at": arrived,
    }


def _conflicts_for(legs: list[dict[str, Any]], sample_id: str) -> list[dict[str, Any]]:
    """All ordering/connectivity conflicts in one sample's timeline."""
    conflicts: list[dict[str, Any]] = []
    for earlier, later in zip(legs, legs[1:]):
        if later["departed_at"] < earlier["arrived_at"]:
            conflicts.append(
                _conflict(
                    "overlapping_transits",
                    (
                        f"sample {sample_id!r}: batch {later['batch_no']!r} departs"
                        f" at {later['departed_at'].isoformat()} before batch"
                        f" {earlier['batch_no']!r} arrives at"
                        f" {earlier['arrived_at'].isoformat()}"
                    ),
                    earlier,
                    later,
                )
            )
        if later["origin"] != earlier["destination"]:
            conflicts.append(
                _conflict(
                    "disconnected_route",
                    (
                        f"sample {sample_id!r}: batch {later['batch_no']!r} starts at"
                        f" {later['origin']!r}, but batch {earlier['batch_no']!r} ends"
                        f" at {earlier['destination']!r}"
                    ),
                    earlier,
                    later,
                )
            )
    return conflicts


def _locate(
    legs: list[dict[str, Any]], as_of: datetime
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(arrival_leg, next_departure_leg)`` for a valid timeline.

    ``arrival_leg`` is the batch whose arrival put the sample into storage;
    ``next_departure_leg`` is the batch that will take it out again, or ``None``
    when the sample is after its last arrival.  ``(None, None)`` means the
    sample has not yet departed on its first transfer or is in transit.
    """
    if as_of < legs[0]["departed_at"]:
        return None, None
    for index, leg in enumerate(legs):
        if as_of < leg["departed_at"]:
            # The sample arrived on the previous batch and is waiting here.
            return legs[index - 1], leg
        if as_of < leg["arrived_at"]:
            # In transit: not in any station's storage.
            return None, leg
    return legs[-1], None


def compute(
    *,
    site: str,
    as_of: datetime,
    associations: Mapping[str, Iterable[str]],
    batches: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Reconstruct which samples are in storage at ``site`` at ``as_of``.

    Args:
        site: station name; compared exactly with batch origins/destinations.
        as_of: aware UTC instant.
        associations: pinned ``sample_id -> [batch_no, ...]`` mapping.
        batches: the pinned batch records (at least every referenced batch).

    Returns:
        ``{"in_stock_samples": [...], "conflicting_samples": [...]}``.
        Results are ordered deterministically by ``(in_stock_since, sample_id)``
        and by ``sample_id`` for conflicts.

    Raises:
        InventoryFailure: a referenced batch is missing, malformed, or has a
            non-positive transit duration.
    """
    legs_by_sample: dict[str, list[dict[str, Any]]] = {}

    batch_by_no: dict[str, dict[str, Any]] = {}
    for row in batches:
        prepared = _prepare_batch(row)
        batch_no = prepared["batch_no"]
        if batch_no in batch_by_no:
            raise InventoryFailure(f"duplicate batch {batch_no!r} in snapshot")
        batch_by_no[batch_no] = prepared

    for sample_id, batch_nos in associations.items():
        seen: set[str] = set()
        sample_legs: list[dict[str, Any]] = []
        for batch_no in batch_nos:
            if batch_no in seen:
                raise InventoryFailure(
                    f"sample {sample_id!r} references batch {batch_no!r} twice"
                )
            seen.add(batch_no)
            leg = batch_by_no.get(batch_no)
            if leg is None:
                raise InventoryFailure(
                    f"sample {sample_id!r} references unknown batch {batch_no!r}"
                )
            sample_legs.append(leg)
        if sample_legs:
            sample_legs.sort(
                key=lambda leg: (
                    leg["departed_at"],
                    leg["arrived_at"],
                    leg["batch_no"],
                )
            )
            legs_by_sample[sample_id] = sample_legs

    in_stock: list[dict[str, Any]] = []
    conflicting: list[dict[str, Any]] = []

    for sample_id in sorted(legs_by_sample):
        legs = legs_by_sample[sample_id]
        conflicts = _conflicts_for(legs, sample_id)
        if conflicts:
            conflicting.append(
                {"sample_id": sample_id, "conflicts": conflicts}
            )
            continue

        arrival, next_departure = _locate(legs, as_of)
        if arrival is None or arrival["destination"] != site:
            continue

        supporting = [{**_batch_ref(arrival), "role": "arrival"}]
        if next_departure is not None:
            supporting.append(
                {**_batch_ref(next_departure), "role": "next_departure"}
            )
        in_stock.append(
            {
                "sample_id": sample_id,
                "in_stock_since": arrival["arrived_at"],
                "in_stock_until": (
                    next_departure["departed_at"]
                    if next_departure is not None
                    else None
                ),
                "supporting_batches": supporting,
            }
        )

    in_stock.sort(key=lambda item: (item["in_stock_since"], item["sample_id"]))
    return {
        "in_stock_samples": in_stock,
        "conflicting_samples": conflicting,
    }
