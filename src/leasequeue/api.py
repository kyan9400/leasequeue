import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import FileResponse, PlainTextResponse

from leasequeue.schemas import (
    ClaimRequest,
    CreateResult,
    EventPage,
    FailCommand,
    Health,
    HeartbeatCommand,
    JobCreate,
    JobPage,
    JobStatus,
    LeaseCommand,
    LeaseGrant,
    MutationResult,
    QueueStats,
)
from leasequeue.service import QueueError, QueueService

router = APIRouter()
static_dir = Path(__file__).parent / "static"
idempotency_pattern = re.compile(r"^[\x21-\x7e]{1,128}$")


def get_service(request: Request) -> QueueService:
    return request.app.state.queue_service


Service = Annotated[QueueService, Depends(get_service)]


@router.get("/static/app.js", include_in_schema=False)
async def javascript() -> FileResponse:
    return FileResponse(static_dir / "app.js", media_type="application/javascript")


@router.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@router.get("/health/live", response_model=Health, tags=["health"])
async def live() -> Health:
    return Health(status="ok")


@router.get("/health/ready", response_model=Health, tags=["health"])
async def ready(service: Service) -> Health:
    if not await service.database.ready():
        raise QueueError(503, "database_unavailable", "The queue database is unavailable")
    return Health(status="ready")


@router.post(
    "/api/jobs",
    response_model=CreateResult,
    status_code=status.HTTP_201_CREATED,
    tags=["jobs"],
)
async def create_job(
    request: JobCreate,
    response: Response,
    service: Service,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CreateResult:
    if idempotency_key is not None and not idempotency_pattern.fullmatch(idempotency_key):
        raise QueueError(
            400,
            "invalid_idempotency_key",
            "Idempotency-Key must contain 1-128 visible ASCII characters",
        )
    job, was_deduplicated = await service.create_job(request, idempotency_key)
    if was_deduplicated:
        response.status_code = status.HTTP_200_OK
    return CreateResult(job=job, deduplicated=was_deduplicated)


@router.get("/api/jobs", response_model=JobPage, tags=["jobs"])
async def list_jobs(
    service: Service,
    queue: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> JobPage:
    jobs = await service.list_jobs(
        queue=queue, status=job_status.value if job_status else None, limit=limit
    )
    return JobPage(jobs=jobs, count=len(jobs))


@router.get("/api/jobs/{job_id}", response_model=MutationResult, tags=["jobs"])
async def get_job(job_id: str, service: Service) -> MutationResult:
    return MutationResult(job=await service.get_job(job_id))


@router.get("/api/jobs/{job_id}/events", response_model=EventPage, tags=["jobs"])
async def get_events(job_id: str, service: Service) -> EventPage:
    return EventPage(events=await service.get_events(job_id))


@router.post(
    "/api/jobs/claim",
    response_model=LeaseGrant,
    responses={204: {"description": "No eligible job is available"}},
    tags=["workers"],
)
async def claim_job(request: ClaimRequest, service: Service) -> LeaseGrant | Response:
    result = await service.claim(
        queue=request.queue,
        worker_id=request.worker_id,
        lease_seconds=request.lease_seconds,
    )
    if result is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    job, token = result
    return LeaseGrant(job=job, lease_token=token)


@router.post("/api/jobs/{job_id}/heartbeat", response_model=MutationResult, tags=["workers"])
async def heartbeat_job(job_id: str, request: HeartbeatCommand, service: Service) -> MutationResult:
    job = await service.heartbeat(job_id, request.lease_token, request.lease_seconds)
    return MutationResult(job=job)


@router.post("/api/jobs/{job_id}/complete", response_model=MutationResult, tags=["workers"])
async def complete_job(job_id: str, request: LeaseCommand, service: Service) -> MutationResult:
    return MutationResult(job=await service.complete(job_id, request.lease_token))


@router.post("/api/jobs/{job_id}/fail", response_model=MutationResult, tags=["workers"])
async def fail_job(job_id: str, request: FailCommand, service: Service) -> MutationResult:
    return MutationResult(job=await service.fail(job_id, request.lease_token, request.error))


@router.post("/api/jobs/{job_id}/redrive", response_model=MutationResult, tags=["jobs"])
async def redrive_job(job_id: str, service: Service) -> MutationResult:
    return MutationResult(job=await service.redrive(job_id))


@router.post("/api/jobs/{job_id}/cancel", response_model=MutationResult, tags=["jobs"])
async def cancel_job(job_id: str, service: Service) -> MutationResult:
    return MutationResult(job=await service.cancel(job_id))


@router.get("/api/stats", response_model=QueueStats, tags=["operations"])
async def stats(service: Service) -> QueueStats:
    return QueueStats.model_validate(await service.stats())


@router.get("/metrics", response_class=PlainTextResponse, tags=["operations"])
async def metrics(service: Service) -> PlainTextResponse:
    values = await service.stats()
    lines = [
        "# HELP leasequeue_jobs Current jobs by state.",
        "# TYPE leasequeue_jobs gauge",
    ]
    for name in ("queued", "running", "retry", "completed", "dead", "cancelled"):
        lines.append(f'leasequeue_jobs{{status="{name}"}} {values[name]}')
    lines.extend(
        [
            "# HELP leasequeue_jobs_total Total jobs retained by the service.",
            "# TYPE leasequeue_jobs_total gauge",
            f"leasequeue_jobs_total {values['total']}",
        ]
    )
    if values["oldest_pending_seconds"] is not None:
        lines.extend(
            [
                "# HELP leasequeue_oldest_pending_seconds Age of the oldest pending job.",
                "# TYPE leasequeue_oldest_pending_seconds gauge",
                f"leasequeue_oldest_pending_seconds {values['oldest_pending_seconds']}",
            ]
        )
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
