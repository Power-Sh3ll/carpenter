import os
import sys
import time

# So the demo runs from a clone without carpenter being installed first.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from carpenter import Registry, Job, Blueprint  # noqa: E402

registry_settings = {
    "max_cpus": 4,
    "keep_jobs": True,
    "max_memory": 2048,
    # Run at most eight at a time. The rest wait their turn rather than forking
    # all at once, and the monitor starts the next one each time a slot comes
    # free. Drop this setting to watch all of them fork at once instead, which
    # is what the registry did before it could meter anything.
    "max_jobs": 8,
    # Shut the registry down once nothing has been running for this long. 0 means
    # "as soon as the last job finishes", which is what a one-shot script wants.
    "terminate_behavior": "on_idle",
    "idle_time": 20,
}

# sys.executable rather than "python", so the demo runs under whichever
# interpreter started it, and an absolute path so it does not depend on the
# directory it was launched from.
blueprint = Blueprint([sys.executable, "-u", os.path.join(ROOT, "task.py")])

with Registry(registry_settings, blueprint) as reg:
    # All 32 are handed over up front. submit_job() accepts every one of them
    # immediately; what it does not do is start them, so the first table shows
    # eight running and twenty four queued. Each task.py run takes 5 to 30
    # seconds, so four batches of eight puts the whole demo at roughly a minute.
    for x in range(32):
        reg.submit_job(Job(f"job_{x}"))

    while not reg.is_shutdown:
        reg.poll_jobs()
        # Queued jobs count as active, so this stays true until the whole
        # backlog has been worked through, not just the four that are running.
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