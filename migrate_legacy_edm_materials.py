# SPDX-License-Identifier: MIT
"""
Migrate legacy madwax / Tobias Becker Blender EDM materials (RNA filename fields on
Material) to Eagle Dynamics io_scene_edm node groups with Image Texture nodes.

Requirements
------------
- Blender 3.6.x / 4.2.x / 4.5.x (same band as the official ED exporter).
- Eagle Dynamics add-on enabled so these node groups exist:
    EDM_Default_Material, EDM_Glass_Material, EDM_Mirror_Material
- Legacy Material RNA (EDMDiffuseMapName, EDMMaterialType, …) must exist while this
  runs. Easiest: enable legacy io_BlenderEdmExporter temporarily, or open a .blend
  saved with it registered (RNA still loads).

Usage
-----
1. Edit CONFIG below (texture roots, which materials to touch).
2. Scripting editor → Run Script, or:

       blender your_scene.blend --python migrate_legacy_edm_materials.py

Optional CLI overrides after `--` (see parse_argv()).

This script only uses bpy; it does not import io_scene_edm modules.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence, Set, Tuple

import bpy


# -----------------------------------------------------------------------------
# CONFIG — edit or override via CLI
# -----------------------------------------------------------------------------

CONFIG = {
    # Absolute paths and/or Blender "//relative" paths (relative to saved .blend).
    "texture_search_dirs": [
        "//textures",
        "//../textures",
        "//",
    ],
    # Only migrate materials assigned to mesh objects (recommended).
    "only_used_by_mesh": True,
    # Skip materials that already use an ED node group tree.
    "skip_if_edm_group_present": True,
    # Mark migrated materials so the script does not run twice.
    "id_prop_marker": "edm_legacy_migration_v1",
    # Common image extensions tried for legacy basename fields (order matters).
    "texture_extensions": (
        ".dds",
        ".png",
        ".tga",
        ".tif",
        ".tiff",
        ".jpg",
        ".jpeg",
        ".exr",
        ".bmp",
    ),
}


# Legacy enum item names from io_BlenderEdmExporter/edmprops.py
LEGACY_TYPE_DEFAULT = "Solid"

LEGACY_TO_TREE = {
    "Solid": "EDM_Default_Material",
    "Glass": "EDM_Glass_Material",
    "Mirror": "EDM_Mirror_Material",
    # Approximate: ED exporter models these via DefaultMaterial + sockets / object props.
    "transp_self_illu": "EDM_Default_Material",
    "self_illu": "EDM_Default_Material",
    "additive_self_illu": "EDM_Default_Material",
    "bano": "EDM_Default_Material",
    "forest": "EDM_Default_Material",
}

# Default-material sockets (EagleDynamics/io_scene_edm/enums.py — NodeSocketInDefaultEnum)
DEF_SOCK = {
    "base_color": "Base Color",
    "rough_met": "RoughMet (Non-Color)",
    "normal": "Normal (Non-Color)",
    "emissive": "Emissive",
    "emissive_mask": "Emissive Mask",
    "emissive_value": "Emissive Value",
    "damage_color": "Damage Base",
    "damage_normal": "Damage Normal (Non-Color)",
    "damage_mask": "Damage Map (Non-Color)",
    "transparency": "Transparency",
}

# Glass (NodeSocketInGlassEnum)
GLASS_SOCK = {
    "diffuse": "Diffuse Color (Dirt)",
    "rough_met": "RoughMet (Non-Color)",
    "normal": "Normal (Non-Color)",
    "damage_mask": "Damage Map (Non-Color)",
    "damage_normal": "Damage Normal (Non-Color)",
}

# Mirror (NodeSocketInMirrorEnum)
MIRROR_SOCK = {
    "base_color": "Base Color",
    "normal": "Normal (Non-Color)",
}


def parse_argv() -> dict:
    """Minimal overrides after ``--``: ``--texture-dir=/abs/path`` (repeatable)."""
    cfg = dict(CONFIG)
    extra_dirs: list[str] = []
    if "--" in sys.argv:
        raw = sys.argv[sys.argv.index("--") + 1 :]
        for a in raw:
            if a.startswith("--texture-dir="):
                extra_dirs.append(a.split("=", 1)[1])
    if extra_dirs:
        cfg["texture_search_dirs"] = list(cfg["texture_search_dirs"]) + extra_dirs
    return cfg


def legacy_addon_registered() -> bool:
    return hasattr(bpy.types.Material, "EDMMaterialType")


def mesh_materials() -> Set[bpy.types.Material]:
    used: Set[bpy.types.Material] = set()
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material:
                used.add(slot.material)
    return used


def edm_tree_names() -> Set[str]:
    return {
        "EDM_Default_Material",
        "EDM_Glass_Material",
        "EDM_Mirror_Material",
        "EDM_Deck_Material",
        "EDM_Fake_Omni_Material",
        "EDM_Fake_Spot_Material",
    }


def material_has_edm_group(mat: bpy.types.Material) -> bool:
    if not mat or not mat.use_nodes or not mat.node_tree:
        return False
    names = edm_tree_names()
    for node in mat.node_tree.nodes:
        if node.type != "GROUP":
            continue
        nt = getattr(node, "node_tree", None)
        if nt and nt.name in names:
            return True
    return False


def resolve_texture_roots(cfg: dict) -> list[Path]:
    roots: list[Path] = []
    for entry in cfg["texture_search_dirs"]:
        entry = (entry or "").strip()
        if not entry:
            continue
        if entry.startswith("//"):
            abs_p = bpy.path.abspath(entry)
        else:
            p0 = Path(entry).expanduser()
            if p0.is_absolute():
                abs_p = str(p0)
            else:
                blend_dir = Path(bpy.path.abspath("//")).resolve()
                abs_p = str(blend_dir / p0)
        p = Path(abs_p).resolve()
        if p.is_dir():
            roots.append(p)
    # De-dupe preserving order
    seen = set()
    out = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def stem_variants(name: str) -> list[str]:
    """Legacy fields were usually basename; strip extensions repeatedly."""
    if not name or not str(name).strip():
        return []
    s = Path(str(name).strip()).name
    stems = []
    cur = s
    for _ in range(4):
        if cur and cur not in stems:
            stems.append(cur)
        p = Path(cur)
        if p.suffix:
            cur = p.stem
        else:
            break
    return stems


def find_image_for_stem(stems: Sequence[str], roots: Sequence[Path], exts: Sequence[str]) -> Optional[bpy.types.Image]:
    for stem in stems:
        for root in roots:
            for ext in exts:
                cand = root / f"{stem}{ext}"
                if cand.is_file():
                    try:
                        return bpy.data.images.load(str(cand), check_existing=True)
                    except RuntimeError:
                        continue
    return None


def guess_uv_layer_name(obj: Optional[bpy.types.Object]) -> str:
    if obj and obj.type == "MESH" and obj.data.uv_layers.active:
        return obj.data.uv_layers.active.name
    return ""


def pick_uv_for_material(mat: bpy.types.Material) -> str:
    """Use UV from an arbitrary mesh that references this material."""
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material == mat:
                name = guess_uv_layer_name(obj)
                if name:
                    return name
    return ""


def ensure_node_tree(mat: bpy.types.Material) -> bpy.types.NodeTree:
    if mat.node_tree:
        return mat.node_tree
    mat.use_nodes = True
    return mat.node_tree


def clear_material_nodes(nt: bpy.types.NodeTree) -> None:
    nt.nodes.clear()


def add_output(nt: bpy.types.NodeTree) -> bpy.types.Node:
    return nt.nodes.new(type="ShaderNodeOutputMaterial")


def add_edm_group(nt: bpy.types.NodeTree, tree_name: str) -> bpy.types.Node:
    grp = bpy.data.node_groups.get(tree_name)
    if grp is None:
        raise RuntimeError(
            f'Node group "{tree_name}" not found. Enable Eagle Dynamics io_scene_edm '
            f"(and run Update EDM Materials once if needed)."
        )
    node = nt.nodes.new(type="ShaderNodeGroup")
    node.node_tree = grp
    node.name = tree_name
    node.width = 340
    return node


def link_shader_to_output(nt: bpy.types.NodeTree, group_node: bpy.types.Node, out_node: bpy.types.Node) -> None:
    for sock in group_node.outputs:
        if sock.type == "SHADER":
            nt.links.new(sock, out_node.inputs["Surface"])
            return
    raise RuntimeError(f"No shader output on group node {group_node.name!r}.")


def set_colorspace(image: bpy.types.Image, non_color: bool) -> None:
    cs = image.colorspace_settings
    try:
        if non_color:
            cs.name = "Non-Color"
        else:
            cs.name = "sRGB"
    except TypeError:
        pass


def make_image_node(
    nt: bpy.types.NodeTree,
    image: Optional[bpy.types.Image],
    non_color: bool,
    location: Tuple[float, float],
    uv_map_name: str,
) -> bpy.types.Node:
    tex = nt.nodes.new(type="ShaderNodeTexImage")
    tex.location = location
    if image:
        tex.image = image
        set_colorspace(image, non_color)
    uv = nt.nodes.new(type="ShaderNodeUVMap")
    uv.location = (location[0] - 220, location[1])
    uv.uv_map = uv_map_name or ""
    nt.links.new(uv.outputs["UV"], tex.inputs["Vector"])
    return tex


def link_rgb(nt: bpy.types.NodeTree, tex_node: bpy.types.Node, group_node: bpy.types.Node, input_name: str) -> None:
    sock = group_node.inputs.get(input_name)
    if sock is None:
        return
    nt.links.new(tex_node.outputs["Color"], sock)


def link_float_socket(nt: bpy.types.NodeTree, tex_node: bpy.types.Node, group_node: bpy.types.Node, input_name: str) -> None:
    sock = group_node.inputs.get(input_name)
    if sock is None:
        return
    nt.links.new(tex_node.outputs["Alpha"], sock)


def try_set_scalar(group_node: bpy.types.Node, socket_name: str, value: float) -> None:
    sock = group_node.inputs.get(socket_name)
    if not sock or sock.is_linked:
        return
    try:
        sock.default_value = value
    except Exception:
        pass


def try_set_transparency_default(group_node: bpy.types.Node, legacy_blending: str) -> None:
    """Best-effort map for legacy EDMBlending '0'..'3'."""
    sock = group_node.inputs.get(DEF_SOCK["transparency"])
    if sock is None or sock.is_linked:
        return
    mapping = {
        "0": "OPAQUE",
        "1": "ALPHA_BLENDING",
        "2": "Z_TEST",
        "3": "SUM_BLENDING",
    }
    token = mapping.get(str(legacy_blending), None)
    if not token:
        return
    try:
        sock.default_value = token
    except Exception:
        try:
            setattr(group_node, sock.identifier, token)
        except Exception:
            pass


def migrate_one_material(mat: bpy.types.Material, cfg: dict, roots: Sequence[Path]) -> bool:
    marker = cfg["id_prop_marker"]
    if mat.get(marker):
        return False

    if cfg["skip_if_edm_group_present"] and material_has_edm_group(mat):
        return False

    if not legacy_addon_registered():
        raise RuntimeError(
            "Legacy Material RNA not found (EDMMaterialType missing). Enable "
            "io_BlenderEdmExporter once, save, then run this script again."
        )

    legacy_type = getattr(mat, "EDMMaterialType", LEGACY_TYPE_DEFAULT)
    tree_name = LEGACY_TO_TREE.get(str(legacy_type), "EDM_Default_Material")
    group_nt = bpy.data.node_groups.get(tree_name)
    if group_nt is None:
        print(f'  SKIP "{mat.name}": tree "{tree_name}" missing.')
        return False

    exts = cfg["texture_extensions"]
    uv_name = pick_uv_for_material(mat)

    nt = ensure_node_tree(mat)
    clear_material_nodes(nt)

    out = add_output(nt)
    out.location = (400, 0)
    group_node = add_edm_group(nt, tree_name)
    group_node.location = (0, 0)
    link_shader_to_output(nt, group_node, out)

    y = 320
    dy = -260

    def next_slot() -> Tuple[float, float]:
        nonlocal y
        loc = (-520, y)
        y += dy
        return loc

    # --- Load images from legacy string fields ---
    use_diffuse = bool(getattr(mat, "EDMUseDiffuseMap", True))
    use_normal = bool(getattr(mat, "EDMUseNormalMap", False))
    use_spec = bool(getattr(mat, "EDMUseSpecularMap", False))
    use_damage = bool(getattr(mat, "EDMUseDamageMap", False))
    use_damage_n = bool(getattr(mat, "EDMUseDamageNormalMap", False))
    use_si_tex = bool(getattr(mat, "EDMUseSelfIllumination", False))

    stems_diffuse = stem_variants(getattr(mat, "EDMDiffuseMapName", ""))
    stems_norm = stem_variants(getattr(mat, "EDMNormalMapName", ""))
    stems_spec = stem_variants(getattr(mat, "EDMSpecularMapName", ""))
    stems_damage = stem_variants(getattr(mat, "EDMDamageMapName", ""))
    stems_damage_n = stem_variants(getattr(mat, "EDMDamageNormalMapName", ""))
    stems_si = stem_variants(getattr(mat, "EDMSelfIlluminationMapName", ""))

    img_diffuse = find_image_for_stem(stems_diffuse, roots, exts) if use_diffuse else None
    img_norm = find_image_for_stem(stems_norm, roots, exts) if use_normal else None
    img_spec = find_image_for_stem(stems_spec, roots, exts) if use_spec else None
    img_damage = find_image_for_stem(stems_damage, roots, exts) if use_damage else None
    img_damage_n = find_image_for_stem(stems_damage_n, roots, exts) if use_damage_n else None
    img_si = find_image_for_stem(stems_si, roots, exts) if use_si_tex else None

    # Wire by shader family
    if tree_name == "EDM_Default_Material":
        if img_diffuse:
            t = make_image_node(nt, img_diffuse, False, next_slot(), uv_name)
            link_rgb(nt, t, group_node, DEF_SOCK["base_color"])
        if img_spec:
            t = make_image_node(nt, img_spec, True, next_slot(), uv_name)
            link_rgb(nt, t, group_node, DEF_SOCK["rough_met"])
        if img_norm:
            t = make_image_node(nt, img_norm, True, next_slot(), uv_name)
            link_rgb(nt, t, group_node, DEF_SOCK["normal"])
        if img_damage:
            t = make_image_node(nt, img_damage, True, next_slot(), uv_name)
            link_rgb(nt, t, group_node, DEF_SOCK["damage_mask"])
        if img_damage_n:
            t = make_image_node(nt, img_damage_n, True, next_slot(), uv_name)
            link_rgb(nt, t, group_node, DEF_SOCK["damage_normal"])
        if img_si:
            t = make_image_node(nt, img_si, False, next_slot(), uv_name)
            link_rgb(nt, t, group_node, DEF_SOCK["emissive"])
            # Legacy single-channel masks sometimes lived in alpha only — optional hook-up:
            link_float_socket(nt, t, group_node, DEF_SOCK["emissive_mask"])

        try_set_scalar(group_node, DEF_SOCK["emissive_value"], float(getattr(mat, "EDMSelfIllumination", 0.0)))
        try_set_scalar(group_node, "Base Alpha*", float(getattr(mat, "EDMDiffuseValue", 1.0)))
        try_set_transparency_default(group_node, str(getattr(mat, "EDMBlending", "0")))

        if str(legacy_type) == "forest":
            try_set_transparency_default(group_node, "3")
        elif str(legacy_type) in {"transp_self_illu"}:
            try_set_transparency_default(group_node, "1")

    elif tree_name == "EDM_Glass_Material":
        if img_diffuse:
            t = make_image_node(nt, img_diffuse, False, next_slot(), uv_name)
            link_rgb(nt, t, group_node, GLASS_SOCK["diffuse"])
        if img_spec:
            t = make_image_node(nt, img_spec, True, next_slot(), uv_name)
            link_rgb(nt, t, group_node, GLASS_SOCK["rough_met"])
        if img_norm:
            t = make_image_node(nt, img_norm, True, next_slot(), uv_name)
            link_rgb(nt, t, group_node, GLASS_SOCK["normal"])
        if img_damage:
            t = make_image_node(nt, img_damage, True, next_slot(), uv_name)
            link_rgb(nt, t, group_node, GLASS_SOCK["damage_mask"])
        if img_damage_n:
            t = make_image_node(nt, img_damage_n, True, next_slot(), uv_name)
            link_rgb(nt, t, group_node, GLASS_SOCK["damage_normal"])

    elif tree_name == "EDM_Mirror_Material":
        if img_diffuse:
            t = make_image_node(nt, img_diffuse, False, next_slot(), uv_name)
            link_rgb(nt, t, group_node, MIRROR_SOCK["base_color"])
        if img_norm:
            t = make_image_node(nt, img_norm, True, next_slot(), uv_name)
            link_rgb(nt, t, group_node, MIRROR_SOCK["normal"])

    mat[marker] = True
    print(f'  OK "{mat.name}" → {tree_name} ({legacy_type})')
    return True


def run_migration(cfg: Optional[dict] = None) -> Tuple[int, int]:
    cfg = cfg or parse_argv()
    roots = resolve_texture_roots(cfg)
    if not roots:
        print(
            "WARNING: No texture directories resolved. "
            "Set CONFIG['texture_search_dirs'] or pass --texture-dir=… "
            "(paths must exist). Images may fail to load.\n"
            f"  Tried: {cfg['texture_search_dirs']!r}"
        )

    candidates: Iterable[bpy.types.Material]
    if cfg["only_used_by_mesh"]:
        candidates = sorted(mesh_materials(), key=lambda m: m.name)
    else:
        candidates = sorted(bpy.data.materials, key=lambda m: m.name)

    done = 0
    skipped = 0
    for mat in candidates:
        try:
            if migrate_one_material(mat, cfg, roots):
                done += 1
            else:
                skipped += 1
        except Exception as ex:
            print(f'  FAIL "{mat.name}": {ex}')
            skipped += 1

    print(f"\nDone. Migrated: {done}, skipped/failed: {skipped}.")
    print(
        "Next: in Blender N-panel run **Update EDM Materials** on the ED exporter, "
        "then verify RoughMet packing and object-level EDM custom props."
    )
    return done, skipped


def main() -> None:
    cfg = parse_argv()
    if not legacy_addon_registered():
        print(
            "NOTE: Legacy RNA not registered yet. If migration finds nothing to read, "
            "enable io_BlenderEdmExporter and re-open this file."
        )
    run_migration(cfg)


if __name__ == "__main__":
    main()
