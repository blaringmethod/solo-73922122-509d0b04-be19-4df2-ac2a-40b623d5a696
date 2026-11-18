"""As-of on-site inventory checks: intervals, conflicts, snapshot pinning."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from conftest import batch, ingest
from fastapi.testclient import TestClient

from app import inventory
from app.main import create_app
from app.store import Store


def check(client, site, as_of, **kwargs):
    return client.post(
        "/inventory-checks",
        json={"site": site, "as_of": as_of, **kwargs},
    )


def seed(client):
    """One source-free route through three stations plus two extra samples.

    S1: A --B1--> Hub           (arrive 01-01 10:00)
    S1: Hub --B2--> Ref         (depart 01-01 20:00, arrive 22:00)
    S2: Ref --B3--> Archive     (depart 01-02 10:00, arrive 12:00)
    S3: stays on B4 from Hub to Cold-Room, departs 01-01 11:00, arrives 12:00
    """
    ingest(
        client,
        sources=[],
        samples=[
            {"sample_id": "S1", "batch_nos": ["B1", "B2"]},
            {"sample_id": "S2", "batch_nos": ["B2", "B3"]},
            {"sample_id": "S3", "batch_nos": ["B4"]},
        ],
        batches=[
            batch("B1", "A", "Hub", "2026-01-01T08:00:00Z", "2026-01-01T10:00:00Z"),
            batch("B2", "Hub", "Ref", "2026-01-01T20:00:00Z", "2026-01-01T22:00:00Z"),
            batch("B3", "Ref", "Archive", "2026-01-02T10:00:00Z", "2026-01-02T12:00:00Z"),
            batch("B4", "Hub", "Cold-Room", "2026-01-01T11:00:00Z", "2026-01-01T12:00:00Z"),
        ],
    )


def test_station_interval_roster_and_supporting_versions(client):
    seed(client)
    response = check(client, "Hub", "2026-01-01T15:00:00Z")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert [s["sample_id"] for s in body["in_stock_samples"]] == ["S1"]
    item = body["in_stock_samples"][0]
    assert item["in_stock_since"] == "2026-01-01T10:00:00Z"
    assert item["in_stock_until"] == "2026-01-01T20:00:00Z"
    assert [(b["batch_no"], b["version"], b["role"]) for b in item["supporting_batches"]] == [
        ("B1", 1, "arrival"),
        ("B2", 1, "next_departure"),
    ]
    assert body["snapshot"]["batch_versions"] == {"B1": 1, "B2": 1, "B3": 1, "B4": 1}
    assert body["conflicting_samples"] == []


def test_after_last_arrival_stays_in_stock(client):
    seed(client)
    body = check(client, "Archive", "2026-06-01T00:00:00Z").json()
    assert [s["sample_id"] for s in body["in_stock_samples"]] == ["S2"]
    item = body["in_stock_samples"][0]
    assert item["in_stock_until"] is None
    assert [b["role"] for b in item["supporting_batches"]] == ["arrival"]


def test_before_first_departure_no_position_inferred(client):
    seed(client)
    body = check(client, "A", "2026-01-01T07:59:59Z").json()
    assert body["in_stock_samples"] == []


def test_first_departure_boundary_is_transit(client):
    seed(client)
    body = check(client, "A", "2026-01-01T08:00:00Z").json()
    assert body["in_stock_samples"] == []  # departed, not yet arrived
    body = check(client, "Hub", "2026-01-01T10:00:00Z").json()
    assert [s["sample_id"] for s in body["in_stock_samples"]] == ["S1"]


def test_leave_boundary_excludes_station(client):
    seed(client)
    body = check(client, "Hub", "2026-01-01T20:00:00Z").json()
    assert "S1" not in {s["sample_id"] for s in body["in_stock_samples"]}
    body = check(client, "Hub", "2026-01-01T19:59:59.999999Z").json()
    assert "S1" in {s["sample_id"] for s in body["in_stock_samples"]}


def test_transit_never_counts_as_in_stock(client):
    seed(client)
    body = check(client, "Ref", "2026-01-01T21:00:00Z").json()
    assert body["in_stock_samples"] == []
    body = check(client, "Hub", "2026-01-01T21:00:00Z").json()
    assert "S1" not in {s["sample_id"] for s in body["in_stock_samples"]}


def test_overlapping_transit_conflict_excluded_from_roster(client):
    ingest(
        client,
        sources=[],
        samples=[{"sample_id": "S-OV", "batch_nos": ["B-OV1", "B-OV2"]}],
        batches=[
            batch("B-OV1", "X", "Y", "2026-01-01T00:00:00Z", "2026-01-01T05:00:00Z"),
            batch("B-OV2", "Y", "Z", "2026-01-01T04:00:00Z", "2026-01-01T06:00:00Z"),
        ],
    )
    body = check(client, "Y", "2026-01-01T04:30:00Z").json()
    assert body["in_stock_samples"] == []
    assert [c["sample_id"] for c in body["conflicting_samples"]] == ["S-OV"]
    conflict = body["conflicting_samples"][0]["conflicts"][0]
    assert conflict["code"] == "overlapping_transits"
    assert conflict["batch_nos"] == ["B-OV1", "B-OV2"]
    assert [b["version"] for b in conflict["batches"]] == [1, 1]


def test_disconnected_route_conflict_excluded_from_roster(client):
    ingest(
        client,
        sources=[],
        samples=[{"sample_id": "S-DC", "batch_nos": ["B-DC1", "B-DC2"]}],
        batches=[
            batch("B-DC1", "X", "Y", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
            batch("B-DC2", "Z", "Y", "2026-01-01T02:00:00Z", "2026-01-01T03:00:00Z"),
        ],
    )
    body = check(client, "Y", "2026-01-01T05:00:00Z").json()
    assert body["in_stock_samples"] == []
    assert body["conflicting_samples"][0]["conflicts"][0]["code"] == "disconnected_route"


def test_conflict_listed_even_when_not_at_requested_site(client):
    ingest(
        client,
        sources=[],
        samples=[{"sample_id": "S-OV", "batch_nos": ["B-OV1", "B-OV2"]}],
        batches=[
            batch("B-OV1", "X", "Y", "2026-01-01T00:00:00Z", "2026-01-01T05:00:00Z"),
            batch("B-OV2", "Y", "Z", "2026-01-01T04:00:00Z", "2026-01-01T06:00:00Z"),
        ],
    )
    body = check(client, "Nowhere", "2026-01-01T04:30:00Z").json()
    assert body["in_stock_samples"] == []
    assert body["conflict_count"] == 1


def test_unknown_site_yields_empty_complete_result(client):
    seed(client)
    response = check(client, "Missing-Station", "2026-01-01T12:00:00Z")
    assert response.status_code == 201
    assert response.json()["in_stock_samples"] == []
    assert response.json()["conflict_count"] == 0


def test_inventory_pins_versions_and_associations(client):
    seed(client)
    body = check(client, "Hub", "2026-01-01T15:00:00Z").json()
    assert body["snapshot"]["batch_versions"] == {
        "B1": 1,
        "B2": 1,
        "B3": 1,
        "B4": 1,
    }
    assert body["snapshot"]["associations"]["S1"] == ["B1", "B2"]
    pinned = {b["batch_no"]: b for b in body["snapshot"]["batches"]}
    assert pinned["B2"]["destination"] == "Ref"


def test_correction_invalidates_old_inventory_and_fresh_reflects_it(client):
    seed(client)
    old = check(client, "Archive", "2026-01-02T13:00:00Z").json()
    assert [s["sample_id"] for s in old["in_stock_samples"]] == ["S2"]

    correction = client.post(
        "/batches/B3/corrections",
        json={
            "seen_version": 1,
            "origin": "Ref",
            "destination": "Cold-Room",
            "departed_at": "2026-01-02T10:00:00Z",
            "arrived_at": "2026-01-02T12:00:00Z",
        },
    )
    assert correction.status_code == 200, correction.text
    assert correction.json()["invalidated_inventory_query_ids"] == [old["query_id"]]

    stale = client.get(f"/inventory-checks/{old['query_id']}")
    assert stale.status_code == 409
    assert stale.json()["code"] == "snapshot_invalidated"
    assert stale.json()["details"]["invalidations"][0]["batch_no"] == "B3"

    fresh = check(client, "Archive", "2026-01-02T13:00:00Z").json()
    assert fresh["in_stock_samples"] == []
    assert fresh["snapshot"]["batch_versions"]["B3"] == 2
    # S3 has been in cold storage since B4 arrived; S2 joined it via the
    # corrected B3, so the corrected value is reflected in a fresh conclusion.
    cold = check(client, "Cold-Room", "2026-01-02T13:00:00Z").json()
    assert {s["sample_id"] for s in cold["in_stock_samples"]} == {"S2", "S3"}


def test_identical_intake_resubmission_does_not_invalidate(client):
    seed(client)
    q = check(client, "Hub", "2026-01-01T15:00:00Z").json()
    ingest(
        client,
        sources=[],
        samples=[
            {"sample_id": "S1", "batch_nos": ["B1", "B2"]},
            {"sample_id": "S2", "batch_nos": ["B2", "B3"]},
            {"sample_id": "S3", "batch_nos": ["B4"]},
        ],
        batches=[
            batch("B1", "A", "Hub", "2026-01-01T08:00:00Z", "2026-01-01T10:00:00Z"),
            batch("B2", "Hub", "Ref", "2026-01-01T20:00:00Z", "2026-01-01T22:00:00Z"),
            batch("B3", "Ref", "Archive", "2026-01-02T10:00:00Z", "2026-01-02T12:00:00Z"),
            batch("B4", "Hub", "Cold-Room", "2026-01-01T11:00:00Z", "2026-01-01T12:00:00Z"),
        ],
    )
    still = client.get(f"/inventory-checks/{q['query_id']}")
    assert still.status_code == 200
    assert still.json()["in_stock_count"] == 1
    assert client.get(
        f"/inventory-checks/{q['query_id']}/invalidations"
    ).json() == []


def test_new_association_invalidates_old_conclusion(client):
    seed(client)
    q = check(client, "Hub", "2026-01-01T15:00:00Z").json()
    # S9 is a new sample that also rides B1 (arrives Hub 10:00, leaves 20:00).
    ingest(
        client,
        sources=[],
        samples=[{"sample_id": "S9", "batch_nos": ["B1"]}],
        batches=[],
    )
    stale = client.get(f"/inventory-checks/{q['query_id']}")
    assert stale.status_code == 409
    details = stale.json()["details"]["invalidations"]
    assert details[0]["kind"] == "association"
    assert details[0]["sample_id"] == "S9"

    fresh = check(client, "Hub", "2026-01-01T15:00:00Z").json()
    assert [s["sample_id"] for s in fresh["in_stock_samples"]] == ["S1", "S9"]


def test_association_added_to_existing_sample_invalidates(client):
    seed(client)
    q = check(client, "Archive", "2026-01-02T13:00:00Z").json()
    # S1, already known, additionally rides B3; its roster membership changes.
    ingest(
        client,
        sources=[],
        samples=[{"sample_id": "S1", "batch_nos": ["B3"]}],
        batches=[],
    )
    assert client.get(f"/inventory-checks/{q['query_id']}").status_code == 409
    fresh = check(client, "Archive", "2026-01-02T13:00:00Z").json()
    assert {s["sample_id"] for s in fresh["in_stock_samples"]} == {"S1", "S2"}


def test_correction_of_unpinned_batch_leaves_inventory_valid(client):
    seed(client)
    # A batch that no sample rides is recorded in the dataset but is not part
    # of any inventory query's pinned basis.
    ingest(
        client,
        sources=[],
        samples=[{"sample_id": "S9", "batch_nos": []}],
        batches=[
            batch("B9", "x", "y", "2026-03-01T00:00:00Z", "2026-03-01T01:00:00Z")
        ],
    )
    q = check(client, "Hub", "2026-01-01T15:00:00Z").json()
    assert "B9" not in q["snapshot"]["batch_versions"]
    response = client.post(
        "/batches/B9/corrections",
        json={
            "seen_version": 1,
            "origin": "x",
            "destination": "z",
            "departed_at": "2026-03-01T00:30:00Z",
            "arrived_at": "2026-03-01T01:00:00Z",
        },
    )
    assert response.status_code == 200
    assert response.json()["invalidated_inventory_query_ids"] == []
    assert client.get(f"/inventory-checks/{q['query_id']}").status_code == 200


def test_inventory_listing_and_invalidation_endpoint(client):
    seed(client)
    q = check(client, "Hub", "2026-01-01T15:00:00Z").json()
    client.post(
        "/batches/B1/corrections",
        json={
            "seen_version": 1,
            "origin": "A",
            "destination": "Hub",
            "departed_at": "2026-01-01T08:00:00Z",
            "arrived_at": "2026-01-01T09:30:00Z",
        },
    )
    listing = client.get("/inventory-checks").json()["queries"]
    row = next(item for item in listing if item["query_id"] == q["query_id"])
    assert row["invalidated"] is True
    assert row["in_stock_count"] == 1  # stored rows kept for audit
    records = client.get(f"/inventory-checks/{q['query_id']}/invalidations").json()
    assert records[0]["kind"] == "batch"
    assert records[0]["version"] == 1


def test_inventory_requires_dataset(client):
    assert check(client, "Hub", "2026-01-01T00:00:00Z").status_code == 404


def test_inventory_validation_errors_are_structured(client):
    seed(client)
    assert check(client, "", "2026-01-01T00:00:00Z").status_code == 422
    response = client.post(
        "/inventory-checks", json={"site": "Hub", "as_of": "not-a-time"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_inventory_naive_and_offset_times_normalised(client):
    seed(client)
    body = client.post(
        "/inventory-checks",
        json={"site": "Hub", "as_of": "2026-01-01T23:00:00+08:00"},
    ).json()
    assert body["as_of"] == "2026-01-01T15:00:00Z"
    assert [s["sample_id"] for s in body["in_stock_samples"]] == ["S1"]


def test_inventory_failure_publishes_no_partial_rows(client, monkeypatch):
    seed(client)

    def boom(**kwargs):
        raise RuntimeError("simulated inventory failure")

    monkeypatch.setattr(inventory, "compute", boom)
    response = check(client, "Hub", "2026-01-01T15:00:00Z")
    assert response.status_code == 500
    detail = response.json()
    assert detail["code"] == "inventory_failed"
    query_id = detail["details"]["query_id"]
    listing = client.get("/inventory-checks").json()["queries"]
    failed = next(row for row in listing if row["query_id"] == query_id)
    assert failed["status"] == "failed"
    assert failed["in_stock_count"] == 0
    read_back = client.get(f"/inventory-checks/{query_id}")
    assert read_back.status_code == 500
    assert read_back.json()["code"] == "inventory_failed"


def test_association_added_during_compute_blocks_publication(client, monkeypatch):
    seed(client)
    real_compute = inventory.compute

    def racing(**kwargs):
        ingest(
            client,
            sources=[],
            samples=[{"sample_id": "S-NEW", "batch_nos": ["B1"]}],
            batches=[],
        )
        return real_compute(**kwargs)

    monkeypatch.setattr(inventory, "compute", racing)
    response = check(client, "Hub", "2026-01-01T15:00:00Z")
    assert response.status_code == 409
    assert response.json()["code"] == "snapshot_changed"
    query_id = response.json()["details"]["query_id"]
    listing = client.get("/inventory-checks").json()["queries"]
    failed = next(row for row in listing if row["query_id"] == query_id)
    assert failed["status"] == "failed"
    assert failed["in_stock_count"] == 0


def test_batch_correction_during_compute_blocks_publication(client, monkeypatch):
    seed(client)
    real_compute = inventory.compute

    def racing(**kwargs):
        response = client.post(
            "/batches/B2/corrections",
            json={
                "seen_version": 1,
                "origin": "Hub",
                "destination": "Ref",
                "departed_at": "2026-01-01T23:00:00Z",
                "arrived_at": "2026-01-02T00:00:00Z",
            },
        )
        assert response.status_code == 200, response.text
        return real_compute(**kwargs)

    monkeypatch.setattr(inventory, "compute", racing)
    response = check(client, "Hub", "2026-01-01T15:00:00Z")
    assert response.status_code == 409
    assert response.json()["code"] == "snapshot_changed"
    query_id = response.json()["details"]["query_id"]
    listing = client.get("/inventory-checks").json()["queries"]
    failed = next(row for row in listing if row["query_id"] == query_id)
    assert failed["status"] == "failed"
    assert failed["in_stock_count"] == 0
    assert failed["conflict_count"] == 0


def test_inventory_conclusion_survives_restart(tmp_path):
    path = str(tmp_path / "inventory-restart.db")
    with TestClient(create_app(path)) as c:
        ingest(
            c,
            sources=[],
            samples=[{"sample_id": "S1", "batch_nos": ["B1"]}],
            batches=[
                batch(
                    "B1", "A", "Hub", "2026-01-01T08:00:00Z", "2026-01-01T10:00:00Z"
                )
            ],
        )
        query_id = check(c, "Hub", "2026-01-01T12:00:00Z").json()["query_id"]

    with TestClient(create_app(path)) as c:
        body = c.get(f"/inventory-checks/{query_id}")
        assert body.status_code == 200
        assert body.json()["in_stock_samples"][0]["sample_id"] == "S1"


def test_pending_inventory_from_crash_is_marked_failed(tmp_path):
    path = str(tmp_path / "inventory-crash.db")
    store = Store(path)
    store.ingest(
        samples=[{"sample_id": "S1", "contaminated": False, "batch_nos": []}],
        batches=[],
        source_sample_ids=[],
    )
    query_id, _snapshot = store.accept_inventory(
        site="Hub",
        as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    # Simulate a process dying before publication (startup recovery ignores
    # very recent pending queries, so age this one past the grace period).
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE inventory_queries"
            " SET accepted_at='2000-01-01T00:00:00.000000+00:00' WHERE id=?",
            (query_id,),
        )
    store.mark_interrupted()
    with TestClient(create_app(path)) as c:
        row = next(
            item
            for item in c.get("/inventory-checks").json()["queries"]
            if item["query_id"] == query_id
        )
        assert row["status"] == "failed"
        fresh = c.post(
            "/inventory-checks",
            json={"site": "Hub", "as_of": "2026-01-01T00:00:00Z"},
        )
        assert fresh.status_code == 201
        assert c.get(f"/inventory-checks/{query_id}").status_code == 500
