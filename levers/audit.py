"""A structural test: fail the build when anything else pulls a lever directly.

Routing today's callers through one owner fixes today. Without a gate, the next module that
wants a restart writes `subprocess.run(["systemctl", "restart", ...])`, because that is the
obvious thing to write — and in a month there are thirty of them again, each with its own idea
of "stuck". The registry is only worth having if the codebase cannot route around it.

So this is a test, not a note in a document. Point it at your source, list the modules that are
allowed to do the dangerous thing, and assert that the finding list is empty.

    from levers.audit import audit, format_findings

    def test_only_the_lever_registry_kills_things():
        found = audit(["src"], allow={"src/myapp/levers_setup.py": "it IS the registry"})
        assert not found, format_findings(found)

Two design choices keep it from becoming noise, which matters more than coverage — a checker
that cries wolf gets disabled, and a disabled checker leaves a codebase unguarded while it still
appears to have a guard:

  * comments and docstrings are not code. Prose explaining a lever is not a pull.
  * the "innocent" pattern forgives lines that only READ state (`systemctl is-active`,
    `docker ps`) and lines that go through your registry.

The allowlist maps a path to the REASON it is exempt. A bare list of filenames is how a guard
becomes decoration: nobody can tell later whether an entry was thought about or added to make
the build green. Passing anything but a mapping is an error.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

__all__ = ["Rule", "Finding", "DEFAULT_RULES", "DEFAULT_INNOCENT", "audit", "code_lines",
           "format_findings"]


@dataclass(frozen=True)
class Rule:
    """One forbidden shape. `pattern` is matched against a single line of real code."""

    pattern: "re.Pattern"
    means: str
    always: bool = False     # `always` rules are not forgiven by the innocent pattern


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    means: str
    source: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}  {self.means}: {self.source}"


def _r(p: str) -> "re.Pattern":
    return re.compile(p)


DEFAULT_RULES: "tuple[Rule, ...]" = (
    Rule(_r(r"""systemctl['"\s,\]]+.*?['"](restart|stop|reboot|poweroff|kill)['"]"""),
         "restarts or stops a service unit"),
    Rule(_r(r"""\bservice\s+\w+\s+(restart|stop)\b"""), "restarts or stops a service"),
    Rule(_r(r"\bos\.kill\s*\(\s*(?!.*,\s*0\s*\))"), "signals a process by pid"),
    Rule(_r(r"\bos\.killpg\s*\("), "signals a whole process group"),
    Rule(_r(r"\bpkill\b|\bkillall\b"), "kills by pattern — a pattern kill takes the healthy "
                                       "process along with the stale one", always=True),
    Rule(_r(r"""subprocess\.\w+\(\s*\[?['"](reboot|shutdown|halt)['"]"""),
         "reboots the machine"),
    Rule(_r(r"psutil\.Process\s*\([^)]*\)\s*\.\s*(kill|terminate|send_signal)\s*\("),
         "kills a process through psutil, around the registry", always=True),
    Rule(_r(r"(?<![\w.])p\s*\.\s*(kill|terminate|send_signal)\s*\("),
         "kills a process_iter process directly", always=True),
    Rule(_r(r"""docker['"\s,\]]+.*?['"](kill|stop|restart|rm)['"]"""),
         "stops or removes a container"),
    Rule(_r(r"""kubectl['"\s,\]]+.*?['"]delete['"]"""), "deletes a Kubernetes object"),
)

# Lines that only READ state, or that go through the registry. Everything here is a line the
# checker used to flag while the code was behaving; each false alarm costs more than the rule
# it belongs to is worth.
DEFAULT_INNOCENT = _r(
    r"is-active|is-enabled|is-failed|list-units|list-unit-files|--value|'show'|\"show\""
    r"|docker\s+ps|\bps\s+aux|levers\.|import\s+levers|from\s+levers\b"
)


