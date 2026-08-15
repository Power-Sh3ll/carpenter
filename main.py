import carpenter.registry as registry
import carpenter.job as job
import subprocess
import time

registry_settings = {
    "max_cpus": 4,
    "keep_jobs": True,
    "max_memory": 2048
}

reg = registry.registry(registry_settings)

job_blueprint = subprocess.Popen(
    ["python", "-u", "task.py"],   # -u added here
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    stdin=subprocess.PIPE,
    text=True,
)

for x in range(5):
    job_name = f"job_{x}"
    new_job = job.job(job_name)
    new_job.process = job_blueprint
    reg.register_job(new_job)
    print(f"Registered {new_job.name} with ID {new_job.id}")
    reg.start_job(new_job)