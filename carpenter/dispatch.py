"""
Choosing which queued job starts next.

Everything here is a pure function of the jobs, the settings, and the current
time. It holds no state and touches no process, which is deliberate: the
decision about what should run is the part with all the interesting behaviour,
and keeping it separate means it can be tested exhaustively without spawning
anything or starting a thread.

The registry supplies `now` from `time.monotonic()` rather than reading a clock
in here, both so tests can hand it an arbitrary instant and because a duration
measured against the wall clock can go backwards when the system clock is
adjusted.
"""


def aging_boost(job, settings, now):
    """
    How many priority levels a job has gained by waiting.

    The boost is quantised into whole steps rather than climbing smoothly, and
    that is the point rather than an approximation. A continuous boost would
    make the longest waiting job outrank every newer one at every instant, since
    its wait is always strictly greater; with equal base priorities that orders
    the queue by age alone and silently turns `lifo` into `fifo` the moment
    aging is switched on. Promoting in whole steps leaves everything within the
    same step to be ordered by `dispatch_order` as usual, so LIFO keeps behaving
    like LIFO right up until a job has genuinely been waiting too long.
    """
    if not settings.aging or job.queued_at is None:
        return 0
    waited = now - job.queued_at
    if waited < settings.age_step:
        return 0
    levels = int(waited // settings.age_step)
    if settings.age_max is not None:
        levels = min(levels, settings.age_max)
    return levels


def effective_priority(job, settings, now):
    """
    The priority the registry actually orders by: the job's own weight, if
    weights are being consulted at all, plus whatever it has gained by waiting.

    Aging applies whether or not `priority_processing` is on. With weights off
    it is what stops a `lifo` queue from stranding its oldest job forever, which
    is a problem a plain LIFO has regardless of whether anyone is using
    priorities.
    """
    base = job.priority if settings.priority_processing else 0
    return base + aging_boost(job, settings, now)


def order_key(job, settings, now):
    """
    The sort key for one waiting job. Lower sorts first, meaning it starts
    sooner.

    Two components. The first is the negated effective priority, so a higher
    weight sorts earlier; it is a flat zero when neither weights nor aging are
    in use, which leaves the second component deciding everything. The second is
    the job's submission sequence, negated for `lifo`.

    The sequence number is used rather than a timestamp because two jobs
    submitted in the same instant would otherwise tie, and because the wall
    clock can move backwards underneath a comparison.
    """
    if settings.priority_processing or settings.aging:
        rank = -effective_priority(job, settings, now)
    else:
        rank = 0

    position = job.sequence if job.sequence is not None else 0
    if settings.dispatch_order == "lifo":
        return (rank, -position)
    return (rank, position)


def order_waiting(waiting, settings, now):
    """
    The waiting jobs in the order the registry will start them.

    Returns a new list and does not modify the one it is given. Python's sort is
    stable, so jobs that tie on every component keep their existing relative
    order rather than shuffling between calls.
    """
    return sorted(waiting, key=lambda job: order_key(job, settings, now))
