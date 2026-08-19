"""
A two minute live view of a registry tree under load, redrawn in place.

    python examples/demo_live.py

Best watched in a terminal at least 25 rows tall. Piping it somewhere prints a
frame every few seconds instead of redrawing, so the run still reads as a log.

The scenario is a video editor. Three kinds of work share one machine and must
not crowd each other out: thumbnails for whatever the user is scrolling past,
renders the user is waiting on, and exports running in the background. Each gets
its own lane, its own slot count and its own idea of what to run next.

Work arrives on a background thread throughout, the way it would from a request
handler, so nothing here is staged: the tables are the registry's own, and what
you see is what it decided.
"""

import os
import random
import sys
import threading
import time

# So the demo runs from a clone without carpenter being installed first.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from carpenter import Blueprint, Job, Registry  # noqa: E402

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")

RUNTIME = 120.0
FRAME = 0.5
LIVE = sys.stdout.isatty()

random.seed(7)


def work(seconds, fail=False):
    command = [sys.executable, "-u", WORKER, f"{seconds:.1f}"]
    if fail:
        command.append("--fail")
    return Blueprint(command)


# The tree ---------------------------------------------------------------------
#
# Four slots on the machine, split three ways. The split is the point: a burst of
# thumbnails cannot reach the render lane's slots, however long its queue gets.

app = Registry(
    {
        "max_jobs": 4,
        "keep_jobs": True,
        "poll_interval": 0.25,
        "output_mode": "discard",
    },
    default_blueprint=work(2),
    name="app",
)

# Newest first, because the thumbnail worth generating is the one on screen now.
# Aging is what stops that from stranding the oldest request forever.
thumbnails = app.mount(Registry({
    "max_jobs": 1,
    "dispatch_order": "lifo",
    "aging": True,
    "age_step": 15,
    "age_max": 5,
}, name="thumbnails"))

# Ordered by weight, because some renders are more urgent than others.
renders = app.mount(Registry({
    "max_jobs": 2,
    "priority_processing": True,
}, name="renders"))

# Plain queue. Background work that should finish in the order it was asked for.
exports = app.mount(Registry({"max_jobs": 1}, name="exports"))


# The workload -----------------------------------------------------------------

PHASES = [
    (0, "Steady state", [
        "Three lanes, four slots between them. Work trickles in and each lane",
        "keeps to its own allowance.",
        "WATCH: the running count per lane never exceeds that lane's max_jobs.",
    ]),
    (20, "A burst of thumbnails", [
        "Twelve thumbnail requests arrive at once, into a lane with one slot.",
        "The queue is served newest first, so the ones from the burst are",
        "immediately behind every fresh arrival.",
        "WATCH: the render and export lanes do not slow down at all. Their slots",
        "were never available to the thumbnails.",
    ]),
    (45, "A render the user is waiting on", [
        "A render arrives with priority 100 while the lane is full and has a",
        "queue of ordinary work.",
        "WATCH: it goes to the front of the render queue, but nothing already",
        "running is interrupted. A weight decides what starts next, not what",
        "finishes first.",
    ]),
    (70, "Aging rescues a stranded request", [
        "New thumbnails keep arriving, and under plain LIFO each one would go in",
        "front of the burst, which would then never run at all.",
        "WATCH: the oldest queued thumbnail below. Its effective priority climbs",
        "one level every 15 seconds, and once it outranks the fresh arrivals it",
        "starts. LIFO still orders everything that has not waited that long.",
    ]),
    (95, "When the work itself goes wrong", [
        "One export is given a command that does not exist, another exits",
        "non-zero. Both land in the export lane.",
        "WATCH: the failed line below, and that nothing else is disturbed. The",
        "registry spawns on its own thread, so a bad command fails its own job",
        "rather than taking the monitor down with every other job on it.",
    ]),
    (112, "Coming down", [
        "One shutdown for the whole tree. Lanes are brought down before the",
        "registry governing them.",
        "WATCH: everything still queued ends as cancelled, not run.",
    ]),
]


def feed(stop):
    """
    Drip work into the tree the way a request handler would, on its own thread
    and through the same locking any other caller goes through.
    """
    started = time.monotonic()

    def elapsed():
        return time.monotonic() - started

    def wait_until(mark):
        while not stop.is_set() and elapsed() < mark:
            time.sleep(0.1)
        return not stop.is_set()

    counter = {"thumb": 0, "render": 0, "export": 0}

    def submit(lane, kind, seconds, priority=0, blueprint=None, name=None):
        counter[kind] += 1
        name = name or f"{kind}_{counter[kind]:02d}"
        # Each kind of work takes a different amount of time, which is what makes
        # the lanes behave differently under the same arrival pressure.
        if blueprint is None:
            blueprint = work(seconds)
        try:
            lane.submit_job(Job(name, blueprint=blueprint, priority=priority))
        except RuntimeError:
            pass  # The tree came down while we were still feeding it.

    next_thumb = 0.0
    next_render = 0.0
    next_export = 0.0
    burst_done = False
    rush_done = False
    breakage_done = False

    while not stop.is_set() and elapsed() < RUNTIME:
        now = elapsed()

        # Each lane is fed at roughly the rate it can serve, so the queues stay
        # populated for the whole run without growing without bound. A lane that
        # never has a queue has nothing to demonstrate about ordering.
        if now >= next_thumb:
            submit(thumbnails, "thumb", random.uniform(1.5, 2.5))
            next_thumb = now + random.uniform(1.6, 2.4)

        if now >= next_render:
            submit(renders, "render", random.uniform(6.0, 10.0), priority=random.choice([0, 0, 10]))
            next_render = now + random.uniform(2.5, 4.0)

        if now >= next_export:
            submit(exports, "export", random.uniform(4.0, 6.0))
            next_export = now + random.uniform(3.5, 5.0)

        if not burst_done and now >= 20:
            for _ in range(12):
                submit(thumbnails, "thumb", random.uniform(1.5, 2.5))
            burst_done = True

        if not rush_done and now >= 45:
            # Named so it can be picked out of the queue below.
            submit(renders, "render", 6.0, priority=100, name="render_rush")
            rush_done = True

        if not breakage_done and now >= 95:
            submit(exports, "export", 1.0, name="export_typo",
                   blueprint=Blueprint(["pyhton", "nope.py"]))
            submit(exports, "export", 1.5, name="export_broken",
                   blueprint=work(1.5, fail=True))
            breakage_done = True

        time.sleep(0.2)

    # Nothing new after this point, so the last phase shows the backlog being
    # cancelled rather than topped up.
    wait_until(RUNTIME)


