"""
Registries mounted inside registries: the leaf rule, settings inheritance,
capacity partitioning, one monitor for the tree, and shutdown order.
"""

import threading

import pytest

from carpenter import Job, Registry, Settings


def lane(name, **settings):
    return Registry(settings, name=name)


def test_a_named_registry_mounts(registry, instant):
    app = registry({"max_jobs": 4}, instant, name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=2))

    assert app["thumbnails"] is thumbs
    assert thumbs.parent is app
    assert thumbs.root() is app
    assert app.children() == [thumbs]
    assert app.is_leaf is False
    assert thumbs.is_leaf is True


def test_mounting_needs_a_name(registry, instant):
    app = registry({}, instant, name="app")
    with pytest.raises(ValueError, match="given a name"):
        app.mount(Registry({}))


def test_registry_names_follow_the_job_name_rule(registry):
    for bad in ["Thumbnails", "thumb-nails", "0lane", "", "thumb nails"]:
        with pytest.raises(ValueError):
            Registry({}, name=bad)


def test_a_registry_holds_jobs_or_lanes_but_not_both(registry, instant):
    app = registry({}, instant, name="app")
    app.mount(lane("thumbnails"))

    with pytest.raises(RuntimeError, match="cannot also hold jobs"):
        app.submit_job(Job("stray"))


def test_a_registry_with_jobs_cannot_take_lanes(registry, instant):
    app = registry({}, instant, name="app")
    app.submit_job(Job("stray"))

    with pytest.raises(RuntimeError, match="cannot also hold registries"):
        app.mount(lane("thumbnails"))


def test_a_lane_must_be_empty_when_mounted(registry, instant):
    app = registry({}, instant, name="app")
    thumbs = lane("thumbnails")
    thumbs.default_blueprint = instant
    thumbs.submit_job(Job("early"))

    with pytest.raises(RuntimeError, match="mount it before submitting"):
        app.mount(thumbs)
    thumbs.shutdown(grace=0)


def test_duplicate_lane_names_are_refused(registry, instant):
    app = registry({}, instant, name="app")
    app.mount(lane("thumbnails"))
    with pytest.raises(ValueError, match="already mounted here"):
        app.mount(lane("thumbnails"))


def test_a_lane_cannot_be_mounted_twice(registry, instant):
    app = registry({}, instant, name="app")
    other = registry({}, instant, name="other")
    thumbs = lane("thumbnails")
    app.mount(thumbs)

    with pytest.raises(RuntimeError, match="already mounted"):
        other.mount(thumbs)


def test_cycles_are_refused(registry, instant):
    app = registry({}, instant, name="app")
    middle = lane("middle")
    app.mount(middle)

    with pytest.raises(ValueError, match="cycle"):
        middle.mount(app)
    with pytest.raises(ValueError, match="itself"):
        app.mount(app)


# Settings inheritance


def test_a_lane_inherits_what_it_did_not_set(registry, instant):
    app = registry({"keep_jobs": True, "poll_interval": 0.25, "max_jobs": 4}, instant, name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=2))

    assert thumbs.keep_jobs is True
    assert thumbs.poll_interval == 0.25
    assert thumbs.max_jobs == 2


def test_a_lane_keeps_what_it_did_set(registry, instant):
    """
    The distinction a plain dict could not make. The lane says keep_jobs=False,
    which is also the default, and that opinion has to survive a parent saying
    True.
    """
    app = registry({"keep_jobs": True, "max_jobs": 4}, instant, name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=2, keep_jobs=False))

    assert thumbs.keep_jobs is False
    assert app.keep_jobs is True


def test_lanes_can_order_themselves_differently(registry, instant):
    app = registry({"max_jobs": 6}, instant, name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=2, dispatch_order="lifo"))
    renders = app.mount(lane("renders", max_jobs=4, priority_processing=True))

    assert thumbs.dispatch_order == "lifo"
    assert renders.dispatch_order == "fifo"
    assert renders.priority_processing is True
    assert thumbs.priority_processing is False


def test_a_lane_inherits_the_default_blueprint(registry, instant):
    app = registry({}, instant, name="app")
    thumbs = app.mount(lane("thumbnails"))

    job = thumbs.submit_job(Job("thumb"))
    assert thumbs.blueprint_for(job) is instant
    assert job.status == "started"


def test_a_lane_can_override_the_default_blueprint(registry, instant, failing):
    app = registry({"keep_jobs": True}, instant, name="app")
    thumbs = Registry({}, default_blueprint=failing, name="thumbnails")
    app.mount(thumbs)

    job = thumbs.submit_job(Job("thumb"))
    assert thumbs.blueprint_for(job) is failing


# Capacity


def test_lane_limits_must_fit_inside_the_parent(registry, instant):
    app = registry({"max_jobs": 4}, instant, name="app")
    app.mount(lane("thumbnails", max_jobs=2))

    with pytest.raises(ValueError, match="would claim"):
        app.mount(lane("renders", max_jobs=3))


