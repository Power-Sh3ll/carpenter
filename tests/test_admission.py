"""
Admission control: what the registry accepts, what it starts, and what it makes
wait. These drive poll_jobs() by hand rather than waiting on the monitor thread,
so the outcomes are decided by the dispatcher rather than by timing.
"""

import time

import pytest

from carpenter import Job


def test_jobs_start_immediately_when_no_limit_is_set(registry, sleeper):
    reg = registry({"keep_jobs": True}, sleeper(5))

    jobs = [reg.submit_job(Job(f"job_{i}")) for i in range(5)]

    assert [job.status for job in jobs] == ["started"] * 5
    assert reg.available_slots() is None


def test_jobs_past_the_limit_are_queued(registry, sleeper):
    reg = registry({"max_jobs": 2, "keep_jobs": True}, sleeper(5))

    jobs = [reg.submit_job(Job(f"job_{i}")) for i in range(5)]

    assert [job.status for job in jobs] == ["started", "started", "queued", "queued", "queued"]
    assert reg.available_slots() == 0
    assert [job.name for job in reg.queued_jobs()] == ["job_2", "job_3", "job_4"]


def test_a_queued_job_has_no_process(registry, sleeper):
    reg = registry({"max_jobs": 1}, sleeper(5))
    reg.submit_job(Job("first"))
    waiting = reg.submit_job(Job("second"))

    assert waiting.process is None
    assert waiting.run == 0
    assert waiting.start_time is None
    assert waiting.submit_time is not None


def test_queued_jobs_count_as_active(registry, sleeper):
    """
    The bug this exists to prevent: an on_idle registry that treats a backlog as
    idleness shuts itself down with work still pending.
    """
    reg = registry({"max_jobs": 1, "terminate_behavior": "on_idle", "idle_time": 0}, sleeper(5))
    reg.submit_job(Job("running"))
    queued = reg.submit_job(Job("waiting"))

    assert queued.is_active()
    assert queued in reg.active_jobs()
    assert reg.idle_seconds() == 0.0
    assert reg.should_terminate() is False


def test_a_completion_starts_the_next_queued_job(registry, sleeper, instant):
    reg = registry({"max_jobs": 1, "keep_jobs": True}, sleeper(5))

    first = reg.submit_job(Job("first", blueprint=instant))
    second = reg.submit_job(Job("second"))

    assert second.status == "queued"

    # Wait on the process itself rather than on the clock, so the sweep below is
    # guaranteed to find it finished.
    first.process.wait()
    reg.poll_jobs()

    assert first.status == "finished"
    assert second.status == "started"


def test_the_backlog_drains_in_submission_order(registry, instant):
    reg = registry({"max_jobs": 1, "keep_jobs": True}, instant)

    jobs = [reg.submit_job(Job(f"job_{i}")) for i in range(4)]
    assert reg.wait_for_jobs(timeout=30)

    assert [job.status for job in jobs] == ["finished"] * 4
    starts = [job.start_time for job in jobs]
    assert starts == sorted(starts)


def test_wait_for_jobs_waits_for_the_whole_backlog(registry, instant):
    reg = registry({"max_jobs": 1, "keep_jobs": True}, instant)
    jobs = [reg.submit_job(Job(f"job_{i}")) for i in range(3)]

    assert reg.wait_for_jobs(timeout=30)
    assert reg.queued_jobs() == []
    assert all(job.is_finished() for job in jobs)


def test_paused_jobs_keep_holding_their_slot(registry, sleeper):
    reg = registry({"max_jobs": 1}, sleeper(5))
    running = reg.submit_job(Job("running"))
    queued = reg.submit_job(Job("waiting"))

    reg.pause_job(running)
    reg.poll_jobs()

    assert running.status == "paused"
    assert queued.status == "queued"
    assert reg.available_slots() == 0


def test_the_monitor_dispatches_without_any_help(registry, instant):
    """
    The tick trigger on its own, with nothing calling poll_jobs() by hand.
    """
    reg = registry({"max_jobs": 1, "keep_jobs": True, "poll_interval": 0.05}, instant)
    jobs = [reg.submit_job(Job(f"job_{i}")) for i in range(3)]

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not all(job.is_finished() for job in jobs):
        time.sleep(0.05)

    assert all(job.is_finished() for job in jobs), [job.status for job in jobs]


def test_submitting_a_running_job_raises(registry, sleeper):
    reg = registry({}, sleeper(5))
    job = reg.submit_job(Job("job"))

    with pytest.raises(RuntimeError, match="already running"):
        reg.submit_job(job)


def test_submitting_a_queued_job_raises(registry, sleeper):
    reg = registry({"max_jobs": 1}, sleeper(5))
    reg.submit_job(Job("first"))
    queued = reg.submit_job(Job("second"))

    with pytest.raises(RuntimeError, match="already queued"):
        reg.submit_job(queued)


def test_submitting_after_shutdown_raises(registry, instant):
    reg = registry({}, instant)
    reg.shutdown(grace=0)

    with pytest.raises(RuntimeError, match="shut down"):
        reg.submit_job(Job("late"))


def test_a_job_with_no_blueprint_anywhere_raises(registry):
    reg = registry({})

    with pytest.raises(ValueError, match="no blueprint"):
        reg.submit_job(Job("job"))


def test_submit_job_returns_the_job(registry, instant):
    reg = registry({"keep_jobs": True}, instant)
    job = Job("job")
    assert reg.submit_job(job) is job
