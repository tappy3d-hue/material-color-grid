bl_info = {
    "name": "Material Color Grid Texture",
    "author": "Claude",
    "version": (1, 6, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N) > Color Grid tab",
    "description": "Pool base colors from selected objects into one shared grid "
                   "texture and material (meshes stay separate). Re-running adds "
                   "new colors while preserving previously baked ones via a stored "
                   "manifest. Can also restore per-color materials from the grid. "
                   "Original material assignment is saved as vertex groups.",
    "category": "Material",
}

import bpy
import math
import json

DEFAULT_GROUP_NAME = "ColorGrid"
MANIFEST_KEY = "mcg_manifest"   # custom property on the shared material
UV_LAYER_NAME = "ColorGridUV"


# ----------------------------------------------------------------------------
# Color / grid helpers
# ----------------------------------------------------------------------------

def linear_to_srgb_channel(c):
    if c < 0.0:
        return 0.0
    if c <= 0.0031308:
        return 12.92 * c
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


def get_base_color(mat):
    """Get Base Color from Principled BSDF (linear RGBA). Fallback to viewport diffuse."""
    if mat.use_nodes and mat.node_tree:
        for node in mat.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                base_input = node.inputs.get("Base Color")
                if base_input is not None:
                    v = base_input.default_value
                    return (v[0], v[1], v[2], v[3] if len(v) > 3 else 1.0)
    dc = mat.diffuse_color
    return (dc[0], dc[1], dc[2], 1.0)


def calculate_grid(n):
    """Return (cols, rows) for n cells. Uses ceil(sqrt(n)) x ceil(n/cols)."""
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
    """Inverse of cell_uv_center: which cell does this UV fall in."""
    col = min(int(u * cols), cols - 1)
    col = max(col, 0)
    row_from_bottom = min(int(v * rows), rows - 1)
    row_from_bottom = max(row_from_bottom, 0)
    row = rows - 1 - row_from_bottom
    return row * cols + col


# ----------------------------------------------------------------------------
# Image / material helpers
# ----------------------------------------------------------------------------

def get_material_image(mat):
    """Return the image used by the material's first Image Texture node, or None."""
    if mat.use_nodes and mat.node_tree:
        for n in mat.node_tree.nodes:
            if n.type == 'TEX_IMAGE' and n.image is not None:
                return n.image
    return None


def new_grid_image(name, width, height):
    """Create a fresh sRGB image; Blender auto-uniquifies the name if taken."""
    img = bpy.data.images.new(name, width=width, height=height, alpha=True)
    img.colorspace_settings.name = 'sRGB'
    return img


def write_grid_pixels(img, colors_linear):
    """Paint the grid of solid colors onto the image. Returns (uv_centers, cols, rows)."""
    width, height = img.size
    n = len(colors_linear)
    cols, rows = calculate_grid(n)

    pixels = [0.0] * (width * height * 4)
    encoded = [(
        linear_to_srgb_channel(r),
        linear_to_srgb_channel(g),
        linear_to_srgb_channel(b),
        a,
    ) for (r, g, b, a) in colors_linear]

    for idx, (r, g, b, a) in enumerate(encoded):
        col = idx % cols
        row = idx // cols
        row_from_bottom = rows - 1 - row
        x0 = (col * width) // cols
        x1 = ((col + 1) * width) // cols
        y0 = (row_from_bottom * height) // rows
        y1 = ((row_from_bottom + 1) * height) // rows
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
    img.pack()

    uv_centers = [cell_uv_center(i, cols, rows) for i in range(n)]
    return uv_centers, cols, rows


def get_or_make_tex_node(mat, image):
    """Ensure the material has an Image Texture node feeding Base Color, pointing at image."""
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        for node in nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bsdf = node
                break
    if bsdf is None:
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')

    tex = None
    for node in nodes:
        if node.type == 'TEX_IMAGE':
            tex = node
            break
    if tex is None:
        tex = nodes.new(type='ShaderNodeTexImage')
        tex.location = (-340, 300)

    tex.image = image
    tex.interpolation = 'Closest'
    links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
    return tex


def find_shared_material_from(objs):
    """Return the shared grid material (carrying a manifest) used by any of objs, or None."""
    for obj in objs:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is not None and MANIFEST_KEY in mat:
                return mat
    return None


