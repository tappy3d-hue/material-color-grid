bl_info = {
    "name": "Material Color Grid Texture",
    "author": "Claude",
    "version": (1, 25, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N) > Color Grid tab",
    "description": "Pool base colors (and optionally roughness/metallic) from selected "
                   "objects into shared grid textures and one material (meshes stay "
                   "separate). Re-running adds new values while preserving baked ones via "
                   "a stored manifest. Can restore per-value materials, compact unused "
                   "cells, export PNGs, and unpack for FBX/Roblox. Can also detect colors "
                   "baked into an image texture (e.g. Tripo exports) and split faces into "
                   "solid-color materials so they atlas as tiny swatches instead of full "
                   "image tiles.",
    "category": "Material",
}

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

# GitHub repo used for the in-Blender updater.
UPDATE_REPO = "tappy3d-hue/material-color-grid"
UPDATE_ASSET_NAME = "material_color_grid.py"
UPDATE_API_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"

DEFAULT_GROUP_NAME = "ColorGrid"
MANIFEST_KEY = "mcg_manifest"
UV_LAYER_NAME = "ColorGridUV"

# Per-object processing modes
MODE_ITEMS = [
    ('ATLAS', "Atlas", "Bake this object's material colors into the shared grid texture"),
    ('VERTEX', "Vertex Color", "Bake base colors into vertex colors (implemented later)"),
    ('JSON', "JSON", "Export colors as data for dynamic coloring in Roblox (implemented later)"),
    ('NONE', "None", "Leave this object untouched"),
]
VALID_MODES = {m[0] for m in MODE_ITEMS}

# Grouping mode for "Detect Colors from Texture".
COLOR_GROUP_MODE_ITEMS = [
    ('AUTO', "Auto (Threshold)",
     "Merge sampled face colors that are within a distance threshold of each other"),
    ('FIXED', "Fixed Count",
     "Cluster sampled face colors into an exact number of groups (k-means)"),
]

# Roughness/Metallic are quantized to this step (0.1) by rounding (four-go-go-nyu).
QUANT_STEP = 0.1

# Map keys -> (node label, BSDF input name, is_data, node y location)
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


# ----------------------------------------------------------------------------
# Color / value helpers
# ----------------------------------------------------------------------------

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


# ----------------------------------------------------------------------------
# Grid geometry helpers
# ----------------------------------------------------------------------------

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


# ----------------------------------------------------------------------------
# Image / material helpers
# ----------------------------------------------------------------------------

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


# EnumProperty get/set for Object.mcg_mode. get() must never write data (it can
# run during UI draw), so unset objects fall back to auto-detection read-only.
def _mode_get(self):
    raw = self.get("mcg_mode_raw", -1)
    if not isinstance(raw, int) or raw < 0 or raw >= len(MODE_ITEMS):
        return _mode_to_index(auto_detect_mode(self))
    return raw


def _mode_set(self, value):
    self["mcg_mode_raw"] = int(value)


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


# ----------------------------------------------------------------------------
# Packed atlas (non-uniform): textures get full tiles, solids get small swatches
# ----------------------------------------------------------------------------

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


# ----------------------------------------------------------------------------
# Panel settings
# ----------------------------------------------------------------------------

class MCGSettings(bpy.types.PropertyGroup):
    grid_name: bpy.props.StringProperty(
        name="Grid Name",
        description="Name for a new grid (texture + material), and the target name when renaming",
        default=DEFAULT_GROUP_NAME,
    )
    resolution: bpy.props.IntProperty(
        name="Atlas Max", description="Maximum atlas size (square, power of two)",
        default=1024, min=64, max=8192,
    )
    minimal_resolution: bpy.props.BoolProperty(
        name="Minimal (1px solids)",
        description="Pack solid-color materials as 1px swatches for the smallest possible atlas",
        default=False)
    cell_pixels: bpy.props.IntProperty(name="Cell Pixels", default=8, min=1, max=256)
    texture_tile_px: bpy.props.IntProperty(
        name="Texture Tile", description="Pixel size each textured material is packed at",
        default=256, min=16, max=2048,
    )
    solid_swatch_px: bpy.props.IntProperty(
        name="Solid Swatch", description="Pixel size of solid-color swatches in the atlas",
        default=16, min=1, max=256,
    )
    cell_padding: bpy.props.IntProperty(
        name="Cell Padding",
        description="Inset (pixels) around each packed cell to prevent edge bleeding",
        default=2, min=0, max=64,
    )
    bake_roughness: bpy.props.BoolProperty(
        name="Roughness Map",
        description="Also bake a grayscale roughness texture (values rounded to 0.1)",
        default=True,
    )
    bake_metallic: bpy.props.BoolProperty(
        name="Metallic Map",
        description="Also bake a grayscale metallic texture (values rounded to 0.1)",
        default=True,
    )
    create_vertex_groups: bpy.props.BoolProperty(name="Vertex Groups", default=True)
    remap_uvs: bpy.props.BoolProperty(name="Remap UVs", default=True)
    replace_materials: bpy.props.BoolProperty(name="Replace Slots", default=True)
    sync_all_users: bpy.props.BoolProperty(name="Update All Users", default=True)
    remove_unused_slots: bpy.props.BoolProperty(name="Remove Unused Slots", default=True)
    auto_export_after_bake: bpy.props.BoolProperty(
        name="Auto Export + Unpack After Bake",
        description="After baking, save the atlas as PNG next to the .blend and reference it "
                    "(unpack), so FBX export embeds the texture without manual steps",
        default=False,
    )
    tri_warn_threshold: bpy.props.IntProperty(
        name="Max Triangles", description="Warn if a mesh exceeds this triangle count",
        default=20000, min=100, max=2000000,
    )
    tex_warn_resolution: bpy.props.IntProperty(
        name="Max Texture", description="Warn if a texture is larger than this (px)",
        default=1024, min=16, max=8192,
    )
    check_report: bpy.props.StringProperty(default="", options={'HIDDEN'})

    # ---- Detect Colors from Texture ----
    detect_group_mode: bpy.props.EnumProperty(
        name="Grouping", items=COLOR_GROUP_MODE_ITEMS, default='AUTO',
    )
    detect_threshold: bpy.props.FloatProperty(
        name="Color Threshold",
        description="Max linear RGB distance within one group (Auto mode). Smaller = more, "
                    "purer groups; larger = fewer, broader groups",
        default=0.04, min=0.001, max=0.5,
    )
    detect_color_count: bpy.props.IntProperty(
        name="Color Count",
        description="Number of color groups to create (Fixed Count mode)",
        default=8, min=2, max=64,
    )
    detect_sample_density: bpy.props.IntProperty(
        name="Samples per Face",
        description="Sampling grid density per triangle (higher = more sample points per "
                    "face, slower but more accurate for large/varied faces)",
        default=3, min=1, max=6,
    )
    detect_aggregate: bpy.props.EnumProperty(
        name="Representative Color",
        items=[
            ('MEDIAN', "Median", "Robust to seam bleeding and antialiasing outliers"),
            ('MEAN', "Mean", "Simple average of the sampled points"),
        ],
        default='MEDIAN',
    )
    detect_merge_across_objects: bpy.props.BoolProperty(
        name="Merge Across Selected Objects",
        description="Cluster colors across all selected objects together, so the same "
                    "color on different objects ends up sharing one material",
        default=True,
    )
    detect_name_prefix: bpy.props.StringProperty(
        name="Prefix", description="Name prefix for the solid-color materials that get created",
        default="DetectedColor",
    )
    detect_cleanup_unused_slots: bpy.props.BoolProperty(
        name="Remove Unused Slots After",
        description="Delete the original image-textured material slot(s) once no face "
                    "uses them anymore",
        default=True,
    )


# ----------------------------------------------------------------------------
# Bake / Update operator
# ----------------------------------------------------------------------------

