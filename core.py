PKG_NAME = __name__.split('.')[0]  # 'material_color_grid'
import bpy

import math

import json

import os

import random

import urllib.request

import urllib.error

import ssl

try:
    import numpy as _np
except ImportError:
    _np = None

UPDATE_REPO = "tappy3d-hue/material-color-grid"

UPDATE_ASSET_NAME = "material_color_grid.py"

UPDATE_API_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"

DEFAULT_GROUP_NAME = "ColorGrid"

MANIFEST_KEY = "mcg_manifest"

UV_LAYER_NAME = "ColorGridUV"

MODE_ITEMS = [
    ('ATLAS', "Atlas", "Bake this object's material colors into the shared grid texture"),
    ('VERTEX', "Vertex Color", "Bake each face's material Base Color into vertex colors (for FBX/glTF)"),
    ('JSON', "JSON", "Export colors as data for dynamic coloring in Roblox (implemented later)"),
    ('NONE', "None", "Leave this object untouched"),
]

VALID_MODES = {m[0] for m in MODE_ITEMS}

COLOR_GROUP_MODE_ITEMS = [
    ('AUTO', "Auto (Threshold)",
     "Merge sampled face colors that are within a distance threshold of each other"),
    ('FIXED', "Fixed Count",
     "Cluster sampled face colors into an exact number of groups (k-means)"),
]

QUANT_STEP = 0.1

MAP_SPEC = [
    ("color",     "BaseColor", "Base Color", False, 400),
    ("roughness", "Roughness", "Roughness",  True, 120),
    ("metallic",  "Metallic",  "Metallic",   True, -160),
]

def make_seed(length=6):
    """Random lowercase-hex seed to make grid names unique across projects."""
    return "".join(random.choice("0123456789abcdef") for _ in range(length))

JSON_ID_KEY = "mcg_export_id"   # per-object: stable short id embedded in export name

def sanitize_name(name, max_len=40):
    """Make a Roblox-safe, short, plain name: keep word chars, trim length."""
    out = []
    for ch in name:
        out.append(ch if (ch.isalnum() or ch in "_") else "_")
    s = "".join(out).strip("_") or "Obj"
    return s[:max_len]

def get_export_id(obj):
    """Return a stable short id for this object, creating one if needed."""
    val = obj.get(JSON_ID_KEY)
    if not val:
        val = make_seed()
        obj[JSON_ID_KEY] = val
    return val

def export_key_for(obj):
    """Stable, unique, Roblox-safe key: <sanitized name>_<id> (idempotent)."""
    eid = get_export_id(obj)
    base = sanitize_name(obj.name)
    suffix = "_" + eid
    if base.endswith(suffix):
        return base
    return base + suffix

def linear_to_srgb_channel(c):
    if c < 0.0:
        return 0.0
    if c <= 0.0031308:
        return 12.92 * c
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055

def color_to_rgb255(linear_rgba):
    """Linear RGBA tuple -> [r,g,b] 0-255 sRGB ints (for Roblox Color3.fromRGB)."""
    r, g, b = linear_rgba[0], linear_rgba[1], linear_rgba[2]
    return [
        max(0, min(255, int(round(linear_to_srgb_channel(r) * 255)))),
        max(0, min(255, int(round(linear_to_srgb_channel(g) * 255)))),
        max(0, min(255, int(round(linear_to_srgb_channel(b) * 255)))),
    ]

def quantize(v):
    """Round to the nearest 0.1 (round half up), clamped to 0..1."""
    v = max(0.0, min(1.0, v))
    return math.floor(v / QUANT_STEP + 0.5) * QUANT_STEP

def _principled(mat):
    if mat.use_nodes and mat.node_tree:
        for node in mat.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                return node
    return None

def get_base_color(mat):
    """Base Color (linear RGBA). Fallback to viewport diffuse."""
    bsdf = _principled(mat)
    if bsdf is not None:
        inp = bsdf.inputs.get("Base Color")
        if inp is not None:
            v = inp.default_value
            return (v[0], v[1], v[2], v[3] if len(v) > 3 else 1.0)
    dc = mat.diffuse_color
    return (dc[0], dc[1], dc[2], 1.0)

def get_principled_value(mat, input_name, default):
    bsdf = _principled(mat)
    if bsdf is not None:
        inp = bsdf.inputs.get(input_name)
        if inp is not None:
            try:
                return float(inp.default_value)
            except TypeError:
                return default
    return default

def calculate_grid(n):
    if n <= 1:
        return 1, 1
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return cols, rows

def cell_uv_center(idx, cols, rows):
    col = idx % cols
    row = idx // cols
    u = (col + 0.5) / cols
    v = (rows - 1 - row + 0.5) / rows
    return (u, v)

def uv_to_cell_index(u, v, cols, rows):
    col = max(0, min(int(u * cols), cols - 1))
    row_from_bottom = max(0, min(int(v * rows), rows - 1))
    row = rows - 1 - row_from_bottom
    return row * cols + col

def compute_uv_centers(n):
    cols, rows = calculate_grid(n)
    return [cell_uv_center(i, cols, rows) for i in range(n)], cols, rows

def new_image(name, width, height, is_data):
    img = bpy.data.images.new(name, width=width, height=height, alpha=True)
    img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'
    return img

