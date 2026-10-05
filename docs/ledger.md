# The filesystem ledger

Why every store on the `gadi` branch keeps its ledger as files, and the
protocol they all follow.

## Why not a database

The stores run as many PBS jobs on many Gadi nodes against one store on
Lustre. Gadi mounts `/g/data` and `/scratch` with `localflock`: POSIX file
locks are only visible within one node. SQLite relies on those locks in every
journal mode, so two jobs on different nodes sharing an `index.db` can corrupt
it. No persistent database server can run on Gadi either: login nodes kill
daemons and compute jobs end at walltime.

What Lustre's metadata server does serialise across nodes is `mkdir`,
`rename`/`replace` and `link`. `troi.ledger` is built from those alone.

## Primitives (`troi.ledger`)

**`Markers(root)`** — one JSON file per written unit, at
`root/<p1>/.../<leaf>.json`. Written to a temporary name and moved into place
with `os.replace`, so a reader sees either nothing or a complete file. Shard
directories so none holds more than a few thousand entries.

**`Claim(root, key, lease_s=600)`** — a cross-node mutex: `mkdir
root/claims/<key>` succeeds for exactly one process. Others poll with jitter.
If a claim directory's mtime is older than `lease_s` the holder is presumed
dead: the directory is renamed aside (one renamer wins) and removed, and the
taker proceeds. Holders refresh the lease with `heartbeat()` or by wrapping
long work in `with claim.keepalive():`. `Claims(root, keys)` acquires several
in sorted order.

**`ensure_array(root, group, name, **kw)`** — creates a zarr array exactly once
across processes.

**`Gap` / `GapReport`** — the vocabulary of every store's `gaps()` audit.
Statuses: `never_fetched`, `absent_upstream`, `before_product_start`,
`after_today`, `claimed_in_progress`. A report is complete when nothing is
`never_fetched` or `claimed_in_progress`; the rest describe the upstream
record, not this store.

## The write protocol

For a unit that shares a Zarr chunk with other units (pysmips' 64-day blocks,
pysentinel2's pixel rectangles, pysilo's 8×8-point blocks):

```
with Claim(root, block_key):      # one writer per block, across nodes
    block = read(zarr)            # what is already there
    data  = fetch(...)            # network; heartbeat if long
    write(zarr, block | data)     # zarr 3 commits each chunk by rename
    markers.write(block_key, ...) # the ledger now says these units exist
```

- Crash before the marker: data may be on disk that the ledger does not know
  about. The next fill refetches and rewrites it. Idempotent.
- Crash after the marker: nothing lost.
- Stale claim: taken over after `lease_s`. The taker re-reads the block, so a
  half-applied write is redone, not merged.

For a unit that *is* a whole Zarr chunk (pycopdem and pyslga 1200 px chunks,
pyozwald year blocks) no marker is needed: zarr 3's `LocalStore` writes each
chunk via temp file + replace, so the chunk file's existence is the ledger.
Create those arrays with `write_empty_chunks=True`, or an all-NaN chunk (ocean)
is never written and looks unfetched forever.

## Absent markers (`troi.absent`)

A 404 writes `absent/<unit>.json` with `status` and `checked_at`. That is a
fact about upstream at a moment, not a verdict. **It never suppresses a
fetch**: the next fill asks again. Only `gaps()` reads it, to report
`absent_upstream` with an age instead of `never_fetched`.

`clamp_end(end)` keeps fills from asking for the future.
`gdal_no_404_cache(url_prefix)` gives `rasterio.Env` the setting that stops
GDAL remembering a 404 for the life of the process.

## Native georeferencing contract

Stores never resample. Every raster `get_ds` sets:

| attr | meaning |
|---|---|
| `crs` | e.g. `'EPSG:4326'` |
| `transform` | six affine numbers `(a, b, c, d, e, f)`, pixel-edge origin, north-up |
| `nodata` | the native nodata value, or `None` if NaN already |
| `native_res_m` | approximate pixel size in metres, for documentation |

Coordinates are pixel centres. Any regridding happens in the consumer that
owns the target grid, from these attrs alone.
