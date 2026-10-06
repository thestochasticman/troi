# The filesystem ledger

Why every store on the `gadi` branch keeps its ledger as files, what the
pieces are, and the protocol they all follow. This is the canonical
description; each store's README shows the same structure with its own
units, keys and directory names.

## The ecosystem at a glance

```mermaid
flowchart LR
    subgraph core ["troi (stdlib only)"]
        direction TB
        T["Troi · Config · Paths<br/>identity and locations"]
        LG["troi.ledger<br/>Markers · Claim · Claims<br/>ensure_array · Gap · GapReport"]
        AB["troi.absent<br/>clamp_end · record_absent<br/>gdal_no_404_cache"]
    end
    subgraph stores ["six stores, each on its source's native grid"]
        direction TB
        S1["pysmips<br/>~1 km · daily · EPSG:4326"]
        S2["pysilo<br/>0.05° points · daily since 1889"]
        S3["pyozwald<br/>0.005° / 0.05° / 0.1° · daily, 8-day"]
        S4["pyslga<br/>~90 m · static"]
        S5["pycopdem<br/>30 m · static"]
        S6["pysentinel2<br/>10 m · EPSG:6933 · per scene day"]
    end
    subgraph consumers
        direction TB
        P["PaddockTS<br/>plots · status --gaps"]
        E["DownscalingMoistureModel<br/>emt.sources → emt.regrid<br/>everything onto the Sentinel-2 grid"]
    end
    core --> stores
    stores --> P
    stores --> E
```

- **troi** owns identity (`Troi`), configuration (`Config`) and the ledger
  primitives. It carries no geospatial dependency.
- **A store** owns one machine-wide Zarr store on its source's own lattice,
  a ledger of what is populated, `fill` that fetches only what is missing,
  `get_ds` that returns native pixels with georeferencing attrs, and `gaps`
  that audits completeness. A store never resamples.
- **A consumer** reads stores. Only the downscaling model needs every source
  on one grid, and it does that regridding itself from the attrs.

## Why not a database

The stores run as many PBS jobs on many Gadi nodes against one store on
Lustre. Gadi mounts `/g/data` and `/scratch` with `localflock`: POSIX file
locks are only visible within one node. SQLite relies on those locks in every
journal mode, so two jobs on different nodes sharing an `index.db` can corrupt
it. No persistent database server can run on Gadi either: login nodes kill
daemons and compute jobs end at walltime.

What Lustre's metadata server does serialise across nodes is `mkdir`,
`rename`/`replace` and `link`. `troi.ledger` is built from those alone.

## Anatomy of a store

Every store directory has the same four kinds of thing, and the three
entry points touch them in the same way.

```mermaid
flowchart LR
    subgraph root ["{tmp_dir}/&lt;name&gt;_store/"]
        direction TB
        Z[("&lt;name&gt;.zarr<br/>native pixels<br/>sparse: only written chunks exist")]
        M["ledger/ (or the chunk files themselves)<br/>which units are populated"]
        A["absent/<br/>dated upstream 404s"]
        C["claims/<br/>mkdir mutexes<br/>empty when nothing is being written"]
    end
    FILL(["fill(bbox, start, end)"])
    GET(["get_ds(bbox, start, end)"])
    GAPS(["gaps(bbox, start, end)"])
    GET -->|"1 · fill first"| FILL
    GET -->|"2 · read native window"| Z
    FILL -->|"① take"| C
    FILL -->|"② read block · fetch · write"| Z
    FILL -->|"③ commit marker"| M
    FILL -.->|"on 404"| A
    FILL -->|"④ release"| C
    GAPS --> M
    GAPS --> A
    GAPS --> C
```

| Piece | What it holds | Who writes it | Who reads it |
|---|---|---|---|
| `<name>.zarr` | Native pixels, one sparse array per variable or product. Only chunks that were written exist on disk. | `fill` | `get_ds` |
| `ledger/` | One small JSON marker per populated unit. For stores whose unit is a whole chunk, the chunk file itself is the ledger and there is no `ledger/`. | `fill`, after the pixels | `fill` (to diff), `gaps` |
| `absent/` | One dated JSON per upstream 404. A fact about upstream at a moment, never a reason to skip a fetch. | `fill` | `gaps` only |
| `claims/` | One directory per unit currently being written. Empty when the store is idle. | `fill` | `fill` (mutex), `gaps` (to report `claimed_in_progress`) |

