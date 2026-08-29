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

The five data stores (pysentinel2, pycopdem, pyozwald, pysilo, pyslga)
share a shape: one machine-wide store on a fixed grid, a ledger of
what's populated, fills that fetch only what's missing, and derived
quantities computed on read rather than stored. A `Troi` is the unit
of request; the store is the unit of storage.