def read_manifest(mat):
    """Return ordered list of {'name':..., 'color':[r,g,b,a]} from material, or []."""
    raw = mat.get(MANIFEST_KEY)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def write_manifest(mat, manifest):
    mat[MANIFEST_KEY] = json.dumps(manifest)


def objects_using_material(mat):
    """All mesh objects in the file whose mesh references mat."""
    result = []
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        if any(m == mat for m in obj.data.materials):
            result.append(obj)
    return result


# ----------------------------------------------------------------------------
# Panel settings (persistent, shown in the sidebar)
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
        description="Make a tiny texture sized to the grid (cells x Cell Pixels) instead of a "
                    "square Resolution. Great for solid-color grids destined for Roblox",
        default=False,
    )
    cell_pixels: bpy.props.IntProperty(
        name="Cell Pixels",
        description="Pixels per cell when Minimal Resolution is on",
        default=8, min=1, max=256,
    )
    create_vertex_groups: bpy.props.BoolProperty(name="Vertex Groups", default=True)
    remap_uvs: bpy.props.BoolProperty(name="Remap UVs", default=True)
    replace_materials: bpy.props.BoolProperty(name="Replace Slots", default=True)
    sync_all_users: bpy.props.BoolProperty(name="Update All Users", default=True)


# ----------------------------------------------------------------------------
# Operator
# ----------------------------------------------------------------------------

