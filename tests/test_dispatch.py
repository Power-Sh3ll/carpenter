"""
The dispatch decision on its own: given a waiting list, some settings and an
instant, which job should start next.

Nothing here spawns a process, starts a thread, sleeps, or reads a clock. That
is the point of keeping the ordering in its own module: every rule about
priority, tie breaks and aging can be checked exhaustively and deterministically,
leaving the tests that need real processes to cover only the plumbing.
"""

import pytest

from carpenter import Job, Settings
from carpenter.dispatch import aging_boost, effective_priority, order_waiting


def resolve(**overrides):
    return Settings.from_dict(overrides).resolve()


def waiting(name, sequence, priority=0, queued_at=0.0):
    """A job in the state the registry leaves it in once it has been queued."""
    job = Job(name, priority=priority)
    job.status = "queued"
    job.sequence = sequence
    job.queued_at = queued_at
    return job


def names(jobs):
    return [job.name for job in jobs]


def test_fifo_is_the_default():
    settings = resolve()
    jobs = [waiting("a", 0), waiting("b", 1), waiting("c", 2)]
    assert names(order_waiting(jobs, settings, 0)) == ["a", "b", "c"]


def test_lifo_reverses_submission_order():
    settings = resolve(dispatch_order="lifo")
    jobs = [waiting("a", 0), waiting("b", 1), waiting("c", 2)]
    assert names(order_waiting(jobs, settings, 0)) == ["c", "b", "a"]


def test_ordering_does_not_modify_the_input():
    settings = resolve(dispatch_order="lifo")
    jobs = [waiting("a", 0), waiting("b", 1)]
    order_waiting(jobs, settings, 0)
    assert names(jobs) == ["a", "b"]


def test_priority_is_ignored_unless_it_is_switched_on():
    settings = resolve()
    jobs = [waiting("low", 0, priority=0), waiting("high", 1, priority=100)]
    assert names(order_waiting(jobs, settings, 0)) == ["low", "high"]


def test_priority_outranks_arrival_order():
    settings = resolve(priority_processing=True)
    jobs = [waiting("low", 0, priority=0), waiting("high", 1, priority=100)]
    assert names(order_waiting(jobs, settings, 0)) == ["high", "low"]


def test_equal_priorities_fall_back_to_fifo():
    settings = resolve(priority_processing=True, dispatch_order="fifo")
    jobs = [waiting("a", 0, priority=5), waiting("b", 1, priority=5)]
    assert names(order_waiting(jobs, settings, 0)) == ["a", "b"]


def test_equal_priorities_fall_back_to_lifo():
    settings = resolve(priority_processing=True, dispatch_order="lifo")
    jobs = [waiting("a", 0, priority=5), waiting("b", 1, priority=5)]
    assert names(order_waiting(jobs, settings, 0)) == ["b", "a"]


def test_all_four_combinations_are_distinct():
    """
    priority_processing and dispatch_order are orthogonal, which is why they are
    two settings rather than one. Each pairing means something different.
    """
    jobs = [
        waiting("first_low", 0, priority=0),
        waiting("second_high", 1, priority=10),
        waiting("third_low", 2, priority=0),
    ]
    assert names(order_waiting(jobs, resolve(), 0)) == ["first_low", "second_high", "third_low"]
    assert names(order_waiting(jobs, resolve(dispatch_order="lifo"), 0)) == ["third_low", "second_high", "first_low"]
    assert names(order_waiting(jobs, resolve(priority_processing=True), 0)) == ["second_high", "first_low", "third_low"]
    assert names(order_waiting(jobs, resolve(priority_processing=True, dispatch_order="lifo"), 0)) == ["second_high", "third_low", "first_low"]


def test_negative_priority_sinks_a_job():
    settings = resolve(priority_processing=True)
    jobs = [waiting("background", 0, priority=-5), waiting("normal", 1, priority=0)]
    assert names(order_waiting(jobs, settings, 0)) == ["normal", "background"]


# Aging


def test_no_boost_before_a_full_step_has_passed():
    settings = resolve(aging=True, age_step=10)
    job = waiting("job", 0, queued_at=0)
    assert aging_boost(job, settings, 9.9) == 0


def test_one_level_per_step():
    settings = resolve(aging=True, age_step=10)
    job = waiting("job", 0, queued_at=0)
    assert aging_boost(job, settings, 10) == 1
    assert aging_boost(job, settings, 25) == 2
    assert aging_boost(job, settings, 100) == 10


