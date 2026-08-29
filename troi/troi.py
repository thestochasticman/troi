from attrs import frozen, field, Factory as F
from contextlib import contextmanager
from typing_extensions import Self
from troi.config import Config
from troi.config import config as default_config
from hashlib import sha256
from datetime import date, datetime
from os import makedirs
from tabulate import tabulate
from os.path import exists
import json
import fcntl
import os

encode = lambda x: sha256(x.encode()).hexdigest()
build_from_input = F(lambda s: encode(''.join([str(s.bbox), str(s.start), str(s.end)])), takes_self=True)

# Snap bbox to ~100m precision (3 decimal degrees) before hashing, so
# near-identical bboxes share a region identity (bbox_hash) in the registry.
# 0.001° ≈ 111m at the equator, ≈ 91m at -35° latitude.
_AOI_PRECISION = 3

def check_if_stub_exists(stub: str, hash_map: dict[str, ])->bool:
    return True if stub in hash_map else False

@contextmanager
def locked_registry(path):
    with open(path, 'a+') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        data = json.load(f) if os.path.getsize(path) else {}
        yield data
        f.seek(0); f.truncate()
        json.dump(data, f, indent=2)
@frozen
class Troi:
    """A request to run a pipeline over a region and time range.

    The identity layer of the Borevitz Lab ecosystem: *this region, these
    dates, this name*. Immutable and hashable — two queries with the same
    inputs are the same troi and share every cached artefact downstream.
    Storage layout is deliberately NOT this class's concern: packages
    compose with a ``Troi`` (no inheritance) and derive their own cache
    locations in a per-package ``Paths`` class, typically from
    ``bbox_hash`` / ``time_hash``.

    Attributes:
        bbox: Bounding box ``[west, south, east, north]`` in EPSG:4326
            (decimal degrees).
        start: Inclusive start date.
        end: Inclusive end date.
        stub: Short identifier used in every output filename. Defaults to
            a SHA-256 hash of ``(bbox, start, end)`` so repeat runs collide
            with their cached outputs. Pass an explicit string for
            human-readable filenames.
        tmp_dir: Per-troi intermediates directory
            (``{config.tmp_dir}/{stub}``). Created on init.
        out_dir: Per-troi final-outputs directory
            (``{config.out_dir}/{stub}``). Created on init.
        bbox_hash: Region identity — bbox snapped to ~100 m, then SHA-256.
            Keys the persistent registry.
        time_hash: Date-range identity (SHA-256 of ``start`` + ``end``).
        centre_lon: Centre longitude of ``bbox`` (derived).
        centre_lat: Centre latitude of ``bbox`` (derived).

    Example:
        ```python
        from datetime import date
        from troi.troi import Troi

        q = Troi(
            bbox=[148.46, -34.39, 148.50, -34.36],
            start=date(2023, 1, 1),
            end=date(2023, 12, 31),
            stub='milgadara',
        )
        q.out_dir  # '.../Troi-Outputs/milgadara'
        ```
    """

    bbox: list[float]
    start: date
    end: date
    stub: str = field(default=build_from_input)
    config: Config = default_config

    bbox_hash: str = field(init=False)
    time_hash: str = field(init=False)
    tmp_dir: str = field(init=False)
    out_dir: str = field(init=False)
    centre_lon: float = field(init=False)
    centre_lat: float = field(init=False)

    bbox_hash.default(lambda s: encode(str([round(c, _AOI_PRECISION) for c in s.bbox])))
    time_hash.default(lambda s: encode(f'{s.start}{s.end}'))
    tmp_dir.default(lambda s: f'{s.config.tmp_dir}/{s.stub}')
    out_dir.default(lambda s: f'{s.config.out_dir}/{s.stub}')
    centre_lon.default(lambda s: (s.bbox[0] + s.bbox[2])/2)
    centre_lat.default(lambda s: (s.bbox[1] + s.bbox[3])/2)

    def __attrs_post_init__(s: Self):
        makedirs(s.tmp_dir, exist_ok=True)
        makedirs(s.out_dir, exist_ok=True)
        s.register()

    def register(s: Self) -> None:
        """Insert this troi into the persistent registry indexed by bbox_hash.

        Idempotent on exact match — re-registering an identical troi updates
        its ``last_run_at`` instead of appending a duplicate.

        Raises:
            ValueError: If ``stub`` is already registered with any different
                attribute (bbox, start, or end). Stubs must uniquely identify
                a troi.
        """
        now = datetime.utcnow().isoformat() + 'Z'
        with locked_registry(s.config.hash_file) as registry:
            for other_bbox_hash, other_entry in registry.items():
                for q in other_entry['queries']:
                    if q['stub'] != s.stub:
                        continue
                    if other_bbox_hash != s.bbox_hash:
                        raise ValueError(
                            f"stub '{s.stub}' is already registered under a "
                            f"different bbox {other_entry['bbox']}; pick a "
                            f"unique stub or check your inputs."
                        )
                    if q['time_hash'] != s.time_hash:
                        raise ValueError(
                            f"stub '{s.stub}' is already registered with a "
                            f"different time range [{q['start']}, {q['end']}]; "
                            f"pick a unique stub or check your inputs."
                        )
                    q['last_run_at'] = now
                    return
            entry = registry.setdefault(s.bbox_hash, {
                'bbox': s.bbox,
                'queries': [],
            })
            entry['queries'].append({
                'stub': s.stub,
                'start': str(s.start),
                'end': str(s.end),
                'time_hash': s.time_hash,
                'created_at': now,
                'last_run_at': now,
            })

    def __str__(s)->str:
        return str(
            tabulate(
                [
                    ['stub', s.stub],
                    ['bbox', s.bbox],
                    ['start', s.start],
                    ['end', s.end]
                ]
            )
        )

    @classmethod
    def from_lat_lon(cls, lat: float, lon: float, buffer_km: float, start: date, end: date, stub: str = None, config: Config=default_config):
        """Build a Troi from a centre point and a square buffer in kilometres.

        Convenience constructor for users who think in "X km around a point"
        rather than bounding-box corners. The km-to-degrees conversion is
        approximate (treats the Earth locally as a sphere) which is fine
        for the buffer sizes typical of paddock-scale work (≲50 km).

        Args:
            lat: Centre latitude in decimal degrees (EPSG:4326).
            lon: Centre longitude in decimal degrees (EPSG:4326).
            buffer_km: Half-side of the square buffer, in kilometres. The
                resulting bbox spans ``2 * buffer_km`` on each side.
            start: Inclusive start date.
            end: Inclusive end date.
            stub: Optional human-readable stub identifier. If omitted, a
                SHA-256 hash of the inputs is used.

        Returns:
            Troi: Instance with ``bbox = [west, south, east, north]``.

        Example:
            ```python
            from datetime import date
            from troi.troi import Troi

            q = Troi.from_lat_lon(
                lat=-34.38,
                lon=148.48,
                buffer_km=2.0,
                start=date(2023, 1, 1),
                end=date(2023, 12, 31),
                stub='milgadara',
            )
            ```
        """
        # Convert km to degrees (approximate)
        # 1 degree latitude ≈ 111 km
        # 1 degree longitude ≈ 111 km * cos(latitude)
        import math
        lat_buffer = buffer_km / 111.0
        lon_buffer = buffer_km / (111.0 * math.cos(math.radians(lat)))

        bbox = [
            lon - lon_buffer,  # west (minX)
            lat - lat_buffer,  # south (minY)
            lon + lon_buffer,  # east (maxX)
            lat + lat_buffer   # north (maxY)
        ]

        if stub is not None:
            return cls(bbox=bbox, start=start, end=end, stub=stub, config=config)
        else:
            return cls(bbox=bbox, start=start, end=end, config=config)

    @classmethod
    def build_from_paddocks(
        cls,
        paddocks_filepath: str,
        start: date,
        end: date,
        stub: str = None,
        label_col: str = None,
        geometry_col: str = None,
        crs: str = 'EPSG:4326',
        config=default_config
    ):
        """Build a Troi from a paddocks file, enveloping all geometries into a bbox.

        Reads paddock geometries from a GeoPackage, Shapefile, or GeoJSON
        and computes a bounding box that contains all features.

        Args:
            paddocks_filepath: Path to the paddocks file. Supported formats:
                - GeoPackage (``.gpkg``)
                - Shapefile (``.shp``)
                - GeoJSON (``.geojson``, ``.json``)
            start: Inclusive start date.
            end: Inclusive end date.
            stub: Optional human-readable stub identifier. If omitted, a
                SHA-256 hash of the inputs is used.
            label_col: Column name containing paddock labels/names (e.g.
                ``'title'``, ``'name'``, ``'paddock'``). If provided, the
                column is renamed to ``'paddock'`` for downstream compatibility.
            geometry_col: Column name containing geometry data. For standard
                geo formats this is auto-detected.
            crs: Coordinate reference system to assume if the file has none.
                Default ``'EPSG:4326'``. The bbox is always returned in EPSG:4326.

        Returns:
            Troi: Instance with ``bbox = [west, south, east, north]``
            encompassing all paddock geometries.

        Example:
            ```python
            from datetime import date
            from troi.troi import Troi

            # From GeoJSON with custom label column
            q = Troi.build_from_paddocks(
                paddocks_filepath='/path/to/paddocks.json',
                start=date(2023, 1, 1),
                end=date(2023, 12, 31),
                label_col='title',
                stub='my_farm',
            )
            ```
        """
        import geopandas as gpd
        from pathlib import Path

        filepath = Path(paddocks_filepath)
        suffix = filepath.suffix.lower()

        if suffix in ('.gpkg', '.shp', '.geojson', '.json'):
            gdf = gpd.read_file(paddocks_filepath)
        else:
            raise ValueError(
                f"Unsupported file format: '{suffix}'. "
                f"Supported: .gpkg, .shp, .geojson, .json"
            )

        # Set geometry column if specified
        if geometry_col is not None and geometry_col in gdf.columns:
            gdf = gdf.set_geometry(geometry_col)

        # Rename label column to 'paddock' if specified
        if label_col is not None:
            if label_col not in gdf.columns:
                raise ValueError(
                    f"Label column '{label_col}' not found. "
                    f"Available columns: {list(gdf.columns)}"
                )
            gdf = gdf.rename(columns={label_col: 'paddock'})

        # Set CRS if missing
        if gdf.crs is None:
            gdf = gdf.set_crs(crs)

        # Reproject to EPSG:4326 if needed
        if gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs('EPSG:4326')

        # Get total bounds: (minx, miny, maxx, maxy)
        minx, miny, maxx, maxy = gdf.total_bounds
        bbox = [float(minx), float(miny), float(maxx), float(maxy)]

        if stub is not None:
            return cls(bbox=bbox, start=start, end=end, stub=stub, config=config)
        else:
            return cls(bbox=bbox, start=start, end=end, config=config)


