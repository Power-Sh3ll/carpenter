"""
What the registry does when there is more work than there are slots.

Run it from anywhere:

    python examples/demo_scheduling.py

Six scenes, each one printing what the registry decided and why. The whole
thing takes about fifteen seconds. Nothing is mocked and no clocks are faked;
the aging scene really does wait.
"""

import os
import sys
import threading
import time

# So the demo runs from a clone without carpenter being installed first.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from carpenter import Blueprint, Job, Registry  # noqa: E402

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")


def work(seconds, fail=False):
    """A blueprint for a job that takes this long."""
    command = [sys.executable, "-u", WORKER, str(seconds)]
    if fail:
        command.append("--fail")
    return Blueprint(command)


def scene(number, title):
    print()
    print("=" * 78)
    print(f" {number}. {title}")
    print("=" * 78)


def show(label, jobs):
    print(f"  {label:<24} {', '.join(job.name for job in jobs) or '(nothing)'}")


def queue_under(settings, submissions):
    """
    Build a registry whose single slot is already occupied, submit the given
    jobs into it, and report the order it intends to start them in.

    Blocking the slot first is what makes this instant and repeatable: nothing
    can start, so every submission is reliably queued and the ordering is read
    rather than raced for.
    """
    settings = dict(settings, max_jobs=1, keep_jobs=True)
    reg = Registry(settings, work(30))
    reg.submit_job(Job("blocker"))
    for name, priority in submissions:
        reg.submit_job(Job(name, priority=priority))
    order = reg.queued_jobs()
    reg.shutdown(grace=0)
    return order


# 1. Admission control ---------------------------------------------------------

scene(1, "More work than slots")
print("""
  Ten jobs are handed over at once to a registry that runs three. submit_job()
  accepts every one of them immediately. What it does not do is start them.
""")

reg = Registry({"max_jobs": 3, "keep_jobs": True, "poll_interval": 0.1}, work(0.6))
jobs = [reg.submit_job(Job(f"job_{i}")) for i in range(10)]

print(f"  submitted {len(jobs)}, running {len(reg.running_jobs())}, "
      f"queued {len(reg.queued_jobs())}, free slots {reg.available_slots()}")
print()
reg.print_registry(max_print_jobs=10)

print("\n  Waiting for the whole backlog, not just the three that are running.\n")
reg.wait_for_jobs(timeout=60)
reg.print_registry(max_print_jobs=10)
reg.shutdown(grace=0)


# 2. A queue or a stack --------------------------------------------------------

scene(2, "A queue, or a stack")
print("""
  dispatch_order decides which waiting job goes next. FIFO suits submitted work
  that should complete in the order it was asked for. LIFO suits work whose
  newest request is its most relevant one, such as a thumbnail for whatever the
  user is looking at right now.
""")

arrivals = [("first", 0), ("second", 0), ("third", 0), ("fourth", 0)]
show("fifo (default):", queue_under({}, arrivals))
show("lifo:", queue_under({"dispatch_order": "lifo"}, arrivals))


# 3. Priority ------------------------------------------------------------------

scene(3, "Weighing jobs against each other")
print("""
  priority_processing decides whether job.priority is consulted at all. It is
  ignored completely when off, rather than quietly half working. The two
  settings are independent, so dispatch_order still breaks ties between equal
  weights.
""")

mixed = [
    ("nightly_report", -10),
    ("thumbnail_a", 0),
    ("thumbnail_b", 0),
    ("user_is_waiting", 100),
]

show("priority off:", queue_under({}, mixed))
show("priority on:", queue_under({"priority_processing": True}, mixed))
show("priority on, lifo ties:", queue_under({"priority_processing": True, "dispatch_order": "lifo"}, mixed))

print("""
  Note where the two thumbnails land. They have equal weight, so dispatch_order
  is what separates them, which is why these are two settings and not one.
""")


# 4. Aging ---------------------------------------------------------------------

scene(4, "Nothing waits forever")
print("""
  A LIFO queue under steady load never reaches its oldest job: every new arrival
  goes in front of it. Aging fixes that by giving a job priority the longer it
  has waited.

  Both registries below get the same treatment. One job is submitted, three
  seconds pass, then three newer jobs arrive. age_step is 2 seconds, so the old
  job has earned exactly one level by the time the others turn up.
""")


def aging_scene(settings):
    settings = dict(settings, max_jobs=1, keep_jobs=True, poll_interval=0.1)
    reg = Registry(settings, work(30))
    reg.submit_job(Job("blocker"))
    reg.submit_job(Job("old_request"))
    time.sleep(3)
    for i in range(3):
        reg.submit_job(Job(f"new_request_{i}"))
    order = reg.queued_jobs()
    boost = reg.effective_priority(reg.get_job(name="old_request"))
    reg.shutdown(grace=0)
    return order, boost


