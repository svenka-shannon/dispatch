"""skill-lint: keep diary-style edits and personal data out of SKILL.md files.

The repo-wide check only covers SKILL.md lines changed vs git HEAD, minus the
hashed backlog in skills/skill-builder/lint-baseline.txt, so pre-existing hits
don't fail today but anything newly written must be clean.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
SKILL_LINT = REPO_ROOT / "skills" / "skill-builder" / "scripts" / "skill-lint"


def _run(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    # stdlib-only script: run with the test interpreter so uv isn't required
    return subprocess.run(
        [sys.executable, str(SKILL_LINT), *args],
        capture_output=True, text=True, cwd=cwd, timeout=120,
    )


def _git_ok() -> bool:
    if not shutil.which("git"):
        return False
    r = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "HEAD"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def test_skill_lint_exists():
    assert SKILL_LINT.is_file(), "skill-lint script missing"


@pytest.mark.skipif(not _git_ok(), reason="git unavailable or repo has no HEAD")
def test_changed_skill_md_files_are_clean():
    r = _run("--changed", "--strict")
    assert r.returncode == 0, (
        "New personal data / dated diary headings in changed SKILL.md lines.\n"
        "Fold lessons into the relevant section as general rules and move personal "
        "data to memory or POSTMORTEMS.md (see skill-builder 'Recording a Lesson').\n\n"
        + r.stdout + r.stderr
    )


def test_rules_flag_known_bad_lines(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: demo\ndescription: demo\n---\n"
        "## Lessons (2026-10-18)\n"  # 5 dated-heading
        "Printer lives at 192.168.1.42.\n"  # 6 private-ip
        "Text +1 (415) 867-5309 when done.\n"  # 7 phone
        "Login is somebody.real@gmail.com.\n"  # 8 email
        "Got it, order #1179324 shipped.\n"  # 9 order-number
        "Saved to /Users/alex/code/thing.db\n"  # 10 abs-user-path
    )
    r = _run(str(skill))
    found = {(int(line.split(":")[1]), line.split(":")[2]) for line in r.stdout.splitlines()}
    assert {
        (5, "dated-heading"), (6, "private-ip"), (7, "phone"),
        (8, "email"), (9, "order-number"), (10, "abs-user-path"),
    } <= found, r.stdout + r.stderr


def test_rules_allow_placeholders(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: demo\ndescription: demo\n---\n"
        "## API Costs (as of 2026-02)\n"
        "Hue bridge at 192.168.x.x, LAN range 10.0.0.0/8.\n"
        'send-sms "+15555550100" "hi" and "+1234567890"\n'
        "Contact you@example.com or alice@gmail.com.\n"
        "Paths live under ~/dispatch or /Users/username/.\n"
        "```bash\n# 2026-10-18 comment inside a code fence is fine\n```\n"
        "A reviewed exception 10.0.0.1 <!-- skill-lint: ignore -->\n"
    )
    r = _run(str(skill), "--strict")
    assert r.returncode == 0, r.stdout + r.stderr
