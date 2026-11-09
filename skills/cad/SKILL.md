---
name: cad
description: "Generate 3D CAD models programmatically using CadQuery. Create parametric parts, export STL/STEP, render previews. Trigger words - cad, 3d model, parametric, stl, step, bracket, enclosure, part, openscad, scad. Also: render assembly/exploded-view videos or GIFs from .scad or STL parts. Trigger words - assembly video, build guide video, exploded view, assembly animation, how to assemble."
---

# CAD Skill

Generate 3D CAD models programmatically using CadQuery (Python) with Blender rendering.

## Quick Start

```bash
# Generate a model from description
~/.claude/skills/cad/scripts/cad-generate "box with 4 mounting holes"

# Render an existing STL
~/.claude/skills/cad/scripts/cad-render /path/to/model.stl

# List example templates
~/.claude/skills/cad/scripts/cad-generate --list-templates

# Assembly / exploded-view video (MP4 + GIF) from a .scad assembly + steps.yaml
~/.claude/skills/cad/scripts/cad-assembly-video assembly.scad --steps steps.yaml --gif --keyframes /tmp/kf
```

## Capabilities

- **Parametric modeling**: Dimensions, hole patterns, arrays all adjustable
- **Export formats**: STL (3D printing), STEP (CAD interchange)
- **Rendering**: Blender Cycles GPU rendering for previews (`cad-render`; the `cad-blender-assembly` videos use EEVEE)
- **Common parts**: Brackets, enclosures, mounts, spacers, adapters

## Architecture

```
User prompt → Claude generates CadQuery Python → Execute → STL/STEP
                                                    ↓
                                              Blender render → PNG
```

## Python Environment

Uses dedicated venv at `~/.venvs/cad` with Python 3.12:
- cadquery (parametric CAD)
- cadquery-ocp (OpenCASCADE kernel)

## Example Code

```python
import cadquery as cq

# L-bracket with mounting holes
result = (
    cq.Workplane("XY")
    .box(50, 30, 5)                    # Base plate
    .faces(">Z").workplane()
    .move(0, 12.5)
    .box(50, 5, 40, centered=(True, True, False))  # Vertical
    .faces("<Z").workplane()
    .pushPoints([(-15, 0), (15, 0)])
    .hole(5)                           # Mounting holes
)

cq.exporters.export(result, "bracket.stl")
cq.exporters.export(result, "bracket.step")
```

## Common Patterns

### Hole Arrays
```python
.pushPoints([(x, y) for x in [-20, 0, 20] for y in [-10, 10]])
.hole(diameter)
```

### Fillets (rounded edges)
```python
.edges("|Z").fillet(radius)  # Vertical edges
.edges(">Z").fillet(radius)  # Top edges
```

### Shell (hollow out)
```python
.shell(-wall_thickness)  # Negative = outward, positive = inward
```

### Extrude with taper
```python
.extrude(height, taper=5)  # 5 degree draft angle
```

## Rendering

`cad-render` previews use Blender Cycles (assembly videos use EEVEE, see below) with:
- GPU acceleration (Metal on macOS)
- 64 samples for quick preview
- Blue metallic material
- Dark background

## Rule: a 3D rendering of a hardware design is real geometry
When asked to "render" or "show in 3D" a part or device, build the model first (CadQuery or OpenSCAD) and render that geometry
(`cad-render`, or an OpenSCAD PNG). **Never use Midjourney or nano-banana for engineering/hardware renders.** They invent
geometry that doesn't match the design.

## OpenSCAD (.scad files)
Keep the `.scad` file as the source of truth. CadQuery can't import it.

