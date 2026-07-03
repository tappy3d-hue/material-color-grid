import bpy

import math

import json

import os

import random

import urllib.request

import urllib.error

import ssl

from .core import (
    default_export_dir, resolve_export_dir, _current_version,
    MODE_ITEMS, COLOR_GROUP_MODE_ITEMS, DEFAULT_GROUP_NAME, UPDATE_REPO, PKG_NAME,
    _mode_to_index, auto_detect_mode,
)

def _mode_get(self):
    raw = self.get("mcg_mode_raw", -1)
    if not isinstance(raw, int) or raw < 0 or raw >= len(MODE_ITEMS):
        return _mode_to_index(auto_detect_mode(self))
    return raw

def _mode_set(self, value):
    self["mcg_mode_raw"] = int(value)

class MCGKeepColor(bpy.types.PropertyGroup):
    color: bpy.props.FloatVectorProperty(
        name="Keep Color", subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0),
        description="Faces close to this color are forced to this exact color (not clustered)",
    )

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
    detect_snap_all_to_kept: bpy.props.BoolProperty(
        name="Snap All to Kept Colors",
        description="Force every face to its nearest kept color, ignoring the threshold. "
                    "No additional clusters are created — the result has exactly the kept "
                    "colors and nothing else (Auto mode only)",
        default=False,
    )
    keep_colors: bpy.props.CollectionProperty(type=MCGKeepColor)
    keep_color_index: bpy.props.IntProperty(default=0)
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

class MCG_OT_check_update(bpy.types.Operator):
    """Check GitHub for a newer release"""
    bl_idname = "mcg.check_update"
    bl_label = "Check for Updates"

    def execute(self, context):
        prefs = context.preferences.addons[PKG_NAME].preferences
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
        prefs = context.preferences.addons[PKG_NAME].preferences
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
        prefs = context.preferences.addons[PKG_NAME].preferences
        url = prefs.latest_html_url or f"https://github.com/{UPDATE_REPO}/releases"
        bpy.ops.wm.url_open(url=url)
        return {'FINISHED'}

class MCGAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = PKG_NAME

    update_status: bpy.props.StringProperty(default="")
    update_available: bpy.props.BoolProperty(default=False)
    latest_tag: bpy.props.StringProperty(default="")
    latest_asset_url: bpy.props.StringProperty(default="")
    latest_html_url: bpy.props.StringProperty(default="")

    export_dir: bpy.props.StringProperty(
        name="Texture Export Folder",
        description="Where auto-exported atlas PNGs are written. Leave blank to use a folder "
                    "next to this add-on. This avoids needing the .blend saved, and keeps a "
                    "consistent path across machines",
        subtype='DIR_PATH',
        default="",
    )

    def draw(self, context):
        layout = self.layout
        v = _current_version()
        col = layout.column()
        col.label(text=f"Installed version: v{v[0]}.{v[1]}.{v[2]}")

        # Auto-updater is temporarily disabled while migrating to the package
        # distribution format. Users update by installing the new zip manually.
        col.operator(MCG_OT_open_releases.bl_idname, icon='URL')
        col.label(text="Update by installing the latest zip from Releases.",
                  icon='INFO')

        col.separator()
        col.prop(self, "export_dir")
        col.label(text=f"Default: {default_export_dir()}", icon='FILE_FOLDER')
