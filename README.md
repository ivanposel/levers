# levers

**One module owns every kill, restart and reboot in your system — and a structural test fails
the build when anything else reaches for one.**

Zero dependencies. Pure standard library. Python 3.9+.

## The problem

Long-running systems grow supervisors the way a house grows extension cords. A health check
restarts the service it was only meant to look at. A janitor kills the process another janitor
is nursing back to life. A deploy script reboots a host in the middle of a job. Each of these
modules is behaving correctly on the information it has, and that is exactly why the bug is
unfixable inside any one of them.

The shape is always the same:

> **two actors, one resource, no owner**

An audit of one real system before this library existed found **35 modules that could reboot the
machine, 39 that could kill a process, and 11 that could restart the same service** — each with
its own idea of "stuck", its own private cooldown, and no way to see the others. The visible
symptoms were a browser that restarted 23 times in seven hours because two janitors fought over
the same tabs, and a supervisor that killed live jobs during handover because two processes for
a moment look exactly like one duplicate.

## What this does

```python
from levers import Levers, Lever

box = Levers(state_dir="/var/lib/myapp/levers")

box.register(Lever(
    name="database",
    run=["systemctl", "restart", "postgresql"],
    cooldown=900,                 # shared on disk, not per-process
    destroys="db",                # the resource this takes away from whoever holds it
    verify=db_answers,            # did it actually come back? not "did the command exit 0"
    escalate=hard_restart,        # the impolite path, run once, then verified again
    doc="restart the database everything attaches to",
))

r = box.pull("database", why="every connection wedged for 20 minutes", actor="health-loop")

if r.ok is True:      ...   # it came back, and that was measured
if r.ok is False:     ...   # it did not
if r.ok is None:      ...   # could not tell. Never recorded as a success.
```

Five rules, enforced in one place:

| | |
|---|---|
| **One pull at a time** | A file lock is held across the whole pull, so two callers serialise and the second sees the first one's cooldown. |
| **Cooldowns are shared** | Cooldown state lives on disk, so a lever cannot be pulled six times in six seconds by six different callers. |
| **Never kill live work** | An optional `busy` callback is consulted before anything destructive. `force=True` skips the cooldown; **nothing** skips this. |
| **Why is mandatory** | A pull with no reason is refused. A log of actions with no reasons cannot be learned from. |
| **Three-state outcomes** | `True` / `False` / `None`. A pull whose result could not be measured is not a success. |

The cooldown is written **before** the action, so a crash mid-pull still cools the lever. A lever
that only cools down on success is a lever that hammers a broken thing.

### The refusal that matters most

```python
box = Levers(state_dir=..., busy=claims.holder)   # any registry you already have

def holder(resource):
    ...                     # returns who holds it, or None
    raise OSError(...)      # or raises, if it cannot tell
```

If `busy` **raises**, the pull is refused with `ok=None` and the reason `refusing to act blind`.
"I could not check whether somebody is working" must never be treated as "nobody is". That one
line is the difference between a safe restart and a nightly job losing its work.

Pair it with any claims/lease registry you already have — a database row, a Redis key, a file of
leases. `levers` deliberately does not ship one.

### Killing processes

```python
box.reap(pid, why="worker stopped answering its heartbeat", actor="supervisor")
```

By pid, never by pattern. `pkill -f myworker` is the literal mechanism by which supervisors kill
their own healthy workers: during a handover there are briefly two of everything, and a pattern
kill takes both. SIGTERM first, SIGKILL after the grace period, and every step is recorded.

### What it deliberately does not do

It does not decide **when** to pull. Your code still judges that. This only guarantees that when
it does, exactly one thing happens, once, with a reason and a record. Moving the judgement in
here would rebuild the same tangle behind one import.

## The structural test

Routing today's callers through one owner fixes today. Without a gate, the next module that wants
a restart writes `subprocess.run(["systemctl", "restart", ...])` — because that is the obvious
thing to write — and in a month there are thirty again.

```python
from levers.audit import audit, format_findings

def test_only_the_registry_pulls_levers():
    found = audit(["src"], allow={
        "src/myapp/recovery.py": "it IS the registry — the one place that signals a process",
    })
    assert not found, format_findings(found)
```

or from CI:

```console
$ levers-audit src/
3 direct lever pulls outside the registry:

  src/myapp/health.py:88   restarts or stops a service unit: subprocess.run(["systemctl", "restart", "api"])
  src/myapp/hygiene.py:41  kills a process through psutil, around the registry: psutil.Process(pid).terminate()
  src/myapp/deploy.py:12   kills by pattern — a pattern kill takes the healthy process along with the stale one: subprocess.run(["pkill", "-f", "api"])

Route these through your Levers registry, or add the file to the allowlist with the reason it needs no guard.
```

It ships rules for `systemctl restart/stop`, `service`, `os.kill` / `os.killpg`, `pkill` /
`killall`, `reboot` / `shutdown`, `psutil` terminate/kill, `docker kill/stop/rm` and
`kubectl delete`. Add your own with `Rule(re.compile(...), "means")`.

Two decisions keep it from becoming noise, which matters more than coverage — **a checker that
cries wolf gets disabled, and a disabled checker leaves a codebase unguarded while it still looks
guarded**:

* **Comments and docstrings are not code.** Prose that explains a lever is not a pull. A module
  that documents "never call `pkill` here" passes.
* **Reads are not pulls.** `systemctl is-active`, `docker ps`, `os.kill(pid, 0)` are how you ask
  a question, not how you break something.
* **The allowlist is a mapping of path → reason, and a bare list is a `TypeError`.** An allowlist
  of naked filenames is how a guard becomes decoration: nobody can tell later whether an entry
  was thought about or added to make the build green.

Rules marked `always=True` (pattern kills, psutil kills) are never forgiven by the "innocent"
filter, because nothing about the surrounding line makes them safe.

## Install

```console
pip install levers          # once published
pip install .               # from a checkout
```

## Limitations, honestly

* **POSIX.** The lock uses `fcntl`; on a platform without it the lock degrades to "no lock" and
  only the on-disk cooldown protects you. Process signalling assumes POSIX semantics.
* **The state directory is the boundary.** Two registries sharing a `state_dir` share cooldowns.
  Across machines you would need shared storage, and a file lock over NFS is not one.
* **The audit is line-based**, not an AST pass. It is deliberately shallow so it can report files
  that do not even parse instead of skipping them silently. It will not catch a kill assembled
  out of variables (`subprocess.run([cmd, verb, unit])`), and it is not a security boundary — it
  is a build-time reminder aimed at the honest developer who wrote the obvious thing.
* **Zombies.** `os.kill(pid, 0)` succeeds on a process that has already exited but has not been
  `wait()`ed for by its parent. `reap()` detects that case on Linux (via `/proc`) and reports
  "nothing left to kill"; elsewhere it will report the kill as failed. Collect your children.
* **`verify` is your job.** A lever with no verifier reports `ok=True` with the evidence
  "issued, not verified", which is all it can honestly say.
* **No scheduler, no policy engine.** Deciding *when* to pull stays in your code.

## Sibling project

[`claimsledger`](https://github.com/OWNER/claimsledger) — the other half: cooperative claims on
shared resources. `Levers(busy=ledger.holder, busy_pid=ledger.busy_pid)` gives you a restart that
refuses to interrupt live work, and refuses outright when it cannot tell.

## Licence

MIT.
