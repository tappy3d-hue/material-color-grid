"""Material Color Grid Texture — Blender add-on (package entry)."""

bl_info = {
    "name": "Material Color Grid Texture",
    "author": "Claude",
    "version": (1, 34, 0),
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
import inspect

from . import core, properties, operators, panels, translations
from . import operators as _ops_mod
from . import properties as _props_mod
from . import panels as _panels_mod
from .core import MODE_ITEMS
from .properties import _mode_get, _mode_set


def _classes_in(module):
    out = []
    for _, cls in inspect.getmembers(module, inspect.isclass):
        # Only classes actually defined in this module (skip re-exports).
        if getattr(cls, "__module__", None) != module.__name__:
            continue
        if hasattr(cls, "bl_rna") or hasattr(cls, "bl_idname"):
            out.append(cls)
    return out


# Deterministic registration order: property groups first, then prefs, then ops, then panel.
_PROP_CLASSES = ["MCGKeepColor", "MCGSettings",
                 "MCG_OT_check_update", "MCG_OT_install_update", "MCG_OT_open_releases",
                 "MCGAddonPreferences"]


def _ordered_property_classes():
    out = []
    for n in _PROP_CLASSES:
        c = getattr(_props_mod, n, None)
        if c is not None:
            out.append(c)
    return out


def _operator_classes():
    return [c for c in _classes_in(_ops_mod) if c not in _ordered_property_classes()]


def _panel_classes():
    return [c for c in _classes_in(_panels_mod)]


def register():
    for c in _ordered_property_classes():
        bpy.utils.register_class(c)
    for c in _operator_classes():
        bpy.utils.register_class(c)
    for c in _panel_classes():
        bpy.utils.register_class(c)
    bpy.types.Object.mcg_mode = bpy.props.EnumProperty(
        name="Mode", items=MODE_ITEMS, get=_mode_get, set=_mode_set,
        description="How this object is processed when baking",
    )
    bpy.types.Scene.mcg_settings = bpy.props.PointerProperty(type=properties.MCGSettings)
    bpy.types.VIEW3D_MT_object.append(panels.menu_func)
    try:
        bpy.app.translations.register(__package__, translations.translations_dict)
    except ValueError:
        pass


def unregister():
    try:
        bpy.app.translations.unregister(__package__)
    except ValueError:
        pass
    bpy.types.VIEW3D_MT_object.remove(panels.menu_func)
    del bpy.types.Scene.mcg_settings
    del bpy.types.Object.mcg_mode
    for c in reversed(_panel_classes()):
        bpy.utils.unregister_class(c)
    for c in reversed(_operator_classes()):
        bpy.utils.unregister_class(c)
    for c in reversed(_ordered_property_classes()):
        bpy.utils.unregister_class(c)
