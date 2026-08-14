import registry
import job
import subprocess
import time

registry_settings = {
    "max_cpus": 4,
    "keep_jobs": True,
    "max_memory": 2048
}

reg = registry.registry(registry_settings)

job1 = job.job("job1")
job1.process = subprocess.Popen(
    ["python", "-u", "task.py"],   # -u added here
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    stdin=subprocess.PIPE,
    text=True,
)

reg.register_job(job1)
reg.start_job(job1)

end_time = time.time() + 20
while time.time() < end_time:
    line = job1.process.stdout.readline()
    if line:
        print(f"[job1 output] {line.strip()}")

    if job1.process.poll() is not None:
        # process has exited — drain any remaining buffered lines
        for remaining_line in job1.process.stdout:
            print(f"[job1 output] {remaining_line.strip()}")
        print(f"[job1] finished with code {job1.process.poll()}")
        break

    time.sleep(0.1)