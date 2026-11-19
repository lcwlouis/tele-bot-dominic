import asyncio

import pytest

from tghelpers import OWNER


@pytest.mark.asyncio
async def test_job_runs_once_per_due_local_day_and_survives_restart(make_app):
    from dom.jobs import DailyJobs

    app = make_app()
    app.config = app.config.model_copy(update={"timezone": "Asia/Singapore"})
    clock = app.dispatch.now
    clock.advance(hours=3)
    ran = []

    async def run():
        ran.append(clock())

    def register():
        jobs = DailyJobs(app, clock)
        jobs.register("example", lambda now, habits: now.hour >= 12, run)
        return jobs

    jobs = register()
    await jobs.tick()
    await jobs.tick()
    await register().tick()
    assert len(ran) == 1
    assert await app.store.kv_get("job:example:last") == clock().date().isoformat()

    clock.advance(hours=1)
    await jobs.tick()
    assert len(ran) == 1
    clock.advance(hours=12)
    await jobs.tick()
    assert len(ran) == 2


@pytest.mark.asyncio
async def test_job_failure_retries_only_three_times_per_day(make_app, caplog):
    from dom.jobs import DailyJobs

    app = make_app()
    clock = app.dispatch.now
    attempts = []

    async def fail():
        attempts.append(clock())
        raise RuntimeError("example failure")

    for _ in range(6):
        jobs = DailyJobs(app, clock)
        jobs.register("fail", lambda now, habits: True, fail)
        await jobs.tick()
    assert len(attempts) == 4
    assert "example failure" in caplog.text
    assert await app.store.kv_get("job:fail:last") is None

    clock.advance(days=1)
    await jobs.tick()
    assert len(attempts) == 5


@pytest.mark.asyncio
async def test_job_ticks_never_overlap(make_app):
    from dom.jobs import DailyJobs

    app = make_app(OWNER)
    jobs = DailyJobs(app, app.dispatch.now)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def first():
        calls.append("first")
        entered.set()
        await release.wait()
        calls.append("finished")

    async def second():
        calls.append("second")

    jobs.register("first", lambda now, habits: True, first)
    jobs.register("second", lambda now, habits: True, second)
    task = asyncio.create_task(jobs.tick())
    await entered.wait()
    concurrent = asyncio.create_task(jobs.tick())
    await asyncio.sleep(0)
    assert calls == ["first"]
    release.set()
    await asyncio.gather(task, concurrent)
    assert calls == ["first", "finished", "second"]