plain, _ = aging_scene({"dispatch_order": "lifo"})
aged, boost = aging_scene({"dispatch_order": "lifo", "aging": True, "age_step": 2, "age_max": 5})

show("lifo alone:", plain)
show("lifo with aging:", aged)
print(f"\n  old_request's effective priority after waiting: {boost}")
print("""
  The old request has been rescued, and the three newer ones are still in LIFO
  order behind it. That is deliberate: the aging boost is granted in whole
  steps, not as a smoothly rising number. A boost that climbed continuously
  would put the longest waiting job in front at every instant, which orders the
  queue by age alone and silently turns lifo into fifo the moment aging is on.
""")


# 5. A job that cannot start ---------------------------------------------------

scene(5, "When the work itself goes wrong")
print("""
  The registry spawns on a background thread, so a command that does not exist
  cannot raise in the caller's face. It fails its own job and nothing else, and
  the registry carries on.
""")

reg = Registry({"keep_jobs": True, "poll_interval": 0.1}, work(0.3))
missing = reg.submit_job(Job("typo", blueprint=Blueprint(["pyhton", "worker.py"])))
broken = reg.submit_job(Job("exits_nonzero", blueprint=work(0.3, fail=True)))
fine = reg.submit_job(Job("healthy"))
reg.wait_for_jobs(timeout=60)

print(f"  typo           status={missing.status:<9} exit_code={missing.exit_code}  "
      f"error={type(missing.error).__name__}")
print(f"  exits_nonzero  status={broken.status:<9} exit_code={broken.exit_code}     "
      f"error={broken.error}")
print(f"  healthy        status={fine.status:<9} exit_code={fine.exit_code}     "
      f"output={fine.output()[0].strip().splitlines()[-1]!r}")
print("""
  exit_code stays None for the job that never ran, which is what separates
  "could not start" from "started and failed".
""")

reg.shutdown(grace=0)


# 6. Lanes ---------------------------------------------------------------------

scene(6, "Giving different work its own capacity")
print("""
  A video editor generates thumbnails and renders video. Both are jobs, but a
  burst of thumbnail requests must never take the slots a render is waiting for.
  Mounting one registry inside another gives each its own limit and its own
  ordering, under a shared ceiling.

  Thumbnails run newest first, since the one the user is looking at now matters
  more than one they scrolled past. Renders run by weight. Neither lane can
  reach the other's slots.
""")

app = Registry({"max_jobs": 4, "keep_jobs": True, "poll_interval": 0.1}, work(30), name="app")
thumbnails = app.mount(Registry({"max_jobs": 1, "dispatch_order": "lifo"}, name="thumbnails"))
renders = app.mount(Registry({"max_jobs": 3, "priority_processing": True}, name="renders"))

for i in range(4):
    thumbnails.submit_job(Job(f"thumb_{i}"))

# These three arrive to free slots and start on the spot whatever their weight,
# since a weight decides what starts next rather than what finishes first.
for i in range(3):
    renders.submit_job(Job(f"render_in_progress_{i}"))
# The lane is full now, so these three queue, and their weights decide the order.
for job_name, weight in [("render_draft", 0), ("render_final", 50), ("render_preview", 10)]:
    renders.submit_job(Job(job_name, priority=weight))

print(f"  the tree              {app!r}")
print(f"  lanes                 {', '.join(lane.name for lane in app)}")
print(f"  thumbnails inherited  keep_jobs={thumbnails.keep_jobs}, "
      f"poll_interval={thumbnails.poll_interval}, and the default blueprint")
print(f"  but kept its own      dispatch_order={thumbnails.dispatch_order!r}, "
      f"max_jobs={thumbnails.max_jobs}")
print()
show("thumbnails queue:", thumbnails.queued_jobs())
show("renders queue:", renders.queued_jobs())
print(f"""
  Four thumbnails were submitted and one runs. The other three slots belong to
  renders and were never available to them, which is the difference between
  partitioning capacity and sharing it. Free slots across the tree: {app.available_slots()}.
""")
app.print_registry(max_print_jobs=6)

monitors = [thread for thread in threading.enumerate() if thread.name == "carpenter-monitor"]
print(f"\n  Monitor threads for the whole tree: {len(monitors)}. Three separate registries")
print("  would have meant three, along with three idle clocks and three shutdown")
print("  policies, which is most of what nesting exists to avoid.")

brought_down = []
for lane in (thumbnails, renders, app):
    lane.on_shutdown = lambda reg, reason: brought_down.append(reg.path())
app.shutdown(grace=0)
print(f"\n  One shutdown, lanes first: {' then '.join(brought_down)}")

print()
print("=" * 78)
print(" Done. print_registry(clear=True) redraws in place if you want a live view.")
print("=" * 78)
