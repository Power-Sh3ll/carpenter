from dataclasses import dataclass, fields
from typing import Any

# How the registry decides it is done supervising.
#   "manual"  - never shuts itself down; the owning application calls
#               shutdown() when it is ready. Correct for a long-lived server.
#   "on_idle" - shuts itself down once it has had no outstanding job for
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

# Which queued job the registry starts next, and the tie break between jobs of
# equal priority when priority_processing is on.
#   "fifo" - a queue. The job that has been waiting longest starts next.
#   "lifo" - a stack. The most recently submitted job starts next, which suits
#            work whose newest request is its most relevant one. Pair it with
#            aging, or the oldest job in a busy queue is never reached.
DISPATCH_ORDERS = ("fifo", "lifo")


class _Unset:
    """
    Marks a setting the caller never mentioned, which is not the same thing as
    a setting they deliberately set to the default value. A registry nested
    inside another one inherits every setting it left unset and keeps every
    setting it named, and only a distinct sentinel can tell those two apart:
    keep_jobs=False and "no opinion on keep_jobs" are indistinguishable once
    both have been read out of a plain dict with .get().

    NOTE: cls means "class" here, not "carpenter's settings".
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "UNSET"

    def __bool__(self):
        return False


UNSET = _Unset()

# The value each setting takes when neither the caller nor an enclosing
# registry has an opinion. Kept beside the dataclass rather than inside it so
# that "unset" and "default" stay separable: every field defaults to UNSET, and
# these are only consulted by resolve().
DEFAULTS = {
    "max_jobs": None,
    "max_cpus": 1,
    "max_memory": 1024,
    "dispatch_order": "fifo",
    "priority_processing": False,
    "aging": False,
    "age_step": 30.0,
    "age_max": None,
    "keep_jobs": False,
    "terminate_behavior": "manual",
    "idle_time": None,
    "poll_interval": 1.0,
    "shutdown_grace": 10,
    "output_mode": "capture",
    "output_dir": "job_logs",
    "max_capture_bytes": 1024 * 1024,
}


@dataclass
class Settings:
    """
    The settings for one registry, in either of two states.

    As written by a caller, any field they did not mention is UNSET. Calling
    resolve() returns a second Settings with every field filled in from the
    caller, then an enclosing registry, then DEFAULTS, and validates the result.
    A registry always holds a resolved instance, so reading a setting off a live
    registry never yields UNSET.

    Callers can build one directly or pass a plain dict, which is more
    convenient when the settings come from a config file:

        Settings(max_jobs=4, keep_jobs=True)
        Settings.from_dict({"max_jobs": 4, "keep_jobs": True})
    """

    max_jobs: Any = UNSET
    max_cpus: Any = UNSET
    max_memory: Any = UNSET
    dispatch_order: Any = UNSET
    priority_processing: Any = UNSET
    aging: Any = UNSET
    age_step: Any = UNSET
    age_max: Any = UNSET
    keep_jobs: Any = UNSET
    terminate_behavior: Any = UNSET
    idle_time: Any = UNSET
    poll_interval: Any = UNSET
    shutdown_grace: Any = UNSET
    output_mode: Any = UNSET
    output_dir: Any = UNSET
    max_capture_bytes: Any = UNSET

    @classmethod
    def names(cls):
        """Every recognised setting name."""
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_dict(cls, settings):
        """
        Build a Settings from a plain dict, rejecting anything unrecognised.

        A misspelled key used to be discarded in silence, so
        Registry({"idel_time": 5}) returned a "manual" registry with no
        complaint and the caller's intent was lost. Unknown keys raise here
        instead, matching how every other bad setting is handled.

        A Settings instance passes through untouched, so callers of this can
        accept either form without checking first.
        """
        if isinstance(settings, cls):
            return settings
        if settings is None:
            return cls()
        if not isinstance(settings, dict):
            raise TypeError(f"settings must be a Settings, a dict, or None, not {type(settings).__name__}")

        known = set(cls.names())
        unknown = sorted(set(settings) - known)
        if unknown:
            raise ValueError(
                f"unknown setting(s): {', '.join(unknown)}. "
                f"Known settings are: {', '.join(sorted(known))}"
            )
        return cls(**settings)

    def is_set(self, name):
        """Whether the caller gave this setting a value of their own."""
        if name not in self.names():
            raise ValueError(f"'{name}' is not a setting")
        return getattr(self, name) is not UNSET

    def resolve(self, parent=None):
        """
        Return a fully populated, validated copy.

        Each field is taken from this instance if it was set, then from parent
        if one was given, then from DEFAULTS. parent is expected to be an
        already resolved Settings, which is how a nested registry inherits from
        the one it is mounted in.
        """
        values = {}
        for name in self.names():
            value = getattr(self, name)
            if value is UNSET and parent is not None:
                value = getattr(parent, name)
            if value is UNSET:
                value = DEFAULTS[name]
            values[name] = value

        resolved = Settings(**values)
        resolved.validate()
        return resolved

    def validate(self):
        """
        Check a resolved Settings, raising ValueError on the first problem.

        This covers only what can be judged from the settings themselves.
        Whether the host actually has this many CPUs is a question about the
        machine rather than about the settings, so the registry checks it.
        """
        if self.max_jobs is not None:
            if not isinstance(self.max_jobs, int) or isinstance(self.max_jobs, bool) or self.max_jobs < 1:
                raise ValueError("max_jobs must be a positive integer or None for unlimited")
        if not isinstance(self.max_cpus, int) or isinstance(self.max_cpus, bool) or self.max_cpus < 1:
            raise ValueError("max_cpus must be a positive integer")
        if self.dispatch_order not in DISPATCH_ORDERS:
            raise ValueError(f"dispatch_order must be one of {DISPATCH_ORDERS}")
        if not isinstance(self.priority_processing, bool):
            raise ValueError("priority_processing must be a boolean")
        if not isinstance(self.aging, bool):
            raise ValueError("aging must be a boolean")
        if not isinstance(self.age_step, (int, float)) or isinstance(self.age_step, bool) or self.age_step <= 0:
            raise ValueError("age_step must be a positive number of seconds")
        if self.age_max is not None:
            if not isinstance(self.age_max, int) or isinstance(self.age_max, bool) or self.age_max < 1:
                raise ValueError("age_max must be a positive integer number of priority levels, or None for unbounded")

        if not isinstance(self.keep_jobs, bool):
            raise ValueError("keep_jobs must be a boolean")
        if not isinstance(self.max_memory, int) or isinstance(self.max_memory, bool) or self.max_memory < 1:
            raise ValueError("max_memory must be a positive integer")

        if self.terminate_behavior not in TERMINATE_BEHAVIORS:
            raise ValueError(f"terminate_behavior must be one of {TERMINATE_BEHAVIORS}")
        if self.terminate_behavior == "on_idle":
            if not isinstance(self.idle_time, (int, float)) or isinstance(self.idle_time, bool) or self.idle_time < 0:
                raise ValueError("terminate_behavior 'on_idle' requires idle_time to be a non-negative number of seconds")
        elif self.idle_time is not None:
            raise ValueError(f"idle_time is only meaningful for terminate_behavior 'on_idle', not '{self.terminate_behavior}'")
        if not isinstance(self.poll_interval, (int, float)) or isinstance(self.poll_interval, bool) or self.poll_interval <= 0:
            raise ValueError("poll_interval must be a positive number of seconds")
        if not isinstance(self.shutdown_grace, (int, float)) or isinstance(self.shutdown_grace, bool) or self.shutdown_grace < 0:
            raise ValueError("shutdown_grace must be a non-negative number of seconds")

        if self.output_mode not in OUTPUT_MODES:
            raise ValueError(f"output_mode must be one of {OUTPUT_MODES}")
        if not isinstance(self.max_capture_bytes, int) or isinstance(self.max_capture_bytes, bool) or self.max_capture_bytes < 1:
            raise ValueError("max_capture_bytes must be a positive integer")
        if self.output_mode == "file" and not isinstance(self.output_dir, str):
            raise ValueError("output_dir must be a string when output_mode is 'file'")

    def to_dict(self):
        """The settings as a plain dict, for logging or for handing back out."""
        return {name: getattr(self, name) for name in self.names()}
