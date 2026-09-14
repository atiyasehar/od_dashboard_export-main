"""Unit tests for geo-id parsing, SQL filters, flow merge, and labels (no database)."""

from __future__ import annotations

from dashboard_server import (
    _building_zone_filter_sql,
    _parse_geo_id_list,
    _zone_geo_id_pred,
)
from run_dashboard import _od10_merge_incoming_payloads, _od10_zone_selection_label, _str_to_bool


class TestParseGeoIdList:
    def test_empty(self):
        assert _parse_geo_id_list(None, "", []) == []

    def test_single(self):
        assert _parse_geo_id_list("562") == ["562"]

    def test_comma_and_space(self):
        assert _parse_geo_id_list("1, 2;3  4") == ["1", "2", "3", "4"]

    def test_dedupe_preserves_order(self):
        assert _parse_geo_id_list("10,20,10", "20,30") == ["10", "20", "30"]

    def test_list_input(self):
        assert _parse_geo_id_list(["101", "102"], "103") == ["101", "102", "103"]


class TestZoneSql:
    def test_single_equals(self):
        sql, params = _zone_geo_id_pred("z.geo_id", ["562"])
        assert "= %s" in sql
        assert "ANY" not in sql
        assert params == ("562",)

    def test_multi_any(self):
        sql, params = _zone_geo_id_pred("z.geo_id", ["1", "2"])
        assert "= ANY(%s)" in sql
        assert params == (["1", "2"],)

    def test_empty_false(self):
        sql, params = _zone_geo_id_pred("z.geo_id", [])
        assert sql == "FALSE"
        assert params == ()

    def test_building_filter_multi(self):
        sql, params = _building_zone_filter_sql(None, "lat", "lon", "10,11")
        assert sql.startswith(" AND ")
        assert "ANY" in sql
        assert params == (["10", "11"],)

    def test_building_filter_none(self):
        sql, params = _building_zone_filter_sql(None, "lat", "lon", None)
        assert sql == ""
        assert params == ()


class TestIncomingMerge:
    def test_drops_intra_selection_origins(self):
        payloads = [
            {
                "dest_geo_id": "A",
                "dest_lat": 45.5,
                "dest_lon": -73.6,
                "dest_zone_trips": 10,
                "dest_zone_emissions_g": 1000,
                "dest_zone_distance_km": 5,
                "flows": [
                    {"orig_geo_id": "B", "trips": 3, "total_emissions_g": 300, "total_distance_km": 1, "orig_lat": 45.4, "orig_lon": -73.5},
                    {"orig_geo_id": "X", "trips": 4, "total_emissions_g": 400, "total_distance_km": 2, "orig_lat": 45.3, "orig_lon": -73.4},
                ],
            },
            {
                "dest_geo_id": "B",
                "dest_lat": 45.6,
                "dest_lon": -73.7,
                "dest_zone_trips": 8,
                "dest_zone_emissions_g": 800,
                "dest_zone_distance_km": 4,
                "flows": [
                    {"orig_geo_id": "A", "trips": 2, "total_emissions_g": 200, "total_distance_km": 1},
                    {"orig_geo_id": "X", "trips": 1, "total_emissions_g": 100, "total_distance_km": 0.5},
                ],
            },
        ]
        out = _od10_merge_incoming_payloads(["A", "B"], payloads)
        origs = {f["orig_geo_id"] for f in out["flows"]}
        assert origs == {"X"}
        assert out["dest_geo_ids"] == ["A", "B"]
        assert out["zone_label"] == "2 zones"
        x = out["flows"][0]
        assert x["trips"] == 5
        assert x["total_emissions_g"] == 500
        assert out["dest_zone_trips"] == 18
        assert out.get("intra_zone")


class TestLabelsAndFlags:
    def test_multi_zone_label(self):
        assert _od10_zone_selection_label(["1", "2"], {}, {}) == "2 zones"

    def test_single_zone_label(self):
        label = _od10_zone_selection_label(["562"], {"562": "CT-1"}, {"562": "Ville-Marie"})
        assert "Ville-Marie" in label
        assert "CT-1" in label

    def test_empty_label(self):
        assert _od10_zone_selection_label([], {}, {}) == ""

    def test_str_to_bool(self):
        assert _str_to_bool(True) is True
        assert _str_to_bool("true") is True
        assert _str_to_bool("0") is False
        assert _str_to_bool(None) is False
