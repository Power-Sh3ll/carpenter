import threading
import time

from carpenter.blueprint import Blueprint

# A job is "active" while it still represents outstanding work. A paused job
# counts: it has not produced its result yet, so the registry is not idle.
ACTIVE_STATUSES = ("started", "paused")
TERMINAL_STATUSES = ("finished", "failed", "stopped", "terminated")


class Job:
    def __init__(self, name, blueprint=None) -> None:
        """
        Create a job with the given name. If blueprint is omitted, the job runs
        whatever default_blueprint the registry it's started in provides;
        passing one here overrides that default for this job only. The job is
        not registered with the registry until register_job() is called, and
        its process is not spawned until start_job() is called.
        """
        # ID is set by the registry when the job is registered
        self.id = None
        self.name = name
        self.blueprint = blueprint
        self.status = "initialized"
        self.process = None
        self.start_time = None

        # Filled in by the registry once it observes the process exit.
        self.end_time = None
        self.exit_code = None

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
        True while the job still represents outstanding work. The registry uses
        this to decide whether it is idle.
        """
        return self.status in ACTIVE_STATUSES

    def is_finished(self) -> bool:
        """True once the job has reached a terminal status."""
        return self.status in TERMINAL_STATUSES

    def duration(self):
        """
        Seconds the job has been running, or how long it ran if it has exited.
        None if it was never started.
        """
        if self.start_time is None:
            return None
        return (self.end_time if self.end_time is not None else time.time()) - self.start_time

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
            "exit_code": self.exit_code,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": self.duration(),
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
        }
        if include_output:
            stdout, stderr = self.output()
            payload["stdout"] = stdout
            payload["stderr"] = stderr
            payload["output_truncated"] = self.output_truncated
        return payload
