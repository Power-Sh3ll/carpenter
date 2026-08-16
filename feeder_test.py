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
# start, name, seconds to run before we kill it). This stands in for a web
# server's request handlers dropping work into a live registry: the feeder runs
# on its own thread, so it goes through the same locking a real handler would,
# and a job arriving during the idle window resets the countdown instead of
# letting the registry shut down.
#
# Two of these are here to show an edge rather than the happy path.
# self_terminated_job is killed five seconds in, standing in for an operator
# stopping a job by hand; it should end up "terminated" rather than "failed",
# and because super_late_job_3 is still running alongside it, it should not
# start the idle countdown either. super_duper_late_job_4 arrives long after the
# idle window has elapsed, so the registry should already be down and refuse it.
ARRIVALS = [
    (5, "late_job_0", None),
    (15, "late_job_1", None),
    (30, "late_job_2", None),
    (60, "super_late_job_3", None),
    (60, "self_terminated_job", 5),
    (120, "super_duper_late_job_4", None),
]


def terminate_if_running(reg, job):
    """
    Kill a job unless it got there on its own first. terminate_job() labels a job
    "terminated" unconditionally, so a timer that fires after the process has
    already exited would relabel a finished job as one we killed.
    """
    if job.is_active():
        reg.terminate_job(job)


def feed_jobs(reg, arrivals, rejected):
    """
    Drip jobs into a running registry on a schedule, recording the names of any
    that the registry turned away.
    """
    start = time.monotonic()
    for delay, name, terminate_after in arrivals:
        # Deadlines are absolute, measured from the feeder's own start, so a slow
        # iteration eats into the next gap rather than shifting everything after
        # it later and later.
        time.sleep(max(0.0, start + delay - time.monotonic()))

        job = Job(name)
        reg.register_job(job)
        try:
            reg.start_job(job)
        except RuntimeError:
            # The idle window elapsed and the registry shut down before this one
            # arrived. That is the outcome super_duper_late_job_4 exists to show,
            # so it is recorded and reported rather than treated as an error. The
            # loop continues so every remaining arrival is accounted for too.
            rejected.append(name)
            continue

        if terminate_after is not None:
            # On a timer rather than inline, so killing one job does not hold up
            # the arrival of the next.
            threading.Timer(terminate_after, terminate_if_running, args=(reg, job)).start()


# Leaving the with-block applies terminate_behavior: under "on_idle" it waits
# for the registry's own idle window to elapse before tearing it down.
with Registry(registry_settings, default_blueprint=blueprint) as reg:
    for x in range(1):
        job_name = f"job_{x}"
        new_job = Job(job_name)
        reg.register_job(new_job)
        reg.start_job(new_job)

    rejected = []
    feeder = threading.Thread(target=feed_jobs, args=(reg, ARRIVALS, rejected), daemon=True)
    feeder.start()

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

# The registry came down on its own idle schedule; nothing below holds that up.
# Joining the feeder only keeps the process alive long enough for the arrivals
# scheduled past the shutdown to be attempted and turned away. Without this the
# main thread would exit while they were still pending, and the daemon feeder
# would die with it, so they would never be attempted at all.
if feeder.is_alive():
    print("\nRegistry is down. Waiting on the remaining arrivals so they can be turned away.")
feeder.join()

# The live view above wipes the screen on every redraw, so this is the copy that
# survives. A rejected job still shows as "initialized": register_job() takes it,
# and only start_job() checks whether the registry is still up.
reg.print_registry()
if rejected:
    print(f"Refused after shutdown: {', '.join(rejected)}")
