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

    result = await asyncio.gather(_check_database(DownDatabase()), _check_redis(DownRedis()))

    assert result == [False, False]
