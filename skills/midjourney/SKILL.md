---
name: midjourney
description: Generate images with Midjourney via scrapling API requests. Trigger words - midjourney, imagine, generate image, ai art.
---

> **Not for engineering/hardware renders.** For a 3D view of a real part or device, use the `cad` skill (real geometry).


# Midjourney Image Generation

Generate images using Midjourney's API via scrapling (no browser automation for generation).

## Account

- **Email:** $(security find-generic-password -s "assistant" -a "email" -w)
- **Plan:** Basic ($10/mo)
- **User ID:** stored in keychain as `midjourney` / `user_id`
- **Login:** Google OAuth (same as other services)

## CLI Usage

```bash
# Generate an image (submits, polls, downloads all 4 variations)
~/.claude/skills/midjourney/scripts/midjourney imagine "a tiny cute robot waving hello, watercolor style"

# With parameters (include in prompt string)
~/.claude/skills/midjourney/scripts/midjourney imagine "epic dragon --ar 16:9 --v 7 --stylize 200"

# Use a seed image (auto-uploads to s.mj.run, supports local paths or URLs)
~/.claude/skills/midjourney/scripts/midjourney imagine "transform into steampunk cyborg --v 7" --image ~/photo.png

# Multiple seed images (blending)
~/.claude/skills/midjourney/scripts/midjourney imagine "combined aesthetic" --image img1.png --image img2.png

# Style reference (match look/feel of reference image)
~/.claude/skills/midjourney/scripts/midjourney imagine "a cozy cafe" --sref ~/style_ref.jpg

# Omni reference (preserve face/character likeness)
~/.claude/skills/midjourney/scripts/midjourney imagine "person as a steampunk cyborg" --image ~/photo.png --oref ~/photo.png

# Omni reference with weight (0-1000, higher = more faithful to face)
~/.claude/skills/midjourney/scripts/midjourney imagine "person in fantasy world" --image ~/photo.png --oref ~/photo.png --ow 200

# Generate and download to specific directory
~/.claude/skills/midjourney/scripts/midjourney imagine "sunset over mountains" --output ~/Pictures/

# Upload an image and get its s.mj.run URL (for manual use)
~/.claude/skills/midjourney/scripts/midjourney upload ~/photo.png

# List recent generations
~/.claude/skills/midjourney/scripts/midjourney recent

# Download a specific image by job ID (index 0-3)
~/.claude/skills/midjourney/scripts/midjourney download <job_id> --index 0
```

## How It Works

1. Extracts auth cookies from Chrome via `chrome cookies midjourney.com` (including HttpOnly)
2. Uses **scrapling** `Fetcher()` with TLS fingerprinting for all HTTP requests
3. Submits job via `POST /api/submit-jobs` with cookies + `x-csrf-protection` header
4. Polls `GET /api/imagine?user_id=...&page_size=20` until job appears (completed jobs only appear here)
5. Downloads all 4 full-res JPEGs from CDN with auth cookies
6. Falls back to Chrome download if CDN rejects direct requests

## API Details (Reverse-Engineered)

- **Submit:** `POST /api/submit-jobs` with body `{f: {mode: "fast", private: false}, channelId: "singleplayer_{userId}", metadata: {imagePrompts: [], imageReferences: [], characterReferences: [], styleReferences: []}, t: "imagine", prompt: "..."}`
- **Submit response:** `{"success": [{"job_id": "uuid", "prompt": "...", "is_queued": false, "event_type": "diffusion", "meta": {height, width, batch_size}}], "failure": []}`
- **Poll/List:** `GET /api/imagine?user_id={USER_ID}&page_size=N` → `{data: [{id, full_command, job_type, enqueue_time, width, height, batch_size, ...}], cursor, checkpoint}`
  - This endpoint only returns **completed** jobs. If a job appears in results, it's done.
  - Response fields: `full_command` (not `prompt`), `job_type` (e.g. `v7_diffusion`), `width`/`height`