def test_age_max_caps_the_boost():
    settings = resolve(aging=True, age_step=10, age_max=3)
    job = waiting("job", 0, queued_at=0)
    assert aging_boost(job, settings, 1000) == 3


def test_no_boost_when_aging_is_off():
    settings = resolve(age_step=10)
    job = waiting("job", 0, queued_at=0)
    assert aging_boost(job, settings, 1000) == 0


def test_aging_does_not_defeat_lifo_within_a_step():
    """
    The reason the boost is quantised rather than continuous.

    A boost that climbed smoothly would give the longest waiting job a strictly
    larger number at every instant, so it would outrank every newer job the
    moment aging was switched on and "lifo" would behave as "fifo". Whole steps
    leave everything inside the same step to be ordered normally.
    """
    settings = resolve(dispatch_order="lifo", aging=True, age_step=10)
    jobs = [waiting("a", 0, queued_at=0), waiting("b", 1, queued_at=3), waiting("c", 2, queued_at=6)]

    # At t=9 nothing has completed a step, so LIFO is untouched.
    assert names(order_waiting(jobs, settings, 9)) == ["c", "b", "a"]


def test_aging_rescues_a_starved_job_from_a_lifo_queue():
    settings = resolve(dispatch_order="lifo", aging=True, age_step=10)
    old = waiting("old", 0, queued_at=0)
    fresh = waiting("fresh", 1, queued_at=95)

    # At t=100 the old job has earned ten levels and the fresh one none.
    assert names(order_waiting([old, fresh], settings, 100)) == ["old", "fresh"]


def test_aging_works_without_priority_processing():
    """
    Starvation is a property of LIFO itself, not of using weights, so aging has
    to be usable without switching weights on.
    """
    settings = resolve(dispatch_order="lifo", aging=True, age_step=10)
    old = waiting("old", 0, priority=0, queued_at=0)
    fresh = waiting("fresh", 1, priority=0, queued_at=99)

    assert settings.priority_processing is False
    assert names(order_waiting([old, fresh], settings, 100)) == ["old", "fresh"]


def test_aging_adds_to_a_declared_priority():
    settings = resolve(priority_processing=True, aging=True, age_step=10)
    important = waiting("important", 0, priority=5, queued_at=100)
    patient = waiting("patient", 1, priority=0, queued_at=0)

    # At t=100 the patient job has no wait recorded against it yet.
    assert effective_priority(important, settings, 100) == 5
    assert effective_priority(patient, settings, 100) == 10
    assert names(order_waiting([important, patient], settings, 100)) == ["patient", "important"]


def test_age_max_stops_aging_from_becoming_a_second_scheduler():
    """
    Without a ceiling a starved job eventually outranks genuinely urgent work
    that has only just arrived, trading a starvation problem for an inversion
    one.
    """
    settings = resolve(priority_processing=True, aging=True, age_step=10, age_max=3)
    urgent = waiting("urgent", 1, priority=5, queued_at=100)
    ancient = waiting("ancient", 0, priority=0, queued_at=0)

    assert effective_priority(ancient, settings, 100) == 3
    assert names(order_waiting([ancient, urgent], settings, 100)) == ["urgent", "ancient"]


def test_a_job_that_was_never_queued_is_not_aged():
    settings = resolve(aging=True, age_step=10)
    job = Job("never_submitted")
    assert job.queued_at is None
    assert aging_boost(job, settings, 1000) == 0


def test_ordering_is_stable_for_complete_ties():
    settings = resolve(priority_processing=True)
    jobs = [waiting("a", 0, priority=1), waiting("b", 0, priority=1)]
    assert names(order_waiting(jobs, settings, 0)) == ["a", "b"]
    assert names(order_waiting(jobs, settings, 0)) == ["a", "b"]


# Settings validation


@pytest.mark.parametrize(
    "settings",
    [
        {"dispatch_order": "queue"},
        {"dispatch_order": None},
        {"priority_processing": "yes"},
        {"aging": "yes"},
        {"age_step": 0},
        {"age_step": -1},
        {"age_step": True},
        {"age_max": 0},
        {"age_max": 1.5},
        {"age_max": True},
    ],
)
def test_invalid_dispatch_settings_raise(settings):
    with pytest.raises(ValueError):
        Settings.from_dict(settings).resolve()


def test_priority_must_be_a_number():
    with pytest.raises(ValueError, match="priority must be a number"):
        Job("job", priority="high")


def test_priority_rejects_booleans():
    with pytest.raises(ValueError, match="priority must be a number"):
        Job("job", priority=True)
