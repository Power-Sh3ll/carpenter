import threading
import time

from carpenter.blueprint import Blueprint

# A job is "active" while it still represents outstanding work. Both queued and
# paused count: neither has produced a result yet, so the registry is not idle
# and must not shut itself down while either is present. Leaving "queued" out
# would let an on_idle registry with a full backlog and no free slots decide it
# had nothing to do.
ACTIVE_STATUSES = ("queued", "started", "paused")
TERMINAL_STATUSES = ("finished", "failed", "stopped", "terminated", "cancelled")


class Job:
    def __init__(self, name, blueprint=None) -> None:
        """
        Create a job with the given name. If blueprint is omitted, the job runs
        whatever default_blueprint the registry it's submitted to provides;
        passing one here overrides that default for this job only.

        A freshly created job is "initialized", which means it belongs to nobody
        yet. Handing it to a registry with submit_job() is what makes it the
        registry's problem, and moves it to "queued".
        """
        # ID is set by the registry when the job is first submitted.
        self.id = None
        self.name = name
        self.blueprint = blueprint
        self.status = "initialized"
        self.process = None
        self.start_time = None

        # Set when the registry accepts the job, so the time a job spends
        # waiting for a free slot is visible separately from the time it spends
        # running.
        self.submit_time = None

        # How many times this job has been spawned. A Job object can be
        # resubmitted after it reaches a terminal status, and this is what keeps
        # each run's log files distinct from the last run's.
        self.run = 0

        # Filled in by the registry once it observes the process exit.
        self.end_time = None
        self.exit_code = None

        # Set when the spawn itself failed, for a command that does not exist or
        # cannot be executed. exit_code stays None in that case, so a job that
        # never ran is distinguishable from one that ran and exited non-zero.
        self.error = None

        # Output, populated according to the registry's output_mode: text for
        # "capture", paths for "file", neither for "discard".
        self.stdout = ""
        self.stderr = ""
        self.stdout_path = None
        self.stderr_path = None
        self.output_truncated = False

        # Guards stdout/stderr while the registry's drain threads append to them.
        self._lock = threading.Lock()
        self._drains = []

        # validate that the name is a non-empty string
        if not isinstance(name, str) or not name:
            raise ValueError("name must be a non-empty string")
        if len(name) > 255:
            raise ValueError("name must be less than 256 characters")
        if not name.isidentifier():
            raise ValueError("name must be a valid identifier (alphanumeric and underscores only, cannot start with a number)")
        if not name.islower():
            raise ValueError("name must be lowercase")
        if blueprint is not None and not isinstance(blueprint, Blueprint):
            raise ValueError("blueprint must be a Blueprint instance or None")

    def is_active(self) -> bool:
        """
        True while the job still represents outstanding work, whether it is
        waiting for a slot, running, or paused. The registry uses this to decide
        whether it is idle.
        """
        return self.status in ACTIVE_STATUSES

    def is_finished(self) -> bool:
        """True once the job has reached a terminal status."""
        return self.status in TERMINAL_STATUSES

    def is_running(self) -> bool:
        """
        True while the job holds a slot in the registry. A paused job still does:
        its process exists and is resident, it is merely not being scheduled.
        """
        return self.status in ("started", "paused")

    def duration(self):
        """
        Seconds the job has been running, or how long it ran if it has exited.
        None if it was never spawned, which includes a job still waiting for a
        free slot.
        """
        if self.start_time is None:
            return None
        return (self.end_time if self.end_time is not None else time.time()) - self.start_time

    def waited(self):
        """
        Seconds between being accepted by the registry and being spawned, or how
        long it has been waiting so far if it is still queued. None if it was
        never submitted.

        A job that stopped being outstanding without ever running, because it
        was cancelled or because its spawn failed, stops counting at that point
        rather than climbing forever.
        """
        if self.submit_time is None:
            return None
        stopped_waiting = self.start_time if self.start_time is not None else self.end_time
        if stopped_waiting is None:
            stopped_waiting = time.time()
        return stopped_waiting - self.submit_time

    def reset(self):
        """
        Clear the result of a previous run so the job can be spawned again.

        Called by the registry when a job in a terminal status is resubmitted.
        The identity of the job survives: the same ID, name and blueprint, with
        the run counter carried forward so this run's logs do not overwrite the
        last run's.
        """
        self.process = None
        self.start_time = None
        self.end_time = None
        self.exit_code = None
        self.error = None
        self.stdout = ""
        self.stderr = ""
        self.stdout_path = None
        self.stderr_path = None
        self.output_truncated = False
        self._drains = []

    def output(self):
        """Read a consistent snapshot of the captured output as (stdout, stderr)."""
        with self._lock:
            return self.stdout, self.stderr

    def to_dict(self, include_output=False) -> dict:
        """
        A JSON-serialisable view of the job, for handing straight back out of a
        web framework. Output is opt-in because it can be large.
        """
        payload = {
            "id": str(self.id) if self.id is not None else None,
            "name": self.name,
            "status": self.status,
            "pid": self.process.pid if self.process is not None else None,
            "run": self.run,
            "exit_code": self.exit_code,
            "error": str(self.error) if self.error is not None else None,
            "submit_time": self.submit_time,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": self.duration(),
            "waited": self.waited(),
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
        }
        if include_output:
            stdout, stderr = self.output()
            payload["stdout"] = stdout
            payload["stderr"] = stderr
            payload["output_truncated"] = self.output_truncated
        return payload