import tempfile


def _temp_config():
    """Return a Config rooted in a fresh tmpdir so tests don't pollute the real registry."""
    tmpdir = tempfile.mkdtemp(prefix='troi_test_')
    return Config(out_dir=tmpdir, tmp_dir=tmpdir)

def test_instantiation():
    troi = Troi(
        bbox=[148.36265, -33.52606, 148.38265, -33.50606],
        start=date(2020, 1, 1),
        end=date(2021, 12, 31),
        stub='RANDOM_TROI_QUERY_2',
        config=_temp_config(),
    )
    return True

def test__str__():
    from unittest.mock import patch
    troi = Troi(
        bbox=[148.36265, -33.52606, 148.38265, -33.50606],
        start=date(2020, 1, 1),
        end=date(2021, 12, 31),
        stub='RANDOM_TROI_QUERY_2',
        config=_temp_config(),
    )
    def print_troi(troi: Troi):
        from unittest.mock import patch
        print(troi)
        return True
    with patch('builtins.print') as mock_print:
        result = print_troi(troi)
        return result is True


def test_add_different_troi_with_same_bbox():
    """Adding a different troi with the same bbox should append under the same bbox_hash."""
    cfg = _temp_config()
    bbox = [148.36265, -33.52606, 148.38265, -33.50606]

    q1 = Troi(bbox=bbox, start=date(2020, 1, 1), end=date(2021, 12, 31),
               stub='query_one', config=cfg)
    q2 = Troi(bbox=bbox, start=date(2022, 1, 1), end=date(2023, 12, 31),
               stub='query_two', config=cfg)

    if q1.bbox_hash != q2.bbox_hash:
        return False

    with open(cfg.hash_file) as f:
        registry = json.load(f)

    if len(registry) != 1:
        return False
    entry = registry[q1.bbox_hash]
    if len(entry['queries']) != 2:
        return False
    stubs = {q['stub'] for q in entry['queries']}
    return stubs == {'query_one', 'query_two'}


def test_stub_collision_raises():
    """Reusing a stub with different attributes must raise ValueError."""
    cfg = _temp_config()
    Troi(
        bbox=[148.36265, -33.52606, 148.38265, -33.50606],
        start=date(2020, 1, 1), end=date(2021, 12, 31),
        stub='collide', config=cfg,
    )
    try:
        Troi(
            bbox=[149.0, -34.0, 149.1, -33.9],
            start=date(2020, 1, 1), end=date(2021, 12, 31),
            stub='collide', config=cfg,
        )
    except ValueError:
        return True
    return False


def test():
    return all([
        test_instantiation(),
        test__str__(),
        test_add_different_troi_with_same_bbox(),
        test_stub_collision_raises(),
    ])

if __name__ == '__main__':
    print(test())
