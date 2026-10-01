"""End-to-end API behavior: device release windows over required sensors."""


def _event(rev, parent, start, end, value=None, withdrawn=False, key=None,
           batch="B1", sensor="S1", unit="mm"):
    return {
        "batch_id": batch,
        "sensor_id": sensor,
        "revision_id": rev,
        "parent_id": parent,
        "idempotency_key": key or f"k-{rev}",
        "start_ns": start,
        "end_ns": end,
        "value": value,
        "unit": unit,
        "withdrawn": withdrawn,
    }


def _post(client, event, batch="B1"):
    return client.post(f"/api/v1/batches/{batch}/events", json=event)


def _release(client, batch="B1", sensors=None, **overrides):
    body = {
        "start_ns": 0,
        "end_ns": 300,
        "min_duration_ns": 50,
        "sensors": (
            [{"sensor_id": "S1", "min_value": 0.0, "max_value": 3.0, "unit": "mm"}]
            if sensors is None
            else sensors
        ),
    }
    body.update(overrides)
    return client.post(f"/api/v1/batches/{batch}/release", json=body)


def _seed_two_sensors(client):
    # S1: [0,300)=2.0mm with [100,200)=5.0mm on top; S2: [0,300)=1.5mm.
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0, key="k-v1"))
    _post(client, _event("v2", "v1", 100, 200, value=5.0, key="k-v2"))
    _post(client, _event("w1", "ROOT", 0, 300, value=1.5, sensor="S2", key="k-w1"))


_TWO_SENSORS = [
    {"sensor_id": "S1", "min_value": 0.0, "max_value": 3.0, "unit": "mm"},
    {"sensor_id": "S2", "min_value": 1.0, "max_value": 2.0, "unit": "mm"},
]


def test_release_all_sensors_qualify_everywhere(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0))
    body = _release(client).json()
    assert body["status"] == "released"
    assert body["batch_id"] == "B1"
    assert (body["window_start_ns"], body["window_end_ns"]) == (0, 300)
    assert body["min_duration_ns"] == 50
    assert body["released_intervals"] == [
        {"start_ns": 0, "end_ns": 300, "duration_ns": 300}
    ]
    assert body["withheld_intervals"] == []
    assert len(body["timeline_hash"]) == 64


def test_release_interleaves_released_and_unqualified_spans(client):
    _seed_two_sensors(client)
    body = _release(client, sensors=_TWO_SENSORS).json()
    assert body["status"] == "released"
    assert [(i["start_ns"], i["end_ns"], i["duration_ns"])
            for i in body["released_intervals"]] == [(0, 100, 100), (200, 300, 100)]
    assert body["withheld_intervals"] == [
        {
            "start_ns": 100,
            "end_ns": 200,
            "duration_ns": 100,
            "reason": "unqualified",
            "sensors": [
                {
                    "sensor_id": "S1",
                    "state": "out_of_bounds",
                    "value": 5.0,
                    "unit": "mm",
                    "missing_ancestors": [],
                }
            ],
        }
    ]


def test_release_short_qualified_run_reports_duration_shortage(client):
    _seed_two_sensors(client)
    body = _release(client, sensors=_TWO_SENSORS, min_duration_ns=150).json()
    assert body["status"] == "withheld"
    assert body["released_intervals"] == []
    assert [(i["start_ns"], i["end_ns"], i["reason"], i["sensors"])
            for i in body["withheld_intervals"]] == [
        (0, 100, "duration_shortage", []),
        (100, 200, "unqualified", body["withheld_intervals"][1]["sensors"]),
        (200, 300, "duration_shortage", []),
    ]
    assert body["withheld_intervals"][1]["sensors"][0]["state"] == "out_of_bounds"


def test_release_merges_qualified_run_across_value_changes(client):
    # Both values qualify: the continuous run merges across the revision
    # boundary and reaches the threshold only as a whole.
    _post(client, _event("v1", "ROOT", 0, 100, value=2.0, key="k-v1"))
    _post(client, _event("v2", "v1", 100, 200, value=2.5, key="k-v2"))
    body = _release(client, min_duration_ns=150).json()
    assert body["released_intervals"] == [
        {"start_ns": 0, "end_ns": 200, "duration_ns": 200}
    ]
    assert body["withheld_intervals"] == [
        {"start_ns": 200, "end_ns": 300, "duration_ns": 100,
         "reason": "unqualified",
         "sensors": [{"sensor_id": "S1", "state": "uncovered", "value": None,
                      "unit": None, "missing_ancestors": []}]}
    ]


