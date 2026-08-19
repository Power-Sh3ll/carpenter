import pytest

from carpenter.settings import DEFAULTS, UNSET, Settings


def test_unmentioned_settings_stay_unset():
    settings = Settings(max_jobs=4)
    assert settings.is_set("max_jobs")
    assert not settings.is_set("keep_jobs")
    assert settings.keep_jobs is UNSET


def test_set_to_the_default_value_is_not_the_same_as_unset():
    """
    The distinction a plain dict cannot make, and the reason Settings exists:
    a caller who says keep_jobs=False has expressed an opinion, and a nested
    registry must not overwrite it with an enclosing registry's True.
    """
    explicit = Settings(keep_jobs=False)
    silent = Settings()

    assert explicit.is_set("keep_jobs")
    assert not silent.is_set("keep_jobs")

    parent = Settings(keep_jobs=True).resolve()
    assert explicit.resolve(parent).keep_jobs is False
    assert silent.resolve(parent).keep_jobs is True


def test_resolve_fills_from_defaults():
    resolved = Settings().resolve()
    for name, expected in DEFAULTS.items():
        assert getattr(resolved, name) == expected


def test_resolve_prefers_own_value_over_parent():
    parent = Settings(poll_interval=5.0, keep_jobs=True).resolve()
    child = Settings(poll_interval=0.1).resolve(parent)

    assert child.poll_interval == 0.1
    assert child.keep_jobs is True


def test_from_dict_rejects_unknown_keys():
    with pytest.raises(ValueError, match="idel_time"):
        Settings.from_dict({"idel_time": 5})


def test_from_dict_error_names_the_known_settings():
    with pytest.raises(ValueError, match="idle_time"):
        Settings.from_dict({"idel_time": 5})


def test_from_dict_accepts_a_settings_instance_unchanged():
    settings = Settings(max_jobs=2)
    assert Settings.from_dict(settings) is settings


def test_from_dict_accepts_none():
    assert Settings.from_dict(None) == Settings()


def test_from_dict_rejects_other_types():
    with pytest.raises(TypeError):
        Settings.from_dict(["max_jobs", 2])


@pytest.mark.parametrize(
    "settings",
    [
        {"max_jobs": 0},
        {"max_jobs": -1},
        {"max_jobs": 1.5},
        {"max_jobs": True},
        {"max_cpus": 0},
        {"keep_jobs": "yes"},
        {"max_memory": 0},
        {"terminate_behavior": "whenever"},
        {"terminate_behavior": "on_idle"},
        {"terminate_behavior": "on_idle", "idle_time": -1},
        {"idle_time": 5},
        {"poll_interval": 0},
        {"shutdown_grace": -1},
        {"output_mode": "somewhere"},
        {"max_capture_bytes": 0},
        {"output_mode": "file", "output_dir": 7},
    ],
)
def test_invalid_settings_raise(settings):
    with pytest.raises(ValueError):
        Settings.from_dict(settings).resolve()


def test_max_jobs_none_means_unlimited():
    assert Settings.from_dict({"max_jobs": None}).resolve().max_jobs is None


def test_idle_time_is_rejected_for_manual_shutdown():
    with pytest.raises(ValueError, match="idle_time"):
        Settings.from_dict({"idle_time": 5}).resolve()


def test_to_dict_round_trips():
    resolved = Settings(max_jobs=3, keep_jobs=True).resolve()
    assert Settings.from_dict(resolved.to_dict()).resolve() == resolved
