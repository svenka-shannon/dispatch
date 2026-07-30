---
name: phone-calls
description: Make and conduct real voice phone calls (FaceTime Audio) — the agent speaks with TTS and hears via whisper transcription. Use when asked to call someone, phone someone, make a voice call, or talk to someone by phone. Trigger words - call, phone call, ring, dial, facetime, voice call.
---

# Phone Calls (FaceTime Audio + TTS + Whisper)

Place real voice calls from the assistant's own Apple ID (`!`identity self.email``).
You speak by playing TTS into a virtual microphone and hear by recording
FaceTime's output and transcribing it with whisper.cpp — then YOU (the agent)
drive the conversation turn by turn.

**CLI:** `~/.claude/skills/phone-calls/scripts/call`

## How it works

```
call say "..."   TTS (kokoro) ──► BlackHole 2ch ──► FaceTime microphone ──► far end
call listen      far end ──► FaceTime output ──► BlackHole 16ch ──► VAD record ──► whisper ──► text
```

`call start` sets system default input/output to the BlackHole devices (saving
your real defaults), opens `facetime-audio://<target>`, and confirms FaceTime's
"place call?" dialog (axctl; falls back to a cliclick Return-key press).
`call end` hangs up (quits FaceTime) and restores audio devices.

Who can be called: any Apple user (iPhone/iPad/Mac) by phone number or Apple ID
email — calls ride FaceTime Audio, no carrier needed. Plain landlines/Android
are NOT reachable (would need iPhone Continuity or a VoIP provider).

## Conducting a call (the loop YOU drive)

```bash
CALL=~/.claude/skills/phone-calls/scripts/call

$CALL doctor                                   # first time: verify setup
$CALL start "+15551234567"                     # or an Apple ID email
$CALL listen --wait-for-speech --max 45        # wait for them to answer + speak
$CALL say "Hi! This is Svenka, Eric's AI assistant, calling about ..."
$CALL listen --wait-for-speech                 # hear their reply (prints transcript)
# ...think, then say/listen repeatedly...
$CALL say "Thanks, goodbye!"
$CALL end                                      # ALWAYS end — restores audio devices
```

Conversation rules:
- **Always identify yourself as an AI assistant** at the start of the call.
- Keep each `say` short (1–3 sentences) — long monologues feel robotic and
  the callee will start talking over you.
- Empty/garbled transcript? Just `listen` again once, then ask them to repeat.
- No speech for 45s after start → likely voicemail or no answer: leave a short
  message with `say`, then `end`. (Ringback tones can trigger a brief false
  "speech" — an empty transcript from it is normal; keep listening.)
- **ALWAYS run `call end`** when done (even on errors) — it restores the Mac's
  audio devices. If a call crashed mid-way: `call devices --restore`.
- Only call people when asked to, or admin/partner tier for things they'd
  clearly want a call about. Mind the hour in the callee's timezone.

## Commands

| Command | Purpose |
|---|---|
| `call doctor` | Check all prerequisites (BlackHole, whisper, TTS, AX) |
| `call start <tel-or-email> [--video]` | Place call |
| `call say "text" [--voice af_nova] [--engine qwen --style "..."]` | Speak |
| `call listen [--wait-for-speech] [--max N] [--json] [--raw f.wav]` | Hear + transcribe |
| `call status` | In-call state + audio routing |
| `call end` | Hang up + restore audio |
| `call devices [--setup/--restore]` | Inspect/repair audio routing |
| `call test-audio` | End-to-end loopback self-test, no call placed |

`listen --json` returns `{"transcript", "speech_detected", "seconds"}` —
use it when you need to distinguish silence-timeout from garbled speech.

## Voices

Default voice is `af_nova` (kokoro, fast ~0.25x RT). Any kokoro voice works
(`~/.claude/skills/tts/scripts/speak --voices`). For an expressive custom
voice use `--engine qwen --style "a warm, upbeat assistant"` (slower, ~7GB RAM
— pre-generate long lines before the call if using qwen).

## Requirements & permissions

- **BlackHole 2ch + 16ch** (virtual audio drivers): `brew install blackhole-2ch
  blackhole-16ch` — the pkg installer needs an admin password (interactive
  sudo), then `sudo killall coreaudiod`. No reboot actually needed.
- switchaudio-osx, ffmpeg, whisper-cpp (+ `~/.local/share/whisper/ggml-base.en.bin`),
  kokoro TTS (/tts skill), cliclick.
- FaceTime signed in as the assistant's Apple ID.
- **Accessibility**: axctl UI-driving works from daemon-spawned sessions (the
  uv python binary holds the grant). From a plain Terminal, axctl is blind
  (Terminal lacks the AX grant) — `call start` then falls back to cliclick's
  Return-key press to confirm the call dialog, which works because the
  cliclick binary has its own grant.
- **Microphone TCC**: first `call listen` / `test-audio` from a new context
  triggers a macOS mic-permission prompt (recording BlackHole counts as mic
  access). Approve once; the auth-dialog-monitor may catch it.

## Troubleshooting

- `call doctor` first. Then `call test-audio` (no call placed) — verifies
  TTS → virtual device → capture → whisper end to end.
- Call never confirms: check `axctl tree FaceTime` for the dialog's actual
  button title and click it; from Terminal, grant Terminal Accessibility.
- Callee hears nothing: FaceTime picked the wrong mic. Verify `call status`
  shows default input = BlackHole 2ch, restart the call. (FaceTime binds
  devices at call start.)
- You hear nothing (`listen` always times out): default output must be
  BlackHole 16ch *before* the call starts; also confirm mic TCC was approved.
- Mac audio stuck weird after a crash: `call devices --restore`.
- state lives in `/tmp/phone-call/state.json`; last capture:
  `/tmp/phone-call/listen-last.wav`.
