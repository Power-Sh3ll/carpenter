import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from carpenter.dispatch import effective_priority, order_waiting
from carpenter.job import TERMINAL_STATUSES
from carpenter.settings import DISPATCH_ORDERS, OUTPUT_MODES, TERMINATE_BEHAVIORS, Settings

__all__ = ["Registry", "DISPATCH_ORDERS", "OUTPUT_MODES", "TERMINATE_BEHAVIORS"]


class Registry:
    """
    Manages jobs and their lifecycle.

    A registry either holds jobs or holds other registries, never both. One
    holding jobs is a lane, and does the actual supervising. One holding other
    registries governs them: it bounds their combined capacity, hands down its
    settings to any they did not set for themselves, runs the single monitor
    thread for the whole tree, and brings everything down together.

    Args:
        settings: A `Settings` instance, a plain dict, or None for all defaults.
        default_blueprint: Used to start any submitted job that doesn't carry its own blueprint.
        on_shutdown: Called as `on_shutdown(registry, reason)` once the registry has stopped
            supervising. The hook for whatever "shutting down" means in the host application:
            exiting a CLI, logging in a web server, releasing a resource pool.
        name: Required to mount this registry inside another, since the name is
            how it is addressed. Same rule as a job name: a lowercase identifier.
    """

    def __init__(self, settings=None, default_blueprint=None, on_shutdown=None, name=None) -> None:
        self._registry = {}

        # Jobs that have been accepted and are waiting for a free slot. The list
        # is unordered: which one starts next is decided by carpenter.dispatch
        # at the moment of dispatch, not by position in here.
        self._waiting = []

        # Counts up across every submission, and is what gives the waiting list
        # a total order. See Job.sequence for why this is not a timestamp.
        self._next_sequence = 0

        # Mounted registries, by name. A registry with any of these holds no
        # jobs of its own.
        self._children = {}
        self._parent = None

        if name is not None:
            self._validate_name(name)
        self.name = name

        # The settings as the caller wrote them, keeping unset apart from
        # defaulted. Kept because mounting re-resolves them against the parent's,
        # and only the declared form knows which keys the caller had an opinion
        # about.
        self._declared = Settings.from_dict(settings)
        self._apply_settings(self._declared.resolve())

        self.default_blueprint = default_blueprint
        self.on_shutdown = on_shutdown

        # Supervision state. _lock guards _registry, _waiting, _children and the
        # idle clock, since a web server will be submitting jobs from request
        # threads while the monitor thread dispatches and reaps them. A mounted
        # registry adopts its parent's lock, so one lock covers a whole tree and
        # there is no order in which two of them could be taken.
        self._lock = threading.RLock()
        self._monitor = None
        self._stop_monitor = threading.Event()
        self._shutdown = False
        self._shutdown_event = threading.Event()
        self._last_busy = time.monotonic()
        self._created_at = time.monotonic()

    @staticmethod
    def _validate_name(name):
        """
        Registry names follow the same rule as job names, so a name is always
        safe to use as a key and reads the same way in both places.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("name must be a non-empty string")
        if len(name) > 255:
            raise ValueError("name must be less than 256 characters")
        if not name.isidentifier():
            raise ValueError("name must be a valid identifier (alphanumeric and underscores only, cannot start with a number)")
        if not name.islower():
            raise ValueError("name must be lowercase")

    def _apply_settings(self, resolved):
        """
        Adopt a resolved Settings, mirroring every key onto the registry itself.

        The mirror is what lets callers read `reg.poll_interval` rather than
        `reg.settings.poll_interval`, which is what the polling loop in the
        README does. Re-run on mount, since a child inherits whatever it did not
        set for itself from the registry it is mounted in.
        """
        self.settings = resolved
        for key in Settings.names():
            setattr(self, key, getattr(resolved, key))

        # Whether the host can actually honour these is a question about the
        # machine rather than about the settings, so it is checked here rather
        # than in Settings.validate().
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

    # Nesting -----------------------------------------------------------------

    @property
    def is_leaf(self):
        """
        Whether this registry holds jobs rather than other registries. A registry
        with no children is a leaf even when it is empty, since submitting the
        first job is what settles the question.
        """
        with self._lock:
            return not self._children

    @property
    def parent(self):
        """The registry this one is mounted in, or None if it stands alone."""
        return self._parent

    def children(self):
        """The mounted registries, in the order they were mounted."""
        with self._lock:
            return list(self._children.values())

    def leaves(self):
        """
        Every registry in this subtree that actually holds jobs, this one
        included if it is a leaf. This is what the tree-wide operations fan out
        over.
        """
        with self._lock:
            if not self._children:
                return [self]
            found = []
            for child in self._children.values():
                found.extend(child.leaves())
            return found

    def root(self):
        """The registry at the top of the tree, which owns the monitor thread."""
        registry = self
        while registry._parent is not None:
            registry = registry._parent
        return registry

    def path(self):
        """
        Dotted path from the root, for reporting. Unnamed registries show as
        their position rather than a name.
        """
        parts = []
        registry = self
        while registry is not None:
            parts.append(registry.name if registry.name is not None else "registry")
            registry = registry._parent
        return ".".join(reversed(parts))

    def mount(self, child):
        """
        Put another registry inside this one and return it.

        The child keeps its own limits and its own ordering, and this registry
        bounds their total. Mirrors submit_job(): the child carries its own name,
        so only the child is passed.

        Mounting settles three things at once. The child inherits every setting
        it did not name for itself. Its capacity is checked against what is left
        of this registry's. And it stops being independently supervised, since
        the whole tree is driven by one monitor thread belonging to the root.

        A child must be empty when mounted. Mount first, submit afterwards.
        """
        if not isinstance(child, Registry):
            raise TypeError(f"mount() takes a Registry, not {type(child).__name__}")
        if child.name is None:
            raise ValueError("a registry must be given a name before it can be mounted")
        if child is self:
            raise ValueError("a registry cannot be mounted inside itself")

        with self._lock:
            if self._shutdown:
                raise RuntimeError("registry has been shut down and cannot mount anything")
            if self._registry:
                raise RuntimeError(
                    f"registry '{self.path()}' already holds jobs, so it cannot also hold registries. "
                    "A registry holds one or the other."
                )
            if child.name in self._children:
                raise ValueError(f"a registry named '{child.name}' is already mounted here")
            if child._parent is not None:
                raise RuntimeError(f"registry '{child.name}' is already mounted in '{child._parent.path()}'")
            if child._registry or child._waiting:
                raise RuntimeError(
                    f"registry '{child.name}' already holds jobs; mount it before submitting to it"
                )

            ancestor = self
            while ancestor is not None:
                if ancestor is child:
                    raise ValueError("mounting this registry here would make a cycle")
                ancestor = ancestor._parent

            # Re-resolve against this registry's settings, so the child keeps
            # every key it named and inherits the rest.
            resolved = child._declared.resolve(self.settings)
            self._check_capacity(child, resolved)

            child._apply_settings(resolved)
            child._parent = self
            # One lock for the whole tree. Taking a child's lock and then a
            # parent's, or the reverse, is the classic way to deadlock two
            # threads; sharing one removes the possibility rather than
            # documenting an ordering nobody will remember.
            child._lock = self._lock
            child._stop_own_monitor()
            self._children[child.name] = child

        return child

    def _check_capacity(self, child, resolved):
        """
        Refuse a child whose limit would let the tree exceed this registry's.
        Assumes the lock is held.

        Capacity is partitioned rather than shared: each lane's limit is its own,
        and they are required to add up to no more than the parent's. That means
        no coordination is needed at dispatch time, because a lane that respects
        its own limit cannot push the tree past the parent's.
        """
        if self.max_jobs is None:
            return
        if resolved.max_jobs is None:
            raise ValueError(
                f"registry '{child.name}' has no max_jobs, so it cannot be mounted in "
                f"'{self.path()}' which limits itself to {self.max_jobs}"
            )
        claimed = sum(existing.max_jobs for existing in self._children.values())
        if claimed + resolved.max_jobs > self.max_jobs:
            raise ValueError(
                f"'{child.name}' would claim {resolved.max_jobs} of "
                f"'{self.path()}' max_jobs={self.max_jobs}, and {claimed} is already claimed "
                f"by {', '.join(self._children) or 'nothing'}"
            )

    def unmount(self, name, grace=None):
        """
        Take a mounted registry back out and return it.

        Everything queued in it is cancelled and everything running is brought
        down, with the same grace period a shutdown would use, because a lane
        nobody governs any more should not still be spawning processes. The
        returned registry is shut down; it is handed back so its finished jobs
        can still be read.
        """
        with self._lock:
            child = self._children.get(name)
            if child is None:
                raise KeyError(f"no registry named '{name}' is mounted in '{self.path()}'")

        child.shutdown(reason="unmounted", grace=grace)

        with self._lock:
            self._children.pop(name, None)
            child._parent = None
        return child

    def _stop_own_monitor(self):
        """
        Stop this registry supervising itself, because something above it now
        does. Assumes the lock is held.
        """
        if self._monitor is not None and self._monitor.is_alive():
            self._stop_monitor.set()
        self._monitor = None

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
        if self.blueprint_for(job) is None:
            raise ValueError(f"job '{job.name}' has no blueprint and registry has no default_blueprint")

        with self._lock:
            if self._shutdown:
                raise RuntimeError("registry has been shut down and cannot accept new jobs")
            if self._children:
                raise RuntimeError(
                    f"registry '{self.path()}' holds registries, so it cannot also hold jobs. "
                    f"Submit to one of its lanes instead: {', '.join(self._children)}"
                )
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
            job.queued_at = time.monotonic()
            job.sequence = self._next_sequence
            self._next_sequence += 1
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
            if self._children:
                # The job belongs to one of the lanes, and only that lane can
                # take it out of its own waiting list.
                for child in self._children.values():
                    if child._holds(job):
                        return child.cancel_job(job)
                raise KeyError(f"job '{job.name}' is not in any lane of '{self.path()}'")

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
        Fill whatever slots are free with the highest ranked waiting jobs.

        Called on every event that can free capacity or add work: a submission,
        a completion, and every monitor tick. The tick is not merely a backstop
        for the other two, since capacity can also be freed by a route that is
        neither, such as a paused job being stopped, and because with aging on
        the ranking changes with the passage of time alone.

        Selection and spawning happen together under the lock, or two threads
        would both see the same free slot and both fill it.
        """
        spawned = []
        with self._lock:
            if self._children:
                for child in self._children.values():
                    spawned.extend(child._dispatch())
                return spawned
            if self._shutdown:
                return spawned
            running = self._running_count()
            # Ranked fresh on every dispatch rather than kept in a sorted
            # structure. Aging changes the keys of jobs already waiting as time
            # passes, with no event to trigger a re-sort, so anything that
            # cached the order would gradually start handing back the wrong job.
            for job in order_waiting(self._waiting, self.settings, time.monotonic()):
                if self.max_jobs is not None and running >= self.max_jobs:
                    break
                self._waiting.remove(job)
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
        blueprint = self.blueprint_for(job)

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

    def blueprint_for(self, job):
        """
        The blueprint a job would actually run, or None if nothing provides one.

        The job's own comes first, then this registry's default, then that of
        each registry it is mounted in. It is the same fallback that already
        takes a job without a blueprint to its registry's default, carried one
        step further up: a registry governing several lanes can give them all a
        default command without each having to repeat it.
        """
        if job.blueprint is not None:
            return job.blueprint
        registry = self
        while registry is not None:
            if registry.default_blueprint is not None:
                return registry.default_blueprint
            registry = registry._parent
        return None

    def _holds(self, job):
        """Whether this registry, or any lane under it, is the one holding this job."""
        with self._lock:
            if self._children:
                return any(child._holds(job) for child in self._children.values())
            return job.id in self._registry or job in self._waiting

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

        On a registry holding other registries this sweeps the whole tree, which
        is what lets one monitor thread supervise every lane.
        """
        with self._lock:
            children = list(self._children.values())
        if children:
            finished = []
            for child in children:
                finished.extend(child.poll_jobs())
            return finished

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

    def all_jobs(self):
        """
        Every job this registry knows about. On one holding other registries,
        every job in the whole tree, lane by lane.
        """
        with self._lock:
            if self._children:
                found = []
                for child in self._children.values():
                    found.extend(child.all_jobs())
                return found
            return list(self._registry.values())

    def active_jobs(self):
        """Every job still outstanding, whether queued, running or paused."""
        return [job for job in self.all_jobs() if job.is_active()]

    def running_jobs(self):
        """Every job holding a slot, which includes paused ones."""
        return [job for job in self.all_jobs() if job.is_running()]

    def queued_jobs(self):
        """
        Every job waiting for a slot, in the order they will be started.

        The order is worked out afresh on each call rather than read off the
        waiting list, so with aging switched on this reflects the ranking as it
        stands now rather than as it stood at the last dispatch.

        On a registry holding other registries this is every lane's queue
        concatenated, each in its own order. There is no ordering between lanes:
        they hold separate capacity and do not compete, so asking which of two
        lanes goes first is not a question the registry answers.
        """
        with self._lock:
            if self._children:
                found = []
                for child in self._children.values():
                    found.extend(child.queued_jobs())
                return found
            return order_waiting(self._waiting, self.settings, time.monotonic())

    def effective_priority(self, job):
        """
        The priority the registry is currently ordering this job by: its own
        weight, if weights are being consulted, plus anything it has gained by
        waiting. Useful for answering "why has this not started yet".
        """
        return effective_priority(job, self.settings, time.monotonic())

    def available_slots(self):
        """
        How many more jobs the registry would start right now, or None when
        max_jobs is unset and the answer is "as many as you like".

        On a registry holding other registries this adds up what its lanes would
        each take. Since capacity is partitioned, a free slot belongs to one
        particular lane and cannot be used by another.
        """
        with self._lock:
            if self._children:
                total = 0
                for child in self._children.values():
                    free = child.available_slots()
                    if free is None:
                        return None
                    total += free
                return total
            if self.max_jobs is None:
                return None
            return max(0, self.max_jobs - self._running_count())

    def uptime(self):
        """Seconds since the registry was created, running or not."""
        return time.monotonic() - self._created_at

    def idle_seconds(self):
        """
        How long the registry has had no outstanding work. Zero while any job is
        queued, running or paused, so it doubles as an "is anything happening"
        check.

        A registry holding other registries is only idle once every lane is, and
        its clock runs from whichever lane was busy most recently.
        """
        with self._lock:
            if self._children:
                if any(job.is_active() for job in self.all_jobs()):
                    return 0.0
                return time.monotonic() - max(leaf._last_busy for leaf in self.leaves())
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

        On a registry holding other registries this is asked of the tree, so no
        lane going quiet on its own can bring the application down while another
        still has work.
        """
        if self.terminate_behavior != "on_idle":
            return False
        with self._lock:
            if any(job.is_active() for job in self.all_jobs()):
                return False
            return self.idle_seconds() >= self.idle_time

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

        A whole tree is supervised by one thread belonging to the registry at
        the top, so asking a mounted registry to start supervising passes the
        request upwards. Three lanes would otherwise mean three threads, three
        idle clocks and three shutdown policies, which is most of what nesting
        exists to avoid.
        """
        root = self.root()
        if root is not self:
            root.start_monitor()
            return

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
        with self._lock:
            children = list(self._children.values())
        if children:
            killed = []
            for child in children:
                killed.extend(child.stop_all(grace=grace))
            return killed

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

        On a registry holding other registries this brings down the whole tree,
        lanes first, so nothing is still spawning while the registry governing it
        is being torn down. Each lane's own on_shutdown fires as it goes, and
        this registry's fires last.
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            children = list(self._children.values())

        self._stop_monitor.set()
        monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=self.poll_interval * 2 + 1)

        for child in children:
            child.shutdown(reason=reason, grace=grace)

        if not children:
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
        Get a job from the registry by its ID or name, searching every lane if
        this registry holds other registries. Returns None if there is no match.

        Taken under the lock: with the default keep_jobs of False the monitor
        thread removes reaped jobs on every sweep, and scanning the dict for a
        name while that happens raises RuntimeError in the caller.
        """
        with self._lock:
            if self._children:
                for child in self._children.values():
                    found = child.get_job(**kwargs)
                    if found is not None:
                        return found
                return None
            if "id" in kwargs:
                return self._registry.get(kwargs["id"])
            elif "name" in kwargs:
                for job in self._registry.values():
                    if job.name == kwargs["name"]:
                        return job
            return None

    # Container protocol ------------------------------------------------------
    #
    # Reading is pure and cheap, so it gets brackets. Writing has process
    # lifecycle consequences, so it gets verbs: mount() rather than assignment,
    # unmount() rather than del. An assignment that reserves capacity, validates
    # against a ceiling and can refuse for four different reasons is hiding too
    # much behind an "=", and a `del` that sends SIGTERM and waits out a grace
    # period is worse.

    def __getitem__(self, key):
        """
        A mounted registry by name, or on a lane, one of its jobs by name.

        Raises KeyError when there is no match, which is the container contract.
        get_job() stays as the softer form that returns None, the same split as
        dict[key] against dict.get(key).
        """
        with self._lock:
            if self._children:
                if key not in self._children:
                    raise KeyError(f"no registry named '{key}' is mounted in '{self.path()}'")
                return self._children[key]

            job = self.get_job(name=key)
            if job is None:
                raise KeyError(f"no job named '{key}' in '{self.path()}'")
            return job

    def __contains__(self, key):
        with self._lock:
            if self._children:
                return key in self._children
            return self.get_job(name=key) is not None

    def __len__(self):
        """Mounted registries, or jobs on a lane."""
        with self._lock:
            if self._children:
                return len(self._children)
            return len(self._registry)

    def __iter__(self):
        """
        The mounted registries, or the jobs on a lane. Well defined precisely
        because a registry never holds both.
        """
        with self._lock:
            if self._children:
                return iter(list(self._children.values()))
            return iter(list(self._registry.values()))

    def __repr__(self):
        with self._lock:
            what = f"{len(self._children)} lanes" if self._children else f"{len(self._registry)} jobs"
        limit = "unlimited" if self.max_jobs is None else f"max_jobs={self.max_jobs}"
        return f"<Registry {self.path()!r} {what} {limit}>"

    def print_registry(self, clear=False, max_print_jobs=20):
        """
        Pretty print a snapshot of the registry's current jobs.

        clear=True homes the cursor and wipes the screen first, so a polling
        loop redraws the table in place instead of scrolling. It is ignored when
        stdout is not a terminal, to keep escape codes out of piped output and
        log files.

        caps at 20 jobs, but the full registry is still available via get_job() and to_dict(). This can also be overriden by using the max_print_jobs param. After max is met only running jobs will be printed. This is to avoid flooding the screen with finished jobs.

        On a registry holding other registries this prints one table per lane,
        each with its own counts and ordering, followed by a line for the tree
        as a whole. That single view across every lane is a good part of why
        nesting is worth having.

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

        with self._lock:
            children = list(self._children.values())

        if children:
            for child in children:
                print()
                print(f"[ {child.path()} ]")
                child.print_registry(max_print_jobs=max_print_jobs)
            print()
            print(rule)
            print(" | ".join(self._summary_parts(self.all_jobs())))
            print(rule)
            return

        # Snapshot under the lock: the monitor thread removes finished jobs when
        # keep_jobs is False, which would otherwise change the dict mid-iteration.
        with self._lock:
            rows = list(self._registry.values())

        # Past the cap, outstanding work is what you want on screen rather than a
        # wall of finished jobs, so active ones are shown first and the rest fill
        # whatever room is left. The summary below still counts every job: it
        # describes the registry, not the part of it that fitted.
        display = rows
        if len(display) > max_print_jobs:
            display = [job for job in rows if job.is_active()] + [job for job in rows if not job.is_active()]
            display = display[:max_print_jobs]

        print(rule)
        print(header)
        print(rule)
        for job in display:
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

        print(" | ".join(self._summary_parts(rows)))
        print(rule)

    def _summary_parts(self, rows):
        """
        The footer under a table: counts, the ordering in force, uptime and
        where the shutdown policy stands.
        """
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
            # Only worth showing while something is actually waiting, since that
            # is the only time the ordering decides anything. A registry holding
            # lanes does not order anything itself, so it stays quiet about it.
            if self.is_leaf:
                policy = [self.dispatch_order]
                if self.priority_processing:
                    policy.append("priority")
                if self.aging:
                    policy.append("aging")
                summary.append("+".join(policy))
        if not self.is_leaf:
            summary.insert(1, f"{len(self._children)} lanes")
        summary.append(f"runtime {self.uptime():.1f}s")
        if self.terminate_behavior == "on_idle":
            # The idle clock only advances while nothing is outstanding, so it
            # reads as a countdown towards idle_time once the last job has gone.
            summary.append(f"idle {self.idle_seconds():.1f}s / {self.idle_time:g}s")
        else:
            summary.append("shutdown: manual")
        if self.is_shutdown:
            summary.append("SHUT DOWN")
        return summary
