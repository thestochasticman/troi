# troi

**The shared core of the Borevitz Lab software ecosystem** — one `Troi`,
one `Config`, one conda environment, used by every lab package.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Borevitz Lab](https://img.shields.io/badge/Borevitz%20Lab-ANU-2ea44f)](https://borevitzlab.anu.edu.au/)

```python
from datetime import date
from troi.troi import Troi

troi = Troi(
    bbox=[148.36265, -33.52606, 148.38265, -33.50606],  # [W, S, E, N]
    start=date(2024, 1, 1),
    end=date(2024, 12, 31),
    stub='my_farm',
)
```

Every downstream package takes a `Troi` and answers it — same identity,
same caches, same reproducibility guarantees everywhere.

| Package | Built on this core | What it does |
|---|---|---|
| [`pysentinel2`](https://github.com/thestochasticman/pysentinel2) | `Troi`, `Config` | Self-filling local Sentinel-2 datacube — nothing downloaded twice |
| [`pysilo`](https://github.com/thestochasticman/pysilo) | `Troi`, `Config` | Cached SILO daily climate — fetch once per ~5 km grid point |
| [`pyozwald`](https://github.com/thestochasticman/pyozwald) | `Troi`, `Config` | Cached OzWALD meteorology + 8-day biophysical series — fetch once per grid point |
| [`pycopdem`](https://github.com/thestochasticman/pycopdem) | `Troi`, `Config` | Cached Copernicus 30 m DEM + on-read slope/TWI/aspect/HLI — one download per chunk |
| [`pyslga`](https://github.com/thestochasticman/pyslga) | `Troi`, `Config` | Cached SLGA soil properties (16 attributes × 6 depths) — one download per chunk |
| [`PaddockTS`](https://github.com/thestochasticman/paddocktimeseries) | `Troi`, `Config` | Paddock segmentation, time series, phenology, reports |

---

## `Troi` — the identity layer

A **frozen, hashable request**: *this region, this date range*. Two
queries with the same inputs are the same troi — they share every
cached artefact on disk.

```python
q.bbox_hash    # region identity  (bbox snapped to ~100 m, then SHA-256)
q.time_hash    # date-range identity
q.out_dir      # final outputs for this stub
q.tmp_dir      # scratch space for this stub
```

Storage layout is *not* `Troi`'s concern — packages derive their own
cache locations (usually from the hashes) in their own `Paths` class.

Three ways to build one:

```python
Troi(bbox=[w, s, e, n], start=..., end=..., stub='site_a')

Troi.from_lat_lon(lat=-34.38, lon=148.48, buffer_km=2.0,
                   start=..., end=..., stub='site_b')

Troi.build_from_paddocks(paddocks_filepath='paddocks.gpkg',   # .gpkg / .shp / .geojson
                          start=..., end=..., stub='site_c')
```

Every constructed troi is recorded in a file-locked registry
(`{out_dir}/queries.json`). Re-running an identical troi is a no-op;
reusing a `stub` for *different* inputs raises `ValueError` — stubs
uniquely name a troi, forever.

## `Config` — the environment layer

Where data lives and which credentials to use. Loaded once, from the
first source found:

| Source | Example |
|---|---|
| `~/.config/Troi.json` | `{"out_dir": "...", "email": "...", "tern_api_key": "..."}` |
| `TROI_*` env vars | `TROI_OUTDIR`, `TROI_TMPDIR`, `TROI_EMAIL`, `TROI_TERN_KEY` |
| Built-in defaults | `~/Documents/Troi-Outputs` · `~/Downloads/Troi-Tmp` |

Or bypass files entirely:

```python
from troi.config import Config

cfg = Config(out_dir='/data/outputs', tmp_dir='/data/tmp')
q = Troi(..., config=cfg)
```

## Design rules

The conventions every lab package follows:

- **No inheritance.** One generic `Troi`; packages *compose* with it
  (functions and small classes taking a `Troi`/`Config`), never
  subclass it.
- **`Config` vs `Paths`.** User-settable inputs live on `Config`;
  locations *derived* from a `Troi` or `Config` live on a per-package
  `Paths` class.
- **Layered APIs.** Data-layer functions are troi-agnostic
  (`bbox, start, end`); thin `*_troi` adapters connect them to the
  reproducibility layer.

---

## Install

### The whole lab in one command

`troi.yml` builds the entire ecosystem — this core, the five
data stores, PaddockTS, JupyterLab, and the full geospatial/ML stack —
from the public conda channel, no checkouts:

```bash
conda env create -f https://raw.githubusercontent.com/thestochasticman/troi/main/troi.yml
conda activate troi
```

### Just this package

```bash
conda install -c conda-forge -c thestochasticman troi
```

### From source

All lab repos share one conda environment, **`troi`**. Each
repo's `environment.yml` creates it if missing and augments it if
present (additive — never use `--prune`):

```bash
conda env update -n troi -f environment.yml
conda activate troi
pip install -e .
```

Optional extra for `Troi.build_from_paddocks`:

```bash
pip install -e '.[paddocks]'   # adds geopandas
```

## Test

```bash
python troi/troi.py    # True
python troi/config.py   # prints the resolved config
```

## License

[MIT](LICENSE) · Borevitz Lab, Australian National University
