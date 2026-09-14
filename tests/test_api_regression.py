"""Flask API regression tests for dashboard pages and OD endpoints.

Database-backed checks skip when PostgreSQL is down. Transit uses on-disk GTFS
cache when present and skips if the overlay cannot be built (offline, no cache).
"""

from __future__ import annotations

import pytest


def _json(resp):
    data = resp.get_json(silent=True)
    assert data is not None, resp.get_data(as_text=True)[:800]
    return data


def _skip_unless_db(db_available: bool) -> None:
    if not db_available:
        pytest.skip("PostgreSQL (od_dashboard) is not reachable")


@pytest.fixture(scope="module")
def sample_geo_ids(client, db_available):
    _skip_unless_db(db_available)
    resp = client.get("/api/od/zone_map?include_geojson=0&island_only=1")
    if resp.status_code == 503:
        pytest.skip("OD zone tables are not loaded")
    assert resp.status_code == 200
    zones = _json(resp).get("zones") or []
    ids = [str(z.get("geo_id")) for z in zones if z.get("geo_id")]
    if len(ids) < 2:
        pytest.skip("need at least two zones in zone_map")
    return ids[:3]


class TestHealthAndPages:
    def test_health_shape(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = _json(resp)
        assert data.get("service") == "popgen-od-dashboard"
        assert "ok" in data
        assert "dbname" in data
        assert "db_port" in data
        assert "deploy" in data

    def test_health_ok_when_db_up(self, client, db_available):
        _skip_unless_db(db_available)
        data = _json(client.get("/api/health"))
        assert data["ok"] is True

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/od.html",
            "/od-buildings.html",
            "/od-flows.html",
            "/od-zones-boundary.html",
        ],
    )
    def test_pages_html(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "<html" in html.lower() or "<!doctype" in html.lower()

    def test_dashboard_config_js(self, client):
        resp = client.get("/assets/dashboard-config.js")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "DashConfig" in body or "__dashDeploy" in body


class TestOdApis:
    def test_home(self, client, db_available):
        _skip_unless_db(db_available)
        resp = client.get("/api/od/home")
        if resp.status_code == 503:
            pytest.skip("OD home tables missing")
        assert resp.status_code == 200
        data = _json(resp)
        assert "stats" in data or "error" not in data

    def test_zone_map_no_geojson(self, client, sample_geo_ids):
        resp = client.get("/api/od/zone_map?include_geojson=0&island_only=1")
        assert resp.status_code == 200
        data = _json(resp)
        assert isinstance(data.get("zones"), list)
        assert len(data["zones"]) >= 2
        assert not data.get("geojson")
        row = data["zones"][0]
        assert row.get("geo_id")

    def test_zone_codes(self, client, db_available):
        _skip_unless_db(db_available)
        resp = client.get("/api/od/zone_codes")
        assert resp.status_code == 200
        data = _json(resp)
        assert isinstance(data.get("zone_codes"), dict)
        assert isinstance(data.get("zone_names"), dict)

    def test_zone_sidebar_requires_geo_id(self, client):
        resp = client.get("/api/od/zone_sidebar")
        assert resp.status_code == 400
        assert _json(resp).get("error")

    def test_zone_sidebar_single(self, client, sample_geo_ids):
        gid = sample_geo_ids[0]
        resp = client.get(f"/api/od/zone_sidebar?geo_id={gid}")
        assert resp.status_code == 200
        data = _json(resp)
        assert data.get("geo_id") == gid or data.get("geo_ids") == [gid]
        assert data.get("zone_count") == 1
        assert isinstance(data.get("stats"), dict)

    def test_zone_sidebar_multi_aggregates(self, client, sample_geo_ids):
        a, b = sample_geo_ids[0], sample_geo_ids[1]
        one = _json(client.get(f"/api/od/zone_sidebar?geo_id={a}"))
        two = _json(client.get(f"/api/od/zone_sidebar?geo_id={b}"))
        both = _json(client.get(f"/api/od/zone_sidebar?geo_ids={a},{b}"))
        assert both.get("zone_count") == 2
        assert both.get("geo_ids") == [a, b]
        assert both.get("zone_label") == "2 zones"
        s1 = float((one.get("stats") or {}).get("trips") or 0)
        s2 = float((two.get("stats") or {}).get("trips") or 0)
        sb = float((both.get("stats") or {}).get("trips") or 0)
        assert sb == pytest.approx(s1 + s2, rel=1e-4, abs=1e-3)

    def test_incoming_flow_requires_dest(self, client):
        resp = client.get("/api/od/zone_incoming_flow")
        assert resp.status_code == 400

    def test_incoming_flow_single_and_multi(self, client, sample_geo_ids):
        a, b = sample_geo_ids[0], sample_geo_ids[1]
        one = client.get(f"/api/od/zone_incoming_flow?dest_geo_id={a}&limit=5")
        assert one.status_code == 200
        payload = _json(one)
        assert payload.get("dest_geo_id") == a or a in (payload.get("dest_geo_ids") or [])
        assert isinstance(payload.get("flows"), list)

        both = client.get(f"/api/od/zone_incoming_flow?dest_geo_id={a},{b}&limit=5")
        assert both.status_code == 200
        merged = _json(both)
        assert merged.get("dest_geo_ids") == [a, b]
        origs = {str(f.get("orig_geo_id")) for f in (merged.get("flows") or [])}
        assert a not in origs
        assert b not in origs

    def test_building_scale(self, client, db_available):
        _skip_unless_db(db_available)
        resp = client.get("/api/od/building_emission_scale")
        if resp.status_code == 503:
            pytest.skip("building emissions table missing")
        assert resp.status_code == 200
        data = _json(resp)
        assert "min_g" in data or "error" not in data

    def test_building_map_needs_zone(self, client, db_available):
        _skip_unless_db(db_available)
        resp = client.get("/api/od/building_map?limit=100")
        if resp.status_code == 503:
            pytest.skip("building emissions table missing")
        assert resp.status_code == 200
        data = _json(resp)
        assert data.get("buildings") == []
        assert data.get("hint")

    def test_building_map_with_zone(self, client, sample_geo_ids):
        gid = sample_geo_ids[0]
        resp = client.get(
            f"/api/od/building_map?zone_geo_id={gid}&limit=50&include_footprints=0"
        )
        if resp.status_code == 503:
            pytest.skip("building emissions table missing")
        assert resp.status_code == 200
        data = _json(resp)
        assert isinstance(data.get("buildings"), list)
        joined = str(data.get("zone_geo_id") or "")
        assert gid in joined or data.get("zone_geo_id") is None

    def test_building_map_multi_zone(self, client, sample_geo_ids):
        a, b = sample_geo_ids[0], sample_geo_ids[1]
        resp = client.get(
            f"/api/od/building_map?zone_geo_ids={a},{b}&limit=50&include_footprints=0"
        )
        if resp.status_code == 503:
            pytest.skip("building emissions table missing")
        assert resp.status_code == 200
        data = _json(resp)
        assert isinstance(data.get("buildings"), list)

    def test_montreal_boundary(self, client, db_available):
        _skip_unless_db(db_available)
        resp = client.get("/api/montreal_boundary.geojson")
        assert resp.status_code == 200
        data = _json(resp)
        assert data.get("type") == "FeatureCollection"


class TestTransitApi:
    def test_rail_overlay(self, client):
        resp = client.get("/api/od/transit_network?group=rail")
        if resp.status_code == 503:
            pytest.skip(_json(resp).get("message") or "transit overlay unavailable")
        assert resp.status_code == 200
        data = _json(resp)
        assert data.get("source") == "gtfs"
        gj = data.get("geojson") or {}
        assert gj.get("type") == "FeatureCollection"
        assert int(data.get("feature_count") or 0) >= 1
        stops = data.get("stops") or {}
        assert stops.get("type") == "FeatureCollection"
        modes = {
            (f.get("properties") or {}).get("mode")
            for f in (gj.get("features") or [])
        }
        assert modes & {"metro", "rem", "train"}

    def test_bus_overlay(self, client):
        resp = client.get("/api/od/transit_network?group=bus")
        if resp.status_code == 503:
            pytest.skip(_json(resp).get("message") or "transit overlay unavailable")
        assert resp.status_code == 200
        data = _json(resp)
        assert data.get("source") == "gtfs"
        gj = data.get("geojson") or {}
        assert gj.get("type") == "FeatureCollection"
        assert int(data.get("feature_count") or 0) >= 1
