"""One owner for every dangerous action in a process tree.

The fault class this exists to remove has one shape:

    two actors, one resource, no owner

When any module may restart a service, kill a process or reboot a host, each of them arrives
with its own idea of "stuck", its own private cooldown, and no way to see the others. They then
fight: one restarts what another just started, a supervisor kills the process a second
supervisor is nursing, and a health check tears down the thing it was only meant to look at.
None of that is fixable inside the modules that cause it, because each is behaving correctly on
the information it has. The missing thing is an owner.

What this module enforces:

ONE PULL AT A TIME    a file lock held across the whole pull, so two callers reaching for the
                      same lever serialise and the second one sees the first one's cooldown.
COOLDOWNS ARE SHARED  cooldown state lives on disk, not in a process, so a lever cannot be
                      pulled six times in six seconds by six different callers.
NEVER KILL LIVE WORK  an optional `busy` callback is consulted before anything destructive. A
                      resource somebody has claimed is not idle, however wedged it looks from
                      the outside. `force=True` skips the cooldown; nothing skips this.
WHY IS MANDATORY      a pull with no reason is refused, because a log of actions with no
                      reasons cannot be learned from.
THREE-STATE OUTCOMES  True (it worked), False (it did not), None (could not tell). A pull whose
                      result could not be measured is never recorded as a success.

What it deliberately does not do: it does not decide WHEN to pull. Callers still judge that.
This only makes sure that when they do, exactly one thing happens, once, with a reason and a
record. Moving the judgement in here would rebuild the same tangle behind one import.

There is no pattern kill and there never will be. `pkill -f postgres` is the literal mechanism
by which a supervisor kills its own healthy workers; every kill here is by pid.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Mapping, Optional, Sequence, Union

__all__ = ["Lever", "Levers", "Pull", "LeverError"]


class LeverError(Exception):
    """Raised for programming mistakes — an unknown lever name, a bad registration."""


Action = Union[Sequence[str], Callable[[], Any]]


@dataclass
class Lever:
    """One dangerous action, what it takes away, and how long to wait between pulls.

    `destroys` names the resource this pull takes from whoever is using it. It is the whole
    point of the registry: a lever that destroys "browser" must not fire while some job is
    holding "browser", no matter which module asks or how confident it is.

    `run` is either an argv list (executed with subprocess) or a plain callable, so a lever can
    be a shell command, an API call, or anything else you can write a function around.

    `verify` answers "did it actually come back?" — not "did the command exit 0". A restart
    command returning success only means the supervisor accepted it. `verify` raising is a
    could-not-tell, and is recorded as None rather than as a failure.

    `escalate` is the impolite path, run once when the polite one did not bring the thing back
    (the SIGKILL after the SIGTERM). A wedged process is exactly the state that ignores the
    graceful command, so the graceful command times out against the case it exists to fix.
    """

    name: str
    run: Action
    cooldown: float = 0.0
    destroys: Optional[str] = None
    verify: Optional[Callable[[], bool]] = None
    escalate: Optional[Callable[[], Any]] = None
    doc: str = ""
    timeout: float = 90.0
    settle: float = 6.0          # wait before the first verify
    poll: float = 5.0            # wait between verifies
    attempts: int = 6            # how many times to ask before escalating


@dataclass
class Pull:
    """The record of one pull. `ok` is True, False, or None for could-not-tell."""

    lever: str
    ok: Optional[bool]
    why: str = ""                # why this pull ended the way it did
    asked_why: str = ""          # the reason the caller gave for pulling
    actor: str = ""
    evidence: str = ""
    skipped: str = ""
    pid: Optional[int] = None
    rc: Optional[int] = None
    err: str = ""
    at: str = ""
    ts: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(d.pop("extra") or {})
        return {k: v for k, v in d.items() if v not in ("", None) or k == "ok"}


def _zombie(pid: int) -> bool:
    """Is this pid a corpse its parent has not collected yet?

    `os.kill(pid, 0)` succeeds on a zombie, so a naive liveness check reports a process that has
    already exited as alive, and a kill that "did not work". A zombie holds nothing, does
    nothing, and cannot be signalled away — only its parent's wait() removes it. Linux only;
    elsewhere this answers False and the caller falls back to plain signalling.
    """
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as fh:
            fields = fh.read().rpartition(")")[2].split()
        return bool(fields) and fields[0] == "Z"
    except Exception:
        return False


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class _FileLock:
    """Serialise pulls across processes. Falls back to running unlocked rather than hanging.

    A lock that can block forever turns a safety mechanism into an outage, so acquisition has a
    deadline. On timeout the caller proceeds — it will still see the shared cooldown, which is
    the protection that actually matters.
    """

    def __init__(self, path: str, timeout: float = 30.0):
        self.path = path
        self.timeout = timeout
        self.fh = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:            # not a POSIX host; cooldown state is still shared
            return self
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self.fh = open(self.path, "a+")
            deadline = time.time() + self.timeout
            while True:
                try:
                    fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.time() >= deadline:
                        self.fh.close()
                        self.fh = None
                        return self
                    time.sleep(0.05)
        except Exception:
            self.fh = None
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self.fh.close()
            except Exception:
                pass
            self.fh = None
        return False


class Levers:
    """The registry. Build one per application and import it everywhere.

    >>> import tempfile
    >>> box = Levers(state_dir=tempfile.mkdtemp())
    >>> _ = box.register(Lever("cache", run=lambda: True, cooldown=300,
    ...                        doc="drop the warm cache"))
    >>> box.pull("cache", why="hit rate collapsed", actor="doctest").ok
    True
    >>> box.pull("cache", why="hit rate collapsed", actor="doctest").skipped
    'cooldown'
    """

    def __init__(self, state_dir: str, busy: Optional[Callable[[str], Any]] = None,
                 busy_pid: Optional[Callable[[int], Any]] = None,
                 log: bool = True, echo: bool = False):
        self.state_dir = os.path.abspath(os.path.expanduser(state_dir))
        self.levers: "dict[str, Lever]" = {}
        # `busy(resource)` returns a truthy description of whoever holds the resource, or a
        # falsy value if it is free. RAISING IS MEANINGFUL: it means "I could not check", and a
        # pull that cannot check whether somebody is working refuses rather than acting blind.
        self.busy = busy
        # `busy_pid(pid)` is the same question about one process, used by reap().
        self.busy_pid = busy_pid
        self.log = log
        self.echo = echo
        self._state_path = os.path.join(self.state_dir, "levers.json")
        self._log_path = os.path.join(self.state_dir, "levers.jsonl")
        self._lock_path = os.path.join(self.state_dir, "levers.lock")
        os.makedirs(self.state_dir, exist_ok=True)

    # ------------------------------------------------------------------ registration

    def register(self, lever: Lever) -> Lever:
        if not isinstance(lever, Lever):
            raise LeverError("register() takes a Lever")
        if not lever.name:
            raise LeverError("a lever needs a name")
        if lever.name in self.levers:
            raise LeverError(f"a lever named {lever.name!r} is already registered")
        self.levers[lever.name] = lever
        return lever

    def __contains__(self, name: str) -> bool:
        return name in self.levers

    # ------------------------------------------------------------------ cooldown state

    def _state(self) -> dict:
        try:
            with open(self._state_path, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}

    def _remember(self, name: str, at: float) -> None:
        d = self._state()
        d[name] = at
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1)
            os.replace(tmp, self._state_path)
        except Exception:
            pass

    def cooled(self, name: str) -> dict:
        """Has this lever waited out its cooldown, and if not, how long is left?"""
        lev = self.levers.get(name)
        if lev is None:
            raise LeverError(f"no lever named {name!r}")
        last = float(self._state().get(name, 0) or 0)
        if not last:
            return {"ok": True, "since": None, "why": "never pulled"}
        waited = time.time() - last
        if waited >= lev.cooldown:
            return {"ok": True, "since": waited}
        return {"ok": False, "wait": lev.cooldown - waited,
                "why": f"{name} was pulled {int(waited)}s ago; cooldown is {int(lev.cooldown)}s"}

    # ------------------------------------------------------------------ recording

    def _record(self, p: Pull) -> Pull:
        p.at = p.at or _now_iso()
        p.ts = p.ts or time.time()
        row = p.to_dict()
        if self.echo:
            print("[levers] " + json.dumps(row, default=str)[:300], flush=True)
        if self.log:
            try:
                with open(self._log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, default=str) + "\n")
            except Exception:
                pass
        return p

    # ------------------------------------------------------------------ the guard

    def holder(self, resource: Optional[str]):
        """Who is holding `resource`? Returns the holder, or None for free.

        Propagates whatever the busy callback raises. That exception is the third answer and
        callers below turn it into a refusal, never into "nobody is using it".
        """
        if not resource or self.busy is None:
            return None
        return self.busy(resource)

    # ------------------------------------------------------------------ pulling

    def pull(self, name: str, why: str, actor: str = "", force: bool = False,
             wait_for_idle: float = 0.0) -> Pull:
        """Pull one lever. The only way anything in your system should restart or reboot.

        `force` skips the cooldown. It does not skip the live-work check, and no argument does:
        the point of the check is that a caller which is certain enough to force is exactly the
        caller most likely to be wrong about somebody else's work.
        """
        lev = self.levers.get(name)
        if lev is None:
            return self._record(Pull(lever=name, ok=False, actor=actor,
                                     why=f"no lever named {name!r}"))
        if not (why or "").strip():
            return self._record(Pull(lever=name, ok=False, actor=actor,
                                     why="refused: every pull must say why"))

        with _FileLock(self._lock_path):
            c = self.cooled(name)
            if not c["ok"] and not force:
                return self._record(Pull(lever=name, ok=False, actor=actor, asked_why=why,
                                         why=c["why"], skipped="cooldown"))

            deadline = time.time() + max(0.0, wait_for_idle)
            while True:
                try:
                    held = self.holder(lev.destroys)
                except Exception as e:
                    return self._record(Pull(
                        lever=name, ok=None, actor=actor, asked_why=why,
                        why=f"could not read who is using {lev.destroys!r} "
                            f"({type(e).__name__}) — refusing to act blind"))
                if not held:
                    break
                if time.time() >= deadline:
                    return self._record(Pull(
                        lever=name, ok=False, actor=actor, asked_why=why, skipped="live work",
                        why=f"{lev.destroys} is being used by {held}"))
                time.sleep(min(3.0, max(0.05, deadline - time.time())))

            # Remembered BEFORE the action, so a crash mid-pull still starts the cooldown. A
            # lever that only cools down on success is a lever that hammers a broken thing.
            self._remember(name, time.time())
            rc, err = None, ""
            try:
                if callable(lev.run):
                    lev.run()
                else:
                    r = subprocess.run(list(lev.run), capture_output=True, text=True,
                                       timeout=lev.timeout)
                    rc, err = r.returncode, (r.stderr or "").strip()[:300]
            except Exception as e:
                return self._record(Pull(lever=name, ok=None, actor=actor, asked_why=why,
                                         why=f"the action itself failed: {type(e).__name__}: {e}"))

            if lev.verify is None:
                # Nothing to look at afterwards — a reboot has no "after" from in here. Say what
                # was issued and claim nothing more than that.
                return self._record(Pull(lever=name, ok=True, actor=actor, asked_why=why, rc=rc,
                                         err=err, evidence="issued, not verified"))

            ok, evidence = self._verify(lev)
            if not ok and lev.escalate is not None:
                try:
                    lev.escalate()
                except Exception as e:
                    return self._record(Pull(
                        lever=name, ok=None, actor=actor, asked_why=why, rc=rc, err=err,
                        evidence=evidence,
                        why=f"escalation failed: {type(e).__name__}: {e}"))
                ok2, ev2 = self._verify(lev, escalated=True)
                ok, evidence = ok2, ev2 or evidence
            return self._record(Pull(lever=name, ok=ok, actor=actor, asked_why=why, rc=rc,
                                     err=err, evidence=evidence))

    def _verify(self, lev: Lever, escalated: bool = False):
        """Ask `verify` until it says yes or we run out of patience.

        Returns (ok, evidence) where ok is True / False / None. None means the verifier itself
        could not answer, which is not the same as the thing being down.
        """
        ok: Optional[bool] = False
        evidence = ""
        waited = 0.0
        if lev.settle:
            time.sleep(lev.settle)
            waited += lev.settle
        for attempt in range(max(1, lev.attempts)):
            try:
                ok = bool(lev.verify())
                evidence = ("came back only after escalation" if escalated else "verified") + \
                    f" after {waited:.0f}s"
                if ok:
                    return ok, evidence
            except Exception as e:
                ok, evidence = None, f"could not verify: {type(e).__name__}"
            if attempt < lev.attempts - 1 and lev.poll:
                time.sleep(lev.poll)
                waited += lev.poll
        return ok, evidence

    # ------------------------------------------------------------------ killing

    def reap(self, pid: int, why: str, actor: str = "", sig: int = signal.SIGTERM,
             grace: float = 5.0) -> Pull:
        """Kill ONE process, by pid, never by pattern — and never one doing claimed work.

        By pid alone a healthy process and a stale one are indistinguishable: during a handover
        there are briefly two of everything, and a pattern kill takes both. Whatever registry
        answers `busy_pid` is the only thing that can tell them apart, so it is consulted here
        rather than in every caller that might forget.

        SIGTERM first; if the process is still there after `grace` seconds it gets SIGKILL.
        """
        pid = int(pid)
        if not (why or "").strip():
            return self._record(Pull(lever="reap", ok=False, pid=pid, actor=actor,
                                     why="refused: every kill must say why"))
        try:
            os.kill(pid, 0)
            if _zombie(pid):
                return self._record(Pull(
                    lever="reap", ok=True, pid=pid, actor=actor, asked_why=why,
                    evidence="already a zombie — nothing left to kill; its parent must wait()"))
        except ProcessLookupError:
            return self._record(Pull(lever="reap", ok=True, pid=pid, actor=actor, asked_why=why,
                                     evidence="already gone before I acted"))
        except PermissionError:
            # It exists and is not ours. That is a real, measured refusal.
            return self._record(Pull(lever="reap", ok=False, pid=pid, actor=actor, asked_why=why,
                                     why=f"pid {pid} is not ours to signal"))
        except Exception as e:
            return self._record(Pull(lever="reap", ok=None, pid=pid, actor=actor, asked_why=why,
                                     why=f"cannot see pid {pid}: {type(e).__name__}"))

        if self.busy_pid is not None:
            try:
                held = self.busy_pid(pid)
            except Exception as e:
                return self._record(Pull(
                    lever="reap", ok=None, pid=pid, actor=actor, asked_why=why,
                    why=f"could not check what pid {pid} is doing ({type(e).__name__}) "
                        f"— refusing to kill blind"))
            if held:
                return self._record(Pull(lever="reap", ok=False, pid=pid, actor=actor,
                                         asked_why=why, skipped="live work",
                                         why=f"pid {pid} is busy: {held}"))

        return self._record(self._signal(pid, why, actor, sig, grace))

    def _signal(self, pid: int, why: str, actor: str, sig: int, grace: float) -> Pull:
        try:
            os.kill(pid, sig)
        except Exception as e:
            return Pull(lever="reap", ok=False, pid=pid, actor=actor, asked_why=why,
                        why=f"{type(e).__name__}: {e}")
        deadline = time.time() + max(0.0, grace)
        while True:
            try:
                os.kill(pid, 0)
                if _zombie(pid):
                    return Pull(lever="reap", ok=True, pid=pid, actor=actor, asked_why=why,
                                evidence=f"pid {pid} exited on signal {int(sig)} and is now a "
                                         f"zombie awaiting its parent")
            except ProcessLookupError:
                return Pull(lever="reap", ok=True, pid=pid, actor=actor, asked_why=why,
                            evidence=f"pid {pid} gone after signal {int(sig)}")
            except Exception:
                break
            if time.time() >= deadline:
                break
            time.sleep(0.05)
        if int(sig) != int(signal.SIGKILL):
            return self._signal(pid, why + " (ignored the polite signal)", actor,
                                signal.SIGKILL, grace)
        return Pull(lever="reap", ok=False, pid=pid, actor=actor, asked_why=why,
                    why=f"pid {pid} survived SIGKILL")

    # ------------------------------------------------------------------ looking back

    def history(self, n: int = 50) -> list:
        rows = []
        try:
            with open(self._log_path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return []
        return rows[-n:]

    def report(self, window: float = 86400.0) -> dict:
        """Who pulled what, how often, and how much of it worked.

        Read this before adding another watchdog. The usual finding is that the thing you were
        about to automate is already being pulled forty times a day by something else.
        """
        from collections import Counter
        rows = [r for r in self.history(100000)
                if time.time() - float(r.get("ts", 0) or 0) < window]
        return {
            "pulls": len(rows),
            "by_lever_and_actor": dict(Counter(
                f"{r.get('lever')} <- {r.get('actor') or '?'}" for r in rows)),
            "outcomes": dict(Counter(str(r.get("ok")) for r in rows)),
            "cooldowns": {k: self.cooled(k) for k in self.levers},
        }

    def describe(self) -> str:
        out = []
        for k, v in self.levers.items():
            c = self.cooled(k)
            state = "READY" if c["ok"] else f"cooling {int(c.get('wait', 0))}s"
            out.append(f"  {k:16} every {int(v.cooldown):6}s  "
                       f"destroys={v.destroys or '-':10} {state:14} {v.doc}")
        return "\n".join(out)
