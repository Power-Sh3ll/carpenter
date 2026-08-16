import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from carpenter.job import TERMINAL_STATUSES

# How the registry decides it is done supervising.
#   "manual"  - never shuts itself down; the owning application calls
#               shutdown() when it is ready. Correct for a long-lived server.
#   "on_idle" - shuts itself down once it has had no active job for
#               idle_time seconds. idle_time=0 means "as soon as the last job
#               finishes", which is what a batch script usually wants.
TERMINATE_BEHAVIORS = ("manual", "on_idle")

# Where a job's stdout/stderr goes.
#   "capture" - piped and drained by the registry into job.stdout/job.stderr,
#               bounded by max_capture_bytes.
#   "file"    - written straight to output_dir by the OS. Cheapest option and
#               the safest for jobs with large or unbounded output.
#   "discard" - sent to os.devnull.
OUTPUT_MODES = ("capture", "file", "discard")


class Registry:
    def __init__(self, settings: dict, default_blueprint=None, on_shutdown=None) -> None:
        """
        Create a registry with the given settings. The registry is responsible for managing jobs and their lifecycle.
        default_blueprint is used to start any registered job that doesn't carry its own blueprint.

        on_shutdown, if given, is called as on_shutdown(registry, reason) once
        the registry has stopped supervising. It is the hook for whatever
        "shutting down" means in the host application: exiting a CLI, logging in
        a web server, releasing a resource pool.
        """
        self._registry = {}
        self.settings = settings
        self.default_blueprint = default_blueprint
        self.on_shutdown = on_shutdown

        self.max_cpus = settings.get("max_cpus", 1)
        self.keep_jobs = settings.get("keep_jobs", False)
        self.max_memory = settings.get("max_memory", 1024)  # in MB

        # Lifecycle settings
        self.terminate_behavior = settings.get("terminate_behavior", "manual")
        self.idle_time = settings.get("idle_time", None)
        self.poll_interval = settings.get("poll_interval", 1.0)
        self.shutdown_grace = settings.get("shutdown_grace", 10)

        # Output settings
        self.output_mode = settings.get("output_mode", "capture")
        self.output_dir = settings.get("output_dir", "job_logs")
        self.max_capture_bytes = settings.get("max_capture_bytes", 1024 * 1024)

        # Validate settings
        if not isinstance(self.max_cpus, int) or self.max_cpus < 1:
            raise ValueError("max_cpus must be a positive integer")
        if not isinstance(self.keep_jobs, bool):
            raise ValueError("keep_jobs must be a boolean")
        if not isinstance(self.max_memory, int) or self.max_memory < 1:
            raise ValueError("max_memory must be a positive integer")

        if self.terminate_behavior not in TERMINATE_BEHAVIORS:
            raise ValueError(f"terminate_behavior must be one of {TERMINATE_BEHAVIORS}")
        if self.terminate_behavior == "on_idle":
            if not isinstance(self.idle_time, (int, float)) or isinstance(self.idle_time, bool) or self.idle_time < 0:
                raise ValueError("terminate_behavior 'on_idle' requires idle_time to be a non-negative number of seconds")
        elif self.idle_time is not None:
            raise ValueError(f"idle_time is only meaningful for terminate_behavior 'on_idle', not '{self.terminate_behavior}'")
        if not isinstance(self.poll_interval, (int, float)) or self.poll_interval <= 0:
            raise ValueError("poll_interval must be a positive number of seconds")
        if not isinstance(self.shutdown_grace, (int, float)) or self.shutdown_grace < 0:
            raise ValueError("shutdown_grace must be a non-negative number of seconds")

        if self.output_mode not in OUTPUT_MODES:
            raise ValueError(f"output_mode must be one of {OUTPUT_MODES}")
        if not isinstance(self.max_capture_bytes, int) or self.max_capture_bytes < 1:
            raise ValueError("max_capture_bytes must be a positive integer")
        if self.output_mode == "file" and not isinstance(self.output_dir, str):
            raise ValueError("output_dir must be a string when output_mode is 'file'")

        # Validate that the settings are compatible with the system resources
        system_resources = self.get_system_resources()
        if self.max_cpus > system_resources["cpu_count"]:
            raise ValueError(f"max_cpus ({self.max_cpus}) exceeds available CPU count ({system_resources['cpu_count']})")
        if self.max_memory > system_resources["memory"]:
            raise ValueError(f"max_memory ({self.max_memory} MB) exceeds available system memory ({system_resources['memory']} MB)")

        # Supervision state. _lock guards _registry and the idle clock, since a
        # web server will be registering jobs from request threads while the
        # monitor thread reaps them.
        self._lock = threading.RLock()
        self._monitor = None
        self._stop_monitor = threading.Event()
        self._shutdown = False
        self._shutdown_event = threading.Event()
        self._last_busy = time.monotonic()
        self._created_at = time.monotonic()

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
        with self._lock:
            self._registry[job.id] = job

    def start_job(self, job):
        if job.process is not None and job.process.poll() is None:
            raise RuntimeError(f"job '{job.name}' is already running")

        blueprint = job.blueprint if job.blueprint is not None else self.default_blueprint
        if blueprint is None:
            raise ValueError(f"job '{job.name}' has no blueprint and registry has no default_blueprint")

        with self._lock:
            if self._shutdown:
                raise RuntimeError("registry has been shut down and cannot start new jobs")

            # A job object can be re-run, so clear any result from a previous run.
            job.end_time = None
            job.exit_code = None
            job.stdout = ""
            job.stderr = ""
            job.output_truncated = False
            job._drains = []

            stdout_target, stderr_target = self._output_targets(job)
            try:
                job.process = blueprint.spawn(stdout=stdout_target, stderr=stderr_target)
            finally:
                # In "file" mode the child holds its own dup of each descriptor,
                # so the parent's copies are closed straight away rather than
                # leaked for the lifetime of the job.
                for target in (stdout_target, stderr_target):
                    if hasattr(target, "close"):
                        target.close()

            job.status = "started"
            job.start_time = time.time()
            self._last_busy = time.monotonic()

        if self.output_mode == "capture":
            self._start_drains(job)

        # Supervision only matters once there is something to supervise, so the
        # monitor starts with the first job rather than in the constructor.
        self.start_monitor()

    def _output_targets(self, job):
        """
        Build the (stdout, stderr) targets for a spawn according to output_mode.
        """
        if self.output_mode == "discard":
            return subprocess.DEVNULL, subprocess.DEVNULL
        if self.output_mode == "file":
            os.makedirs(self.output_dir, exist_ok=True)
            job.stdout_path = os.path.join(self.output_dir, f"{job.name}.{job.id}.stdout.log")
            job.stderr_path = os.path.join(self.output_dir, f"{job.name}.{job.id}.stderr.log")
            return open(job.stdout_path, "w"), open(job.stderr_path, "w")
        return subprocess.PIPE, subprocess.PIPE

    def _start_drains(self, job):
        """
        Start one reader thread per pipe. A pipe nobody reads holds about 64KB
        before the OS blocks the writing child indefinitely, so in "capture"
        mode these threads are what keep a chatty job alive.
        """
        for attr, stream in (("stdout", job.process.stdout), ("stderr", job.process.stderr)):
            if stream is None:
                continue
            thread = threading.Thread(
                target=self._drain,
                args=(job, stream, attr),
                name=f"carpenter-drain-{job.name}-{attr}",
                daemon=True,
            )
            job._drains.append(thread)
            thread.start()

    def _drain(self, job, stream, attr):
        """
        Read one of a job's pipes to EOF, keeping at most max_capture_bytes.
        Reading continues past the cap so the child is never blocked by output
        we have decided to throw away.
        """
        try:
            for line in stream:
                with job._lock:
                    current = getattr(job, attr)
                    room = self.max_capture_bytes - len(current)
                    if room >= len(line):
                        setattr(job, attr, current + line)
                    else:
                        if room > 0:
                            setattr(job, attr, current + line[:room])
                        job.output_truncated = True
        except (ValueError, OSError):
            # Stream closed underneath us during shutdown; nothing left to read.
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _join_drains(self, job, timeout=5):
        """Wait for a finished job's reader threads to flush what they have left."""
        for thread in job._drains:
            thread.join(timeout=timeout)
        job._drains = []

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
        if job.status == "paused":
            # A SIGSTOPped process cannot act on SIGTERM until it is resumed.
            job.process.send_signal(signal.SIGCONT)
        job.process.terminate()
        job.status = "stopped"

    def terminate_job(self, job):
        if job.process is None:
            raise RuntimeError(f"job '{job.name}' has not been started")
        job.process.kill()
        job.status = "terminated"

    def poll_jobs(self):
        """
        Sweep every active job, recording the exit code and end time of any that
        have finished, and return the list of jobs that finished on this sweep.

        This is the only thing that moves a job out of "started", so something
        has to call it: the background monitor does so on every tick, and
        wait_for_jobs() does so while it blocks.
        """
        finished = []
        with self._lock:
            for job in list(self._registry.values()):
                # Jobs we stopped or killed are reaped too: they are already in a
                # terminal status, but their process still has to be collected or
                # it stays a zombie for the life of the parent.
                if job.process is None or job.exit_code is not None:
                    continue
                exit_code = job.process.poll()
                if exit_code is None:
                    continue
                self._finalize(job, exit_code)
                finished.append(job)

            if finished or any(job.is_active() for job in self._registry.values()):
                self._last_busy = time.monotonic()

        for job in finished:
            self._join_drains(job)
            if not self.keep_jobs:
                with self._lock:
                    self._registry.pop(job.id, None)
        return finished

    def _finalize(self, job, exit_code):
        """Record the outcome of a process we have just seen exit. Assumes the lock is held."""
        job.exit_code = exit_code
        job.end_time = time.time()
        # A job we stopped or killed on purpose keeps that label; its non-zero
        # exit code is our doing, not a failure of the work.
        if job.status not in TERMINAL_STATUSES:
            job.status = "finished" if exit_code == 0 else "failed"

    def active_jobs(self):
        """Every job still running or paused."""
        with self._lock:
            return [job for job in self._registry.values() if job.is_active()]

    def uptime(self):
        """Seconds since the registry was created, running or not."""
        return time.monotonic() - self._created_at

    def idle_seconds(self):
        """
        How long the registry has had no outstanding work. Zero while any job is
        active, so it doubles as an "is anything happening" check.
        """
        with self._lock:
            if any(job.is_active() for job in self._registry.values()):
                return 0.0
            return time.monotonic() - self._last_busy

    def should_terminate(self):
        """
        Whether terminate_behavior says it is time to shut down. Being busy is
        checked separately from the idle clock: a running registry reports zero
        idle seconds, which would otherwise satisfy an idle_time of 0.
        """
        if self.terminate_behavior != "on_idle":
            return False
        with self._lock:
            if any(job.is_active() for job in self._registry.values()):
                return False
            return time.monotonic() - self._last_busy >= self.idle_time

    def wait_for_jobs(self, timeout=None):
        """
        Block until every active job has exited, polling as it goes. Returns True
        if the registry drained, False if the timeout expired first.

        A paused job never exits on its own, so it will hold this open until it
        is resumed or stopped; pass a timeout if that is a possibility.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            self.poll_jobs()
            if not self.active_jobs():
                return True
            if deadline is None:
                time.sleep(self.poll_interval)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(self.poll_interval, remaining))

    def start_monitor(self):
        """
        Start the background thread that reaps finished jobs and applies
        terminate_behavior. Idempotent, and called automatically by start_job().
        """
        with self._lock:
            if self._shutdown:
                return
            if self._monitor is not None and self._monitor.is_alive():
                return
            self._stop_monitor.clear()
            self._monitor = threading.Thread(target=self._monitor_loop, name="carpenter-monitor", daemon=True)
            self._monitor.start()

    def _monitor_loop(self):
        while not self._stop_monitor.wait(self.poll_interval):
            self.poll_jobs()
            if self.should_terminate():
                self.shutdown(reason="idle")
                return

    def stop_all(self, grace=None):
        """
        Ask every active job to exit, then kill whatever is still alive after the
        grace period. Returns the jobs that had to be killed.
        """
        grace = self.shutdown_grace if grace is None else grace
        killed = []
        # Reap first, so a job that finished on its own a moment ago is recorded
        # as "finished" rather than mislabelled as something we stopped.
        self.poll_jobs()
        active = self.active_jobs()
        for job in active:
            try:
                self.stop_job(job)
            except (ProcessLookupError, RuntimeError):
                continue

        deadline = time.monotonic() + grace
        for job in active:
            try:
                job.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                job.process.kill()
                job.status = "terminated"
                job.process.wait()
                killed.append(job)
        self.poll_jobs()
        return killed

    def shutdown(self, reason="manual", grace=None):
        """
        Stop supervising and bring down anything still running. Idempotent, and
        safe to call from the monitor thread itself.

        This does not wait for jobs to finish their work; call wait_for_jobs()
        first if that is what you want.
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True

        self._stop_monitor.set()
        monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=self.poll_interval * 2 + 1)

        self.stop_all(grace=grace)

        if self.on_shutdown is not None:
            self.on_shutdown(self, reason)

        self._shutdown_event.set()

    def wait_for_shutdown(self, timeout=None):
        """
        Block until the registry has shut down, whether that was the monitor
        reaching its idle window or someone calling shutdown(). Returns True if
        it shut down, False if the timeout expired first.

        Under "manual" nothing will ever release this on its own, so only wait
        on it unbounded when the behaviour is "on_idle".
        """
        # The monitor is what applies terminate_behavior, so make sure it is
        # running: a registry that was never given a job has not started it yet.
        self.start_monitor()
        return self._shutdown_event.wait(timeout)

    @property
    def is_shutdown(self):
        return self._shutdown

    def __enter__(self):
        # The monitor deliberately is not started here: with an on_idle
        # behaviour it would see an empty registry and could shut down before
        # the block has started its first job. start_job() starts it instead.
        return self

    def __exit__(self, exc_type, exc, tb):
        # A clean exit honours terminate_behavior rather than overriding it.
        # Under "on_idle" the registry is the one that decides when it is
        # finished, so leaving the block waits out its idle window instead of
        # cutting it short; under "manual" the caller decides, and leaving the
        # block is that decision, so the jobs are drained and it comes down.
        # An exception skips both and tears everything down immediately rather
        # than hanging on work whose caller has already given up.
        if exc_type is None:
            if self.terminate_behavior == "on_idle":
                self.wait_for_shutdown()
            else:
                self.wait_for_jobs()
        self.shutdown(reason="context-exit")
        return False

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

    def print_registry(self, clear=False):
        """
        Pretty print a snapshot of the registry's current jobs.

        clear=True homes the cursor and wipes the screen first, so a polling
        loop redraws the table in place instead of scrolling. It is ignored when
        stdout is not a terminal, to keep escape codes out of piped output and
        log files.

        Green: Done
        Red: Failed/Terminated
        Yellow: Paused
        Gray: working (started/stopped)
        """
        if clear and sys.stdout.isatty():
            print("\033[H\033[J", end="")

        header = f"{'Job ID':<36} {'Name':<20} {'Status':<10} {'Runtime':<10} {'Exit Code':<10}"
        rule = "-" * len(header)

        # Snapshot under the lock: the monitor thread removes finished jobs when
        # keep_jobs is False, which would otherwise change the dict mid-iteration.
        with self._lock:
            rows = list(self._registry.values())

        print(rule)
        print(header)
        print(rule)
        for job in rows:
            exit_code = "-" if job.exit_code is None else job.exit_code
            runtime = "-" if job.duration() is None else f"{job.duration():.1f}s"
            if job.status == "finished":
                color = "\033[32m"  # Green
            elif job.status in ("failed", "terminated"):
                color = "\033[31m"  # Red
            elif job.status == "paused":
                color = "\033[33m"  # Yellow
            else:
                color = "\033[90m"  # Gray
            reset = "\033[0m"
            print(f"{color}", end="")
            # !s stringifies the UUID first; UUID has no __format__ of its own,
            # so a width spec applied directly to it raises TypeError.
            print(f"{job.id!s:<36} {job.name:<20} {job.status:<10} {runtime:<10} {exit_code!s:<10}{reset}")
        print(rule)

        active = sum(1 for job in rows if job.is_active())
        summary = [
            f"{len(rows)} job{'' if len(rows) == 1 else 's'}",
            f"{active} active",
            f"runtime {self.uptime():.1f}s",
        ]
        if self.terminate_behavior == "on_idle":
            # The idle clock only advances while nothing is running, so it reads
            # as a countdown towards idle_time once the last job has finished.
            summary.append(f"idle {self.idle_seconds():.1f}s / {self.idle_time:g}s")
        else:
            summary.append("shutdown: manual")
        if self.is_shutdown:
            summary.append("SHUT DOWN")
        print(" | ".join(summary))
        print(rule)
