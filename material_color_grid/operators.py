import bpy

import math

import json

import os

import random

import urllib.request

import urllib.error

import ssl

from .core import *  # helpers, constants
from .properties import MCGKeepColor, MCGSettings, MCGAddonPreferences

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

        # Optional: auto-export atlas PNGs to the configured folder and reference them.
        exported_note = ""
        if self.auto_export:
            out_dir = resolve_export_dir(context)
            if not out_dir:
                self.report({'WARNING'}, "Could not resolve a texture export folder")
            else:
                n = 0
                for key in ("color", "roughness", "metallic"):
                    img = images.get(key)
                    if img is None:
                        continue
                    try:
                        export_image_to(img, os.path.join(out_dir, img.name + ".png"))
                        n += 1
                    except (RuntimeError, OSError):
                        pass
                exported_note = f"; exported {n} PNG(s)"

        mode = "Updated" if old_count else "Created"
        maps = "+".join(k for k in ("color", "roughness", "metallic") if k in images)
        extra = f"; skipped modes: {', '.join(other_modes)}" if other_modes else ""
        self.report({'INFO'},
                    f"{mode} atlas {S}x{S} ({len(manifest)} cells, "
                    f"+{len(manifest) - old_count} new) maps: {maps}{extra}{exported_note}")
        return {'FINISHED'}

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

