---
name: phone-calls
description: Make and conduct real voice phone calls — any phone number (restaurants, businesses) via the tethered iPhone's cellular line, or FaceTime Audio to Apple users. The agent speaks with TTS and hears via whisper transcription. Use when asked to call someone, phone a business, make a reservation by phone, or talk to someone by voice. Trigger words - call, phone call, ring, dial, facetime, voice call, call the restaurant.
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

`call start` routes system default input/output to the BlackHole devices
(saving your real defaults), then dials numbers by AX-driving the **Phone
app keypad** (quit-and-relaunch first — Phone binds its audio devices at
launch, so dialing with a stale Phone.app records silence; one `axctl
clickseq` presses Keypad + digits + Call). Apple ID emails fall back to
`facetime-audio://` + notification-banner click. `call end` hangs up (quits
Phone/FaceTime) and restores audio devices.

**Run calls from a daemon-spawned session** (normal chat/task sessions). The
launchd context holds the Accessibility AND Microphone grants; a plain
Terminal has mic but no AX, so dialing fails there (`--dialer url` +
granting Terminal Accessibility is the workaround).

Who can be called:
- **Any phone number** (restaurants, businesses, landlines, Android): phone
  numbers dial as `tel://`, which FaceTime relays over the **tethered
  iPhone's cellular line** (Continuity "Calls from iPhone"). Requires the
  iPhone signed into the same Apple ID, on the same Wi-Fi, with
  Settings → Phone → Calls on Other Devices → this Mac enabled. The call
  goes out from the iPhone's number.
- **Apple IDs** (emails) ride FaceTime Audio directly — no iPhone needed.
  `--facetime` forces FaceTime Audio for a phone number too.

Calling a business? Expect an IVR/hold music: use `call listen --max 60`
(fixed window) for menus, and `say` digits won't work — DTMF is not supported
yet; pick lines that a human answers, or note the limitation to the user.

## Conducting a call — use `converse` (fast, default)

`converse` is a real-time loop in ONE process: persistent audio streams,
preloaded TTS, whisper, and a no-tools model turn via the Agent SDK
(CLI-per-turn was way too slow for a live call). YOU set the goal; the loop
holds the conversation and hangs up when the goal is met.

```bash
CALL=~/.claude/skills/phone-calls/scripts/call

$CALL doctor                       # first time: verify setup
$CALL start "+15551234567"         # dials via Phone app keypad (AX)
$CALL converse \
  --goal "Book a table for 2 at 7:30pm tonight under <owner last name>, callback <owner phone>; if 7:30 unavailable take anything 7-8pm" \
  --greeting "Hi! I'm calling for <owner name>, I'd like to book a table." \
  --transcript /tmp/phone-call/reservation.txt
$CALL end                          # ALWAYS end — restores audio devices
cat /tmp/phone-call/reservation.txt   # then report the outcome to the user
```

`converse` prints a final machine-readable line (also appended to the
transcript): `RESULT: reached` (a real two-way exchange with a person) or
`RESULT: not-reached` (no answer, voicemail, IVR/menu, wrong number). Report
not-reached calls as "couldn't reach them", never as done.

Manual per-turn primitives (`say` / `listen`) still exist for special cases
(playing a pre-made recording), but do NOT drive a live conversation with
them — each invocation pays process+model startup and the callee will hang up
on the dead air.

`converse` handles the persona itself (see CONVERSE_SYSTEM in the script):
it introduces itself as "{owner}'s assistant", sounds natural, does **not**
announce it's an AI on routine errands, but answers honestly and never
denies being an AI if anyone asks. Set `--goal` with the concrete objective
and any fallback (e.g. "if 7:30 is unavailable, take anything 7–8pm").

