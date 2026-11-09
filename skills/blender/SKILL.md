---
name: blender
description: 3D rendering and animation with Blender (Homebrew cask, headless EEVEE works). Trigger words - blender, 3d, render, animation, murmuration.
---

# Blender Skill

Blender is a free 3D creation suite for modeling, animation, rendering, and more.

**Update (2026-11): the official Homebrew cask works headless on macOS 26.** `brew install --cask blender` installs 5.2.x, and `/Applications/Blender.app/Contents/MacOS/Blender --background --python script.py` renders EEVEE frames fine on Apple Silicon. Use that binary. The `~/blender-git` source build described below is historical and may not exist; only use it if the cask binary hangs in dyld_start. For narrated assembly videos, see `cad-blender-assembly` in the `/cad` skill.

## Quick Reference

```bash
# Blender CLI (built from source)
~/blender-git/build_darwin/bin/Blender.app/Contents/MacOS/Blender --background --python script.py
~/blender-git/build_darwin/bin/Blender.app/Contents/MacOS/Blender --background --python script.py --render-anim
~/blender-git/build_darwin/bin/Blender.app/Contents/MacOS/Blender --version

# Interactive control via socket (requires GUI blender running with MCP addon)
~/.claude/skills/blender/scripts/blender-socket status
~/.claude/skills/blender/scripts/blender-socket scene
~/.claude/skills/blender/scripts/blender-socket screenshot -o /tmp/viewport.png
~/.claude/skills/blender/scripts/blender-socket exec "bpy.ops.mesh.primitive_cube_add()"

# MCP tools via bunx (alternative interface)
~/.bun/bin/bunx mcptools call get_scene_info uvx blender-mcp
~/.bun/bin/bunx mcptools call get_viewport_screenshot uvx blender-mcp
~/.bun/bin/bunx mcptools call execute_blender_code uvx blender-mcp -p '{"code": "bpy.ops.mesh.primitive_sphere_add()"}'
```

## BlenderMCP Integration (Interactive Control)

BlenderMCP allows real-time control of Blender via socket connection. This enables:
- Getting scene info (objects, materials, lights)
- Taking viewport screenshots
- Executing Python code in the running Blender session
- Visual feedback loop for iterative 3D work

### Setup (One-Time)

The BlenderMCP addon is already installed at `~/Library/Application Support/Blender/5.2/scripts/addons/blendermcp_addon.py`.

### Starting Blender with MCP Server

```bash
# Start Blender GUI with auto-start server
~/blender-git/build_darwin/bin/Blender.app/Contents/MacOS/Blender --python ~/.claude/skills/blender/scripts/autostart_mcp.py &

# Wait a few seconds for server to start, then test
sleep 5
~/.claude/skills/blender/scripts/blender-socket status
```

### Socket CLI Commands

```bash
# Check connection status
~/.claude/skills/blender/scripts/blender-socket status

# Get scene info (objects, materials, counts)
~/.claude/skills/blender/scripts/blender-socket scene

# Get specific object info
~/.claude/skills/blender/scripts/blender-socket object Cube

# Take viewport screenshot
~/.claude/skills/blender/scripts/blender-socket screenshot -o /tmp/viewport.png -s 1200

# Execute Python code in Blender
~/.claude/skills/blender/scripts/blender-socket exec "bpy.ops.mesh.primitive_torus_add()"

# Execute Python script file
~/.claude/skills/blender/scripts/blender-socket exec /path/to/script.py

# Send raw JSON command
~/.claude/skills/blender/scripts/blender-socket raw '{"type": "get_scene_info", "params": {}}'
```

### MCP Tools via bunx

Alternative interface using the MCP protocol:

```bash
# List available tools
~/.bun/bin/bunx mcptools tools uvx blender-mcp

# Get scene info
~/.bun/bin/bunx mcptools call get_scene_info uvx blender-mcp -p '{"user_prompt": "check scene"}'

# Take viewport screenshot
~/.bun/bin/bunx mcptools call get_viewport_screenshot uvx blender-mcp -p '{"max_size": 800}'

# Execute code
~/.bun/bin/bunx mcptools call execute_blender_code uvx blender-mcp -p '{"code": "bpy.ops.mesh.primitive_cube_add(size=2)"}'

# Search Poly Haven assets (HDRIs, textures, models)
~/.bun/bin/bunx mcptools call search_polyhaven_assets uvx blender-mcp -p '{"asset_type": "hdris"}'
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `get_scene_info` | Get objects, materials, scene details |
| `get_object_info` | Get specific object details + bounding box |
| `get_viewport_screenshot` | Capture current 3D viewport |
| `execute_blender_code` | Run Python code in Blender |
| `search_polyhaven_assets` | Search Poly Haven for HDRIs/textures/models |
| `download_polyhaven_asset` | Download and import Poly Haven assets |
| `search_sketchfab_models` | Search Sketchfab 3D models |
| `download_sketchfab_model` | Download and import Sketchfab models |

## Installation (macOS Tahoe)

**IMPORTANT:** Official blender binaries DO NOT work on macOS 26 Tahoe. They hang at 0% CPU in dyld_start. Must build from source:

```bash
# Install dependencies
brew install cmake git-lfs
git lfs install

