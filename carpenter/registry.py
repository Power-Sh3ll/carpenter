import os
import platform
import shutil
import signal
import subprocess
import time
import uuid

class Registry:
    def __init__(self, settings: dict, default_blueprint=None) -> None:
        """
        Create a registry with the given settings. The registry is responsible for managing jobs and their lifecycle.
        default_blueprint is used to start any registered job that doesn't carry its own blueprint.
        """
        self._registry = {}
        self.settings = settings
        self.default_blueprint = default_blueprint

        self.max_cpus = settings.get("max_cpus", 1)
        self.keep_jobs = settings.get("keep_jobs", False)
        self.max_memory = settings.get("max_memory", 1024)  # in MB

        # Validate settings
        if not isinstance(self.max_cpus, int) or self.max_cpus < 1:
            raise ValueError("max_cpus must be a positive integer")
        if not isinstance(self.keep_jobs, bool):
            raise ValueError("keep_jobs must be a boolean")
        if not isinstance(self.max_memory, int) or self.max_memory < 1:
            raise ValueError("max_memory must be a positive integer")

        # Validate that the settings are compatible with the system resources
        system_resources = self.get_system_resources()
        if self.max_cpus > system_resources["cpu_count"]:
            raise ValueError(f"max_cpus ({self.max_cpus}) exceeds available CPU count ({system_resources['cpu_count']})")
        if self.max_memory > system_resources["memory"]:
            raise ValueError(f"max_memory ({self.max_memory} MB) exceeds available system memory ({system_resources['memory']} MB)")

    def get_system_resources(self):
        """
        Get the current system resources (CPU count and memory) safely across platforms.
        """

        # 1. CPU Count
        sys_cpu_count = os.cpu_count()

        # 2. Memory Check (Platform-dependent)
        current_os = platform.system()
        sys_memory = 0.0

        if current_os == "Windows":
            try:
                # Query Windows management tools for total physical bytes
                out = subprocess.check_output(['wmic', 'ComputerSystem', 'get', 'TotalPhysicalMemory']).decode()
                # Extract just the numbers from the output string
                bytes_mem = int(''.join(filter(str.isdigit, out)))
                sys_memory = bytes_mem / (1024 ** 2)  # Convert to MB
            except Exception:
                sys_memory = 0.0
        else:
            # Linux and macOS fallback
            try:
                sys_memory = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / (1024 ** 2)
            except AttributeError:
                sys_memory = 0.0

        # 3. GPU Detection (Cross-platform fix using shutil)
        sys_has_gpu = False
        if shutil.which("nvidia-smi") is not None:
            sys_has_gpu = True
        elif current_os == "Linux" and (os.path.exists('/proc/driver/nvidia/version') or os.path.exists('/dev/nvidia0')):
            sys_has_gpu = True
            
        sys_gpu_type = "NVIDIA" if sys_has_gpu else "None"

        return {
            "cpu_count": sys_cpu_count, 
            "memory": sys_memory, 
            "has_gpu": sys_has_gpu, 
            "gpu_type": sys_gpu_type
        }



    def register_job(self, job):
        """
        adds a UUID to the job and adds it to the registry. The job is not started until the registry's start method is called.
        """
        job.id = uuid.uuid4()
        self._registry[job.id] = job
        
    def start_job(self, job):
        if job.process is not None and job.process.poll() is None:
            raise RuntimeError(f"job '{job.name}' is already running")

        blueprint = job.blueprint if job.blueprint is not None else self.default_blueprint
        if blueprint is None:
            raise ValueError(f"job '{job.name}' has no blueprint and registry has no default_blueprint")

        job.process = blueprint.spawn()
        job.status = "started"
        job.start_time = time.time()

    def pause_job(self, job):
        if job.process is None:
            raise RuntimeError(f"job '{job.name}' has not been started")
        if platform.system() == "Windows":
            raise NotImplementedError("pause/resume is not supported on Windows (no SIGSTOP/SIGCONT)")
        job.process.send_signal(signal.SIGSTOP)
        job.status = "paused"

    def resume_job(self, job):
        if job.process is None or job.status != "paused":
            raise RuntimeError(f"job '{job.name}' is not paused")
        job.process.send_signal(signal.SIGCONT)
        job.status = "started"

    def stop_job(self, job):
        if job.process is None:
            raise RuntimeError(f"job '{job.name}' has not been started")
        job.process.terminate()
        job.status = "stopped"

    def terminate_job(self, job):
        if job.process is None:
            raise RuntimeError(f"job '{job.name}' has not been started")
        job.process.kill()
        job.status = "terminated"

    def get_job(self, **kwargs):
        """
        Get a job from the registry by its ID or name.
        """
        if "id" in kwargs:
            return self._registry.get(kwargs["id"])
        elif "name" in kwargs:
            for job in self._registry.values():
                if job.name == kwargs["name"]:
                    return job
        return None

    def print_registry(self):
        """
        Print the current registry of jobs.
        """
        for job_id, job in self._registry.items():
            print(f"Job ID: {job_id}, Name: {job.name}, Status: {job.status}")