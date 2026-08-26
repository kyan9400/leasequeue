from typing import Any

from httpx import AsyncClient


async def submit(
    client: AsyncClient, job_request: dict[str, object], key: str = "request-481"
) -> dict[str, Any]:
    response = await client.post("/api/jobs", json=job_request, headers={"Idempotency-Key": key})
    assert response.status_code == 201
    return response.json()


async def test_health_dashboard_and_security_headers(client: AsyncClient) -> None:
    live = await client.get("/health/live")
    ready = await client.get("/health/ready")
    dashboard = await client.get("/")
    javascript = await client.get("/static/app.js")

    assert live.json() == {"status": "ok"}
    assert ready.json() == {"status": "ready"}
    assert dashboard.status_code == 200
    assert "Work that survives the worker" in dashboard.text
    assert javascript.headers["content-type"].startswith("application/javascript")
    assert dashboard.headers["x-content-type-options"] == "nosniff"
    assert dashboard.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in dashboard.headers["content-security-policy"]


async def test_idempotent_submission_returns_original_job(
    client: AsyncClient, job_request: dict[str, object]
) -> None:
    first = await submit(client, job_request)
    second_response = await client.post(
        "/api/jobs", json=job_request, headers={"Idempotency-Key": "request-481"}
    )

    assert second_response.status_code == 200
    second = second_response.json()
    assert second["deduplicated"] is True
    assert second["job"]["id"] == first["job"]["id"]

    changed = {**job_request, "priority": 99}
    mismatch = await client.post(
        "/api/jobs", json=changed, headers={"Idempotency-Key": "request-481"}
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "idempotency_mismatch"


async def test_worker_can_claim_heartbeat_and_complete(
    client: AsyncClient, job_request: dict[str, object]
) -> None:
    created = await submit(client, job_request)
    claim = await client.post(
        "/api/jobs/claim",
        json={"queue": "billing", "workerId": "worker-01", "leaseSeconds": 30},
    )
    assert claim.status_code == 200
    lease = claim.json()
    assert lease["job"]["status"] == "running"
    assert lease["job"]["attempt"] == 1
    assert len(lease["leaseToken"]) >= 32

    heartbeat = await client.post(
        f"/api/jobs/{created['job']['id']}/heartbeat",
        json={"leaseToken": lease["leaseToken"], "leaseSeconds": 120},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["job"]["status"] == "running"

    rejected = await client.post(
        f"/api/jobs/{created['job']['id']}/complete",
        json={"leaseToken": "x" * 40},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "lease_rejected"

    complete = await client.post(
        f"/api/jobs/{created['job']['id']}/complete",
        json={"leaseToken": lease["leaseToken"]},
    )
    assert complete.status_code == 200
    assert complete.json()["job"]["status"] == "completed"
    assert complete.json()["job"]["leaseOwner"] is None

    job = await client.get(f"/api/jobs/{created['job']['id']}")
    assert "leaseToken" not in job.text
    events = (await client.get(f"/api/jobs/{created['job']['id']}/events")).json()
    assert [item["eventType"] for item in events["events"]] == [
        "submitted",
        "claimed",
        "heartbeat",
        "completed",
    ]


async def test_exhausted_job_enters_dead_letter_and_can_be_redriven(
    client: AsyncClient, job_request: dict[str, object]
) -> None:
    request = {**job_request, "maxAttempts": 1}
    created = await submit(client, request, "dead-letter-case")
    lease = (
        await client.post(
            "/api/jobs/claim",
            json={"queue": "billing", "workerId": "worker-02", "leaseSeconds": 30},
        )
    ).json()
    failed = await client.post(
        f"/api/jobs/{created['job']['id']}/fail",
        json={"leaseToken": lease["leaseToken"], "error": "Provider unavailable"},
    )
    assert failed.status_code == 200
    assert failed.json()["job"]["status"] == "dead"
    assert failed.json()["job"]["lastError"] == "Provider unavailable"

    redriven = await client.post(f"/api/jobs/{created['job']['id']}/redrive")
    assert redriven.status_code == 200
    assert redriven.json()["job"]["status"] == "queued"
    assert redriven.json()["job"]["attempt"] == 0


async def test_filters_cancellation_empty_claim_and_metrics(
    client: AsyncClient, job_request: dict[str, object]
) -> None:
    created = await submit(client, job_request, "cancel-case")
    cancelled = await client.post(f"/api/jobs/{created['job']['id']}/cancel")
    assert cancelled.json()["job"]["status"] == "cancelled"

    jobs = await client.get("/api/jobs", params={"queue": "billing", "status": "cancelled"})
    assert jobs.json()["count"] == 1
    no_work = await client.post(
        "/api/jobs/claim",
        json={"queue": "billing", "workerId": "worker-03", "leaseSeconds": 30},
    )
    assert no_work.status_code == 204

    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    assert 'leasequeue_jobs{status="cancelled"} 1' in metrics.text
    assert "leasequeue_jobs_total 1" in metrics.text


async def test_validation_errors_are_bounded_and_structured(
    client: AsyncClient, job_request: dict[str, object]
) -> None:
    invalid = await client.post(
        "/api/jobs",
        json={**job_request, "queue": "not a valid queue"},
        headers={"Idempotency-Key": "valid-key"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"

    invalid_key = await client.post(
        "/api/jobs", json=job_request, headers={"Idempotency-Key": "space is rejected"}
    )
    assert invalid_key.status_code == 400
    assert invalid_key.json()["error"]["code"] == "invalid_idempotency_key"

    large = await client.post(
        "/api/jobs",
        json={**job_request, "payload": {"content": "a" * 66_000}},
        headers={"Idempotency-Key": "large-payload"},
    )
    assert large.status_code == 413
    assert large.json()["error"]["code"] == "payload_too_large"
