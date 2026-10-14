import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app, sign_payload
from app.schemas import EventIn
from app.storage import Store

SECRET = "test-webhook-secret"
ADMIN = "test-admin-token"


class FakeClock:
    def __init__(self):
        self.current = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def env(tmp_path):
    clock = FakeClock()
    path = tmp_path / "workflow.sqlite3"
    with TestClient(create_app(db_path=path, webhook_secret=SECRET,
                               admin_token=ADMIN, clock=clock)) as client:
        yield client, clock, Store(path)


def event(event_id="event_001", lead_id="demo_lead_1", event_type="lead.created", **kwargs):
    return dict(event_id=event_id, lead_id=lead_id,
                event_type=event_type, display_name="Fictional Demo Company",
                source="synthetic-contact-form") | kwargs


def send(client, data, secret=SECRET):
    raw = json.dumps(data, separators=(",", ":")).encode()
    return client.post("/webhooks/events", content=raw,
                       headers={"Content-Type": "application/json",
                                "X-Demo-Signature": sign_payload(raw, secret)})


def ops(client, route, method="get", json_payload=None, token=ADMIN):
    return client.request(method, route, json=json_payload, headers={"X-Admin-Token": token})


def process(client, limit=10):
    return ops(client, "/ops/process", "post", {"limit": limit})


def test_health_and_home(env):
    client, _, _ = env
    assert client.get("/health").json() == {"status": "ok"}
    assert "CRM Workflow Automation" in client.get("/").text


def test_missing_bad_signature_rejected(env):
    client, _, store = env
    assert client.post("/webhooks/events", json=event()).status_code == 401
    assert send(client, event(), secret="wrong-secret").status_code == 401
    assert store.metrics()["events"] == 0


def test_invalid_event_rejected(env):
    client, _, store = env
    assert send(client, event(event_id="bad/id")).status_code == 422
    assert send(client, event(event_type="unknown")).status_code == 422
    assert send(client, event(simulate_failures=9)).status_code == 422
    assert store.metrics()["events"] == 0


def test_payload_size_limit(env):
    client, _, _ = env
    raw = b"x" * 17000
    r = client.post("/webhooks/events", content=raw,
                    headers={"X-Demo-Signature": sign_payload(raw, SECRET)})
    assert r.status_code == 413


def test_admin_routes_are_protected(env):
    client, _, _ = env
    for route in ["/v1/leads", "/v1/events", "/v1/activities", "/ops/metrics"]:
        assert client.get(route).status_code == 401
        assert ops(client, route, token="wrong").status_code == 401
    assert client.post("/ops/process", json={"limit": 1}).status_code == 401


def test_lead_create_then_reply_then_followup(env):
    client, clock, store = env
    receipt = send(client, event())
    assert receipt.status_code == 200
    assert receipt.json() == {"event_id": "event_001", "enqueued": True, "status": "pending"}
    assert process(client).json() == {"attempted": 1, "processed": 1, "retried": 0, "dead": 0}
    assert ops(client, "/v1/leads").json()[0]["stage_name"] == "new"
    assert send(client, event(event_id="event_002", event_type="lead.replied")).status_code == 200
    assert process(client).json()["processed"] == 1
    assert ops(client, "/v1/leads").json()[0]["stage_name"] == "replied"
    for i in range(3, 8):
        assert send(client, event(event_id=f"event_00{i}", event_type="followup.requested")).status_code == 200
        assert process(client).json()["processed"] == 1
    assert ops(client, "/v1/leads").json()[0]["stage_name"] == "follow_up_4"
    assert len(store.list_activities()) == 7
    assert store.list_activities()[-1]["action"] == "followup_limit_reached"


def test_duplicate_webhook_is_idempotent(env):
    client, _, store = env
    assert send(client, event()).json()["enqueued"] is True
    assert send(client, event()).json() == {"event_id": "event_001", "enqueued": False, "status": "pending"}
    assert process(client).json()["processed"] == 1
    assert send(client, event()).json()["status"] == "processed"
    assert len(store.list_activities()) == 1
    assert len(store.list_leads()) == 1


def test_duplicate_lead_created_does_not_rewind_stage(env):
    client, _, store = env
    send(client, event())
    process(client)
    send(client, event(event_id="reply001", event_type="lead.replied"))
    process(client)
    send(client, event(event_id="another_create", display_name="Different Name"))
    process(client)
    assert store.list_leads()[0]["stage"] == 2
    assert store.list_leads()[0]["display_name"] == "Fictional Demo Company"
    assert store.list_activities()[-1]["action"] == "create_ignored_existing"