# The view ---------------------------------------------------------------------

def phase_at(elapsed):
    number, current = 1, PHASES[0]
    for index, phase in enumerate(PHASES):
        if elapsed >= phase[0]:
            number, current = index + 1, phase
    return number, current


def oldest_queued(lane):
    queue = lane.queued_jobs()
    if not queue:
        return None
    return max(queue, key=lambda job: job.waited() or 0.0)


def lane_row(lane):
    """
    One line per lane: what is running, and what is queued in the order the lane
    will actually start it.

    queued_jobs() is used rather than the registry's own table because the table
    lists jobs as they were submitted, while this demo is about the order they
    come out in, which is a different question.
    """
    running = lane.running_jobs()
    queue = lane.queued_jobs()

    run_text = " ".join(f"{job.name}({job.duration():.0f}s)" for job in running) or "idle"

    shown = []
    for job in queue[:3]:
        mark = " "
        if lane.priority_processing and job.priority > 0:
            mark = "!"
        elif lane.effective_priority(job) > job.priority:
            mark = "*"
        shown.append(f"{job.name}{mark}")
    more = f" +{len(queue) - 3}" if len(queue) > 3 else ""

    return (f"  {lane.name:<11} {lane.max_jobs}sl  "
            f"{run_text:<32} {' '.join(shown)}{more}")


def draw(elapsed):
    if LIVE:
        print("\033[H\033[J", end="")

    number, (_, title, lines) = phase_at(elapsed)
    bar = "=" * 88
    print(bar)
    print(f" carpenter, live.   t+{elapsed:5.1f}s of {RUNTIME:.0f}s      "
          f"Phase {number} of {len(PHASES)}: {title}")
    print(bar)
    for line in lines:
        print(f"  {line}")

    print()
    print(f"  {'lane':<11} {'cap':<5} {'running':<32} next up (in dispatch order)")
    print("  " + "-" * 84)
    for lane in app:
        print(lane_row(lane))
    print("  " + "-" * 84)
    jobs = app.all_jobs()
    done = sum(1 for job in jobs if job.is_finished())
    print(f"  {len(jobs)} jobs across {len(app)} lanes | "
          f"{len(app.running_jobs())}/{app.max_jobs} running | "
          f"{len(app.queued_jobs())} queued | {done} finished | "
          f"free slots {app.available_slots()}")
    print("  ! carries a priority weight    * has been promoted by waiting")

    failed = [job for job in jobs if job.status == "failed"]
    if failed:
        detail = ", ".join(
            f"{job.name} ({type(job.error).__name__ if job.error else 'exit ' + str(job.exit_code)})"
            for job in failed[-3:]
        )
        print(f"  failed: {detail}")

    stale = oldest_queued(thumbnails)
    if stale is not None:
        print(f"\n  oldest queued thumbnail: {stale.name} "
              f"waiting {stale.waited():.0f}s, "
              f"effective priority {thumbnails.effective_priority(stale)} "
              f"(base {stale.priority}, one level per {thumbnails.age_step:g}s)")
    else:
        print("\n  thumbnail queue is empty")


def main():
    stop = threading.Event()
    feeder = threading.Thread(target=feed, args=(stop,), daemon=True)
    feeder.start()

    started = time.monotonic()
    last_drawn = -99.0
    interval = FRAME if LIVE else 5.0

    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= RUNTIME:
                break
            if elapsed - last_drawn >= interval:
                draw(elapsed)
                last_drawn = elapsed
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n  interrupted, bringing the tree down")

    stop.set()

    order = []
    for lane in list(app) + [app]:
        lane.on_shutdown = lambda reg, reason: order.append(reg.path())

    queued_before = len(app.queued_jobs())
    app.shutdown(grace=2)
    draw(time.monotonic() - started)

    print()
    print("=" * 88)
    print("  The registry's own view of the tree, one table per lane.")
    print("=" * 88)
    app.print_registry(max_print_jobs=4)

    jobs = app.all_jobs()
    cancelled = [job for job in jobs if job.status == "cancelled"]
    finished = [job for job in jobs if job.status == "finished"]
    failed = [job for job in jobs if job.status == "failed"]

    print()
    print("=" * 88)
    print(f"  Shut down in one call. Lanes first: {' then '.join(order)}")
    print(f"  {queued_before} job(s) were still queued, and were cancelled rather than run.")
    print(f"  finished {len(finished)} | failed {len(failed)} | cancelled {len(cancelled)} "
          f"| total {len(jobs)}")
    if failed:
        print()
        for job in failed:
            reason = type(job.error).__name__ if job.error else f"exit code {job.exit_code}"
            print(f"  {job.name:<16} failed: {reason}")
        print("  A command that does not exist fails its own job and leaves the rest alone.")
    print("=" * 88)


if __name__ == "__main__":
    main()