def test_release_value_at_closed_bounds_qualifies(client):
    _post(client, _event("lo", "ROOT", 0, 100, value=0.0, key="k-lo"))
    _post(client, _event("hi", "lo", 100, 200, value=3.0, key="k-hi"))
    body = _release(client).json()
    assert body["released_intervals"] == [
        {"start_ns": 0, "end_ns": 200, "duration_ns": 200}
    ]


def test_release_unit_mismatch_disqualifies(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0, unit="F"))
    body = _release(client).json()
    assert body["status"] == "withheld"
    [span] = body["withheld_intervals"]
    assert (span["start_ns"], span["end_ns"], span["reason"]) == (0, 300, "unqualified")
    assert span["sensors"] == [
        {"sensor_id": "S1", "state": "unit_mismatch", "value": 2.0,
         "unit": "F", "missing_ancestors": []}
    ]


def test_release_withdrawn_span_disqualifies_and_breaks_runs(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0, key="k-v1"))
    _post(client, _event("w", "v1", 100, 200, withdrawn=True, value=None, key="k-w"))
    body = _release(client, min_duration_ns=100).json()
    assert body["released_intervals"] == [
        {"start_ns": 0, "end_ns": 100, "duration_ns": 100},
        {"start_ns": 200, "end_ns": 300, "duration_ns": 100},
    ]
    assert [(i["start_ns"], i["end_ns"], i["reason"]) for i in body["withheld_intervals"]] == [
        (100, 200, "unqualified")
    ]
    assert body["withheld_intervals"][0]["sensors"][0]["state"] == "withdrawn"


def test_release_blocked_span_pending_backfill_even_with_calibrated_value(client):
    # A grounded calibrated value already covers [40,60), but a blocked
    # revision overlaps it: backfilling may change the fold winner, so the
    # span is reported as blocked (pending backfill), not qualified.
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0, key="k-v1"))
    _post(client, _event("orphan", "ghost", 40, 60, value=9.0, key="k-o"))
    body = _release(client, min_duration_ns=1).json()
    assert [(i["start_ns"], i["end_ns"]) for i in body["released_intervals"]] == [
        (0, 40),
        (60, 300),
    ]
    [span] = body["withheld_intervals"]
    assert (span["start_ns"], span["end_ns"], span["reason"]) == (40, 60, "unqualified")
    assert span["sensors"] == [
        {"sensor_id": "S1", "state": "blocked", "value": None,
         "unit": None, "missing_ancestors": ["ghost"]}
    ]


def test_release_cycle_revisions_do_not_participate(client):
    # c1 <-> c2 form a parent cycle: excluded from the scan, so their span
    # is uncovered rather than blocked or qualified.
    _post(client, _event("c1", "c2", 0, 100, value=2.0, key="k-c1"))
    _post(client, _event("c2", "c1", 0, 100, value=2.0, key="k-c2"))
    _post(client, _event("v1", "ROOT", 100, 300, value=2.0, key="k-v1"))
    body = _release(client).json()
    assert body["released_intervals"] == [
        {"start_ns": 100, "end_ns": 300, "duration_ns": 200}
    ]
    assert [(i["start_ns"], i["end_ns"], i["reason"]) for i in body["withheld_intervals"]] == [
        (0, 100, "unqualified")
    ]
    assert body["withheld_intervals"][0]["sensors"][0]["state"] == "uncovered"


def test_release_unqualified_spans_merge_only_while_issue_vector_matches(client):
    # S1 out of bounds on [0,100); S2 uncovered on [0,50) and out of bounds
    # on [50,150): the failing-sensor vector changes at 50 and 100, so the
    # withheld spans split there even though the sensor set matches on
    # [0,100) (uncovered vs out_of_bounds are different issues).
    _post(client, _event("v1", "ROOT", 0, 300, value=9.0, key="k-v1"))
    _post(client, _event("w1", "ROOT", 50, 150, value=9.0, sensor="S2", key="k-w1"))
    _post(client, _event("w2", "w1", 150, 300, value=1.5, sensor="S2", key="k-w2"))
    _post(client, _event("v2", "v1", 100, 300, value=2.0, key="k-v2"))
    body = _release(client, sensors=_TWO_SENSORS).json()
    spans = [
        (
            i["start_ns"],
            i["end_ns"],
            [(s["sensor_id"], s["state"]) for s in i["sensors"]],
        )
        for i in body["withheld_intervals"]
    ]
    assert spans == [
        (0, 50, [("S1", "out_of_bounds"), ("S2", "uncovered")]),
        (50, 100, [("S1", "out_of_bounds"), ("S2", "out_of_bounds")]),
        (100, 150, [("S2", "out_of_bounds")]),
    ]
    assert body["released_intervals"] == [
        {"start_ns": 150, "end_ns": 300, "duration_ns": 150}
    ]


