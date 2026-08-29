# The `Troi` class

A **frozen, hashable request**: *this region, these dates, this name*.
Two trois with the same inputs are the same troi — downstream packages
key every cached artefact off it, so repeat runs find their own work.

```python
from datetime import date
from troi import Troi

t = Troi(bbox=[148.46, -34.39, 148.50, -34.36],   # [W, S, E, N], EPSG:4326
         start=date(2023, 1, 1), end=date(2023, 12, 31),
         stub='milgadara')
```

## Fields

Constructor inputs:

| Field | Meaning |
|---|---|
| `bbox` | `[west, south, east, north]` in decimal degrees (EPSG:4326) |
| `start`, `end` | Inclusive date range |
| `stub` | Short name used in every output filename. Defaults to a SHA-256 of `(bbox, start, end)`; pass a string for human-readable outputs |
| `config` | A [`Config`](config.md); defaults to the machine's resolved config |

Derived on construction (never passed):

| Field | Derivation |
|---|---|
| `bbox_hash` | Region identity: bbox **snapped to 3 decimal degrees (~100 m)**, then SHA-256 — near-identical bboxes share a region identity in the registry |
| `time_hash` | SHA-256 of `start` + `end` |
| `out_dir` | `{config.out_dir}/{stub}` — final outputs; created on init |
| `tmp_dir` | `{config.tmp_dir}/{stub}` — intermediates; created on init |
| `centre_lon`, `centre_lat` | Bbox centre |

Storage layout is deliberately **not** `Troi`'s concern: packages derive
their own cache locations (usually from the hashes) in a per-package
`Paths` class — see [conventions](conventions.md).

## Constructors

```python
Troi(bbox=[w, s, e, n], start=..., end=..., stub='site_a')

# centre point + square buffer in km (spherical approximation,
# fine for paddock-scale buffers ≲ 50 km)
Troi.from_lat_lon(lat=-34.38, lon=148.48, buffer_km=2.0,
                  start=..., end=..., stub='site_b')

# envelope of a vector file's features (.gpkg / .shp / .geojson);
# reprojects to EPSG:4326 if needed. label_col renames a column to
# 'paddock' for downstream compatibility. Requires geopandas
# (`pip install troi[paddocks]`).
Troi.build_from_paddocks(paddocks_filepath='paddocks.gpkg',
                         start=..., end=..., stub='site_c',
                         label_col='title')
```

## Side effects of construction

Constructing a `Troi` (1) creates `out_dir` and `tmp_dir`, and
(2) records the troi in the [registry](registry.md). Reusing a `stub`
with different inputs raises `ValueError` — a stub names one troi,
forever.
