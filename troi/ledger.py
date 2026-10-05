"""Filesystem-only ledger primitives for stores on shared filesystems.

The lab's stores run as many PBS jobs on many Gadi nodes against one
store on Lustre. Gadi mounts ``/g/data`` and ``/scratch`` with
``localflock``, so file locks are node-local and SQLite (any journal
mode) is unsafe across nodes. What *is* atomic across nodes on Lustre is
what the metadata server serialises: ``mkdir``, ``rename``/``replace``,
``link``. Everything here is built from those three and the standard
library only.

Two primitives:

- :class:`Markers` -- one small JSON file per written unit of a store
  (a block, a chunk, a point-year). A marker is written to a temporary
  name and moved into place with :func:`os.replace`, so a reader sees
  either no marker or a complete one, never a partial one. Last writer
  wins, which is fine because every writer of a unit writes the same
  fact.
- :class:`Claim` -- a cross-node mutex made of a directory. ``mkdir``
  succeeds for exactly one process; everyone else waits. A claim carries
  a lease: if its directory's mtime is older than ``lease_s`` the holder
  is presumed dead and the claim is taken over (``rename`` it aside --
  only one renamer wins -- then remove it). Long-running holders call
  :meth:`Claim.heartbeat` (or wrap work in :meth:`Claim.keepalive`) to
  refresh the mtime.

The protocol every store follows for a unit that is read-modify-written
(a Zarr block shared by several fetch units)::

    with Claim(root, key):          # one writer per block
        block = read(zarr)          # see what is already there
        data = fetch(...)           # network
        write(zarr, block | data)   # zarr 3 commits each chunk by rename
        markers.write(key, {...})   # ledger says: these units are present
    # release

A crash before the marker leaves data on disk the ledger does not know
about; the next fill refetches and rewrites it, which is idempotent. A
crash after the marker loses nothing. A stale claim is taken over after
``lease_s``; the taker re-reads the block, so a half-applied write is
simply redone.

Units written as whole Zarr chunks (pycopdem, pyslga, pyozwald) need no
marker: zarr 3's ``LocalStore`` writes every chunk via temp file +
replace, so the chunk file's existence is the ledger.

:class:`Gap` / :class:`GapReport` are the shared vocabulary for every
store's ``gaps(bbox, start, end)`` audit.
"""
import json
import os
import random
import shutil
import socket
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Iterable, Optional

