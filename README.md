# Material Color Grid Texture

Blender → Roblox のアセット移行を効率化するBlenderアドオン。マテリアルの色やテクスチャを、
無駄のない1枚のアトラステクスチャ（＋共有マテリアル1つ）にまとめ、Robloxでのテクスチャ容量・
ドローコールを抑えます。

- Blender 4.x（4.2以降推奨）
- サイドバー `N` → **Color Grid** タブ
- English UI (Japanese shown automatically when Blender's language is set to 日本語)

## インストール

1. リリースページから **material_color_grid.zip** をダウンロード
2. Blender: `編集 → プリファレンス → アドオン → インストール` → zipを選択
3. アドオン一覧で有効化

更新するときは、旧バージョンを一度削除してからBlenderを再起動し、新しいzipを入れ直してください。

## 主な機能

- **パックドアトラス**: 画像テクスチャ付きマテリアルは「タイル」(既定256px)、単色マテリアルは
  「スウォッチ」(既定16px) として非均一パッキング。収まる最小の2の累乗サイズ（上限既定1024）で
  アトラスを生成。**Minimal (1px solids)** をONにすると単色を1pxに詰めて極小アトラスに
- **インクリメンタル更新**: マニフェスト(色台帳)をマテリアルに保存。再ベイクで既存の色を保った
  まま追記され、既存オブジェクトのUVは自動で再配置
- **オブジェクト単位モード**: 選択オブジェクトごとに **Atlas / Vertex Color / JSON / None** を
  切替（トグルボタン、一括設定あり）。1つのBakeボタンがモードに従って処理を振り分け
- **Detect Colors from Texture**: 見た目は単色なのに実体はフル解像度画像（Tripoベイク等）を、
  面ごとに色をサンプリング→クラスタリングして単色マテリアルに変換。しきい値自動 / 色数指定の
  両対応。以降のAtlasベイクで極小スウォッチになりデータ量を大幅削減
- **Keep These Colors（スポイト）**: Autoモードで「絶対に残したい色」をテクスチャからスポイト
  で採取。ホバーでプレビュー→クリックで追加、右クリック/Escで終了。3Dビューは自動で
  フラット＋テクスチャ表示になり、色を正確に拾える。**Snap All to Kept Colors** をONにすると
  全ての面が最も近い保持色にスナップされ、指定色数ぴったりのマテリアルになる
- **頂点カラー**: マテリアルBase Color → 頂点カラー焼き込み（FBX/glTFでRobloxへ、テクスチャ
  不要）。逆変換 **Vertex Color → Material** も対応
- **JSON出力 + Roblox用プラグイン**: 色を焼かずにマテリアルごとへパーツ分割し、
  `{名前_ID: [r,g,b]}` を出力。同梱の `ColorImporter.rbxmx`（Studioプラグイン）で貼り付け→
  選択→一括適用。適用後は各Partに `MCGColor` 属性を書き戻し
- **Roughness / Metallic**: 0.1刻みで量子化した別アトラスとして任意出力
- **Restore Materials**: アトラスを元の個別マテリアルへ逆変換。テクスチャセルは元画像が残って
  いれば再リンク（無劣化）、無ければアトラスから切り出し。UVも復元
- **Roblox Check + Fix**: トライアングル数 / スケール未適用 / UV有無・0-1範囲 / N-gon /
  空スロット / テクスチャ過大 を検査。**Decimate Selected (Over Limit)** / **Downscale
  Selected Textures** で選択メッシュに一括修正（左下パネルで微調整可）
- **Roblox用FBXエクスポート**: 公式推奨設定（Path Mode=Copy＋Embed、FBX Unit Scale、
  Leaf Bones無効、Scale 0.01）で書き出すワンボタン
- **プリセット**: Hero / Prop / Background で解像度・マップ構成を一括切替
- **自動エクスポート**: Bake後にアトラスPNGを共通フォルダへ保存しパック解除（FBX埋め込み対応）
- **共通の出力フォルダ**: 既定は `~/Documents/MaterialColorGrid/`（Win/Mac/Linux共通）。
  プリファレンスで任意のフォルダに変更可

## 基本ワークフロー（Atlas）

1. オブジェクトを選択（複数可。メッシュは結合しなくてよい）
2. Objects & Modes で対象を **Atlas** に（既定で自動判定）
3. 必要ならプリセットやタイル/スウォッチサイズを調整
4. **Bake Selected to Grid** → 共有アトラス＋共有マテリアルが生成される
5. **Auto Export + Unpack After Bake** をONにしておくと、そのままFBXエクスポート
   （Path Mode=Copy＋Embed）でテクスチャが埋め込まれる
6. **Export FBX for Roblox** で書き出し

同じアトラスに後からオブジェクトを追加したい場合は、既存のアトラス使用オブジェクトと
一緒に選択してBakeすれば追記されます（全体が再パックされ、UVは自動追従）。

## Detect Colors from Texture（Tripoモデルの軽量化）

1. 画像テクスチャ付きモデルを選択
2. **Detect Colors from Texture** セクションを設定
   - Grouping: **Auto (Threshold)** は色距離のしきい値で自動分割、**Fixed Count** は色数指定
   - 面内複数点をサンプリングし、中央値（既定）/平均で代表色を決定
3. 残したい色があれば **Keep These Colors** の右上のスポイトで採取
   （ホバーで確認→クリックで追加、右クリックで終了）
4. **Detect Colors & Split Materials** を実行
   → 面が色ごとの単色マテリアルに置き換わる → そのままAtlasベイクへ

Autoモードで「保持色以外は許さない」場合は **Snap All to Kept Colors** をONにすると
指定した色数ぴったりのマテリアルになります。

## JSON方式（Roblox側で動的着色）

1. 対象オブジェクトを **JSON** モードに
2. **Export JSON…** → 確認ダイアログ（複数マテリアルは自動でパーツ分割、グリッド済みは復元→分割、
   オブジェクト名はJSONキーと同じ `名前_ID` にリネーム）
3. **Export FBX for Roblox** でFBX書き出し → Studioへインポート
4. `ColorImporter.rbxmx` をStudioのPlugins Folderに配置（初回のみ、要再起動）
5. プラグインにJSONを貼り付け → モデルを選択 → 適用

## Roblox向けのヒント

- アトラスは2の累乗・上限1024がRobloxのメッシュテクスチャ推奨と整合
- 同じアトラス（同じTextureID）を共有するMeshPartはエンジンが自動バッチングし、
  ドローコールが減る
- 単色だけの背景・大量配置プロップは、Atlasより **Vertex Color** モードのほうが
  テクスチャメモリほぼゼロで軽い
- メタリックが全て0なら Metallic Map はOFFでよい

## 国際化

UIは英語。Blenderの言語設定が日本語なら自動で日本語表示になります
（`プリファレンス → インターフェース → 言語`）。ツールチップの日本語化には、
同じ画面で「翻訳: ツールチップ」も有効にしてください。

## ライセンス

GPL-3.0-or-later（Blenderアドオンの標準）。

## リポジトリ構成

- `material_color_grid/` — アドオンパッケージ本体
  - `__init__.py`, `core.py`, `properties.py`, `operators.py`, `panels.py`,
    `translations.py`, `blender_manifest.toml`
- `material_color_grid.zip` — 配布用zip（`material_color_grid/`をそのまま圧縮したもの）
- `ColorImporter.rbxmx` — Roblox Studio用プラグイン