Install the **snapshot** cask (2024+ dev build with the fast Manifold backend; it links `openscad` onto PATH):
```bash
brew install --cask openscad@snapshot
openscad --version        # e.g. "OpenSCAD version 2026.10.11"
```
Don't use the plain `openscad` cask: it's the 2021.01 Intel build and Homebrew has disabled it (fails Gatekeeper).
Fallback if the snapshot cask breaks: download the DMG from https://openscad.org/downloads.html, then
`hdiutil attach ~/Downloads/OpenSCAD-*.dmg && cp -R /Volumes/OpenSCAD/OpenSCAD.app /Applications/ &&
xattr -dr com.apple.quarantine /Applications/OpenSCAD.app && hdiutil detach /Volumes/OpenSCAD`.
Never run the binary from a mounted DMG.

Headless use:
```bash
openscad -o part.stl part.scad
openscad --backend=manifold --render -o part.png --autocenter --viewall --imgsize=1600,1200 part.scad   # add --camera=... for a specific view
~/.claude/skills/cad/scripts/cad-render part.stl                         # nicer Blender render from the STL
```

## Assembly / exploded-view videos (`cad-assembly-video`)

Turns a `.scad` assembly (or STL parts) plus a `steps.yaml` into a captioned MP4 (and GIF) where the parts
fly in one step at a time. Use this when someone asks for an assembly video, build guide, or "how do I put this together".

```bash
~/.claude/skills/cad/scripts/cad-assembly-video assembly.scad --steps steps.yaml \
    [--out out.mp4] [--gif] [--fps 24] [--size 1280x720] [--keyframes DIR] \
    [--colorscheme Tomorrow] [--gif-width 720] [--jobs N] [--font path.ttf] [--keep]
```
- Prints JSON: `out`, `size_bytes`, `duration_s`, `frames`, `fps`, `width`, `height`, `gif`, `gif_size_bytes`, `keyframes`, `workdir` (with `--keep`).
- Default output is `/tmp/cad_output/<name>_assembly_<timestamp>.mp4` (H.264, yuv420p, faststart, so it plays inline on iPhone).
- `--keyframes DIR` writes one captioned PNG per step (part caught mid-flight), plus `00_intro.png` and `NN_final.png`. **Read these before sending.**
- `--keep` keeps the work dir (`wrapper.scad`, `frames/`, probe renders) for debugging. Open `wrapper.scad` in OpenSCAD to scrub `$t`.
- The `assembly` argument is optional if every step uses `stl:`.

**Prerequisites:** `openscad` 2024+ snapshot (see install above; uses `--backend=manifold --render`), `ffmpeg`, `uv`.
Captions are drawn with Pillow and composited with ffmpeg's `overlay` filter, because Homebrew's ffmpeg is built without
`drawtext`/libfreetype.

**How it works:** it copies the assembly into a temp dir and rewrites relative `use<>`/`include<>`/`import()` paths to
absolute ones. Then it generates `wrapper.scad`, with one module per step and explode offsets eased by `$t`. The camera
is keyframed through `$vpr`/`$vpt`/`$vpd`. Probe renders (`--viewall --summary`) choose a fixed camera that fits the
assembled model plus every fly-in path, raised so the model clears the caption bar. Frames come from
`openscad --animate N`, sharded across CPU cores. A 3-step, 15 s demo takes about 7 s wall time.

