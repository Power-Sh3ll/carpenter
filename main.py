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

# Leaving the with-block applies terminate_behavior: under "on_idle" it waits
# for the jobs to drain before tearing the registry down.
with Registry(registry_settings, default_blueprint=blueprint) as reg:
    for x in range(5):
        job_name = f"job_{x}"
        new_job = Job(job_name)
        reg.register_job(new_job)
        print(f"Registered {new_job.name} with ID {new_job.id}")
        reg.start_job(new_job)

    # Live view of the jobs while they run. poll_jobs() is the call that moves a
    # job out of "started", so the loop drives it itself rather than depending on
    # when the monitor thread last ticked. The check sits at the bottom so the
    # final, all-finished table is printed exactly once before falling through.
    while True:
        reg.poll_jobs()
        active = reg.active_jobs()
        print(f"\n--- {len(active)} running | idle {reg.idle_seconds():.1f}s ---")
        reg.print_registry()
        if not active:
            break
        # Nothing can change faster than the registry polls, so printing more
        # often than poll_interval just reprints the same table.
        time.sleep(max(1.0, reg.poll_interval))
