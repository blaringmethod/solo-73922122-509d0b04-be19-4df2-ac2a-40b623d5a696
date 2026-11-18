"""Unit tests for the as-of inventory computation (no HTTP layer)."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from app import inventory

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def leg(no, origin, dest, departed, arrived, version=1):
    return {
        "batch_no": no,
        "version": version,
        "origin": origin,
        "destination": dest,
        "departed_at": departed,
        "arrived_at": arrived,
    }


def reference_in_stock(site, as_of, associations, batches):
    """Independent interval-based reference.

    Builds the storage stays for a valid timeline as ``(arrival, next departure)``
    pairs and checks the half-open containment ``arrival <= as_of < departure``.
    Samples with any ordering/connectivity conflict are excluded up front.
    """
    by_no = {b["batch_no"]: b for b in batches}
    out = []
    for sample_id, batch_nos in associations.items():
        legs = sorted(
            (by_no[no] for no in batch_nos),
            key=lambda item: (item["departed_at"], item["arrived_at"], item["batch_no"]),
        )
        valid = True
        for earlier, later in zip(legs, legs[1:]):
            if later["departed_at"] < earlier["arrived_at"]:
                valid = False
            if later["origin"] != earlier["destination"]:
                valid = False
        if not valid or not legs:
            continue
        if as_of < legs[0]["departed_at"]:
            continue
        stays = []
        for index, current in enumerate(legs):
            next_departure = (
                legs[index + 1]["departed_at"] if index + 1 < len(legs) else None
            )
            stays.append((current, next_departure))
        for arrival, next_departure in stays:
            if arrival["destination"] != site:
                continue
            if as_of < arrival["arrived_at"]:
                continue
            if next_departure is not None and as_of >= next_departure:
                continue
            out.append(sample_id)
            break
    return sorted(out)


def test_single_batch_interval_boundaries():
    batches = [leg("B1", "A", "H", BASE, BASE + timedelta(hours=2))]
    for offset, expected in [
        (-1, []),
        (0, []),            # departed: in transit, not in storage anywhere
        (1, []),
        (2, ["S1"]),        # arrived: in storage
        (48, ["S1"]),       # last arrival persists forever
    ]:
        result = inventory.compute(
            site="H" if offset else "A",
            as_of=BASE + timedelta(hours=offset),
            associations={"S1": ["B1"]},
            batches=batches,
        )
        assert [s["sample_id"] for s in result["in_stock_samples"]] == expected
        assert result["conflicting_samples"] == []


def test_missing_referenced_batch_fails():
    with pytest.raises(inventory.InventoryFailure):
        inventory.compute(
            site="H",
            as_of=BASE,
            associations={"S1": ["NOPE"]},
            batches=[],
        )


def test_duplicate_association_fails():
    batches = [leg("B1", "A", "H", BASE, BASE + timedelta(hours=1))]
    with pytest.raises(inventory.InventoryFailure):
        inventory.compute(
            site="H",
            as_of=BASE,
            associations={"S1": ["B1", "B1"]},
            batches=batches,
        )


def test_malformed_batch_fails():
    with pytest.raises(inventory.InventoryFailure):
        inventory.compute(
            site="H",
            as_of=BASE,
            associations={"S1": ["B1"]},
            batches=[leg("B1", "A", "H", BASE, BASE)],
        )


def test_conflict_details_include_pinned_versions():
    batches = [
        leg("B1", "A", "H", BASE, BASE + timedelta(hours=5), version=3),
        leg("B2", "H", "R", BASE + timedelta(hours=4), BASE + timedelta(hours=6), version=2),
    ]
    result = inventory.compute(
        site="H",
        as_of=BASE + timedelta(hours=4, minutes=30),
        associations={"S1": ["B1", "B2"]},
        batches=batches,
    )
    assert result["in_stock_samples"] == []
    conflict = result["conflicting_samples"][0]["conflicts"][0]
    assert conflict["code"] == "overlapping_transits"
    assert [(b["batch_no"], b["version"]) for b in conflict["batches"]] == [
        ("B1", 3),
        ("B2", 2),
    ]


def test_disconnected_but_non_overlapping_route_conflict():
    batches = [
        leg("B1", "A", "H", BASE, BASE + timedelta(hours=1)),
        leg("B2", "X", "R", BASE + timedelta(hours=2), BASE + timedelta(hours=3)),
    ]
    result = inventory.compute(
        site="R",
        as_of=BASE + timedelta(hours=5),
        associations={"S1": ["B1", "B2"]},
        batches=batches,
    )
    assert result["in_stock_samples"] == []
    assert [c["code"] for c in result["conflicting_samples"][0]["conflicts"]] == [
        "disconnected_route"
    ]


def test_ordering_is_deterministic_and_independent_of_input_order():
    batches = [
        leg("B1", "A", "H", BASE, BASE + timedelta(hours=1)),
        leg("B2", "H", "R", BASE + timedelta(hours=2), BASE + timedelta(hours=3)),
        leg("B3", "A", "H", BASE, BASE + timedelta(hours=1)),
        leg("B4", "H", "R", BASE + timedelta(hours=2), BASE + timedelta(hours=3)),
    ]
    associations = {"S2": ["B1", "B2"], "S1": ["B3", "B4"], "S3": ["B1"]}
    output = inventory.compute(
        site="H",
        as_of=BASE + timedelta(hours=1, minutes=30),
        associations=associations,
        batches=batches,
    )
    assert [s["sample_id"] for s in output["in_stock_samples"]] == ["S1", "S2", "S3"]
    shuffled = list(reversed(batches))
    again = inventory.compute(
        site="H",
        as_of=BASE + timedelta(hours=1, minutes=30),
        associations=dict(reversed(list(associations.items()))),
        batches=shuffled,
    )
    assert again == output


def _random_case(rng: random.Random, size: int):
    batches = []
    associations: dict[str, list[str]] = {}
    sites = ["A", "B", "C", "D"]
    for index in range(size):
        origin = rng.choice(sites)
        destination = rng.choice([s for s in sites if s != origin])
        departed = BASE + timedelta(minutes=rng.randint(0, 72 * 60))
        arrived = departed + timedelta(minutes=rng.randint(5, 480))
        no = f"B{index:03d}"
        batches.append(leg(no, origin, destination, departed, arrived))
        sample_id = f"S{rng.randrange(max(2, size // 2))}"
        associations.setdefault(sample_id, []).append(no)
    return associations, batches


def test_randomised_against_interval_reference():
    rng = random.Random(20261118)
    for case in range(400):
        associations, batches = _random_case(rng, rng.randint(1, 12))
        site = rng.choice(["A", "B", "C", "D"])
        as_of = BASE + timedelta(minutes=rng.randint(-60, 73 * 60))
        result = inventory.compute(
            site=site, as_of=as_of, associations=associations, batches=batches
        )
        actual = sorted(s["sample_id"] for s in result["in_stock_samples"])
        expected = reference_in_stock(site, as_of, associations, batches)
        assert actual == expected, f"case {case}: {site} {as_of}\n{batches}"
        reported = {c["sample_id"] for c in result["conflicting_samples"]}
        in_stock = {s["sample_id"] for s in result["in_stock_samples"]}
        assert not reported & in_stock
        for entry in result["conflicting_samples"]:
            assert entry["conflicts"]


def test_large_roster_performance():
    batches = []
    associations = {}
    for index in range(20_000):
        departed = BASE + timedelta(seconds=index)
        arrived = departed + timedelta(minutes=1)
        no = f"B{index:06d}"
        batches.append(leg(no, "Hub", "Leaf", departed, arrived))
        associations[f"S{index:06d}"] = [no]
    result = inventory.compute(
        site="Leaf",
        as_of=BASE + timedelta(hours=7),
        associations=associations,
        batches=batches,
    )
    assert len(result["in_stock_samples"]) == 20_000
