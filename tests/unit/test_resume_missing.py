"""Stale resume ids (transcript deleted) must not crash-loop session creation (2026-10-12 cherry-eric outage)."""
from pathlib import Path

from assistant.sdk_backend import resume_conversation_exists


def test_resume_exists_when_transcript_present(tmp_path: Path):
    proj = tmp_path / "-Users-x-transcripts-imessage-abc"
    proj.mkdir()
    (proj / "aaaa-bbbb.jsonl").write_text("{}\n")
    assert resume_conversation_exists("aaaa-bbbb", projects_root=tmp_path)


def test_resume_missing_when_transcript_gone(tmp_path: Path):
    (tmp_path / "-Users-x-transcripts-imessage-abc").mkdir()
    assert not resume_conversation_exists("aaf9b52d-2e26-4f74-ba87-2cb49bd8dac6", projects_root=tmp_path)


def test_resume_found_in_any_project_dir(tmp_path: Path):
    other = tmp_path / "-Users-x-transcripts-imessage-abc-spawn-1"
    other.mkdir()
    (other / "cccc.jsonl").write_text("{}\n")
    assert resume_conversation_exists("cccc", projects_root=tmp_path)
