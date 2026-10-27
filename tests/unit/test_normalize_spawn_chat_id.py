r"""normalize_chat_id must not invent a new contact out of a spawn session id.

2026-09-20 incident: a reminder spawn session is keyed
"<real_chat_id>-spawn-<hex>" (manager.py). normalize_chat_id's phone branch
does re.sub(r"[^\d+]", "", ...), which deletes the letters/dashes of the
suffix and welds its digits onto the phone number:

    +15555550100-spawn-e4683624  ->  +155555501004683624

The daemon then treated that as an unknown contact and auto-created a SECOND
admin-tier session for it. It sat [BUSY] with an empty transcript for ~16h;
because is_busy gates the stuck-session watchdog off, it could never self-heal.
"""

from assistant.common import normalize_chat_id

SPAWN = "+15555550100-spawn-e4683624"
MANGLED = "+155555501004683624"


def test_spawn_suffix_is_preserved_not_mangled():
    assert normalize_chat_id(SPAWN) == SPAWN


def test_spawn_id_never_becomes_the_mangled_phone_number():
    # The exact regression: digits of the suffix welded onto the number.
    assert normalize_chat_id(SPAWN) != MANGLED


def test_spawn_normalization_is_a_no_op():
    # Must be a no-op, not a rename -- otherwise each pass can mint a new key.
    once = normalize_chat_id(SPAWN)
    assert normalize_chat_id(once) == once


def test_spawn_keeps_isolation_from_its_parent_session():
    # The spawn is deliberately a SEPARATE session; don't collapse it.
    assert normalize_chat_id(SPAWN) != normalize_chat_id("+15555550100")


def test_parent_part_of_spawn_id_is_still_normalized():
    assert normalize_chat_id("5555550100-spawn-abc123") == "+15555550100-spawn-abc123"


def test_ordinary_ids_are_unaffected():
    assert normalize_chat_id("+15555550100") == "+15555550100"
    assert normalize_chat_id("5555550100") == "+15555550100"
    assert normalize_chat_id("signal:+15555550100") == "signal:+15555550100"
    uuid = "b3d258b9a4de447ca412eb335c82a077"
    assert normalize_chat_id(uuid) == uuid
