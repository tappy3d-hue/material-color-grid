bl_info = {
    "name": "Material Color Grid Texture",
    "author": "Claude",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Object Menu / F3 Search",
    "description": "Pool base colors from all selected objects into one shared "
                   "grid texture and material (meshes stay separate), and store "
                   "original material assignment as vertex groups.",
    "category": "Material",
}

import bpy
import math


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def linear_to_srgb_channel(c):
    """Convert a single linear channel to sRGB."""
    if c < 0.0:
        return 0.0
    if c <= 0.0031308:
        return 12.92 * c
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


def get_base_color(mat):
    """Get Base Color from Principled BSDF (linear RGBA). Fallback to viewport diffuse."""
    if mat.use_nodes and mat.node_tree:
        # Prefer the node connected to Material Output's surface
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


def create_grid_image(name, colors_linear, resolution=512):
    """
    Create a packed image with a grid of solid colors.

    colors_linear: list of (r,g,b,a) in linear space.
    Returns: (image, uv_centers, cols, rows)
        uv_centers[i] = (u, v) center of the i-th cell, in UV space (0..1)
    """
    n = len(colors_linear)
    cols, rows = calculate_grid(n)

    width = resolution
    height = resolution

    # Remove existing image with same name to avoid orphan accumulation
    if name in bpy.data.images:
        bpy.data.images.remove(bpy.data.images[name])

    img = bpy.data.images.new(name, width=width, height=height, alpha=True)
    img.colorspace_settings.name = 'sRGB'

    # Initialize all pixels to transparent black
    pixels = [0.0] * (width * height * 4)

    # Convert colors to sRGB-encoded values so that when Blender samples this
    # sRGB image and decodes it back to linear, we recover the original linear
    # base color used by Principled BSDF.
    encoded_colors = []
    for (r, g, b, a) in colors_linear:
        encoded_colors.append((
            linear_to_srgb_channel(r),
            linear_to_srgb_channel(g),
            linear_to_srgb_channel(b),
            a,
        ))

    for idx, color in enumerate(encoded_colors):
        col = idx % cols
        row = idx // cols  # row 0 = top

        # Blender image origin is bottom-left, so flip row
        row_from_bottom = rows - 1 - row

        x_start = (col * width) // cols
        x_end = ((col + 1) * width) // cols
        y_start = (row_from_bottom * height) // rows
        y_end = ((row_from_bottom + 1) * height) // rows

        r, g, b, a = color
        for y in range(y_start, y_end):
            row_offset = y * width * 4
            for x in range(x_start, x_end):
                i = row_offset + x * 4
                pixels[i]     = r
                pixels[i + 1] = g
                pixels[i + 2] = b
                pixels[i + 3] = a

    img.pixels = pixels
    img.update()
    img.pack()

    uv_centers = []
    for idx in range(n):
        col = idx % cols
        row = idx // cols
        u = (col + 0.5) / cols
        v = (rows - 1 - row + 0.5) / rows
        uv_centers.append((u, v))

    return img, uv_centers, cols, rows


def build_grid_material(name, image):
    """Build a simple material using Principled BSDF + Image Texture (Closest)."""
    if name in bpy.data.materials:
        bpy.data.materials.remove(bpy.data.materials[name])

    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')

    tex = nodes.new(type='ShaderNodeTexImage')
    tex.image = image
    tex.interpolation = 'Closest'  # sharp cell boundaries
    tex.location = (-340, 300)

    if bsdf is not None:
        links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])

    return mat


# ----------------------------------------------------------------------------
# Operator
# ----------------------------------------------------------------------------

