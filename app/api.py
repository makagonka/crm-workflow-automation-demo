"""HMAC signed synthetic intake and protected, manual local processing."""
import hashlib
import hmac
import os
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError

from app.schemas import EventIn, EventReceipt, ProcessRequest, ProcessReport
from app.storage import Store


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sign_payload(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def create_app(*, db_path: str | Path | None = None, webhook_secret: str | None = None,
               admin_token: str | None = None, clock: Callable[[], datetime] | None = None) -> FastAPI:
    app = FastAPI(title="CRM Workflow Automation Demo", version="1.0.0")
    store = Store(db_path or os.getenv("DEMO_DB_PATH", "./data/demo.sqlite3"))
    webhook_secret = webhook_secret or os.getenv("DEMO_WEBHOOK_SECRET", "local-demo-webhook-change-me")
    admin_token = admin_token or os.getenv("DEMO_ADMIN_TOKEN", "local-demo-admin-change-me")
    clock = clock or now_utc

    def require_admin(x_admin_token: str | None = Header(default=None)):
        if not x_admin_token or not hmac.compare_digest(x_admin_token, admin_token):
            raise HTTPException(status_code=401, detail="Admin token required")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/webhooks/events", response_model=EventReceipt)
    async def receive(request: Request, x_demo_signature: str | None = Header(default=None)):
        raw = await request.body()
        if len(raw) > 16000:
            raise HTTPException(413, "Payload too large")
        if not x_demo_signature or not hmac.compare_digest(x_demo_signature, sign_payload(raw, webhook_secret)):
            raise HTTPException(401, "Invalid webhook signature")
        try:
            event = EventIn.model_validate_json(raw)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid event payload") from exc
        enqueued, status = store.enqueue(event, clock())
        return EventReceipt(event_id=event.event_id, enqueued=enqueued, status=status)

    @app.post("/ops/process", response_model=ProcessReport, dependencies=[Depends(require_admin)])
    def process(body: ProcessRequest):
        return ProcessReport(**store.process_batch(clock(), body.limit))

    @app.get("/ops/metrics", dependencies=[Depends(require_admin)])
    def metrics():
        return store.metrics()

    @app.get("/v1/leads", dependencies=[Depends(require_admin)])
    def leads():
        return store.list_leads()

    @app.get("/v1/events", dependencies=[Depends(require_admin)])
    def events():
        return store.list_events()

    @app.get("/v1/activities", dependencies=[Depends(require_admin)])
    def activities():
        return store.list_activities()

    return app


app = create_app()