def code_lines(text: str):
    """Yield (lineno, line) for real code only, skipping comments and docstrings.

    Written by hand rather than with `ast` on purpose: this has to run over files that do not
    parse (a different Python version, a partial merge) and report them rather than skip them
    silently. A checker that quietly ignores what it cannot read is worse than no checker.
    """
    out, in_doc = [], None
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if in_doc is not None:
            if in_doc in s:
                in_doc = None
            continue
        opened = None
        # A whole docstring on one line closes itself: `"""one line."""` has an even count
        # and would otherwise be read as code, which is how a module that documents the
        # dangerous thing gets reported for doing it.
        for q in ('"""', "'''"):
            if s.startswith(q) and s.count(q) >= 2 and s.count(q) % 2 == 0:
                opened = "__done__"
                break
            if q in s and s.count(q) % 2 == 1:
                opened = q
                break
        if opened == "__done__":
            continue
        if opened is not None:
            in_doc = opened
            if s.startswith(opened):
                continue          # the whole line is docstring
        if s.startswith("#"):
            continue
        out.append((i, line))
    return out


def _python_files(paths: Iterable[str], exclude: Sequence[str]) -> list:
    out = []
    for p in paths:
        if os.path.isfile(p):
            out.append(p)
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs
                       if d not in {".git", "__pycache__", ".venv", "venv", "build", "dist",
                                    ".tox", ".mypy_cache", ".pytest_cache"}]
            for f in sorted(files):
                if f.endswith(".py"):
                    out.append(os.path.join(root, f))
    if exclude:
        pats = [re.compile(x) for x in exclude]
        out = [p for p in out if not any(x.search(p) for x in pats)]
    return sorted(set(out))


def audit(paths: Iterable[str], allow: Optional[Mapping[str, str]] = None,
          rules: Sequence[Rule] = DEFAULT_RULES,
          innocent: "Optional[re.Pattern]" = DEFAULT_INNOCENT,
          exclude: Sequence[str] = ()) -> "list[Finding]":
    """Scan `paths` and return every direct lever pull outside the allowed files.

    `allow` maps a path SUFFIX to the reason it is exempt, so "src/app/levers_setup.py" and
    "levers_setup.py" both work and nobody has to write absolute paths into a test.
    """
    if allow is None:
        allow = {}
    if not isinstance(allow, Mapping):
        raise TypeError(
            "allow must be a mapping of path -> reason. A bare list of filenames is how a "
            "guard becomes decoration: write down WHY each file is exempt.")
    for k, v in allow.items():
        if not str(v).strip():
            raise ValueError(f"the allowlist entry for {k!r} has no reason")

    def allowed(path: str) -> bool:
        norm = os.path.normpath(path)
        return any(norm == os.path.normpath(k) or norm.endswith(os.sep + os.path.normpath(k))
                   or os.path.basename(norm) == k for k in allow)

    found = []
    for path in _python_files(paths, exclude):
        if allowed(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except Exception as e:
            # Could not read it. That is not a pass — say so and let the build decide.
            found.append(Finding(path, 0, f"could not read this file ({type(e).__name__})", ""))
            continue
        for i, line in code_lines(text):
            hit = None
            for rule in rules:
                if rule.always and rule.pattern.search(line):
                    hit = rule
                    break
            if hit is None:
                if innocent is not None and innocent.search(line):
                    continue
                for rule in rules:
                    if not rule.always and rule.pattern.search(line):
                        hit = rule
                        break
            if hit is not None:
                found.append(Finding(path, i, hit.means, line.strip()[:120]))
    return found


def format_findings(found: "Sequence[Finding]") -> str:
    if not found:
        return "ok — every lever is pulled through the registry"
    lines = [f"{len(found)} direct lever pulls outside the registry:", ""]
    lines += [f"  {f}" for f in found]
    lines += ["", "Route these through your Levers registry, or add the file to the allowlist "
                  "with the reason it needs no guard."]
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="levers-audit",
                                 description="fail when anything pulls a lever directly")
    ap.add_argument("paths", nargs="*", default=["."])
    ap.add_argument("--allow", action="append", default=[], metavar="PATH=REASON",
                    help="exempt a file, with the reason it is exempt")
    ap.add_argument("--exclude", action="append", default=[], metavar="REGEX")
    a = ap.parse_args(argv)
    allow = {}
    for item in a.allow:
        k, _, v = item.partition("=")
        allow[k.strip()] = v.strip() or "no reason given on the command line"
    found = audit(a.paths or ["."], allow=allow, exclude=a.exclude)
    print(format_findings(found))
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