def test_release_window_clips_coverage_and_released_span(client):
    _post(client, _event("v1", "ROOT", 0, 1000, value=2.0))
    body = _release(client, start_ns=100, end_ns=200, min_duration_ns=100).json()
    assert body["released_intervals"] == [
        {"start_ns": 100, "end_ns": 200, "duration_ns": 100}
    ]
    # Half-open: a segment ending exactly at the window start contributes
    # nothing; the window edge itself is the boundary.
    _post(client, _event("v2", "v1", 50, 100, value=9.0, key="k-v2"))
    body = _release(client, start_ns=100, end_ns=200, min_duration_ns=100).json()
    assert body["withheld_intervals"] == []
    assert body["released_intervals"] == [
        {"start_ns": 100, "end_ns": 200, "duration_ns": 100}
    ]


def test_release_is_read_only_and_deterministic(client):
    events = [
        _event("r", "ROOT", 0, 300, value=2.0, key="k-r"),
        _event("a", "r", 0, 120, value=9.0, key="k-a"),
        _event("b", "r", 60, 180, value=1.0, key="k-b"),
        _event("w", "a", 30, 90, withdrawn=True, value=None, key="k-w"),
    ]

    def run(order):
        client.post("/admin/reset")
        for e in order:
            assert _post(client, e).status_code == 200
        before = client.get("/api/v1/batches/B1/result").json()["timeline_hash"]
        body = _release(client, min_duration_ns=20).json()
        after = client.get("/api/v1/batches/B1/result").json()["timeline_hash"]
        assert before == after  # the release query never mutates state
        return body

    first = run(events)
    assert run(list(reversed(events))) == first
    assert run([events[2], events[0], events[3], events[1]]) == first
    assert first["status"] == "released"


def test_release_unknown_batch_or_sensor_404(client):
    assert _release(client).status_code == 404
    _post(client, _event("v1", "ROOT", 0, 10, value=1.0, sensor="SX", key="k-x"))
    r = _release(client)
    assert r.status_code == 404
    assert "S1" in r.json()["detail"] and "B1" in r.json()["detail"]
    # The first unknown sensor of the list is reported.
    r = _release(client, sensors=[
        {"sensor_id": "SX", "min_value": 0.0, "max_value": 3.0, "unit": "mm"},
        {"sensor_id": "S1", "min_value": 0.0, "max_value": 3.0, "unit": "mm"},
    ])
    assert r.status_code == 404
    assert "S1" in r.json()["detail"]
    assert _release(client, sensors=[
        {"sensor_id": "SX", "min_value": 0.0, "max_value": 3.0, "unit": "mm"}
    ]).status_code == 200


def test_release_invalid_params_422(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0))
    # Empty / inverted query window.
    assert _release(client, start_ns=100, end_ns=100).status_code == 422
    assert _release(client, start_ns=200, end_ns=100).status_code == 422
    assert _release(client, start_ns=-1, end_ns=100).status_code == 422
    # Empty sensor list.
    assert _release(client, sensors=[]).status_code == 422
    # Duplicate sensor entries.
    dup = {"sensor_id": "S1", "min_value": 0.0, "max_value": 3.0, "unit": "mm"}
    assert _release(client, sensors=[dup, dict(dup)]).status_code == 422
    # Inverted / non-finite bounds.
    assert _release(client, sensors=[
        {"sensor_id": "S1", "min_value": 3.0, "max_value": 0.0, "unit": "mm"}
    ]).status_code == 422
    assert _release(client, sensors=[
        {"sensor_id": "S1", "min_value": 0.0, "max_value": "NaN", "unit": "mm"}
    ]).status_code == 422
    assert _release(client, sensors=[
        {"sensor_id": "S1", "min_value": "-Infinity", "max_value": 3.0, "unit": "mm"}
    ]).status_code == 422
    # Non-positive duration threshold.
    assert _release(client, min_duration_ns=0).status_code == 422
    # Missing required fields / unknown extra field.
    assert client.post("/api/v1/batches/B1/release", json={"start_ns": 0}).status_code == 422
    assert _release(client, tolerance=0.1).status_code == 422


def test_openapi_lists_release_endpoint(client):
    spec = client.get("/openapi.json").json()
    path = spec["paths"]["/api/v1/batches/{batch_id}/release"]["post"]
    ref = path["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/ReleaseResponse")
    props = spec["components"]["schemas"]["ReleaseRequest"]["properties"]
    assert {"start_ns", "end_ns", "min_duration_ns", "sensors"} <= set(props)
    req_props = spec["components"]["schemas"]["ReleaseSensorRequirement"]["properties"]
    assert {"sensor_id", "min_value", "max_value", "unit"} <= set(req_props)
