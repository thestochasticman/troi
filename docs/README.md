# troi — reference documentation

`troi` (Time and Region Of Interest) is the shared core of the Borevitz
Lab ecosystem: the identity object every package answers, and the
configuration every package reads. It contains exactly two things and a
set of conventions.

| Page | What it covers |
|---|---|
| [The `Troi` class](troi.md) | The frozen identity object: fields, hashing, the three constructors |
| [`Config`](config.md) | Where data lives and which credentials to use; exact resolution order |
| [The registry](registry.md) | `queries.json`: stub uniqueness, file locking, crash safety |
| [Ecosystem conventions](conventions.md) | Composition over inheritance, `Config` vs `Paths`, troi-agnostic layers |

```python
from datetime import date
from troi import Troi

troi = Troi(bbox=[148.36, -33.53, 148.38, -33.51],
            start=date(2024, 1, 1), end=date(2024, 12, 31),
            stub='my_farm')
```
