from typing import Any

import pytest
from app.api.routes.health import _check_database, _check_redis


class DownDatabase:
    async def execute(self, statement: object) -> None:
        raise ConnectionError("database unavailable")


class DownRedis:
    async def ping(self) -> bool:
        raise ConnectionError("redis unavailable")


@pytest.mark.asyncio
async def test_readiness_probes_report_independent_failures() -> None:
    import asyncio

    # The doubles imitate a closed session and a dead Redis, so they are annotated as
    # ``Any`` rather than pretending to satisfy AsyncSession/Redis structurally.
    database: Any = DownDatabase()
    redis: Any = DownRedis()
    result = await asyncio.gather(_check_database(database), _check_redis(redis))

    assert result == [False, False]