def test_retry_after_transient_failure(env):
    client, clock, store = env
    assert send(client, event(simulate_failures=1)).status_code == 200
    first = process(client).json()
    assert first == {"attempted": 1, "processed": 0, "retried": 1, "dead": 0}
    assert store.list_events()[0]["attempts"] == 1
    assert process(client).json()["attempted"] == 0
    clock.advance(1)
    assert process(client).json()["attempted"] == 0
    clock.advance(1)
    assert process(client).json()["processed"] == 1
    assert store.list_events()[0]["attempts"] == 2
    assert len(store.list_activities()) == 1


def test_dead_letter_after_three_attempts(env):
    client, clock, store = env
    send(client, event(simulate_failures=4))
    assert process(client).json()["retried"] == 1
    clock.advance(2)
    assert process(client).json()["retried"] == 1
    clock.advance(4)
    assert process(client).json()["dead"] == 1
    assert store.list_events()[0]["status"] == "dead"
    clock.advance(30)
    assert process(client).json()["attempted"] == 0
    assert store.metrics()["dead"] == 1
    assert len(store.list_activities()) == 0


def test_followup_without_reply_dead_letters(env):
    client, _, store = env
    send(client, event())
    process(client)
    send(client, event(event_id="followup-early", event_type="followup.requested"))
    assert process(client).json()["dead"] == 1
    assert store.list_leads()[0]["stage"] == 1


def test_update_before_create_dead_letters(env):
    client, _, store = env
    send(client, event(event_type="lead.replied"))
    assert process(client).json()["dead"] == 1
    assert store.metrics()["leads"] == 0


def test_recovery_of_processing_lease_does_not_double_apply(env):
    client, clock, store = env
    send(client, event())
    claimed = store.claim(clock())
    assert claimed["status"] == "pending"
    store.apply(claimed, clock())
    assert len(store.list_activities()) == 1
    clock.advance(31)
    assert process(client).json()["processed"] == 1
    assert len(store.list_activities()) == 1
    assert store.list_events()[0]["status"] == "processed"


def test_parallel_claim_does_not_duplicate(env):
    client, clock, store = env
    send(client, event())
    first = store.claim(clock())
    second = store.claim(clock())
    assert first is not None
    assert second is None


def test_empty_queue_and_metrics(env):
    client, _, store = env
    assert process(client).json() == {"attempted": 0, "processed": 0, "retried": 0, "dead": 0}
    assert ops(client, "/ops/metrics").json() == {
        "leads": 0, "events": 0, "pending": 0, "processing": 0, "processed": 0, "dead": 0,
    }


def test_process_limit_validation(env):
    client, _, _ = env
    assert ops(client, "/ops/process", "post", {"limit": 0}).status_code == 422
    assert ops(client, "/ops/process", "post", {"limit": 51}).status_code == 422


def test_multiple_events_with_limit_one(env):
    client, _, store = env
    for i in range(3):
        send(client, event(event_id=f"create_{i}", lead_id=f"demo_lead_{i}"))
    assert process(client, 1).json()["attempted"] == 1
    assert store.metrics()["pending"] == 2
    assert process(client, 2).json()["attempted"] == 2
    assert store.metrics()["processed"] == 3


def test_payload_can_be_revalidated_from_store(env):
    client, _, store = env
    send(client, event())
    row = store.list_events()[0]
    assert EventIn.model_validate(row["payload"]).lead_id == "demo_lead_1"


def test_no_real_crm_or_webhook_delivery(env):
    client, _, store = env
    send(client, event())
    process(client)
    assert store.list_leads()[0]["source"] == "synthetic-contact-form"
    assert store.list_activities()[0]["action"] == "created"


def test_hmac_signature_is_bound_to_exact_body():
    assert sign_payload(b"{\"a\":1}", SECRET) != sign_payload(b"{\"a\":2}", SECRET)


def test_bad_json_is_rejected_after_valid_signature(env):
    client, _, store = env
    raw = b"{ definitely not json }"
    response = client.post("/webhooks/events", content=raw,
                          headers={"X-Demo-Signature": sign_payload(raw, SECRET)})
    assert response.status_code == 422
    assert store.metrics()["events"] == 0


def test_two_failures_then_third_attempt_succeeds(env):
    client, clock, store = env
    send(client, event(simulate_failures=2))
    assert process(client).json()["retried"] == 1
    clock.advance(2)
    assert process(client).json()["retried"] == 1
    clock.advance(4)
    assert process(client).json()["processed"] == 1
    assert store.list_events()[0]["attempts"] == 3
    assert len(store.list_activities()) == 1


def test_worker_crash_before_apply_releases_lease(env):
    client, clock, store = env
    send(client, event())
    store.claim(clock())  # Process died without an API side-effect
    assert process(client).json()["attempted"] == 0
    clock.advance(31)
    assert process(client).json()["processed"] == 1
    assert len(store.list_activities()) == 1
