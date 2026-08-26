from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    retry = "retry"
    completed = "completed"
    dead = "dead"
    cancelled = "cancelled"


name_pattern = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"


class JobCreate(ApiModel):
    queue: str = Field(default="default", pattern=name_pattern)
    task: str = Field(pattern=name_pattern)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-100, le=100)
    max_attempts: int = Field(default=3, ge=1, le=20)
    delay_seconds: int = Field(default=0, ge=0, le=2_592_000)
    retry_base_seconds: int = Field(default=15, ge=1, le=3600)


class JobView(ApiModel):
    id: str
    queue: str
    task: str
    payload: dict[str, Any]
    status: JobStatus
    priority: int
    attempt: int
    max_attempts: int
    retry_base_seconds: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class JobPage(ApiModel):
    jobs: list[JobView]
    count: int


class ClaimRequest(ApiModel):
    queue: str = Field(default="default", pattern=name_pattern)
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:@/-]+$")
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class LeaseGrant(ApiModel):
    job: JobView
    lease_token: str


class LeaseCommand(ApiModel):
    lease_token: str = Field(min_length=32, max_length=256)


class HeartbeatCommand(LeaseCommand):
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class FailCommand(LeaseCommand):
    error: str = Field(min_length=1, max_length=1000)


class EventView(ApiModel):
    id: int
    job_id: str
    event_type: str
    detail: dict[str, Any]
    created_at: datetime


class EventPage(ApiModel):
    events: list[EventView]


class QueueStats(ApiModel):
    queued: int = 0
    running: int = 0
    retry: int = 0
    completed: int = 0
    dead: int = 0
    cancelled: int = 0
    total: int = 0
    oldest_pending_seconds: int | None = None


class Health(ApiModel):
    status: str


class MutationResult(ApiModel):
    job: JobView


class CreateResult(MutationResult):
    deduplicated: bool