def paint_grid(img, cells, srgb):
    """Paint a grid of solid colors/values onto an existing image. cells: list of (r,g,b,a)."""
    width, height = img.size
    n = len(cells)
    cols, rows = calculate_grid(n)

    if srgb:
        enc = [(linear_to_srgb_channel(r), linear_to_srgb_channel(g),
                linear_to_srgb_channel(b), a) for (r, g, b, a) in cells]
    else:
        enc = list(cells)

    pixels = [0.0] * (width * height * 4)
    for idx, (r, g, b, a) in enumerate(enc):
        col = idx % cols
        row = idx // cols
        rfb = rows - 1 - row
        x0 = (col * width) // cols
        x1 = ((col + 1) * width) // cols
        y0 = (rfb * height) // rows
        y1 = ((rfb + 1) * height) // rows
        for y in range(y0, y1):
            ro = y * width * 4
            for x in range(x0, x1):
                i = ro + x * 4
                pixels[i] = r
                pixels[i + 1] = g
                pixels[i + 2] = b
                pixels[i + 3] = a

    img.pixels = pixels
    img.update()
    # If this image was previously exported/unpacked (source FILE), the stale PNG on
    # disk could shadow these fresh pixels on reload. Re-internalize it so the freshly
    # painted pixels are authoritative until the next explicit Export.
    if img.source == 'FILE':
        img.source = 'GENERATED'
    img.pack()

def get_grid_images(mat):
    """Return {'color':img, 'roughness':img, 'metallic':img} for image nodes present."""
    result = {}
    if not (mat.use_nodes and mat.node_tree):
        return result
    tex_nodes = [n for n in mat.node_tree.nodes if n.type == 'TEX_IMAGE']
    label_to_key = {spec[1]: spec[0] for spec in MAP_SPEC}
    for n in tex_nodes:
        if n.image is None:
            continue
        key = label_to_key.get(n.label)
        if key:
            result[key] = n.image
    # Legacy grids: a single untagged image node is the color map.
    if "color" not in result:
        for n in tex_nodes:
            if n.image is not None:
                result["color"] = n.image
                break
    return result

def get_material_image(mat):
    return get_grid_images(mat).get("color")

def setup_grid_material(mat, images):
    """Ensure image nodes (by label) feed Base Color / Roughness / Metallic."""
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    bsdf = nodes.get("Principled BSDF") or _principled(mat)
    if bsdf is None:
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')

    # Normalize a legacy single untagged image node into the BaseColor slot.
    tex_nodes = [n for n in nodes if n.type == 'TEX_IMAGE']
    labelled = {n.label for n in tex_nodes if n.label}
    if "BaseColor" not in labelled:
        for n in tex_nodes:
            if not n.label:
                n.label = "BaseColor"
                break

    by_label = {n.label: n for n in nodes if n.type == 'TEX_IMAGE'}
    for key, label, inp_name, is_data, y in MAP_SPEC:
        img = images.get(key)
        if img is None:
            continue
        node = by_label.get(label)
        if node is None:
            node = nodes.new(type='ShaderNodeTexImage')
            node.label = label
            node.location = (-380, y)
            by_label[label] = node
        node.image = img
        node.interpolation = 'Closest'
        inp = bsdf.inputs.get(inp_name)
        if inp is not None:
            links.new(node.outputs['Color'], inp)

def find_shared_material_from(objs):
    for obj in objs:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is not None and MANIFEST_KEY in mat:
                return mat
    return None

def material_has_image_basecolor(mat):
    """True if the material's Base Color is driven by an image texture node."""
    if not (mat.use_nodes and mat.node_tree):
        return False
    bsdf = _principled(mat)
    if bsdf is None:
        return False
    bc = bsdf.inputs.get("Base Color")
    if bc is None or not bc.links:
        return False
    node = bc.links[0].from_node
    return node.type == 'TEX_IMAGE'

def auto_detect_mode(obj):
    """Pick a sensible default mode for an object."""
    if not obj.material_slots or all(s.material is None for s in obj.material_slots):
        return 'NONE'
    # Already a baked grid object -> leave it alone
    if any(s.material is not None and MANIFEST_KEY in s.material for s in obj.material_slots):
        return 'NONE'
    # Otherwise default to atlas (covers both image-textured and solid materials)
    return 'ATLAS'

def _mode_to_index(ident):
    for i, (k, _n, _d) in enumerate(MODE_ITEMS):
        if k == ident:
            return i
    return 0

def get_object_mode(obj):
    """Read the object's mode (auto-detected if never set). Does not write."""
    return obj.mcg_mode

def set_object_mode(obj, mode):
    if mode in VALID_MODES:
        obj.mcg_mode = mode

