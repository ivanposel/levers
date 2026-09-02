import os
import signal
import subprocess
import sys
import time

import pytest

from levers import Lever, Levers, LeverError


@pytest.fixture()
def box(tmp_path):
    return Levers(state_dir=str(tmp_path / "state"))


def test_a_pull_with_no_reason_is_refused(box):
    box.register(Lever("cache", run=lambda: True))
    r = box.pull("cache", why="  ")
    assert r.ok is False
    assert "must say why" in r.why


def test_an_unknown_lever_is_a_refusal_not_a_crash(box):
    r = box.pull("nope", why="because")
    assert r.ok is False and "no lever named" in r.why


def test_a_verified_pull_is_true(box):
    ran = []
    box.register(Lever("cache", run=lambda: ran.append(1), verify=lambda: True,
                       settle=0, poll=0, attempts=1))
    r = box.pull("cache", why="hit rate collapsed", actor="test")
    assert r.ok is True and ran == [1]
    assert "verified" in r.evidence


def test_a_pull_that_did_not_come_back_is_false(box):
    box.register(Lever("cache", run=lambda: True, verify=lambda: False,
                       settle=0, poll=0, attempts=2))
    assert box.pull("cache", why="wedged").ok is False


def test_a_verifier_that_cannot_answer_is_none_not_false(box):
    """The whole three-state point: 'I could not look' is not 'it is broken'."""
    def cannot():
        raise OSError("the probe timed out")
    box.register(Lever("cache", run=lambda: True, verify=cannot, settle=0, poll=0, attempts=1))
    r = box.pull("cache", why="wedged")
    assert r.ok is None
    assert "could not verify" in r.evidence


def test_escalation_runs_once_and_is_re_verified(box):
    state = {"up": False}
    box.register(Lever("cache", run=lambda: None, verify=lambda: state["up"],
                       escalate=lambda: state.update(up=True),
                       settle=0, poll=0, attempts=2))
    r = box.pull("cache", why="wedged")
    assert r.ok is True
    assert "escalation" in r.evidence


def test_a_failing_escalation_is_could_not_tell(box):
    def boom():
        raise RuntimeError("no")
    box.register(Lever("cache", run=lambda: None, verify=lambda: False, escalate=boom,
                       settle=0, poll=0, attempts=1))
    assert box.pull("cache", why="wedged").ok is None


def test_an_action_that_raises_is_could_not_tell(box):
    def boom():
        raise RuntimeError("the command itself blew up")
    box.register(Lever("cache", run=boom))
    r = box.pull("cache", why="wedged")
    assert r.ok is None


def test_cooldowns_are_shared_through_the_state_dir(tmp_path):
    """Two independent registries in two processes must see one cooldown."""
    one = Levers(state_dir=str(tmp_path / "s"))
    two = Levers(state_dir=str(tmp_path / "s"))
    for b in (one, two):
        b.register(Lever("cache", run=lambda: True, cooldown=600))
    assert one.pull("cache", why="first").ok is True
    r = two.pull("cache", why="second, from another process")
    assert r.ok is False and r.skipped == "cooldown"


def test_force_skips_the_cooldown(box):
    box.register(Lever("cache", run=lambda: True, cooldown=600))
    box.pull("cache", why="first")
    assert box.pull("cache", why="second", force=True).ok is True


def test_the_cooldown_starts_even_when_the_action_explodes(box):
    def boom():
        raise RuntimeError("no")
    box.register(Lever("cache", run=boom, cooldown=600))
    box.pull("cache", why="first")
    r = box.pull("cache", why="second")
    assert r.skipped == "cooldown", "a lever that only cools on success hammers a broken thing"


def test_live_work_blocks_the_pull(tmp_path):
    box = Levers(state_dir=str(tmp_path), busy=lambda res: "the nightly export" if res == "db" else None)
    box.register(Lever("database", run=lambda: pytest.fail("acted on a busy resource"),
                       destroys="db"))
    r = box.pull("database", why="looks wedged")
    assert r.ok is False and r.skipped == "live work"
    assert "nightly export" in r.why


def test_force_does_not_skip_live_work(tmp_path):
    box = Levers(state_dir=str(tmp_path), busy=lambda res: "a job")
    box.register(Lever("database", run=lambda: pytest.fail("forced through live work"),
                       destroys="db"))
    assert box.pull("database", why="certain it is stuck", force=True).skipped == "live work"


def test_an_unreadable_busy_check_refuses_rather_than_assuming_idle(tmp_path):
    def cannot(_res):
        raise OSError("the claims file is gone")
    box = Levers(state_dir=str(tmp_path), busy=cannot)
    box.register(Lever("database", run=lambda: pytest.fail("acted blind"), destroys="db"))
    r = box.pull("database", why="looks wedged")
    assert r.ok is None
    assert "refusing to act blind" in r.why


def test_wait_for_idle_gets_the_lever_once_the_work_ends(tmp_path):
    calls = {"n": 0}

    def busy(_res):
        calls["n"] += 1
        return "a job" if calls["n"] < 2 else None

    box = Levers(state_dir=str(tmp_path), busy=busy)
    box.register(Lever("database", run=lambda: True, destroys="db"))
    assert box.pull("database", why="wedged", wait_for_idle=5).ok is True


def test_a_lever_with_no_resource_is_never_blocked(tmp_path):
    box = Levers(state_dir=str(tmp_path), busy=lambda res: pytest.fail("asked about nothing"))
    box.register(Lever("cache", run=lambda: True))
    assert box.pull("cache", why="fine").ok is True


def test_registering_twice_is_a_programming_error(box):
    box.register(Lever("cache", run=lambda: True))
    with pytest.raises(LeverError):
        box.register(Lever("cache", run=lambda: True))


def test_the_journal_and_the_report(box):
    box.register(Lever("cache", run=lambda: True))
    box.pull("cache", why="one", actor="alice")
    box.pull("cache", why="two", actor="bob")
    rows = box.history()
    assert [r["actor"] for r in rows] == ["alice", "bob"]
    assert all(r["asked_why"] for r in rows), "a pull with no recorded reason teaches nothing"
    rep = box.report()
    assert rep["pulls"] == 2
    assert rep["by_lever_and_actor"]["cache <- alice"] == 1


def test_subprocess_levers_run_real_commands(box):
    box.register(Lever("touch", run=[sys.executable, "-c", "print('hi')"]))
    r = box.pull("touch", why="prove argv levers work")
    assert r.ok is True and r.rc == 0
