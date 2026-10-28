---
name: cad
description: Generate 3D CAD models programmatically using CadQuery. Create parametric parts, export STL/STEP, render previews. Trigger words - cad, 3d model, parametric, stl, step, bracket, enclosure, part, openscad, scad.
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
```

## Capabilities

- **Parametric modeling**: Dimensions, hole patterns, arrays all adjustable
- **Export formats**: STL (3D printing), STEP (CAD interchange)
- **Rendering**: Blender Cycles GPU rendering for previews
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

Uses Blender Cycles with:
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

Install: the Homebrew cask is **disabled** (it fails Gatekeeper), so install from the DMG and keep a permanent copy.
Don't run the binary from a mounted DMG, because it's gone once the DMG is unmounted:
```bash
# download the macOS DMG from https://openscad.org/downloads.html, then:
hdiutil attach ~/Downloads/OpenSCAD-*.dmg
cp -R /Volumes/OpenSCAD/OpenSCAD.app /Applications/
xattr -dr com.apple.quarantine /Applications/OpenSCAD.app
hdiutil detach /Volumes/OpenSCAD
```
Headless use:
```bash
OSC=/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD
$OSC -o part.stl part.scad
$OSC -o part.png --autocenter --viewall --imgsize=1600,1200 part.scad   # add --camera=... for a specific view
~/.claude/skills/cad/scripts/cad-render part.stl                         # nicer Blender render from the STL
```

## Limitations

- No GUI preview (headless only)
- Fillets can fail on complex geometry (use simpler shapes)
- Large models may take longer to render

## Output Locations

- STL/STEP: `/tmp/cad_output/` (timestamped)
- Renders: `/tmp/cad_output/` (PNG)