class OBJECT_OT_material_color_grid(bpy.types.Operator):
    """Bake selected objects' material values into shared grid textures"""
    bl_idname = "object.material_color_grid"
    bl_label = "Material Color Grid Texture"
    bl_options = {'REGISTER', 'UNDO'}

    resolution: bpy.props.IntProperty(name="Atlas Max", default=1024, min=16, max=8192)
    group_name: bpy.props.StringProperty(name="Group Name", default=DEFAULT_GROUP_NAME)
    minimal_resolution: bpy.props.BoolProperty(name="Minimal Resolution", default=False)
    cell_pixels: bpy.props.IntProperty(name="Cell Pixels", default=8, min=1, max=256)
    texture_tile_px: bpy.props.IntProperty(
        name="Texture Tile", description="Pixel size each textured material is packed at",
        default=256, min=16, max=2048)
    solid_swatch_px: bpy.props.IntProperty(
        name="Solid Swatch", description="Pixel size of solid-color swatches in the atlas",
        default=16, min=1, max=256)
    cell_padding: bpy.props.IntProperty(
        name="Cell Padding",
        description="Inset (pixels) around each packed cell to prevent edge bleeding",
        default=2, min=0, max=64,
    )
    bake_roughness: bpy.props.BoolProperty(name="Roughness Map", default=True)
    bake_metallic: bpy.props.BoolProperty(name="Metallic Map", default=True)
    create_vertex_groups: bpy.props.BoolProperty(name="Create Vertex Groups", default=True)
    remap_uvs: bpy.props.BoolProperty(name="Remap UVs to Color Cells", default=True)
    replace_materials: bpy.props.BoolProperty(name="Replace Material Slots", default=True)
    sync_all_users: bpy.props.BoolProperty(name="Update All Objects Using Texture", default=True)
    remove_unused_slots: bpy.props.BoolProperty(
        name="Remove Unused Slots Before Bake",
        description="Delete material slots not used by any face on the object before baking "
                    "(same as Blender's Remove Unused Slots), so unused colors don't take grid cells",
        default=True,
    )
    auto_export: bpy.props.BoolProperty(
        name="Auto Export + Unpack After Bake",
        description="Save the atlas PNG next to the .blend and reference it after baking",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        all_sel = [o for o in context.selected_objects if o.type == 'MESH']
        if not all_sel:
            self.report({'ERROR'}, "No mesh objects selected")
            return {'CANCELLED'}

        # Split selection by per-object mode. Only ATLAS objects are baked here;
        # VERTEX / JSON are handled by their own operators (implemented later).
        sel = [o for o in all_sel if get_object_mode(o) == 'ATLAS']
        other_modes = sorted({get_object_mode(o) for o in all_sel
                              if get_object_mode(o) not in ('ATLAS', 'NONE')})

        if not sel:
            msg = "No objects set to Atlas mode."
            if other_modes:
                msg += " (Vertex Color / JSON export will come in a later step.)"
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}

        # Remove material slots not used by any face, before collecting colors.
        if self.remove_unused_slots:
            for obj in sel:
                remove_unused_slots(context, obj)

        shared_mat = find_shared_material_from(sel)
        manifest = read_manifest(shared_mat) if shared_mat else []
        old_count = len(manifest)
        old_cols, old_rows = calculate_grid(old_count) if old_count else (1, 1)
        name_to_index = {e["name"]: i for i, e in enumerate(manifest)}

        existing_objs, source_objs = [], []
        for obj in sel:
            uses_shared = shared_mat is not None and any(
                slot.material == shared_mat for slot in obj.material_slots)
            (existing_objs if uses_shared else source_objs).append(obj)

        # Merge new materials from source objects
        for obj in source_objs:
            for slot in obj.material_slots:
                mat = slot.material
                if mat is None or mat == shared_mat:
                    continue
                src_img = get_basecolor_image(mat)
                entry = {
                    "name": mat.name,
                    "color": list(get_base_color(mat)),
                    "roughness": get_principled_value(mat, "Roughness", 0.5),
                    "metallic": get_principled_value(mat, "Metallic", 0.0),
                    "image": src_img.name if src_img is not None else "",
                }
                if mat.name in name_to_index:
                    manifest[name_to_index[mat.name]] = entry
                else:
                    name_to_index[mat.name] = len(manifest)
                    manifest.append(entry)

        if not manifest:
            self.report({'ERROR'}, "No valid materials found on selected objects")
            return {'CANCELLED'}

        # ---- Packing: snapshot old layout, then pack all materials ----
        old_S = int(shared_mat.get("mcg_atlas_size", 0)) if shared_mat else 0
        old_rects = [manifest[i].get("px") for i in range(old_count)]
        old_atlas = get_material_image(shared_mat) if shared_mat else None
        old_aw, old_ah = (old_atlas.size if old_atlas else (0, 0))
        pad = self.cell_padding
        eff_swatch = 1 if self.minimal_resolution else self.solid_swatch_px

        S, rects = compute_packing(manifest, self.texture_tile_px,
                                   eff_swatch, pad, self.resolution)
        assign_packing(manifest, rects, S)

        existing_imgs = get_grid_images(shared_mat) if shared_mat else {}

        want = {"color": True,
                "roughness": self.bake_roughness or ("roughness" in existing_imgs),
                "metallic": self.bake_metallic or ("metallic" in existing_imgs)}

        if shared_mat is None:
            raw = (self.group_name or "").strip()
            seed = make_seed()
            base = (raw + "_" + seed) if raw else seed
            shared_mat = bpy.data.materials.new(name=base + "_Mat")
        else:
            base = None

        suffix = {"color": "", "roughness": "_Rough", "metallic": "_Metal"}
        images = {}
        for key, _label, _inp, is_data, _y in MAP_SPEC:
            if not want.get(key):
                continue
            img = existing_imgs.get(key)
            if img is None:
                nm = (base or shared_mat.name.replace("_Mat", "")) + suffix[key]
                if not nm:
                    nm = DEFAULT_GROUP_NAME + suffix[key]
                img = new_image(nm, S, S, is_data)
            elif img.size[0] != S or img.size[1] != S:
                img.scale(S, S)
            img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'
            composite_packed(img, manifest, S, pad, key)
            images[key] = img

        setup_grid_material(shared_mat, images)
        write_manifest(shared_mat, manifest)
        shared_mat["mcg_padding"] = pad
        shared_mat["mcg_atlas_size"] = S

        # Remap existing objects: preserve each loop's relative position within its
        # old cell, mapping into the new packed rect (same material index).
        if old_count:
            targets = (objects_using_material(shared_mat)
                       if self.sync_all_users else existing_objs)
            done = set()
            for obj in targets:
                mesh = obj.data
                if mesh.name in done:
                    continue
                done.add(mesh.name)
                uv = mesh.uv_layers.get(UV_LAYER_NAME) or mesh.uv_layers.active
                if uv is None:
                    continue
                ow, oh = (old_aw or S, old_ah or S)
                for poly in mesh.polygons:
                    for li in poly.loop_indices:
                        u, v = uv.data[li].uv
                        idx = old_uv_to_index(u, v, old_count, old_rects, old_S,
                                              ow, oh)
                        idx = min(idx, len(manifest) - 1)
                        ou0, ov0, ou1, ov1 = old_inner_uv(idx, old_rects, old_S,
                                                          old_count, pad, ow, oh)
                        lu = min(max((u - ou0) / (ou1 - ou0), 0.0), 1.0) if ou1 > ou0 else 0.5
                        lv = min(max((v - ov0) / (ov1 - ov0), 0.0), 1.0) if ov1 > ov0 else 0.5
                        nu0, nv0, nu1, nv1 = entry_inner_uv(manifest[idx]["px"], S, pad)
                        uv.data[li].uv = (nu0 + lu * (nu1 - nu0),
                                          nv0 + lv * (nv1 - nv0))

        # Apply to source objects
        processed = set()
        for obj in source_objs:
            mesh = obj.data
            slot_to_idx = {}
            for slot_idx, slot in enumerate(obj.material_slots):
                mat = slot.material
                if mat is None or mat == shared_mat:
                    continue
                slot_to_idx[slot_idx] = name_to_index[mat.name]

            if self.create_vertex_groups:
                mat_to_verts = {}
                for poly in mesh.polygons:
                    if poly.material_index in slot_to_idx:
                        nm = manifest[slot_to_idx[poly.material_index]]["name"]
                        mat_to_verts.setdefault(nm, set()).update(poly.vertices)
                for nm, verts in mat_to_verts.items():
                    ex = obj.vertex_groups.get(nm)
                    if ex is not None:
                        obj.vertex_groups.remove(ex)
                    obj.vertex_groups.new(name=nm).add(list(verts), 1.0, 'REPLACE')

            if mesh.name in processed:
                continue
            processed.add(mesh.name)

            if self.remap_uvs:
                src_layer = mesh.uv_layers.active
                orig = None
                if src_layer is not None:
                    orig = [tuple(src_layer.data[i].uv) for i in range(len(mesh.loops))]
                while mesh.uv_layers:
                    mesh.uv_layers.remove(mesh.uv_layers[0])
                uvl = mesh.uv_layers.new(name=UV_LAYER_NAME)
                mesh.uv_layers.active = uvl
                uvl.active_render = True
                for poly in mesh.polygons:
                    idx = slot_to_idx.get(poly.material_index)
                    if idx is None:
                        for li in poly.loop_indices:
                            uvl.data[li].uv = (0.5, 0.5)
                        continue
                    is_img = bool(manifest[idx].get("image"))
                    px = manifest[idx]["px"]
                    for li in poly.loop_indices:
                        ou, ov = orig[li] if orig else (0.0, 0.0)
                        uvl.data[li].uv = packed_uv(px, S, pad, ou, ov, is_img)

            if self.replace_materials:
                mesh.materials.clear()
                mesh.materials.append(shared_mat)
                for poly in mesh.polygons:
                    poly.material_index = 0
            elif shared_mat.name not in [m.name for m in mesh.materials if m]:
                mesh.materials.append(shared_mat)

        # Optional: auto-export atlas PNGs next to the .blend and reference them.
        exported_note = ""
        if self.auto_export:
            blend_dir = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else ""
            if not blend_dir:
                self.report({'WARNING'}, "Save the .blend first to auto-export textures")
            else:
                n = 0
                for key in ("color", "roughness", "metallic"):
                    img = images.get(key)
                    if img is None:
                        continue
                    try:
                        export_image_to(img, os.path.join(blend_dir, img.name + ".png"))
                        n += 1
                    except (RuntimeError, OSError):
                        pass
                exported_note = f"; exported {n} PNG(s)"

        mode = "Updated" if old_count else "Created"
        maps = "+".join(k for k in ("color", "roughness", "metallic") if k in images)
        extra = f"; skipped modes: {', '.join(other_modes)}" if other_modes else ""
        self.report({'INFO'},
                    f"{mode} grid {new_cols}x{new_rows} ({len(manifest)} cells, "
                    f"+{len(manifest) - old_count} new) maps: {maps}{extra}{exported_note}")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Restore operator
