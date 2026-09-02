import os
import signal
import subprocess
import sys
import time

import pytest

from levers import Lever, Levers


def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


@pytest.fixture()
def box(tmp_path):
    return Levers(state_dir=str(tmp_path))


def test_reap_kills_one_process_by_pid(box):
    p = _sleeper()
    try:
        r = box.reap(p.pid, why="it is a test", actor="test")
        assert r.ok is True
        assert p.wait(timeout=5) is not None
    finally:
        if p.poll() is None:
            p.kill()


def test_a_kill_with_no_reason_is_refused(box):
    p = _sleeper()
    try:
        assert box.reap(p.pid, why="").ok is False
        assert p.poll() is None
    finally:
        p.kill()


def test_a_process_that_is_already_gone_is_a_measured_success(box):
    p = _sleeper()
    p.kill()
    p.wait(timeout=5)
    r = box.reap(p.pid, why="tidy up")
    assert r.ok is True and "already gone" in r.evidence


def test_reap_refuses_a_process_doing_claimed_work(tmp_path):
    p = _sleeper()
    box = Levers(state_dir=str(tmp_path),
                 busy_pid=lambda pid: "uploading batch 12" if pid == p.pid else None)
    try:
        r = box.reap(p.pid, why="looks like a duplicate")
        assert r.ok is False and r.skipped == "live work"
        assert p.poll() is None, "killed a process that was doing claimed work"
    finally:
        p.kill()


def test_an_unreadable_claim_check_refuses_to_kill_blind(tmp_path):
    p = _sleeper()

    def cannot(_pid):
        raise OSError("the claims file is gone")

    box = Levers(state_dir=str(tmp_path), busy_pid=cannot)
    try:
        r = box.reap(p.pid, why="looks like a duplicate")
        assert r.ok is None
        assert "refusing to kill blind" in r.why
        assert p.poll() is None
    finally:
        p.kill()


def test_sigterm_is_escalated_to_sigkill(box):
    """A process that ignores the polite signal still dies, and the record says so."""
    code = ("import signal, time\n"
            "signal.signal(signal.SIGTERM, lambda *a: None)\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n")
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    try:
        p.stdout.readline()
        r = box.reap(p.pid, why="it ignores SIGTERM", grace=1.0)
        assert r.ok is True
        assert p.wait(timeout=5) == -signal.SIGKILL
    finally:
        if p.poll() is None:
            p.kill()


def test_a_zombie_is_not_something_to_kill(box):
    """os.kill(pid, 0) succeeds on a corpse; a corpse is still not a running process."""
    p = _sleeper()
    p.terminate()
    time.sleep(0.5)                      # exited, but not yet wait()ed for -> zombie
    r = box.reap(p.pid, why="tidy up")
    assert r.ok is True
    assert "zombie" in r.evidence or "already gone" in r.evidence
    p.wait(timeout=5)
