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
from carpenter.settings import OUTPUT_MODES, TERMINATE_BEHAVIORS, Settings

__all__ = ["Registry", "OUTPUT_MODES", "TERMINATE_BEHAVIORS"]


class Registry:
    """
    Manages jobs and their lifecycle.

    Args:
        settings: A `Settings` instance, a plain dict, or None for all defaults.
        default_blueprint: Used to start any submitted job that doesn't carry its own blueprint.
        on_shutdown: Called as `on_shutdown(registry, reason)` once the registry has stopped
            supervising. The hook for whatever "shutting down" means in the host application:
            exiting a CLI, logging in a web server, releasing a resource pool.
    """

    def __init__(self, settings=None, default_blueprint=None, on_shutdown=None) -> None:
        self._registry = {}

        # Jobs that have been accepted and are waiting for a free slot, oldest
        # first. Nothing else decides what runs next, so this is the whole of
        # the scheduling state.
        self._waiting = []

        self.settings = Settings.from_dict(settings).resolve()
        self.default_blueprint = default_blueprint
        self.on_shutdown = on_shutdown

        # Mirrored onto the registry so callers can read a live setting without
        # reaching through .settings, which is what the polling loop in the
        # README does with poll_interval and terminate_behavior.
        for name in Settings.names():
            setattr(self, name, getattr(self.settings, name))

        # Validate that the settings are compatible with the system resources.
        # This is a question about the machine rather than about the settings,
        # so it lives here rather than in Settings.validate().
        system_resources = self.get_system_resources()
        if self.max_cpus > system_resources["cpu_count"]:
            raise ValueError(f"max_cpus ({self.max_cpus}) exceeds available CPU count ({system_resources['cpu_count']})")
        if self.max_memory > system_resources["memory"]:
            raise ValueError(f"max_memory ({self.max_memory} MB) exceeds available system memory ({system_resources['memory']} MB)")

        # Supervision state. _lock guards _registry, _waiting and the idle
        # clock, since a web server will be submitting jobs from request threads
        # while the monitor thread dispatches and reaps them.
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

    def submit_job(self, job):
        """
        Hand a job to the registry and return it.

        This is the only way in. The registry assigns the job an ID if it does
        not have one, puts it in the waiting list, and spawns it when there is a
        free slot, which may be immediately or may be much later. Nothing about
        this call guarantees that a process exists by the time it returns, so
        callers should not reach for job.process straight afterwards.

        A job that has reached a terminal status can be submitted again, which
        clears the previous run's result and queues it afresh. Submitting a job
        that is already queued or running raises, since the registry has no way
        to run one Job object twice at once.
        """
        blueprint = job.blueprint if job.blueprint is not None else self.default_blueprint
        if blueprint is None:
            raise ValueError(f"job '{job.name}' has no blueprint and registry has no default_blueprint")

        with self._lock:
            if self._shutdown:
                raise RuntimeError("registry has been shut down and cannot accept new jobs")
            if job.status == "queued":
                raise RuntimeError(f"job '{job.name}' is already queued")
            if job.is_running():
                raise RuntimeError(f"job '{job.name}' is already running")

            if job.id is None:
                job.id = uuid.uuid4()
            if job.is_finished():
                # Resubmitting an already finished Job object runs it again. The
                # identity survives; only the result of the last run is cleared.
                job.reset()

            job.status = "queued"
            job.submit_time = time.time()
            self._registry[job.id] = job
            self._waiting.append(job)
            self._last_busy = time.monotonic()

        # Dispatch trigger one of three: work has just arrived. Without this a
        # job submitted to an idle registry would sit until the next monitor
        # tick, so a job that runs for 50ms would still take a poll_interval to
        # start.
        self._dispatch()

        # Supervision only matters once there is something to supervise, so the
        # monitor starts with the first job rather than in the constructor.
        self.start_monitor()
        return job

    def cancel_job(self, job):
        """
        Withdraw a job that is still waiting for a slot.

        Only valid while the job is queued. Nothing was ever spawned, so no
        signal is sent and there is no grace period; the job simply stops being
        outstanding work. Use stop_job() or terminate_job() for a job that is
        already running.
        """
        with self._lock:
            if job.status != "queued":
                raise RuntimeError(f"job '{job.name}' is not queued (status: {job.status})")
            try:
                self._waiting.remove(job)
            except ValueError:
                pass
            job.status = "cancelled"
            job.end_time = time.time()
            self._last_busy = time.monotonic()
            if not self.keep_jobs:
                self._registry.pop(job.id, None)

    def _dispatch(self):
        """
        Fill whatever slots are free from the front of the waiting list.

        Called on every event that can free capacity or add work: a submission,
        a completion, and every monitor tick. The tick is not merely a backstop
        for the other two, since capacity can also be freed by a route that is
        neither, such as a paused job being stopped.

        Selection and spawning happen together under the lock, or two threads
        would both see the same free slot and both fill it.
        """
        spawned = []
        with self._lock:
            if self._shutdown:
                return spawned
            running = self._running_count()
            while self._waiting and (self.max_jobs is None or running < self.max_jobs):
                job = self._waiting.pop(0)
                if self._spawn(job):
                    running += 1
                    spawned.append(job)
                # A job whose spawn failed never occupied a slot, so the loop
                # carries on to the next one rather than stalling the queue.
        return spawned

    def _spawn(self, job):
        """
        Launch one job's process. Assumes the lock is held. Returns whether a
        process now exists.

        Every failure here is contained. This runs on the monitor thread as well
        as on a caller's thread, and an exception escaping it would kill the
        monitor, which would leave every other job in the registry unreaped for
        the life of the process. One mistyped command must not be able to do
        that, so a failed spawn marks its own job and nothing else.
        """
        blueprint = job.blueprint if job.blueprint is not None else self.default_blueprint

        job.run += 1
        stdout_target = None
        stderr_target = None
        try:
            stdout_target, stderr_target = self._output_targets(job)
            # stdin is DEVNULL rather than the Blueprint's own PIPE default. A
            # supervised job has nobody at a console to type at it, and the
            # registry never writes to the pipe, so a PIPE here would only leave
            # one write end open per job for as long as the Popen is referenced.
            # Under keep_jobs that is the life of the registry. A job that reads
            # stdin now sees EOF immediately instead of blocking forever.
            job.process = blueprint.spawn(
                stdout=stdout_target,
                stderr=stderr_target,
                stdin=subprocess.DEVNULL,
            )
            # The drains are registered here, inside the same try and before the
            # job becomes visible as "started". Starting them afterwards leaves
            # a window where a short lived job can be reaped by the monitor
            # first, which then joins a drain list that is empty or half built:
            # the captured output is lost, and joining a thread that has been
            # appended but not yet started raises outright. Inside the try
            # because starting a thread can fail too, and thread exhaustion is
            # reachable at a few hundred jobs when each one wants two.
            if self.output_mode == "capture":
                self._start_drains(job)
        except Exception as exc:
            if job.process is not None:
                # The process exists but could not be set up for supervision, so
                # it comes down rather than being left running unwatched with
                # nobody draining its pipes. Its pipes are closed here too: a
                # Popen that is dropped with them still open raises during
                # garbage collection, long after anyone could tie it to this.
                try:
                    job.process.kill()
                except Exception:
                    pass
                for stream in (job.process.stdout, job.process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                try:
                    job.process.wait(timeout=5)
                except Exception:
                    pass
                job.process = None
            job.error = exc
            job.status = "failed"
            job.end_time = time.time()
            self._last_busy = time.monotonic()
            return False
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
        return True

    def _running_count(self):
        """How many jobs hold a slot. Assumes the lock is held."""
        return sum(1 for job in self._registry.values() if job.is_running())

    def _output_targets(self, job):
        """
        Build the (stdout, stderr) targets for a spawn according to output_mode.

        The run number is part of the filename because a Job object can be run
        more than once and these are opened for writing: without it, a second
        run would silently truncate the first run's logs.
        """
        if self.output_mode == "discard":
            return subprocess.DEVNULL, subprocess.DEVNULL
        if self.output_mode == "file":
            os.makedirs(self.output_dir, exist_ok=True)
            job.stdout_path = os.path.join(self.output_dir, f"{job.name}.{job.id}.{job.run}.stdout.log")
            job.stderr_path = os.path.join(self.output_dir, f"{job.name}.{job.id}.{job.run}.stderr.log")
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
            thread.start()
            job._drains.append(thread)

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
            raise RuntimeError(self._not_started_message(job))
        if platform.system() == "Windows":
            raise NotImplementedError("pause/resume is not supported on Windows (no SIGSTOP/SIGCONT)")
        job.process.send_signal(signal.SIGSTOP)
        with self._lock:
            job.status = "paused"

    def resume_job(self, job):
        if job.process is None or job.status != "paused":
            raise RuntimeError(f"job '{job.name}' is not paused")
        job.process.send_signal(signal.SIGCONT)
        with self._lock:
            job.status = "started"

    def stop_job(self, job):
        if job.process is None:
            raise RuntimeError(self._not_started_message(job))
        if job.status == "paused":
            # A SIGSTOPped process cannot act on SIGTERM until it is resumed.
            job.process.send_signal(signal.SIGCONT)
        job.process.terminate()
        with self._lock:
            job.status = "stopped"

    def terminate_job(self, job):
        if job.process is None:
            raise RuntimeError(self._not_started_message(job))
        job.process.kill()
        with self._lock:
            job.status = "terminated"

    def _not_started_message(self, job):
        """
        A job with no process is either still waiting for a slot or was never
        submitted, and the two want different things from the caller.
        """
        if job.status == "queued":
            return f"job '{job.name}' is queued and has no process yet; use cancel_job() to withdraw it"
        return f"job '{job.name}' has not been started"

    def poll_jobs(self):
        """
        Sweep every running job, recording the exit code and end time of any that
        have finished, then fill any slots that frees. Returns the list of jobs
        that finished on this sweep.

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

        # Dispatch trigger two of three: a completion has just freed a slot, and
        # this is the moment the registry learns about it. Without this the slot
        # would stay empty until the next tick, costing up to a poll_interval of
        # throughput after every single job.
        self._dispatch()
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
        """Every job still outstanding, whether queued, running or paused."""
        with self._lock:
            return [job for job in self._registry.values() if job.is_active()]

    def running_jobs(self):
        """Every job holding a slot, which includes paused ones."""
        with self._lock:
            return [job for job in self._registry.values() if job.is_running()]

    def queued_jobs(self):
        """Every job waiting for a slot, in the order they will be started."""
        with self._lock:
            return list(self._waiting)

    def available_slots(self):
        """
        How many more jobs the registry would start right now, or None when
        max_jobs is unset and the answer is "as many as you like".
        """
        if self.max_jobs is None:
            return None
        with self._lock:
            return max(0, self.max_jobs - self._running_count())

    def uptime(self):
        """Seconds since the registry was created, running or not."""
        return time.monotonic() - self._created_at

    def idle_seconds(self):
        """
        How long the registry has had no outstanding work. Zero while any job is
        queued, running or paused, so it doubles as an "is anything happening"
        check.
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

        A queued job counts as busy. A registry at its slot limit with a backlog
        has plenty left to do even though nothing has changed for a while.
        """
        if self.terminate_behavior != "on_idle":
            return False
        with self._lock:
            if any(job.is_active() for job in self._registry.values()):
                return False
            return time.monotonic() - self._last_busy >= self.idle_time

    def wait_for_jobs(self, timeout=None):
        """
        Block until every outstanding job has exited, polling as it goes. Returns
        True if the registry drained, False if the timeout expired first.

        Queued jobs are waited on as well as running ones, so with a max_jobs
        limit this blocks until the whole backlog has been worked through, not
        just the jobs that happened to be running when it was called.

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
        Start the background thread that dispatches queued jobs, reaps finished
        ones, and applies terminate_behavior. Idempotent, and called
        automatically by submit_job().
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
        # Dispatch trigger three of three lives inside poll_jobs(), which runs
        # on every tick whether or not anything finished. It is the only trigger
        # that can notice capacity freed by a route that is neither a submission
        # nor a completion, such as a paused job being stopped.
        while not self._stop_monitor.wait(self.poll_interval):
            self.poll_jobs()
            if self.should_terminate():
                self.shutdown(reason="idle")
                return

    def stop_all(self, grace=None):
        """
        Withdraw everything still queued, ask every running job to exit, then
        kill whatever is still alive after the grace period. Returns the jobs
        that had to be killed.
        """
        grace = self.shutdown_grace if grace is None else grace
        killed = []
        # Reap first, so a job that finished on its own a moment ago is recorded
        # as "finished" rather than mislabelled as something we stopped.
        self.poll_jobs()

        # Queued jobs never started, so they are withdrawn rather than signalled.
        for job in self.queued_jobs():
            try:
                self.cancel_job(job)
            except RuntimeError:
                continue

        running = self.running_jobs()
        for job in running:
            try:
                self.stop_job(job)
            except (ProcessLookupError, RuntimeError):
                continue

        deadline = time.monotonic() + grace
        for job in running:
            if job.process is None:
                continue
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
        first if that is what you want. Anything still queued is cancelled
        rather than run.
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
        # the block has started its first job. submit_job() starts it instead.
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

        Taken under the lock: with the default keep_jobs of False the monitor
        thread removes reaped jobs on every sweep, and scanning the dict for a
        name while that happens raises RuntimeError in the caller.
        """
        with self._lock:
            if "id" in kwargs:
                return self._registry.get(kwargs["id"])
            elif "name" in kwargs:
                for job in self._registry.values():
                    if job.name == kwargs["name"]:
                        return job
            return None

    def print_registry(self, clear=False, max_print_jobs=20):
        """
        Pretty print a snapshot of the registry's current jobs.

        clear=True homes the cursor and wipes the screen first, so a polling
        loop redraws the table in place instead of scrolling. It is ignored when
        stdout is not a terminal, to keep escape codes out of piped output and
        log files.

        caps at 20 jobs, but the full registry is still available via get_job() and to_dict(). This can also be overriden by using the max_print_jobs param. After max is met only running jobs will be printed. This is to avoid flooding the screen with finished jobs.

        Color Codes:
        Green: Done
        Red: Failed/Terminated
        Yellow: Paused
        Cyan: Queued
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
            if len(rows) > max_print_jobs:
                rows = [job for job in rows if job.is_active()] + rows[:max_print_jobs]
        print(rule)
        print(header)
        print(rule)
        for job in rows[:max_print_jobs]:
            exit_code = "-" if job.exit_code is None else job.exit_code
            # A queued job has not run, so its runtime slot shows how long it has
            # been waiting instead of a dash that says nothing.
            if job.status == "queued":
                waited = job.waited()
                runtime = "-" if waited is None else f"+{waited:.1f}s"
            else:
                runtime = "-" if job.duration() is None else f"{job.duration():.1f}s"
            if job.status == "finished":
                color = "\033[32m"  # Green
            elif job.status in ("failed", "terminated"):
                color = "\033[31m"  # Red
            elif job.status == "paused":
                color = "\033[33m"  # Yellow
            elif job.status == "queued":
                color = "\033[36m"  # Cyan
            else:
                color = "\033[90m"  # Gray
            reset = "\033[0m"
            print(f"{color}", end="")
            # !s stringifies the UUID first; UUID has no __format__ of its own,
            # so a width spec applied directly to it raises TypeError.
            print(f"{job.id!s:<36} {job.name:<20} {job.status:<10} {runtime:<10} {exit_code!s:<10}{reset}")
        print(rule)

        running = sum(1 for job in rows if job.is_running())
        queued = sum(1 for job in rows if job.status == "queued")
        summary = [
            f"{len(rows)} job{'' if len(rows) == 1 else 's'}",
            f"{running} running",
        ]
        if self.max_jobs is not None:
            summary[-1] = f"{running}/{self.max_jobs} running"
        if queued:
            summary.append(f"{queued} queued")
        summary.append(f"runtime {self.uptime():.1f}s")
        if self.terminate_behavior == "on_idle":
            # The idle clock only advances while nothing is outstanding, so it
            # reads as a countdown towards idle_time once the last job has gone.
            summary.append(f"idle {self.idle_seconds():.1f}s / {self.idle_time:g}s")
        else:
            summary.append("shutdown: manual")
        if self.is_shutdown:
            summary.append("SHUT DOWN")
        print(" | ".join(summary))
        print(rule)
