"""Frontend multi-select helpers (Node). Skips if node is not on PATH."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "dashboard" / "assets" / "dashboard-zone-ui.js"

RUNNER = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');
const ctx = { console };
ctx.window = ctx;
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(code, ctx);
const U = ctx.DashZoneUi;
if (!U) { throw new Error('DashZoneUi missing'); }
const out = {
  parse: U.parseGeoIdList('1, 2;2  3'),
  format: U.formatGeoIdList(['10', '20']),
  toggleAdd: U.toggleGeoId(['1'], '2'),
  toggleRemove: U.toggleGeoId(['1', '2'], '1'),
  multi: U.clickIsMultiToggle({ ctrlKey: true }),
  singleClick: U.clickIsMultiToggle({}),
  label: U.selectionLabel(['a', 'b']),
  agg: U.aggregateZoneStats([
    { trips: 10, total_emissions_g: 100, total_distance_km: 2 },
    { trips: 5, total_emissions_g: 50, total_distance_km: 1 },
  ]),
  cats: U.mergeCategoryRows([
    [{ category: 'work', trips: 1, total_emissions_g: 10 }],
    [{ category: 'work', trips: 2, total_emissions_g: 20 }, { category: 'shop', trips: 1, total_emissions_g: 5 }],
  ]),
};
const flows = U.mergeIncomingPayloads(['A', 'B'], [
  {
    dest_geo_id: 'A', dest_zone_trips: 10, dest_zone_emissions_g: 100,
    flows: [
      { orig_geo_id: 'B', trips: 1, total_emissions_g: 10 },
      { orig_geo_id: 'X', trips: 2, total_emissions_g: 20 },
    ],
  },
  {
    dest_geo_id: 'B', dest_zone_trips: 8, dest_zone_emissions_g: 80,
    flows: [{ orig_geo_id: 'X', trips: 3, total_emissions_g: 30 }],
  },
]);
out.flowOrigs = (flows.flows || []).map((f) => f.orig_geo_id);
out.flowXTrips = (flows.flows || []).find((f) => f.orig_geo_id === 'X').trips;
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def zone_ui():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    proc = subprocess.run(
        ["node", "-e", RUNNER, str(JS)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        pytest.fail(proc.stderr or proc.stdout or "node failed")
    return json.loads(proc.stdout)


def test_parse_and_toggle(zone_ui):
    assert zone_ui["parse"] == ["1", "2", "3"]
    assert zone_ui["format"] == "10,20"
    assert zone_ui["toggleAdd"] == ["1", "2"]
    assert zone_ui["toggleRemove"] == ["2"]


def test_click_and_label(zone_ui):
    assert zone_ui["multi"] is True
    assert zone_ui["singleClick"] is False
    assert zone_ui["label"] == "2 zones"


def test_aggregate_and_categories(zone_ui):
    assert zone_ui["agg"]["trips"] == 15
    assert zone_ui["agg"]["total_emissions_g"] == 150
    assert zone_ui["agg"]["zone_count"] == 2
    cats = {c["category"]: c for c in zone_ui["cats"]}
    assert cats["work"]["trips"] == 3
    assert cats["shop"]["total_emissions_g"] == 5


def test_merge_incoming_drops_intra(zone_ui):
    assert zone_ui["flowOrigs"] == ["X"]
    assert zone_ui["flowXTrips"] == 5