class OBJECT_OT_material_color_grid(bpy.types.Operator):
    """Create a grid color texture from this object's materials and assign it"""
    bl_idname = "object.material_color_grid"
    bl_label = "Material Color Grid Texture"
    bl_options = {'REGISTER', 'UNDO'}

    resolution: bpy.props.IntProperty(
        name="Resolution",
        description="Output texture resolution (square)",
        default=512,
        min=16,
        max=8192,
    )

    create_vertex_groups: bpy.props.BoolProperty(
        name="Create Vertex Groups",
        description="Create a vertex group per original material so you can re-select faces later",
        default=True,
    )

    remap_uvs: bpy.props.BoolProperty(
        name="Remap UVs to Color Cells",
        description="Create a new UV map where each face is mapped to its original material's color cell "
                    "(useful so the new material visually matches the original; you can re-UV later)",
        default=True,
    )

    replace_materials: bpy.props.BoolProperty(
        name="Replace Material Slots",
        description="Remove all existing material slots and assign only the new grid material",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        # ---- Gather all selected mesh objects ----
        mesh_objects = [o for o in context.selected_objects if o.type == 'MESH']
        if not mesh_objects:
            self.report({'ERROR'}, "No mesh objects selected")
            return {'CANCELLED'}

        # ---- Build a GLOBAL unique material list across ALL selected objects ----
        unique_mats = []          # ordered list of materials (shared across objects)
        mat_name_to_index = {}    # material name -> index in unique_mats
        for obj in mesh_objects:
            for slot in obj.material_slots:
                mat = slot.material
                if mat is None or mat.name in mat_name_to_index:
                    continue
                mat_name_to_index[mat.name] = len(unique_mats)
                unique_mats.append(mat)

        if not unique_mats:
            self.report({'ERROR'}, "No valid materials found on selected objects")
            return {'CANCELLED'}

        # ---- Build ONE shared texture + ONE shared material ----
        colors = [get_base_color(m) for m in unique_mats]
        img_name = "SharedColorGrid"
        image, uv_centers, cols, rows = create_grid_image(
            img_name, colors, self.resolution
        )
        new_mat_name = "SharedColorGridMat"
        new_mat = build_grid_material(new_mat_name, image)

        # ---- Apply to each object (geometry stays separate) ----
        processed_meshes = set()  # guard against linked-duplicate meshes
        for obj in mesh_objects:
            mesh = obj.data

            # slot index -> global unique material index (for this object)
            slot_to_uniq = {}
            for slot_idx, slot in enumerate(obj.material_slots):
                mat = slot.material
                if mat is None:
                    continue
                slot_to_uniq[slot_idx] = mat_name_to_index[mat.name]

            # Mesh-level edits (UVs, material slots) only once per shared mesh datablock
            mesh_already_done = mesh.name in processed_meshes

            # ---- Vertex groups (per object, by material name) ----
            if self.create_vertex_groups:
                mat_to_verts = {}
                for poly in mesh.polygons:
                    uniq_idx = slot_to_uniq.get(poly.material_index)
                    if uniq_idx is None:
                        continue
                    name = unique_mats[uniq_idx].name
                    mat_to_verts.setdefault(name, set()).update(poly.vertices)

                for mat_name, verts in mat_to_verts.items():
                    existing = obj.vertex_groups.get(mat_name)
                    if existing is not None:
                        obj.vertex_groups.remove(existing)
                    vg = obj.vertex_groups.new(name=mat_name)
                    vg.add(list(verts), 1.0, 'REPLACE')

            if not mesh_already_done:
                # ---- Remap UVs (before clearing materials) ----
                if self.remap_uvs:
                    while mesh.uv_layers:
                        mesh.uv_layers.remove(mesh.uv_layers[0])
                    uv_layer = mesh.uv_layers.new(name="ColorGridUV")
                    mesh.uv_layers.active = uv_layer
                    uv_layer.active_render = True

                    for poly in mesh.polygons:
                        uniq_idx = slot_to_uniq.get(poly.material_index)
                        uv = uv_centers[uniq_idx] if uniq_idx is not None else (0.5, 0.5)
                        for loop_idx in poly.loop_indices:
                            uv_layer.data[loop_idx].uv = uv

                # ---- Assign shared material ----
                if self.replace_materials:
                    mesh.materials.clear()
                    mesh.materials.append(new_mat)
                    for poly in mesh.polygons:
                        poly.material_index = 0
                else:
                    if new_mat.name not in [m.name for m in mesh.materials if m]:
                        mesh.materials.append(new_mat)

                processed_meshes.add(mesh.name)

        self.report(
            {'INFO'},
            f"Grid {cols}x{rows} ({len(unique_mats)} colors, "
            f"{cols * rows - len(unique_mats)} empty) applied to "
            f"{len(mesh_objects)} object(s) -> '{new_mat_name}'"
        )
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

def menu_func(self, context):
    self.layout.separator()
    self.layout.operator(
        OBJECT_OT_material_color_grid.bl_idname,
        icon='TEXTURE',
    )


classes = (
    OBJECT_OT_material_color_grid,
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
