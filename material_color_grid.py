bl_info = {
    "name": "Material Color Grid Texture",
    "author": "Claude",
    "version": (1, 3, 0),
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

        # ---- Resolve the texture image and shared material ----
        if shared_mat is None:
            # Fresh grid: create uniquely-named image + material from group_name.
            name = (self.group_name or DEFAULT_GROUP_NAME).strip() or DEFAULT_GROUP_NAME
            img = new_grid_image(name, self.resolution, self.resolution)
            shared_mat = bpy.data.materials.new(name=name + "_Mat")
        else:
            # Update mode: reuse the material's existing image (keep its name/size).
            img = get_material_image(shared_mat)
            if img is None:
                img = new_grid_image(shared_mat.name + "_Tex", self.resolution, self.resolution)

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

        box = layout.box()
        box.label(text="Bake / Update", icon='TEXTURE')
        box.operator(
            OBJECT_OT_material_color_grid.bl_idname,
            text="Bake Selected to Grid",
        )
        box.label(text="Select objects, then bake.", icon='INFO')

        box = layout.box()
        box.label(text="Reverse", icon='MATERIAL')
        box.operator(
            OBJECT_OT_restore_materials_from_grid.bl_idname,
            text="Restore Materials",
        )
        box.label(text="Rebuild per-color materials.", icon='INFO')


def menu_func(self, context):
    self.layout.separator()
    self.layout.operator(OBJECT_OT_material_color_grid.bl_idname, icon='TEXTURE')
    self.layout.operator(OBJECT_OT_restore_materials_from_grid.bl_idname, icon='MATERIAL')


classes = (
    OBJECT_OT_material_color_grid,
    OBJECT_OT_restore_materials_from_grid,
    VIEW3D_PT_color_grid,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.VIEW3D_MT_object.append(menu_func)


def unregister():
    bpy.types.VIEW3D_MT_object.remove(menu_func)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
