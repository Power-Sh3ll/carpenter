from carpenter import Registry, Job, Blueprint
import time

registry_settings = {
    "max_cpus": 4,
    "keep_jobs": True,
    "max_memory": 2048,
    # Shut the registry down once nothing has been running for this long. 0 means
    # "as soon as the last job finishes", which is what a one-shot script wants.
    "terminate_behavior": "on_idle",
    "idle_time": 20,
}

blueprint = Blueprint(["python", "-u", "task.py"])

with Registry(registry_settings, blueprint) as reg:
    for x in range(100):
        new_job = Job(f"job_{x}")
        reg.register_job(new_job)
        reg.start_job(new_job)
        wait_time = 0.1

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