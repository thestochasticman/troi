# Ecosystem conventions

The rules every lab package follows, so that knowing one package means
knowing all of them.

## Composition, never inheritance

There is one generic `Troi`. Packages take it as an argument — plain
functions and small frozen classes — and never subclass it. If a
package needs more identity, it derives it (hashes, windows, paths)
rather than extending the class.

## `Config` vs `Paths`

- **`Config`** holds user-settable *inputs*: data roots, credentials.
  It lives here, in troi, shared by everyone.
- **`Paths`** holds *derived* locations: where a package's store or a
  troi's cache actually sits on disk. Each package owns its own
  (`pysentinel2.paths.Paths`, `pycopdem.paths.Paths`, …), computed from
  a `Config` and/or a `Troi`. Nothing derived is ever user-set;
  nothing user-set is ever derived.

## Layered APIs: troi-agnostic core, thin adapters

Data-layer functions take plain geometry and dates —
`fill(bbox, start, end)`, `get_ds(bbox, start, end)` — so they work
without the reproducibility layer. Thin `*_troi` adapters
(`get_ds_troi(troi)`, `fill_troi(troi)`) connect them to `Troi` for
pipelines. New capability goes in the agnostic layer; adapters stay
one line.

## Canonical import

```python
from troi import Troi, Config, config
```

The flat form is canonical everywhere; `troi.troi` / `troi.config`
module paths are implementation layout.

## Store packages: download once, ever

The six data stores (pysentinel2, pycopdem, pyozwald, pysilo, pyslga,
pysmips) share a shape: one machine-wide store on each source's native
grid, a ledger of what's populated, fills that fetch only what's
missing, and derived quantities computed on read rather than stored. A
`Troi` is the unit of request; the store is the unit of storage.

On the `gadi` branches the ledger is filesystem-only (`troi.ledger`),
because Gadi's Lustre makes SQLite unsafe across nodes and no database
server can run there. See [ledger.md](ledger.md) for the primitives and
the write protocol every store follows.

### Stores keep native grids; consumers regrid

A store never resamples. `get_ds` returns the source's own pixels with
the georeferencing attrs `crs`, `transform`, `nodata` and
`native_res_m` (table in [ledger.md](ledger.md)), and has no target-grid
argument. Putting several sources on one grid is the job of the
consumer that owns that grid -- the downscaling model, for the
Sentinel-2 grid -- and it does so from those attrs alone. troi carries
no geospatial dependency.

### Completeness is auditable

Every store has `gaps(bbox, start, end, ...)` returning a
`troi.ledger.GapReport`: the units the request enumerates that are not
present, each classified (`never_fetched`, `absent_upstream` with age,
`before_product_start`, `after_today`, `claimed_in_progress`). A 404
from upstream is recorded as a dated marker for this audit only; it
never stops the next fill from asking again. Fills clamp `end` to today.
