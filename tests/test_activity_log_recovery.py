"""Activity log self-healing (2026-10-21 incident: a reset DB connection stopped
all logging for 26 hours while the MCP kept serving)."""

import asyncio

import pytest

from src import activity_log as al
from src.activity_log import ActivityLogger


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, *args):
        if self.pool.broken:
            raise ConnectionResetError("connection reset by peer")
        self.pool.rows.append(args)

    async def fetch(self, *a, **k):
        return [{"column_name": "partner"}, {"column_name": "partner_customer_id"}]


class FakePool:
    created = 0

    def __init__(self, broken=False):
        FakePool.created += 1
        self.broken, self.rows, self.terminated = broken, [], False

    def acquire(self, timeout=None):
        pool = self

        class Ctx:
            async def __aenter__(self):
                return FakeConn(pool)

            async def __aexit__(self, *a):
                return False
        return Ctx()

    def terminate(self):
        self.terminated = True


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(al.asyncio, "sleep", lambda s: real_sleep(0))
    FakePool.created = 0


async def _settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_broken_pool_is_rebuilt_and_logging_resumes(monkeypatch):
    pools = [FakePool(broken=True), FakePool()]
    log = ActivityLogger("postgresql://fake")

    async def new_pool():
        return pools.pop(0)
    monkeypatch.setattr(log, "_new_pool", new_pool)
    await log.ensure_started()
    first = log._pool
    for i in range(4):
        log.enqueue({"tool_name": f"t{i}"})
    await _settle(200)
    assert first.terminated and log._pool is not first
    assert log.stats()["pool_rebuilds"] == 1
    assert [r[4] for r in log._pool.rows] == ["t3"]   # entries after the rebuild are written
    assert log.stats()["inserted"] == 1 and log.stats()["failures"] == 3


@pytest.mark.asyncio
async def test_dead_writer_is_restarted(monkeypatch):
    log = ActivityLogger("postgresql://fake")

    async def new_pool():
        return FakePool()
    monkeypatch.setattr(log, "_new_pool", new_pool)
    await log.ensure_started()
    log._worker.cancel()
    await _settle()
    assert log._worker.done()
    await log.ensure_started()             # next request
    log.enqueue({"tool_name": "after"})
    await _settle()
    assert log.stats()["writer_restarts"] == 1
    assert log._pool.rows and log._pool.rows[-1][4] == "after"


@pytest.mark.asyncio
async def test_failed_init_is_retried_later(monkeypatch):
    log = ActivityLogger("postgresql://fake")
    attempts = []

    async def new_pool():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("db down")
        return FakePool()
    monkeypatch.setattr(log, "_new_pool", new_pool)
    await log.ensure_started()
    assert log.stats()["enabled"] is False
    await log.ensure_started()             # within the retry window: no hammering
    assert len(attempts) == 1
    log._init_retry_at = 0                 # window passed
    await log.ensure_started()
    assert log.stats()["enabled"] is True and len(attempts) == 2
