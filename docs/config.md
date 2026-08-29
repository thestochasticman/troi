# `Config`

Where data lives and which credentials to use. A frozen attrs class:

| Field | Default | Used by |
|---|---|---|
| `out_dir` | `~/Documents/Troi-Outputs` | Every package's final outputs (`{out_dir}/{stub}`) |
| `tmp_dir` | `~/Downloads/Troi-Tmp` | Every package's stores and intermediates |
| `hash_file` | `{out_dir}/queries.json` | The [registry](registry.md) |
| `email` | `None` | pysilo (SILO requires an email) |
| `tern_api_key` | `None` | pyslga (TERN API key) |

## Resolution order — exact semantics

The module-level `config` singleton resolves **once, at import**, from
the first source that exists. Sources do **not** merge:

1. **Config file** — `~/.config/Troi.json` (path overridable via
   `TROI_CONFIG`). If this file exists it is the whole config:
   `Config(**json)` — fields absent from the file fall back to the
   *class defaults*, and **env vars are ignored entirely**.
2. **Environment variables** — used only when no config file exists:
   `TROI_OUTDIR`, `TROI_TMPDIR`, `TROI_EMAIL`, `TROI_TERN_KEY`.
3. **Built-in defaults** — the table above.

```json
// ~/.config/Troi.json
{"out_dir": "/data/outputs", "tmp_dir": "/data/tmp",
 "email": "you@example.org", "tern_api_key": "..."}
```

Inspect what resolved:

```bash
python -m troi.config
```

## Per-call configs

Bypass the singleton entirely — tests and multi-root setups construct
their own:

```python
from troi import Config, Troi

cfg = Config(out_dir='/data/outputs', tmp_dir='/data/tmp')
t = Troi(..., config=cfg)
```

Constructing a `Config` creates both directories if missing.