from attrs import frozen, field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def atomic_write_json(path: str, payload: dict) -> None:
    """Write ``payload`` to ``path`` so that no reader ever sees a partial file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{uuid.uuid4().hex}.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(payload, f, separators=(',', ':'), sort_keys=True)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _key_name(key) -> str:
    """Directory-safe name for a claim key (a string or a tuple of parts)."""
    parts = key if isinstance(key, (tuple, list)) else (key,)
    return '-'.join(str(p).replace('/', '_') for p in parts)


# -- markers -----------------------------------------------------------------

@frozen
class Markers:
    """A tree of JSON markers under ``root``: ``root/<p1>/.../<leaf>.json``.

    ``parts`` is a sequence of path components; the last is the leaf
    name. Keep directories to a few thousand entries (Lustre directory
    listings slow down past that) by sharding -- e.g. pysmips uses
    ``(product, time_chunk, 'cy_cx')``.
    """
    root: str

    def path(self, parts) -> str:
        parts = [str(p) for p in parts]
        return os.path.join(self.root, *parts[:-1], f'{parts[-1]}.json')

    def write(self, parts, payload: dict) -> dict:
        """Atomically write a marker; returns the payload as stored
        (``written_at``, ``host``, ``pid`` are added)."""
        full = {**payload, 'written_at': now_iso(),
                'host': socket.gethostname(), 'pid': os.getpid()}
        atomic_write_json(self.path(parts), full)
        return full

    def read(self, parts) -> Optional[dict]:
        try:
            with open(self.path(parts)) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def exists(self, parts) -> bool:
        return os.path.exists(self.path(parts))

    def list(self, dir_parts) -> list[str]:
        """Leaf names (without ``.json``) in a marker directory, sorted.
        Temporary files are never listed."""
        d = os.path.join(self.root, *[str(p) for p in dir_parts])
        try:
            names = os.listdir(d)
        except FileNotFoundError:
            return []
        return sorted(n[:-5] for n in names if n.endswith('.json'))

    def remove(self, parts) -> bool:
        try:
            os.unlink(self.path(parts))
            return True
        except FileNotFoundError:
            return False


# -- claims ------------------------------------------------------------------

class ClaimTimeout(TimeoutError):
    """Waited ``wait_s`` for a live claim that was never released."""


class Claim:
    """A cross-node mutex: ``mkdir root/claims/<key>``.

    Args:
        root: Store root; claims live in ``root/claims``.
        key: A string or tuple of parts identifying the unit.
        lease_s: A claim whose directory mtime is older than this is
            presumed abandoned and taken over.
        wait_s: How long to wait for a live claim before raising
            :class:`ClaimTimeout`. ``None`` waits forever.
        poll_s: Base poll interval while waiting (jittered).
    """

    def __init__(self, root: str, key, lease_s: float = 600.0,
                 wait_s: Optional[float] = 3600.0, poll_s: float = 1.0):
        self.root = root
        self.key = key
        self.dir = os.path.join(root, 'claims', _key_name(key))
        self.lease_s = lease_s
        self.wait_s = wait_s
        self.poll_s = poll_s
        self.held = False
        self.took_over = 0            # stale claims removed on the way in

    # -- lifecycle --
    def acquire(self) -> 'Claim':
        os.makedirs(os.path.dirname(self.dir), exist_ok=True)
        t0 = time.monotonic()
        while True:
            try:
                os.mkdir(self.dir)
            except FileExistsError:
                if self._take_over_if_stale():
                    continue
                if self.wait_s is not None and time.monotonic() - t0 > self.wait_s:
                    raise ClaimTimeout(f'claim {self.dir} held by another process '
                                       f'for more than {self.wait_s}s')
                time.sleep(self.poll_s * (0.5 + random.random()))
                continue
            self.held = True
            try:
                with open(os.path.join(self.dir, 'owner.json'), 'w') as f:
                    json.dump({'host': socket.gethostname(), 'pid': os.getpid(),
                               'acquired_at': now_iso()}, f)
            except OSError:
                pass                    # the directory is the lock; owner.json is advisory
            return self

    def _take_over_if_stale(self) -> bool:
        """Remove a claim whose lease has expired. True if we removed one
        (or someone else did meanwhile), so the caller should retry."""
        try:
            age = time.time() - os.stat(self.dir).st_mtime
        except FileNotFoundError:
            return True                 # released between our mkdir and stat
        if age <= self.lease_s:
            return False
        aside = f'{self.dir}.stale.{uuid.uuid4().hex}'
        try:
            os.rename(self.dir, aside)  # only one renamer wins
        except (FileNotFoundError, OSError):
            return True                 # someone else took it over; retry
        shutil.rmtree(aside, ignore_errors=True)
        self.took_over += 1
        return True

    def heartbeat(self) -> None:
        """Refresh the lease. Call at least every ``lease_s / 2`` while
        holding the claim across long work (or use :meth:`keepalive`)."""
        if self.held:
            try:
                os.utime(self.dir, None)
            except FileNotFoundError:
                pass

    def release(self) -> None:
        if self.held:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.held = False

    def __enter__(self) -> 'Claim':
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()

    # -- helpers --
    def keepalive(self, every_s: Optional[float] = None):
        """Context manager running :meth:`heartbeat` on a daemon thread."""
        return _KeepAlive(self, every_s or max(5.0, self.lease_s / 4))


class _KeepAlive:
    def __init__(self, claim: Claim, every_s: float):
        self.claim, self.every_s = claim, every_s

    def __enter__(self):
        import threading
        self._stop = threading.Event()

        def run():
            while not self._stop.wait(self.every_s):
                self.claim.heartbeat()
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)


class Claims:
    """Acquire several claims in sorted key order (deadlock-free) and
    release them all on exit."""

    def __init__(self, root: str, keys: Iterable, **claim_kw):
        self.claims = [Claim(root, k, **claim_kw) for k in sorted(keys, key=_key_name)]

    def __enter__(self) -> list[Claim]:
        acquired = []
        try:
            for c in self.claims:
                c.acquire()
                acquired.append(c)
        except BaseException:
            for c in reversed(acquired):
                c.release()
            raise
        return self.claims

    def __exit__(self, *exc) -> None:
        for c in reversed(self.claims):
            c.release()

    def heartbeat(self) -> None:
        for c in self.claims:
            c.heartbeat()


def ensure_array(root: str, group, name: str, **create_kw):
    """``group.create_array(name, **create_kw)`` exactly once across
    processes: creation runs under a claim on ``('meta', name)`` and is
    skipped if another process got there first. ``group`` is a zarr
    group (duck-typed; troi does not import zarr)."""
    if name in group:
        return group[name]
    with Claim(root, ('meta', name)):
        if name in group:
            return group[name]
        return group.create_array(name=name, **create_kw)


# -- gaps --------------------------------------------------------------------

STATUSES = ('never_fetched', 'absent_upstream', 'before_product_start',
            'after_today', 'claimed_in_progress')


@frozen
class Gap:
    """One expected unit that is not present, and why."""
    unit: tuple
    status: str = field()
    age_days: Optional[int] = None      # absent_upstream: days since last 404
    detail: Optional[str] = None

    @status.validator
    def _check(self, attribute, value):
        if value not in STATUSES:
            raise ValueError(f'status must be one of {STATUSES}, got {value!r}')


@frozen
class GapReport:
    """Result of a store's ``gaps(bbox, start, end)``.

    ``expected`` is the number of units the request enumerates; ``gaps``
    lists the ones not present. A report is *complete* when nothing is
    ``never_fetched`` or ``claimed_in_progress`` -- everything else is a
    fact about the upstream record, not about this store.
    """
    expected: int
    gaps: tuple = ()

    @property
    def counts(self) -> Counter:
        return Counter(g.status for g in self.gaps)

    @property
    def complete(self) -> bool:
        c = self.counts
        return c['never_fetched'] == 0 and c['claimed_in_progress'] == 0

    def summary(self) -> str:
        c = self.counts
        present = self.expected - len(self.gaps)
        lines = [f'{present}/{self.expected} units present']
        for s in STATUSES:
            if c[s]:
                lines.append(f'  {s}: {c[s]}')
        ages = [g.age_days for g in self.gaps if g.status == 'absent_upstream' and g.age_days is not None]
        if ages:
            lines.append(f'  absent last checked {min(ages)}..{max(ages)} days ago')
        return '\n'.join(lines)

    def __bool__(self) -> bool:
        return self.complete


# -- offline tests ------------------------------------------------------------

def _tmp_root() -> str:
    import tempfile
    return tempfile.mkdtemp(prefix='troi_ledger_test_')


def test_marker_roundtrip_and_listing():
    m = Markers(_tmp_root())
    m.write(('p', 7, 'a'), {'days': '0101'})
    m.write(('p', 7, 'b'), {'days': '1111'})
    got = m.read(('p', 7, 'a'))
    return (
        got['days'] == '0101' and 'written_at' in got and 'host' in got
        and m.read(('p', 7, 'zzz')) is None
        and m.list(('p', 7)) == ['a', 'b']
        and m.list(('nope',)) == []
        and m.remove(('p', 7, 'a')) and not m.remove(('p', 7, 'a'))
        and m.list(('p', 7)) == ['b']
    )


def test_marker_write_leaves_no_temp_files():
    m = Markers(_tmp_root())
    for i in range(50):
        m.write(('d', i), {'i': i})
    names = os.listdir(os.path.join(m.root, 'd'))
    return len(names) == 50 and all(n.endswith('.json') for n in names)


def _claim_worker(root, counter_path, cycles):
    for _ in range(cycles):
        with Claim(root, 'counter', poll_s=0.01):
            try:
                with open(counter_path) as f:
                    n = json.load(f)
            except FileNotFoundError:
                n = 0
            with open(counter_path, 'w') as f:
                json.dump(n + 1, f)


def test_claim_is_mutually_exclusive_across_processes():
    """Two processes increment a shared, unlocked counter under the claim
    200 times each; no increment may be lost."""
    import multiprocessing as mp
    root = _tmp_root()
    counter = os.path.join(root, 'counter.json')
    ctx = mp.get_context('fork')
    ps = [ctx.Process(target=_claim_worker, args=(root, counter, 200)) for _ in range(2)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=120)
    with open(counter) as f:
        n = json.load(f)
    return n == 400 and all(p.exitcode == 0 for p in ps) and not os.listdir(os.path.join(root, 'claims'))


def test_stale_claim_is_taken_over_once():
    root = _tmp_root()
    stale = Claim(root, ('p', 1, 2)).acquire()
    old = time.time() - 10_000
    os.utime(stale.dir, (old, old))
    fresh = Claim(root, ('p', 1, 2), lease_s=600, wait_s=5, poll_s=0.01)
    with fresh:
        held = os.path.isdir(fresh.dir)
        leftovers = [n for n in os.listdir(os.path.join(root, 'claims')) if 'stale' in n]
    return held and fresh.took_over == 1 and leftovers == [] and not os.path.exists(fresh.dir)


def test_live_claim_blocks_then_times_out():
    root = _tmp_root()
    holder = Claim(root, 'x').acquire()
    t0 = time.monotonic()
    try:
        Claim(root, 'x', lease_s=600, wait_s=0.3, poll_s=0.05).acquire()
        return False
    except ClaimTimeout:
        waited = time.monotonic() - t0
    finally:
        holder.release()
    return 0.3 <= waited < 3


def test_heartbeat_refreshes_lease():
    root = _tmp_root()
    with Claim(root, 'hb') as c:
        old = time.time() - 10_000
        os.utime(c.dir, (old, old))
        c.heartbeat()
        age = time.time() - os.stat(c.dir).st_mtime
    return age < 5


def test_claims_many_sorted_and_released():
    root = _tmp_root()
    with Claims(root, [('b', 2), ('a', 9), ('a', 1)]) as cs:
        order = [c.key for c in cs]
        all_held = all(os.path.isdir(c.dir) for c in cs)
    return (order == [('a', 1), ('a', 9), ('b', 2)] and all_held
            and os.listdir(os.path.join(root, 'claims')) == [])


def test_ensure_array_creates_once():
    class FakeGroup(dict):
        created = 0

        def create_array(self, name, **kw):
            FakeGroup.created += 1
            self[name] = kw
            return kw
    root = _tmp_root()
    g = FakeGroup()
    a = ensure_array(root, g, 'elev', shape=(2, 2))
    b = ensure_array(root, g, 'elev', shape=(2, 2))
    return a is b and FakeGroup.created == 1 and os.listdir(os.path.join(root, 'claims')) == []


def test_gap_report():
    r = GapReport(expected=10, gaps=(
        Gap(('a', 1), 'never_fetched'),
        Gap(('a', 2), 'absent_upstream', age_days=3),
        Gap(('a', 3), 'before_product_start'),
    ))
    ok_status = True
    try:
        Gap(('x',), 'bogus')
        ok_status = False
    except ValueError:
        pass
    r2 = GapReport(expected=3, gaps=(Gap(('a', 2), 'absent_upstream', age_days=1),))
    return (not r.complete and r.counts['never_fetched'] == 1
            and '7/10 units present' in r.summary() and 'absent last checked 3..3' in r.summary()
            and ok_status and r2.complete and bool(r2))


def test():
    return all([
        test_marker_roundtrip_and_listing(),
        test_marker_write_leaves_no_temp_files(),
        test_claim_is_mutually_exclusive_across_processes(),
        test_stale_claim_is_taken_over_once(),
        test_live_claim_blocks_then_times_out(),
        test_heartbeat_refreshes_lease(),
        test_claims_many_sorted_and_released(),
        test_ensure_array_creates_once(),
        test_gap_report(),
    ])


if __name__ == '__main__':
    print(test())
