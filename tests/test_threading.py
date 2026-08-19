"""
The registry is documented as safe to drive from more than one thread, which is
the case a web server puts it in: request handlers submitting while the monitor
dispatches and reaps. These are the regression tests for that claim.
"""

import threading
import time

from carpenter import Job


def test_get_job_by_name_during_reaping(registry, instant):
    """
    Regression test for scanning _registry without the lock. With the default
    keep_jobs of False the monitor pops reaped jobs on every sweep, and an
    unsynchronised scan raises RuntimeError: dictionary changed size during
    iteration in whichever thread happened to be looking.
    """
    reg = registry({"keep_jobs": False, "poll_interval": 0.01}, instant)
    errors = []
    stop = threading.Event()

    def submit_forever():
        i = 0
        while not stop.is_set():
            try:
                reg.submit_job(Job(f"job_{i}"))
            except Exception as exc:  # noqa: BLE001 - the point is to catch anything
                errors.append(exc)
                return
            i += 1

    def look_forever():
        while not stop.is_set():
            try:
                reg.get_job(name="job_5")
                reg.active_jobs()
                reg.queued_jobs()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [
        threading.Thread(target=submit_forever, daemon=True),
        threading.Thread(target=look_forever, daemon=True),
        threading.Thread(target=look_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    time.sleep(2)
    stop.set()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []


def test_concurrent_submission_never_exceeds_the_limit(registry, sleeper):
    """
    Selection and spawning have to be atomic. If they are not, two threads both
    see the same free slot and the registry runs over its own limit.
    """
    reg = registry({"max_jobs": 3, "keep_jobs": True}, sleeper(2))
    overruns = []
    barrier = threading.Barrier(8)

    def submit(index):
        barrier.wait()
        reg.submit_job(Job(f"job_{index}"))
        running = len(reg.running_jobs())
        if running > 3:
            overruns.append(running)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert overruns == []
    assert len(reg.running_jobs()) == 3
    assert len(reg.queued_jobs()) == 5


def test_control_methods_do_not_race_the_monitor(registry, sleeper):
    reg = registry({"keep_jobs": True, "poll_interval": 0.01}, sleeper(0.3))
    errors = []

    def churn():
        for i in range(20):
            try:
                job = reg.submit_job(Job(f"churn_{i}"))
                if job.process is not None:
                    reg.stop_job(job)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [threading.Thread(target=churn) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
