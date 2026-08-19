"""
What happens to a job once the registry has it: withdrawal, resubmission, and
the failure modes that only exist now that the registry does the spawning.
"""

import pytest

from carpenter import Job


def test_cancelling_a_queued_job(registry, sleeper):
    reg = registry({"max_jobs": 1, "keep_jobs": True}, sleeper(5))
    reg.submit_job(Job("running"))
    queued = reg.submit_job(Job("waiting"))

    reg.cancel_job(queued)

    assert queued.status == "cancelled"
    assert queued.is_finished()
    assert not queued.is_active()
    assert queued.process is None
    assert queued.run == 0
    assert reg.queued_jobs() == []


def test_cancelling_stops_the_wait_clock(registry, sleeper):
    reg = registry({"max_jobs": 1, "keep_jobs": True}, sleeper(5))
    reg.submit_job(Job("running"))
    queued = reg.submit_job(Job("waiting"))

    reg.cancel_job(queued)
    waited = queued.waited()

    assert waited is not None
    assert queued.waited() == waited


def test_a_cancelled_job_is_dropped_when_keep_jobs_is_false(registry, sleeper):
    reg = registry({"max_jobs": 1, "keep_jobs": False}, sleeper(5))
    reg.submit_job(Job("running"))
    queued = reg.submit_job(Job("waiting"))

    reg.cancel_job(queued)

    assert reg.get_job(id=queued.id) is None


def test_cancelling_a_running_job_raises(registry, sleeper):
    reg = registry({}, sleeper(5))
    job = reg.submit_job(Job("job"))

    with pytest.raises(RuntimeError, match="not queued"):
        reg.cancel_job(job)


def test_stopping_a_queued_job_points_at_cancel(registry, sleeper):
    reg = registry({"max_jobs": 1}, sleeper(5))
    reg.submit_job(Job("running"))
    queued = reg.submit_job(Job("waiting"))

    with pytest.raises(RuntimeError, match="cancel_job"):
        reg.stop_job(queued)


def test_cancelling_frees_nothing_because_it_held_nothing(registry, sleeper):
    reg = registry({"max_jobs": 1, "keep_jobs": True}, sleeper(5))
    running = reg.submit_job(Job("running"))
    first = reg.submit_job(Job("first_in_line"))
    second = reg.submit_job(Job("second_in_line"))

    reg.cancel_job(first)
    reg.poll_jobs()

    assert running.status == "started"
    assert second.status == "queued"


def test_a_failed_spawn_marks_only_its_own_job(registry, unspawnable, instant):
    reg = registry({"keep_jobs": True}, instant)

    bad = reg.submit_job(Job("bad", blueprint=unspawnable))
    good = reg.submit_job(Job("good"))

    assert bad.status == "failed"
    assert isinstance(bad.error, OSError)
    assert bad.exit_code is None
    assert good.status == "started"


def test_a_failed_spawn_does_not_consume_a_slot(registry, unspawnable, sleeper):
    reg = registry({"max_jobs": 1, "keep_jobs": True}, sleeper(5))

    bad = reg.submit_job(Job("bad", blueprint=unspawnable))
    good = reg.submit_job(Job("good"))

    assert bad.status == "failed"
    assert good.status == "started"


def test_a_failed_spawn_leaves_the_monitor_alive(registry, unspawnable, instant):
    reg = registry({"keep_jobs": True, "poll_interval": 0.05}, instant)
    reg.submit_job(Job("bad", blueprint=unspawnable))

    good = reg.submit_job(Job("good"))
    assert reg.wait_for_jobs(timeout=30)

    assert good.status == "finished"
    assert reg._monitor.is_alive()


def test_resubmitting_a_finished_job_runs_it_again(registry, instant):
    reg = registry({"keep_jobs": True}, instant)

    job = reg.submit_job(Job("job"))
    assert reg.wait_for_jobs(timeout=30)
    first_run_started = job.start_time

    reg.submit_job(job)
    assert reg.wait_for_jobs(timeout=30)

    assert job.status == "finished"
    assert job.run == 2
    assert job.start_time > first_run_started


def test_resubmitting_keeps_the_job_id(registry, instant):
    reg = registry({"keep_jobs": True}, instant)

    job = reg.submit_job(Job("job"))
    original_id = job.id
    assert reg.wait_for_jobs(timeout=30)

    reg.submit_job(job)
    assert job.id == original_id


def test_resubmitting_clears_the_previous_result(registry, failing, instant):
    reg = registry({"keep_jobs": True}, failing)

    job = reg.submit_job(Job("job"))
    assert reg.wait_for_jobs(timeout=30)
    assert job.status == "failed"
    assert job.exit_code == 3

    job.blueprint = instant
    reg.submit_job(job)
    assert reg.wait_for_jobs(timeout=30)

    assert job.status == "finished"
    assert job.exit_code == 0
    assert job.error is None


def test_each_run_writes_its_own_log_files(registry, instant, tmp_path):
    """
    Log paths carry the run number because they are opened for writing. Without
    it a second run silently truncates the first run's logs.
    """
    reg = registry(
        {"keep_jobs": True, "output_mode": "file", "output_dir": str(tmp_path)},
        instant,
    )

    job = reg.submit_job(Job("job"))
    assert reg.wait_for_jobs(timeout=30)
    first_path = job.stdout_path

    reg.submit_job(job)
    assert reg.wait_for_jobs(timeout=30)

    assert job.stdout_path != first_path
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        f"job.{job.id}.1.stderr.log",
        f"job.{job.id}.1.stdout.log",
        f"job.{job.id}.2.stderr.log",
        f"job.{job.id}.2.stdout.log",
    ]


def test_shutdown_cancels_the_backlog(registry, sleeper):
    reg = registry({"max_jobs": 1, "keep_jobs": True}, sleeper(30))
    running = reg.submit_job(Job("running"))
    queued = reg.submit_job(Job("waiting"))

    reg.shutdown(grace=0)

    assert queued.status == "cancelled"
    assert running.is_finished()
    assert reg.active_jobs() == []


def test_a_job_that_never_ran_reports_no_duration(registry, sleeper):
    reg = registry({"max_jobs": 1, "keep_jobs": True}, sleeper(5))
    reg.submit_job(Job("running"))
    queued = reg.submit_job(Job("waiting"))

    assert queued.duration() is None
    assert queued.waited() is not None

    reg.cancel_job(queued)
    assert queued.duration() is None


def test_to_dict_reports_the_new_fields(registry, sleeper):
    reg = registry({"max_jobs": 1, "keep_jobs": True}, sleeper(5))
    reg.submit_job(Job("running"))
    queued = reg.submit_job(Job("waiting"))

    payload = queued.to_dict()
    assert payload["status"] == "queued"
    assert payload["run"] == 0
    assert payload["pid"] is None
    assert payload["error"] is None
    assert payload["submit_time"] is not None
    assert payload["waited"] is not None