Writing a good `--goal` / `--greeting`:
- **Put every detail the callee may ask for in `--goal`** (name, callback
  number, email if it's OK to share). The prompt only lets the model share
  details listed in the goal; anything else becomes "I'll have them follow up".
- The prompt keeps the model in the **caller's** role: it confirms what the
  callee will do in second person ("so you'll call back at that number?") and
  never speaks as the business ("we'll reach out").
- Emails and US phone numbers are rewritten into speakable form before TTS
  ("jane dot doe at example dot com", digit groups), and the TTS sentence
  splitter never cuts inside an email. Raw addresses get mangled otherwise.
- The `--greeting` is pre-rendered while the phone rings. If the first
  utterance is short (< 2s, a live "Hello?"), it plays **instantly** while
  whisper transcribes in parallel. Longer openings are transcribed first and
  checked for voicemail/IVR cues, so a recording never gets the opener.

Not reached (no answer, voicemail, IVR/menu, wrong number):
- Default `--voicemail hangup`: hang up silently. **No voicemail on errands
  unless the user asked for one** — an errand greeting is usually a question
  and makes no sense as a message.
- `--voicemail leave` (personal calls): waits out the outgoing greeting and
  beep, then speaks `--greeting` as the message. Write the greeting so it
  works as a message.
- Detection: the first utterance is checked for voicemail/IVR cues (beep,
  "leave a message", "not available", "press N", "mailbox", "voicemail")
  before greeting; mid-call, the model outputs `[UNREACHED]` on a recording,
  a menu it can't navigate, or a wrong number. A `[HANGUP]` only logs
  "goal met" after a real two-way exchange; otherwise "ended".

Conversation rules (also for the manual `say`/`listen` primitives):
- Keep each `say` short (1–3 sentences) — long monologues feel robotic and
  the callee will start talking over you.
- Empty/garbled transcript? Just `listen` again once, then ask them to repeat.
- No speech for 45s after start, or a voicemail/menu → not reached: `end`
  without a message (unless the user asked for a voicemail or it's a personal
  call). (Ringback tones can trigger a brief false "speech" — an empty
  transcript from it is normal; keep listening.)
- **ALWAYS run `call end`** when done (even on errors) — it restores the Mac's
  audio devices. If a call crashed mid-way: `call devices --restore`.
- Only call people when asked to, or admin/partner tier for things they'd
  clearly want a call about. Mind the hour in the callee's timezone.

## Rehearsing a call

Rehearse over the **same transport as the real call**: if the real call is
to a phone number, rehearse to a phone number via the cellular path (plain
`call start "+1..."`), never `--facetime` — FaceTime Audio has different
audio, latency and answer behavior, so it doesn't validate the real call.
Only rehearse over FaceTime when the real call will be FaceTime. Use the same
`converse` flags (`--model`, `--effort`, `--voicemail`, goal, greeting) as the
real call.

## Commands

| Command | Purpose |
|---|---|
| `call doctor` | Check all prerequisites (BlackHole, whisper, TTS, AX) |
| `call start <tel-or-email> [--facetime] [--video]` | Place call (numbers → cellular via iPhone; emails → FaceTime) |
| `call converse --goal "..." [--greeting "..."] [--voicemail hangup\|leave] [--model M] [--effort low]` | **Real-time conversation loop (the main path)** |
| `call say "text" [--voice af_heart] [--engine qwen --style "..."]` | Speak one line (pre-made audio, special cases) |
| `call listen [--wait-for-speech] [--max N] [--json] [--raw f.wav]` | Hear + transcribe |
| `call status` | In-call state + audio routing |
| `call end` | Hang up + restore audio |
| `call devices [--setup/--restore]` | Inspect/repair audio routing |
| `call test-audio` | End-to-end loopback self-test, no call placed |

`listen --json` returns `{"transcript", "speech_detected", "seconds"}` —
use it when you need to distinguish silence-timeout from garbled speech.

## Voices

`converse` defaults to **Kyutai Pocket TTS** (`--tts-engine pocket`, voice
`alba`) — natural conversational prosody, RTF ~0.11, ~30ms first chunk. Other
Pocket voices: `marius`, `jean`, `eve`, `javert`, … (`--voice <name>`).
Auditioned at `/tmp/phone-call/tts-samples/phone8k-pocket-tts-*.wav`.
Pocket downloads its model to the HF cache on first run.

Fallback engine `--tts-engine kokoro` (voice `af_heart`, kokoro's most natural
preset, grade A vs `af_nova`'s C); converse auto-falls-back to kokoro if Pocket
fails to load. The one-shot `say` command still uses kokoro.

Considered but slower on this M4: Chatterbox-Turbo (best naturalness, RTF 0.29
via mlx-audio 4-bit — usable if you want to trade ~1s/turn for voice quality or
zero-shot cloning); Piper (50× real-time but less natural than kokoro). Full
comparison with measured RTF: `/tmp/phone-call/tts-research.md`.

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

## Latency and brain model

A turn costs roughly 1.2-2.5s from the callee's last word to our voice:
0.6s end-of-speech silence + whisper + model first token + TTS first chunk.
The next levers are a smaller whisper model or streaming STT.

Brain options (`--model`, thinking always off; `--effort low|medium|high`).
Approximate time to first text token via the Agent SDK:

| Model | First token | Notes |
|---|---|---|
| `claude-haiku-5-5` (default, `--effort low`) | 1.0-2.4s | smarter than Haiku 4.5; default |
| `claude-haiku-4-5-20251001` | ~0.8s | use when raw speed matters more |
| `claude-sonnet-5-5` | 1.2-3.1s | better conversation, more lag |
| `claude-opus-5-5` | 1.7-3.5s | slowest |

Add ~1s for end-of-speech detection plus whisper on top of these.
