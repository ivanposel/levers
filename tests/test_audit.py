import re
import textwrap

import pytest

from levers.audit import DEFAULT_RULES, Rule, audit, format_findings


def write(tmp_path, name, body):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_a_direct_restart_is_a_finding(tmp_path):
    write(tmp_path, "worker.py", """
        import subprocess
        def fix():
            subprocess.run(["systemctl", "restart", "postgresql"])
    """)
    found = audit([str(tmp_path)])
    assert len(found) == 1
    assert "restarts or stops a service unit" in found[0].means
    assert "worker.py" in format_findings(found)


def test_reading_state_is_not_a_pull(tmp_path):
    write(tmp_path, "probe.py", """
        import subprocess
        def up():
            return subprocess.run(["systemctl", "is-active", "postgresql"]).returncode == 0
    """)
    assert audit([str(tmp_path)]) == []


def test_going_through_the_registry_is_not_a_pull(tmp_path):
    write(tmp_path, "worker.py", """
        import levers
        def fix(box):
            box.pull("database", why="wedged", actor="worker")
    """)
    assert audit([str(tmp_path)]) == []


def test_prose_about_a_lever_is_not_a_pull(tmp_path):
    write(tmp_path, "worker.py", '''
        """This module never calls pkill or os.kill(pid, 9); it asks the registry.

        Not even systemctl restart, which is the point.
        """
        VALUE = 1
    ''')
    assert audit([str(tmp_path)]) == [], "a checker that reads documentation as a violation " \
                                        "cries wolf, and a noisy checker gets disabled"


def test_a_comment_is_not_a_pull(tmp_path):
    write(tmp_path, "worker.py", """
        # do not call subprocess.run(["reboot"]) here
        VALUE = 1
    """)
    assert audit([str(tmp_path)]) == []


def test_os_kill_with_signal_zero_is_a_liveness_probe(tmp_path):
    write(tmp_path, "probe.py", """
        import os
        def alive(pid):
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
    """)
    assert audit([str(tmp_path)]) == []


def test_a_pattern_kill_is_never_forgiven(tmp_path):
    """`always` rules ignore the innocent pattern — nothing makes a pattern kill safe."""
    write(tmp_path, "worker.py", """
        import subprocess, levers
        def fix():
            subprocess.run(["pkill", "-f", "worker"])
    """)
    found = audit([str(tmp_path)])
    assert len(found) == 1 and "pattern kill" in found[0].means


def test_psutil_kills_are_caught(tmp_path):
    write(tmp_path, "hygiene.py", """
        import psutil
        def cull(pid):
            psutil.Process(pid).terminate()
    """)
    found = audit([str(tmp_path)])
    assert found and "psutil" in found[0].means


def test_the_allowlist_exempts_a_file(tmp_path):
    write(tmp_path, "pkg/setup_levers.py", """
        import subprocess
        subprocess.run(["systemctl", "restart", "postgresql"])
    """)
    assert audit([str(tmp_path)]) != []
    assert audit([str(tmp_path)], allow={"setup_levers.py": "it IS the registry"}) == []
    assert audit([str(tmp_path)], allow={"pkg/setup_levers.py": "it IS the registry"}) == []


def test_an_allowlist_without_reasons_is_rejected(tmp_path):
    with pytest.raises(TypeError):
        audit([str(tmp_path)], allow=["setup_levers.py"])
    with pytest.raises(ValueError):
        audit([str(tmp_path)], allow={"setup_levers.py": ""})


def test_an_unreadable_file_is_reported_not_skipped(tmp_path):
    p = write(tmp_path, "worker.py", "VALUE = 1\n")
    p.chmod(0o000)
    try:
        found = audit([str(tmp_path)])
        if found:                       # running as root can read it anyway
            assert "could not read" in found[0].means
    finally:
        p.chmod(0o644)


def test_custom_rules(tmp_path):
    write(tmp_path, "worker.py", """
        def drop():
            db.execute("DROP TABLE customers")
    """)
    rule = Rule(re.compile(r"DROP\s+TABLE", re.I), "drops a table")
    found = audit([str(tmp_path)], rules=(rule,))
    assert len(found) == 1


def test_this_package_passes_its_own_audit():
    """The registry is allowed to signal; nothing else in the package is."""
    import os
    import levers
    root = os.path.dirname(os.path.abspath(levers.__file__))
    found = audit([root], allow={
        "_core.py": "it IS the registry — the one place that signals a process",
        "audit.py": "it is the checker: a list that NAMES the dangerous verbs is the "
                    "opposite of a violation",
    })
    assert found == [], format_findings(found)


def test_a_one_line_docstring_is_still_prose(tmp_path):
    write(tmp_path, "worker.py", '''
        """Never call pkill here; ask the registry."""
        VALUE = 1
    ''')
    assert audit([str(tmp_path)]) == []
