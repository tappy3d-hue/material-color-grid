# Material Color Grid Texture

A Blender add-on that bakes the Base Color of each material on an object into a single grid texture, builds a new material that uses it, and stores the original material assignment as vertex groups so you can re-select faces later.

![grid example](docs/example.png)

## What it does

Given a mesh with several materials:

1. Collects the **Principled BSDF → Base Color** of every material slot.
2. Generates a square texture split into a `ceil(√n) × ceil(n/cols)` grid of solid colors (e.g. 4 mats → 2×2, 10 mats → 4×3 with 2 empty cells).
3. Creates a new material with that texture (Image Texture → Principled BSDF, `Closest` interpolation) and assigns it.
4. Optionally creates a new UV map (`ColorGridUV`) where every face is mapped to its original material's color cell — so the result visually matches the original — and removes all other UV maps.
5. Optionally creates one vertex group per original material name, containing the verts of the faces that used it, so you can re-select them via `Select → Vertex Group`.

Color values are linear→sRGB-encoded before being written, so the sampled color in the new shader matches the original Base Color exactly.

## Installation

1. Download `material_color_grid.py` (or this repo as a zip).
2. In Blender: `Edit → Preferences → Add-ons → Install...`
3. Pick the `.py` file and enable the checkbox.

Tested on Blender 3.x and 4.x.

## Usage

1. Select a mesh object that has multiple materials.
2. `Object` menu → **Material Color Grid Texture**, or press `F3` and search for it.
3. Tweak options in the Redo panel (bottom-left):
   - **Resolution** — texture size (square)
   - **Create Vertex Groups** — one group per original material name
   - **Remap UVs to Color Cells** — replace UV maps with a new one pointing at each face's color cell
   - **Replace Material Slots** — wipe old slots and assign only the new grid material

## Notes

- Same material used in multiple slots is collapsed to a single cell.
- Materials without a Principled BSDF fall back to the viewport diffuse color.
- The generated image is packed into the .blend; export it with `Image → Save As` if you need an external file.
- Vertex groups are created with weight 1.0 on every vertex of the relevant faces; verts on material boundaries will belong to multiple groups.

## License

MIT — see [LICENSE](LICENSE).
