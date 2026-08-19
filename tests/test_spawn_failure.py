"""
Containment of failures at spawn time. The registry spawns on whichever thread
gets there first, which is often the monitor, so nothing here may escape: an
exception reaching the monitor loop kills it, and every job in the registry then
goes unreaped for the life of the process.
"""

import sys

from carpenter import Blueprint, Job


def test_a_drain_failure_does_not_escape(registry, instant, monkeypatch):
    """
    Starting a thread can fail in its own right, and two threads per job puts
    thread exhaustion within reach at a few hundred jobs. A drain that cannot
    start must fail its own job rather than the monitor.
    """
    reg = registry({"keep_jobs": True}, instant)

    def refuse(job):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(reg, "_start_drains", refuse)
    job = reg.submit_job(Job("job"))

    assert job.status == "failed"
    assert isinstance(job.error, RuntimeError)
    assert job.exit_code is None


def test_a_process_that_cannot_be_supervised_is_killed(registry, sleeper, monkeypatch):
    """
    The spawn succeeded but the setup after it did not, so there is a live
    process nobody is watching. It comes down rather than being left to run.
    """
    reg = registry({"keep_jobs": True}, sleeper(30))
    spawned = []

    def capture_then_refuse(job):
        spawned.append(job.process)
        raise RuntimeError("no threads for you")

    monkeypatch.setattr(reg, "_start_drains", capture_then_refuse)
    job = reg.submit_job(Job("job"))

    assert job.status == "failed"
    assert job.process is None
    assert len(spawned) == 1
    # The process the registry gave up on is not still running.
    assert spawned[0].poll() is not None


def test_the_registry_keeps_working_after_a_drain_failure(registry, instant, monkeypatch):
    reg = registry({"keep_jobs": True, "poll_interval": 0.05}, instant)

    original = reg._start_drains
    monkeypatch.setattr(reg, "_start_drains", lambda job: (_ for _ in ()).throw(RuntimeError("nope")))
    bad = reg.submit_job(Job("bad"))
    monkeypatch.setattr(reg, "_start_drains", original)

    good = reg.submit_job(Job("good"))
    assert reg.wait_for_jobs(timeout=30)

    assert bad.status == "failed"
    assert good.status == "finished"
    assert reg._monitor.is_alive()


def test_output_targets_failing_does_not_leave_a_process(registry, instant, monkeypatch):
    reg = registry({"keep_jobs": True}, instant)

    def refuse(job):
        raise OSError("no space left on device")

    monkeypatch.setattr(reg, "_output_targets", refuse)
    job = reg.submit_job(Job("job"))

    assert job.status == "failed"
    assert isinstance(job.error, OSError)
    assert job.process is None


def test_a_spawn_failure_still_counts_as_a_run(registry, unspawnable):
    """
    The run counter tracks attempts, not successes, so a retry after a failed
    spawn does not reuse the failed attempt's log filenames.
    """
    reg = registry({"keep_jobs": True}, unspawnable)
    job = reg.submit_job(Job("job"))

    assert job.status == "failed"
    assert job.run == 1

    reg.submit_job(job)
    assert job.run == 2


def test_devnull_stdin_gives_a_reading_job_eof(registry):
    """
    A supervised job has nobody at a console. Reading stdin returns EOF rather
    than blocking forever, which is what a PIPE nobody writes to would do.
    """
    blueprint = Blueprint(
        [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"]
    )
    reg = registry({"keep_jobs": True}, blueprint)

    job = reg.submit_job(Job("reader"))
    assert reg.wait_for_jobs(timeout=30)

    stdout, _ = job.output()
    assert stdout.strip() == "''"
    assert job.exit_code == 0
