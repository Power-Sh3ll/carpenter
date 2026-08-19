"""
Output capture, with particular attention to short lived jobs. A job that exits
before the registry has finished setting it up is where the drain threads and
the reaper race each other.
"""

import sys

from carpenter import Blueprint, Job


def test_drains_are_registered_before_the_job_is_published_as_started(registry, instant, monkeypatch):
    """
    The invariant that closes the reaper race, asserted directly.

    A job becomes visible to the monitor when its status turns "started". If the
    drain threads are registered after that, the monitor can reap a short lived
    job first and join a drain list that is empty or half built, which loses the
    captured output and can raise outright. Racing for that reproduces perhaps
    one run in five, so the ordering itself is what gets tested rather than the
    symptom.
    """
    reg = registry({"keep_jobs": True}, instant)
    observed = {}
    original = reg._start_drains

    def spy(job):
        observed["status"] = job.status
        result = original(job)
        observed["drains"] = len(job._drains)
        return result

    monkeypatch.setattr(reg, "_start_drains", spy)
    reg.submit_job(Job("job"))

    assert observed["status"] != "started"
    assert observed["drains"] == 2


def test_output_of_an_instant_job_is_not_lost(registry):
    """
    The symptom the ordering above prevents, exercised against real processes.
    A job this short is reliably finished by the time the next monitor sweep
    runs, so any gap between "started" and "draining" swallows the output.
    """
    blueprint = Blueprint([sys.executable, "-c", "print('hello from the job')"])
    reg = registry({"keep_jobs": True, "poll_interval": 0.01}, blueprint)

    jobs = [reg.submit_job(Job(f"job_{i}")) for i in range(20)]
    assert reg.wait_for_jobs(timeout=30)

    for job in jobs:
        stdout, _ = job.output()
        assert stdout.strip() == "hello from the job", job.name


def test_output_of_a_queued_job_is_captured_once_it_runs(registry):
    blueprint = Blueprint([sys.executable, "-c", "print('queued then run')"])
    reg = registry({"max_jobs": 1, "keep_jobs": True, "poll_interval": 0.01}, blueprint)

    jobs = [reg.submit_job(Job(f"job_{i}")) for i in range(5)]
    assert any(job.status == "queued" for job in jobs)
    assert reg.wait_for_jobs(timeout=30)

    for job in jobs:
        stdout, _ = job.output()
        assert stdout.strip() == "queued then run", job.name


def test_stderr_is_captured_separately(registry):
    blueprint = Blueprint(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"]
    )
    reg = registry({"keep_jobs": True}, blueprint)

    job = reg.submit_job(Job("job"))
    assert reg.wait_for_jobs(timeout=30)

    stdout, stderr = job.output()
    assert stdout.strip() == "out"
    assert stderr.strip() == "err"


def test_discard_mode_captures_nothing(registry):
    blueprint = Blueprint([sys.executable, "-c", "print('noisy')"])
    reg = registry({"keep_jobs": True, "output_mode": "discard"}, blueprint)

    job = reg.submit_job(Job("job"))
    assert reg.wait_for_jobs(timeout=30)

    assert job.output() == ("", "")
    assert job.status == "finished"


def test_file_mode_writes_the_output(registry, tmp_path):
    blueprint = Blueprint([sys.executable, "-c", "print('to a file')"])
    reg = registry(
        {"keep_jobs": True, "output_mode": "file", "output_dir": str(tmp_path)},
        blueprint,
    )

    job = reg.submit_job(Job("job"))
    assert reg.wait_for_jobs(timeout=30)

    with open(job.stdout_path) as handle:
        assert handle.read().strip() == "to a file"
