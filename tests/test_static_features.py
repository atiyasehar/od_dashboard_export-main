"""Static regression checks for map assets and page wiring (no Flask, no DB)."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "dashboard"
ASSETS = DASH / "assets"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestBasemap:
    def test_no_carto_tiles(self):
        text = _read("dashboard/assets/dashboard-map-basemap.js")
        assert "cartocdn" not in text.lower()
        assert "basemaps.cartocdn.com" not in text.lower()

    def test_osm_streets_and_dim(self):
        text = _read("dashboard/assets/dashboard-map-basemap.js")
        assert "tile.openstreetmap.org" in text
        assert "dash-basemap-dim" in text
        assert "brightness(0.62)" in text


class TestTransitAssets:
    def test_pin_svg_and_export(self):
        text = _read("dashboard/assets/dashboard-map-transit.js")
        assert "DashMapTransit" in text
        assert "fill-rule=\"evenodd\"" in text or "fill-rule='evenodd'" in text
        assert "ellipse" in text
        assert "circleMarker" in text
        assert "Transit overlay" in text or "transitPane" in text

    @pytest.mark.parametrize(
        "page",
        [
            "dashboard/od.html",
            "dashboard/od-buildings.html",
            "dashboard/od-flows.html",
            "dashboard/od-zones-boundary.html",
        ],
    )
    def test_pages_attach_transit(self, page):
        html = _read(page)
        assert "dashboard-map-transit.js" in html
        assert "DashMapTransit.attach" in html
        assert "dashboard-map-basemap.js" in html


class TestZoneUiAsset:
    def test_multi_select_helpers_exported(self):
        text = _read("dashboard/assets/dashboard-zone-ui.js")
        assert "parseGeoIdList" in text
        assert "toggleGeoId" in text
        assert "clickIsMultiToggle" in text
        assert "aggregateZoneStats" in text
        assert "mergeIncomingPayloads" in text
        assert "renderSelectionChips" in text
        assert "global.DashZoneUi" in text


class TestNoCursorWatermarks:
    def test_dashboard_html_js_clean(self):
        hits = []
        for path in list(DASH.rglob("*.html")) + list(ASSETS.glob("*.js")):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "cursor.com" in text.lower() or "Made with Cursor" in text:
                hits.append(str(path.relative_to(ROOT)))
        assert hits == []