## Primitives (`troi.ledger`)

**`Markers(root)`** — one JSON file per written unit, at
`root/<p1>/.../<leaf>.json`. Written to a temporary name and moved into place
with `os.replace`, so a reader sees either nothing or a complete file. Last
writer wins, which is fine because every writer of a unit writes the same
fact. Shard directories so none holds more than a few thousand entries.

**`Claim(root, key, lease_s=600, wait_s=3600)`** — a cross-node mutex:
`mkdir root/claims/<key>` succeeds for exactly one process. Others poll with
jitter. The claim carries a lease, which is what makes a crashed job's claim
recoverable:

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Free
    Free --> Held : mkdir succeeds
    Held --> Held : heartbeat or keepalive refreshes the mtime
    Held --> Free : release removes the directory
    Held --> Expired : holder dies or stalls past lease_s
    Expired --> Free : a waiter renames it aside and removes it
    note right of Held
        A waiter whose mkdir fails polls with jitter
        until Free, or raises ClaimTimeout after wait_s.
        Only one renamer of an expired claim wins.
    end note
```

A holder that will work longer than the lease wraps that work in
`with claim.keepalive():`, which runs `heartbeat()` on a daemon thread. A
holder whose work is always far shorter than the lease needs no heartbeat.
`Claims(root, keys)` acquires several claims in sorted order, so two jobs
wanting overlapping sets cannot deadlock, and has the same `keepalive()`.

**`ensure_array(root, group, name, **kw)`** — creates a zarr array exactly
once across processes, under a claim on `('meta', name)`, so cold-start jobs
never race on `zarr.json`.

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
    data  = fetch(...)            # network; keepalive if long
    write(zarr, block | data)     # zarr 3 commits each chunk by rename
    markers.write(block_key, ...) # the ledger now says these units exist
```

Two jobs on two nodes asking for the same block:

```mermaid
sequenceDiagram
    participant A as Job A (node 1)
    participant L as Lustre (claims, zarr, ledger)
    participant B as Job B (node 2)
    participant U as Upstream
    A->>L: diff request against markers → block missing
    B->>L: diff request against markers → block missing
    A->>L: mkdir claims/block ✓ held
    B->>L: mkdir claims/block ✗ exists, mtime fresh
    loop poll with jitter, until Free or wait_s
        B->>L: stat claims/block
    end
    A->>L: re-diff under the claim → still missing
    A->>U: fetch the missing units (keepalive refreshes the claim)
    A->>L: read block · merge · write chunk (temp + rename)
    A->>L: write marker (temp + rename)
    A->>L: remove claims/block
    B->>L: mkdir claims/block ✓ held
    B->>L: re-diff under the claim → nothing missing
    B->>L: remove claims/block (no network)
```

The re-diff under the claim is what turns the second job's work into a
no-op. The diff before the claim is only an optimisation: a fully present
request takes no claim and touches no network.

What each failure point leaves behind:

| Crash or stall | On disk afterwards | Next fill |
|---|---|---|
| Before the chunk write | nothing new | fetches again |
| After the chunk, before the marker | pixels the ledger does not know about | refetches and rewrites them; idempotent |
| After the marker | a complete unit | no work |
| Holder stalls past the lease without heartbeat | a stale claim directory | taken over; the taker re-reads the block, so a half-applied write is redone, not merged |
| Holder dies mid-write | possibly a chunk file without its marker, plus a stale claim | as above |

Non-404 per-unit errors are held until the units that did succeed are
written and marked, then raised. A failing day never discards its block.

## Two kinds of ledger

Which ledger a store keeps follows from the size of its fetch unit relative
to a Zarr chunk.

