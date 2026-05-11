# edm-exporter-update

Helper script to move old **legacy EDM exporter** material settings (per-material texture
basenames and type enums) into Eagle Dynamics’ **official** Blender add-on
[`io_scene_edm`](https://github.com/EagleDynamics/Blender-EDM-Exporter). Install that
add-on (zip from [Eagle Dynamics](https://mods.eagle.ru/blender_plugin/index.html) or
the GitHub org) in Blender **3.6**, **4.2**, or **4.5** LTS as documented upstream.

## Run migration (CLI)

```text
blender /path/to/scene.blend --python /path/to/edm-exporter-update/migrate_legacy_edm_materials.py -- --texture-dir=/absolute/path/to/textures
```

See the docstring at the top of `migrate_legacy_edm_materials.py` for behaviour,
`CONFIG` options, and when the script creates missing `EDM_*` node groups.
