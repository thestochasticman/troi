# The registry — `queries.json`

Every constructed `Troi` is recorded in a single JSON file at
`{config.out_dir}/queries.json` (the filename is historical and kept
for data compatibility). It answers "what has ever been run on this
machine, where, and when" without touching any package's stores.

## Layout

Keyed by region identity (`bbox_hash`), each entry holding the bbox and
its trois:

```json
{
  "<bbox_hash>": {
    "bbox": [148.4636, -34.4275, 148.5081, -34.3681],
    "queries": [
      {"stub": "Milgadara_2018-25",
       "start": "2018-01-01", "end": "2025-12-31",
       "time_hash": "...",
       "created_at": "...", "last_run_at": "..."}
    ]
  }
}
```

## Rules

- **Idempotent re-register**: constructing an identical troi again just
  refreshes its `last_run_at`.
- **Stubs are forever**: reusing a `stub` with a different bbox or a
  different date range raises `ValueError` at construction. A stub
  uniquely names one (region, range) — this is what makes output
  filenames trustworthy.
- **Concurrency-safe**: all access goes through an exclusive `fcntl`
  file lock; concurrent processes serialize on it.
- **Crash-safe enough**: the file is rewritten atomically-in-place under
  the lock (truncate + dump). Losing it loses only bookkeeping — no
  package keys caches off the registry.
