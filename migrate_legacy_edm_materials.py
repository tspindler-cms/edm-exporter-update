# SPDX-License-Identifier: MIT
"""
Migrate legacy Blender EDM materials (Tobi-be / madwax ``io_BlenderEdmExporter``) to
Eagle Dynamics ``io_scene_edm`` node setups. Official ED exporter source and docs:
https://github.com/EagleDynamics/Blender-EDM-Exporter (Blender **3.6**, **4.2**, and **4.5** LTS per upstream).

Requirements
------------
- Blender 4.2+ recommended (same band as Eagle Dynamics ``io_scene_edm``). The script
  can still run on 3.6 if the legacy add-on is enabled there.
- Eagle Dynamics ``io_scene_edm`` enabled. Template node groups (``EDM_Default_Material``,
  etc.) are **not** created by ``Update EDM materials`` when they are absent from the
  file; this script creates any missing templates from the add-on pickles when
  ``CONFIG["ensure_edm_node_groups"]`` is true (default).
- Legacy Material fields (``EDMDiffuseMapName``, ``EDMMaterialType``, …) from
  ``io_BlenderEdmExporter`` / madwax / Tobi-be: if the legacy add-on is **not**
  loaded (typical in Blender 4.x), this script **re-registers the same Material RNA
  names** so values saved in a 3.6 .blend file are readable again, then removes those
  definitions when finished. Enable the real legacy add-on if you prefer not to use
  the stub (``CONFIG["register_legacy_rna_stub"] = False``).

Export expects **custom** ED shader nodes (``EdmDefaultShaderNodeType``, …), not a plain
``ShaderNodeGroup`` wired to ``EDM_Default_Material`` ("Green RW" error). This script
uses ``post_init(MatDesc)`` like *Add* → EDM in the shader editor. If an older script
version left a plain group, remove the material ID property ``edm_legacy_migration_v1``
and migrate again.

Usage
-----
1. Edit CONFIG below (texture roots, which materials to touch).
2. Scripting editor → Run Script, or:

       blender your_scene.blend --python migrate_legacy_edm_materials.py

Optional CLI overrides after `--` (see parse_argv()).

For missing ED shader node groups, the script temporarily prepends the add-on
directory to ``sys.path`` and imports ``materials.materials`` (same as ``io_scene_edm``)
to run ``MatDesc.create()`` — no dependency on the GUI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty


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
    # If True and legacy add-on is absent, register temporary Material RNA matching
    # io_BlenderEdmExporter so 3.6-saved fields load in 4.x. Set False to require the
    # real legacy add-on instead.
    "register_legacy_rna_stub": True,
    # If True, create EDM_Default_Material / Glass / Mirror in bpy.data when missing
    # (typical for legacy-only .blend files opened in 4.x). Uses io_scene_edm pickles.
    "ensure_edm_node_groups": True,
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


# -----------------------------------------------------------------------------
# Legacy Material RNA stub (3.6 file → 4.x without legacy add-on)
# -----------------------------------------------------------------------------
# When a .blend was saved with io_BlenderEdmExporter, Material fields are stored by
# identifier. Re-defining the same bpy.props on bpy.types.Material restores Python
# access in a 4.x session where the legacy add-on is not installed.

_LEGACY_RNA_STUB_NAMES: List[str] = []
_LEGACY_RNA_STUB_OWNER = False


def _register_legacy_material_rna_stub() -> None:
    """Attach legacy Material RNA to bpy.types.Material (matches madwax edmprops)."""
    global _LEGACY_RNA_STUB_NAMES
    if _LEGACY_RNA_STUB_NAMES:
        return

    items_mat_type = [
        ("Glass", "Glass", "Glass"),
        ("Solid", "Solid", "Solid"),
        ("transp_self_illu", "transparent self-illuminated", "transparent self-illuminated"),
        ("self_illu", "self-illuminated", "self-illuminated"),
        ("additive_self_illu", "additive_self-illuminated", "additive_self-illuminated"),
        ("bano", "bano_material", "bano_material"),
        ("forest", "forest", "forest"),
        ("Mirror", "Mirror", "Mirror"),
    ]
    items_blending = [
        ("0", "0", "0"),
        ("1", "1", "1"),
        ("2", "2", "2"),
        ("3", "3", "3"),
    ]

    defs: List[Tuple[str, object]] = [
        ("EDMMaterialType", EnumProperty(items=items_mat_type, name="MaterialType", default="Solid")),
        ("EDMBlending", EnumProperty(items=items_blending, name="Blending", default="0")),
        ("EDMUseDiffuseMap", BoolProperty(name="Use diffuse map", default=True)),
        ("EDMUseDamageMap", BoolProperty(name="Use Damage map", default=False)),
        ("EDMUseDamageNormalMap", BoolProperty(name="Use Damage Normal map", default=False)),
        ("EDMUseNormalMap", BoolProperty(name="Use normalmap", default=False)),
        ("EDMUseSelfIllumination", BoolProperty(name="Use Selfillumination", default=False)),
        ("EDMUseSpecularMap", BoolProperty(name="Use specular map/Roughtmet", default=False)),
        ("EDMDiffuseMapName", StringProperty(name="filename", default="texture")),
        ("EDMDamageMapName", StringProperty(name="filename", default="damage")),
        ("EDMDamageNormalMapName", StringProperty(name="filename", default="damage_normal")),
        ("EDMNormalMapName", StringProperty(name="filename", default="texture_normal")),
        ("EDMSelfIlluminationMapName", StringProperty(name="filename", default="texture_illumination")),
        ("EDMSpecularMapName", StringProperty(name="filename", default="texture_spec")),
        ("EDMSelfIllumination", FloatProperty(name="Self Illumination Value", default=1.0, min=0.0, max=100.0)),
        ("EDMDiffuseValue", FloatProperty(name="Diffuse value", default=1.0, min=0.0, max=10.0)),
    ]

    for attr, prop in defs:
        setattr(bpy.types.Material, attr, prop)
        _LEGACY_RNA_STUB_NAMES.append(attr)


def _unregister_legacy_material_rna_stub() -> None:
    global _LEGACY_RNA_STUB_NAMES
    for name in _LEGACY_RNA_STUB_NAMES:
        try:
            delattr(bpy.types.Material, name)
        except Exception:
            pass
    _LEGACY_RNA_STUB_NAMES = []


def _acquire_legacy_material_rna(cfg: dict) -> bool:
    """Ensure legacy Material RNA is readable. Returns True if migration may proceed."""
    global _LEGACY_RNA_STUB_OWNER
    if hasattr(bpy.types.Material, "EDMMaterialType"):
        return True
    if not cfg.get("register_legacy_rna_stub", True):
        print(
            "ERROR: Legacy Material RNA is missing and CONFIG['register_legacy_rna_stub'] "
            "is False. Enable io_BlenderEdmExporter or turn the stub back on."
        )
        return False
    _register_legacy_material_rna_stub()
    _LEGACY_RNA_STUB_OWNER = True
    print(
        "NOTE: Temporary legacy Material RNA registered (read-only) so 3.6 .blend "
        "fields are visible without the old add-on. It is removed when migration ends."
    )
    return hasattr(bpy.types.Material, "EDMMaterialType")


def _release_legacy_material_rna() -> None:
    global _LEGACY_RNA_STUB_OWNER
    if _LEGACY_RNA_STUB_OWNER:
        _unregister_legacy_material_rna_stub()
        _LEGACY_RNA_STUB_OWNER = False


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


def legacy_addon_registered() -> bool:
    """True if legacy Material EDM fields are readable (io_BlenderEdmExporter or stub)."""
    return hasattr(bpy.types.Material, "EDMMaterialType")


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


# Node groups this migration wires textures into (must exist or be creatable).
_EDM_MIGRATION_TEMPLATE_TREES: Tuple[str, ...] = (
    "EDM_Default_Material",
    "EDM_Glass_Material",
    "EDM_Mirror_Material",
)

# io_scene_edm custom shader nodes (export rejects plain ShaderNodeGroup — "Green RW").
_EDM_CUSTOM_SHADER_BL_IDNAMES: Set[str] = {
    "EdmDefaultShaderNodeType",
    "EdmDeckShaderNodeType",
    "EdmFakeOmniShaderNodeType",
    "EdmFakeSpotShaderNodeType",
    "EdmGlassShaderNodeType",
    "EdmMirrorShaderNodeType",
    "EdmMatrialShaderNodeType",
}

# tree_name → bpy node type for nodes.new(type=…); then post_init(MatDesc) wires the group.
_EDM_TREE_TO_CUSTOM_BL_ID: Dict[str, str] = {
    "EDM_Default_Material": "EdmDefaultShaderNodeType",
    "EDM_Glass_Material": "EdmGlassShaderNodeType",
    "EDM_Mirror_Material": "EdmMirrorShaderNodeType",
}


def _io_scene_edm_addon_root() -> Optional[Path]:
    """Directory containing ``materials/``, ``data/*.pickle``, etc."""
    try:
        import io_scene_edm as edm  # type: ignore

        init = getattr(edm, "__file__", None)
        if init:
            return Path(init).resolve().parent
    except ImportError:
        pass
    cand = Path(bpy.utils.user_resource("SCRIPTS", path="addons")) / "io_scene_edm"
    return cand if cand.is_dir() else None


def _prepare_io_scene_edm_syspath() -> Optional[str]:
    """Return io_scene_edm add-on root as str and ensure it is on sys.path."""
    root = _io_scene_edm_addon_root()
    if root is None:
        return None
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    return root_s


def load_edm_material_descriptions() -> Optional[Dict[str, Any]]:
    """MatDesc dict keyed by ``EDM_Default_Material``, … (needs add-on on sys.path)."""
    if _prepare_io_scene_edm_syspath() is None:
        print(
            "ERROR: io_scene_edm add-on not found (import io_scene_edm failed and "
            f"folder missing: {Path(bpy.utils.user_resource('SCRIPTS', path='addons')) / 'io_scene_edm'})."
        )
        return None
    try:
        from materials.materials import build_material_descriptions  # type: ignore

        return build_material_descriptions()
    except ImportError as ex:
        print(f"ERROR: Could not import io_scene_edm material helpers: {ex}")
        return None


def ensure_edm_template_node_groups(cfg: dict, edm_descs: Dict[str, Any]) -> bool:
    """
    Legacy .blend files often have no ED node trees. ``bpy.ops.edm.import_matrials``
    only *updates* trees that already exist, so it does nothing in that case.
    Here we call the same ``MatDesc.create()`` path as ``node.ed_add_default``.
    """
    if not cfg.get("ensure_edm_node_groups", True):
        return True

    missing = [n for n in _EDM_MIGRATION_TEMPLATE_TREES if bpy.data.node_groups.get(n) is None]
    if not missing:
        return True

    if _prepare_io_scene_edm_syspath() is None:
        return False

    ok = True
    for name in missing:
        mat_desc = edm_descs.get(name)
        if mat_desc is None:
            root = _io_scene_edm_addon_root()
            print(
                f'ERROR: No material description for "{name}" (missing or unreadable '
                f"pickle under {root / 'data' if root else '?'}?)."
            )
            ok = False
            continue
        try:
            mat_desc.create()
            print(f'NOTE: Created missing node group "{name}" from io_scene_edm data.')
        except Exception as ex:
            print(f'ERROR: Failed to create node group "{name}": {ex}')
            ok = False

    still = [n for n in _EDM_MIGRATION_TEMPLATE_TREES if bpy.data.node_groups.get(n) is None]
    if still:
        print(f"ERROR: After template creation, still missing: {still!r}.")
        return False
    return ok


def material_has_edm_group(mat: bpy.types.Material) -> bool:
    """True if material already uses an ED *custom* shader node (export-safe)."""
    if not mat or not mat.use_nodes or not mat.node_tree:
        return False
    for node in mat.node_tree.nodes:
        if getattr(node, "bl_idname", "") in _EDM_CUSTOM_SHADER_BL_IDNAMES:
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


def add_edm_shader_node(
    nt: bpy.types.NodeTree, tree_name: str, edm_descs: Dict[str, Any]
) -> bpy.types.Node:
    """
    Add an Eagle Dynamics custom shader node (same as ``node.ed_add_defaultnew``).
    Plain ``ShaderNodeGroup`` + node_tree is rejected at export ("Green RW").
    """
    bl_id = _EDM_TREE_TO_CUSTOM_BL_ID.get(tree_name)
    if not bl_id:
        raise RuntimeError(f'No custom ED node type mapped for tree "{tree_name}".')
    mat_desc = edm_descs.get(tree_name)
    if mat_desc is None:
        raise RuntimeError(
            f'Material description for "{tree_name}" missing. '
            f"Check io_scene_edm ``data/{tree_name}.pickle``."
        )
    node = nt.nodes.new(type=bl_id)
    node.name = tree_name
    node.width = 340
    node.post_init(mat_desc)
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


def migrate_one_material(
    mat: bpy.types.Material, cfg: dict, roots: Sequence[Path], edm_descs: Dict[str, Any]
) -> bool:
    marker = cfg["id_prop_marker"]
    if mat.get(marker):
        return False

    if cfg["skip_if_edm_group_present"] and material_has_edm_group(mat):
        return False

    if not legacy_addon_registered():
        raise RuntimeError(
            "Legacy Material RNA missing (EDMMaterialType). "
            "run_migration() should call _acquire_legacy_material_rna() first."
        )

    legacy_type = getattr(mat, "EDMMaterialType", LEGACY_TYPE_DEFAULT)
    tree_name = LEGACY_TO_TREE.get(str(legacy_type), "EDM_Default_Material")
    if edm_descs.get(tree_name) is None:
        print(f'  SKIP "{mat.name}": no MatDesc for tree "{tree_name}".')
        return False

    exts = cfg["texture_extensions"]
    uv_name = pick_uv_for_material(mat)

    nt = ensure_node_tree(mat)
    clear_material_nodes(nt)

    out = add_output(nt)
    out.location = (400, 0)
    group_node = add_edm_shader_node(nt, tree_name, edm_descs)
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
    if not _acquire_legacy_material_rna(cfg):
        return 0, 0
    try:
        edm_descs = load_edm_material_descriptions()
        if edm_descs is None:
            return 0, 0

        if not ensure_edm_template_node_groups(cfg, edm_descs):
            print(
                "Migration stopped: install/enable Eagle Dynamics io_scene_edm, or open "
                "a .blend that already contains EDM_Default_Material node groups."
            )
            return 0, 0

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
                if migrate_one_material(mat, cfg, roots, edm_descs):
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
    finally:
        _release_legacy_material_rna()


def main() -> None:
    cfg = parse_argv()
    run_migration(cfg)


if __name__ == "__main__":
    main()
