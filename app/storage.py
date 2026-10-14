"""Durable event inbox/outbox, deterministic fake CRM and retry/dead-letter handling."""
import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from app.schemas import EventIn, STAGES


class PermanentEventError(Exception):
    """An event cannot be applied under current workflow rules."""


class TransientEventError(Exception):
    """Synthetic adapter failure used to demonstrate retry semantics."""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as conn:
            conn.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','processing','processed','dead')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt TEXT NOT NULL,
                claimed_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_status_due ON events(status,next_attempt);
            CREATE TABLE IF NOT EXISTS crm_leads (
                lead_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                source TEXT NOT NULL,
                stage INTEGER NOT NULL CHECK(stage BETWEEN 1 AND 6),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                lead_id TEXT NOT NULL,
                action TEXT NOT NULL,
                stage INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """)

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def enqueue(self, event: EventIn, now: datetime) -> tuple[bool, str]:
        timestamp = iso(now)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT status FROM events WHERE event_id=?", (event.event_id,)).fetchone()
            if existing is not None:
                return False, existing["status"]
            db.execute("""INSERT INTO events
                (event_id,lead_id,kind,payload,status,next_attempt,created_at)
                VALUES (?,?,?,?,'pending',?,?)""",
                (event.event_id, event.lead_id, event.event_type, event.model_dump_json(), timestamp, timestamp))
            return True, "pending"

    def claim(self, now: datetime):
        """Single-work-item lease; reclaims worker crashes after 30 seconds."""
        now_str = iso(now)
        expired = iso(now - timedelta(seconds=30))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM events
                WHERE (status='pending' AND next_attempt<=?)
                   OR (status='processing' AND claimed_at<=?)
                ORDER BY next_attempt,event_id LIMIT 1""", (now_str, expired)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE events SET status='processing',attempts=attempts+1,claimed_at=? WHERE event_id=?",
                       (now_str, row["event_id"]))
            return dict(row) | {"attempts": row["attempts"] + 1}

    def apply(self, event: dict, now: datetime) -> None:
        """CRM mutation and unique activity record happen in ONE transaction.

        If the worker dies after applying but before marking the event processed,
        a reclaim will see the activity and will not apply it twice.
        """
        payload = EventIn.model_validate_json(event["payload"])
        if event["attempts"] <= payload.simulate_failures:
            raise TransientEventError("Synthetic CRM adapter unavailable")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            applied = db.execute("SELECT 1 FROM activities WHERE event_id=?", (payload.event_id,)).fetchone()
            if applied is not None:
                return
            old = db.execute("SELECT * FROM crm_leads WHERE lead_id=?", (payload.lead_id,)).fetchone()
            if payload.event_type == "lead.created":
                if old is None:
                    stage = 1
                    db.execute("""INSERT INTO crm_leads
                        (lead_id,display_name,source,stage,updated_at) VALUES (?,?,?,?,?)""",
                        (payload.lead_id, payload.display_name, payload.source, stage, iso(now)))
                    action = "created"
                else:
                    stage = old["stage"]
                    action = "create_ignored_existing"
            elif old is None:
                raise PermanentEventError("Lead not found; create it before sending updates")
            elif payload.event_type == "lead.replied":
                stage = max(2, old["stage"])
                action = "replied"
                db.execute("UPDATE crm_leads SET stage=?,updated_at=? WHERE lead_id=?",
                           (stage, iso(now), payload.lead_id))
            else:  # followup.requested
                if old["stage"] == 1:
                    raise PermanentEventError("Follow-up requires a replied lead")
                stage = min(6, old["stage"] + 1)
                action = "followup_recorded" if stage > old["stage"] else "followup_limit_reached"
                db.execute("UPDATE crm_leads SET stage=?,updated_at=? WHERE lead_id=?",
                           (stage, iso(now), payload.lead_id))
            db.execute("""INSERT INTO activities(event_id,lead_id,action,stage,created_at)
                VALUES(?,?,?,?,?)""", (payload.event_id, payload.lead_id, action, stage, iso(now)))

    def success(self, event_id: str, now: datetime) -> None:
        with self.db() as db:
            db.execute("""UPDATE events SET status='processed',claimed_at=NULL,finished_at=?,last_error=''
                WHERE event_id=? AND status='processing'""", (iso(now), event_id))

    def failure(self, event: dict, now: datetime, permanent: bool = False) -> str:
        dead = permanent or event["attempts"] >= 3
        state = "dead" if dead else "pending"
        available = now + timedelta(seconds=min(60, 2 ** event["attempts"]))
        with self.db() as db:
            db.execute("""UPDATE events SET status=?, claimed_at=NULL, next_attempt=?, last_error=?,
                finished_at=? WHERE event_id=?""",
                (state, iso(available),
                 "Permanent workflow rule violation" if permanent else "Synthetic adapter failure; retry scheduled",
                 iso(now) if dead else None, event["event_id"]))
        return state

    def list_events(self):
        with self.db() as db:
            return [dict(r) | {"payload": json.loads(r["payload"])}
                    for r in db.execute("SELECT * FROM events ORDER BY created_at,event_id")]

    def list_leads(self):
        with self.db() as db:
            return [dict(r) | {"stage_name": STAGES[r["stage"] - 1]}
                    for r in db.execute("SELECT * FROM crm_leads ORDER BY lead_id")]

    def list_activities(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM activities ORDER BY id")]

    def metrics(self):
        with self.db() as db:
            rows = db.execute("SELECT status,COUNT(*) AS total FROM events GROUP BY status").fetchall()
            by_state = {row["status"]: row["total"] for row in rows}
            lead_count = db.execute("SELECT COUNT(*) FROM crm_leads").fetchone()[0]
        return {"leads": lead_count, "events": sum(by_state.values()),
                **{status: by_state.get(status, 0) for status in ("pending", "processing", "processed", "dead")}}

    def process_batch(self, now: datetime, limit: int = 10) -> dict:
        result = {"attempted": 0, "processed": 0, "retried": 0, "dead": 0}
        # Keep each invocation bounded; delayed events await the next invocation.
        for _ in range(limit):
            event = self.claim(now)
            if event is None:
                break
            result["attempted"] += 1
            try:
                self.apply(event, now)
            except PermanentEventError:
                self.failure(event, now, permanent=True)
                result["dead"] += 1
            except TransientEventError:
                state = self.failure(event, now)
                result["dead" if state == "dead" else "retried"] += 1
            except Exception:
                # Unexpected adapter bugs must not fabricate successful delivery.
                logging.getLogger(__name__).exception("Unexpected worker error for event %s", event["event_id"])
                state = self.failure(event, now)
                result["dead" if state == "dead" else "retried"] += 1
            else:
                self.success(event["event_id"], now)
                result["processed"] += 1
        return result
