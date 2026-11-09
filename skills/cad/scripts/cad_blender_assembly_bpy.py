"""Blender (Eevee) side of cad-blender-assembly. Do not run directly - use the
`cad-blender-assembly render` wrapper, which converts the YAML config to JSON and shards frames.

  Blender --background --python cad_blender_assembly_bpy.py -- --config cfg.json --out DIR [...]

Config keys (mm, Z up): parts {name: material}, movers [...], cameras {segment: [...]},
stl_dir, timeline (from `cad-blender-assembly narrate`). See ~/.claude/skills/cad/SKILL.md.
"""
import bpy, bmesh, json, math, os, sys, argparse
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--fps", type=int, default=24)
ap.add_argument("--draft", action="store_true")
ap.add_argument("--step", type=int, default=1, help="render every Nth frame")
ap.add_argument("--only", default="", help="comma list of frame numbers to render")
ap.add_argument("--start", type=int, default=0)
ap.add_argument("--end", type=int, default=-1)
ap.add_argument("--res", default="1280x720")
ap.add_argument("--samples", type=int, default=24)
ap.add_argument("--no-rt", action="store_true", help="disable Eevee ray tracing (faster)")
A = ap.parse_args(argv)
MM = 0.001
CFG = json.load(open(A.config))
A.stl = CFG['stl_dir']; A.timeline = CFG['timeline']

