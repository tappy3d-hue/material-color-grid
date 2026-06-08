bl_info = {
    "name": "Material Color Grid Texture",
    "author": "Claude",
    "version": (1, 11, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N) > Color Grid tab",
    "description": "Pool base colors (and optionally roughness/metallic) from selected "
                   "objects into shared grid textures and one material (meshes stay "
                   "separate). Re-running adds new values while preserving baked ones via "
                   "a stored manifest. Can restore per-value materials, compact unused "
                   "cells, export PNGs, and unpack for FBX/Roblox.",
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

# GitHub repo used for the in-Blender updater.
UPDATE_REPO = "tappy3d-hue/material-color-grid"
UPDATE_ASSET_NAME = "material_color_grid.py"
UPDATE_API_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"

DEFAULT_GROUP_NAME = "ColorGrid"
MANIFEST_KEY = "mcg_manifest"
UV_LAYER_NAME = "ColorGridUV"

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


# ----------------------------------------------------------------------------
# Color / value helpers
# ----------------------------------------------------------------------------

def linear_to_srgb_channel(c):
    if c < 0.0:
        return 0.0
    if c <= 0.0031308:
        return 12.92 * c
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


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
        name="Resolution", description="Texture resolution (square)",
        default=512, min=16, max=8192,
    )
    minimal_resolution: bpy.props.BoolProperty(
        name="Minimal Resolution",
        description="Make tiny textures sized to the grid (cells x Cell Pixels)",
        default=False,
    )
    cell_pixels: bpy.props.IntProperty(
        name="Cell Pixels", description="Pixels per cell when Minimal Resolution is on",
        default=8, min=1, max=256,
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


# ----------------------------------------------------------------------------
# Bake / Update operator
# ----------------------------------------------------------------------------

class OBJECT_OT_material_color_grid(bpy.types.Operator):
    """Bake selected objects' material values into shared grid textures"""
    bl_idname = "object.material_color_grid"
    bl_label = "Material Color Grid Texture"
    bl_options = {'REGISTER', 'UNDO'}

    resolution: bpy.props.IntProperty(name="Resolution", default=512, min=16, max=8192)
    group_name: bpy.props.StringProperty(name="Group Name", default=DEFAULT_GROUP_NAME)
    minimal_resolution: bpy.props.BoolProperty(name="Minimal Resolution", default=False)
    cell_pixels: bpy.props.IntProperty(name="Cell Pixels", default=8, min=1, max=256)
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

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        sel = [o for o in context.selected_objects if o.type == 'MESH']
        if not sel:
            self.report({'ERROR'}, "No mesh objects selected")
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
                entry = {
                    "name": mat.name,
                    "color": list(get_base_color(mat)),
                    "roughness": get_principled_value(mat, "Roughness", 0.5),
                    "metallic": get_principled_value(mat, "Metallic", 0.0),
                }
                if mat.name in name_to_index:
                    manifest[name_to_index[mat.name]] = entry
                else:
                    name_to_index[mat.name] = len(manifest)
                    manifest.append(entry)

        if not manifest:
            self.report({'ERROR'}, "No valid materials found on selected objects")
            return {'CANCELLED'}

        uv_centers, new_cols, new_rows = compute_uv_centers(len(manifest))

        # Decide texture dimensions
        if self.minimal_resolution:
            tw, th = new_cols * self.cell_pixels, new_rows * self.cell_pixels
        else:
            tw = th = self.resolution

        existing_imgs = get_grid_images(shared_mat) if shared_mat else {}

        # Which maps to produce: color always; data maps if toggled on or already present
        want = {"color": True,
                "roughness": self.bake_roughness or ("roughness" in existing_imgs),
                "metallic": self.bake_metallic or ("metallic" in existing_imgs)}

        # Resolve material
        if shared_mat is None:
            raw = (self.group_name or "").strip()
            seed = make_seed()
            base = (raw + "_" + seed) if raw else seed
            shared_mat = bpy.data.materials.new(name=base + "_Mat")
        else:
            base = None  # reuse existing image names

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
                img = new_image(nm, tw, th, is_data)
            elif self.minimal_resolution and (img.size[0] != tw or img.size[1] != th):
                img.scale(tw, th)
            # ensure correct colorspace
            img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'
            cells, srgb = cells_for_map(manifest, key)
            paint_grid(img, cells, srgb)
            images[key] = img

        setup_grid_material(shared_mat, images)
        write_manifest(shared_mat, manifest)

        # Remap existing textured objects (UV layout change)
        if old_count and (new_cols, new_rows) != (old_cols, old_rows):
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
                for poly in mesh.polygons:
                    u, v = uv.data[poly.loop_indices[0]].uv
                    idx = min(uv_to_cell_index(u, v, old_cols, old_rows), len(uv_centers) - 1)
                    for li in poly.loop_indices:
                        uv.data[li].uv = uv_centers[idx]

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
                while mesh.uv_layers:
                    mesh.uv_layers.remove(mesh.uv_layers[0])
                uvl = mesh.uv_layers.new(name=UV_LAYER_NAME)
                mesh.uv_layers.active = uvl
                uvl.active_render = True
                for poly in mesh.polygons:
                    idx = slot_to_idx.get(poly.material_index)
                    uv = uv_centers[idx] if idx is not None else (0.5, 0.5)
                    for li in poly.loop_indices:
                        uvl.data[li].uv = uv

            if self.replace_materials:
                mesh.materials.clear()
                mesh.materials.append(shared_mat)
                for poly in mesh.polygons:
                    poly.material_index = 0
            elif shared_mat.name not in [m.name for m in mesh.materials if m]:
                mesh.materials.append(shared_mat)

        mode = "Updated" if old_count else "Created"
        maps = "+".join(k for k in ("color", "roughness", "metallic") if k in images)
        self.report({'INFO'},
                    f"{mode} grid {new_cols}x{new_rows} ({len(manifest)} cells, "
                    f"+{len(manifest) - old_count} new) maps: {maps}")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Restore operator
# ----------------------------------------------------------------------------

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

    def _build_material(self, entry):
        name = entry["name"]
        col = entry["color"]
        rgba = (col[0], col[1], col[2], col[3] if len(col) > 3 else 1.0)
        rough = float(entry.get("roughness", 0.5))
        metal = float(entry.get("metallic", 0.0))

        # Reuse an existing same-name material ONLY if it already matches (no overwrite).
        existing = bpy.data.materials.get(name)
        if existing is not None and self._matches(existing, rgba, rough, metal):
            return existing

        # Otherwise create a new material. If the name is taken by a different
        # material, Blender appends a numeric suffix (e.g. ".001") automatically,
        # so the existing one is never modified.
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

        cols, rows = calculate_grid(len(manifest))
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
                idx = min(uv_to_cell_index(u, v, cols, rows), len(manifest) - 1)
                face_cell.append(idx)
                used.add(idx)

            used_sorted = sorted(used)
            cell_to_slot = {c: i for i, c in enumerate(used_sorted)}
            mesh.materials.clear()
            for c in used_sorted:
                if c not in cache:
                    cache[c] = self._build_material(manifest[c])
                mesh.materials.append(cache[c])
            for poly, c in zip(mesh.polygons, face_cell):
                poly.material_index = cell_to_slot[c]
            count += 1

        self.report({'INFO'}, f"Restored materials on {count} object(s)")
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

        old_cols, old_rows = calculate_grid(len(manifest))
        imgs = get_grid_images(mat)
        if not imgs:
            self.report({'ERROR'}, "No texture images found")
            return {'CANCELLED'}

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
                idx = min(uv_to_cell_index(u, v, old_cols, old_rows), len(manifest) - 1)
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

        uv_centers, _, _ = compute_uv_centers(len(new_manifest))
        for key, img in imgs.items():
            is_data = key != "color"
            cells, srgb = cells_for_map(new_manifest, key)
            paint_grid(img, cells, srgb)
        write_manifest(mat, new_manifest)

        for uv, faces in per_mesh.values():
            for poly, old_idx in faces:
                new_uv = uv_centers[old_to_new[old_idx]]
                for li in poly.loop_indices:
                    uv.data[li].uv = new_uv

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

class VIEW3D_PT_color_grid(bpy.types.Panel):
    bl_label = "Color Grid"
    bl_idname = "VIEW3D_PT_color_grid"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Color Grid"

    def draw(self, context):
        layout = self.layout
        s = context.scene.mcg_settings

        box = layout.box()
        box.label(text="Bake / Update", icon='TEXTURE')
        box.prop(s, "grid_name")
        box.prop(s, "minimal_resolution")
        box.prop(s, "cell_pixels" if s.minimal_resolution else "resolution")
        row = box.row(align=True)
        row.prop(s, "bake_roughness", toggle=True)
        row.prop(s, "bake_metallic", toggle=True)
        col = box.column(align=True)
        col.prop(s, "create_vertex_groups")
        col.prop(s, "remap_uvs")
        col.prop(s, "replace_materials")
        col.prop(s, "sync_all_users")
        col.prop(s, "remove_unused_slots")
        op = box.operator(OBJECT_OT_material_color_grid.bl_idname, text="Bake Selected to Grid")
        op.group_name = s.grid_name
        op.resolution = s.resolution
        op.minimal_resolution = s.minimal_resolution
        op.cell_pixels = s.cell_pixels
        op.bake_roughness = s.bake_roughness
        op.bake_metallic = s.bake_metallic
        op.create_vertex_groups = s.create_vertex_groups
        op.remap_uvs = s.remap_uvs
        op.replace_materials = s.replace_materials
        op.sync_all_users = s.sync_all_users
        op.remove_unused_slots = s.remove_unused_slots

        box = layout.box()
        box.label(text="Tools", icon='TOOL_SETTINGS')
        op = box.operator(OBJECT_OT_rename_grid.bl_idname, text="Rename Current Grid")
        op.new_name = s.grid_name
        box.operator(OBJECT_OT_select_grid_users.bl_idname, text="Select Objects Using Grid")
        box.operator(OBJECT_OT_compact_grid.bl_idname, text="Compact Grid (Remove Unused)")
        box.operator(OBJECT_OT_export_grid_png.bl_idname, text="Export Textures (PNG)")

        box = layout.box()
        box.label(text="Reverse", icon='MATERIAL')
        box.operator(OBJECT_OT_restore_materials_from_grid.bl_idname, text="Restore Materials")


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
    VIEW3D_PT_color_grid,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.mcg_settings = bpy.props.PointerProperty(type=MCGSettings)
    bpy.types.VIEW3D_MT_object.append(menu_func)


def unregister():
    bpy.types.VIEW3D_MT_object.remove(menu_func)
    del bpy.types.Scene.mcg_settings
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
