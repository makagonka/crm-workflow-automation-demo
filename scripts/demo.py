"""Runs a realistic synthetic workflow without a running server or API keys."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app.schemas import EventIn
from app.storage import Store


def run() -> None:
    now = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
    with TemporaryDirectory() as temp:
        store = Store(Path(temp) / "crm.sqlite3")
        examples = [
            EventIn(event_id="demo-create", lead_id="lead-100", event_type="lead.created",
                    display_name="Example Studio", simulate_failures=1),
            EventIn(event_id="demo-reply", lead_id="lead-100", event_type="lead.replied"),
            EventIn(event_id="demo-followup", lead_id="lead-100", event_type="followup.requested"),
        ]
        for i, message in enumerate(examples):
            store.enqueue(message, now)
            result = store.process_batch(now, limit=1)
            print(message.event_type, result)
            if i == 0:
                now += timedelta(seconds=2)
                print("retry:", store.process_batch(now))
        stages = store.list_leads()
        assert len(stages) == 1 and stages[0]["stage_name"] == "follow_up_1"
        assert store.metrics()["processed"] == 3
        print("Final synthetic CRM:", stages)
        print("Metrics:", store.metrics())


if __name__ == "__main__":
    run()
