"""Tests for decide_tapback — the guard that stops Cmd+T tapbacks landing on
the wrong message (Cmd+T always reacts to the newest INCOMING message) or
toggling an existing reaction off."""
from assistant.manager import TAPBACK_ASSOCIATED_TYPE, decide_tapback

OLD_IN = "AAAAAAAA-0000-0000-0000-000000000001"
NEW_IN = "BBBBBBBB-0000-0000-0000-000000000002"
OUT_1 = "CCCCCCCC-0000-0000-0000-000000000003"
REACT_IN = "DDDDDDDD-0000-0000-0000-000000000004"

# (guid, is_from_me, associated_message_type), newest first
ROWS = [
    (OUT_1, 1, 0),        # our own reply — newest overall, must be ignored
    (REACT_IN, 0, 2000),  # inbound tapback row — not a message, ignored
    (NEW_IN, 0, 0),       # newest inbound real message
    (OLD_IN, 0, 0),
]


def test_newest_inbound_target_ok_despite_newer_outbound():
    assert decide_tapback(f"p:0/{NEW_IN}", "thumbsup", ROWS) == (True, "ok")


def test_older_inbound_target_skipped():
    ok, reason = decide_tapback(f"p:0/{OLD_IN}", "thumbsup", ROWS)
    assert not ok
    assert reason == "tapback skipped: target not newest inbound"


def test_outbound_target_skipped():
    ok, reason = decide_tapback(f"p:0/{OUT_1}", "heart", ROWS)
    assert not ok and "not newest inbound" in reason


def test_unknown_target_skipped():
    ok, reason = decide_tapback("p:0/EEEEEEEE-0000", "heart", ROWS)
    assert not ok and "not newest inbound" in reason


def test_no_inbound_rows_skipped():
    ok, reason = decide_tapback(f"p:0/{OUT_1}", "heart", [(OUT_1, 1, 0)])
    assert not ok and "no inbound" in reason


def test_guid_prefix_forms_normalized():
    for form in (NEW_IN, f"p:0/{NEW_IN}", f"p:1/{NEW_IN}", f"bp:{NEW_IN}"):
        assert decide_tapback(form, "haha", ROWS)[0], form


def test_same_reaction_in_memory_skipped():
    ok, reason = decide_tapback(f"p:0/{NEW_IN}", "thumbsup", ROWS, already_sent=True)
    assert not ok and "already performed" in reason


def test_same_reaction_in_chat_db_skipped_to_avoid_toggle_off():
    prior = [TAPBACK_ASSOCIATED_TYPE["thumbsup"]]
    ok, reason = decide_tapback(f"p:0/{NEW_IN}", "thumbsup", ROWS, prior_own_reaction_types=prior)
    assert not ok and "already performed" in reason


def test_different_reaction_on_target_allowed():
    prior = [TAPBACK_ASSOCIATED_TYPE["heart"]]
    assert decide_tapback(f"p:0/{NEW_IN}", "thumbsup", ROWS, prior_own_reaction_types=prior)[0]


def test_previously_removed_reaction_can_be_resent():
    # newest own row is a removal (3001) of an earlier like (2001)
    prior = [3001, 2001]
    assert decide_tapback(f"p:0/{NEW_IN}", "thumbsup", ROWS, prior_own_reaction_types=prior)[0]