# Clone and build (~20-30 min on M4 Pro)
mkdir -p ~/blender-git
cd ~/blender-git
git clone --depth 1 https://projects.blender.org/blender/blender.git
cd blender
make update  # Downloads libraries (~500MB)
make         # Compiles blender

# Test (use full path - symlinks can be problematic)
~/blender-git/build_darwin/bin/Blender.app/Contents/MacOS/Blender --version
```

## Render Engines (Blender 5.x)

```python
# BLENDER_EEVEE - Fast real-time rendering (use this!)
bpy.context.scene.render.engine = 'BLENDER_EEVEE'

# CYCLES - Ray-traced photorealistic rendering (slow but high quality)
bpy.context.scene.render.engine = 'CYCLES'

# BLENDER_WORKBENCH - Viewport rendering
bpy.context.scene.render.engine = 'BLENDER_WORKBENCH'
```

**NOTE:** In Blender 5.x, `BLENDER_EEVEE_NEXT` was renamed to just `BLENDER_EEVEE`.

## Rendering to Video

EEVEE on macOS CLI renders frames as PNGs, not directly to video. Workflow:

```python
# In your Blender script - output PNG frames
bpy.context.scene.render.filepath = "/Users/sven/blender-renders/output"
bpy.context.scene.render.image_settings.file_format = 'PNG'
```

Then convert with ffmpeg:
```bash
ffmpeg -y -framerate 24 -i output%04d.png -c:v libx264 -pix_fmt yuv420p -crf 18 video.mp4
```

## Python Scripting

Blender includes its own Python with the `bpy` module for scripting:

```python
import bpy
import math

# Clear scene
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete()

# Create cube
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0))
cube = bpy.context.active_object

# Add material (use_nodes will be deprecated in Blender 6.0 but works now)
mat = bpy.data.materials.new(name="RedMaterial")
mat.use_nodes = True
bsdf = mat.node_tree.nodes["Principled BSDF"]
bsdf.inputs["Base Color"].default_value = (1, 0, 0, 1)
cube.data.materials.append(mat)

# Setup camera
bpy.ops.object.camera_add(location=(0, -10, 5))
camera = bpy.context.active_object
camera.rotation_euler = (math.radians(60), 0, 0)
bpy.context.scene.camera = camera

# Setup render
bpy.context.scene.render.engine = 'BLENDER_EEVEE'
bpy.context.scene.render.resolution_x = 1280
bpy.context.scene.render.resolution_y = 720
bpy.context.scene.render.filepath = "/Users/sven/blender-renders/frame"

# Render single frame
bpy.ops.render.render(write_still=True)
```

## Animation Keyframes

```python
# Set frame range
bpy.context.scene.frame_start = 1
bpy.context.scene.frame_end = 120
bpy.context.scene.render.fps = 24

# Create keyframes
obj = bpy.context.active_object
obj.location = (0, 0, 0)
obj.keyframe_insert(data_path="location", frame=1)
obj.location = (5, 0, 0)
obj.keyframe_insert(data_path="location", frame=60)
```

## Bird Murmuration Example

A complete boid flocking simulation is at `~/blender-renders/murmuration.py`. It creates:
- 200 birds with cone shapes
- Boid behavior (separation, alignment, cohesion)
- Circling around a moving center point
- 5 seconds at 24fps (120 frames)

Run with:
```bash
cd ~/blender-renders
~/blender-git/build_darwin/bin/Blender.app/Contents/MacOS/Blender --background --python murmuration.py --render-anim
ffmpeg -y -framerate 24 -i murmuration%04d.png -c:v libx264 -pix_fmt yuv420p -crf 18 murmuration.mp4
```

## CLI Options

```bash
# Basic
--background              # Run headless (no GUI)
--python script.py        # Execute Python script
--render-anim             # Render animation
--render-frame 1          # Render single frame
--version                 # Show version

# Example: Run script and render
~/blender-git/build_darwin/bin/Blender.app/Contents/MacOS/Blender \
  --background \
  --python render_script.py \
  --render-anim
```

## Troubleshooting

### Blender hangs at startup
This happens with official binaries on macOS Tahoe. Solution: build from source (see Installation above).

### "BLENDER_EEVEE_NEXT" not found
In Blender 5.x, use `'BLENDER_EEVEE'` instead of `'BLENDER_EEVEE_NEXT'`.

### Font warnings (blf_load_font_default)
Cosmetic warnings in CLI mode - can be ignored.

### use_nodes deprecation warning
`Material.use_nodes` and `World.use_nodes` are deprecated in Blender 5.x and will be removed in 6.0, but still work for now.

### EEVEE shadow settings (Blender 5.x)
In Blender 5.x, use `bpy.context.scene.eevee.use_shadows = True` (not `use_soft_shadows`).

## Project Locations

- **Source**: `~/blender-git/blender/`
- **Build**: `~/blender-git/build_darwin/`
- **Binary**: `~/blender-git/build_darwin/bin/Blender.app/Contents/MacOS/Blender`
- **Renders**: `~/blender-renders/`
- **Murmuration script**: `~/blender-renders/murmuration.py`
