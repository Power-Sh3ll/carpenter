import subprocess


class Blueprint:
    def __init__(self, command, cwd=None, env=None) -> None:
        """
        Describes a reusable subprocess template. spawn() launches a fresh,
        independent process each time it's called, so one Blueprint can back
        any number of Jobs.
        """
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise ValueError("command must be a non-empty list of strings")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("cwd must be a string or None")
        if env is not None and not isinstance(env, dict):
            raise ValueError("env must be a dict or None")

        self.command = command
        self.cwd = cwd
        self.env = env

    def spawn(self) -> subprocess.Popen:
        """
        Launch a new, independent subprocess based on this blueprint. Safe to
        call multiple times; each call returns its own Popen instance.
        """
        return subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
        )