### steps.yaml
```yaml
title: "Bracket kit - assembly guide"   # intro caption (intro shows all parts spread out)
seconds_per_step: 3        # default per step
fly_fraction: 0.6          # share of each step spent flying in; the rest is a hold
explode_distance: 60       # default offset, mm (auto = 0.6 x largest assembled dimension)
mode: use                  # use (default): modules only, top-level geometry ignored
                           # include: also exposes top-level variables (plate_t etc.)
defines: {show_all: false} # emitted AFTER include, so it overrides the file's own values (raw OpenSCAD exprs)
colorscheme: Tomorrow
intro: true                # false = skip the parts-overview intro (intro_seconds: 2)
camera: {rot: [60, 0, 30], zoom: 1.0}   # global: rot=[rx,ry,rz]; optional center: [x,y,z], dist: mm
steps:
  - caption: "Lay the base plate flat, holes facing up"
    part: base_plate()                    # any OpenSCAD statement(s), already at assembled position
    color: SteelBlue                      # default: a palette color per step
    explode: "-z 30"                      # flies in FROM here: [x,y,z] | "+z" | "-x 40" | number (= z)
  - caption: "Slide the bracket in from the back"
    part: translate([0,0,5]) bracket()
    explode: [0, 50, 0]
    camera: {rot: [70, 0, 120]}           # camera eases to this view before the part moves
  - caption: "Drive both M4 screws"
    parts: ["translate([-10,0,9]) screw()", "translate([10,0,9]) screw()"]   # several parts, one step
    explode: "+z 35"
    camera: {rot: [45, 0, 20], fit: step} # fit: step = frame just this step's part and its travel (close-up)
    seconds: 4
  - caption: "Add the cover (from an STL)"
    stl: parts/cover.stl                  # path relative to steps.yaml (or the assembly dir)
    at: [0, 0, 40]                        # optional translate / rotate for stl or part
    rotate: [0, 0, 90]
    intro_explode: [0, 0, 120]            # optional override of the intro layout position
  - caption: "Flip it over and check the feet"   # a step with no part = a camera move + caption only
    camera: "0,0,10,120,0,30,400"          # raw tx,ty,tz,rx,ry,rz,dist also accepted
outro: {spin: true, seconds: 4, caption: "Assembly complete", zoom: 1.0}   # turntable of the finished model; false = none; zoom >1 = closer
```
Tips:
- Each `part` must already be at its assembled position. `explode` is the direction it comes **from**: screws come from `+z`, a bottom plate from `-z`.
- If parts clip or hide each other in the intro, set `intro_explode` on those steps. If a small part is hard to see, use `camera: {fit: step}` or `zoom: 2`.
- `mode: use` can't see the assembly's top-level variables, so write numbers in `part`, or switch to `mode: include` and silence the top-level geometry with a guard variable in `defines`.
- STL-only projects can omit the `.scad` argument and use `stl:` + `at:` for every step.

**Realistic + narrated: `cad-blender-assembly`** (Blender EEVEE). Use it when someone wants the video to look real
(PETG plastic with layer lines, brushed-metal rods and screws, a wooden bench, soft area lights, shadows, depth of
field, a moving camera) and/or wants voice narration. Blender comes from `brew install --cask blender`, and v5.2
renders headless fine on macOS. The pipeline has three subcommands:
```bash
S=~/.claude/skills/cad/scripts/cad-blender-assembly
$S narrate narration.yaml /tmp/x/audio          # Kokoro TTS -> narration.wav + timeline.json (segment timings)
$S render  config.yaml /tmp/x/draft --draft --step 3   # half-res, every 3rd frame: check before the final
$S render  config.yaml /tmp/x/frames --procs 3 --samples 14   # 1280x720 @24fps, 3 Blender processes in parallel
$S mux     /tmp/x/frames /tmp/x/audio out.mp4 [--step 3 for a draft]   # subtitles + step badge + AAC narration
```
- **narration.yaml** is `{voice: af_heart, segments: [{id, caption, say, title?, subtitle?}]}`. The first segment is
  the intro and the last is the outro (both show the finished model, with a title card from `title`/`subtitle`).
  Every other segment is one step. Each segment lasts lead (0.5 s) + spoken clip + tail (0.9 s), so the animation is
  timed by the narration. Spell out the spoken text phonetically (`M three by eight`, `H X seven eleven`), or Kokoro
  reads part numbers as huge numbers. Keep the compact form (`M3 x 8`) in `caption`.
