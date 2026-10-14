"""Strict API request contracts; examples must contain only synthetic data."""
from typing import Literal
from pydantic import BaseModel, Field


class EventIn(BaseModel):
    event_id: str = Field(min_length=4, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    lead_id: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    event_type: Literal["lead.created", "lead.replied", "followup.requested"]
    display_name: str = Field(default="Sample client", min_length=1, max_length=100)
    source: str = Field(default="synthetic-webform", min_length=3, max_length=80)
    simulate_failures: int = Field(default=0, ge=0, le=4)


class EventReceipt(BaseModel):
    event_id: str
    enqueued: bool
    status: str


class ProcessRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)


class ProcessReport(BaseModel):
    attempted: int
    processed: int
    retried: int
    dead: int


STAGES = (
    "new",
    "replied",
    "follow_up_1",
    "follow_up_2",
    "follow_up_3",
    "follow_up_4",
)