class OBJECT_OT_export_fbx_roblox(bpy.types.Operator):
    """Export selected objects as an FBX with Roblox-recommended settings"""
    bl_idname = "object.mcg_export_fbx_roblox"
    bl_label = "Export FBX for Roblox"
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filename_ext = ".fbx"

    apply_scale_001: bpy.props.BoolProperty(
        name="Scale 0.01 (Blender→Studs)",
        description="Export at 0.01 so 1 m in Blender imports at the expected size in Studio",
        default=True,
    )
    apply_transform_before: bpy.props.BoolProperty(
        name="Apply Transforms First",
        description="Apply Location/Rotation/Scale on the objects before export",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def invoke(self, context, event):
        if not any(o.type == 'MESH' for o in context.selected_objects):
            self.report({'ERROR'}, "Select mesh objects to export")
            return {'CANCELLED'}
        blend = bpy.data.filepath
        base = os.path.splitext(os.path.basename(blend))[0] if blend else "roblox_export"
        self.filepath = base + ".fbx"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        path = self.filepath
        if not path.lower().endswith(".fbx"):
            path += ".fbx"

        if self.apply_transform_before:
            try:
                bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
            except RuntimeError:
                pass  # e.g. multi-user data; export still proceeds

        # Roblox-recommended FBX settings (per Roblox creator docs):
        #  Path Mode = COPY + embed textures, apply scalings to FBX unit scale,
        #  no leaf bones, no animation (static assets).
        kwargs = dict(
            filepath=path,
            use_selection=True,
            path_mode='COPY',
            embed_textures=True,
            apply_scale_options='FBX_SCALE_UNITS',
            add_leaf_bones=False,
            bake_anim=False,
            mesh_smooth_type='FACE',
            use_mesh_modifiers=True,
        )
        if self.apply_scale_001:
            kwargs["global_scale"] = 0.01

        try:
            bpy.ops.export_scene.fbx(**kwargs)
        except (RuntimeError, TypeError) as e:
            self.report({'ERROR'}, f"FBX export failed: {e}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Exported FBX: {os.path.basename(path)}")
        return {'FINISHED'}

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

class OBJECT_OT_bake_vertex_colors(bpy.types.Operator):
    """Bake Base Color into vertex colors for VERTEX-mode objects"""
    bl_idname = "object.bake_vertex_colors"
    bl_label = "Bake Material Color to Vertex Colors"
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

class MCG_OT_keep_color_remove(bpy.types.Operator):
    """Remove a kept color"""
    bl_idname = "mcg.keep_color_remove"
    bl_label = "Remove Kept Color"
    index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        s = context.scene.mcg_settings
        if 0 <= self.index < len(s.keep_colors):
            s.keep_colors.remove(self.index)
        return {'FINISHED'}

class MCG_OT_keep_color_clear(bpy.types.Operator):
    """Remove all kept colors"""
    bl_idname = "mcg.keep_color_clear"
    bl_label = "Clear Kept Colors"

    def execute(self, context):
        context.scene.mcg_settings.keep_colors.clear()
        return {'FINISHED'}

class MCG_OT_pick_keep_color(bpy.types.Operator):
    """Eyedropper: click faces in the 3D view to sample their texture color to keep.
    The viewport is shown flat/textured while picking. Right-click or Esc to finish."""
    bl_idname = "mcg.pick_keep_color"
    bl_label = "Pick Colors from Texture"

    _saved = None

    def _sample_face(self, context, mx, my):
        from bpy_extras import view3d_utils
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None:
            return None
        coord = (mx, my)
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        depsgraph = context.evaluated_depsgraph_get()
        hit, loc, nrm, face_index, obj, mat = context.scene.ray_cast(
            depsgraph, origin, direction)
        if not hit or obj is None:
            return None
        obj = obj.original
        if obj.type != 'MESH':
            return None
        mesh = obj.data
        if face_index < 0 or face_index >= len(mesh.polygons):
            return None
        uv_layer = mesh.uv_layers.active
        if uv_layer is None:
            return None
        poly = mesh.polygons[face_index]
        if poly.material_index >= len(obj.material_slots):
            return None
        slot = obj.material_slots[poly.material_index]
        m = slot.material
        if m is None or not material_has_image_basecolor(m):
            return None
        img = get_basecolor_image(m)
        if img is None:
            return None
        buf_info = get_image_buffer({}, img)
        if buf_info is None:
            return None
        s = context.scene.mcg_settings
        return face_representative_color(buf_info, uv_layer, poly,
                                         s.detect_sample_density, s.detect_aggregate)

    _hover_color = None

    def _tag_redraw(self, context):
        wm = context.window_manager
        for win in wm.windows:
            for area in win.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
                    for region in area.regions:
                        if region.type == 'UI':
                            region.tag_redraw()

    def modal(self, context, event):
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}
        if event.type == 'MOUSEMOVE':
            color = self._sample_face(context, event.mouse_region_x, event.mouse_region_y)
            self._hover_color = color
            if context.area:
                if color is not None:
                    r, g, b = color_to_rgb255(color)
                    n = len(context.scene.mcg_settings.keep_colors)
                    context.area.header_text_set(
                        f"Hover: #{r:02X}{g:02X}{b:02X}  ({n} kept)  —  "
                        f"click to add, right-click / Esc to finish")
                else:
                    n = len(context.scene.mcg_settings.keep_colors)
                    context.area.header_text_set(
                        f"No textured face under cursor  ({n} kept)  —  "
                        f"right-click / Esc to finish")
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            color = self._hover_color
            if color is None:
                color = self._sample_face(context, event.mouse_region_x, event.mouse_region_y)
            if color is not None:
                item = context.scene.mcg_settings.keep_colors.add()
                item.color = (color[0], color[1], color[2])
                r, g, b = color_to_rgb255(color)
                n = len(context.scene.mcg_settings.keep_colors)
                if context.area:
                    context.area.header_text_set(
                        f"Added #{r:02X}{g:02X}{b:02X}  ({n} kept)  —  "
                        f"click more, right-click / Esc to finish")
                self._tag_redraw(context)
            return {'RUNNING_MODAL'}
        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._restore(context)
            self._tag_redraw(context)
            return {'FINISHED'}
        return {'RUNNING_MODAL'}

    def _restore(self, context):
        try:
            context.window.cursor_modal_restore()
        except (AttributeError, ReferenceError):
            pass
        if context.area:
            context.area.header_text_set(None)
        if self._saved is not None:
            space, stype, light, ctype = self._saved
            try:
                space.shading.type = stype
                space.shading.light = light
                space.shading.color_type = ctype
            except (AttributeError, ReferenceError):
                pass

    def invoke(self, context, event):
        if context.space_data is None or context.space_data.type != 'VIEW_3D':
            self.report({'ERROR'}, "Run this in a 3D Viewport")
            return {'CANCELLED'}
        space = context.space_data
        try:
            sh = space.shading
            self._saved = (space, sh.type, sh.light, sh.color_type)
            sh.type = 'SOLID'
            sh.light = 'FLAT'
            sh.color_type = 'TEXTURE'
        except AttributeError:
            self._saved = None
        context.window.cursor_modal_set('EYEDROPPER')
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, "Hover to preview, click to add. Right-click / Esc to finish.")
        return {'RUNNING_MODAL'}

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
    snap_all_to_kept: bpy.props.BoolProperty(
        name="Snap All to Kept Colors", default=False)

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def _keep_colors(self, context):
        return [(kc.color[0], kc.color[1], kc.color[2])
                for kc in context.scene.mcg_settings.keep_colors]

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
            # Keep-colors: faces within snap distance of a kept color are forced to
            # that exact color and excluded from clustering.
            # Keep Colors only apply in Auto (Threshold) mode. In Fixed Count mode
            # the user has asked for an exact number of colors, so honoring both
            # would over-produce clusters.
            keeps = self._keep_colors(context) if self.group_mode == 'AUTO' else []
            snap = self.threshold
            snap_all = (self.group_mode == 'AUTO' and self.snap_all_to_kept and bool(keeps))
            kept_by_keep = {}
            rest = []
            if keeps:
                karr = _np.array(keeps, dtype=_np.float64)
                for r in recs:
                    c = _np.array(r["color"][:3], dtype=_np.float64)
                    d = _np.sqrt(((karr - c) ** 2).sum(axis=1))
                    ki = int(_np.argmin(d))
                    if snap_all or d[ki] <= snap:
                        kept_by_keep.setdefault(ki, []).append(r)
                    else:
                        rest.append(r)
            else:
                rest = recs

            by_obj = {}

            # Materials for kept colors (exact color the user picked).
            for ki, krecs in kept_by_keep.items():
                cnt = len(krecs)
                rgba = (keeps[ki][0], keeps[ki][1], keeps[ki][2],
                        sum(float(r["color"][3]) for r in krecs) / cnt)
                rough = sum(r["rough"] for r in krecs) / cnt
                metal = sum(r["metal"] for r in krecs) / cnt
                kmat = get_or_create_color_material(
                    self.name_prefix, rgba, rough, metal, mat_cache)
                for r in krecs:
                    by_obj.setdefault(r["obj"], []).append((r["poly_index"], kmat))
                total_groups += 1

            if rest:
                colors = _np.array([r["color"][:3] for r in rest], dtype=_np.float64)
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
                for r, lab in zip(rest, labels):
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

                for r, lab in zip(rest, labels):
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