# ----------------------------------------------------------------------------

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


class OBJECT_OT_restore_materials_from_grid(bpy.types.Operator):
    """Rebuild per-value materials from the grid (reverse of baking)"""
    bl_idname = "object.restore_materials_from_grid"
    bl_label = "Restore Materials From Grid"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    @staticmethod
    def _matches(mat, rgba, rough, metal, tol=1e-4):
        """True if an existing material already has these values (safe to reuse)."""
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

    def _build_material(self, entry, image=None):
        name = entry["name"]
        col = entry["color"]
        rgba = (col[0], col[1], col[2], col[3] if len(col) > 3 else 1.0)
        rough = float(entry.get("roughness", 0.5))
        metal = float(entry.get("metallic", 0.0))

        # Solid entries can reuse an existing matching material (no overwrite).
        if image is None:
            existing = bpy.data.materials.get(name)
            if existing is not None and self._matches(existing, rgba, rough, metal):
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
            if image is not None:
                tex = mat.node_tree.nodes.new(type='ShaderNodeTexImage')
                tex.image = image
                tex.location = (-340, 300)
                mat.node_tree.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
        mat.diffuse_color = rgba
        return mat

    def execute(self, context):
        sel = [o for o in context.selected_objects if o.type == 'MESH']
        shared_mat = find_shared_material_from(sel)
        if shared_mat is None:
            self.report({'ERROR'}, "Selected objects don't use a grid material with a manifest")
            return {'CANCELLED'}
        manifest = read_manifest(shared_mat)
        if not manifest:
            self.report({'ERROR'}, "No manifest stored on the grid material")
            return {'CANCELLED'}

        atlas = get_material_image(shared_mat)

        # Resolve a texture image per textured entry: re-link original if it still
        # exists (lossless), otherwise extract the cell pixels from the atlas.
        cell_images = {}
        relinked = extracted = 0
        for idx, e in enumerate(manifest):
            img_name = e.get("image", "")
            if not img_name:
                continue
            orig = bpy.data.images.get(img_name)
            if orig is not None:
                cell_images[idx] = orig
                relinked += 1
            elif atlas is not None:
                out = extract_entry_image(atlas, idx, manifest, shared_mat,
                                          f"{e['name']}_Restored")
                if out is not None:
                    cell_images[idx] = out
                    extracted += 1

        cache, processed, count = {}, set(), 0

        for obj in sel:
            if not any(slot.material == shared_mat for slot in obj.material_slots):
                continue
            mesh = obj.data
            if mesh.name in processed:
                continue
            processed.add(mesh.name)
            uv = mesh.uv_layers.get(UV_LAYER_NAME) or mesh.uv_layers.active
            if uv is None:
                continue

            face_cell, used = [], set()
            for poly in mesh.polygons:
                u, v = uv.data[poly.loop_indices[0]].uv
                idx = restore_index_from_uv(u, v, manifest, shared_mat)
                face_cell.append(idx)
                used.add(idx)

            used_sorted = sorted(used)
            cell_to_slot = {c: i for i, c in enumerate(used_sorted)}
            mesh.materials.clear()
            for c in used_sorted:
                if c not in cache:
                    cache[c] = self._build_material(manifest[c], cell_images.get(c))
                mesh.materials.append(cache[c])
            for poly, c in zip(mesh.polygons, face_cell):
                poly.material_index = cell_to_slot[c]

            # Un-map UVs for textured cells: inner rect -> 0-1.
            for poly, c in zip(mesh.polygons, face_cell):
                if c not in cell_images:
                    continue
                u0, v0, u1, v1 = restore_inner_uv(c, manifest, shared_mat, atlas)
                iw = max(u1 - u0, 1e-6)
                ih = max(v1 - v0, 1e-6)
                for li in poly.loop_indices:
                    u, v = uv.data[li].uv
                    uv.data[li].uv = ((u - u0) / iw, (v - v0) / ih)
            count += 1

        note = ""
        if relinked or extracted:
            note = f" (textures: {relinked} relinked, {extracted} extracted)"
        self.report({'INFO'}, f"Restored materials on {count} object(s){note}")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Rename / Select / Compact / Export operators
# ----------------------------------------------------------------------------

class OBJECT_OT_rename_grid(bpy.types.Operator):
    """Rename the grid textures and material used by the selected objects"""
    bl_idname = "object.rename_color_grid"
    bl_label = "Rename Current Grid"
    bl_options = {'REGISTER', 'UNDO'}

    new_name: bpy.props.StringProperty(name="New Name", default="")

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        sel = [o for o in context.selected_objects if o.type == 'MESH']
        mat = find_shared_material_from(sel)
        if mat is None:
            self.report({'ERROR'}, "Selected objects don't use a grid material")
            return {'CANCELLED'}
        name = (self.new_name or "").strip()
        if not name:
            self.report({'ERROR'}, "Enter a name first")
            return {'CANCELLED'}

        imgs = get_grid_images(mat)
        mat.name = name + "_Mat"
        suffix = {"color": "", "roughness": "_Rough", "metallic": "_Metal"}
        for key, img in imgs.items():
            img.name = name + suffix.get(key, "")
        self.report({'INFO'}, f"Renamed grid to '{name}'")
        return {'FINISHED'}


class OBJECT_OT_select_grid_users(bpy.types.Operator):
    """Select every object in the file that uses the same grid material"""
    bl_idname = "object.select_color_grid_users"
    bl_label = "Select Objects Using Grid"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        sel = [o for o in context.selected_objects if o.type == 'MESH']
        mat = find_shared_material_from(sel)
        if mat is None:
            self.report({'ERROR'}, "Selected objects don't use a grid material")
            return {'CANCELLED'}
        users = objects_using_material(mat)
        for o in context.selected_objects:
            o.select_set(False)
        for o in users:
            o.select_set(True)
        if users:
            context.view_layer.objects.active = users[0]
        self.report({'INFO'}, f"Selected {len(users)} object(s) using '{mat.name}'")
        return {'FINISHED'}


