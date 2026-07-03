import bpy

import math

import json

import os

import random

import urllib.request

import urllib.error

import ssl

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
        "Bake Material Color → Vertex": "マテリアル色 → 頂点カラーへベイク",
        "Bake Material Color to Vertex Colors": "マテリアル色を頂点カラーにベイク",
        "Bake Selected (Atlas + Vertex)": "選択をベイク (アトラス+頂点)",
        "Nothing to Bake": "ベイク対象なし",
        "Process Selected": "選択を処理",
        "Rename Current Grid": "現在のグリッドをリネーム",
        "Select Objects Using Grid": "グリッド使用オブジェクトを選択",
        "Compact Grid (Remove Unused)": "グリッドを整理 (未使用を削除)",
        "Export Textures (PNG)": "テクスチャを書き出し (PNG)",
        "Export FBX for Roblox": "Roblox用FBXを書き出し",
        "Export Grid Textures (PNG)": "グリッドテクスチャを書き出し (PNG)",
        "Export JSON…": "JSONを書き出し…",
        "Export Color JSON": "カラーJSONを書き出し",
        "Run Roblox Check": "Robloxチェックを実行",
        "Decimate Selected (Over Limit)": "選択メッシュをデシメート (上限超のみ)",
        "Downscale Selected Textures": "選択のテクスチャを縮小",
        "Fix (destructive):": "修正 (破壊的):",
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
        "Snap All to Kept Colors": "全てを保持色にスナップ",
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
        # Tooltips (property / operator descriptions)
        "How this object is processed when baking":
            "このオブジェクトをベイク時にどう処理するか",
        "If set, only this object; otherwise all selected meshes":
            "指定時はこのオブジェクトのみ、未指定なら選択中の全メッシュ",
        "Name for a new grid (texture + material), and the target name when renaming":
            "新規グリッド(テクスチャ+マテリアル)の名前、リネーム時は変更先の名前",
        "Maximum atlas size (square, power of two)":
            "アトラスの最大サイズ(正方形・2の累乗)",
        "Pixel size each textured material is packed at":
            "テクスチャ付きマテリアルをパックする際のピクセルサイズ",
        "Pixel size of solid-color swatches in the atlas":
            "アトラス内の単色スウォッチのピクセルサイズ",
        "Inset (pixels) around each packed cell to prevent edge bleeding":
            "にじみ防止のため各セル周囲に取る余白(ピクセル)",
        "Pack solid-color materials as 1px swatches for the smallest possible atlas":
            "単色マテリアルを1pxスウォッチで詰めて可能な限り小さいアトラスにする",
        "Also bake a grayscale roughness texture (values rounded to 0.1)":
            "ラフネスのグレースケールも生成(値は0.1刻み)",
        "Also bake a grayscale metallic texture (values rounded to 0.1)":
            "メタリックのグレースケールも生成(値は0.1刻み)",
        "Delete material slots not used by any face on the object before baking "
        "(same as Blender's Remove Unused Slots), so unused colors don't take grid cells":
            "ベイク前に、どの面にも使われていないマテリアルスロットを削除"
            "(Blender標準の未使用スロット削除と同等)。未使用色がセルを占有しないように",
        "After baking, save the atlas as PNG next to the .blend and reference it (unpack), "
        "so FBX export embeds the texture without manual steps":
            "ベイク後にアトラスをPNG保存し参照に切替(パック解除)。"
            "手動操作なしでFBXにテクスチャが埋め込まれる",
        "Save the atlas PNG next to the .blend and reference it after baking":
            "ベイク後にアトラスPNGを保存して参照に切り替える",
        "Warn if a mesh exceeds this triangle count":
            "メッシュがこのトライアングル数を超えたら警告",
        "Warn if a texture is larger than this (px)":
            "テクスチャがこのサイズ(px)を超えたら警告",
        "Where auto-exported atlas PNGs are written. Leave blank to use a folder next to "
        "this add-on. This avoids needing the .blend saved, and keeps a consistent path "
        "across machines":
            "自動書き出しされたアトラスPNGの保存先。空欄ならアドオン隣のフォルダを使用。"
            ".blendの保存が不要になり、PC間で一定のパスを保てる",
        "After saving, point each image at its PNG and unpack it so it is no longer embedded "
        "in the .blend. Makes FBX export reference real files, fixing textures not loading "
        "in Roblox Studio":
            "保存後、各画像をPNGファイル参照に切替えパック解除。FBXが実ファイルを参照するようになり、"
            "Roblox Studioでテクスチャが出ない問題を修正",
        "Export at 0.01 so 1 m in Blender imports at the expected size in Studio":
            "0.01で書き出し、Blenderの1mがStudioで期待サイズになるように",
        "Apply Location/Rotation/Scale on the objects before export":
            "書き出し前にオブジェクトの位置・回転・スケールを適用",
        "Max linear RGB distance within one group (Auto mode). Smaller = more, purer groups; "
        "larger = fewer, broader groups":
            "1グループ内の最大リニアRGB距離(自動モード)。小さいほど多く純粋な群、"
            "大きいほど少なく広い群",
        "Number of color groups to create (Fixed Count mode)":
            "作成する色グループ数(色数指定モード)",
        "Sampling grid density per triangle (higher = more sample points per face, slower "
        "but more accurate for large/varied faces)":
            "三角形あたりのサンプル密度(高いほど面あたりの点が増え、遅いが大きい/複雑な面で正確)",
        "Cluster colors across all selected objects together, so the same color on different "
        "objects ends up sharing one material":
            "選択オブジェクト全体で色をまとめてクラスタリングし、別オブジェクトの同色が"
            "1マテリアルを共有するように",
        "Name prefix for the solid-color materials that get created":
            "作成される単色マテリアルの名前の接頭辞",
        "Delete the original image-textured material slot(s) once no face uses them anymore":
            "どの面も使わなくなった元の画像テクスチャマテリアルスロットを削除",
        "Faces close to this color are forced to this exact color (not clustered)":
            "この色に近い面はクラスタリングせず、この色そのものに固定",
        "Warn if a mesh exceeds this triangle count":
            "メッシュがこのトライアングル数を超えたら警告",
        "Lower bound on how far meshes may be decimated":
            "メッシュをデシメートできる下限の比率",
        "Apply the Decimate modifier (uncheck to keep it live)":
            "デシメートモディファイアを適用(オフでライブのまま保持)",
    }
    out = {}
    for en, ja in pairs.items():
        out[("*", en)] = ja
        out[("Operator", en)] = ja
    return out

translations_dict = {"ja_JP": _tr_pairs()}
