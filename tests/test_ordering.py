"""
The ordering as the registry actually applies it.

The rules themselves are covered exhaustively in test_dispatch.py without any
processes. These check the plumbing: that the registry consults the ranking, and
that the job it starts next is the one the ranking named.

Each test fills the single slot with a long running job first, so everything
submitted afterwards is reliably queued and no test depends on winning a race.
"""

from carpenter import Job


def blocked_registry(registry, sleeper, **settings):
    """A registry with its one slot occupied, and the job occupying it."""
    settings.setdefault("max_jobs", 1)
    settings.setdefault("keep_jobs", True)
    reg = registry(settings, sleeper(30))
    blocker = reg.submit_job(Job("blocker"))
    assert blocker.status == "started"
    return reg, blocker


def release(reg, blocker):
    """Free the slot and let the registry fill it, without waiting on a clock."""
    reg.stop_job(blocker)
    blocker.process.wait()
    reg.poll_jobs()


def names(jobs):
    return [job.name for job in jobs]


def test_fifo_through_the_registry(registry, sleeper):
    reg, blocker = blocked_registry(registry, sleeper)
    for name in ("first", "second", "third"):
        reg.submit_job(Job(name))

    assert names(reg.queued_jobs()) == ["first", "second", "third"]

    release(reg, blocker)
    assert reg.get_job(name="first").status == "started"


def test_lifo_through_the_registry(registry, sleeper):
    reg, blocker = blocked_registry(registry, sleeper, dispatch_order="lifo")
    for name in ("first", "second", "third"):
        reg.submit_job(Job(name))

    assert names(reg.queued_jobs()) == ["third", "second", "first"]

    release(reg, blocker)
    assert reg.get_job(name="third").status == "started"
    assert reg.get_job(name="first").status == "queued"


def test_priority_through_the_registry(registry, sleeper):
    reg, blocker = blocked_registry(registry, sleeper, priority_processing=True)
    reg.submit_job(Job("routine", priority=0))
    reg.submit_job(Job("urgent", priority=100))
    reg.submit_job(Job("background", priority=-10))

    assert names(reg.queued_jobs()) == ["urgent", "routine", "background"]

    release(reg, blocker)
    assert reg.get_job(name="urgent").status == "started"


def test_priority_is_inert_until_switched_on(registry, sleeper):
    reg, blocker = blocked_registry(registry, sleeper)
    reg.submit_job(Job("routine", priority=0))
    reg.submit_job(Job("urgent", priority=100))

    assert names(reg.queued_jobs()) == ["routine", "urgent"]

    release(reg, blocker)
    assert reg.get_job(name="routine").status == "started"


def test_priority_can_be_changed_while_queued(registry, sleeper):
    reg, blocker = blocked_registry(registry, sleeper, priority_processing=True)
    reg.submit_job(Job("first"))
    late = reg.submit_job(Job("late"))

    assert names(reg.queued_jobs()) == ["first", "late"]

    late.priority = 50
    assert names(reg.queued_jobs()) == ["late", "first"]

    release(reg, blocker)
    assert late.status == "started"


def test_aging_promotes_a_starved_job_through_the_registry(registry, sleeper):
    reg, blocker = blocked_registry(
        registry, sleeper, dispatch_order="lifo", aging=True, age_step=10
    )
    old = reg.submit_job(Job("old"))
    fresh = reg.submit_job(Job("fresh"))

    # LIFO while both are inside the same step.
    assert names(reg.queued_jobs()) == ["fresh", "old"]

    # Backdate the wait rather than sleeping for it. queued_at is monotonic, so
    # this is the same thing the passage of time would have done.
    old.queued_at -= 60

    assert names(reg.queued_jobs()) == ["old", "fresh"]

    release(reg, blocker)
    assert old.status == "started"
    assert fresh.status == "queued"


def test_age_max_bounds_the_promotion(registry, sleeper):
    reg, blocker = blocked_registry(
        registry,
        sleeper,
        priority_processing=True,
        aging=True,
        age_step=10,
        age_max=2,
    )
    ancient = reg.submit_job(Job("ancient", priority=0))
    urgent = reg.submit_job(Job("urgent", priority=5))

    ancient.queued_at -= 10_000

    assert reg.effective_priority(ancient) == 2
    assert reg.effective_priority(urgent) == 5
    assert names(reg.queued_jobs()) == ["urgent", "ancient"]

    release(reg, blocker)
    assert urgent.status == "started"


def test_effective_priority_reports_zero_when_nothing_is_switched_on(registry, sleeper):
    reg, _ = blocked_registry(registry, sleeper)
    job = reg.submit_job(Job("job", priority=99))
    assert reg.effective_priority(job) == 0


def test_the_whole_backlog_drains_in_priority_order(registry, instant):
    reg = registry({"max_jobs": 1, "keep_jobs": True, "priority_processing": True}, instant)

    # The first submission finds the slot free and starts at once whatever its
    # weight, so the ordering being checked is over everything after it.
    reg.submit_job(Job("opener"))
    jobs = [
        reg.submit_job(Job("low", priority=1)),
        reg.submit_job(Job("high", priority=10)),
        reg.submit_job(Job("middle", priority=5)),
    ]
    assert reg.wait_for_jobs(timeout=30)

    started_in_order = sorted(jobs, key=lambda job: job.start_time)
    assert names(started_in_order) == ["high", "middle", "low"]


def test_resubmitting_puts_a_job_at_the_back_of_a_fifo_queue(registry, sleeper, instant):
    reg, blocker = blocked_registry(registry, sleeper)

    early = reg.submit_job(Job("early", blueprint=instant))
    reg.submit_job(Job("later"))

    # Take the early job out and put it back; it is now the newest arrival.
    reg.cancel_job(early)
    reg.submit_job(early)

    assert names(reg.queued_jobs()) == ["later", "early"]