class OBJECT_OT_compact_grid(bpy.types.Operator):
    """Remove cells no longer used by any object, then re-pack the grid"""
    bl_idname = "object.compact_color_grid"
    bl_label = "Compact Grid (Remove Unused)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        sel = [o for o in context.selected_objects if o.type == 'MESH']
        mat = find_shared_material_from(sel)
        if mat is None:
            self.report({'ERROR'}, "Selected objects don't use a grid material")
            return {'CANCELLED'}
        manifest = read_manifest(mat)
        if not manifest:
            self.report({'ERROR'}, "No manifest stored on the grid material")
            return {'CANCELLED'}

        atlas = get_material_image(mat)
        old_S = int(mat.get("mcg_atlas_size", 0))
        old_rects = [e.get("px") for e in manifest]
        imgs = get_grid_images(mat)
        if not imgs:
            self.report({'ERROR'}, "No texture images found")
            return {'CANCELLED'}
        ow, oh = (atlas.size if atlas else (old_S or 1, old_S or 1))

        used, per_mesh = set(), {}
        for obj in objects_using_material(mat):
            mesh = obj.data
            if mesh.name in per_mesh:
                continue
            uv = mesh.uv_layers.get(UV_LAYER_NAME) or mesh.uv_layers.active
            if uv is None:
                continue
            faces = []
            for poly in mesh.polygons:
                u, v = uv.data[poly.loop_indices[0]].uv
                idx = restore_index_from_uv(u, v, manifest, mat)
                used.add(idx)
                faces.append((poly, idx))
            per_mesh[mesh.name] = (uv, faces)

        if not used:
            self.report({'ERROR'}, "No objects with grid UVs found")
            return {'CANCELLED'}
        removed = len(manifest) - len(used)
        if removed <= 0:
            self.report({'INFO'}, "No unused cells to remove")
            return {'FINISHED'}

        old_to_new, new_manifest = {}, []
        for old_idx in sorted(used):
            old_to_new[old_idx] = len(new_manifest)
            new_manifest.append(manifest[old_idx])

        # Capture old inner rects before re-packing (for relative remap).
        old_inner = {oi: restore_inner_uv(oi, manifest, mat, atlas) for oi in used}

        s = context.scene.mcg_settings
        pad = int(mat.get("mcg_padding", s.cell_padding))
        eff_swatch = 1 if s.minimal_resolution else s.solid_swatch_px
        S, rects = compute_packing(new_manifest, s.texture_tile_px,
                                   eff_swatch, pad, s.resolution)
        assign_packing(new_manifest, rects, S)

        for key, img in imgs.items():
            if img.size[0] != S or img.size[1] != S:
                img.scale(S, S)
            composite_packed(img, new_manifest, S, pad, key)
        write_manifest(mat, new_manifest)
        mat["mcg_atlas_size"] = S
        mat["mcg_padding"] = pad

        # Remap UVs, preserving each loop's relative position within its rect.
        for uv, faces in per_mesh.values():
            for poly, old_idx in faces:
                new_idx = old_to_new[old_idx]
                ou0, ov0, ou1, ov1 = old_inner[old_idx]
                nu0, nv0, nu1, nv1 = entry_inner_uv(new_manifest[new_idx]["px"], S, pad)
                for li in poly.loop_indices:
                    u, v = uv.data[li].uv
                    lu = min(max((u - ou0) / (ou1 - ou0), 0.0), 1.0) if ou1 > ou0 else 0.5
                    lv = min(max((v - ov0) / (ov1 - ov0), 0.0), 1.0) if ov1 > ov0 else 0.5
                    uv.data[li].uv = (nu0 + lu * (nu1 - nu0), nv0 + lv * (nv1 - nv0))

        self.report({'INFO'}, f"Removed {removed} unused cell(s); {len(new_manifest)} remain")
        return {'FINISHED'}