- **config.yaml**: `stl_dir`, `timeline`, and `scene_radius` (mm; scales the bench and the light rig, which is tuned
  for about 600). Also `floor_z` (mm, the bench top) and `parts: {stl_name: material}`. Materials are `petg[:#hex]`,
  `petg_matte`, `steel`, `alu`, `screw`, `brass`, `rubber`, `pcb[:#hex]`, `chip` and `mdf`.
  `movers:` is a list of `{step, parts: [stl names], offset: [x,y,z] mm, delay, dur, split, sort_axis, stagger, mirror, spin}`.
  Each mover appears when its step starts, sits at `offset`, then eases home over `dur` seconds, starting `delay`
  seconds after that step's speech begins. With `split: true`, each loose piece of the first STL becomes its own
  mover, staggered by `stagger`, and loose pieces of the other listed STLs attach to the nearest one (for example,
  O-rings to their roller). `mirror: y` flips the offset for pieces on the -y side. `spin: 3` turns a screw 3 times
  as it drives in. Any part not named in a mover stays static (a backing board, for example).
  `cameras: {segment_id: [[tx,ty,tz], dist, azimuth, elevation, drift_deg_per_s, fstop]}`: azimuth 0 looks from -Y;
  set the distance to about 250 mm for close-ups of small parts. The camera eases between framings, drifts and dollies in slowly.
- STLs must already be at their assembled positions (mm). The easiest way is one OpenSCAD wrapper with
  `part = "..."` switches that call the assembly's `asm_*` modules, exported with `openscad --backend=manifold -D 'part="x"'`.
  Export a separate STL per material, and add visual-only hardware (rods, screws, PCBs) there.
- Performance on an M4: about 2.5 frames/s at 1280x720 with 14 samples and 3 processes, so a 130 s video renders in
  about 21 min. A draft (`--draft --step 3`) takes about 3 min. Extract frames from the draft MP4 with
  `ffmpeg -ss T -i draft.mp4 -frames:v 1` and READ them.
  Typical draft fixes: overexposure (lower the light power), a close-up hidden behind a big part (orbit the camera
  to the other side, e.g. azimuth 158), title cards covering the model (they go at the top), floating parts (add the mount/bracket).
- Worked example: `~/code/riftbound-sorter/cad/render_v2/` (parts.scad, export_stls.sh, narration.yaml, blender-config.yaml).

## Rule: BOM / consistency audit before sending any assembly video or guide
Before an assembly video or guide goes to the owner (2026-11-08: the owner spotted missing items himself while watching v2):
1. List every part, fastener, tool and consumable mentioned anywhere: narration.yaml (caption and say), steps.yaml, and the written guide.
2. Compare that list with BOM.md, the order history (Amazon orders, package-watch) and the repo. Build a table: item | in video | in BOM | ordered | status.
3. Check the specs: PSU amps against the summed load with headroom, calibration mass within the load-cell rating, and screw sizes against the screws actually bought.
4. Name any placeholder or stand-in shapes in the message to the owner (e.g. "arm post is a placeholder cylinder, no CAD in repo").
5. Verify the audio. Run `uv run ~/.claude/skills/transcribe/scripts/transcribe.py <AUDIO_DIR>/narration.wav` and compare it with the `say` text. Watch for mangled part numbers, skipped lines and drift.
6. Report MISSING items and spec issues up front, and fix what you can BEFORE sending. Don't let the owner find them.

## Rule: a requested video must be a video
If the owner asks for a **video** (assembly guide, "show me how to put it together", animation), send an **MP4**, plus a
GIF if useful. An HTML page or a set of stills is not a video. HTML can go along as a clearly labelled extra
("interactive version"), never instead of the video.
When told to **iterate**, render a draft with `--keyframes` and Read the keyframe PNGs. Fix the camera angle, occluded
or off-screen parts, the explode directions, and caption wording or length. Do that at least **twice** before sending
the video. In the message, say what each step shows.

## Limitations

- No GUI preview (headless only)
- Fillets can fail on complex geometry (use simpler shapes)
- Large models may take longer to render

## Output Locations

- STL/STEP: `/tmp/cad_output/` (timestamped)
- Renders: `/tmp/cad_output/` (PNG)
