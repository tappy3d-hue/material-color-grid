import bpy

import math

import json

import os

import random

import urllib.request

import urllib.error

import ssl

from .core import *
from .operators import *
from .properties import MCGSettings

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

        # Keep Colors — palette-style grid of swatches
        keep = box.box()
        keep.enabled = (s.detect_group_mode == 'AUTO')
        krow = keep.row()
        krow.label(text="Keep These Colors", icon='COLOR')
        krow.operator(MCG_OT_pick_keep_color.bl_idname, text="", icon='EYEDROPPER')
        if len(s.keep_colors):
            krow.operator(MCG_OT_keep_color_clear.bl_idname, text="", icon='TRASH')
        if s.detect_group_mode != 'AUTO':
            keep.label(text="Only used in Auto (Threshold) mode.", icon='INFO')
        if len(s.keep_colors):
            grid = keep.grid_flow(row_major=True, columns=6, even_columns=True,
                                  even_rows=True, align=True)
            for i, kc in enumerate(s.keep_colors):
                cell = grid.column(align=True)
                cell.prop(kc, "color", text="")
                cell.operator(MCG_OT_keep_color_remove.bl_idname,
                              text="", icon='X').index = i
        elif s.detect_group_mode == 'AUTO':
            keep.label(text="Use the eyedropper to sample colors to keep.", icon='INFO')
        keep.prop(s, "detect_snap_all_to_kept")

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
        op.snap_all_to_kept = s.detect_snap_all_to_kept

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
            label = "Bake Material Color → Vertex"
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
        box.operator(OBJECT_OT_export_fbx_roblox.bl_idname, text="Export FBX for Roblox")

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

        fix = box.column(align=True)
        fix.label(text="Fix (destructive):")
        fix.operator(OBJECT_OT_decimate_to_limit.bl_idname,
                     text="Decimate Selected (Over Limit)").limit = s.tri_warn_threshold
        fix.operator(OBJECT_OT_downscale_textures.bl_idname,
                     text="Downscale Selected Textures").limit = s.tex_warn_resolution

        box = layout.box()
        box.label(text="Reverse", icon='MATERIAL')
        box.operator(OBJECT_OT_restore_materials_from_grid.bl_idname, text="Restore Materials")
        box.operator(OBJECT_OT_vertex_color_to_material.bl_idname,
                     text="Vertex Color → Material")

def menu_func(self, context):
    self.layout.separator()
    self.layout.operator(OBJECT_OT_material_color_grid.bl_idname, icon='TEXTURE')
    self.layout.operator(OBJECT_OT_restore_materials_from_grid.bl_idname, icon='MATERIAL')