# ───────────────────────── scene setup ─────────────────────────
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.engine = "BLENDER_EEVEE"
W, H = (int(v) for v in A.res.split("x"))
sc.render.resolution_x, sc.render.resolution_y = (W // 2, H // 2) if A.draft else (W, H)
sc.render.resolution_percentage = 100
sc.render.image_settings.file_format = "PNG"
sc.render.film_transparent = False
ee = sc.eevee
ee.taa_render_samples = 12 if A.draft else A.samples
for attr, val in [("use_raytracing", True), ("use_shadows", True), ("shadow_ray_count", 2),
                  ("shadow_step_count", 8), ("use_gtao", True), ("fast_gi_method", "GLOBAL_ILLUMINATION")]:
    try: setattr(ee, attr, val)
    except Exception: pass
try:
    ee.ray_tracing_options.resolution_scale = "2"
except Exception: pass
if A.no_rt: ee.use_raytracing = False
sc.view_settings.view_transform = "AgX"
try: sc.view_settings.look = "AgX - Medium High Contrast"
except Exception: pass

# world: soft warm-grey studio
world = bpy.data.worlds.new("studio"); sc.world = world
world.use_nodes = True
wn = world.node_tree.nodes; bg = wn["Background"]
bg.inputs[0].default_value = (0.62, 0.64, 0.68, 1); bg.inputs[1].default_value = 0.16


def mat(name, color, rough=0.5, metal=0.0, layer_lines=False, brushed=False, wood=False, spec=0.5, coat=0.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; N = nt.nodes; L = nt.links
    p = N["Principled BSDF"]
    c = tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    c = tuple(v ** 2.2 for v in c) + (1,)
    p.inputs["Base Color"].default_value = c
    p.inputs["Roughness"].default_value = rough
    p.inputs["Metallic"].default_value = metal
    try: p.inputs["Specular IOR Level"].default_value = spec
    except Exception: pass
    if coat:
        p.inputs["Coat Weight"].default_value = coat
    if layer_lines:  # 0.2 mm FDM layer lines as a faint bump along Z
        geo = N.new("ShaderNodeNewGeometry"); sep = N.new("ShaderNodeSeparateXYZ")
        mul = N.new("ShaderNodeMath"); mul.operation = "MULTIPLY"; mul.inputs[1].default_value = 2 * math.pi / (0.2 * MM)
        sn = N.new("ShaderNodeMath"); sn.operation = "SINE"
        bump = N.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.06
        bump.inputs["Distance"].default_value = 0.00004
        L.new(geo.outputs["Position"], sep.inputs[0]); L.new(sep.outputs["Z"], mul.inputs[0])
        L.new(mul.outputs[0], sn.inputs[0]); L.new(sn.outputs[0], bump.inputs["Height"])
        L.new(bump.outputs[0], p.inputs["Normal"])
    if brushed:  # streaky roughness variation
        tc = N.new("ShaderNodeTexCoord"); mp = N.new("ShaderNodeMapping")
        mp.inputs["Scale"].default_value = (1, 1, 400)
        nz = N.new("ShaderNodeTexNoise"); nz.inputs["Scale"].default_value = 60
        rmp = N.new("ShaderNodeMapRange"); rmp.inputs[3].default_value = rough * 0.6; rmp.inputs[4].default_value = rough * 1.4
        L.new(tc.outputs["Object"], mp.inputs[0]); L.new(mp.outputs[0], nz.inputs[0])
        L.new(nz.outputs[0], rmp.inputs[0]); L.new(rmp.outputs[0], p.inputs["Roughness"])
        try: p.inputs["Anisotropic"].default_value = 0.6
        except Exception: pass
    if wood:  # oak-ish bench top
        tc = N.new("ShaderNodeTexCoord"); mp = N.new("ShaderNodeMapping")
        mp.inputs["Scale"].default_value = (0.6, 9, 1)
        wv = N.new("ShaderNodeTexWave"); wv.inputs["Scale"].default_value = 3.0
        wv.inputs["Distortion"].default_value = 1.5; wv.inputs["Detail"].default_value = 4
        nz = N.new("ShaderNodeTexNoise"); nz.inputs["Scale"].default_value = 8
        mix = N.new("ShaderNodeMix"); mix.data_type = "FLOAT"; mix.inputs[0].default_value = 0.35
        ramp = N.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = (0.16, 0.085, 0.04, 1)
        ramp.color_ramp.elements[1].color = (0.36, 0.22, 0.11, 1)
        L.new(tc.outputs["Object"], mp.inputs[0]); L.new(mp.outputs[0], wv.inputs[0]); L.new(mp.outputs[0], nz.inputs[0])
        L.new(wv.outputs["Fac"], mix.inputs[2]); L.new(nz.outputs["Fac"], mix.inputs[3])
        L.new(mix.outputs[0], ramp.inputs[0]); L.new(ramp.outputs[0], p.inputs["Base Color"])
        bump = N.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.05
        L.new(mix.outputs[0], bump.inputs["Height"]); L.new(bump.outputs[0], p.inputs["Normal"])
    return m


PRESET = {  # kind -> mat() kwargs; "kind:#RRGGBB" overrides the colour
    "petg": dict(color="#C9CAC6", rough=0.42, layer_lines=True),
    "petg_matte": dict(color="#1B1C1F", rough=0.62, layer_lines=True, spec=0.35),
    "steel": dict(color="#C4C6C8", rough=0.28, metal=1.0, brushed=True),
    "alu": dict(color="#D3D5D8", rough=0.32, metal=1.0, brushed=True),
    "screw": dict(color="#B9BCC0", rough=0.22, metal=1.0),
    "brass": dict(color="#C9A04E", rough=0.3, metal=1.0),
    "rubber": dict(color="#151515", rough=0.85, spec=0.2),
    "pcb": dict(color="#1E6B3C", rough=0.35, coat=0.4),
    "chip": dict(color="#141416", rough=0.5),
    "mdf": dict(color="#A3835C", rough=0.8),
    "plastic": dict(color="#888888", rough=0.4),
}
M = {"wood": mat("bench oak", "#8A6440", 0.55, wood=True)}


def material(spec):
    if spec not in M:
        kind, _, col = spec.partition(":")
        kw = dict(PRESET.get(kind, PRESET["plastic"]))
        if col: kw["color"] = col
        color = kw.pop("color"); rough = kw.pop("rough", 0.4)
        M[spec] = mat(spec, color, rough, **kw)
    return M[spec]


PART_MAT = CFG["parts"]


def import_part(name):
    bpy.ops.wm.stl_import(filepath=os.path.join(A.stl, name + ".stl"), global_scale=MM)
    ob = bpy.context.selected_objects[0]; ob.name = name
    bm = bmesh.new(); bm.from_mesh(ob.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bm.to_mesh(ob.data); bm.free()
    ob.data.materials.append(material(PART_MAT[name]))
    bpy.context.view_layer.objects.active = ob
    for o in bpy.context.selected_objects: o.select_set(o == ob)
    try: bpy.ops.object.shade_smooth_by_angle(angle=math.radians(35))
    except Exception: bpy.ops.object.shade_auto_smooth(angle=math.radians(35))
    return ob


def split_loose(ob):
    for o in bpy.context.selected_objects: o.select_set(False)
    ob.select_set(True); bpy.context.view_layer.objects.active = ob
    bpy.ops.mesh.separate(type="LOOSE")
    parts = list(bpy.context.selected_objects)
    for p in parts:
        bpy.context.view_layer.objects.active = p
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    return parts


def center(objs):
    pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return (lo + hi) / 2


objs = {}
for name in PART_MAT:
    objs[name] = import_part(name)

# ───────────────────────── movers ─────────────────────────
# A mover is an Empty with parts parented to it. It starts at `off` (mm) relative to its home,
# appears when its step begins and eases home over [delay, delay+dur] seconds after speech starts.
movers = []  # dicts: step, empty, home, off, delay, dur, spin


def mover(step, parts, off, delay=0.0, dur=1.8, spin=0.0):
    c = center(parts)
    e = bpy.data.objects.new(f"mv_{step}_{len(movers)}", None); sc.collection.objects.link(e)
    e.location = c
    bpy.context.view_layer.update()  # so the parent matrix is current before re-parenting
    for p in parts:
        mw = p.matrix_world.copy(); p.parent = e; p.matrix_world = mw
    movers.append(dict(step=step, empty=e, home=c.copy(), off=Vector(off) * MM, delay=delay, dur=dur,
                       spin=spin, parts=parts))


AX = {"x": 0, "y": 1, "z": 2}
for spec in CFG["movers"]:
    names = spec["parts"] if isinstance(spec["parts"], list) else [spec["parts"]]
    off = spec.get("offset", [0, 0, 100]); kw = dict(delay=spec.get("delay", 0.2), dur=spec.get("dur", 1.8),
                                                      spin=spec.get("spin", 0.0))
    if not spec.get("split"):
        mover(spec["step"], [objs[n] for n in names], off, **kw); continue
    # split: loose pieces of the first STL become units (sorted along sort_axis, staggered);
    # pieces of further STLs attach to the nearest unit. mirror: flip offset on that axis per side.
    units = sorted(split_loose(objs[names[0]]), key=lambda o: o.matrix_world.translation[AX[spec.get("sort_axis", "x")]])
    groups = [[u] for u in units]
    for n in names[1:]:
        for pc in split_loose(objs[n]):
            j = min(range(len(units)), key=lambda q: (units[q].matrix_world.translation - pc.matrix_world.translation).length)
            groups[j].append(pc)
    for k, g in enumerate(groups):
        o = list(off)
        if spec.get("mirror"):
            ax = AX[spec["mirror"]]
            if g[0].matrix_world.translation[ax] < 0: o[ax] = -o[ax]
        mover(spec["step"], g, o, delay=kw["delay"] + spec.get("stagger", 0.4) * k, dur=kw["dur"], spin=kw["spin"])

# bench + lights
R = CFG.get('scene_radius', 600) * MM / 0.6  # light rig is tuned for a ~0.6 m model
bpy.ops.mesh.primitive_plane_add(size=10 * R, location=(0, 0, CFG.get('floor_z', -12) * MM))
bench = bpy.context.active_object; bench.name = "bench"; bench.data.materials.append(M["wood"])


def area(name, loc, power, size, color=(1, 1, 1)):
    ld = bpy.data.lights.new(name, "AREA"); ld.energy = power * R * R; ld.size = size * R; ld.color = color
    lo = bpy.data.objects.new(name, ld); sc.collection.objects.link(lo); lo.location = loc = Vector(loc) * R
    d = Vector((0, 0, 0.05)) - Vector(loc)
    lo.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    return lo


area("key", (-1.1, -1.3, 1.6), 110, 1.6, (1.0, 0.96, 0.9))
area("fill", (1.5, -0.8, 0.9), 30, 2.0, (0.9, 0.95, 1.0))
area("rim", (0.3, 1.6, 1.2), 70, 1.2)

camd = bpy.data.cameras.new("cam"); camd.lens = 45; camd.sensor_width = 36
cam = bpy.data.objects.new("cam", camd); sc.collection.objects.link(cam); sc.camera = cam
tgt = bpy.data.objects.new("focus", None); sc.collection.objects.link(tgt)
camd.dof.use_dof = True; camd.dof.focus_object = tgt
camd.clip_start = 0.01

# ───────────────────────── timeline & camera ─────────────────────────
TL = json.load(open(A.timeline))
SEG = {s["id"]: s for s in TL["segments"]}
ORDER = [s["id"] for s in TL["segments"]]
STEP_IDS = ORDER[1:-1]

# camera per segment: target (mm), distance (mm), azimuth (deg, 0 = front, + = toward +X), elevation, az drift deg/s, f-stop
CAM = {k: tuple(v) for k, v in CFG['cameras'].items()}
for sid in ORDER:
    CAM.setdefault(sid, CAM.get('default', ((0, 0, 0), 1000, -30, 30, 2.0, 11)))


def ease(t):
    t = max(0.0, min(1.0, t)); return t * t * t * (t * (6 * t - 15) + 10)


def cam_state(sid, t_in):
    tg, d, az, el, drift, fs = CAM[sid]
    s = SEG[sid]
    d = d * (1.0 - 0.05 * min(1.0, t_in / s["duration"]))  # slow dolly-in
    return Vector(tg) * MM, d * MM, az + drift * t_in, el, fs


def pose_camera(t):
    idx = max(i for i, sid in enumerate(ORDER) if SEG[sid]["start"] <= t + 1e-6)
    sid = ORDER[idx]; t_in = t - SEG[sid]["start"]
    tg, d, az, el, fs = cam_state(sid, t_in)
    blend = 1.6
    if idx > 1:
        blend = min(3.2, 1.4 + abs(CAM[sid][2] - CAM[ORDER[idx - 1]][2]) / 90)
    if idx > 1 and t_in < blend:  # smooth move from previous framing (cut after intro)
        pid = ORDER[idx - 1]
        ptg, pd, paz, pel, pfs = cam_state(pid, t - SEG[pid]["start"])
        k = ease(t_in / blend)
        tg = ptg.lerp(tg, k); d = pd + (d - pd) * k; az = paz + (az - paz) * k; el = pel + (el - pel) * k
        fs = pfs + (fs - pfs) * k
    a, e = math.radians(az), math.radians(el)
    pos = tg + d * Vector((math.cos(e) * math.sin(a), -math.cos(e) * math.cos(a), math.sin(e)))
    cam.location = pos
    cam.rotation_euler = (tg - pos).to_track_quat("-Z", "Y").to_euler()
    tgt.location = tg
    camd.dof.aperture_fstop = fs
    return sid


def pose_parts(t, sid):
    if sid in (ORDER[0], ORDER[-1]):  # intro / outro show the finished assembly
        for mv in movers:
            mv["empty"].location = mv["home"]; mv["empty"].rotation_euler = (0, 0, 0)
            for p in mv["parts"]: p.hide_render = False
        return
    cur = ORDER.index(sid)
    for mv in movers:
        si = ORDER.index(mv["step"])
        vis = si <= cur
        for p in mv["parts"]: p.hide_render = not vis
        if not vis: continue
        if si < cur:
            k = 1.0
        else:
            ts = t - SEG[sid]["speech_start"]
            k = ease((ts - mv["delay"]) / mv["dur"])
        mv["empty"].location = mv["home"] + mv["off"] * (1 - k)
        mv["empty"].rotation_euler = (0, 0, -mv["spin"] * 2 * math.pi * (1 - k))


fps = A.fps
total_frames = int(math.ceil(TL["total"] * fps))
os.makedirs(A.out, exist_ok=True)
if A.only:
    frames = [int(x) for x in A.only.split(",")]
else:
    end = total_frames if A.end < 0 else min(A.end, total_frames)
    frames = list(range(A.start, end, A.step))
print(f"total frames {total_frames}, rendering {len(frames)}", flush=True)
for f in frames:
    t = f / fps
    sid = pose_camera(t)
    pose_parts(t, sid)
    sc.render.filepath = os.path.join(A.out, f"f_{f:05d}.png")
    bpy.ops.render.render(write_still=True)
    print(f"frame {f} {sid}", flush=True)