- **Auth:** httpOnly cookies (set by Google OAuth login) + `x-csrf-protection: 1` header
- **CDN:** `https://cdn.midjourney.com/{job_id}/0_{index}.jpeg` (full-res), `0_{index}_640_N.webp?method=shortest` (thumbnail)
- **Upload image:** `POST /api/storage-upload-file` (multipart form, field name `file`) → `{"shortUrl": "https://s.mj.run/...", "bucketPathname": "..."}`
  - MUST use curl_cffi with TLS fingerprinting (Cloudflare blocks plain requests)
  - Uses low-level `Curl()` + `CurlMime` for multipart (scrapling Fetcher doesn't support multipart)
- **Download:** Direct via scrapling with auth cookies, Chrome fallback if needed

## Image Prompts & References

- **Image Prompt (`--image`):** Upload local file → get s.mj.run URL → prepend to prompt + include in `metadata.imagePrompts[]`
- **Style Reference (`--sref`):** Same upload flow → include in `metadata.styleReferences[]`
- **Image Weight:** `--iw N` (0-3, default 1) controls how strongly the image prompt affects the result
- **Omni Reference (`--oref`):** Upload face photo → include in `metadata.characterReferences[]` + `--oref <url>` in prompt. Preserves facial likeness.
- **Omni Reference Weight:** `--ow N` (0-1000, default 100) controls face fidelity
- **Best practice:** When transforming people, always use `--oref` with the same photo as `--image` plus `--iw 2` for best face preservation

## scrapling Notes

- Use `Fetcher()` with no arguments — `auto_match` parameter is deprecated
- POST requests use `json=` parameter (not `body=` or `data=`): `fetcher.post(url, headers=headers, json=body)`
- Response parsing: use `response.json()` first, fallback to `json.loads(response.body.decode())`
- `response.html_content` can return empty for JSON APIs — don't rely on it for JSON parsing

## Image URL Patterns

```
# Full resolution
https://cdn.midjourney.com/{job_id}/0_{0-3}.jpeg

# Thumbnail (640px)
https://cdn.midjourney.com/{job_id}/0_{0-3}_640_N.webp?method=shortest

# Small thumbnail (384px)
https://cdn.midjourney.com/{job_id}/0_{0-3}_384_N.webp?method=shortest&qst=6
```

## Prompt Tips

- Default version is v7 (latest, no need to specify `--v 7`)
- `--ar W:H` for aspect ratio (e.g., `--ar 16:9`, `--ar 1:1`)
- `--stylize N` (0-1000) controls artistic interpretation
- `--weird N` (0-3000) for unusual aesthetics
- `--chaos N` (0-100) for variation between the 4 results
- `--no item` negative prompting
- `--style raw` for less Midjourney aesthetic
- `--draft` for faster, lower quality (good for iteration)
- Use `niji` model for anime: `--niji 7`

## Settings

Stored in localStorage `settings_v20`:
- Version: 7 (default, latest)
- Stylize: 100 (default)
- Mode: fast
- Aspect ratio: 1:1 (default)

## V8 Alpha (March 17, 2026)

- Available via `--v 8` in prompt
- 4-5x faster generation
- Native 2K resolution with `--hd` (4x GPU cost)
- Better prompt adherence and text rendering
- `--q 4` for higher quality (4x GPU cost)
- Style references cost 4x GPU in V8
- Seed reproducibility: 99% identical with same seed

## Video Generation

```bash
# Animate an image from a recent generation (uses image index 0 by default)
~/.claude/skills/midjourney/scripts/midjourney video <job_id> --index 0

# With motion control and prompt
~/.claude/skills/midjourney/scripts/midjourney video <job_id> --index 2 --motion high --prompt "dramatic camera zoom"

# HD resolution (720p, costs more GPU)
~/.claude/skills/midjourney/scripts/midjourney video <job_id> --index 0 --resolution 720
```

- Generate 5-second videos from images (4 variations per submission)
- Motion control: `--motion auto` (default), `low` (subtle), or `high` (dramatic)
- Resolution: SD (480p default) or HD (720p, Fast Mode only)
- Looping: `--loop` for seamless start/end
- End frame: `--end <image_url>` for custom ending
- Batch size: `--bs 1/2/4` (controls how many videos per prompt)
- Extend up to 21 seconds (4 extensions of 4 seconds each)

### Video API Details

- **Submit:** `POST /api/submit-jobs` with `{t: "video", videoType: "vid_1.1_i2v_480", parentJob: {job_id, image_num}, animateMode: "auto", newPrompt: "..."}`
- **Poll:** Same as images — `GET /api/imagine?user_id=...` — video jobs appear when complete with `video_segments: [125]` (125 frames = 5 sec at 25fps)
- **CDN:** `https://cdn.midjourney.com/video/{job_id}/{index}.mp4` (note `/video/` path prefix — different from images!)
- **Prompt:** Strip image URLs from parent job's `full_command` before submitting (API rejects image prompts with parent jobs)

## Notes

- Basic plan: ~200 fast generations/month
- Each generation produces 4 image variations (batch_size=4)
- Upscale options: Subtle, Creative (uses extra GPU time)
- Can vary images: Subtle, Strong
- Images are 1024x1024 by default (v7), varies with aspect ratio
- Chrome must be logged in to midjourney.com (Google OAuth) for cookie extraction
- Generation typically completes in 30-60 seconds
- CLI downloads all 4 variations automatically on imagine
