"""What a store remembers about data upstream does not have.

A fill asks the datastore for a unit and gets a 404. The store records
that fact -- *what* was asked, *when*, *what came back* -- as a dated
marker under ``absent/``, and nothing else. The marker never suppresses
a fetch: the next fill asks again, because the day may have been
published since. Only the store's ``gaps()`` audit reads absent
markers, to say "this unit is missing because upstream did not have it
N days ago" rather than "this store never asked".

Also here: :func:`clamp_end`, so no fill ever asks for a day in the
future, and :func:`gdal_no_404_cache`, because GDAL's ``/vsicurl/``
remembers a 404 for the life of the process and would otherwise turn
"ask again" into "never ask again" inside a long-running job.
"""
from datetime import date, datetime, timezone
from typing import Optional

from troi.ledger import Markers, now_iso


def clamp_end(end: date, today: Optional[date] = None) -> date:
    """``end``, but never after today (UTC)."""
    today = today or datetime.now(timezone.utc).date()
    return min(end, today)


def record_absent(markers: Markers, parts, status: int = 404, **detail) -> dict:
    """Write ``absent/<parts>.json`` saying upstream answered ``status``
    for this unit just now. ``parts`` are relative to the store's
    ``absent`` marker root (``markers`` should be rooted there)."""
    return markers.write(parts, {'status': status, 'checked_at': now_iso(), **detail})


def absent_age_days(marker: Optional[dict], today: Optional[date] = None) -> Optional[int]:
    """Days since the absent marker was last written, or None if no marker."""
    if not marker:
        return None
    today = today or datetime.now(timezone.utc).date()
    checked = datetime.fromisoformat(marker['checked_at']).date()
    return (today - checked).days


def gdal_no_404_cache(*url_prefixes: str) -> dict:
    """Environment entries for ``rasterio.Env`` that stop GDAL caching
    directory listings and 404s for these URL prefixes, so a re-ask
    reaches the network. Prefixes are given as plain ``https://...``
    URLs and translated to ``/vsicurl/`` paths."""
    paths = [p if p.startswith('/vsicurl/') else f'/vsicurl/{p}' for p in url_prefixes]
    return {'CPL_VSIL_CURL_NON_CACHED': ':'.join(paths)}


# -- offline tests ------------------------------------------------------------

def test_clamp_end():
    t = date(2026, 10, 5)
    return clamp_end(date(2026, 12, 31), t) == t and clamp_end(date(2020, 1, 1), t) == date(2020, 1, 1)


def test_record_and_age():
    import tempfile
    m = Markers(tempfile.mkdtemp(prefix='troi_absent_test_'))
    rec = record_absent(m, ('totalbucket', 2026, '2026-10-04'), unit='2026-10-04')
    back = m.read(('totalbucket', 2026, '2026-10-04'))
    today = datetime.now(timezone.utc).date()
    return (back['status'] == 404 and back['unit'] == '2026-10-04'
            and absent_age_days(back, today) == 0 and absent_age_days(None) is None
            and rec['checked_at'] == back['checked_at'])


def test_gdal_env():
    env = gdal_no_404_cache('https://data.tern.org.au/model-derived/smips',
                            '/vsicurl/https://x.org/y')
    return env == {'CPL_VSIL_CURL_NON_CACHED':
                   '/vsicurl/https://data.tern.org.au/model-derived/smips:/vsicurl/https://x.org/y'}


def test():
    return all([test_clamp_end(), test_record_and_age(), test_gdal_env()])


if __name__ == '__main__':
    print(test())
