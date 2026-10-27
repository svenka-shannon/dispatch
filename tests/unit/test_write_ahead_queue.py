

class TestSendCommandAliased:
    """2026-10-01: `R=.../reply; $R "msg"` sends were invisible to last_send_at → false
    'you have not sent ANY reply' nudges."""

    def test_aliased_reply_counts(self):
        from assistant.sdk_session import _is_send_command
        assert _is_send_command('R=~/.claude/skills/sms-assistant/scripts/reply; $R "hi"')
        assert _is_send_command('R=~/.claude/skills/sms-assistant/scripts/reply; $R "a" && $R --burst "b"')
        assert _is_send_command('S=~/.claude/skills/sms-assistant/scripts/send-sms; "${S}" +1555 "x"')

    def test_unrelated_alias_does_not_count(self):
        from assistant.sdk_session import _is_send_command
        assert not _is_send_command('X=foo; $X bar')