```mermaid
flowchart TB
    subgraph marker ["Marker ledger — fetch unit smaller than a chunk"]
        direction LR
        m1["fetch units<br/>a day · a point-span · a pixel rect"] --> m2["one Zarr chunk<br/>shared by many units<br/>read-modify-write under a claim"] --> m3["marker says<br/>which units are in<br/>(day mask · through date · rect)"]
    end
    subgraph exist ["Existence ledger — fetch unit is the chunk"]
        direction LR
        e1["fetch unit<br/>one whole chunk"] --> e2["zarr 3 LocalStore writes it<br/>by temp file + rename"] --> e3["chunk file exists ⇒ done<br/>no marker needed"]
    end
```

The existence ledger needs `write_empty_chunks=True` at array creation.
Without it an all-NaN chunk (ocean) is never materialised and looks
unfetched forever.

| Store | Fetch unit | Ledger | Claim key | Lease | Heartbeat |
|---|---|---|---|---|---|
| pysmips | one COG window per (day, 128 px chunk), 8 days in flight | marker: 64-char day mask per (product, time chunk, chunk) | every (product, tc, cy, cx) of the time chunk, via `Claims` | 600 s | keepalive |
| pysilo | one DataDrill CSV per (point, missing span), 4 in flight | marker: `through` date per point per (block, year) | `('silo', by, bx)` | 900 s | keepalive |
| pyozwald | one OPeNDAP window per (variable, year, block), 4 in flight | marker: `through` date per (cadence, variable, year, block) | `(cadence, var, year, by, bx)` | 900 s | keepalive |
| pycopdem | one COG window per 1200 px chunk | existence of the chunk file | `('dem', cy, cx)` | 600 s | none, seconds of work |
| pyslga | one COG window per 1200 px chunk per layer | existence of the chunk file; `layers/<key>.json` registers a layer | `('slga', key, cy, cx)`; `('layer', key)` | 600 s | none, seconds of work |
| pysentinel2 | one bulk `odc.stac.load` per (window, batch of days), 16 threads | marker: coverage rect per (day, uuid) | `('s2', day)`, taken around the writes only | 1800 s | none, download happens outside the claim |

## Absent markers (`troi.absent`)

A 404 writes `absent/<unit>.json` with `status` and `checked_at`. That is a
fact about upstream at a moment, not a verdict. **It never suppresses a
fetch**: the next fill asks again. Only `gaps()` reads it, to report
`absent_upstream` with an age instead of `never_fetched`.

`clamp_end(end)` keeps fills from asking for the future.
`gdal_no_404_cache(url_prefix)` gives `rasterio.Env` the setting that stops
GDAL remembering a 404 for the life of the process.

## What `gaps()` reports

Every store enumerates the units a request would need, exactly as `fill`
does, and classifies each one that is not present. No network.

```mermaid
flowchart TD
    U["expected unit of the request"] --> P{"ledger says present?"}
    P -- yes --> OK(["present"])
    P -- no --> S{"before the product's<br/>first date?"}
    S -- yes --> BPS(["before_product_start"])
    S -- no --> T{"after today?"}
    T -- yes --> AT(["after_today"])
    T -- no --> AB{"absent marker<br/>for this unit?"}
    AB -- yes --> AU(["absent_upstream<br/>+ days since checked"])
    AB -- no --> C{"claim directory<br/>exists?"}
    C -- yes --> CIP(["claimed_in_progress"])
    C -- no --> NF(["never_fetched"])
```

`report.complete` is true when nothing is `never_fetched` or
`claimed_in_progress`. After a successful fill of a published range the only
expected statuses are `absent_upstream` for days the source has not
published yet, `before_product_start` and `after_today`.

## Native georeferencing contract

Stores never resample. Every raster `get_ds` sets:

| attr | meaning |
|---|---|
| `crs` | e.g. `'EPSG:4326'` |
| `transform` | six affine numbers `(a, b, c, d, e, f)`, pixel-edge origin, north-up |
| `nodata` | the native nodata value, or `None` if NaN already |
| `native_res_m` | approximate pixel size in metres, for documentation |

Coordinates are pixel centres. Any regridding happens in the consumer that
owns the target grid, from these attrs alone. See
[conventions.md](conventions.md).