class OBJECT_OT_export_grid_png(bpy.types.Operator):
    """Save the grid textures to PNG files (e.g. to upload to Roblox)"""
    bl_idname = "object.export_color_grid_png"
    bl_label = "Export Grid Textures (PNG)"
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filename_ext = ".png"

    reference_after_export: bpy.props.BoolProperty(
        name="Reference Exported Files (Unpack)",
        description="After saving, point each image at its PNG and unpack it so it is no longer "
                    "embedded in the .blend. Makes FBX export reference real files, fixing "
                    "textures not loading in Roblox Studio",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def _images(self, context):
        sel = [o for o in context.selected_objects if o.type == 'MESH']
        mat = find_shared_material_from(sel)
        return get_grid_images(mat) if mat else {}

    def invoke(self, context, event):
        imgs = self._images(context)
        if not imgs:
            self.report({'ERROR'}, "Selected objects don't use a grid material with textures")
            return {'CANCELLED'}
        base = imgs.get("color")
        # Default to the image's unique datablock name: stable per grid, distinct
        # between different grids, so re-exporting updates the SAME files.
        self.filepath = ((base.name if base else "ColorGrid")) + ".png"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        imgs = self._images(context)
        if not imgs:
            self.report({'ERROR'}, "No grid textures found")
            return {'CANCELLED'}

        path = self.filepath
        stem = path[:-4] if path.lower().endswith(".png") else path
        suffix = {"color": "", "roughness": "_Rough", "metallic": "_Metal"}
        present = [k for k in ("color", "roughness", "metallic") if imgs.get(k) is not None]

        # Grid names carry a random seed, so different grids never share a filename.
        # Re-exporting the same grid overwrites its own files (the intended update).
        saved = 0
        for key in present:
            img = imgs[key]
            out = stem + suffix[key] + ".png"
            img.filepath_raw = out
            img.file_format = 'PNG'
            try:
                img.save()
            except RuntimeError as e:
                self.report({'ERROR'}, f"Save failed for {key}: {e}")
                return {'CANCELLED'}
            if self.reference_after_export:
                img.filepath = out
                img.source = 'FILE'
                if img.packed_file is not None:
                    try:
                        img.unpack(method='REMOVE')
                    except RuntimeError:
                        pass
            saved += 1

        note = " (unpacked)" if self.reference_after_export else ""
        self.report({'INFO'},
                    f"Saved/updated {saved} texture(s){note} as {os.path.basename(stem)}.png")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

PRESETS = {
    'HERO': dict(texture_tile_px=512, solid_swatch_px=16, resolution=1024, cell_padding=4,
                 bake_roughness=True, bake_metallic=True),
    'PROP': dict(texture_tile_px=256, solid_swatch_px=16, resolution=1024, cell_padding=2,
                 bake_roughness=True, bake_metallic=True),
    'BACKGROUND': dict(texture_tile_px=128, solid_swatch_px=8, resolution=512, cell_padding=2,
                       bake_roughness=False, bake_metallic=False),
}


class MCG_OT_apply_preset(bpy.types.Operator):
    """Apply a usage preset to the bake settings"""
    bl_idname = "mcg.apply_preset"
    bl_label = "Apply Preset"

    preset: bpy.props.EnumProperty(items=[
        ('HERO', "Hero", "Close-up assets: 1024 atlas, padding 4, all maps"),
        ('PROP', "Prop", "Standard props: 512 atlas, padding 2, all maps"),
        ('BACKGROUND', "Background", "Distant/mass-placed: minimal solid-color atlas"),
    ])

    def execute(self, context):
        s = context.scene.mcg_settings
        for k, v in PRESETS[self.preset].items():
            setattr(s, k, v)
        self.report({'INFO'}, f"Applied {self.preset.title()} preset")
        return {'FINISHED'}


class OBJECT_OT_set_grid_mode(bpy.types.Operator):
    """Set the processing mode (for one object, or all selected meshes)"""
    bl_idname = "object.set_grid_mode"
    bl_label = "Set Mode"
    bl_options = {'REGISTER', 'UNDO'}

    mode: bpy.props.EnumProperty(items=MODE_ITEMS, name="Mode")
    object_name: bpy.props.StringProperty(
        name="Object", default="",
        description="If set, only this object; otherwise all selected meshes")

    def execute(self, context):
        if self.object_name:
            obj = bpy.data.objects.get(self.object_name)
            targets = [obj] if (obj is not None and obj.type == 'MESH') else []
        else:
            targets = [o for o in context.selected_objects if o.type == 'MESH']
        if not targets:
            self.report({'WARNING'}, "No mesh objects to set")
            return {'CANCELLED'}
        for obj in targets:
            set_object_mode(obj, self.mode)
        return {'FINISHED'}


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


class OBJECT_OT_bake_vertex_colors(bpy.types.Operator):
    """Bake Base Color into vertex colors for VERTEX-mode objects"""
    bl_idname = "object.bake_vertex_colors"
    bl_label = "Bake Vertex Colors"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        targets = [o for o in context.selected_objects
                   if o.type == 'MESH' and get_object_mode(o) == 'VERTEX']
        if not targets:
            self.report({'WARNING'}, "No objects set to Vertex Color mode")
            return {'CANCELLED'}

        done = 0
        for obj in targets:
            if bake_vertex_colors(obj):
                done += 1
        self.report({'INFO'}, f"Baked vertex colors on {done} object(s). "
                              f"Export FBX/glTF to keep them.")
        return {'FINISHED'}


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


class OBJECT_OT_vertex_color_to_material(bpy.types.Operator):
    """Convert each object's vertex colors into materials (Base Color)"""
    bl_idname = "object.mcg_vcol_to_material"
    bl_label = "Vertex Color → Material"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        sel = [o for o in context.selected_objects if o.type == 'MESH']
        cache = {}
        objs = 0
        made = 0
        skipped = 0
        for obj in sel:
            res = vertex_colors_to_materials(obj, cache)
            if res is None:
                skipped += 1
                continue
            objs += 1
            made += res[1]
        if objs == 0:
            self.report({'WARNING'}, "No active vertex color found on selected meshes")
            return {'CANCELLED'}
        note = f", {skipped} skipped (no vertex color)" if skipped else ""
        self.report({'INFO'},
                    f"Converted {objs} object(s); {made} material(s) created{note}")
        return {'FINISHED'}


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


# ----------------------------------------------------------------------------
# Detect Colors from Texture
#
# For materials whose Base Color is driven by an image (e.g. a Tripo bake that
# *looks* solid but is really a full-resolution photo/render), sample each
# face's color from the texture, cluster faces by color, and replace them with
# solid-color materials. The result feeds straight into the normal Atlas bake,
# where solid colors pack as tiny swatches instead of full image tiles.
# ----------------------------------------------------------------------------

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


class OBJECT_OT_detect_colors_from_texture(bpy.types.Operator):
    """Sample each face's color from its image-textured Base Color, group faces by
    color, and replace them with solid-color materials (feeds the Atlas bake)"""
    bl_idname = "object.mcg_detect_colors"
    bl_label = "Detect Colors from Texture"
    bl_options = {'REGISTER', 'UNDO'}

    group_mode: bpy.props.EnumProperty(
        name="Grouping", items=COLOR_GROUP_MODE_ITEMS, default='AUTO')
    threshold: bpy.props.FloatProperty(
        name="Color Threshold", default=0.04, min=0.001, max=0.5)
    color_count: bpy.props.IntProperty(
        name="Color Count", default=8, min=2, max=64)
    sample_density: bpy.props.IntProperty(
        name="Samples per Face", default=3, min=1, max=6)
    aggregate: bpy.props.EnumProperty(
        name="Representative Color",
        items=[('MEDIAN', "Median", ""), ('MEAN', "Mean", "")], default='MEDIAN')
    merge_across_objects: bpy.props.BoolProperty(
        name="Merge Across Selected Objects", default=True)
    name_prefix: bpy.props.StringProperty(name="Prefix", default="DetectedColor")
    cleanup_unused_slots: bpy.props.BoolProperty(
        name="Remove Unused Slots After", default=True)

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        if _np is None:
            self.report({'ERROR'}, "NumPy is required for Detect Colors (should ship with Blender)")
            return {'CANCELLED'}

        sel = [o for o in context.selected_objects if o.type == 'MESH']
        img_cache = {}
        records = []

        for obj in sel:
            mesh = obj.data
            uv_layer = mesh.uv_layers.active
            if uv_layer is None or not mesh.polygons:
                continue
            slot_info = {}
            for idx, slot in enumerate(obj.material_slots):
                mat = slot.material
                if mat is None or not material_has_image_basecolor(mat):
                    continue
                img = get_basecolor_image(mat)
                if img is None:
                    continue
                buf_info = get_image_buffer(img_cache, img)
                if buf_info is None:
                    continue
                slot_info[idx] = (buf_info,
                                  get_principled_value(mat, "Roughness", 0.5),
                                  get_principled_value(mat, "Metallic", 0.0))
            if not slot_info:
                continue
            for poly in mesh.polygons:
                info = slot_info.get(poly.material_index)
                if info is None:
                    continue
                buf_info, rough, metal = info
                rep = face_representative_color(buf_info, uv_layer, poly,
                                                self.sample_density, self.aggregate)
                if rep is None:
                    continue
                records.append({"obj": obj, "poly_index": poly.index,
                                "color": rep, "rough": rough, "metal": metal})

        if not records:
            self.report({'WARNING'},
                        "No image-textured Base Color materials found on selected meshes")
            return {'CANCELLED'}

        groups = {}
        if self.merge_across_objects:
            groups[None] = records
        else:
            for r in records:
                groups.setdefault(r["obj"], []).append(r)

        mat_cache = {}
        touched_objs = set()
        total_groups = 0

        for recs in groups.values():
            colors = _np.array([r["color"][:3] for r in recs], dtype=_np.float64)
            if self.group_mode == 'FIXED':
                labels, centers = kmeans_colors(colors, self.color_count)
            else:
                labels, centers = threshold_cluster_colors(colors, self.threshold)
            n_clusters = len(centers)
            total_groups += n_clusters

            alpha_sum = [0.0] * n_clusters
            rough_sum = [0.0] * n_clusters
            metal_sum = [0.0] * n_clusters
            counts = [0] * n_clusters
            for r, lab in zip(recs, labels):
                alpha_sum[lab] += float(r["color"][3])
                rough_sum[lab] += r["rough"]
                metal_sum[lab] += r["metal"]
                counts[lab] += 1

            cluster_mats = []
            for ci in range(n_clusters):
                cnt = max(1, counts[ci])
                rgba = (float(centers[ci][0]), float(centers[ci][1]),
                        float(centers[ci][2]), alpha_sum[ci] / cnt)
                mat = get_or_create_color_material(
                    self.name_prefix, rgba, rough_sum[ci] / cnt, metal_sum[ci] / cnt,
                    mat_cache)
                cluster_mats.append(mat)

            by_obj = {}
            for r, lab in zip(recs, labels):
                by_obj.setdefault(r["obj"], []).append((r["poly_index"], cluster_mats[lab]))

            for obj, assigns in by_obj.items():
                mesh = obj.data
                mat_to_slot = {}
                for i, m in enumerate(mesh.materials):
                    if m is not None:
                        mat_to_slot.setdefault(m.name, i)
                for pidx, mat in assigns:
                    si = mat_to_slot.get(mat.name)
                    if si is None:
                        mesh.materials.append(mat)
                        si = len(mesh.materials) - 1
                        mat_to_slot[mat.name] = si
                    mesh.polygons[pidx].material_index = si
                touched_objs.add(obj)

        if self.cleanup_unused_slots:
            for obj in touched_objs:
                remove_unused_slots(context, obj)

        self.report({'INFO'},
                    f"{len(touched_objs)} object(s), {total_groups} color group(s) "
                    f"from {len(records)} face sample(s)")
        return {'FINISHED'}


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


class OBJECT_OT_export_color_json(bpy.types.Operator):
    """Split JSON-mode objects per material and export colors for the Roblox plugin"""
    bl_idname = "object.export_color_json"
    bl_label = "Export Color JSON"
    bl_options = {'REGISTER', 'UNDO'}

    to_clipboard: bpy.props.BoolProperty(name="Copy to Clipboard", default=True)
    to_file: bpy.props.BoolProperty(
        name="Also Save .json (next to .blend)", default=False)

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def _targets(self, context):
        return [o for o in context.selected_objects
                if o.type == 'MESH' and get_object_mode(o) == 'JSON']

    def invoke(self, context, event):
        if not self._targets(context):
            self.report({'WARNING'}, "No objects set to JSON mode")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        layout.label(text="JSON export will modify these objects:", icon='ERROR')
        box = layout.box()
        col = box.column(align=True)
        for name, kind, count in analyze_json_targets(self._targets(context)):
            if kind == 'grid':
                txt = f"{name}: restore from grid, split into {count}"
            elif kind == 'multi':
                txt = f"{name}: split into {count} parts (per material)"
            else:
                txt = f"{name}: keep as-is (1 material)"
            col.label(text=txt, icon='MESH_DATA')
        layout.label(text="Objects are replaced by per-material parts.", icon='INFO')
        layout.prop(self, "to_clipboard")
        layout.prop(self, "to_file")

    def execute(self, context):
        targets = self._targets(context)
        if not targets:
            self.report({'WARNING'}, "No objects set to JSON mode")
            return {'CANCELLED'}

        if context.mode != 'OBJECT' and context.active_object:
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError:
                pass

        final_objs = []
        for obj in list(targets):
            if is_grid_object(obj):
                restore_object_materials(obj)
            if used_material_count(obj) > 1:
                final_objs.extend(split_and_rename_by_material(context, obj))
            else:
                # Single material: rename for consistency, keep as one part.
                mat = next((s.material for s in obj.material_slots if s.material), None)
                if mat is not None:
                    obj.name = f"{sanitize_name(obj.name, 20)}_{sanitize_name(mat.name, 16)}"
                final_objs.append(obj)

        # Rename each object to its export key so the FBX part name in Roblox
        # matches the JSON key EXACTLY. export_key_for is idempotent.
        for obj in final_objs:
            obj.name = export_key_for(obj)

        data = build_color_json(final_objs)
        if not data:
            self.report({'WARNING'}, "No usable materials to export")
            return {'CANCELLED'}

        text = json.dumps(data, indent=4)
        wrote = []
        if self.to_clipboard:
            context.window_manager.clipboard = text
            wrote.append("clipboard")
        if self.to_file:
            blend_dir = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else ""
            if not blend_dir:
                self.report({'WARNING'}, "Save the .blend first to write a .json file")
            else:
                path = os.path.join(blend_dir, "colors.json")
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
                    wrote.append("colors.json")
                except OSError as e:
                    self.report({'ERROR'}, f"File write failed: {e}")
                    return {'CANCELLED'}

        self.report({'INFO'},
                    f"Exported {len(data)} part color(s) to {', '.join(wrote) or 'nowhere'}")
        return {'FINISHED'}


def selected_modes(context):
    """Set of modes among selected mesh objects."""
    return {get_object_mode(o) for o in context.selected_objects if o.type == 'MESH'}


class OBJECT_OT_process_selected(bpy.types.Operator):
    """Process selected objects according to each one's mode"""
    bl_idname = "object.mcg_process_selected"
    bl_label = "Process Selected"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        s = context.scene.mcg_settings
        modes = selected_modes(context)
        ran = []

        if 'ATLAS' in modes:
            bpy.ops.object.material_color_grid(
                group_name=s.grid_name,
                resolution=s.resolution,
                texture_tile_px=s.texture_tile_px,
                solid_swatch_px=s.solid_swatch_px,
                minimal_resolution=s.minimal_resolution,
                cell_padding=s.cell_padding,
                bake_roughness=s.bake_roughness,
                bake_metallic=s.bake_metallic,
                create_vertex_groups=s.create_vertex_groups,
                remap_uvs=s.remap_uvs,
                replace_materials=s.replace_materials,
                sync_all_users=s.sync_all_users,
                remove_unused_slots=s.remove_unused_slots,
                auto_export=s.auto_export_after_bake,
            )
            ran.append("atlas")

        if 'VERTEX' in modes:
            bpy.ops.object.bake_vertex_colors()
            ran.append("vertex")

        if not ran:
            self.report({'WARNING'},
                        "Set objects to Atlas or Vertex mode "
                        "(JSON mode uses the Export JSON button).")
            return {'CANCELLED'}

        return {'FINISHED'}


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


class OBJECT_OT_roblox_check(bpy.types.Operator):
    """Check selected objects for common Roblox export issues"""
    bl_idname = "object.mcg_roblox_check"
    bl_label = "Run Roblox Check"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        s = context.scene.mcg_settings
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        lines = []
        total_tris = 0

        for obj in meshes:
            mesh = obj.data
            tris = _tri_count(mesh)
            total_tris += tris
            if tris > s.tri_warn_threshold:
                lines.append(f"W|{obj.name}: {tris} tris (> {s.tri_warn_threshold})")

            if not mesh.polygons:
                lines.append(f"W|{obj.name}: mesh has no faces")

            ngons = _ngon_count(mesh)
            if ngons:
                lines.append(f"W|{obj.name}: {ngons} n-gon(s) (5+ sided)")

            empties = _empty_slot_count(obj)
            if empties:
                lines.append(f"W|{obj.name}: {empties} empty material slot(s)")

            sc = obj.scale
            if any(abs(c - 1.0) > 1e-3 for c in sc):
                lines.append(f"W|{obj.name}: scale not applied "
                             f"({sc[0]:.2f},{sc[1]:.2f},{sc[2]:.2f})")

            oor = _uv_out_of_range(mesh)
            if oor is None:
                lines.append(f"W|{obj.name}: no UV map")
            elif oor:
                lines.append(f"I|{obj.name}: UVs outside 0-1 (tiling?)")

            used_mats = used_material_count(obj)
            if used_mats > 1 and get_object_mode(obj) != 'ATLAS':
                lines.append(f"I|{obj.name}: {used_mats} materials "
                             f"(consider Atlas mode)")

            for slot in obj.material_slots:
                mat = slot.material
                if mat is None:
                    continue
                img = get_basecolor_image(mat)
                if img is not None and max(img.size) > s.tex_warn_resolution:
                    lines.append(f"W|{obj.name}: texture {img.size[0]}x{img.size[1]} "
                                 f"(> {s.tex_warn_resolution})")
                    break

        # Summary line (mesh count ~ rough draw-call ballpark, batching aside)
        summary = f"G|{len(meshes)} mesh(es), {total_tris} tris total"
        if not lines:
            lines.append(summary + " — all checks passed")
        else:
            lines.insert(0, summary)

        s.check_report = "\n".join(lines)
        self.report({'INFO'}, f"Checked {len(meshes)} object(s)")
        return {'FINISHED'}


def _mode_label(mode):
    for ident, name, _desc in MODE_ITEMS:
        if ident == mode:
            return name
    return mode


class VIEW3D_PT_color_grid(bpy.types.Panel):
    bl_label = "Color Grid"
    bl_idname = "VIEW3D_PT_color_grid"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Color Grid"

    def draw(self, context):
        layout = self.layout
        s = context.scene.mcg_settings

        # ---- Per-object mode list (selected meshes only) ----
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        active = context.active_object if (
            context.active_object and context.active_object.type == 'MESH') else None
        MAX_ROWS = 16

        box = layout.box()
        box.label(text="Objects & Modes", icon='OUTLINER')
        if not meshes:
            box.label(text="Select mesh objects.", icon='INFO')
        else:
            # Bulk set (applies to all selected meshes)
            sub = box.column(align=True)
            sub.label(text=f"Set all ({len(meshes)} meshes):")
            row = sub.row(align=True)
            for ident, name, _desc in MODE_ITEMS:
                op = row.operator(OBJECT_OT_set_grid_mode.bl_idname, text=name)
                op.mode = ident
                op.object_name = ""

            box.separator()

            def mode_row(container, obj):
                container.label(text=obj.name, icon='MESH_DATA')
                r = container.row(align=True)
                current = get_object_mode(obj)
                for ident, name, _desc in MODE_ITEMS:
                    op = r.operator(OBJECT_OT_set_grid_mode.bl_idname, text=name,
                                    depress=(current == ident))
                    op.mode = ident
                    op.object_name = obj.name

            if len(meshes) <= MAX_ROWS:
                for obj in meshes:
                    mode_row(box.column(align=True), obj)
            else:
                # Too many to list: show counts + only the active object's row.
                counts = {}
                for o in meshes:
                    m = get_object_mode(o)
                    counts[m] = counts.get(m, 0) + 1
                summary = ", ".join(f"{_mode_label(k)}:{counts[k]}" for k in counts)
                box.label(text=f"{len(meshes)} meshes — {summary}", icon='INFO')
                box.label(text="Too many to list. Use Set all, or select fewer.")
                if active is not None:
                    box.separator()
                    box.label(text="Active object:")
                    mode_row(box.column(align=True), active)

        box = layout.box()
        box.label(text="Detect Colors from Texture", icon='IMAGE_RGB')
        box.label(text="For image textures that look solid (e.g. Tripo bakes).",
                  icon='INFO')
        box.prop(s, "detect_group_mode")
        if s.detect_group_mode == 'AUTO':
            box.prop(s, "detect_threshold")
        else:
            box.prop(s, "detect_color_count")
        row = box.row(align=True)
        row.prop(s, "detect_sample_density")
        row.prop(s, "detect_aggregate", text="")
        box.prop(s, "detect_merge_across_objects")
        box.prop(s, "detect_name_prefix")
        box.prop(s, "detect_cleanup_unused_slots")
        op = box.operator(OBJECT_OT_detect_colors_from_texture.bl_idname,
                          text="Detect Colors & Split Materials")
        op.group_mode = s.detect_group_mode
        op.threshold = s.detect_threshold
        op.color_count = s.detect_color_count
        op.sample_density = s.detect_sample_density
        op.aggregate = s.detect_aggregate
        op.merge_across_objects = s.detect_merge_across_objects
        op.name_prefix = s.detect_name_prefix
        op.cleanup_unused_slots = s.detect_cleanup_unused_slots

        modes = {get_object_mode(o) for o in meshes} if meshes else set()
        has_atlas = 'ATLAS' in modes
        has_vertex = 'VERTEX' in modes

        box = layout.box()
        box.label(text="Bake / Process", icon='TEXTURE')

        # Atlas settings only matter when an Atlas object is selected.
        if has_atlas:
            row = box.row(align=True)
            row.label(text="Preset:")
            for ident, label in (('HERO', "Hero"), ('PROP', "Prop"),
                                 ('BACKGROUND', "Background")):
                row.operator(MCG_OT_apply_preset.bl_idname, text=label).preset = ident
            box.prop(s, "grid_name")
            box.prop(s, "minimal_resolution")
            row = box.row(align=True)
            row.prop(s, "texture_tile_px")
            sub = row.row(align=True)
            sub.enabled = not s.minimal_resolution
            sub.prop(s, "solid_swatch_px")
            box.prop(s, "resolution")
            box.prop(s, "cell_padding")
            row = box.row(align=True)
            row.prop(s, "bake_roughness", toggle=True)
            row.prop(s, "bake_metallic", toggle=True)
            col = box.column(align=True)
            col.prop(s, "create_vertex_groups")
            col.prop(s, "remap_uvs")
            col.prop(s, "replace_materials")
            col.prop(s, "sync_all_users")
            col.prop(s, "remove_unused_slots")
            col.prop(s, "auto_export_after_bake")

        # Adaptive primary button label.
        if has_atlas and has_vertex:
            label = "Bake Selected (Atlas + Vertex)"
        elif has_atlas:
            label = "Bake Selected to Grid"
        elif has_vertex:
            label = "Bake Vertex Colors"
        else:
            label = "Nothing to Bake"

        row = box.row()
        row.scale_y = 1.3
        row.enabled = has_atlas or has_vertex
        row.operator(OBJECT_OT_process_selected.bl_idname, text=label)
        if not (has_atlas or has_vertex) and meshes:
            box.label(text="Set objects to Atlas or Vertex (JSON uses Export below).", icon='INFO')

        box = layout.box()
        box.label(text="Tools", icon='TOOL_SETTINGS')
        op = box.operator(OBJECT_OT_rename_grid.bl_idname, text="Rename Current Grid")
        op.new_name = s.grid_name
        box.operator(OBJECT_OT_select_grid_users.bl_idname, text="Select Objects Using Grid")
        box.operator(OBJECT_OT_compact_grid.bl_idname, text="Compact Grid (Remove Unused)")
        box.operator(OBJECT_OT_export_grid_png.bl_idname, text="Export Textures (PNG)")

        box = layout.box()
        box.label(text="Export Color Data (JSON)", icon='TEXT')
        box.operator(OBJECT_OT_export_color_json.bl_idname, text="Export JSON…")
        box.label(text="Splits JSON-mode objects per material.", icon='INFO')

        box = layout.box()
        box.label(text="Roblox Check", icon='CHECKMARK')
        row = box.row(align=True)
        row.prop(s, "tri_warn_threshold")
        box.prop(s, "tex_warn_resolution")
        box.operator(OBJECT_OT_roblox_check.bl_idname, text="Run Roblox Check")
        if s.check_report:
            icon_for = {'W': 'ERROR', 'I': 'INFO', 'G': 'CHECKMARK'}
            rep = box.column(align=True)
            for line in s.check_report.split("\n"):
                if "|" in line:
                    lvl, msg = line.split("|", 1)
                else:
                    lvl, msg = 'I', line
                rep.label(text=msg, icon=icon_for.get(lvl, 'DOT'))

        box = layout.box()
        box.label(text="Reverse", icon='MATERIAL')
        box.operator(OBJECT_OT_restore_materials_from_grid.bl_idname, text="Restore Materials")
        box.operator(OBJECT_OT_vertex_color_to_material.bl_idname,
                     text="Vertex Color → Material")


def menu_func(self, context):
    self.layout.separator()
    self.layout.operator(OBJECT_OT_material_color_grid.bl_idname, icon='TEXTURE')
    self.layout.operator(OBJECT_OT_restore_materials_from_grid.bl_idname, icon='MATERIAL')


# ----------------------------------------------------------------------------
# Updater
# ----------------------------------------------------------------------------

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


class MCG_OT_check_update(bpy.types.Operator):
    """Check GitHub for a newer release"""
    bl_idname = "mcg.check_update"
    bl_label = "Check for Updates"

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        try:
            rel = _fetch_latest_release()
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
            prefs.update_status = f"Check failed: {e}"
            prefs.update_available = False
            self.report({'ERROR'}, f"Update check failed: {e}")
            return {'CANCELLED'}

        latest = rel["version"]
        prefs.latest_tag = rel["tag"] or ""
        prefs.latest_asset_url = rel["asset_url"] or ""
        prefs.latest_html_url = rel["html_url"] or ""

        cur = _current_version()
        if latest is None:
            prefs.update_status = f"Could not parse latest version ('{rel['tag']}')"
            prefs.update_available = False
        elif latest > cur:
            prefs.update_available = True
            prefs.update_status = (
                f"Update available: {rel['tag']} "
                f"(installed v{cur[0]}.{cur[1]}.{cur[2]})"
            )
        else:
            prefs.update_available = False
            prefs.update_status = f"Up to date (v{cur[0]}.{cur[1]}.{cur[2]})"
        return {'FINISHED'}


class MCG_OT_install_update(bpy.types.Operator):
    """Download the latest release and overwrite this addon's file (restart to apply)"""
    bl_idname = "mcg.install_update"
    bl_label = "Download & Install Update"

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        url = prefs.latest_asset_url
        if not url:
            self.report({'ERROR'}, "No download URL. Run Check for Updates first.")
            return {'CANCELLED'}

        try:
            data = _http_get(url, want_json=False)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            prefs.update_status = f"Download failed: {e}"
            self.report({'ERROR'}, f"Download failed: {e}")
            return {'CANCELLED'}

        # Basic sanity: it should look like our addon source.
        text = data.decode("utf-8", errors="replace")
        if "Material Color Grid Texture" not in text or "bl_info" not in text:
            prefs.update_status = "Downloaded file doesn't look valid; aborted."
            self.report({'ERROR'}, "Downloaded file failed validation; not installed.")
            return {'CANCELLED'}

        path = _addon_file_path()
        try:
            # Back up the current file, then overwrite.
            backup = path + ".bak"
            try:
                if os.path.exists(path):
                    with open(path, "rb") as src, open(backup, "wb") as dst:
                        dst.write(src.read())
            except OSError:
                pass
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        except OSError as e:
            prefs.update_status = f"Write failed: {e}"
            self.report({'ERROR'}, f"Could not write addon file: {e}")
            return {'CANCELLED'}

        prefs.update_available = False
        prefs.update_status = f"Installed {prefs.latest_tag}. Restart Blender to apply."
        self.report({'INFO'}, "Update installed. Please restart Blender to apply.")
        return {'FINISHED'}


class MCG_OT_open_releases(bpy.types.Operator):
    """Open the GitHub releases page in a browser"""
    bl_idname = "mcg.open_releases"
    bl_label = "Open Releases Page"

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        url = prefs.latest_html_url or f"https://github.com/{UPDATE_REPO}/releases"
        bpy.ops.wm.url_open(url=url)
        return {'FINISHED'}


class MCGAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    update_status: bpy.props.StringProperty(default="")
    update_available: bpy.props.BoolProperty(default=False)
    latest_tag: bpy.props.StringProperty(default="")
    latest_asset_url: bpy.props.StringProperty(default="")
    latest_html_url: bpy.props.StringProperty(default="")

    def draw(self, context):
        layout = self.layout
        v = _current_version()
        col = layout.column()
        col.label(text=f"Installed version: v{v[0]}.{v[1]}.{v[2]}")

        row = col.row(align=True)
        row.operator(MCG_OT_check_update.bl_idname, icon='FILE_REFRESH')
        row.operator(MCG_OT_open_releases.bl_idname, icon='URL')

        if self.update_status:
            icon = 'ERROR' if self.update_available else 'INFO'
            col.label(text=self.update_status, icon=icon)

        if self.update_available:
            col.operator(MCG_OT_install_update.bl_idname, icon='IMPORT')
            col.label(text="After installing, restart Blender to apply.", icon='INFO')


# ----------------------------------------------------------------------------
# Translations (UI is English; Japanese shown when Blender's language is ja_JP)
# ----------------------------------------------------------------------------

def _tr_pairs():
    pairs = {
        # Sections
        "Objects & Modes": "オブジェクトとモード",
        "Bake / Process": "ベイク / 処理",
        "Tools": "ツール",
        "Export Color Data (JSON)": "カラーデータ出力 (JSON)",
        "Roblox Check": "Robloxチェック",
        "Reverse": "逆変換",
        "Detect Colors from Texture": "テクスチャから色を検出",
        # Buttons / operators
        "Bake Selected to Grid": "選択をグリッドにベイク",
        "Bake Vertex Colors": "頂点カラーをベイク",
        "Bake Selected (Atlas + Vertex)": "選択をベイク (アトラス+頂点)",
        "Nothing to Bake": "ベイク対象なし",
        "Process Selected": "選択を処理",
        "Rename Current Grid": "現在のグリッドをリネーム",
        "Select Objects Using Grid": "グリッド使用オブジェクトを選択",
        "Compact Grid (Remove Unused)": "グリッドを整理 (未使用を削除)",
        "Export Textures (PNG)": "テクスチャを書き出し (PNG)",
        "Export Grid Textures (PNG)": "グリッドテクスチャを書き出し (PNG)",
        "Export JSON…": "JSONを書き出し…",
        "Export Color JSON": "カラーJSONを書き出し",
        "Run Roblox Check": "Robloxチェックを実行",
        "Restore Materials": "マテリアルを復元",
        "Restore Materials From Grid": "グリッドからマテリアルを復元",
        "Vertex Color → Material": "頂点カラー → マテリアル",
        "Detect Colors & Split Materials": "色を検出してマテリアル分割",
        "Check for Updates": "更新を確認",
        "Download & Install Update": "更新をダウンロード＆インストール",
        "Open Releases Page": "リリースページを開く",
        "Apply Preset": "プリセットを適用",
        "Set Mode": "モードを設定",
        # Properties
        "Grid Name": "グリッド名",
        "Texture Tile": "テクスチャタイル",
        "Solid Swatch": "単色スウォッチ",
        "Atlas Max": "アトラス上限",
        "Cell Padding": "セル余白",
        "Minimal (1px solids)": "最小 (単色1px)",
        "Roughness Map": "ラフネスマップ",
        "Metallic Map": "メタリックマップ",
        "Create Vertex Groups": "頂点グループを作成",
        "Remap UVs to Color Cells": "UVをカラーセルに再配置",
        "Replace Material Slots": "マテリアルスロットを置換",
        "Update All Objects Using Texture": "テクスチャ使用オブジェクトを全更新",
        "Update All Users": "使用オブジェクトを全更新",
        "Remove Unused Slots": "未使用スロットを削除",
        "Remove Unused Slots Before Bake": "ベイク前に未使用スロットを削除",
        "Auto Export + Unpack After Bake": "ベイク後に自動書き出し+パック解除",
        "Max Triangles": "最大トライアングル数",
        "Max Texture": "最大テクスチャ解像度",
        "Color Threshold": "色のしきい値",
        "Color Count": "色数",
        "Samples per Face": "面あたりサンプル数",
        "Representative Color": "代表色",
        "Merge Across Selected Objects": "選択オブジェクト間で統合",
        "Prefix": "接頭辞",
        "Remove Unused Slots After": "実行後に未使用スロットを削除",
        "Grouping": "グループ化",
        "Mode": "モード",
        "Copy to Clipboard": "クリップボードにコピー",
        "Also Save .json (next to .blend)": ".jsonも保存 (.blendと同じ場所)",
        # Modes / presets
        "Atlas": "アトラス",
        "Vertex Color": "頂点カラー",
        "None": "なし",
        "Hero": "ヒーロー",
        "Prop": "プロップ",
        "Background": "背景",
        "Median": "中央値",
        "Mean": "平均",
        "Auto (Threshold)": "自動 (しきい値)",
        "Fixed Count": "色数を指定",
        # Info labels
        "Select mesh objects.": "メッシュオブジェクトを選択してください。",
        "Set all:": "一括設定:",
        "Too many to list. Use Set all, or select fewer.":
            "多すぎて表示できません。一括設定を使うか選択を減らしてください。",
        "Active object:": "アクティブオブジェクト:",
        "Set objects to Atlas or Vertex (JSON uses Export below).":
            "アトラスか頂点カラーに設定してください (JSONは下の書き出しを使用)。",
        "Splits JSON-mode objects per material.":
            "JSONモードのオブジェクトをマテリアルごとに分割します。",
        "For image textures that look solid (e.g. Tripo bakes).":
            "単色に見える画像テクスチャ用 (Tripoベイクなど)。",
        "Preset:": "プリセット:",
        "After installing, restart Blender to apply.":
            "インストール後、Blenderを再起動してください。",
        "Objects are replaced by per-material parts.":
            "オブジェクトはマテリアルごとのパーツに置き換わります。",
        "JSON export will modify these objects:":
            "JSON書き出しは以下のオブジェクトを変更します:",
    }
    out = {}
    for en, ja in pairs.items():
        out[("*", en)] = ja
        out[("Operator", en)] = ja
    return out


translations_dict = {"ja_JP": _tr_pairs()}


classes = (
    MCGSettings,
    MCGAddonPreferences,
    MCG_OT_check_update,
    MCG_OT_install_update,
    MCG_OT_open_releases,
    OBJECT_OT_material_color_grid,
    OBJECT_OT_restore_materials_from_grid,
    OBJECT_OT_rename_grid,
    OBJECT_OT_select_grid_users,
    OBJECT_OT_compact_grid,
    OBJECT_OT_export_grid_png,
    OBJECT_OT_export_color_json,
    OBJECT_OT_bake_vertex_colors,
    OBJECT_OT_vertex_color_to_material,
    OBJECT_OT_detect_colors_from_texture,
    OBJECT_OT_process_selected,
    OBJECT_OT_roblox_check,
    OBJECT_OT_set_grid_mode,
    MCG_OT_apply_preset,
    VIEW3D_PT_color_grid,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Object.mcg_mode = bpy.props.EnumProperty(
        name="Mode", items=MODE_ITEMS, get=_mode_get, set=_mode_set,
        description="How this object is processed when baking",
    )
    bpy.types.Scene.mcg_settings = bpy.props.PointerProperty(type=MCGSettings)
    bpy.types.VIEW3D_MT_object.append(menu_func)
    try:
        bpy.app.translations.register(__name__, translations_dict)
    except ValueError:
        pass  # already registered (script re-run)


def unregister():
    try:
        bpy.app.translations.unregister(__name__)
    except ValueError:
        pass
    bpy.types.VIEW3D_MT_object.remove(menu_func)
    del bpy.types.Scene.mcg_settings
    del bpy.types.Object.mcg_mode
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
