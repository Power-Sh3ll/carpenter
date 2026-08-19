import sys

import pytest

from carpenter import Blueprint, Registry


@pytest.fixture
def sleeper():
    """A blueprint for a job that runs for a given number of seconds and exits 0."""

    def make(seconds=0.2):
        return Blueprint([sys.executable, "-c", f"import time; time.sleep({seconds})"])

    return make


@pytest.fixture
def instant():
    """A blueprint for a job that exits 0 immediately."""
    return Blueprint([sys.executable, "-c", "pass"])


@pytest.fixture
def failing():
    """A blueprint for a job that exits non-zero."""
    return Blueprint([sys.executable, "-c", "raise SystemExit(3)"])


@pytest.fixture
def unspawnable():
    """
    A blueprint whose command does not exist, so the spawn itself raises rather
    than the process failing.
    """
    return Blueprint(["carpenter-no-such-command-exists"])


@pytest.fixture
def registry():
    """
    Build registries and guarantee they are shut down, so a failing assertion
    cannot leave a monitor thread and a handful of subprocesses behind.
    """
    built = []

    def make(settings=None, default_blueprint=None, **kwargs):
        reg = Registry(settings, default_blueprint=default_blueprint, **kwargs)
        built.append(reg)
        return reg

    yield make

    for reg in built:
        try:
            reg.shutdown(grace=0)
        except Exception:
            pass
