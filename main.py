import threading
import time

from carpenter import Blueprint, Job, Registry

registry_settings = {
    "max_cpus": 4,
    "keep_jobs": True,
    "max_memory": 2048,
    # Shut the registry down once nothing has been running for this long. 0 means
    # "as soon as the last job finishes", which is what a one-shot script wants.
    "terminate_behavior": "on_idle",
    "idle_time": 20,
}

blueprint = Blueprint(["python", "-u", "task.py"])  # -u added here

# Jobs that turn up after the registry is already running, as (seconds from
# start, name). This stands in for a web server's request handlers dropping work
# into a live registry: the feeder runs on its own thread, so it goes through the
# same locking a real handler would, and a job arriving during the idle window
# resets the countdown instead of letting the registry shut down.
ARRIVALS = [(5, "late_job_0"), (15, "late_job_1"), (30, "late_job_2")]


def feed_jobs(reg, arrivals):
    """Drip jobs into a running registry on a schedule."""
    start = time.monotonic()
    for delay, name in arrivals:
        time.sleep(max(0.0, start + delay - time.monotonic()))
        job = Job(name)
        try:
            reg.register_job(job)
            reg.start_job(job)
        except RuntimeError:
            # The idle window elapsed and the registry shut down before this one
            # arrived, which is the correct outcome, not an error to report.
            return


# Leaving the with-block applies terminate_behavior: under "on_idle" it waits
# for the registry's own idle window to elapse before tearing it down.
with Registry(registry_settings, default_blueprint=blueprint) as reg:
    for x in range(1):
        job_name = f"job_{x}"
        new_job = Job(job_name)
        reg.register_job(new_job)
        reg.start_job(new_job)

    threading.Thread(target=feed_jobs, args=(reg, ARRIVALS), daemon=True).start()

    # Live view. poll_jobs() is the call that moves a job out of "started", so
    # the loop drives it itself rather than depending on when the monitor thread
    # last ticked. Running until is_shutdown means the idle counter stays on
    # screen through the whole idle_time window, not just while jobs are up.
    while not reg.is_shutdown:
        reg.poll_jobs()
        active = reg.active_jobs()
        # Anything printed before this is wiped by the redraw, and the counts it
        # used to print by hand are in the table's own footer now.
        reg.print_registry(clear=True)
        # Under "manual" nothing else will ever flip is_shutdown, so the drained
        # registry is this loop's own stopping point.
        if not active and reg.terminate_behavior == "manual":
            break
        # Nothing can change faster than the registry polls, so printing more
        # often than poll_interval just reprints the same table.
        time.sleep(max(1.0, reg.poll_interval))
