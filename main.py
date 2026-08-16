from carpenter import Blueprint, Job, Registry

registry_settings = {
    "max_cpus": 4,
    "keep_jobs": True,
    "max_memory": 2048
}

blueprint = Blueprint(["python", "-u", "task.py"])  # -u added here
reg = Registry(registry_settings, default_blueprint=blueprint)

for x in range(5):
    job_name = f"job_{x}"
    new_job = Job(job_name)
    reg.register_job(new_job)
    print(f"Registered {new_job.name} with ID {new_job.id}")
    reg.start_job(new_job)