def read_manifest(mat):
    raw = mat.get(MANIFEST_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    # Backfill missing fields for older manifests.
    for e in data:
        e.setdefault("roughness", 0.5)
        e.setdefault("metallic", 0.0)
        e.setdefault("color", [0.8, 0.8, 0.8, 1.0])
        e.setdefault("image", "")
        e.setdefault("px", None)
    return data

def write_manifest(mat, manifest):
    mat[MANIFEST_KEY] = json.dumps(manifest)

def objects_using_material(mat):
    return [obj for obj in bpy.data.objects
            if obj.type == 'MESH' and any(m == mat for m in obj.data.materials)]

def remove_unused_slots(context, obj):
    """Remove material slots not used by any polygon (like Blender's built-in).
    Returns the number of slots removed. Polygon material_index values are
    remapped correctly by the operator."""
    if not obj.material_slots:
        return 0
    before = len(obj.material_slots)
    view_layer = context.view_layer
    prev_active = view_layer.objects.active
    view_layer.objects.active = obj
    try:
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.material_slot_remove_unused()
    except RuntimeError:
        pass
    finally:
        view_layer.objects.active = prev_active
    return before - len(obj.material_slots)

def cells_for_map(manifest, key):
    """Build the list of (r,g,b,a) cells for a given map key."""
    if key == "color":
        return [tuple(e["color"]) for e in manifest], True
    if key == "roughness":
        return [(quantize(e["roughness"]),) * 3 + (1.0,) for e in manifest], False
    if key == "metallic":
        return [(quantize(e["metallic"]),) * 3 + (1.0,) for e in manifest], False
    return [], True

def get_basecolor_image(mat):
    """Return the image plugged directly into Base Color, or None."""
    bsdf = _principled(mat)
    if bsdf is None:
        return None
    bc = bsdf.inputs.get("Base Color")
    if bc is None or not bc.links:
        return None
    node = bc.links[0].from_node
    if node.type == 'TEX_IMAGE' and node.image is not None:
        return node.image
    return None

def _cell_rect_px(idx, cols, rows, W, H):
    col = idx % cols
    row = idx // cols
    rfb = rows - 1 - row
    x0 = (col * W) // cols
    x1 = ((col + 1) * W) // cols
    y0 = (rfb * H) // rows
    y1 = ((rfb + 1) * H) // rows
    return x0, x1, y0, y1

def composite_atlas(img, fills):
    """Paint the atlas using numpy. fills[idx] is ('img', image) or ('rgba', (r,g,b,a)).
    Image pixels are copied (nearest-neighbor scaled) directly; solid fills are written
    as given (already sRGB-encoded for color, raw for data)."""
    if _np is None:
        # Fallback: solid only (image cells become their rgba fallback if provided)
        cells = []
        for f in fills:
            cells.append(f[1] if f[0] == 'rgba' else (1.0, 1.0, 1.0, 1.0))
        paint_grid(img, cells, srgb=False)
        return

    W, H = img.size
    n = len(fills)
    cols, rows = calculate_grid(n)
    arr = _np.zeros((H, W, 4), dtype=_np.float32)

    for idx, fill in enumerate(fills):
        x0, x1, y0, y1 = _cell_rect_px(idx, cols, rows, W, H)
        tw, th = x1 - x0, y1 - y0
        if tw <= 0 or th <= 0:
            continue
        if fill[0] == 'img' and fill[1] is not None and fill[1].size[0] > 0 and fill[1].size[1] > 0:
            src = fill[1]
            sw, sh = src.size
            buf = _np.empty(sw * sh * 4, dtype=_np.float32)
            src.pixels.foreach_get(buf)
            buf = buf.reshape(sh, sw, 4)
            xi = _np.linspace(0, sw - 1, tw).astype(_np.int64)
            yi = _np.linspace(0, sh - 1, th).astype(_np.int64)
            arr[y0:y1, x0:x1, :] = buf[yi][:, xi, :]
        else:
            rgba = fill[1] if fill[0] == 'rgba' else (1.0, 1.0, 1.0, 1.0)
            arr[y0:y1, x0:x1, :] = rgba

    img.pixels.foreach_set(arr.reshape(-1))
    img.update()
    img.pack()

def export_image_to(img, path):
    """Write an image to a PNG path and switch it to reference that file (unpack)."""
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    img.filepath = path
    img.source = 'FILE'
    if img.packed_file is not None:
        try:
            img.unpack(method='REMOVE')
        except RuntimeError:
            pass

def extract_rect_image(atlas, x, y, w, h, name):
    """Cut an arbitrary pixel rect out of the atlas into a new sRGB image."""
    if _np is None or w < 1 or h < 1:
        return None
    W, H = atlas.size
    x = max(0, min(x, W)); y = max(0, min(y, H))
    w = max(1, min(w, W - x)); h = max(1, min(h, H - y))
    buf = _np.empty(W * H * 4, dtype=_np.float32)
    atlas.pixels.foreach_get(buf)
    buf = buf.reshape(H, W, 4)
    cell = buf[y:y + h, x:x + w, :]
    out = bpy.data.images.new(name, width=w, height=h, alpha=True)
    out.colorspace_settings.name = 'sRGB'
    out.pixels.foreach_set(_np.ascontiguousarray(cell).reshape(-1))
    out.update()
    out.pack()
    return out

def extract_cell_image(atlas, idx, cols, rows, padding_px, name):
    """Cut a cell's pixels (inside padding) out of the atlas into a new sRGB image."""
    if _np is None:
        return None
    W, H = atlas.size
    x0, x1, y0, y1 = _cell_rect_px(idx, cols, rows, W, H)
    x0 += padding_px
    x1 -= padding_px
    y0 += padding_px
    y1 -= padding_px
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    buf = _np.empty(W * H * 4, dtype=_np.float32)
    atlas.pixels.foreach_get(buf)
    buf = buf.reshape(H, W, 4)
    cell = buf[y0:y1, x0:x1, :]
    out = bpy.data.images.new(name, width=x1 - x0, height=y1 - y0, alpha=True)
    out.colorspace_settings.name = 'sRGB'
    out.pixels.foreach_set(_np.ascontiguousarray(cell).reshape(-1))
    out.update()
    out.pack()
    return out

def paint_color_map(img, manifest, padding_px=0):
    """Color atlas: image cells copy their texture, others fill solid sRGB color."""
    fills = []
    for e in manifest:
        src = bpy.data.images.get(e.get("image", "")) if e.get("image") else None
        if src is not None:
            fills.append(('img', src))
        else:
            c = e["color"]
            fills.append(('rgba', (
                linear_to_srgb_channel(c[0]),
                linear_to_srgb_channel(c[1]),
                linear_to_srgb_channel(c[2]),
                c[3] if len(c) > 3 else 1.0,
            )))
    composite_atlas(img, fills)

def paint_data_map(img, manifest, key):
    """Roughness/metallic atlas: solid quantized grayscale per cell."""
    fills = []
    for e in manifest:
        v = quantize(e.get(key, 0.0))
        fills.append(('rgba', (v, v, v, 1.0)))
    composite_atlas(img, fills)

def cell_uv_rect(idx, cols, rows):
    """UV-space (u0, v0, w, h) of a cell."""
    col = idx % cols
    row = idx // cols
    u0 = col / cols
    v0 = (rows - 1 - row) / rows
    return u0, v0, 1.0 / cols, 1.0 / rows

def image_cell_uv(idx, cols, rows, ou, ov, W, H, padding_px):
    """Map an original UV (0-1) into cell idx's inner rect (with px padding inset)."""
    u0, v0, cw, ch = cell_uv_rect(idx, cols, rows)
    iu = (padding_px / W) if W else 0.0
    iv = (padding_px / H) if H else 0.0
    inner_w = max(cw - 2 * iu, 1e-6)
    inner_h = max(ch - 2 * iv, 1e-6)
    fu = ou - math.floor(ou)
    fv = ov - math.floor(ov)
    return (u0 + iu + fu * inner_w, v0 + iv + fv * inner_h)

def _shelf_pack(boxes, S):
    """Shelf-pack (w,h) boxes into an SxS square. Returns [(x,y)...] or None."""
    order = sorted(range(len(boxes)), key=lambda i: boxes[i][1], reverse=True)
    pos = [None] * len(boxes)
    x = y = shelf_h = 0
    for i in order:
        w, h = boxes[i]
        if w > S or h > S:
            return None
        if x + w > S:
            x = 0
            y += shelf_h
            shelf_h = 0
        if y + h > S:
            return None
        pos[i] = (x, y)
        x += w
        shelf_h = max(shelf_h, h)
    return pos

def compute_packing(manifest, tile, swatch, pad, max_size):
    """Pack each material's box. Returns (atlas_size, rects) with rects[i]=[x,y,bw,bh]."""
    bases = [tile if e.get("image") else swatch for e in manifest]
    scale = 1.0
    for _ in range(10):
        boxes = [(max(1, int(round(b * scale))) + 2 * pad,
                  max(1, int(round(b * scale))) + 2 * pad) for b in bases]
        S = 16
        placed = None
        while S <= max_size:
            placed = _shelf_pack(boxes, S)
            if placed is not None:
                break
            S *= 2
        if placed is not None:
            return S, [[placed[i][0], placed[i][1], boxes[i][0], boxes[i][1]]
                       for i in range(len(boxes))]
        scale *= 0.8
    # Last resort: tiny uniform boxes at max_size.
    n = len(bases)
    side = max(1, int((max_size) / max(1, math.ceil(math.sqrt(n)))))
    cols = max(1, max_size // side)
    rects = []
    for i in range(n):
        cx = (i % cols) * side
        cy = (i // cols) * side
        rects.append([cx, cy, side, side])
    return max_size, rects

def assign_packing(manifest, rects, atlas_size):
    for e, r in zip(manifest, rects):
        e["px"] = list(r)

def entry_inner_uv(px, S, pad):
    """Inner UV rect (u0,v0,u1,v1) of a packed box, inset by padding."""
    x, y, bw, bh = px
    return ((x + pad) / S, (y + pad) / S, (x + bw - pad) / S, (y + bh - pad) / S)

def packed_uv(px, S, pad, ou, ov, is_image):
    u0, v0, u1, v1 = entry_inner_uv(px, S, pad)
    if not is_image:
        return ((u0 + u1) * 0.5, (v0 + v1) * 0.5)
    fu = ou - math.floor(ou)
    fv = ov - math.floor(ov)
    return (u0 + fu * (u1 - u0), v0 + fv * (v1 - v0))

def packed_idx_from_uv(u, v, manifest, S):
    pu, pv = u * S, v * S
    for i, e in enumerate(manifest):
        px = e.get("px")
        if not px:
            continue
        x, y, bw, bh = px
        if x <= pu <= x + bw and y <= pv <= y + bh:
            return i
    best, bd = 0, 1e18
    for i, e in enumerate(manifest):
        px = e.get("px")
        if not px:
            continue
        x, y, bw, bh = px
        d = (x + bw * 0.5 - pu) ** 2 + (y + bh * 0.5 - pv) ** 2
        if d < bd:
            bd, best = d, i
    return best

def composite_packed(img, manifest, S, pad, key):
    """Paint a packed atlas map (key: 'color' | 'roughness' | 'metallic')."""
    if _np is None:
        return
    arr = _np.zeros((S, S, 4), dtype=_np.float32)
    for e in manifest:
        px = e.get("px")
        if not px:
            continue
        x, y, bw, bh = px
        x1, y1 = x + bw, y + bh
        if key == "color":
            src = bpy.data.images.get(e.get("image", "")) if e.get("image") else None
            if src is not None and src.size[0] > 0 and src.size[1] > 0:
                sw, sh = src.size
                buf = _np.empty(sw * sh * 4, dtype=_np.float32)
                src.pixels.foreach_get(buf)
                buf = buf.reshape(sh, sw, 4)
                xi = _np.linspace(0, sw - 1, bw).astype(_np.int64)
                yi = _np.linspace(0, sh - 1, bh).astype(_np.int64)
                arr[y:y1, x:x1, :] = buf[yi][:, xi, :]
            else:
                c = e["color"]
                arr[y:y1, x:x1, :] = (
                    linear_to_srgb_channel(c[0]), linear_to_srgb_channel(c[1]),
                    linear_to_srgb_channel(c[2]), c[3] if len(c) > 3 else 1.0)
        else:
            v = quantize(e.get(key, 0.0))
            arr[y:y1, x:x1, :] = (v, v, v, 1.0)
    img.pixels.foreach_set(arr.reshape(-1))
    img.update()
    img.pack()

def old_inner_uv(idx, old_rects, old_S, old_count, pad, ow, oh):
    """Inner UV rect for an index in the OLD layout (packed or legacy grid)."""
    px = old_rects[idx] if idx < len(old_rects) else None
    if px and old_S > 0:
        return entry_inner_uv(px, old_S, pad)
    cols, rows = calculate_grid(old_count)
    u0, v0, cw, ch = cell_uv_rect(idx, cols, rows)
    iu = (pad / ow) if ow else 0.0
    iv = (pad / oh) if oh else 0.0
    return (u0 + iu, v0 + iv, u0 + cw - iu, v0 + ch - iv)

def old_uv_to_index(u, v, old_count, old_rects, old_S, ow, oh):
    """Index for a UV in the OLD layout (packed or legacy grid)."""
    if old_S > 0 and any(r for r in old_rects):
        pu, pv = u * old_S, v * old_S
        for i, px in enumerate(old_rects):
            if not px:
                continue
            x, y, bw, bh = px
            if x <= pu <= x + bw and y <= pv <= y + bh:
                return i
        best, bd = 0, 1e18
        for i, px in enumerate(old_rects):
            if not px:
                continue
            x, y, bw, bh = px
            d = (x + bw * 0.5 - pu) ** 2 + (y + bh * 0.5 - pv) ** 2
            if d < bd:
                bd, best = d, i
        return best
    cols, rows = calculate_grid(old_count)
    return min(uv_to_cell_index(u, v, cols, rows), old_count - 1)

def restore_inner_uv(idx, manifest, mat, atlas):
    pad = int(mat.get("mcg_padding", 0))
    S = int(mat.get("mcg_atlas_size", 0))
    if S > 0 and manifest[idx].get("px"):
        return entry_inner_uv(manifest[idx]["px"], S, pad)
    cols, rows = calculate_grid(len(manifest))
    u0, v0, cw, ch = cell_uv_rect(idx, cols, rows)
    W, H = (atlas.size if atlas else (1, 1))
    iu = (pad / W) if W else 0.0
    iv = (pad / H) if H else 0.0
    return (u0 + iu, v0 + iv, u0 + cw - iu, v0 + ch - iv)

def restore_index_from_uv(u, v, manifest, mat):
    S = int(mat.get("mcg_atlas_size", 0))
    if S > 0 and manifest and manifest[0].get("px"):
        return packed_idx_from_uv(u, v, manifest, S)
    cols, rows = calculate_grid(len(manifest))
    return min(uv_to_cell_index(u, v, cols, rows), len(manifest) - 1)

def extract_entry_image(atlas, idx, manifest, mat, name):
    pad = int(mat.get("mcg_padding", 0))
    S = int(mat.get("mcg_atlas_size", 0))
    if S > 0 and manifest[idx].get("px"):
        x, y, bw, bh = manifest[idx]["px"]
        return extract_rect_image(atlas, x + pad, y + pad, bw - 2 * pad, bh - 2 * pad, name)
    cols, rows = calculate_grid(len(manifest))
    return extract_cell_image(atlas, idx, cols, rows, pad, name)

PRESETS = {
    'HERO': dict(texture_tile_px=512, solid_swatch_px=16, resolution=1024, cell_padding=4,
                 bake_roughness=True, bake_metallic=True),
    'PROP': dict(texture_tile_px=256, solid_swatch_px=16, resolution=1024, cell_padding=2,
                 bake_roughness=True, bake_metallic=True),
    'BACKGROUND': dict(texture_tile_px=128, solid_swatch_px=8, resolution=512, cell_padding=2,
                       bake_roughness=False, bake_metallic=False),
}

def bake_vertex_colors(obj):
    """Write each face's material Base Color into a vertex color (Color Attribute).
    Colors are stored sRGB-encoded so they look right after FBX/glTF export.
    Returns True on success."""
    mesh = obj.data
    if not mesh.polygons:
        return False

    # Per-slot sRGB color (RGBA 0-1), falling back to white where missing.
    slot_colors = []
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            slot_colors.append((1.0, 1.0, 1.0, 1.0))
        else:
            lin = get_base_color(mat)
            slot_colors.append((
                linear_to_srgb_channel(lin[0]),
                linear_to_srgb_channel(lin[1]),
                linear_to_srgb_channel(lin[2]),
                1.0,
            ))
    if not slot_colors:
        slot_colors = [(1.0, 1.0, 1.0, 1.0)]

    # Create/replace a per-corner (face-corner) color attribute named "Col".
    attr_name = "Col"
    ca = mesh.color_attributes
    existing = ca.get(attr_name)
    if existing is not None:
        try:
            ca.remove(existing)
        except RuntimeError:
            pass
    layer = ca.new(name=attr_name, type='BYTE_COLOR', domain='CORNER')
    ca.active_color = layer
    ca.render_color_index = list(ca).index(layer) if layer in list(ca) else 0

    for poly in mesh.polygons:
        idx = poly.material_index
        col = slot_colors[idx] if 0 <= idx < len(slot_colors) else (1.0, 1.0, 1.0, 1.0)
        for li in poly.loop_indices:
            layer.data[li].color = col
    mesh.update()
    return True

def vertex_colors_to_materials(obj, cache):
    """Create/assign materials from an object's active color attribute.
    cache maps a quantized linear color -> material (shared across objects).
    Returns (n_faces_assigned, made_new_count) or None if no color attribute."""
    mesh = obj.data
    ca = mesh.color_attributes.active_color if mesh.color_attributes else None
    if ca is None or not mesh.polygons:
        return None

    corner = (ca.domain == 'CORNER')

    def face_color(poly):
        elem = ca.data[poly.loop_indices[0]] if corner else ca.data[poly.vertices[0]]
        c = elem.color
        return (c[0], c[1], c[2], c[3] if len(c) > 3 else 1.0)

    made = 0
    mesh.materials.clear()
    slot_of = {}
    for poly in mesh.polygons:
        c = face_color(poly)
        key = (round(c[0], 3), round(c[1], 3), round(c[2], 3))
        mat = cache.get(key)
        if mat is None:
            r, g, b = color_to_rgb255(c)
            mat = bpy.data.materials.new(name=f"VC_{r:02x}{g:02x}{b:02x}")
            mat.use_nodes = True
            bsdf = _principled(mat)
            if bsdf is not None:
                bsdf.inputs["Base Color"].default_value = c
            mat.diffuse_color = c
            cache[key] = mat
            made += 1
        if key not in slot_of:
            slot_of[key] = len(mesh.materials)
            mesh.materials.append(mat)
        poly.material_index = slot_of[key]
    return len(mesh.polygons), made

def material_matches(mat, rgba, rough, metal, tol=1e-4):
    bsdf = _principled(mat)
    if bsdf is None:
        return False
    bc = bsdf.inputs.get("Base Color")
    if bc is None:
        return False
    v = bc.default_value
    if any(abs(v[i] - rgba[i]) > tol for i in range(3)):
        return False
    ri = bsdf.inputs.get("Roughness")
    if ri is not None and abs(float(ri.default_value) - rough) > tol:
        return False
    mi = bsdf.inputs.get("Metallic")
    if mi is not None and abs(float(mi.default_value) - metal) > tol:
        return False
    return True

def build_or_reuse_material(entry):
    """Get/create a material for a manifest entry without overwriting a different same-name one."""
    name = entry["name"]
    col = entry["color"]
    rgba = (col[0], col[1], col[2], col[3] if len(col) > 3 else 1.0)
    rough = float(entry.get("roughness", 0.5))
    metal = float(entry.get("metallic", 0.0))

    existing = bpy.data.materials.get(name)
    if existing is not None and material_matches(existing, rgba, rough, metal):
        return existing

    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = _principled(mat)
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = rgba
        if bsdf.inputs.get("Roughness") is not None:
            bsdf.inputs["Roughness"].default_value = rough
        if bsdf.inputs.get("Metallic") is not None:
            bsdf.inputs["Metallic"].default_value = metal
    mat.diffuse_color = rgba
    return mat

def _srgb_to_linear_np(c):
    """Vectorized sRGB -> linear for a numpy array of 0-1 values."""
    return _np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)

def get_image_buffer(cache, img):
    """Return (buf, W, H) for an image, linear RGBA float32, cached by image name.
    The RGB channels are converted sRGB->linear when the image is an sRGB texture,
    so sampled colors match Blender's linear Base Color values (no washed-out look)."""
    key = img.name
    if key in cache:
        return cache[key]
    W, H = img.size
    if _np is None or W <= 0 or H <= 0:
        cache[key] = None
        return None
    buf = _np.empty(W * H * 4, dtype=_np.float32)
    img.pixels.foreach_get(buf)
    buf = buf.reshape(H, W, 4)
    # image.pixels holds raw (sRGB-encoded) values for an sRGB texture; linearize
    # RGB so downstream colors live in linear space like the rest of the addon.
    is_srgb = getattr(img.colorspace_settings, "name", "sRGB") not in ('Non-Color', 'Raw', 'Linear')
    if is_srgb:
        buf = buf.copy()
        buf[..., :3] = _srgb_to_linear_np(buf[..., :3])
    cache[key] = (buf, W, H)
    return cache[key]

def sample_color_uv(buf_info, u, v):
    """Nearest-neighbor sample of a cached image buffer at UV (wraps 0-1)."""
    buf, W, H = buf_info
    uu = u - math.floor(u)
    vv = v - math.floor(v)
    x = min(int(uu * W), W - 1)
    y = min(int(vv * H), H - 1)
    return buf[y, x]

def barycentric_grid_points(subdiv):
    """Evenly spaced barycentric (a,b,c) samples inside a triangle, biased slightly
    off the exact centroid grid so edge cases don't land exactly on a boundary."""
    n = max(1, int(subdiv))
    pts = []
    for i in range(n):
        for j in range(n - i):
            a = (i + 1.0 / 3.0) / n
            b = (j + 1.0 / 3.0) / n
            c = 1.0 - a - b
            if c >= -1e-6:
                pts.append((a, b, max(c, 0.0)))
    return pts or [(1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)]

def face_uv_samples(uv_layer, poly, subdiv):
    """Multiple UV sample points spread across a face (triangle-fanned for n-gons)."""
    loop_idx = list(poly.loop_indices)
    uvs = [tuple(uv_layer.data[li].uv) for li in loop_idx]
    if len(uvs) < 3:
        return uvs
    bary = barycentric_grid_points(subdiv)
    pts = []
    for t in range(1, len(uvs) - 1):
        v0, v1, v2 = uvs[0], uvs[t], uvs[t + 1]
        for (a, b, c) in bary:
            pts.append((a * v0[0] + b * v1[0] + c * v2[0],
                        a * v0[1] + b * v1[1] + c * v2[1]))
    return pts

def face_representative_color(buf_info, uv_layer, poly, subdiv, aggregate):
    """Sample multiple points inside a face and return one representative linear RGBA."""
    pts = face_uv_samples(uv_layer, poly, subdiv)
    if not pts:
        return None
    samples = _np.array([sample_color_uv(buf_info, u, v) for (u, v) in pts],
                        dtype=_np.float32)
    if aggregate == 'MEAN':
        return samples.mean(axis=0)
    return _np.median(samples, axis=0)

def kmeans_colors(colors, k, iters=25, seed=0):
    """Simple k-means++ over an (N,3) array. Returns (labels, centers)."""
    n = len(colors)
    k = max(1, min(k, n))
    rng = _np.random.default_rng(seed)
    colors = _np.asarray(colors, dtype=_np.float64)
    centers = _np.empty((k, colors.shape[1]), dtype=_np.float64)
    centers[0] = colors[rng.integers(n)]
    dist2 = ((colors - centers[0]) ** 2).sum(axis=1)
    for i in range(1, k):
        total = dist2.sum()
        idx = rng.choice(n, p=(dist2 / total)) if total > 0 else rng.integers(n)
        centers[i] = colors[idx]
        dist2 = _np.minimum(dist2, ((colors - centers[i]) ** 2).sum(axis=1))
    labels = _np.full(n, -1, dtype=_np.int64)
    for _ in range(iters):
        d = ((colors[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        new_labels = d.argmin(axis=1)
        changed = not _np.array_equal(new_labels, labels)
        labels = new_labels
        if not changed:
            break
        for j in range(k):
            mask = labels == j
            if mask.any():
                centers[j] = colors[mask].mean(axis=0)
    return labels, centers

def threshold_cluster_colors(colors, threshold):
    """Greedy leader clustering: assign each color to the nearest existing cluster
    center if within threshold, else start a new cluster. Returns (labels, centers)."""
    colors = _np.asarray(colors, dtype=_np.float64)
    n = len(colors)
    centers, sums, counts = [], [], []
    labels = _np.empty(n, dtype=_np.int64)
    for i in range(n):
        c = colors[i]
        assigned = -1
        if centers:
            carr = _np.array(centers)
            d = _np.sqrt(((carr - c) ** 2).sum(axis=1))
            j = int(_np.argmin(d))
            if d[j] <= threshold:
                assigned = j
        if assigned == -1:
            centers.append(c.copy())
            sums.append(c.copy())
            counts.append(1)
            labels[i] = len(centers) - 1
        else:
            counts[assigned] += 1
            sums[assigned] = sums[assigned] + c
            centers[assigned] = sums[assigned] / counts[assigned]
            labels[i] = assigned
    return labels, _np.array(centers) if centers else _np.zeros((0, 3))

def get_or_create_color_material(prefix, rgba, rough, metal, cache):
    """Get/create a solid-color material named by its sRGB hex, reusing an existing
    same-named material if its values already match (keeps re-runs idempotent)."""
    r, g, b = color_to_rgb255(rgba)
    name = f"{prefix}_{r:02x}{g:02x}{b:02x}"
    if name in cache:
        return cache[name]
    existing = bpy.data.materials.get(name)
    if existing is not None and material_matches(existing, rgba, rough, metal, tol=0.01):
        cache[name] = existing
        return existing
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = _principled(mat)
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = rgba
        if bsdf.inputs.get("Roughness") is not None:
            bsdf.inputs["Roughness"].default_value = rough
        if bsdf.inputs.get("Metallic") is not None:
            bsdf.inputs["Metallic"].default_value = metal
    mat.diffuse_color = rgba
    cache[name] = mat
    return mat

def restore_object_materials(obj):
    """Rebuild per-face individual materials on a grid-baked object from its manifest."""
    shared_mat = None
    for slot in obj.material_slots:
        if slot.material is not None and MANIFEST_KEY in slot.material:
            shared_mat = slot.material
            break
    if shared_mat is None:
        return False
    manifest = read_manifest(shared_mat)
    if not manifest:
        return False

    mesh = obj.data
    cols, rows = calculate_grid(len(manifest))
    uv = mesh.uv_layers.get(UV_LAYER_NAME) or mesh.uv_layers.active
    if uv is None:
        return False

    face_cell, used = [], set()
    for poly in mesh.polygons:
        u, v = uv.data[poly.loop_indices[0]].uv
        idx = min(uv_to_cell_index(u, v, cols, rows), len(manifest) - 1)
        face_cell.append(idx)
        used.add(idx)

    used_sorted = sorted(used)
    cell_to_slot = {c: i for i, c in enumerate(used_sorted)}
    mesh.materials.clear()
    cache = {}
    for c in used_sorted:
        cache[c] = build_or_reuse_material(manifest[c])
        mesh.materials.append(cache[c])
    for poly, c in zip(mesh.polygons, face_cell):
        poly.material_index = cell_to_slot[c]
    return True

def used_material_count(obj):
    """Number of distinct materials actually used by the object's faces."""
    names = set()
    for poly in obj.data.polygons:
        if 0 <= poly.material_index < len(obj.material_slots):
            mat = obj.material_slots[poly.material_index].material
            if mat is not None:
                names.add(mat.name)
    return len(names)

def is_grid_object(obj):
    return any(s.material is not None and MANIFEST_KEY in s.material
               for s in obj.material_slots)

def separate_object_by_material(context, obj):
    """Split obj into one object per material. Returns list of resulting objects."""
    view_layer = context.view_layer
    for o in list(context.selected_objects):
        o.select_set(False)
    obj.select_set(True)
    view_layer.objects.active = obj
    if obj.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

    before = set(context.scene.objects)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    try:
        bpy.ops.mesh.separate(type='MATERIAL')
    except RuntimeError:
        pass
    bpy.ops.object.mode_set(mode='OBJECT')
    new_objs = [o for o in context.scene.objects if o not in before]
    return [obj] + new_objs

def split_and_rename_by_material(context, obj):
    """Separate by material and rename each piece <origname>_<matname>. Returns pieces."""
    base = sanitize_name(obj.name, 20)
    pieces = separate_object_by_material(context, obj)
    for p in pieces:
        remove_unused_slots(context, p)
        mat = p.material_slots[0].material if p.material_slots else None
        if mat is not None:
            p.name = f"{base}_{sanitize_name(mat.name, 16)}"
        # new id will be generated lazily via export_key_for
    return pieces

def analyze_json_targets(targets):
    """Return list of (name, kind, count) for the confirm dialog."""
    plan = []
    for obj in targets:
        if is_grid_object(obj):
            plan.append((obj.name, 'grid', used_material_count(obj)))
        else:
            n = used_material_count(obj)
            plan.append((obj.name, 'multi' if n > 1 else 'single', max(n, 1)))
    return plan

def build_color_json(objs):
    """Build {export_key: [r,g,b]} for the given objects (uses first material's base color)."""
    data = {}
    for obj in objs:
        mat = None
        for slot in obj.material_slots:
            if slot.material is not None and MANIFEST_KEY not in slot.material:
                mat = slot.material
                break
        if mat is None:
            continue
        data[export_key_for(obj)] = color_to_rgb255(get_base_color(mat))
    return data

def selected_modes(context):
    """Set of modes among selected mesh objects."""
    return {get_object_mode(o) for o in context.selected_objects if o.type == 'MESH'}

def _tri_count(mesh):
    return sum(len(p.vertices) - 2 for p in mesh.polygons)

def _ngon_count(mesh):
    return sum(1 for p in mesh.polygons if len(p.vertices) > 4)

def _empty_slot_count(obj):
    return sum(1 for s in obj.material_slots if s.material is None)

def _uv_out_of_range(mesh):
    uv = mesh.uv_layers.active
    if uv is None:
        return None  # no UV
    for d in uv.data:
        u, v = d.uv
        if u < -0.001 or u > 1.001 or v < -0.001 or v > 1.001:
            return True
    return False

def _current_version():
    return bl_info["version"]

def _parse_version(tag):
    """'v1.10.0' or '1.10.0' -> (1, 10, 0). Returns None if unparseable."""
    if not tag:
        return None
    s = tag.strip().lstrip("vV")
    parts = s.split(".")
    nums = []
    for p in parts:
        digits = "".join(ch for ch in p if ch.isdigit())
        if digits == "":
            break
        nums.append(int(digits))
    return tuple(nums) if nums else None

def _http_get(url, want_json=False, timeout=15):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={
        "User-Agent": "material-color-grid-updater",
        "Accept": "application/vnd.github+json" if want_json else "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8")) if want_json else data

def _fetch_latest_release():
    """Return dict {version, tag, asset_url, html_url} for the latest release, or raises."""
    info = _http_get(UPDATE_API_URL, want_json=True)
    tag = info.get("tag_name", "")
    html_url = info.get("html_url", "")
    asset_url = None
    for asset in info.get("assets", []):
        if asset.get("name") == UPDATE_ASSET_NAME:
            asset_url = asset.get("browser_download_url")
            break
    return {
        "version": _parse_version(tag),
        "tag": tag,
        "asset_url": asset_url,
        "html_url": html_url,
    }

def _addon_file_path():
    """Absolute path to THIS addon's .py file on disk."""
    return os.path.abspath(__file__)

def default_export_dir():
    """Cross-platform default: ~/Documents/MaterialColorGrid, falling back to
    ~/MaterialColorGrid if the Documents folder does not exist (e.g. minimal
    Linux setups)."""
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    base = docs if os.path.isdir(docs) else home
    return os.path.join(base, "MaterialColorGrid")

def resolve_export_dir(context):
    """User-set export folder, else the default next to the add-on. Ensures it exists."""
    path = ""
    try:
        prefs = context.preferences.addons[PKG_NAME].preferences
        path = bpy.path.abspath(prefs.export_dir) if prefs.export_dir else ""
    except (KeyError, AttributeError):
        path = ""
    if not path:
        path = default_export_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return ""
    return path