def test_lane_limits_may_exactly_fill_the_parent(registry, instant):
    app = registry({"max_jobs": 4}, instant, name="app")
    app.mount(lane("thumbnails", max_jobs=2))
    app.mount(lane("renders", max_jobs=2))
    assert len(app) == 2


def test_a_lane_that_names_no_limit_inherits_the_parents(registry, instant):
    """
    Inheritance settles this before the capacity check sees it, so a lane with
    no opinion is bounded by its parent rather than being unlimited inside it.
    One such lane may therefore use the whole allowance, and a second cannot.
    """
    app = registry({"max_jobs": 4}, instant, name="app")
    renders = app.mount(lane("renders"))

    assert renders.max_jobs == 4
    with pytest.raises(ValueError, match="would claim"):
        app.mount(lane("thumbnails"))


def test_a_lane_that_insists_on_being_unlimited_is_refused(registry, instant):
    """
    Explicitly asking for no limit is different from saying nothing, and inside
    a registry that does have a limit it cannot be honoured.
    """
    app = registry({"max_jobs": 4}, instant, name="app")
    with pytest.raises(ValueError, match="no max_jobs"):
        app.mount(lane("renders", max_jobs=None))


def test_an_unlimited_parent_accepts_anything(registry, instant):
    app = registry({}, instant, name="app")
    app.mount(lane("thumbnails", max_jobs=2))
    app.mount(lane("renders"))
    assert len(app) == 2


def test_lanes_do_not_take_each_others_slots(registry, sleeper):
    app = registry({"max_jobs": 4, "keep_jobs": True}, sleeper(30), name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=1))
    renders = app.mount(lane("renders", max_jobs=3))

    for i in range(5):
        thumbs.submit_job(Job(f"thumb_{i}"))

    assert len(thumbs.running_jobs()) == 1
    assert len(thumbs.queued_jobs()) == 4

    # The render lane's three slots were never available to the thumbnails,
    # which is the whole point of partitioning rather than sharing.
    assert renders.available_slots() == 3
    renders.submit_job(Job("render_0"))
    assert renders.get_job(name="render_0").status == "started"


def test_available_slots_adds_up_across_lanes(registry, sleeper):
    app = registry({"max_jobs": 4, "keep_jobs": True}, sleeper(30), name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=1))
    app.mount(lane("renders", max_jobs=3))

    assert app.available_slots() == 4
    thumbs.submit_job(Job("thumb"))
    assert app.available_slots() == 3


# One monitor for the tree


def test_the_tree_has_exactly_one_monitor_thread(registry, sleeper):
    app = registry({"max_jobs": 4, "keep_jobs": True, "poll_interval": 0.05}, sleeper(2), name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=2))
    renders = app.mount(lane("renders", max_jobs=2))

    thumbs.submit_job(Job("thumb"))
    renders.submit_job(Job("render"))

    monitors = [t for t in threading.enumerate() if t.name == "carpenter-monitor"]
    assert len(monitors) == 1
    assert app._monitor is not None
    assert thumbs._monitor is None
    assert renders._monitor is None


def test_lanes_share_one_lock(registry, instant):
    app = registry({}, instant, name="app")
    thumbs = app.mount(lane("thumbnails"))
    assert thumbs._lock is app._lock


def test_the_monitor_drives_every_lane(registry, instant):
    app = registry({"max_jobs": 2, "keep_jobs": True, "poll_interval": 0.05}, instant, name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=1))
    renders = app.mount(lane("renders", max_jobs=1))

    jobs = [thumbs.submit_job(Job(f"thumb_{i}")) for i in range(3)]
    jobs += [renders.submit_job(Job(f"render_{i}")) for i in range(3)]

    assert app.wait_for_jobs(timeout=30)
    assert all(job.status == "finished" for job in jobs), [j.status for j in jobs]


# Tree-wide views


def test_the_tree_reports_every_job(registry, sleeper):
    app = registry({"max_jobs": 4, "keep_jobs": True}, sleeper(30), name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=1))
    renders = app.mount(lane("renders", max_jobs=1))

    thumbs.submit_job(Job("thumb_0"))
    thumbs.submit_job(Job("thumb_1"))
    renders.submit_job(Job("render_0"))

    assert len(app.all_jobs()) == 3
    assert len(app.running_jobs()) == 2
    assert len(app.queued_jobs()) == 1
    assert len(app.active_jobs()) == 3


def test_get_job_searches_the_tree(registry, sleeper):
    app = registry({"keep_jobs": True}, sleeper(30), name="app")
    renders = app.mount(lane("renders"))
    job = renders.submit_job(Job("render_0"))

    assert app.get_job(name="render_0") is job
    assert app.get_job(id=job.id) is job
    assert app.get_job(name="nonexistent") is None


def test_a_busy_lane_keeps_the_tree_out_of_its_idle_window(registry, sleeper):
    app = registry(
        {"max_jobs": 2, "terminate_behavior": "on_idle", "idle_time": 0, "keep_jobs": True},
        sleeper(30),
        name="app",
    )
    thumbs = app.mount(lane("thumbnails", max_jobs=1))
    app.mount(lane("renders", max_jobs=1))

    thumbs.submit_job(Job("thumb"))

    assert app.idle_seconds() == 0.0
    assert app.should_terminate() is False


