from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from leasequeue.config import Settings
from leasequeue.main import create_app


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(database_path=tmp_path / "test.db"))
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client,
    ):
        yield test_client


@pytest.fixture
def job_request() -> dict[str, object]:
    return {
        "queue": "billing",
        "task": "invoice.generate",
        "payload": {"invoiceId": "inv_481", "format": "pdf"},
        "priority": 10,
        "maxAttempts": 3,
        "retryBaseSeconds": 1,
    }