class OBJECT_OT_decimate_to_limit(bpy.types.Operator):
    """Decimate selected meshes that exceed the triangle limit down to it.
    The per-object ratio is auto-computed; fine-tune in the redo panel below."""
    bl_idname = "object.mcg_decimate_to_limit"
    bl_label = "Decimate Over-Limit Meshes"
    bl_options = {'REGISTER', 'UNDO'}

    limit: bpy.props.IntProperty(
        name="Triangle Limit", default=20000, min=100, max=2000000)
    min_ratio: bpy.props.FloatProperty(
        name="Min Ratio", description="Lower bound on how far meshes may be decimated",
        default=0.05, min=0.01, max=1.0)
    apply_modifier: bpy.props.BoolProperty(
        name="Apply Modifier", description="Apply the Decimate modifier (uncheck to keep it live)",
        default=True)

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        done = 0
        for obj in meshes:
            tris = _tri_count(obj.data)
            if tris <= self.limit:
                continue
            ratio = max(self.min_ratio, self.limit / float(tris))
            mod = obj.modifiers.new(name="MCG_Decimate", type='DECIMATE')
            mod.decimate_type = 'COLLAPSE'
            mod.ratio = ratio
            if self.apply_modifier:
                prev = context.view_layer.objects.active
                context.view_layer.objects.active = obj
                try:
                    bpy.ops.object.modifier_apply(modifier=mod.name)
                except RuntimeError:
                    obj.modifiers.remove(mod)
                context.view_layer.objects.active = prev
            done += 1
        if done == 0:
            self.report({'INFO'}, "No meshes exceed the triangle limit")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Decimated {done} mesh(es) toward {self.limit} tris")
        return {'FINISHED'}

class OBJECT_OT_downscale_textures(bpy.types.Operator):
    """Downscale Base Color textures larger than the limit (power-of-two)"""
    bl_idname = "object.mcg_downscale_textures"
    bl_label = "Downscale Oversized Textures"
    bl_options = {'REGISTER', 'UNDO'}

    limit: bpy.props.IntProperty(name="Max Texture", default=1024, min=16, max=8192)

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        seen, done = set(), 0
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            for slot in obj.material_slots:
                if slot.material is None:
                    continue
                img = get_basecolor_image(slot.material)
                if img is None or img.name in seen:
                    continue
                seen.add(img.name)
                w, h = img.size
                if max(w, h) <= self.limit:
                    continue
                scale = self.limit / float(max(w, h))
                nw = max(1, 1 << int(math.floor(math.log2(max(1, int(w * scale))))))
                nh = max(1, 1 << int(math.floor(math.log2(max(1, int(h * scale))))))
                try:
                    img.scale(nw, nh)
                    done += 1
                except RuntimeError:
                    pass
        if done == 0:
            self.report({'INFO'}, "No textures exceed the limit")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Downscaled {done} texture(s) to <= {self.limit}px")
        return {'FINISHED'}