def test_cancel_from_the_top_finds_the_right_lane(registry, sleeper):
    app = registry({"max_jobs": 2, "keep_jobs": True}, sleeper(30), name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=1))
    app.mount(lane("renders", max_jobs=1))

    thumbs.submit_job(Job("running"))
    queued = thumbs.submit_job(Job("waiting"))

    app.cancel_job(queued)

    assert queued.status == "cancelled"
    assert thumbs.queued_jobs() == []


# Lifecycle


def test_shutting_down_the_tree_brings_down_every_lane(registry, sleeper):
    app = registry({"max_jobs": 2, "keep_jobs": True}, sleeper(30), name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=1))
    renders = app.mount(lane("renders", max_jobs=1))

    running = thumbs.submit_job(Job("thumb"))
    queued = thumbs.submit_job(Job("thumb_waiting"))
    other = renders.submit_job(Job("render"))

    app.shutdown(grace=0)

    assert app.is_shutdown
    assert thumbs.is_shutdown
    assert renders.is_shutdown
    assert running.is_finished()
    assert other.is_finished()
    assert queued.status == "cancelled"


def test_lanes_come_down_before_their_parent(registry, sleeper):
    order = []
    app = registry(
        {"max_jobs": 2},
        sleeper(30),
        name="app",
        on_shutdown=lambda reg, reason: order.append(reg.name),
    )
    app.mount(Registry({"max_jobs": 1}, name="thumbnails", on_shutdown=lambda reg, reason: order.append(reg.name)))
    app.mount(Registry({"max_jobs": 1}, name="renders", on_shutdown=lambda reg, reason: order.append(reg.name)))

    app.shutdown(grace=0)

    assert order == ["thumbnails", "renders", "app"]


def test_unmount_brings_the_lane_down_and_returns_it(registry, sleeper):
    app = registry({"max_jobs": 2, "keep_jobs": True}, sleeper(30), name="app")
    thumbs = app.mount(lane("thumbnails", max_jobs=1))
    running = thumbs.submit_job(Job("thumb"))
    queued = thumbs.submit_job(Job("thumb_waiting"))

    returned = app.unmount("thumbnails", grace=0)

    assert returned is thumbs
    assert thumbs.parent is None
    assert "thumbnails" not in app
    assert running.is_finished()
    assert queued.status == "cancelled"
    # Handed back so its results can still be read.
    assert returned.get_job(name="thumb") is running


def test_unmount_frees_the_capacity_it_claimed(registry, instant):
    app = registry({"max_jobs": 4}, instant, name="app")
    app.mount(lane("thumbnails", max_jobs=3))

    with pytest.raises(ValueError, match="would claim"):
        app.mount(lane("renders", max_jobs=2))

    app.unmount("thumbnails", grace=0)
    app.mount(lane("renders", max_jobs=2))
    assert "renders" in app


def test_unmounting_something_that_is_not_there(registry, instant):
    app = registry({}, instant, name="app")
    with pytest.raises(KeyError):
        app.unmount("nothing")


# Container protocol


def test_brackets_read_lanes_and_jobs(registry, sleeper):
    app = registry({"keep_jobs": True}, sleeper(30), name="app")
    renders = app.mount(lane("renders"))
    job = renders.submit_job(Job("render_0"))

    assert app["renders"] is renders
    assert app["renders"]["render_0"] is job


def test_missing_keys_raise_keyerror(registry, instant):
    app = registry({"keep_jobs": True}, instant, name="app")
    renders = app.mount(lane("renders"))

    with pytest.raises(KeyError):
        app["nope"]
    with pytest.raises(KeyError):
        renders["nope"]
    # get_job stays the soft form.
    assert renders.get_job(name="nope") is None


def test_contains_and_len_and_iteration(registry, sleeper):
    app = registry({"keep_jobs": True}, sleeper(30), name="app")
    thumbs = app.mount(lane("thumbnails"))
    app.mount(lane("renders"))
    thumbs.submit_job(Job("thumb_0"))
    thumbs.submit_job(Job("thumb_1"))

    assert "thumbnails" in app
    assert "nope" not in app
    assert len(app) == 2
    assert [reg.name for reg in app] == ["thumbnails", "renders"]

    assert "thumb_0" in thumbs
    assert len(thumbs) == 2
    assert sorted(job.name for job in thumbs) == ["thumb_0", "thumb_1"]


def test_path_reports_position_in_the_tree(registry, instant):
    app = registry({}, instant, name="app")
    thumbs = app.mount(lane("thumbnails"))
    assert app.path() == "app"
    assert thumbs.path() == "app.thumbnails"


def test_settings_resolve_is_what_drives_inheritance():
    """
    The mechanism behind all of the above, on its own.
    """
    parent = Settings(keep_jobs=True, poll_interval=0.25).resolve()
    child = Settings(max_jobs=2, keep_jobs=False).resolve(parent)

    assert child.max_jobs == 2
    assert child.keep_jobs is False
    assert child.poll_interval == 0.25