class OBJECT_OT_material_color_grid(bpy.types.Operator):
    """Bake selected objects' material base colors into one shared grid texture"""
    bl_idname = "object.material_color_grid"
    bl_label = "Material Color Grid Texture"
    bl_options = {'REGISTER', 'UNDO'}

    resolution: bpy.props.IntProperty(
        name="Resolution",
        description="Output texture resolution (square)",
        default=512, min=16, max=8192,
    )
    group_name: bpy.props.StringProperty(
        name="Group Name",
        description="Name for a NEW grid's texture and material. Ignored when updating "
                    "an existing grid (the selected object's existing texture is reused). "
                    "If the name already exists, a number suffix is added automatically",
        default=DEFAULT_GROUP_NAME,
    )
    minimal_resolution: bpy.props.BoolProperty(
        name="Minimal Resolution",
        description="Size the texture to the grid (cells x Cell Pixels) instead of a square Resolution",
        default=False,
    )
    cell_pixels: bpy.props.IntProperty(
        name="Cell Pixels",
        description="Pixels per cell when Minimal Resolution is on",
        default=8, min=1, max=256,
    )
    create_vertex_groups: bpy.props.BoolProperty(
        name="Create Vertex Groups",
        description="Create a vertex group per original material so you can re-select faces later",
        default=True,
    )
    remap_uvs: bpy.props.BoolProperty(
        name="Remap UVs to Color Cells",
        description="Map each face to its material's color cell (replaces existing UV maps)",
        default=True,
    )
    replace_materials: bpy.props.BoolProperty(
        name="Replace Material Slots",
        description="Remove existing material slots and assign only the shared grid material",
        default=True,
    )
    sync_all_users: bpy.props.BoolProperty(
        name="Update All Objects Using Texture",
        description="When updating an existing grid, remap UVs of ALL objects in the file that use "
                    "the shared texture (not just selected ones), so their colors stay correct after "
                    "the grid layout changes",
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

        # ---- Detect existing shared grid (update mode) ----
        shared_mat = find_shared_material_from(sel)
        manifest = read_manifest(shared_mat) if shared_mat else []
        old_count = len(manifest)
        old_cols, old_rows = calculate_grid(old_count) if old_count else (1, 1)

        name_to_index = {e["name"]: i for i, e in enumerate(manifest)}

        # ---- Classify selected objects ----
        existing_objs = []  # already use the shared material
        source_objs = []    # carry original (to-be-added) materials
        for obj in sel:
            uses_shared = shared_mat is not None and any(
                slot.material == shared_mat for slot in obj.material_slots
            )
            (existing_objs if uses_shared else source_objs).append(obj)

        # ---- Merge new colors from source objects into manifest ----
        for obj in source_objs:
            for slot in obj.material_slots:
                mat = slot.material
                if mat is None or mat == shared_mat:
                    continue
                color = list(get_base_color(mat))
                if mat.name in name_to_index:
                    manifest[name_to_index[mat.name]]["color"] = color  # refresh
                else:
                    name_to_index[mat.name] = len(manifest)
                    manifest.append({"name": mat.name, "color": color})

        if not manifest:
            self.report({'ERROR'}, "No valid materials found on selected objects")
            return {'CANCELLED'}

        # ---- Resolve target texture dimensions ----
        n = len(manifest)
        grid_cols, grid_rows = calculate_grid(n)
        if self.minimal_resolution:
            target_w = grid_cols * self.cell_pixels
            target_h = grid_rows * self.cell_pixels
        else:
            target_w = target_h = self.resolution

        # ---- Resolve the texture image and shared material ----
        if shared_mat is None:
            # Fresh grid: create uniquely-named image + material from group_name.
            name = (self.group_name or DEFAULT_GROUP_NAME).strip() or DEFAULT_GROUP_NAME
            img = new_grid_image(name, target_w, target_h)
            shared_mat = bpy.data.materials.new(name=name + "_Mat")
        else:
            # Update mode: reuse the material's existing image.
            img = get_material_image(shared_mat)
            if img is None:
                img = new_grid_image(shared_mat.name + "_Tex", target_w, target_h)
            elif self.minimal_resolution and (img.size[0] != target_w or img.size[1] != target_h):
                # In minimal mode, keep the image sized exactly to the grid.
                img.scale(target_w, target_h)

        colors_linear = [tuple(e["color"]) for e in manifest]
        uv_centers, new_cols, new_rows = write_grid_pixels(img, colors_linear)

        get_or_make_tex_node(shared_mat, img)
        write_manifest(shared_mat, manifest)

        # ---- Remap EXISTING textured objects (old layout -> new layout) ----
        # Index order is preserved, so we only need to re-place by cell index.
        layout_changed = (new_cols, new_rows) != (old_cols, old_rows)
        if old_count and layout_changed:
            targets = (objects_using_material(shared_mat)
                       if self.sync_all_users else existing_objs)
            done_meshes = set()
            for obj in targets:
                mesh = obj.data
                if mesh.name in done_meshes:
                    continue
                done_meshes.add(mesh.name)
                uv_layer = mesh.uv_layers.get(UV_LAYER_NAME) or mesh.uv_layers.active
                if uv_layer is None:
                    continue
                for poly in mesh.polygons:
                    li0 = poly.loop_indices[0]
                    u, v = uv_layer.data[li0].uv
                    idx = uv_to_cell_index(u, v, old_cols, old_rows)
                    idx = min(idx, len(uv_centers) - 1)
                    new_uv = uv_centers[idx]
                    for loop_idx in poly.loop_indices:
                        uv_layer.data[loop_idx].uv = new_uv

        # ---- Apply to SOURCE objects (fresh bake) ----
        processed = set()
        for obj in source_objs:
            mesh = obj.data

            slot_to_idx = {}
            for slot_idx, slot in enumerate(obj.material_slots):
                mat = slot.material
                if mat is None or mat == shared_mat:
                    continue
                slot_to_idx[slot_idx] = name_to_index[mat.name]

            # vertex groups (per object, by material name)
            if self.create_vertex_groups:
                mat_to_verts = {}
                for poly in mesh.polygons:
                    if poly.material_index in slot_to_idx:
                        mat_name = manifest[slot_to_idx[poly.material_index]]["name"]
                        mat_to_verts.setdefault(mat_name, set()).update(poly.vertices)
                for mat_name, verts in mat_to_verts.items():
                    ex = obj.vertex_groups.get(mat_name)
                    if ex is not None:
                        obj.vertex_groups.remove(ex)
                    vg = obj.vertex_groups.new(name=mat_name)
                    vg.add(list(verts), 1.0, 'REPLACE')

            if mesh.name in processed:
                continue
            processed.add(mesh.name)

            if self.remap_uvs:
                while mesh.uv_layers:
                    mesh.uv_layers.remove(mesh.uv_layers[0])
                uv_layer = mesh.uv_layers.new(name=UV_LAYER_NAME)
                mesh.uv_layers.active = uv_layer
                uv_layer.active_render = True
                for poly in mesh.polygons:
                    idx = slot_to_idx.get(poly.material_index)
                    uv = uv_centers[idx] if idx is not None else (0.5, 0.5)
                    for loop_idx in poly.loop_indices:
                        uv_layer.data[loop_idx].uv = uv

            if self.replace_materials:
                mesh.materials.clear()
                mesh.materials.append(shared_mat)
                for poly in mesh.polygons:
                    poly.material_index = 0
            else:
                if shared_mat.name not in [m.name for m in mesh.materials if m]:
                    mesh.materials.append(shared_mat)

        mode = "Updated" if old_count else "Created"
        self.report(
            {'INFO'},
            f"{mode} grid {new_cols}x{new_rows} ({len(manifest)} colors, "
            f"+{len(manifest) - old_count} new) on {len(source_objs)} new / "
            f"{len(existing_objs)} existing object(s)"
        )
        return {'FINISHED'}


class OBJECT_OT_restore_materials_from_grid(bpy.types.Operator):
    """Rebuild per-color materials from the grid texture (reverse of baking)"""
    bl_idname = "object.restore_materials_from_grid"
    bl_label = "Restore Materials From Grid"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def _build_material(self, entry):
        """Get/create a material named entry['name'] with its base color set."""
        name = entry["name"]
        col = entry["color"]
        rgba = (col[0], col[1], col[2], col[3] if len(col) > 3 else 1.0)

        mat = bpy.data.materials.get(name)
        if mat is None:
            mat = bpy.data.materials.new(name=name)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf is None:
            for n in mat.node_tree.nodes:
                if n.type == 'BSDF_PRINCIPLED':
                    bsdf = n
                    break
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = rgba
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

        mat_cache = {}   # cell index -> material (shared across objects)
        processed = set()
        count = 0

        for obj in sel:
            if not any(slot.material == shared_mat for slot in obj.material_slots):
                continue
            mesh = obj.data
            if mesh.name in processed:
                continue
            processed.add(mesh.name)

            uv_layer = mesh.uv_layers.get(UV_LAYER_NAME) or mesh.uv_layers.active
            if uv_layer is None:
                continue

            # Determine which color cell each face maps to
            face_cell = []
            used_cells = set()
            for poly in mesh.polygons:
                u, v = uv_layer.data[poly.loop_indices[0]].uv
                idx = min(uv_to_cell_index(u, v, cols, rows), len(manifest) - 1)
                face_cell.append(idx)
                used_cells.add(idx)

            # Build this object's slot list from the cells it actually uses
            used_sorted = sorted(used_cells)
            cell_to_slot = {cell: i for i, cell in enumerate(used_sorted)}

            mesh.materials.clear()
            for cell in used_sorted:
                if cell not in mat_cache:
                    mat_cache[cell] = self._build_material(manifest[cell])
                mesh.materials.append(mat_cache[cell])

            for poly, cell in zip(mesh.polygons, face_cell):
                poly.material_index = cell_to_slot[cell]

            count += 1

        self.report({'INFO'}, f"Restored materials on {count} object(s)")
        return {'FINISHED'}


class OBJECT_OT_rename_grid(bpy.types.Operator):
    """Rename the grid texture and material used by the selected objects"""
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

        img = get_material_image(mat)
        mat.name = name + "_Mat"
        if img is not None:
            img.name = name
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


class OBJECT_OT_export_grid_png(bpy.types.Operator):
    """Save the grid texture to a PNG file (e.g. to upload to Roblox)"""
    bl_idname = "object.export_color_grid_png"
    bl_label = "Export Grid Texture (PNG)"
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filename_ext = ".png"

    reference_after_export: bpy.props.BoolProperty(
        name="Reference Exported File (Unpack)",
        description="After saving, point the image at the exported PNG and unpack it so it is no "
                    "longer embedded in the .blend. This makes FBX export reference a real file on "
                    "disk, which fixes textures not loading in Roblox Studio",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def _get_image(self, context):
        sel = [o for o in context.selected_objects if o.type == 'MESH']
        mat = find_shared_material_from(sel)
        if mat is None:
            return None
        return get_material_image(mat)

    def invoke(self, context, event):
        img = self._get_image(context)
        if img is None:
            self.report({'ERROR'}, "Selected objects don't use a grid material with a texture")
            return {'CANCELLED'}
        self.filepath = (img.name or "ColorGrid") + ".png"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        img = self._get_image(context)
        if img is None:
            self.report({'ERROR'}, "No grid texture found")
            return {'CANCELLED'}
        path = self.filepath
        if not path.lower().endswith(".png"):
            path += ".png"

        img.filepath_raw = path
        img.file_format = 'PNG'
        try:
            img.save()
        except RuntimeError as e:
            self.report({'ERROR'}, f"Save failed: {e}")
            return {'CANCELLED'}

        if self.reference_after_export:
            # Make the image an external file reference (the FBX exporter needs this).
            img.filepath = path
            img.source = 'FILE'
            if img.packed_file is not None:
                try:
                    img.unpack(method='REMOVE')
                except RuntimeError:
                    pass
            note = " (unpacked, now references the file)"
        else:
            note = ""

        self.report({'INFO'}, f"Saved texture to {path}{note}")
        return {'FINISHED'}


class OBJECT_OT_compact_grid(bpy.types.Operator):
    """Remove color cells no longer used by any object, then re-pack the grid"""
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
        img = get_material_image(mat)
        if img is None:
            self.report({'ERROR'}, "No texture image found")
            return {'CANCELLED'}

        # Scan every object using this grid: which cells are actually used?
        users = objects_using_material(mat)
        used = set()
        per_obj = {}  # mesh name -> (uv_layer, [(poly, old_idx)])
        for obj in users:
            mesh = obj.data
            if mesh.name in per_obj:
                continue
            uv_layer = mesh.uv_layers.get(UV_LAYER_NAME) or mesh.uv_layers.active
            if uv_layer is None:
                continue
            faces = []
            for poly in mesh.polygons:
                u, v = uv_layer.data[poly.loop_indices[0]].uv
                idx = min(uv_to_cell_index(u, v, old_cols, old_rows), len(manifest) - 1)
                used.add(idx)
                faces.append((poly, idx))
            per_obj[mesh.name] = (uv_layer, faces)

        if not used:
            self.report({'ERROR'}, "No objects with grid UVs found")
            return {'CANCELLED'}

        removed = len(manifest) - len(used)
        if removed <= 0:
            self.report({'INFO'}, "No unused colors to remove")
            return {'FINISHED'}

        # Build compacted manifest, preserving relative order
        old_to_new = {}
        new_manifest = []
        for old_idx in sorted(used):
            old_to_new[old_idx] = len(new_manifest)
            new_manifest.append(manifest[old_idx])

        # Repaint and remap
        colors_linear = [tuple(e["color"]) for e in new_manifest]
        uv_centers, _, _ = write_grid_pixels(img, colors_linear)
        write_manifest(mat, new_manifest)

        for uv_layer, faces in per_obj.values():
            for poly, old_idx in faces:
                new_uv = uv_centers[old_to_new[old_idx]]
                for li in poly.loop_indices:
                    uv_layer.data[li].uv = new_uv

        self.report({'INFO'}, f"Removed {removed} unused color(s); {len(new_manifest)} remain")
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
        if s.minimal_resolution:
            box.prop(s, "cell_pixels")
        else:
            box.prop(s, "resolution")
        col = box.column(align=True)
        col.prop(s, "create_vertex_groups")
        col.prop(s, "remap_uvs")
        col.prop(s, "replace_materials")
        col.prop(s, "sync_all_users")
        op = box.operator(OBJECT_OT_material_color_grid.bl_idname, text="Bake Selected to Grid")
        op.group_name = s.grid_name
        op.resolution = s.resolution
        op.minimal_resolution = s.minimal_resolution
        op.cell_pixels = s.cell_pixels
        op.create_vertex_groups = s.create_vertex_groups
        op.remap_uvs = s.remap_uvs
        op.replace_materials = s.replace_materials
        op.sync_all_users = s.sync_all_users

        box = layout.box()
        box.label(text="Tools", icon='TOOL_SETTINGS')
        op = box.operator(OBJECT_OT_rename_grid.bl_idname, text="Rename Current Grid")
        op.new_name = s.grid_name
        box.operator(OBJECT_OT_select_grid_users.bl_idname, text="Select Objects Using Grid")
        box.operator(OBJECT_OT_compact_grid.bl_idname, text="Compact Grid (Remove Unused)")
        box.operator(OBJECT_OT_export_grid_png.bl_idname, text="Export Texture (PNG)")

        box = layout.box()
        box.label(text="Reverse", icon='MATERIAL')
        box.operator(
            OBJECT_OT_restore_materials_from_grid.bl_idname,
            text="Restore Materials",
        )


def menu_func(self, context):
    self.layout.separator()
    self.layout.operator(OBJECT_OT_material_color_grid.bl_idname, icon='TEXTURE')
    self.layout.operator(OBJECT_OT_restore_materials_from_grid.bl_idname, icon='MATERIAL')


classes = (
    MCGSettings,
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
