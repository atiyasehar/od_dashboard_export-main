"""Unit tests for GTFS transit overlay helpers (no network)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from transit_overlay import (
    _as_fc,
    _colour,
    _mode_for,
    _read_bundle,
    _resolve_station_id,
    _simplify,
    gtfs_zip_to_layers,
)


def _write_gtfs(path: Path) -> Path:
    files = {
        "routes.txt": (
            "route_id,route_short_name,route_long_name,route_type,route_color\n"
            "1,Green,Line 1,1,00B300\n"
            "2,10,Bus 10,3,112233\n"
        ),
        "trips.txt": (
            "route_id,service_id,trip_id,shape_id\n"
            "1,WD,t1,s1\n"
            "2,WD,t2,s2\n"
        ),
        "shapes.txt": (
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
            "s1,45.50,-73.60,1\n"
            "s1,45.51,-73.59,2\n"
            "s2,45.52,-73.58,1\n"
            "s2,45.53,-73.57,2\n"
        ),
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station,stop_code\n"
            "ST1,Station One,45.50,-73.60,1,,101\n"
            "ST1P,Station One platform,45.5001,-73.6001,0,ST1,101\n"
            "B1,Bus Stop,45.52,-73.58,0,,10\n"
        ),
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "t1,08:00:00,08:00:00,ST1P,1\n"
            "t2,08:10:00,08:10:00,B1,1\n"
        ),
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return path


class TestModeAndColour:
    def test_metro_from_route_type(self):
        assert _mode_for("stm", {"route_type": "1"}) == "metro"

    def test_train(self):
        assert _mode_for("exo", {"route_type": "2"}) == "train"

    def test_bus(self):
        assert _mode_for("stm", {"route_type": "3"}) == "bus"

    def test_rem_feed_wins(self):
        assert _mode_for("rem", {"route_type": "3"}) == "rem"

    def test_rem_monorail_type(self):
        assert _mode_for("stm", {"route_type": "12"}) == "rem"

    def test_premier_name_is_not_rem(self):
        assert _mode_for("stm", {"route_type": "3", "route_long_name": "PREMIER"}) == "bus"

    def test_colour_from_gtfs(self):
        assert _colour({"route_color": "00B300"}, "metro") == "#00B300"

    def test_colour_fallback(self):
        assert _colour({}, "train") == "#6d28d9"


class TestSimplifyAndBundle:
    def test_simplify_keeps_ends(self):
        pts = [[i, i] for i in range(400)]
        out = _simplify(pts, max_pts=40)
        assert out[0] == pts[0]
        assert out[-1] == pts[-1]
        assert len(out) <= 42

    def test_read_bundle(self, tmp_path: Path):
        payload = {
            "geojson": _as_fc([{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}, "properties": {}}]),
            "stops": _as_fc([]),
        }
        p = tmp_path / "t.json"
        p.write_text(__import__("json").dumps(payload), encoding="utf-8")
        got = _read_bundle(p)
        assert got and got["feature_count"] == 1
        assert got["stop_count"] == 0


class TestGtfsZip:
    def test_layers_from_fixture(self, tmp_path: Path):
        zpath = _write_gtfs(tmp_path / "mini.zip")
        lines, stops = gtfs_zip_to_layers(zpath, "stm")
        modes = {(f["properties"]["mode"]) for f in lines}
        assert modes == {"metro", "bus"}
        stop_ids = {f["properties"]["stop_id"] for f in stops}
        assert "ST1" in stop_ids
        assert "B1" in stop_ids
        metro_stop = next(f for f in stops if f["properties"]["stop_id"] == "ST1")
        assert metro_stop["properties"]["mode"] == "metro"
        assert metro_stop["properties"]["name"] == "Station One"

    def test_parent_station_collapse(self):
        idx = {
            "ST1": {"parent_station": "", "location_type": "1"},
            "ST1P": {"parent_station": "ST1", "location_type": "0"},
        }
        assert _resolve_station_id(idx, "ST1P") == "ST1"
        assert _resolve_station_id(idx, "ST1") == "ST1"


class TestPrefetch:
    def test_offline_without_cache_does_not_raise(self, tmp_path: Path):
        from transit_overlay import prefetch_transit_network

        prefetch_transit_network(tmp_path, allow_download=False, background=False)
