import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from leasequeue.database import Database
from leasequeue.schemas import JobCreate
from leasequeue.service import QueueError, QueueService, iso_time


@pytest.fixture
async def service(tmp_path: Path) -> QueueService:
    database = Database(tmp_path / "service.db")
    await database.initialize()
    return QueueService(database)


def request(*, max_attempts: int = 3, delay_seconds: int = 0) -> JobCreate:
    return JobCreate(
        queue="events",
        task="projection.refresh",
        payload={"aggregateId": "order-17"},
        max_attempts=max_attempts,
        delay_seconds=delay_seconds,
        retry_base_seconds=1,
    )


async def test_atomic_claim_grants_one_lease(service: QueueService) -> None:
    await service.create_job(request(), "atomic-claim")
    results = await asyncio.gather(
        service.claim(queue="events", worker_id="worker-a", lease_seconds=30),
        service.claim(queue="events", worker_id="worker-b", lease_seconds=30),
    )

    grants = [item for item in results if item is not None]
    assert len(grants) == 1
    assert grants[0][0]["attempt"] == 1


async def test_delayed_job_is_not_claimed_early(service: QueueService) -> None:
    await service.create_job(request(delay_seconds=600), "delayed")
    assert await service.claim(queue="events", worker_id="worker-a", lease_seconds=30) is None


async def test_expired_lease_is_recovered_by_next_claim(service: QueueService) -> None:
    job, _ = await service.create_job(request(), "lease-expiry")
    first = await service.claim(queue="events", worker_id="worker-a", lease_seconds=30)
    assert first is not None

    expired_at = iso_time(datetime.now(UTC) - timedelta(seconds=1))
    async with service.database.connect() as connection:
        await connection.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (expired_at, job["id"])
        )
        await connection.commit()

    second = await service.claim(queue="events", worker_id="worker-b", lease_seconds=30)
    assert second is not None
    assert second[0]["id"] == job["id"]
    assert second[0]["attempt"] == 2
    assert second[0]["lease_owner"] == "worker-b"
    assert "lease_expired" in [event["event_type"] for event in await service.get_events(job["id"])]


async def test_retry_backoff_and_state_guards(service: QueueService) -> None:
    job, _ = await service.create_job(request(), "retry-case")
    grant = await service.claim(queue="events", worker_id="worker-a", lease_seconds=30)
    assert grant is not None
    failed = await service.fail(job["id"], grant[1], "Temporary fault")
    assert failed["status"] == "retry"
    assert failed["available_at"] > failed["updated_at"]

    with pytest.raises(QueueError, match="not found") as error:
        await service.cancel("missing-job")
    assert error.value.status_code == 404

    with pytest.raises(QueueError, match="dead-lettered"):
        await service.redrive(job["id"])
