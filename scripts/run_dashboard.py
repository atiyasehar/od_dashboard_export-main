"""Flask server for the OD emissions dashboard (PM23 survey / precomputed tables).

Serves ``dashboard/`` and ``/api/od/*`` endpoints. Reads precomputed tables in
PostgreSQL.

Run::

  python scripts/run_dashboard.py
  python scripts/run_dashboard.py --db-name od_dashboard
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from functools import wraps
from pathlib import Path

import psycopg2
from flask import Flask, Response, jsonify, redirect, request, send_from_directory

try:
    from flask_cors import CORS
except ImportError:  # CORS optional; same-origin works without it
    CORS = None

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dashboard_server as _dashboard_server  # noqa: E402

from zone_map_anchors import fetch_zone_flow_anchors, patch_flow_payload_anchors  # noqa: E402

from dashboard_server import (  # noqa: E402
    CMM_BOUNDS,
    MONTREAL_BOUNDS,
    MONTREAL_ISLAND_LAT_MAX,
    MONTREAL_ISLAND_LAT_MIN,
    MONTREAL_ISLAND_LON_MAX,
    MONTREAL_ISLAND_LON_MIN,
    _build_zone_map_geojson,
    _build_zones_boundary_geojson,
    _building_footprint_geojson_sql,
    _building_map_lat_sql,
    _building_map_lon_sql,
    _building_zone_filter_sql,
    _column_exists,
    _building_address_from_footprint_row,
    _fetch_building_footprint_row,
    _attach_zone_code,
    _flow_coord,
    _geom_json_to_feature,
    _resolve_geo_id_query,
    _short_zone_id,
    _zone_code_for,
    _zone_code_index,
    _zone_name_for,
    _zone_name_index,
    _zone_label_for,
    _zone_short_name_for,
    _montreal_island_geometry_geojson_for_postgis,
    _montreal_boundary_geojson_fc,
    _zones_geom_table,
    _zones_geom_join,
    _zone_code_maps_for_geom,
    ensure_zones_geom_compat,
    _normalize_building_by,
    _parse_building_grid_cell_deg,
    _request_island_only,
    _table_exists,
)

DB_PARAMS = dict(_dashboard_server.DB_PARAMS)
SCHEMA = _dashboard_server.SCHEMA


def get_conn():
    return psycopg2.connect(**DB_PARAMS)


def _apply_db_cli(
    *,
    db_host: str | None = None,
    db_port: str | int | None = None,
    db_name: str | None = None,
    db_user: str | None = None,
    db_password: str | None = None,
    db_schema: str | None = None,
) -> None:
    """Override module DB settings from CLI (defaults unchanged when arg is None)."""
    global SCHEMA
    if db_host:
        DB_PARAMS["host"] = db_host
    if db_port is not None and str(db_port).strip():
        DB_PARAMS["port"] = str(db_port)
    if db_name:
        DB_PARAMS["dbname"] = db_name
    if db_user:
        DB_PARAMS["user"] = db_user
    if db_password:
        DB_PARAMS["password"] = db_password
    if db_schema is not None:
        SCHEMA = db_schema.strip()
        _dashboard_server.SCHEMA = SCHEMA
    _dashboard_server.DB_PARAMS.clear()
    _dashboard_server.DB_PARAMS.update(DB_PARAMS)


def _load_deploy_env(path: Path, *, override: bool = False) -> None:
    """Load KEY=VALUE lines into os.environ (for local deploy.env / .env files)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key or not val:
            continue
        if override or key not in os.environ:
            os.environ[key] = val


def _load_default_deploy_env(bundle_root: Path | None = None) -> None:
    roots: list[Path] = []
    if bundle_root:
        roots.append(bundle_root)
    roots.append(Path(__file__).resolve().parent.parent)
    seen: set[Path] = set()
    for root in roots:
        root = root.resolve()
        if root in seen:
            continue
        seen.add(root)
        for name in ("deploy.env", ".env"):
            _load_deploy_env(root / name)


def _data_dir() -> Path:
    return _dashboard_server.REPO_DATA_DIR


def _resolve_bundle_root(explicit: str | None = None) -> Path | None:
    if explicit:
        root = Path(explicit).resolve()
        if (root / "dashboard").is_dir() and (root / "data").is_dir():
            return root
        raise SystemExit(f"Invalid bundle root (need dashboard/ and data/): {root}")
    env = os.environ.get("POPGEN_BUNDLE_ROOT", "").strip()
    if env:
        return _resolve_bundle_root(env)
    candidate = Path(__file__).resolve().parent.parent
    if (
        (candidate / "dashboard").is_dir()
        and (candidate / "data").is_dir()
        and (candidate / "manifest.json").is_file()
    ):
        return candidate
    return None


def _apply_bundle_layout(bundle_root: Path) -> Path:
    _dashboard_server.REPO_DATA_DIR = bundle_root / "data"
    return bundle_root / "dashboard"


_BUNDLE_ROOT = _resolve_bundle_root()
_STATIC_DIR = _apply_bundle_layout(_BUNDLE_ROOT) if _BUNDLE_ROOT else _data_dir() / "dashboard"

from od_table_names import DEFAULT_ROUTES, apply_run_tag  # noqa: E402
from popgen_constants import BUILDINGS_TABLE  # noqa: E402
from zone_map_anchors import (  # noqa: E402
    zone_point_on_surface_lat_sql,
    zone_point_on_surface_lon_sql,
)

OD10_ANCHOR_CANDIDATES = (
    os.environ.get("OD10_ANCHOR_TABLE", "").strip(),
    "zone_flow_anchors_od10",
)

OD10_RUN_TAG = (os.environ.get("OD10_RUN_TAG", "od10") or "od10").strip()

OD10_ZONE_EMISSIONS_CANDIDATES = (
    os.environ.get("OD10_ZONE_EMISSIONS_TABLE", "").strip(),
    apply_run_tag("zone_emissions", OD10_RUN_TAG),
)
OD10_RULES_CANDIDATES = (
    os.environ.get("OD10_RULES_TABLE", "").strip(),
    "zone_emissions_rules_od10",
    "zone_emissions_od10_rules",
)
OD10_DEST_CANDIDATES = (
    os.environ.get("OD10_DEST_TABLE", "").strip(),
    "zone_emissions_dest_od10",
    "zone_emissions_od10_dest",
)
OD10_CATEGORIES_CANDIDATES = (
    os.environ.get("OD10_CATEGORIES_TABLE", "").strip(),
    "zone_emissions_categories_od10",
    "zone_emissions_od10_categories",
)
OD10_FLOW_LIMIT_MAX = int(os.environ.get("OD10_FLOW_LIMIT_MAX", "500") or "500")


def _parse_od10_flow_limit(raw, *, default: int | None = 10) -> int | None:
    """
    Parse ?limit= for incoming-flow endpoints.

    Returns None = all origin zones (no rank cap). Integers 1..OD10_FLOW_LIMIT_MAX slice by rank.
    Accepts limit=all|0|none for full list.
    """
    s = str(raw if raw is not None else "").strip().lower()
    if s in ("", "default"):
        if default is None:
            return None
        s = str(default).strip().lower()
    if s in ("all", "0", "none", "-1", "*"):
        return None
    try:
        n = int(float(s))
    except (TypeError, ValueError):
        if default is None:
            return None
        n = int(default)
    if n is None or n <= 0:
        return None
    return max(1, min(n, max(OD10_FLOW_LIMIT_MAX, 1)))


def _od10_flow_rank_filter(limit: int | None) -> tuple[str, list]:
    """SQL fragment and params for optional rank cap on incoming-flow pair tables."""
    if limit is None:
        return "", []
    return " AND f.rank <= %s", [limit]


def _od10_metrics_weighted() -> bool:
    """Default: PM23 survey-expanded totals in API primary fields (maps, KPIs, flows)."""
    v = (os.environ.get("OD10_METRICS", "weighted") or "weighted").strip().lower()
    return v not in ("legs", "route", "unweighted", "0", "false", "no", "off")


def _od10_metrics_note() -> str:
    if _od10_metrics_weighted():
        return (
            "Primary KPIs = CMM residents, island-touch car legs, expanded with PM23 d_fexp. "
            "Zone map sums rules- or dest-attributed subsets."
        )
    return (
        "trips/emissions_g/distance_km = per routed car leg; "
        "*_weighted = PM23 survey expansion"
    )


def _od10_promote_weighted_row(row: dict) -> dict:
    """Expose weighted totals as primary ``trips`` / ``total_emissions_g`` when configured."""
    if not _od10_metrics_weighted():
        return row
    tw = row.get("trips_weighted")
    ew = row.get("total_emissions_g_weighted") or row.get("emissions_g_weighted")
    dw = row.get("total_distance_km_weighted") or row.get("distance_km_weighted")
    if tw is None and ew is None:
        return row
    if "trips_legs" not in row and row.get("trips") is not None:
        row["trips_legs"] = row["trips"]
    if "total_emissions_g_legs" not in row and row.get("total_emissions_g") is not None:
        row["total_emissions_g_legs"] = row["total_emissions_g"]
    if "total_distance_km_legs" not in row and row.get("total_distance_km") is not None:
        row["total_distance_km_legs"] = row["total_distance_km"]
    if tw is not None:
        row["trips"] = float(tw)
    if ew is not None:
        row["total_emissions_g"] = float(ew)
        row["total_emissions_tonnes"] = float(ew) / 1e6
    if dw is not None:
        row["total_distance_km"] = float(dw)
    trips = float(row.get("trips") or 0)
    emis = float(row.get("total_emissions_g") or 0)
    if trips > 0 and emis >= 0:
        row["avg_emissions_g_per_trip"] = emis / trips
    return row


def _od10_zero_building_metrics(props: dict) -> dict:
    """Ensure trip/emission/distance fields are explicit numbers (0 when missing)."""
    out = dict(props)
    legs = int(out.get("trips_legs") if out.get("trips_legs") is not None else out.get("trips") or 0)
    out["trips_legs"] = legs
    out["trips"] = legs
    out["trips_weighted"] = float(out.get("trips_weighted") if out.get("trips_weighted") is not None else 0)
    out["total_emissions_g"] = float(out.get("total_emissions_g") or 0)
    out["total_distance_km"] = round(float(out.get("total_distance_km") or 0), 2)
    if out.get("trips_assigned_weighted") is None:
        out["trips_assigned_weighted"] = float(out.get("trips_weighted") or 0) if legs > 0 else 0.0
    return out


def _od10_building_panel_row(row: dict) -> dict:
    """Building maps/panels: prefer expanded trips when capacity-split."""
    out = _od10_zero_building_metrics(row)
    legs = int(out.get("trips_legs") or 0)
    weighted = float(out.get("trips_weighted") or 0)
    if out.get("trips_is_capacity_share") or out.get("trips_is_leg_d_fexp_split"):
        out["trips"] = legs
        out["trips_legs"] = legs
        if weighted > 0:
            out["trips_display"] = weighted
    if out.get("trips_assigned_weighted") is None and weighted > 0:
        out["trips_assigned_weighted"] = weighted
    return out


def _od10_building_population_alloc_enabled() -> bool:
    return os.environ.get("OD10_BUILDING_POPULATION_ALLOC", "0").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _od10_zone_geo_col(building_by: str) -> str:
    return "dest_geo_id" if building_by == "dest" else "emission_zone_geo_id"


def _od10_building_capacity_weight_sql(cur, *, b: str = "b", j: str = "j") -> str:
    """Match assignment pools: jobs, else residential units, else sq_ft (work/commercial)."""
    has_jobs = _table_exists(cur, "building_jobs")
    jobs = f"COALESCE({j}.jobs_building::double precision, 0)" if has_jobs else "0"
    units = f"GREATEST(COALESCE(NULLIF({b}.units::double precision, 0), 1), 1)"
    sqft = f"GREATEST(COALESCE({b}.sq_ft::double precision, 1), 1)"
    return f"""CASE
        WHEN ({jobs}) > 0 THEN ({jobs})
        WHEN lower(COALESCE({b}.use_class::text, '')) = 'residential' THEN ({units})
        ELSE ({sqft})
    END"""


def _od10_building_capacity_share(
    cur,
    *,
    detail_tab: str,
    building_by: str,
    building_id: str,
    zone_geo_id: str | None,
) -> dict | None:
    """Zone trip/emission totals split by building household capacity (d_fexp-weighted zone total)."""
    if not _od10_building_population_alloc_enabled():
        return None
    zid = str(zone_geo_id or "").strip()
    if not zid:
        return None
    zcol = _od10_zone_geo_col(building_by)
    has_jobs = _table_exists(cur, "building_jobs")
    jobs_join = (
        f"LEFT JOIN {SCHEMA}.building_jobs j ON j.building_id::text = b.id::text"
        if has_jobs
        else ""
    )
    w_sql = _od10_building_capacity_weight_sql(cur, b="b", j="j")
    emis_c = _od10_detail_emissions_col(cur, detail_tab)
    island_sql = _od10_detail_island_sql(cur, detail_tab)
    cur.execute(
        f"""
        WITH zone_tot AS (
          SELECT COALESCE(SUM(d_fexp), 0)::double precision AS t_exp,
                 COALESCE(SUM({emis_c}), 0)::double precision AS e_exp,
                 COALESCE(SUM(distance_m), 0)::double precision / 1000.0 AS d_exp
          FROM {SCHEMA}.{detail_tab}
          WHERE {island_sql}
            AND split_part(trim({zcol}::text), '.', 1) = %s
        ),
        bldg AS (
          SELECT ({w_sql})::double precision AS w
          FROM {SCHEMA}.{BUILDINGS_TABLE} b
          {jobs_join}
          WHERE b.id::text = %s
            AND split_part(trim(b.zone_geo_id::text), '.', 1) = %s
        ),
        bldg_meta AS (
          SELECT lower(COALESCE(use_class::text, '')) AS uc
          FROM {SCHEMA}.{BUILDINGS_TABLE}
          WHERE id::text = %s
        ),
        w_all AS (
          SELECT COALESCE(SUM(({w_sql})::double precision), 0)::double precision AS w_sum
          FROM {SCHEMA}.{BUILDINGS_TABLE} b
          {jobs_join}
          CROSS JOIN bldg_meta m
          WHERE split_part(trim(b.zone_geo_id::text), '.', 1) = %s
            AND lower(COALESCE(b.use_class::text, '')) = m.uc
        )
        SELECT zt.t_exp * b.w / NULLIF(wa.w_sum, 0),
               zt.e_exp * b.w / NULLIF(wa.w_sum, 0),
               zt.d_exp * b.w / NULLIF(wa.w_sum, 0)
        FROM zone_tot zt
        CROSS JOIN bldg b
        CROSS JOIN w_all wa
        """,
        (zid, building_id, zid, building_id, zid),
    )
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return {
        "trips_weighted": float(row[0] or 0),
        "total_emissions_g": float(row[1] or 0),
        "total_distance_km": round(float(row[2] or 0), 2),
        "trips_is_capacity_share": True,
    }


def _od10_building_population_imputation(
    cur,
    *,
    detail_tab: str,
    building_by: str,
    building_id: str,
    zone_geo_id: str | None,
) -> dict | None:
    """Population-level trips/emissions for a building with no routed legs (capacity share of zone)."""
    shared = _od10_building_capacity_share(
        cur,
        detail_tab=detail_tab,
        building_by=building_by,
        building_id=building_id,
        zone_geo_id=zone_geo_id,
    )
    if not shared:
        return None
    return {
        **shared,
        "trips_is_imputed": True,
        "trips_legs": 0,
        "trips_assigned_weighted": 0.0,
    }


OD10_FLOWS_CANDIDATES = (
    os.environ.get("OD10_FLOWS_TABLE", "").strip(),
    apply_run_tag("zone_incoming_flows", OD10_RUN_TAG),
)
OD10_FLOWS_TOP_CANDIDATES = (
    os.environ.get("OD10_FLOWS_TOP_TABLE", "").strip(),
    apply_run_tag("zone_incoming_flows_top", OD10_RUN_TAG),
)
OD10_FLOWS_TOT_CANDIDATES = (
    os.environ.get("OD10_FLOWS_TOT_TABLE", "").strip(),
    apply_run_tag("zone_incoming_flows_totals", OD10_RUN_TAG),
)
OD10_DEST_FLOWS_TOP_CANDIDATES = (
    os.environ.get("OD10_DEST_FLOWS_TOP_TABLE", "").strip(),
    apply_run_tag("zone_dest_incoming_flows_top", OD10_RUN_TAG),
)
OD10_DEST_FLOWS_TOT_CANDIDATES = (
    os.environ.get("OD10_DEST_FLOWS_TOT_TABLE", "").strip(),
    apply_run_tag("zone_dest_incoming_flows_totals", OD10_RUN_TAG),
)
OD10_BUILDING_EMISSIONS_CANDIDATES = (
    os.environ.get("OD10_BUILDING_EMISSIONS_TABLE", "").strip(),
    apply_run_tag("building_emissions", OD10_RUN_TAG),
)


def _resolve_od10_table(cur, candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        n = (name or "").strip()
        if n and _table_exists(cur, n):
            return n
    return None


def _od10_api_errors(fn):
    """Return JSON (not HTML) when an OD endpoint raises."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": "server_error", "message": str(exc)}), 500

    return wrapper


app = Flask(
    __name__,
    static_folder=str(_STATIC_DIR),
    static_url_path="/static",
)
if CORS is not None:
    CORS(app, resources={r"/*": {"origins": "*"}}, methods=["GET", "OPTIONS"])

DEPLOY = {
    "url_prefix": "",
    "api_prefix": "/api",
    "show_boundary_button": True,
    "offline": False,
}
_DEPLOY_CONFIGURED = False


def _str_to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _norm_deploy_path(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s or s == "/":
        return ""
    if not s.startswith("/"):
        s = "/" + s
    return s.rstrip("/")


def _is_api_request(path: str) -> bool:
    ap = DEPLOY["api_prefix"] or "/api"
    up = DEPLOY["url_prefix"] or ""
    prefixes = [ap + "/", ap, "/api/", "/api"]
    if up:
        prefixes.extend([up + ap + "/", up + ap])
    for prefix in prefixes:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    return False


def _register_route_aliases(old_prefix: str, new_prefix: str) -> None:
    old_prefix = _norm_deploy_path(old_prefix) or "/api"
    new_prefix = _norm_deploy_path(new_prefix)
    if not new_prefix or new_prefix == old_prefix:
        return
    url_prefix = DEPLOY["url_prefix"]
    existing = {rule.rule for rule in app.url_map.iter_rules()}
    for rule in list(app.url_map.iter_rules()):
        if not rule.rule.startswith(old_prefix):
            continue
        suffix = rule.rule[len(old_prefix):]
        paths = [new_prefix + suffix]
        if url_prefix:
            paths.append(url_prefix + new_prefix + suffix)
        for path in paths:
            if path in existing or path == rule.rule:
                continue
            ep = f"{rule.endpoint}__api_{abs(hash(path))}"
            app.add_url_rule(path, ep, app.view_functions[rule.endpoint], methods=rule.methods)
            existing.add(path)


def _duplicate_url_prefixed_routes() -> None:
    prefix = DEPLOY["url_prefix"]
    if not prefix:
        return
    existing = {rule.rule for rule in app.url_map.iter_rules()}
    for rule in list(app.url_map.iter_rules()):
        if rule.endpoint == "static":
            continue
        if rule.rule.startswith(prefix):
            continue
        alias = prefix + rule.rule
        if alias in existing:
            continue
        ep = f"{rule.endpoint}__pfx_{abs(hash(alias))}"
        app.add_url_rule(alias, ep, app.view_functions[rule.endpoint], methods=rule.methods)
        existing.add(alias)


def configure_deployment(
    *,
    url_prefix: str | None = None,
    api_prefix: str | None = None,
    show_boundary_button: bool | None = None,
    offline: bool | None = None,
) -> None:
    """Apply URL/API mount prefixes and optional UI flags (safe to call once at startup)."""
    global DEPLOY, _DEPLOY_CONFIGURED
    DEPLOY = {
        "url_prefix": _norm_deploy_path(url_prefix if url_prefix is not None else DEPLOY["url_prefix"]),
        "api_prefix": _norm_deploy_path(api_prefix if api_prefix is not None else DEPLOY["api_prefix"]) or "/api",
        "show_boundary_button": (
            DEPLOY["show_boundary_button"] if show_boundary_button is None else bool(show_boundary_button)
        ),
        "offline": DEPLOY["offline"] if offline is None else bool(offline),
    }
    if _DEPLOY_CONFIGURED:
        return
    _register_route_aliases("/api", DEPLOY["api_prefix"])
    _duplicate_url_prefixed_routes()
    _DEPLOY_CONFIGURED = True


def _od10_unified_flows_table(cur) -> str | None:
    return _resolve_od10_table(cur, OD10_FLOWS_CANDIDATES)


def _od10_flow_metric_cols(zone_by: str) -> tuple[str, str, str]:
    """Column names on ``zone_incoming_flows_*`` for the active attribution mode."""
    if (zone_by or "rules").strip().lower() == "dest":
        return "trips_dest", "emissions_g_dest", "distance_km_dest"
    return "trips_rules", "emissions_g_rules", "distance_km_rules"


def _od10_flows_nontrivial_sql(trips_c: str, emis_c: str, alias: str = "f") -> str:
    """Skip precomputed OD rows with zero trips and zero emissions (rank padding)."""
    p = f"{alias}."
    return f"AND (COALESCE({p}{trips_c}, 0) > 0 OR COALESCE({p}{emis_c}, 0) > 0)"


def _od10_interzonal_flow_filter_sql(alias: str = "f") -> str:
    """Exclude same-zone (intra) OD pairs from inter-zonal flow lists."""
    p = f"{alias}."
    return f"AND {p}orig_geo_id::text IS DISTINCT FROM {p}dest_geo_id::text"


def _od10_zone_dest_totals(cur, zone_by: str, dest_id: str) -> dict | None:
    """Zone-wide KPI totals for a destination (includes intra-zone trips)."""
    zone_t = _od10_zone_table(cur, zone_by)
    if not zone_t:
        return None
    gid = str(dest_id or "").strip()
    if not gid:
        return None
    cols = _od10_zone_col_names(cur, zone_t, zone_by)
    trips_c, emis_c = _od10_zone_kpi_cols(cur, zone_t, zone_by=zone_by)
    if _od10_metrics_weighted() and _column_exists(cur, zone_t, cols["distance_km_weighted"]):
        dist_c = cols["distance_km_weighted"]
    else:
        dist_c = cols["distance_km"]
    cur.execute(
        f"""
        SELECT {trips_c}, {emis_c}, COALESCE({dist_c}, 0)::double precision
        FROM {SCHEMA}.{zone_t}
        WHERE geo_id::text = %s
        """,
        (gid,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "trips": float(row[0] or 0),
        "total_emissions_g": float(row[1] or 0),
        "total_distance_km": float(row[2] or 0),
    }


def _od10_intra_zone_flow(
    dest_id: str,
    zone_totals: dict | None,
    incoming_trips: float,
    incoming_emis: float,
    incoming_km: float,
) -> dict | None:
    """Intra-zone share = zone total minus inter-zonal incoming flows."""
    if not zone_totals:
        return None
    trips = max(0.0, float(zone_totals.get("trips") or 0) - float(incoming_trips or 0))
    emis = max(0.0, float(zone_totals.get("total_emissions_g") or 0) - float(incoming_emis or 0))
    km = max(0.0, float(zone_totals.get("total_distance_km") or 0) - float(incoming_km or 0))
    if trips <= 0 and emis <= 0:
        return None
    gid = str(dest_id)
    return {
        "orig_geo_id": gid,
        "dest_geo_id": gid,
        "is_intra_zone": True,
        "trips": trips,
        "total_emissions_g": emis,
        "total_distance_km": round(km, 2),
    }


def _od10_enrich_incoming_payload(cur, payload: dict, zone_by: str) -> None:
    """Attach zone totals and intra-zone flow derived from zone KPI minus incoming."""
    dest_id = str(payload.get("dest_geo_id") or "").strip()
    if not dest_id:
        return
    zone_totals = _od10_zone_dest_totals(cur, zone_by, dest_id)
    if zone_totals:
        payload["dest_zone_trips"] = zone_totals["trips"]
        payload["dest_zone_emissions_g"] = zone_totals["total_emissions_g"]
        payload["dest_zone_distance_km"] = zone_totals["total_distance_km"]
        if payload.get("dest_rules_trips") is None:
            payload["dest_rules_trips"] = zone_totals["trips"]
        if payload.get("dest_rules_emissions_g") is None:
            payload["dest_rules_emissions_g"] = zone_totals["total_emissions_g"]
    inc_trips = float(payload.get("total_incoming_trips") or 0)
    inc_emis = float(payload.get("total_incoming_emissions_g") or 0)
    inc_km = float(payload.get("total_incoming_distance_km") or 0)
    intra = _od10_intra_zone_flow(dest_id, zone_totals, inc_trips, inc_emis, inc_km)
    if intra:
        dest_lat = payload.get("dest_lat")
        dest_lon = payload.get("dest_lon")
        intra["orig_lat"] = dest_lat
        intra["orig_lon"] = dest_lon
        intra["dest_lat"] = dest_lat
        intra["dest_lon"] = dest_lon
        payload["intra_zone"] = intra
    else:
        payload["intra_zone"] = None


def _od10_building_emissions_table(cur) -> str | None:
    return _resolve_od10_table(cur, OD10_BUILDING_EMISSIONS_CANDIDATES)


def _od10_building_db_stats(cur) -> dict | None:
    """Quick fingerprint so deploys can verify new vs old building_emissions dump."""
    btab = _od10_building_emissions_table(cur)
    if not btab:
        return None
    has_cap = _column_exists(cur, btab, "trips_is_capacity_share")
    has_ref = _column_exists(cur, btab, "refreshed_at")
    cap_sql = "BOOL_OR(trips_is_capacity_share)" if has_cap else "NULL"
    ref_sql = "MAX(refreshed_at)" if has_ref else "NULL"
    cur.execute(
        f"""
        SELECT COUNT(*)::bigint,
               COALESCE(SUM(trips_weighted_rules), 0)::double precision,
               COALESCE(SUM(trips_weighted_dest), 0)::double precision,
               {cap_sql},
               {ref_sql}
        FROM {SCHEMA}.{btab}
        """
    )
    row = cur.fetchone()
    if not row:
        return None
    n, rules_w, dest_w, cap_any, refreshed_at = row
    return {
        "table": btab,
        "rows": int(n or 0),
        "trips_weighted_rules_total": float(rules_w or 0),
        "trips_weighted_dest_total": float(dest_w or 0),
        "capacity_alloc": bool(cap_any) if cap_any is not None else None,
        "refreshed_at": refreshed_at.isoformat() if refreshed_at is not None else None,
        "expected_rows_approx": 924757,
    }


def _od10_is_unified_building_table(cur, btab: str) -> bool:
    return _column_exists(cur, btab, "emissions_g_rules")


def _od10_building_col_names(cur, btab: str, building_by: str) -> dict[str, str]:
    """Physical columns on ``building_emissions_*`` (unified or legacy)."""
    if _od10_is_unified_building_table(cur, btab):
        suffix = "dest" if (building_by or "rules").strip().lower() == "dest" else "rules"
        return {
            "trips": f"trips_{suffix}",
            "trips_weighted": f"trips_weighted_{suffix}",
            "emissions_g": f"emissions_g_{suffix}",
            "distance_km": f"distance_km_{suffix}",
        }
    return {
        "trips": "trips",
        "trips_weighted": "trips_weighted",
        "emissions_g": "emissions_g",
        "distance_km": "distance_km",
    }


def _od10_building_metric_cols(
    cur, btab: str, building_by: str
) -> tuple[str, str, str, str]:
    cols = _od10_building_col_names(cur, btab, building_by)
    return cols["trips"], cols["trips_weighted"], cols["emissions_g"], cols["distance_km"]


def _od10_is_unified_zone_table(cur, ztab: str) -> bool:
    return _column_exists(cur, ztab, "emissions_g_rules")


def _od10_zone_unified_table(cur) -> str | None:
    return _resolve_od10_table(cur, OD10_ZONE_EMISSIONS_CANDIDATES)


def _od10_zone_col_names(cur, ztab: str, zone_by: str) -> dict[str, str]:
    """Map logical metric names to physical columns (unified or legacy split table)."""
    if _od10_is_unified_zone_table(cur, ztab):
        suffix = "dest" if (zone_by or "rules").strip().lower() == "dest" else "rules"
        return {
            "trips": f"trips_{suffix}",
            "trips_weighted": f"trips_weighted_{suffix}",
            "emissions_g": f"emissions_g_{suffix}",
            "emissions_g_weighted": f"emissions_g_weighted_{suffix}",
            "distance_km": f"distance_km_{suffix}",
            "distance_km_weighted": f"distance_km_weighted_{suffix}",
        }
    return {
        "trips": "trips",
        "trips_weighted": "trips_weighted",
        "emissions_g": "emissions_g",
        "emissions_g_weighted": "emissions_g_weighted",
        "distance_km": "distance_km",
        "distance_km_weighted": "distance_km_weighted",
    }


def _od10_zone_kpi_cols(cur, zone_tab: str, *, zone_by: str = "rules") -> tuple[str, str]:
    cols = _od10_zone_col_names(cur, zone_tab, zone_by)
    if _od10_metrics_weighted() and _column_exists(cur, zone_tab, cols["trips_weighted"]):
        return cols["trips_weighted"], cols["emissions_g_weighted"]
    return cols["trips"], cols["emissions_g"]


def _od10_dest_anchor_coords(cur, dest_id: str) -> tuple[float | None, float | None]:
    anchor_t = _resolve_od10_table(cur, OD10_ANCHOR_CANDIDATES)
    if not anchor_t:
        return None, None
    has_map = _column_exists(cur, anchor_t, "map_lat") and _column_exists(cur, anchor_t, "map_lon")
    if has_map:
        cur.execute(
            f"""
            SELECT COALESCE(map_lat, dest_lat), COALESCE(map_lon, dest_lon)
            FROM {SCHEMA}.{anchor_t} WHERE geo_id = %s
            """,
            (dest_id,),
        )
    else:
        cur.execute(
            f"SELECT dest_lat, dest_lon FROM {SCHEMA}.{anchor_t} WHERE geo_id = %s",
            (dest_id,),
        )
    row = cur.fetchone()
    if not row:
        return None, None
    return _flow_coord(row[0]), _flow_coord(row[1])


def _od10_zone_kpis(cur, zone_by: str, dest_id: str) -> tuple[float | None, float | None]:
    zone_t = _od10_zone_table(cur, zone_by)
    if not zone_t:
        return None, None
    trips_c, emis_c = _od10_zone_kpi_cols(cur, zone_t, zone_by=zone_by)
    cur.execute(
        f"SELECT {trips_c}, {emis_c} FROM {SCHEMA}.{zone_t} WHERE geo_id::text = %s",
        (dest_id,),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    return (
        float(row[0]) if row[0] is not None else None,
        float(row[1]) if row[1] is not None else None,
    )


def _od10_flows_tables(cur, zone_by: str = "rules") -> tuple[str | None, str | None]:
    use_dest = (zone_by or "rules").strip().lower() == "dest"
    top_cands = OD10_DEST_FLOWS_TOP_CANDIDATES if use_dest else OD10_FLOWS_TOP_CANDIDATES
    tot_cands = OD10_DEST_FLOWS_TOT_CANDIDATES if use_dest else OD10_FLOWS_TOT_CANDIDATES
    top_t = _resolve_od10_table(cur, top_cands)
    tot_t = _resolve_od10_table(cur, tot_cands)
    if top_t and tot_t:
        return top_t, tot_t
    return None, None


def _od10_zone_table(cur, zone_by: str) -> str | None:
    unified = _od10_zone_unified_table(cur)
    if unified:
        return unified
    use_dest = (zone_by or "").strip().lower() == "dest"
    candidates = OD10_DEST_CANDIDATES if use_dest else OD10_RULES_CANDIDATES
    return _resolve_od10_table(cur, candidates)


def _od10_zone_bootstrap_table(cur) -> str | None:
    return _od10_zone_unified_table(cur) or _resolve_od10_table(cur, OD10_RULES_CANDIDATES)


def _arg_float(name: str, default):
    v = request.args.get(name, None)
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@app.route("/api/health")
def api_health():
    up = DEPLOY["url_prefix"]
    ap = DEPLOY["api_prefix"]
    building_stats = None
    zone_label_stats = None
    db_ok = False
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()
            building_stats = _od10_building_db_stats(cur)
            zone_label_stats = {
                "zones_table": "zones_geom",
                "zones_geom_rows": _dashboard_server._zones_geom_row_count(cur),
                "zone_codes_loaded": len(_zone_code_index()),
                "zone_names_loaded": len(_zone_name_index()),
                "sample_label_562": _zone_label_for("562"),
            }
            db_ok = True
            cur.close()
        finally:
            conn.close()
    except Exception as exc:
        building_stats = {"error": str(exc)}
    return jsonify({
        "ok": db_ok,
        "service": "popgen-od-dashboard",
        "metrics_mode": "weighted" if _od10_metrics_weighted() else "legs",
        "schema": SCHEMA,
        "dbname": DB_PARAMS.get("dbname"),
        "db_host": DB_PARAMS.get("host"),
        "db_port": DB_PARAMS.get("port"),
        "api_build": "2026-06-23-capacity-buildings",
        "building_emissions": building_stats,
        "zone_labels": zone_label_stats,
        "deploy": {
            "url_prefix": up,
            "api_prefix": ap,
            "api_base": f"{up}{ap}" if up or ap else "/api",
            "show_boundary_button": DEPLOY["show_boundary_button"],
            "offline": DEPLOY["offline"],
        },
    })


@app.errorhandler(500)
def api_internal_error(err):
    """Never return HTML 500 for API routes."""
    if _is_api_request(request.path):
        msg = getattr(err, "description", None) or str(err) or "internal error"
        return jsonify({"error": "server_error", "message": msg}), 500
    return (
        "<!doctype html><title>500 Internal Server Error</title>"
        "<h1>Internal Server Error</h1>",
        500,
    )


@app.route("/api/montreal_boundary.geojson")
def api_montreal_boundary():
    buffer_m = _arg_float("buffer_m", None)
    conn = get_conn()
    try:
        cur = conn.cursor()
        fc = _montreal_boundary_geojson_fc(cur, buffer_m=buffer_m)
    finally:
        conn.close()
    if fc:
        return jsonify(fc)
    return jsonify({"type": "FeatureCollection", "features": []})


@app.route("/api/od/zone_codes")
@app.route("/api/zone_codes")
def api_od10_zone_codes():
    conn = get_conn()
    try:
        cur = conn.cursor()
        zone_codes, zone_names = _zone_code_maps_for_geom(cur)
    finally:
        conn.close()
    return jsonify({"zone_codes": zone_codes, "zone_names": zone_names})


@app.route("/api/od/zones_boundary")
@app.route("/api/zones_boundary")
def api_od10_zones_boundary():
    """CMM (or island) zone polygon outlines — boundary lines only, for map background."""
    island_only = _request_island_only(default=False)
    conn = get_conn()
    try:
        cur = conn.cursor()
        geojson, zone_count, bounds = _build_zones_boundary_geojson(cur, island_only=island_only)
        if bounds is None:
            bounds = MONTREAL_BOUNDS if island_only else CMM_BOUNDS
        return jsonify({
            "geojson": geojson,
            "zone_count": zone_count,
            "bounds": bounds,
            "island_only": island_only,
            "source": "od",
        })
    finally:
        conn.close()


OD10_DETAIL_CANDIDATES = (
    os.environ.get("OD10_DETAIL_TABLE", "").strip(),
    "trips_route_emissions",
    "od10_actual_od_ct_routes_emissions_detail",
)
OD10_ROUTES_CANDIDATES = (
    os.environ.get("OD10_ROUTES_TABLE", "").strip(),
    DEFAULT_ROUTES,
    "od10_actual_od_ct_routes",
)


def _resolve_od10_routes_table(cur) -> str | None:
    return _resolve_od10_table(cur, OD10_ROUTES_CANDIDATES)


def _od10_detail_emissions_col(cur, detail: str) -> str:
    """Per-leg emissions column on ``trips_route_emissions`` (or legacy detail tables)."""
    if _column_exists(cur, detail, "route_emissions_g"):
        return "route_emissions_g"
    if _column_exists(cur, detail, "emissions_g"):
        return "emissions_g"
    return "route_emissions_g"


def _od10_detail_island_sql(cur, detail: str, *, prefix: str = "") -> str:
    """Predicate: car leg touches Montreal island (orig or dest)."""
    p = f"{prefix}." if prefix else ""
    if _column_exists(cur, detail, "orig_on_island") and _column_exists(cur, detail, "dest_on_island"):
        return f"({p}orig_on_island OR {p}dest_on_island)"
    if all(
        _column_exists(cur, detail, c)
        for c in ("orig_lat", "orig_lon", "dest_lat", "dest_lon")
    ):
        return f"""(
            ({p}orig_lat IS NOT NULL AND {p}orig_lon IS NOT NULL
             AND {p}orig_lat::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
             AND {p}orig_lon::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX})
            OR ({p}dest_lat IS NOT NULL AND {p}dest_lon IS NOT NULL
             AND {p}dest_lat::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
             AND {p}dest_lon::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX})
        )"""
    return "TRUE"


def _od10_detail_ready(cur, detail: str) -> bool:
    emis = _od10_detail_emissions_col(cur, detail)
    return _column_exists(cur, detail, emis) and _column_exists(cur, detail, "distance_m")


def _od10_survey_funnel_stats(cur) -> dict | None:
    """Island-touch PM23 car legs vs survey-expanded totals (from flat detail table)."""
    detail = _resolve_od10_table(cur, OD10_DETAIL_CANDIDATES)
    if not detail:
        return None
    required = ("d_fexp", "expanded_emissions_g", "distance_m", "emission_zone_geo_id")
    if not all(_column_exists(cur, detail, c) for c in required):
        return None
    island_sql = _od10_detail_island_sql(cur, detail)
    attr_pred = (
        "emission_zone_geo_id IS NOT NULL AND btrim(emission_zone_geo_id::text) <> ''"
    )
    cur.execute(
        f"""
        SELECT
            COUNT(*)::bigint,
            COALESCE(SUM(d_fexp), 0)::double precision,
            COALESCE(SUM(expanded_emissions_g), 0)::double precision,
            COALESCE(SUM(distance_m * d_fexp), 0)::double precision / 1000.0,
            COUNT(*) FILTER (WHERE {attr_pred})::bigint,
            COALESCE(SUM(d_fexp) FILTER (WHERE {attr_pred}), 0)::double precision,
            COALESCE(SUM(expanded_emissions_g) FILTER (WHERE {attr_pred}), 0)::double precision,
            COALESCE(SUM(distance_m * d_fexp) FILTER (WHERE {attr_pred}), 0)::double precision
                / 1000.0,
            COALESCE(AVG(d_fexp), 0)::double precision
        FROM {SCHEMA}.{detail}
        WHERE {island_sql}
        """
    )
    row = cur.fetchone()
    (
        n_island,
        w_island,
        em_island,
        km_island,
        n_attr,
        w_attr,
        em_attr,
        km_attr,
        avg_w,
    ) = row
    return {
        "table": detail,
        "island_touch_legs": int(n_island or 0),
        "island_touch_expanded": float(w_island or 0),
        "island_touch_emissions_g": float(em_island or 0),
        "island_touch_distance_km": float(km_island or 0),
        "attributed_legs": int(n_attr or 0),
        "attributed_expanded": float(w_attr or 0),
        "attributed_emissions_g": float(em_attr or 0),
        "attributed_distance_km": float(km_attr or 0),
        "avg_d_fexp": float(avg_w or 0),
        "weight_field": "d_fexp",
        "note": (
            "Eligible = island-touch car legs (orig or dest on island). "
            "Rules map = legs with a rules-attributed emission zone."
        ),
    }


def _od10_apply_island_eligible_primary(stats: dict, funnel: dict | None) -> dict:
    """Use island-touch eligible totals as sidebar KPIs; keep map sums as map_rules_*."""
    if not funnel or not _od10_metrics_weighted():
        return stats
    legs = int(funnel.get("island_touch_legs") or 0)
    expanded = float(funnel.get("island_touch_expanded") or 0)
    if legs <= 0 or expanded <= 0:
        return stats
    out = dict(stats)
    # Preserve map aggregation (rules or dest choropleth) for subtitles / tooltips.
    out["map_rules_trips_legs"] = out.get("trips_legs", out.get("trips"))
    out["map_rules_trips_weighted"] = out.get("trips_weighted", out.get("trips"))
    out["map_rules_emissions_g"] = out.get("total_emissions_g_weighted", out.get("total_emissions_g"))
    out["map_rules_distance_km"] = out.get("total_distance_km_weighted", out.get("total_distance_km"))
    out["trips_legs"] = float(legs)
    out["trips_weighted"] = expanded
    em_w = float(funnel.get("island_touch_emissions_g") or 0)
    km_w = float(funnel.get("island_touch_distance_km") or 0)
    out["total_emissions_g_legs"] = out.get("total_emissions_g_legs", em_w)
    out["total_emissions_g_weighted"] = em_w
    out["total_distance_km_legs"] = out.get("total_distance_km_legs", km_w)
    out["total_distance_km_weighted"] = km_w
    out["kpi_scope"] = "island_eligible"
    out["metrics_mode"] = "weighted"
    return _od10_promote_weighted_row(out)


def _od10_has_weighted_columns(cur, ztab: str, *, zone_by: str = "rules") -> bool:
    cols = _od10_zone_col_names(cur, ztab, zone_by)
    return _column_exists(cur, ztab, cols["trips_weighted"])


def _od10_stats_from_table(cur, ztab: str, *, zone_by: str | None = None) -> dict:
    """Aggregate legs + weighted columns from a zone or category table."""
    mode = (zone_by or "rules").strip().lower()
    cols = _od10_zone_col_names(cur, ztab, mode)
    if _od10_has_weighted_columns(cur, ztab, zone_by=mode):
        cur.execute(
            f"""
            SELECT COALESCE(SUM({cols['trips']}), 0)::bigint,
                   COALESCE(SUM({cols['trips_weighted']}), 0)::double precision,
                   COALESCE(SUM({cols['emissions_g']}), 0)::double precision,
                   COALESCE(SUM({cols['emissions_g_weighted']}), 0)::double precision,
                   COALESCE(SUM({cols['distance_km']}), 0)::double precision,
                   COALESCE(SUM({cols['distance_km_weighted']}), 0)::double precision
            FROM {SCHEMA}.{ztab}
            """
        )
        trips, trips_w, emis_g, emis_w, dist_km, dist_w = cur.fetchone()
    else:
        cur.execute(
            f"""
            SELECT COALESCE(SUM({cols['trips']}), 0), COALESCE(SUM({cols['emissions_g']}), 0),
                   COALESCE(SUM({cols['distance_km']}), 0)
            FROM {SCHEMA}.{ztab}
            """
        )
        trips, emis_g, dist_km = cur.fetchone()
        trips_w = float(trips or 0)
        emis_w = float(emis_g or 0)
        dist_w = float(dist_km or 0)
    trips = float(trips or 0)
    trips_w = float(trips_w or 0)
    emis_g = float(emis_g or 0)
    emis_w = float(emis_w or 0)
    dist_km = float(dist_km or 0)
    dist_w = float(dist_w or 0)
    out = {
        "trips": trips,
        "trips_legs": trips,
        "trips_weighted": trips_w,
        "total_emissions_g": emis_g,
        "total_emissions_g_legs": emis_g,
        "total_emissions_g_weighted": emis_w,
        "total_emissions_tonnes": emis_g / 1e6,
        "total_emissions_tonnes_weighted": emis_w / 1e6,
        "total_distance_km": dist_km,
        "total_distance_km_legs": dist_km,
        "total_distance_km_weighted": dist_w,
        "avg_emissions_g_per_trip": (emis_g / trips) if trips else 0.0,
        "avg_emissions_g_per_trip_weighted": (emis_w / trips_w) if trips_w else 0.0,
        "counting_modes": (
            ["legs", "weighted"] if _od10_has_weighted_columns(cur, ztab, zone_by=mode) else ["legs"]
        ),
    }
    if zone_by:
        out["zone_by"] = zone_by
        out["table"] = ztab
    out["metrics_mode"] = "weighted" if _od10_metrics_weighted() else "legs"
    return _od10_promote_weighted_row(out)


def _od10_zone_map_rows(
    cur,
    ztab: str,
    *,
    zone_by: str = "rules",
    min_g: float,
    max_g: float | None,
    island_on: bool,
    include_geojson: bool,
) -> dict:
    mode = (zone_by or "rules").strip().lower()
    if mode not in ("rules", "dest"):
        mode = "rules"
    cols = _od10_zone_col_names(cur, ztab, mode)
    clip_gj = _montreal_island_geometry_geojson_for_postgis() if island_on else None
    params: list = []
    island_cte = ""
    island_pred = ""
    if island_on and clip_gj:
        island_cte = (
            "WITH island AS (SELECT ST_MakeValid(ST_Force2D("
            "ST_SetSRID(ST_GeomFromGeoJSON(%s)::geometry, 4326))) AS ig)\n"
        )
        params.append(clip_gj)
        island_pred = (
            "\n              AND EXISTS (SELECT 1 FROM island ix WHERE ST_Intersects("
            "ix.ig, ST_MakeValid(ST_Force2D(g.geom::geometry))))"
        )
    pos_lat = zone_point_on_surface_lat_sql("g")
    pos_lon = zone_point_on_surface_lon_sql("g")
    anchor_tab = _resolve_od10_table(cur, OD10_ANCHOR_CANDIDATES)
    if anchor_tab:
        map_lat = f"COALESCE(za.map_lat, {pos_lat})"
        map_lon = f"COALESCE(za.map_lon, {pos_lon})"
        join_anchor = f"LEFT JOIN {SCHEMA}.{anchor_tab} za ON za.geo_id = g.geo_id::text"
    else:
        map_lat = pos_lat
        map_lon = pos_lon
        join_anchor = ""
    has_w = _od10_has_weighted_columns(cur, ztab, zone_by=mode)
    use_w = _od10_metrics_weighted() and has_w
    extra_cols = ""
    if has_w:
        extra_cols = f"""
               COALESCE(z.{cols['trips_weighted']}, 0)::double precision AS trips_weighted,
               COALESCE(z.{cols['emissions_g_weighted']}, 0)::double precision AS emissions_g_weighted,
               COALESCE(z.{cols['distance_km']}, 0)::double precision AS distance_km,
               COALESCE(z.{cols['distance_km_weighted']}, 0)::double precision AS distance_km_weighted,"""
    emis_col_raw = f"z.{cols['emissions_g_weighted']}" if use_w else f"z.{cols['emissions_g']}"
    emis_col = f"COALESCE({emis_col_raw}, 0)"
    zgeom = _zones_geom_table(cur)
    if not zgeom:
        return {"zones": [], "geojson": None}
    sql = island_cte + f"""
        SELECT g.geo_id::text AS geo_id,
               COALESCE(z.{cols['trips']}, 0)::double precision AS trips,
               COALESCE(z.{cols['emissions_g']}, 0)::double precision AS emissions_g,{extra_cols}
               {map_lat}::double precision AS lat,
               {map_lon}::double precision AS lon
        FROM {SCHEMA}.{zgeom} g
        LEFT JOIN {SCHEMA}.{ztab} z ON z.geo_id::text = g.geo_id::text
        {join_anchor}
        WHERE {emis_col} >= %s"""
    params.append(min_g)
    if max_g is not None:
        sql += f"\n          AND {emis_col} <= %s"
        params.append(max_g)
    sql += island_pred
    cur.execute(sql, tuple(params))
    out: list[dict] = []
    for row in cur.fetchall():
        if has_w:
            geo_id, trips, emissions_g, trips_w, emis_w, dist_km, dist_w, lat, lon = row
        else:
            geo_id, trips, emissions_g, lat, lon = row
            trips_w = emis_w = dist_km = dist_w = None
        if lat is None or lon is None:
            continue
        zone = {
            "geo_id": geo_id,
            "trips": float(trips or 0),
            "trips_legs": float(trips or 0),
            "total_emissions_g": float(emissions_g or 0),
            "total_emissions_g_legs": float(emissions_g or 0),
            "lat": float(lat),
            "lon": float(lon),
        }
        if has_w:
            zone["trips_weighted"] = float(trips_w or 0)
            zone["total_emissions_g_weighted"] = float(emis_w or 0)
            zone["distance_km"] = float(dist_km or 0)
            zone["distance_km_weighted"] = float(dist_w or 0)
        out.append(_attach_zone_code(_od10_promote_weighted_row(zone)))
    geojson_fc = None
    if include_geojson:
        geojson_fc = _build_zone_map_geojson(
            cur, out, island_clip_geojson=(clip_gj if island_on else None)
        )
    return {"zones": out, "geojson": geojson_fc}


@app.route("/api/od/zone_map")
@_od10_api_errors
def api_od10_zone_map():
    zone_by = (request.args.get("zone_by", "rules") or "rules").strip().lower()
    if zone_by == "meeting":
        zone_by = "rules"
    include_geojson = str(request.args.get("include_geojson", "1")).strip().lower() not in (
        "0", "false", "no", "off",
    )
    island_on = _request_island_only(default=True)
    min_g = float(_arg_float("min_kg", 0.0) or 0.0) * 1000.0
    max_kg = _arg_float("max_kg", None)
    max_g = float(max_kg) * 1000.0 if max_kg is not None else None

    conn = get_conn()
    try:
        cur = conn.cursor()
        ztab = _od10_zone_table(cur, zone_by)
        if not ztab:
            return jsonify({
                "error": "missing_table",
                "message": (
                    f"No OD10 zone table found in schema '{SCHEMA}' "
                    f"(expected zone_emissions_od10). "
                    "Restore the bundle dump into od_dashboard (schema public), "
                    "or pass --db-schema if tables live elsewhere."
                ),
            }), 503
        payload = _od10_zone_map_rows(
            cur,
            ztab,
            zone_by=zone_by,
            min_g=min_g,
            max_g=max_g,
            island_on=island_on,
            include_geojson=include_geojson,
        )
        payload["zone_by"] = zone_by
        payload["source"] = "od"
        payload["table"] = ztab
        payload["metrics_mode"] = "weighted" if _od10_metrics_weighted() else "legs"
        payload["metrics_note"] = _od10_metrics_note()
        if str(request.args.get("with_building_scale", "")).strip().lower() in (
            "1", "true", "yes", "on",
        ):
            detail_tab = _od10_detail_table(cur)
            building_tab = _od10_building_emissions_table(cur)
            if detail_tab or building_tab:
                building_by = "dest" if zone_by == "dest" else "rules"
                bounds = _od10_building_emission_scale_bounds(
                    cur, detail_tab, building_by=building_by
                )
                if bounds:
                    payload["building_emission_scale"] = bounds
        return jsonify(payload)
    finally:
        conn.close()


@app.route("/api/od/zone_maps")
@_od10_api_errors
def api_od10_zone_maps():
    """Both choropleths in one response (destination + rules)."""
    include_geojson = str(request.args.get("include_geojson", "1")).strip().lower() not in (
        "0", "false", "no", "off",
    )
    island_on = _request_island_only(default=True)
    min_g = float(_arg_float("min_kg", 0.0) or 0.0) * 1000.0
    max_kg = _arg_float("max_kg", None)
    max_g = float(max_kg) * 1000.0 if max_kg is not None else None

    conn = get_conn()
    try:
        cur = conn.cursor()
        zone_tab = _od10_zone_unified_table(cur)
        if zone_tab:
            rules_tab = dest_tab = zone_tab
        else:
            dest_tab = _od10_zone_table(cur, "dest")
            rules_tab = _od10_zone_table(cur, "rules")
        if not dest_tab or not rules_tab:
            return jsonify({
                "error": "missing_table",
                "message": (
                    f"No OD10 zone tables in schema '{SCHEMA}' "
                    f"(expected zone_emissions_od10). "
                    "Restore the bundle dump into od_dashboard (schema public), "
                    "or pass --db-schema if tables live elsewhere."
                ),
            }), 503
        dest_payload = _od10_zone_map_rows(
            cur,
            dest_tab,
            zone_by="dest",
            min_g=min_g,
            max_g=max_g,
            island_on=island_on,
            include_geojson=include_geojson,
        )
        rules_payload = _od10_zone_map_rows(
            cur,
            rules_tab,
            zone_by="rules",
            min_g=min_g,
            max_g=max_g,
            island_on=island_on,
            include_geojson=include_geojson,
        )
        mode = "weighted" if _od10_metrics_weighted() else "legs"
        note = _od10_metrics_note()
        return jsonify({
            "source": "od",
            "metrics_mode": mode,
            "metrics_note": note,
            "dest": {**dest_payload, "zone_by": "dest", "table": dest_tab, "metrics_mode": mode},
            "rules": {**rules_payload, "zone_by": "rules", "table": rules_tab, "metrics_mode": mode},
        })
    finally:
        conn.close()


@app.route("/api/od/home")
@app.route("/api/od/bootstrap")
@_od10_api_errors
def api_od10_bootstrap():
    conn = get_conn()
    try:
        cur = conn.cursor()
        zone_tab = _od10_zone_bootstrap_table(cur)
        if not zone_tab:
            return jsonify({
                "error": "missing_table",
                "message": (
                    f"No OD10 zone table found in schema '{SCHEMA}' "
                    f"(expected zone_emissions_od10). "
                    "Restore the bundle dump into od_dashboard (schema public), "
                    "or pass --db-schema if tables live elsewhere."
                ),
            }), 503
        stats = _od10_stats_from_table(cur, zone_tab, zone_by="rules")
        stats["table"] = zone_tab
        by_category: list[dict] = []
        by_category = _od10_fetch_global_by_category(cur)
        dest_stats = None
        if _od10_zone_unified_table(cur):
            dest_stats = _od10_stats_from_table(cur, zone_tab, zone_by="dest")
        else:
            dest_tab = _resolve_od10_table(cur, OD10_DEST_CANDIDATES)
            if dest_tab:
                dest_stats = _od10_stats_from_table(cur, dest_tab, zone_by="dest")
        survey_funnel = _od10_survey_funnel_stats(cur)
        stats_island = None
        if survey_funnel:
            stats_island = _od10_apply_island_eligible_primary(
                {"metrics_mode": "weighted" if _od10_metrics_weighted() else "legs"},
                survey_funnel,
            )
            stats_island["survey_funnel"] = survey_funnel
            stats["survey_funnel"] = survey_funnel
            if dest_stats is not None:
                dest_stats["survey_funnel"] = survey_funnel
        primary_stats = stats_island if stats_island else stats
        building_scale_rules = None
        building_scale_dest = None
        detail_tab = _od10_detail_table(cur)
        building_tab = _od10_building_emissions_table(cur)
        if detail_tab or building_tab:
            building_scale_rules = _od10_building_emission_scale_bounds(
                cur, detail_tab, building_by="rules"
            )
            building_scale_dest = _od10_building_emission_scale_bounds(
                cur, detail_tab, building_by="dest"
            )
        zone_codes, zone_names = _zone_code_maps_for_geom(cur)
        return jsonify({
            "stats": primary_stats,
            "stats_rules": stats,
            "stats_dest": dest_stats,
            "stats_island_eligible": stats_island,
            "by_category": by_category,
            "survey_funnel": survey_funnel,
            "building_emission_scale_rules": building_scale_rules,
            "building_emission_scale_dest": building_scale_dest,
            "zone_codes": zone_codes,
            "zone_names": zone_names,
            "source": "od",
            "metrics_mode": "weighted" if _od10_metrics_weighted() else "legs",
            "metrics_note": _od10_metrics_note(),
        })
    finally:
        conn.close()


def _od10_single_zone_stats(cur, geo_id: str, *, zone_by: str) -> dict | None:
    """KPI block for one zone row from zone_emissions_od10."""
    ztab = _od10_zone_unified_table(cur)
    if not ztab:
        return None
    gid = str(geo_id or "").strip()
    if not gid:
        return None
    mode = "dest" if (zone_by or "rules").strip().lower() == "dest" else "rules"
    cols = _od10_zone_col_names(cur, ztab, mode)
    if _od10_has_weighted_columns(cur, ztab, zone_by=mode):
        cur.execute(
            f"""
            SELECT COALESCE({cols['trips']}, 0)::double precision,
                   COALESCE({cols['trips_weighted']}, 0)::double precision,
                   COALESCE({cols['emissions_g']}, 0)::double precision,
                   COALESCE({cols['emissions_g_weighted']}, 0)::double precision,
                   COALESCE({cols['distance_km']}, 0)::double precision,
                   COALESCE({cols['distance_km_weighted']}, 0)::double precision
            FROM {SCHEMA}.{ztab}
            WHERE geo_id::text = %s
            """,
            (gid,),
        )
        trips, trips_w, emis_g, emis_w, dist_km, dist_w = cur.fetchone() or (0, 0, 0, 0, 0, 0)
    else:
        cur.execute(
            f"""
            SELECT COALESCE({cols['trips']}, 0)::double precision,
                   COALESCE({cols['emissions_g']}, 0)::double precision,
                   COALESCE({cols['distance_km']}, 0)::double precision
            FROM {SCHEMA}.{ztab}
            WHERE geo_id::text = %s
            """,
            (gid,),
        )
        row = cur.fetchone() or (0, 0, 0)
        trips, emis_g, dist_km = row
        trips_w = float(trips or 0)
        emis_w = float(emis_g or 0)
        dist_w = float(dist_km or 0)
    trips = float(trips or 0)
    trips_w = float(trips_w or 0)
    emis_g = float(emis_g or 0)
    emis_w = float(emis_w or 0)
    dist_km = float(dist_km or 0)
    dist_w = float(dist_w or 0)
    out = {
        "geo_id": gid,
        "zone_by": mode,
        "trips": trips,
        "trips_legs": trips,
        "trips_weighted": trips_w,
        "total_emissions_g": emis_g,
        "total_emissions_g_legs": emis_g,
        "total_emissions_g_weighted": emis_w,
        "total_emissions_tonnes": emis_g / 1e6,
        "total_emissions_tonnes_weighted": emis_w / 1e6,
        "total_distance_km": dist_km,
        "total_distance_km_legs": dist_km,
        "total_distance_km_weighted": dist_w,
        "avg_emissions_g_per_trip": (emis_g / trips) if trips else 0.0,
        "avg_emissions_g_per_trip_weighted": (emis_w / trips_w) if trips_w else 0.0,
        "kpi_scope": "zone",
    }
    out["metrics_mode"] = "weighted" if _od10_metrics_weighted() else "legs"
    return _od10_promote_weighted_row(out)


def _od10_fetch_global_by_category(cur) -> list[dict]:
    """Island-wide travel-reason rows for sidebar charts."""
    cats_tab = _resolve_od10_table(cur, OD10_CATEGORIES_CANDIDATES)
    if not cats_tab:
        return []
    out: list[dict] = []
    if _od10_has_weighted_columns(cur, cats_tab):
        cur.execute(
            f"""
            SELECT category, trips, trips_weighted, emissions_g, emissions_g_weighted,
                   distance_km, distance_km_weighted
            FROM {SCHEMA}.{cats_tab}
            ORDER BY emissions_g DESC
            """
        )
        for cat, t, tw, e, ew, d, dw in cur.fetchall():
            e = float(e or 0)
            row = {
                "category": cat,
                "trips": float(t or 0),
                "trips_legs": float(t or 0),
                "trips_weighted": float(tw or 0),
                "total_emissions_g": e,
                "total_emissions_g_legs": e,
                "total_emissions_g_weighted": float(ew or 0),
                "total_emissions_tonnes": e / 1e6,
                "total_emissions_tonnes_weighted": float(ew or 0) / 1e6,
                "total_distance_km": float(d or 0),
                "total_distance_km_weighted": float(dw or 0),
            }
            out.append(_od10_promote_weighted_row(row))
    else:
        cur.execute(
            f"SELECT category, trips, emissions_g, distance_km "
            f"FROM {SCHEMA}.{cats_tab} ORDER BY emissions_g DESC"
        )
        for cat, t, e, d in cur.fetchall():
            e = float(e or 0)
            out.append({
                "category": cat,
                "trips": float(t or 0),
                "trips_legs": float(t or 0),
                "total_emissions_g": e,
                "total_emissions_tonnes": e / 1e6,
                "total_distance_km": float(d or 0),
            })
    return out


def _od10_zone_detail_column(cur, detail: str, *, zone_by: str) -> str | None:
    mode = (zone_by or "rules").strip().lower()
    if mode == "dest":
        for col in ("dest_geo_id", "route_dest_geo_id", "dest_zone_geo_id"):
            if _column_exists(cur, detail, col):
                return col
        return None
    for col in ("emission_zone_geo_id", "route_attributed_geo_id", "route_attributed_zone_geo_id"):
        if _column_exists(cur, detail, col):
            return col
    return None


def _od10_zone_match_sql(alias: str, zcol: str) -> str:
    return f"split_part(btrim({alias}.{zcol}::text), '.', 1) = %s"


def _od10_detail_travel_reason_bucket_sql(cur, detail: str, alias: str = "d") -> str | None:
    """Best available travel-reason bucket expression on a leg-level detail table."""
    has_purpose = _column_exists(cur, detail, "purpose")
    has_assign = _column_exists(cur, detail, "route_emission_assignment")
    if has_purpose or has_assign:
        return _dashboard_server._routes_travel_reason_bucket_sql(alias, has_purpose=has_purpose)
    if has_purpose:
        from meeting_emissions_attribution import motif_travel_reason_sql

        return motif_travel_reason_sql(purpose_expr=f"{alias}.purpose")
    return None


def _od10_zone_by_category_is_usable(categories: list[dict]) -> bool:
    """False when breakdown is empty or collapsed to a single catch-all bucket."""
    if not categories:
        return False
    if len(categories) == 1:
        cat = str(categories[0].get("category") or "").strip().lower()
        if cat in ("other", "unassigned", ""):
            return False
    return True


def _od10_category_rows_from_query(cur, rows) -> list[dict]:
    out: list[dict] = []
    for bucket, emis, dist_km, trips in rows:
        cat = _dashboard_server._route_bucket_category(bucket)
        em = float(emis or 0)
        out.append({
            "category": cat,
            "trips": float(trips or 0),
            "trips_legs": float(trips or 0),
            "total_emissions_g": em,
            "total_emissions_tonnes": em / 1e6,
            "total_distance_km": float(dist_km or 0),
        })
    return out


def _od10_scale_categories_for_zone(
    categories: list[dict], zone_stats: dict | None
) -> list[dict]:
    """Approximate zone mix from island totals when leg-level breakdown is unavailable."""
    if not categories or not zone_stats:
        return []
    z_emis = float(zone_stats.get("total_emissions_g") or 0)
    z_trips = float(zone_stats.get("trips") or 0)
    z_km = float(zone_stats.get("total_distance_km") or 0)
    island_emis = sum(float(c.get("total_emissions_g") or 0) for c in categories)
    island_trips = sum(float(c.get("trips") or 0) for c in categories)
    island_km = sum(float(c.get("total_distance_km") or 0) for c in categories)
    if island_emis <= 0 or z_emis <= 0:
        return []
    emis_ratio = z_emis / island_emis
    trips_ratio = (z_trips / island_trips) if island_trips > 0 else emis_ratio
    km_ratio = (z_km / island_km) if island_km > 0 else emis_ratio
    out: list[dict] = []
    for c in categories:
        out.append({
            "category": c.get("category"),
            "trips": float(c.get("trips") or 0) * trips_ratio,
            "trips_legs": float(c.get("trips") or 0) * trips_ratio,
            "total_emissions_g": float(c.get("total_emissions_g") or 0) * emis_ratio,
            "total_emissions_tonnes": float(c.get("total_emissions_g") or 0) * emis_ratio / 1e6,
            "total_distance_km": float(c.get("total_distance_km") or 0) * km_ratio,
        })
    return out


def _od10_zone_by_category_from_detail(cur, geo_id: str, *, zone_by: str) -> list[dict]:
    """Travel-reason chart rows for one zone from trips_route_emissions (or legacy detail)."""
    detail = _od10_detail_table(cur)
    if not detail:
        return []
    gid = str(geo_id or "").strip()
    if not gid:
        return []
    mode = "dest" if (zone_by or "rules").strip().lower() == "dest" else "rules"
    zcol = _od10_zone_detail_column(cur, detail, zone_by=mode)
    if not zcol:
        return []
    emis_c = _od10_detail_emissions_col(cur, detail)
    bucket_sql = _od10_detail_travel_reason_bucket_sql(cur, detail, "d")
    if not bucket_sql:
        return []
    use_w = _od10_metrics_weighted() and _column_exists(cur, detail, "d_fexp")
    if use_w and _column_exists(cur, detail, "expanded_emissions_g"):
        emis_expr = f"COALESCE(d.expanded_emissions_g, d.{emis_c})"
        trips_expr = "COALESCE(d.d_fexp, 1)::double precision"
        dist_expr = "COALESCE(d.distance_m, 0)::double precision * COALESCE(d.d_fexp, 1)"
    else:
        emis_expr = f"COALESCE(d.{emis_c}, 0)::double precision"
        trips_expr = "1::double precision"
        dist_expr = f"COALESCE(d.distance_m, 0)::double precision"
    zone_match = _od10_zone_match_sql("d", zcol)
    cur.execute(
        f"""
        SELECT {bucket_sql} AS bucket,
               SUM({emis_expr})::double precision,
               SUM({dist_expr})::double precision / 1000.0,
               SUM({trips_expr})::double precision
        FROM {SCHEMA}.{detail} d
        WHERE {zone_match}
          AND {emis_expr} > 0
        GROUP BY 1
        ORDER BY 2 DESC NULLS LAST
        """,
        (gid,),
    )
    return _od10_category_rows_from_query(cur, cur.fetchall())


def _od10_zone_by_category_from_routes(cur, geo_id: str, *, zone_by: str) -> list[dict]:
    """Travel-reason chart rows for one zone from trip_routes table."""
    routes_t = _resolve_od10_routes_table(cur)
    if not routes_t:
        return []
    gid = str(geo_id or "").strip()
    if not gid:
        return []
    mode = "dest" if (zone_by or "rules").strip().lower() == "dest" else "rules"
    if mode == "dest":
        if not _column_exists(cur, routes_t, "dest_geo_id"):
            return []
        zcol = "dest_geo_id"
    else:
        zcol = None
        for col in ("route_attributed_geo_id", "emission_zone_geo_id", "route_attributed_zone_geo_id"):
            if _column_exists(cur, routes_t, col):
                zcol = col
                break
        if not zcol:
            return []
    emis_c = "route_emissions_g" if _column_exists(cur, routes_t, "route_emissions_g") else None
    if not emis_c:
        return []
    has_purpose = _column_exists(cur, routes_t, "purpose")
    if has_purpose or _column_exists(cur, routes_t, "route_emission_assignment"):
        bucket_sql = _dashboard_server._routes_travel_reason_bucket_sql(
            "r", has_purpose=has_purpose
        )
    else:
        bucket_sql = "'other'"
    zone_match = _od10_zone_match_sql("r", zcol)
    car_modes = _dashboard_server.CAR_MODE_GROUPS
    cur.execute(
        f"""
        SELECT {bucket_sql} AS bucket,
               SUM(r.{emis_c})::double precision,
               SUM(COALESCE(r.distance_m, 0))::double precision / 1000.0,
               COUNT(*)::double precision
        FROM {SCHEMA}.{routes_t} r
        WHERE {zone_match}
          AND r.{emis_c} IS NOT NULL AND r.{emis_c} > 0
          AND trim(r.mode_group::text) IN {car_modes}
        GROUP BY 1
        ORDER BY 2 DESC NULLS LAST
        """,
        (gid,),
    )
    return _od10_category_rows_from_query(cur, cur.fetchall())


def _od10_zone_by_category(cur, geo_id: str, *, zone_by: str, zone_stats: dict | None) -> list[dict]:
    by_category = _od10_zone_by_category_from_detail(cur, geo_id, zone_by=zone_by)
    if not _od10_zone_by_category_is_usable(by_category):
        by_category = _od10_zone_by_category_from_routes(cur, geo_id, zone_by=zone_by)
    if not _od10_zone_by_category_is_usable(by_category):
        by_category = _od10_scale_categories_for_zone(
            _od10_fetch_global_by_category(cur), zone_stats
        )
    return by_category


@app.route("/api/od/zone_sidebar")
@app.route("/api/od/zone_bootstrap")
@_od10_api_errors
def api_od10_zone_bootstrap():
    geo_id = (request.args.get("geo_id") or request.args.get("dest_geo_id") or "").strip()
    if not geo_id:
        return jsonify({"error": "geo_id is required"}), 400
    zone_by = (request.args.get("zone_by") or request.args.get("attribution") or "rules").strip().lower()
    if zone_by not in ("rules", "dest"):
        zone_by = "rules"
    conn = get_conn()
    try:
        cur = conn.cursor()
        stats = _od10_single_zone_stats(cur, geo_id, zone_by=zone_by)
        by_category = _od10_zone_by_category(cur, geo_id, zone_by=zone_by, zone_stats=stats)
        zone_codes, zone_names = _zone_code_maps_for_geom(cur)
        label = zone_codes.get(geo_id) or zone_names.get(geo_id) or geo_id
        if label and label != geo_id and zone_names.get(geo_id):
            label = f"{zone_names.get(geo_id)} – {zone_codes.get(geo_id, geo_id)}"
        elif zone_names.get(geo_id) and zone_codes.get(geo_id):
            label = f"{zone_names.get(geo_id)} – {zone_codes.get(geo_id)}"
        return jsonify({
            "geo_id": geo_id,
            "zone_by": zone_by,
            "zone_label": label,
            "stats": stats,
            "by_category": by_category,
            "source": "od",
        })
    finally:
        conn.close()


def _od10_patch_incoming_flow_anchors(cur, payload: dict) -> None:
    """Replace stale precomputed orig/dest lat-lon with live anchors from zones_geom + trip routes."""
    routes_t = _resolve_od10_routes_table(cur)
    if not routes_t:
        return
    geo_ids: set[str] = set()
    dest_id = str(payload.get("dest_geo_id") or "").strip()
    if dest_id:
        geo_ids.add(dest_id)
    for flow in payload.get("flows") or []:
        oid = str(flow.get("orig_geo_id") or "").strip()
        if oid:
            geo_ids.add(oid)
    if not geo_ids:
        return
    anchors = fetch_zone_flow_anchors(cur, routes_t, geo_ids)
    patch_flow_payload_anchors(payload, anchors)


def _od10_patch_flows_all_anchors(cur, zones: dict[str, dict]) -> None:
    routes_t = _resolve_od10_routes_table(cur)
    if not routes_t or not zones:
        return
    geo_ids: set[str] = set()
    for z in zones.values():
        did = str(z.get("dest_geo_id") or "").strip()
        if did:
            geo_ids.add(did)
        for flow in z.get("flows") or []:
            oid = str(flow.get("orig_geo_id") or "").strip()
            if oid:
                geo_ids.add(oid)
    if not geo_ids:
        return
    anchors = fetch_zone_flow_anchors(cur, routes_t, geo_ids)
    for z in zones.values():
        patch_flow_payload_anchors(z, anchors)


def _od10_incoming_payload_from_rows(
    dest_id: str,
    *,
    frows: list,
    trow,
    limit: int | None,
    flows_t: str,
    zone_by: str = "rules",
) -> dict:
    dest_lat = _flow_coord(trow[3]) if trow else None
    dest_lon = _flow_coord(trow[4]) if trow else None
    flows = [
        _attach_zone_code({
            "orig_geo_id": str(r[0]),
            "trips": float(r[1] or 0),
            "total_emissions_g": float(r[2] or 0),
            "total_distance_km": float(r[3] or 0),
            "orig_lat": _flow_coord(r[4]),
            "orig_lon": _flow_coord(r[5]),
            "dest_lat": dest_lat,
            "dest_lon": dest_lon,
            "dest_zone_code": _zone_code_for(dest_id),
        }, geo_id_key="orig_geo_id")
        for r in frows
    ]
    inc_km = float(trow[2] or 0) if trow and len(trow) > 2 and trow[2] is not None else 0.0
    weighted = _od10_metrics_weighted()
    return {
        "dest_geo_id": dest_id,
        "dest_zone_code": _zone_code_for(dest_id),
        "dest_zone_name": _zone_name_for(dest_id),
        "dest_zone_short_name": _zone_short_name_for(dest_id),
        "dest_zone_label": _zone_label_for(dest_id),
        "dest_short_id": _short_zone_id(dest_id),
        "zone_by": zone_by,
        "source": "od",
        "metrics_mode": "weighted" if weighted else "legs",
        "metrics_note": _od10_metrics_note(),
        "precomputed": True,
        "dest_lat": dest_lat,
        "dest_lon": dest_lon,
        "total_incoming_trips": float(trow[0] or 0) if trow else 0,
        "total_incoming_emissions_g": float(trow[1] or 0) if trow else 0.0,
        "total_incoming_distance_km": round(inc_km, 2),
        "origin_zone_count": int(trow[5] or 0) if trow and len(trow) > 5 else 0,
        "dest_rules_trips": float(trow[6] or 0) if trow and len(trow) > 6 and trow[6] is not None else None,
        "dest_rules_emissions_g": float(trow[7] or 0) if trow and len(trow) > 7 and trow[7] is not None else None,
        "flows_shown_trips": sum(f["trips"] for f in flows),
        "flow_count": len(flows),
        "flows": flows,
        "limit": limit if limit is not None else "all",
        "table": flows_t,
        "intra_zone": None,
    }


def _od10_detail_table(cur) -> str | None:
    return _resolve_od10_table(cur, OD10_DETAIL_CANDIDATES)


def _od10_building_id_col(building_by: str) -> str:
    return "dest_building_id" if building_by == "dest" else "emission_building_id"


def _od10_building_touch_sql(building_id_expr: str) -> str:
    """Legs where the building is a trip origin or destination footprint."""
    b = building_id_expr
    return f"(orig_building_id::text = {b} OR dest_building_id::text = {b})"


def _od10_building_agg_subquery(
    cur,
    detail_tab: str | None,
    building_by: str,
    min_g: float,
) -> str:
    """Per-building totals from precomputed unified table, else flat detail."""
    bt = _od10_building_emissions_table(cur)
    if bt:
        trips_c, tw_c, em_c, dist_c = _od10_building_metric_cols(cur, bt, building_by)
        return f"""
    SELECT building_id,
           {em_c}::double precision AS emissions_g,
           {trips_c}::bigint AS trips,
           COALESCE({tw_c}, 0)::double precision AS trips_weighted,
           COALESCE({dist_c}, 0)::double precision AS distance_km
    FROM {SCHEMA}.{bt}
    WHERE {em_c} >= {float(min_g)}
      AND building_id IS NOT NULL
      AND btrim(building_id) <> ''
    """
    if not detail_tab:
        raise ValueError("missing OD10 building emissions and detail tables")
    bid = _od10_building_id_col(building_by)
    emis_c = _od10_detail_emissions_col(cur, detail_tab)
    island_sql = _od10_detail_island_sql(cur, detail_tab)
    return f"""
    SELECT {bid}::text AS building_id,
           SUM({emis_c})::double precision AS emissions_g,
           COUNT(*)::bigint AS trips,
           COALESCE(SUM(d_fexp), 0)::double precision AS trips_weighted,
           COALESCE(SUM(distance_m), 0)::double precision / 1000.0 AS distance_km
    FROM {SCHEMA}.{detail_tab}
    WHERE {island_sql}
      AND {emis_c} >= {float(min_g)}
      AND {bid} IS NOT NULL
      AND btrim({bid}::text) <> ''
    GROUP BY {bid}
    """


def _od10_building_emission_scale_bounds(
    cur,
    detail_tab: str | None,
    *,
    building_by: str,
) -> dict | None:
    """Min/max per-building leg emissions (g) for map legend scales."""
    agg = _od10_building_agg_subquery(cur, detail_tab, building_by, 0.0)
    cur.execute(
        f"""
        SELECT COALESCE(MIN(s.emissions_g), 0)::double precision,
               COALESCE(MAX(s.emissions_g), 0)::double precision,
               COUNT(*)::bigint
        FROM ({agg}) AS s
        WHERE s.emissions_g > 0
        """
    )
    row = cur.fetchone()
    if not row or int(row[2] or 0) <= 0:
        return None
    min_g = float(row[0] or 0)
    max_g = float(row[1] or 0)
    if max_g <= 0:
        return None
    return {
        "building_by": building_by,
        "min_g": min_g,
        "max_g": max_g,
        "building_count": int(row[2]),
        "metrics_mode": "legs",
    }


@app.route("/api/od/building_emission_scale")
def api_od10_building_emission_scale():
    """Per-building min/max emissions (g) for the buildings map colour legend."""
    building_by = _normalize_building_by(request.args.get("building_by") or "rules")
    conn = get_conn()
    try:
        cur = conn.cursor()
        detail_tab = _od10_detail_table(cur)
        building_tab = _od10_building_emissions_table(cur)
        if not detail_tab and not building_tab:
            return jsonify({
                "error": "missing_table",
                "message": (
                    "No OD10 building emissions table. Run: "
                    "python scripts/build_od10_building_emissions_precompute.py"
                ),
            }), 503
        bounds = _od10_building_emission_scale_bounds(
            cur, detail_tab, building_by=building_by
        )
        if not bounds:
            return jsonify({
                "building_by": building_by,
                "min_g": 0,
                "max_g": 0,
                "building_count": 0,
                "metrics_mode": "legs",
            })
        return jsonify(bounds)
    finally:
        conn.close()


def _request_include_footprints(default: bool = True) -> bool:
    raw = request.args.get("include_footprints")
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _footprint_features_from_inventory_rows(
    rows: list,
    inventory_by_id: dict[str, dict],
) -> list[dict]:
    """Fallback footprints from building_map row geom_json when fabric query returns none."""
    features: list[dict] = []
    for r in rows:
        if not r or r[0] is None:
            continue
        geom_json = r[11] if len(r) > 11 else None
        if not geom_json:
            continue
        bid = str(r[0])
        inv = inventory_by_id.get(bid)
        props: dict = {
            "building_id": bid,
            "zone_geo_id": r[6] if len(r) > 6 else None,
            "in_inventory": inv is not None,
        }
        if inv:
            props.update({
                "total_emissions_g": inv.get("total_emissions_g", 0),
                "trips": inv.get("trips", inv.get("trips_legs", 0)),
                "trips_legs": inv.get("trips_legs", inv.get("trips", 0)),
                "trips_weighted": inv.get("trips_weighted", 0),
                "trips_display": inv.get("trips_display"),
                "total_distance_km": inv.get("total_distance_km", 0),
            })
        props = _od10_zero_building_metrics(props)
        feat = _geom_json_to_feature(geom_json, props)
        if feat:
            features.append(feat)
    return features


def _zone_building_fabric_features(
    cur,
    *,
    zone_geo_id: str,
    geom_sql: str,
    inventory_by_id: dict[str, dict],
    row_limit: int,
) -> tuple[list[dict], bool]:
    """Footprint polygons in a zone; inventory buildings first, then others up to row_limit."""
    inv_ids = [str(bid).strip() for bid in inventory_by_id.keys() if bid]
    features: list[dict] = []
    seen: set[str] = set()

    def _append_row(r: tuple) -> None:
        bid = str(r[0] or "").strip()
        geom_json = r[2]
        if not bid or not geom_json or bid in seen:
            return
        seen.add(bid)
        inv = inventory_by_id.get(bid)
        props: dict = {
            "building_id": bid,
            "zone_geo_id": r[1],
            "in_inventory": inv is not None,
        }
        if inv:
            props.update({
                "total_emissions_g": inv.get("total_emissions_g", 0),
                "trips": inv.get("trips", inv.get("trips_legs", 0)),
                "trips_legs": inv.get("trips_legs", inv.get("trips", 0)),
                "trips_weighted": inv.get("trips_weighted", 0),
                "trips_display": inv.get("trips_display"),
                "total_distance_km": inv.get("total_distance_km", 0),
            })
        props = _od10_zero_building_metrics(props)
        feat = _geom_json_to_feature(geom_json, props)
        if feat:
            features.append(feat)

    if inv_ids:
        cur.execute(
            f"""
            SELECT b.id::text,
                   split_part(trim(b.zone_geo_id::text), '.', 1) AS zone_geo_id,
                   {geom_sql} AS geom_json
            FROM {SCHEMA}.{BUILDINGS_TABLE} AS b
            WHERE b.geometry IS NOT NULL
              AND split_part(trim(b.zone_geo_id::text), '.', 1) = %s
              AND b.id::text = ANY(%s)
            ORDER BY b.id::text
            """,
            (zone_geo_id, inv_ids),
        )
        for r in cur.fetchall():
            if len(features) >= row_limit:
                break
            _append_row(r)

    remaining = row_limit - len(features)
    if remaining > 0:
        exclude = list(seen)
        if exclude:
            cur.execute(
                f"""
                SELECT b.id::text,
                       split_part(trim(b.zone_geo_id::text), '.', 1) AS zone_geo_id,
                       {geom_sql} AS geom_json
                FROM {SCHEMA}.{BUILDINGS_TABLE} AS b
                WHERE b.geometry IS NOT NULL
                  AND split_part(trim(b.zone_geo_id::text), '.', 1) = %s
                  AND NOT (b.id::text = ANY(%s))
                ORDER BY b.id::text
                LIMIT %s
                """,
                (zone_geo_id, exclude, remaining + 1),
            )
        else:
            cur.execute(
                f"""
                SELECT b.id::text,
                       split_part(trim(b.zone_geo_id::text), '.', 1) AS zone_geo_id,
                       {geom_sql} AS geom_json
                FROM {SCHEMA}.{BUILDINGS_TABLE} AS b
                WHERE b.geometry IS NOT NULL
                  AND split_part(trim(b.zone_geo_id::text), '.', 1) = %s
                ORDER BY b.id::text
                LIMIT %s
                """,
                (zone_geo_id, remaining + 1),
            )
        extra = cur.fetchall()
        extra_truncated = len(extra) > remaining
        if extra_truncated:
            extra = extra[:remaining]
        for r in extra:
            _append_row(r)
    else:
        extra_truncated = False

    cur.execute(
        f"""
        SELECT COUNT(*)::bigint
        FROM {SCHEMA}.{BUILDINGS_TABLE} AS b
        WHERE b.geometry IS NOT NULL
          AND split_part(trim(b.zone_geo_id::text), '.', 1) = %s
        """,
        (zone_geo_id,),
    )
    zone_total = int((cur.fetchone() or [0])[0] or 0)
    truncated = zone_total > row_limit or extra_truncated or len(inv_ids) > row_limit
    return features, truncated


@app.route("/api/od/zone_building_fabric")
def api_od10_zone_building_fabric():
    """All building footprint polygons in a zone (independent of emissions filter)."""
    zone_geo_id = (request.args.get("zone_geo_id") or "").strip()
    if not zone_geo_id:
        return jsonify({"error": "zone_geo_id_required"}), 400
    try:
        row_limit = int(request.args.get("limit", "50000") or "50000")
    except Exception:
        row_limit = 50000
    row_limit = max(100, min(row_limit, 500000))
    geom_sql = _building_footprint_geojson_sql("b")
    conn = get_conn()
    try:
        cur = conn.cursor()
        features, truncated = _zone_building_fabric_features(
            cur,
            zone_geo_id=zone_geo_id,
            geom_sql=geom_sql,
            inventory_by_id={},
            row_limit=row_limit,
        )
        return jsonify({
            "zone_geo_id": zone_geo_id,
            "fabric_truncated": truncated,
            "limit": row_limit,
            "footprint_fc": {"type": "FeatureCollection", "features": features},
        })
    finally:
        conn.close()


@app.route("/api/od/building_map")
def api_od10_building_map():
    """Building-level emissions from OD10 routes detail (rules or destination building)."""
    try:
        min_kg = float(request.args.get("min_kg", "0") or "0")
    except Exception:
        min_kg = 0.0
    min_g = max(0.0, min_kg * 1000.0)
    max_g = None
    raw_max = request.args.get("max_kg")
    if raw_max is not None and str(raw_max).strip() != "":
        try:
            max_g = float(raw_max) * 1000.0
        except Exception:
            max_g = None
    try:
        row_limit = int(request.args.get("limit", "50000") or "50000")
    except Exception:
        row_limit = 50000
    row_limit = max(100, min(row_limit, 500000))
    grid_cell_deg = _parse_building_grid_cell_deg(request.args.get("grid_cell_deg"))
    zone_geo_id = (request.args.get("zone_geo_id") or "").strip()
    building_by = _normalize_building_by(request.args.get("building_by") or "rules")
    island_only = _request_island_only(default=True)

    conn = get_conn()
    try:
        cur = conn.cursor()
        detail_tab = _od10_detail_table(cur)
        building_tab = _od10_building_emissions_table(cur)
        if not detail_tab and not building_tab:
            return jsonify({
                "error": "missing_table",
                "message": (
                    "No OD10 building emissions table. Run: "
                    "python scripts/build_od10_building_emissions_precompute.py "
                    "or python script_10/10_build_od10_routes_emissions_detail.py"
                ),
            }), 503

        buildings_rel = BUILDINGS_TABLE
        lat_sql = _building_map_lat_sql("b")
        lon_sql = _building_map_lon_sql("b")
        include_footprints = _request_include_footprints(default=bool(zone_geo_id))
        geom_sql = _building_footprint_geojson_sql("b") if include_footprints else "NULL::text"
        zone_pred, zone_params = _building_zone_filter_sql(cur, lat_sql, lon_sql, zone_geo_id)
        agg_sql = _od10_building_agg_subquery(cur, detail_tab, building_by, min_g)
        source_table = building_tab or detail_tab

        if not zone_geo_id:
            cur.close()
            return jsonify({
                "buildings": [],
                "building_by": building_by,
                "source_table": source_table,
                "truncated": False,
                "limit": row_limit,
                "zone_geo_id": None,
                "point_source": "footprint",
                "metrics_mode": "weighted" if _od10_metrics_weighted() else "legs",
                "hint": "Select a zone on the map to load buildings.",
            })

        emax = ""
        params: list = []
        if max_g is not None and max_g >= min_g:
            emax = " AND e.emissions_g <= %s"
            params.append(max_g)
        params.extend(zone_params)

        if grid_cell_deg is not None:
            cell = float(grid_cell_deg)
            cur.execute(
                f"""
                SELECT ((FLOOR(({lat_sql})::double precision / {cell}) * {cell}) + ({cell} / 2.0))::double precision AS lat,
                       ((FLOOR(({lon_sql})::double precision / {cell}) * {cell}) + ({cell} / 2.0))::double precision AS lon,
                       SUM(e.emissions_g)::double precision AS total_emissions_g,
                       SUM(e.trips)::bigint AS trips,
                       SUM(e.trips_weighted)::double precision AS trips_weighted,
                       COUNT(*)::bigint AS building_count,
                       SUM(e.distance_km)::double precision AS total_distance_km
                FROM ({agg_sql}) AS e
                JOIN {SCHEMA}.{buildings_rel} AS b ON b.id::text = e.building_id
                WHERE b.geometry IS NOT NULL{emax}{zone_pred}
                GROUP BY FLOOR(({lat_sql})::double precision / {cell}), FLOOR(({lon_sql})::double precision / {cell})
                ORDER BY SUM(e.emissions_g) DESC
                LIMIT %s
                """,
                tuple(params + [row_limit + 1]),
            )
            rows = cur.fetchall()
            truncated = len(rows) > row_limit
            if truncated:
                rows = rows[:row_limit]
            out = []
            for r in rows:
                item = {
                    "lat": float(r[0]),
                    "lon": float(r[1]),
                    "total_emissions_g": float(r[2] or 0),
                    "trips": int(r[3] or 0),
                    "trips_weighted": float(r[4] or 0),
                    "building_count": int(r[5] or 0),
                    "total_distance_km": round(float(r[6] or 0), 2),
                }
                item = _od10_building_panel_row({
                    **item,
                    "trips_legs": item["trips"],
                })
                out.append(item)
            cur.close()
            return jsonify({
                "buildings": out,
                "building_by": building_by,
                "source_table": source_table,
                "truncated": truncated,
                "limit": row_limit,
                "zone_geo_id": zone_geo_id,
                "grid_cell_deg": cell,
                "metrics_mode": "legs",
            })

        metrics_mode = "expanded" if _od10_building_population_alloc_enabled() else "legs"

        if _od10_building_population_alloc_enabled():
            bid = _od10_building_id_col(building_by)
            zcol = _od10_zone_geo_col(building_by)
            has_jobs = _table_exists(cur, "building_jobs")
            jobs_join = (
                f"LEFT JOIN {SCHEMA}.building_jobs j ON j.building_id::text = b.id::text"
                if has_jobs
                else ""
            )
            w_sql = _od10_building_capacity_weight_sql(cur, b="b", j="j")
            emax_sql = ""
            pop_params: list = [zone_geo_id, zone_geo_id]
            if max_g is not None and max_g >= min_g:
                emax_sql = " AND disp_emissions_g <= %s"
                pop_params.append(max_g)
            pop_params.append(min_g)
            pop_params.append(row_limit + 1)
            emis_c = _od10_detail_emissions_col(cur, detail_tab)
            island_sql = _od10_detail_island_sql(cur, detail_tab)
            cur.execute(
                f"""
                WITH zone_tot AS (
                  SELECT COALESCE(SUM(d_fexp), 0)::double precision AS t_exp,
                         COALESCE(SUM({emis_c}), 0)::double precision AS e_exp,
                         COALESCE(SUM(distance_m), 0)::double precision / 1000.0 AS d_exp
                  FROM {SCHEMA}.{detail_tab}
                  WHERE {island_sql}
                    AND split_part(trim({zcol}::text), '.', 1) = %s
                ),
                agg AS (
                  SELECT {bid}::text AS building_id,
                         SUM({emis_c})::double precision AS emissions_g,
                         COUNT(*)::bigint AS trips_legs,
                         COALESCE(SUM(d_fexp), 0)::double precision AS trips_weighted,
                         COALESCE(SUM(distance_m), 0)::double precision / 1000.0 AS distance_km
                  FROM {SCHEMA}.{detail_tab}
                  WHERE {island_sql}
                    AND {bid} IS NOT NULL AND btrim({bid}::text) <> ''
                  GROUP BY {bid}
                ),
                bldgs AS (
                  SELECT b.id::text AS building_id,
                         split_part(trim(b.zone_geo_id::text), '.', 1) AS zone_geo_id,
                         lower(COALESCE(b.use_class::text, '')) AS use_class,
                         ({w_sql})::double precision AS w,
                         {lat_sql}::double precision AS lat,
                         {lon_sql}::double precision AS lon,
                         {geom_sql} AS geom_json
                  FROM {SCHEMA}.{buildings_rel} b
                  {jobs_join}
                  WHERE b.geometry IS NOT NULL
                    AND split_part(trim(b.zone_geo_id::text), '.', 1) = %s
                ),
                weighted AS (
                  SELECT b.*,
                         SUM(b.w) OVER (PARTITION BY b.use_class) AS w_all
                  FROM bldgs b
                ),
                scored AS (
                  SELECT w.building_id,
                         w.lat,
                         w.lon,
                         w.zone_geo_id,
                         w.geom_json,
                         COALESCE(a.trips_legs, 0)::bigint AS trips_legs,
                         COALESCE(a.trips_weighted, 0)::double precision AS trips_assigned_weighted,
                         zt.e_exp * w.w / NULLIF(w.w_all, 0) AS disp_emissions_g,
                         zt.t_exp * w.w / NULLIF(w.w_all, 0) AS trips_weighted,
                         zt.d_exp * w.w / NULLIF(w.w_all, 0) AS total_distance_km,
                         (COALESCE(a.trips_legs, 0) = 0) AS trips_is_imputed,
                         true AS trips_is_capacity_share
                  FROM weighted w
                  CROSS JOIN zone_tot zt
                  LEFT JOIN agg a ON a.building_id = w.building_id
                  WHERE w.lat IS NOT NULL AND w.lon IS NOT NULL
                )
                SELECT building_id, lat, lon, disp_emissions_g, trips_legs, trips_weighted,
                       zone_geo_id, total_distance_km, trips_is_imputed, trips_assigned_weighted,
                       trips_is_capacity_share, geom_json
                FROM scored
                WHERE disp_emissions_g >= %s{emax_sql}
                ORDER BY disp_emissions_g DESC
                LIMIT %s
                """,
                tuple(pop_params),
            )
        else:
            if not building_tab:
                raise ValueError("building_map requires building_emissions table when population alloc is off")
            trips_c, tw_c, em_c, dist_c = _od10_building_metric_cols(cur, building_tab, building_by)
            legs_params: list = [zone_geo_id]
            emax_legs = ""
            if max_g is not None and max_g >= min_g:
                emax_legs = " AND COALESCE(e.emissions_g, 0) <= %s"
                legs_params.append(max_g)
            legs_params.append(min_g)
            legs_params.append(row_limit + 1)
            if building_tab:
                cap_share_sql = (
                    "COALESCE(e.trips_is_capacity_share, false)"
                    if _column_exists(cur, building_tab, "trips_is_capacity_share")
                    else "false"
                )
                cur.execute(
                    f"""
                    SELECT b.id::text AS building_id,
                           {lat_sql}::double precision AS lat,
                           {lon_sql}::double precision AS lon,
                           COALESCE(e.{em_c}, 0)::double precision AS total_emissions_g,
                           COALESCE(e.{trips_c}, 0)::bigint AS trips_legs,
                           COALESCE(e.{tw_c}, 0)::double precision AS trips_weighted,
                           split_part(trim(b.zone_geo_id::text), '.', 1) AS zone_geo_id,
                           COALESCE(e.{dist_c}, 0)::double precision AS total_distance_km,
                           false AS trips_is_imputed,
                           COALESCE(e.{tw_c}, 0)::double precision AS trips_assigned_weighted,
                           {cap_share_sql} AS trips_is_capacity_share,
                           {geom_sql} AS geom_json
                    FROM {SCHEMA}.{buildings_rel} AS b
                    LEFT JOIN {SCHEMA}.{building_tab} AS e ON e.building_id = b.id::text
                    WHERE b.geometry IS NOT NULL
                      AND split_part(trim(b.zone_geo_id::text), '.', 1) = %s
                      AND COALESCE(e.{em_c}, 0) >= %s{emax_legs}
                    ORDER BY COALESCE(e.{em_c}, 0) DESC, b.id::text
                    LIMIT %s
                    """,
                    tuple(legs_params),
                )
            elif detail_tab:
                emis_c = _od10_detail_emissions_col(cur, detail_tab)
                island_sql = _od10_detail_island_sql(cur, detail_tab)
                cur.execute(
                    f"""
                    WITH leg_touch AS (
                      SELECT orig_building_id::text AS building_id,
                             {emis_c} AS emissions_g,
                             d_fexp,
                             distance_m
                      FROM {SCHEMA}.{detail_tab}
                      WHERE {island_sql}
                        AND orig_building_id IS NOT NULL
                        AND btrim(orig_building_id::text) <> ''
                      UNION ALL
                      SELECT dest_building_id::text AS building_id,
                             {emis_c} AS emissions_g,
                             d_fexp,
                             distance_m
                      FROM {SCHEMA}.{detail_tab}
                      WHERE {island_sql}
                        AND dest_building_id IS NOT NULL
                        AND btrim(dest_building_id::text) <> ''
                    ),
                    touch AS (
                      SELECT building_id,
                             COUNT(*)::bigint AS trips_legs,
                             COALESCE(SUM(emissions_g), 0)::double precision AS emissions_g,
                             COALESCE(SUM(d_fexp), 0)::double precision AS trips_weighted,
                             COALESCE(SUM(distance_m), 0)::double precision / 1000.0 AS distance_km
                      FROM leg_touch
                      GROUP BY building_id
                    )
                    SELECT b.id::text AS building_id,
                           {lat_sql}::double precision AS lat,
                           {lon_sql}::double precision AS lon,
                           COALESCE(t.emissions_g, 0)::double precision AS total_emissions_g,
                           COALESCE(t.trips_legs, 0)::bigint AS trips_legs,
                           COALESCE(t.trips_weighted, 0)::double precision AS trips_weighted,
                           split_part(trim(b.zone_geo_id::text), '.', 1) AS zone_geo_id,
                           COALESCE(t.distance_km, 0)::double precision AS total_distance_km,
                           false AS trips_is_imputed,
                           COALESCE(t.trips_weighted, 0)::double precision AS trips_assigned_weighted,
                           false AS trips_is_capacity_share,
                           {geom_sql} AS geom_json
                    FROM {SCHEMA}.{buildings_rel} AS b
                    LEFT JOIN touch AS t ON t.building_id = b.id::text
                    WHERE b.geometry IS NOT NULL
                      AND split_part(trim(b.zone_geo_id::text), '.', 1) = %s
                      AND COALESCE(t.emissions_g, 0) >= %s{emax_legs}
                    ORDER BY COALESCE(t.emissions_g, 0) DESC, b.id::text
                    LIMIT %s
                    """,
                    tuple(legs_params),
                )
            else:
                cur.close()
                return jsonify({"error": "missing_table", "message": "No building emissions source."}), 503
        rows = cur.fetchall()
        truncated = len(rows) > row_limit
        if truncated:
            rows = rows[:row_limit]
        out = []
        for r in rows:
            if r[0] is None or r[1] is None or r[2] is None:
                continue
            item = {
                "building_id": r[0],
                "lat": float(r[1]),
                "lon": float(r[2]),
                "total_emissions_g": float(r[3] or 0),
                "trips_legs": int(r[4] or 0),
                "trips_weighted": float(r[5] or 0),
                "zone_geo_id": r[6],
                "total_distance_km": round(float(r[7] or 0), 2),
                "trips_is_imputed": bool(r[8]) if len(r) > 8 else False,
                "trips_assigned_weighted": float(r[9] or 0) if len(r) > 9 else float(r[5] or 0),
                "trips_is_capacity_share": bool(r[10]) if len(r) > 10 else False,
            }
            item = _od10_building_panel_row(item)
            out.append(item)
        inventory_by_id = {str(b["building_id"]): b for b in out}
        fabric_truncated = False
        footprint_features: list[dict] = []
        if include_footprints and zone_geo_id:
            footprint_features, fabric_truncated = _zone_building_fabric_features(
                cur,
                zone_geo_id=zone_geo_id,
                geom_sql=geom_sql,
                inventory_by_id=inventory_by_id,
                row_limit=row_limit,
            )
            if not footprint_features:
                footprint_features = _footprint_features_from_inventory_rows(rows, inventory_by_id)
        return jsonify({
            "buildings": out,
            "building_by": building_by,
            "source_table": source_table,
            "truncated": truncated,
            "fabric_truncated": fabric_truncated,
            "limit": row_limit,
            "zone_geo_id": zone_geo_id or None,
            "point_source": "footprint",
            "metrics_mode": metrics_mode,
            "population_alloc": _od10_building_population_alloc_enabled(),
            "footprint_fc": (
                {"type": "FeatureCollection", "features": footprint_features}
                if include_footprints
                else None
            ),
        })
    finally:
        conn.close()


@app.route("/api/od/building_footprint")
def api_od10_building_footprint():
    building_id = (request.args.get("building_id") or "").strip()
    if not building_id:
        return jsonify({"error": "building_id_required"}), 400
    conn = get_conn()
    try:
        cur = conn.cursor()
        row = _fetch_building_footprint_row(cur, building_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        feature = _geom_json_to_feature(row[12], {"building_id": row[0], "zone_geo_id": row[1]})
        if not feature:
            return jsonify({"error": "no_geometry"}), 404
        return jsonify({"building_id": row[0], "geojson": feature})
    finally:
        conn.close()


@app.route("/api/od/building_detail")
def api_od10_building_detail():
    building_id = (request.args.get("building_id") or "").strip()
    if not building_id:
        return jsonify({"error": "building_id_required"}), 400
    building_by = _normalize_building_by(request.args.get("building_by") or "rules")
    conn = get_conn()
    try:
        cur = conn.cursor()
        row = _fetch_building_footprint_row(cur, building_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        detail_tab = _od10_detail_table(cur)
        building_tab = _od10_building_emissions_table(cur)
        emissions_g = 0.0
        trips = 0
        trips_weighted = 0.0
        distance_km = 0.0
        if building_tab and not _od10_building_population_alloc_enabled():
            trips_c, tw_c, em_c, dist_c = _od10_building_metric_cols(cur, building_tab, building_by)
            cur.execute(
                f"""
                SELECT COALESCE({em_c}, 0)::double precision,
                       COALESCE({trips_c}, 0)::bigint,
                       COALESCE({tw_c}, 0)::double precision,
                       COALESCE({dist_c}, 0)::double precision
                FROM {SCHEMA}.{building_tab}
                WHERE building_id = %s
                """,
                (building_id,),
            )
            er = cur.fetchone()
            if er:
                emissions_g = float(er[0] or 0)
                trips = int(er[1] or 0)
                trips_weighted = float(er[2] or 0)
                distance_km = float(er[3] or 0)
        elif detail_tab:
            touch = _od10_building_touch_sql("%s")
            emis_c = _od10_detail_emissions_col(cur, detail_tab)
            island_sql = _od10_detail_island_sql(cur, detail_tab)
            cur.execute(
                f"""
                SELECT COALESCE(SUM({emis_c}), 0)::double precision,
                       COUNT(*)::bigint,
                       COALESCE(SUM(d_fexp), 0)::double precision,
                       COALESCE(SUM(distance_m), 0)::double precision / 1000.0
                FROM {SCHEMA}.{detail_tab}
                WHERE {island_sql}
                  AND {touch}
                """,
                (building_id, building_id),
            )
            er = cur.fetchone()
            if er:
                emissions_g = float(er[0] or 0)
                trips = int(er[1] or 0)
                trips_weighted = float(er[2] or 0)
                distance_km = float(er[3] or 0)
        trips_is_imputed = False
        trips_is_capacity_share = False
        trips_assigned_weighted = trips_weighted if trips > 0 else 0.0
        if detail_tab and _od10_building_population_alloc_enabled():
            shared = _od10_building_capacity_share(
                cur,
                detail_tab=detail_tab,
                building_by=building_by,
                building_id=building_id,
                zone_geo_id=row[1],
            )
            if shared:
                if trips <= 0:
                    trips_is_imputed = True
                    trips_assigned_weighted = 0.0
                else:
                    trips_assigned_weighted = trips_weighted
                trips_weighted = float(shared["trips_weighted"] or 0)
                emissions_g = float(shared["total_emissions_g"] or 0)
                distance_km = float(shared["total_distance_km"] or 0)
                trips_is_capacity_share = True
        lat_v = row[10]
        lon_v = row[11]
        building = {
            "building_id": row[0],
            "zone_geo_id": row[1],
            "type": row[2],
            "use_class": row[3],
            "name": row[4],
            "address": _building_address_from_footprint_row(row),
            "csdname": row[6],
            "units": row[7],
            "floors": row[8],
            "sq_ft": row[9],
            "lat": float(lat_v) if lat_v is not None else None,
            "lon": float(lon_v) if lon_v is not None else None,
            "building_by": building_by,
            "trips_legs": trips,
            "trips_weighted": trips_weighted,
            "trips_assigned_weighted": trips_assigned_weighted,
            "trips_is_imputed": trips_is_imputed,
            "trips_is_capacity_share": trips_is_capacity_share,
            "total_emissions_g": emissions_g,
            "total_distance_km": round(distance_km, 2),
            "source": "od",
        }
        building = _od10_building_panel_row(building)
        feature = _geom_json_to_feature(
            row[12],
            {"building_id": building["building_id"], "zone_geo_id": building["zone_geo_id"]},
        )
        return jsonify({"building": building, "geojson": feature})
    finally:
        conn.close()


@app.route("/api/od/flows_zones")
def api_od10_flows_zones():
    """Fast zone list for od-flows.html (rules or dest choropleth, no polygons)."""
    zone_by = (request.args.get("zone_by", "rules") or "rules").strip().lower()
    if zone_by not in ("rules", "dest"):
        zone_by = "rules"
    with app.test_request_context(
        f"/api/od/zone_map?zone_by={zone_by}&min_kg=0&island_only=1&include_geojson=0",
        method="GET",
    ):
        return api_od10_zone_map()


@app.route("/api/od/zone_incoming_flow")
def api_od10_zone_incoming_flow():
    dest_id = (request.args.get("dest_geo_id", "") or "").strip()
    if not dest_id:
        return jsonify({"error": "dest_geo_id is required"}), 400
    zone_by = (request.args.get("zone_by", "rules") or "rules").strip().lower()
    if zone_by == "meeting":
        zone_by = "rules"
    if zone_by not in ("rules", "dest"):
        zone_by = "rules"
    limit = _parse_od10_flow_limit(request.args.get("limit"), default=10)

    conn = get_conn()
    try:
        cur = conn.cursor()
        flows_t = _od10_unified_flows_table(cur)
        if flows_t:
            trips_c, emis_c, dist_c = _od10_flow_metric_cols(zone_by)
            flow_filter = _od10_flows_nontrivial_sql(trips_c, emis_c)
            interzonal = _od10_interzonal_flow_filter_sql("f")
            params: list = [dest_id]
            limit_sql = ""
            if limit is not None:
                limit_sql = " LIMIT %s"
                params.append(limit)
            zg_orig = _zones_geom_join(cur, "f.orig_geo_id", "zg_o")
            zg_dest = _zones_geom_join(cur, "f.dest_geo_id", "zg_d")
            cur.execute(
                f"""
                SELECT f.orig_geo_id, {trips_c}, {emis_c}, {dist_c}, f.orig_lat, f.orig_lon
                FROM {SCHEMA}.{flows_t} f
                {zg_orig}
                {zg_dest}
                WHERE f.dest_geo_id = %s
                {interzonal}
                {flow_filter}
                ORDER BY {emis_c} DESC NULLS LAST, {trips_c} DESC, f.orig_geo_id
                {limit_sql}
                """,
                tuple(params),
            )
            frows = cur.fetchall()
            cur.execute(
                f"""
                SELECT SUM({trips_c}), SUM({emis_c}), SUM({dist_c}), COUNT(*)
                FROM {SCHEMA}.{flows_t} f
                {zg_orig}
                {zg_dest}
                WHERE f.dest_geo_id = %s
                {interzonal}
                {flow_filter}
                """,
                (dest_id,),
            )
            agg = cur.fetchone()
            dest_lat, dest_lon = _od10_dest_anchor_coords(cur, dest_id)
            dr_trips, dr_emis = _od10_zone_kpis(cur, zone_by, dest_id)
            trow = None
            if agg:
                trow = (agg[0], agg[1], agg[2], dest_lat, dest_lon, agg[3], dr_trips, dr_emis)
            payload = _od10_incoming_payload_from_rows(
                dest_id,
                frows=frows,
                trow=trow,
                limit=limit,
                flows_t=flows_t,
                zone_by=zone_by,
            )
            _od10_enrich_incoming_payload(cur, payload, zone_by)
            _od10_patch_incoming_flow_anchors(cur, payload)
            return jsonify(payload)

        top_t, tot_t = _od10_flows_tables(cur, zone_by)
        if not top_t or not tot_t:
            return jsonify({
                "error": "missing_table",
                "zone_by": zone_by,
                "message": (
                    f"OD10 incoming-flow table missing (zone_incoming_flows_{OD10_RUN_TAG}). "
                    "Restore data/db/od_dashboard_tables.dump into the od_dashboard database."
                ),
            }), 503
        rank_sql = ""
        rank_params: list = []
        if limit is not None:
            rank_sql = " AND rank <= %s"
            rank_params = [limit]
        zg_orig = _zones_geom_join(cur, "f.orig_geo_id", "zg_o")
        zg_dest = _zones_geom_join(cur, "f.dest_geo_id", "zg_d")
        cur.execute(
            f"""
            SELECT f.orig_geo_id, f.trips, f.total_emissions_g, f.total_distance_km, f.orig_lat, f.orig_lon
            FROM {SCHEMA}.{top_t} f
            {zg_orig}
            {zg_dest}
            WHERE f.dest_geo_id = %s{rank_sql}
            ORDER BY f.rank
            """,
            tuple([dest_id] + rank_params),
        )
        frows = cur.fetchall()
        has_tot_dist = _column_exists(cur, tot_t, "total_incoming_distance_km")
        dist_col = (
            "total_incoming_distance_km::double precision"
            if has_tot_dist
            else "NULL::double precision"
        )
        cur.execute(
            f"""
            SELECT total_incoming_trips, total_incoming_emissions_g, {dist_col},
                   dest_lat, dest_lon, origin_zone_count,
                   dest_rules_trips, dest_rules_emissions_g
            FROM {SCHEMA}.{tot_t} WHERE dest_geo_id = %s
            """,
            (dest_id,),
        )
        trow = cur.fetchone()
        payload = _od10_incoming_payload_from_rows(
            dest_id,
            frows=frows,
            trow=trow,
            limit=limit,
            flows_t=top_t,
            zone_by=zone_by,
        )
        _od10_enrich_incoming_payload(cur, payload, zone_by)
        _od10_patch_incoming_flow_anchors(cur, payload)
        return jsonify(payload)
    finally:
        conn.close()


@app.route("/api/od/zone_incoming_flows_all")
def api_od10_zone_incoming_flows_all():
    zone_by = (request.args.get("zone_by", "rules") or "rules").strip().lower()
    if zone_by == "meeting":
        zone_by = "rules"
    if zone_by not in ("rules", "dest"):
        zone_by = "rules"
    limit = _parse_od10_flow_limit(request.args.get("limit"), default=None)

    conn = get_conn()
    try:
        cur = conn.cursor()
        flows_t = _od10_unified_flows_table(cur)
        if flows_t:
            trips_c, emis_c, dist_c = _od10_flow_metric_cols(zone_by)
            flow_filter = _od10_flows_nontrivial_sql(trips_c, emis_c)
            interzonal = _od10_interzonal_flow_filter_sql("f")
            anchor_t = _resolve_od10_table(cur, OD10_ANCHOR_CANDIDATES)
            zone_t = _od10_zone_table(cur, zone_by)
            if anchor_t:
                anchor_select = "COALESCE(a.map_lat, a.dest_lat), COALESCE(a.map_lon, a.dest_lon)"
                anchor_join = f"LEFT JOIN {SCHEMA}.{anchor_t} a ON a.geo_id = r.dest_geo_id"
            else:
                anchor_select = "NULL::double precision, NULL::double precision"
                anchor_join = ""
            if zone_t:
                z_trips_c, z_emis_c = _od10_zone_kpi_cols(cur, zone_t, zone_by=zone_by)
                zone_select = f"z.{z_trips_c}, z.{z_emis_c}"
                zone_join = f"LEFT JOIN {SCHEMA}.{zone_t} z ON z.geo_id::text = r.dest_geo_id"
            else:
                zone_select = "NULL::double precision, NULL::double precision"
                zone_join = ""
            rank_filter = ""
            params: list = []
            if limit is not None:
                rank_filter = "WHERE r.rn <= %s"
                params.append(limit)
            zg_orig = _zones_geom_join(cur, "f.orig_geo_id", "zg_o")
            zg_dest = _zones_geom_join(cur, "f.dest_geo_id", "zg_d")
            zg_orig_r = _zones_geom_join(cur, "f.orig_geo_id", "zg_or")
            zg_dest_r = _zones_geom_join(cur, "f.dest_geo_id", "zg_dr")
            cur.execute(
                f"""
                WITH ranked AS (
                    SELECT f.dest_geo_id, f.orig_geo_id, f.orig_lat, f.orig_lon,
                           {trips_c} AS trips,
                           {emis_c} AS total_emissions_g,
                           {dist_c} AS total_distance_km,
                           ROW_NUMBER() OVER (
                               PARTITION BY f.dest_geo_id
                               ORDER BY {emis_c} DESC NULLS LAST, {trips_c} DESC, f.orig_geo_id
                           ) AS rn
                    FROM {SCHEMA}.{flows_t} f
                    {zg_orig}
                    {zg_dest}
                    WHERE 1=1
                    {interzonal}
                    {flow_filter}
                ),
                totals AS (
                    SELECT f.dest_geo_id,
                           SUM({trips_c})::double precision AS total_incoming_trips,
                           SUM({emis_c})::double precision AS total_incoming_emissions_g,
                           SUM({dist_c})::double precision AS total_incoming_distance_km,
                           COUNT(*)::bigint AS origin_zone_count
                    FROM {SCHEMA}.{flows_t} f
                    {zg_orig_r}
                    {zg_dest_r}
                    WHERE 1=1
                    {interzonal}
                    {flow_filter}
                    GROUP BY f.dest_geo_id
                )
                SELECT r.dest_geo_id, r.rn, r.orig_geo_id, r.trips,
                       r.total_emissions_g, r.total_distance_km, r.orig_lat, r.orig_lon,
                       t.total_incoming_trips, t.total_incoming_emissions_g,
                       t.total_incoming_distance_km, t.origin_zone_count,
                       {anchor_select},
                       {zone_select}
                FROM ranked r
                JOIN totals t ON t.dest_geo_id = r.dest_geo_id
                {anchor_join}
                {zone_join}
                {rank_filter}
                ORDER BY r.dest_geo_id, r.rn
                """,
                tuple(params),
            )
            zones: dict[str, dict] = {}
            for row in cur.fetchall():
                did = str(row[0])
                z = zones.get(did)
                if z is None:
                    inc_km = row[10]
                    z = {
                        "dest_geo_id": did,
                        "dest_zone_code": _zone_code_for(did),
                        "zone_by": zone_by,
                        "source": "od",
                        "dest_lat": _flow_coord(row[12]),
                        "dest_lon": _flow_coord(row[13]),
                        "total_incoming_trips": float(row[8] or 0),
                        "total_incoming_emissions_g": float(row[9] or 0),
                        "total_incoming_distance_km": round(float(inc_km or 0), 2),
                        "origin_zone_count": int(row[11] or 0),
                        "dest_rules_trips": float(row[14] or 0) if row[14] is not None else None,
                        "dest_rules_emissions_g": float(row[15] or 0) if row[15] is not None else None,
                        "flows": [],
                    }
                    zones[did] = z
                z["flows"].append(_attach_zone_code({
                    "orig_geo_id": str(row[2]),
                    "trips": float(row[3] or 0),
                    "total_emissions_g": float(row[4] or 0),
                    "total_distance_km": float(row[5] or 0),
                    "orig_lat": _flow_coord(row[6]),
                    "orig_lon": _flow_coord(row[7]),
                    "dest_lat": _flow_coord(row[12]),
                    "dest_lon": _flow_coord(row[13]),
                    "dest_zone_code": _zone_code_for(did),
                }, geo_id_key="orig_geo_id"))
            for z in zones.values():
                z["flow_count"] = len(z["flows"])
                z["flows_shown_trips"] = sum(f["trips"] for f in z["flows"])
                _od10_enrich_incoming_payload(cur, z, zone_by)
            _od10_patch_flows_all_anchors(cur, zones)
            return jsonify({
                "supported": True,
                "zone_by": zone_by,
                "source": "od",
                "limit": limit if limit is not None else "all",
                "precomputed": True,
                "zone_count": len(zones),
                "zones": zones,
            })

        top_t, tot_t = _od10_flows_tables(cur, zone_by)
        if not top_t or not tot_t:
            return jsonify({
                "supported": False,
                "reason": "missing_precompute",
                "zone_by": zone_by,
                "message": (
                    f"Missing zone_incoming_flows_{OD10_RUN_TAG}. "
                    "Restore data/db/od_dashboard_tables.dump into the od_dashboard database."
                ),
                "zones": {},
            })
        has_tot_dist = _column_exists(cur, tot_t, "total_incoming_distance_km")
        dist_col = (
            "z.total_incoming_distance_km::double precision"
            if has_tot_dist
            else "NULL::double precision"
        )
        rank_sql, rank_params = _od10_flow_rank_filter(limit)
        zg_orig = _zones_geom_join(cur, "f.orig_geo_id", "zg_o")
        zg_dest = _zones_geom_join(cur, "f.dest_geo_id", "zg_d")
        cur.execute(
            f"""
            SELECT f.dest_geo_id, f.rank, f.orig_geo_id, f.trips,
                   f.total_emissions_g, f.total_distance_km, f.orig_lat, f.orig_lon,
                   z.total_incoming_trips, z.total_incoming_emissions_g, {dist_col},
                   z.origin_zone_count, z.dest_lat, z.dest_lon,
                   z.dest_rules_trips, z.dest_rules_emissions_g
            FROM {SCHEMA}.{top_t} f
            JOIN {SCHEMA}.{tot_t} z ON z.dest_geo_id = f.dest_geo_id
            {zg_orig}
            {zg_dest}
            WHERE 1=1{rank_sql}
            ORDER BY f.dest_geo_id, f.rank
            """,
            tuple(rank_params),
        )
        zones: dict[str, dict] = {}
        for row in cur.fetchall():
            did = str(row[0])
            z = zones.get(did)
            if z is None:
                inc_km = row[10]
                z = {
                    "dest_geo_id": did,
                    "dest_zone_code": _zone_code_for(did),
                    "zone_by": zone_by,
                    "source": "od",
                    "dest_lat": _flow_coord(row[12]),
                    "dest_lon": _flow_coord(row[13]),
                    "total_incoming_trips": float(row[8] or 0),
                    "total_incoming_emissions_g": float(row[9] or 0),
                    "total_incoming_distance_km": round(float(inc_km or 0), 2),
                    "origin_zone_count": int(row[11] or 0),
                    "dest_rules_trips": float(row[14] or 0) if row[14] is not None else None,
                    "dest_rules_emissions_g": float(row[15] or 0) if row[15] is not None else None,
                    "flows": [],
                }
                zones[did] = z
            z["flows"].append(_attach_zone_code({
                "orig_geo_id": str(row[2]),
                "trips": float(row[3] or 0),
                "total_emissions_g": float(row[4] or 0),
                "total_distance_km": float(row[5] or 0),
                "orig_lat": _flow_coord(row[6]),
                "orig_lon": _flow_coord(row[7]),
                "dest_lat": _flow_coord(row[12]),
                "dest_lon": _flow_coord(row[13]),
                "dest_zone_code": _zone_code_for(did),
            }, geo_id_key="orig_geo_id"))
        for z in zones.values():
            z["flow_count"] = len(z["flows"])
            z["flows_shown_trips"] = sum(f["trips"] for f in z["flows"])
        _od10_patch_flows_all_anchors(cur, zones)
        return jsonify({
            "supported": True,
            "zone_by": zone_by,
            "source": "od",
            "limit": limit if limit is not None else "all",
            "precomputed": True,
            "zone_count": len(zones),
            "zones": zones,
        })
    finally:
        conn.close()


def _deploy_cfg_dict() -> dict:
    up = DEPLOY["url_prefix"]
    ap = DEPLOY["api_prefix"]
    api_base = f"{up}{ap}" if up or ap else "/api"
    return {
        "urlPrefix": up,
        "apiPrefix": ap,
        "apiBase": api_base,
        "showBoundaryButton": DEPLOY["show_boundary_button"],
        "offline": DEPLOY["offline"],
    }


def _deploy_inline_script() -> str:
    """Inject before dashboard-config.js so deploy flags work even when assets are static."""
    cfg = json.dumps(_deploy_cfg_dict(), separators=(",", ":"))
    return f"<script>window.__dashDeploy={cfg};</script>\n  "


def _rewrite_html_for_offline(html: str) -> str:
    """Swap CDN assets for bundled vendor files; drop Google Fonts."""
    html = re.sub(r"\s*<link[^>]*fonts\.(googleapis|gstatic)\.com[^>]*>\s*", "\n", html, flags=re.I)
    replacements = (
        ("https://unpkg.com/leaflet@1.9.4/dist/leaflet.css", "assets/vendor/leaflet.css"),
        ("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js", "assets/vendor/leaflet.js"),
        (
            "https://cdn.jsdelivr.net/npm/leaflet.heat@0.2.0/dist/leaflet-heat.js",
            "assets/vendor/leaflet-heat.js",
        ),
        (
            "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js",
            "assets/vendor/chart.umd.min.js",
        ),
        (
            "https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0",
            "assets/vendor/chartjs-plugin-datalabels.min.js",
        ),
    )
    for old, new in replacements:
        html = html.replace(old, new)
    if "dashboard-offline.css" not in html:
        html = html.replace(
            "<head>",
            '<head>\n  <link rel="stylesheet" href="assets/dashboard-offline.css" />',
            1,
        )
    return html


def _serve_html_page(filename: str):
    """Serve dashboard HTML with runtime deploy settings inlined (NGCI / subpath safe)."""
    path = Path(app.static_folder) / filename
    if not path.is_file():
        return f"<p>{filename} not found.</p>", 404
    html = path.read_text(encoding="utf-8")
    if DEPLOY["offline"]:
        html = _rewrite_html_for_offline(html)
    inject = _deploy_inline_script()
    marker = '<script src="assets/dashboard-config.js'
    if marker in html and "__dashDeploy" not in html:
        html = html.replace(marker, inject + marker, 1)
    resp = Response(html, mimetype="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


def _serve_od_dashboard():
    return _serve_html_page("od-dashboard.html")


def _serve_od_html():
    return _serve_html_page("od.html")


def _redirect_legacy_od_page(new_path: str):
    qs = request.query_string.decode()
    prefix = DEPLOY["url_prefix"]
    path = f"{prefix}{new_path}" if prefix else new_path
    return redirect(path + (f"?{qs}" if qs else ""), code=301)


@app.route("/")
@app.route("/od")
@app.route("/od-dashboard")
@app.route("/od-dashboard.html")
def od_dashboard_page():
    return _serve_od_dashboard()


@app.route("/od.html")
def od_page():
    return _serve_od_html()


@app.route("/od-flows")
@app.route("/od-flows.html")
def od_flows_page():
    return _serve_html_page("od-flows.html")


@app.route("/od-buildings")
@app.route("/od-buildings.html")
def od_buildings_page():
    return _serve_html_page("od-buildings.html")


@app.route("/od-zones-boundary")
@app.route("/od-zones-boundary.html")
def od_zones_boundary_page():
    if not DEPLOY["show_boundary_button"]:
        prefix = DEPLOY["url_prefix"] or ""
        return redirect(f"{prefix}/" if prefix else "/", code=302)
    return _serve_html_page("od-zones-boundary.html")


@app.route("/od10")
@app.route("/od10-dashboard")
@app.route("/od10-dashboard.html")
def legacy_od10_dashboard_page():
    return _redirect_legacy_od_page("/")


@app.route("/od10.html")
def legacy_od10_page():
    return _redirect_legacy_od_page("/od.html")


@app.route("/od10-flows")
@app.route("/od10-flows.html")
def legacy_od10_flows_page():
    return _redirect_legacy_od_page("/od-flows.html")


@app.route("/od10-buildings")
@app.route("/od10-buildings.html")
def legacy_od10_buildings_page():
    return _redirect_legacy_od_page("/od-buildings.html")


@app.route("/od10-zones-boundary")
@app.route("/od10-zones-boundary.html")
def legacy_od10_zones_boundary_page():
    return _redirect_legacy_od_page("/od-zones-boundary.html")


@app.route("/assets/dashboard-config.js")
def dashboard_config_js():
    cfg = _deploy_cfg_dict()
    static_cfg = Path(app.static_folder) / "assets" / "dashboard-config.js"
    helpers = ""
    if static_cfg.is_file():
        helpers = static_cfg.read_text(encoding="utf-8")
    deploy = (
        f"(function(){{var c={json.dumps(cfg)};"
        "if(window.DashApplyDeploy){DashApplyDeploy(c);}else{window.DashConfig=c;}})();"
    )
    body = helpers + "\n" + deploy
    resp = Response(body, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.route("/<path:path>")
def static_file(path):
    if path == "assets/dashboard-config.js":
        return dashboard_config_js()
    resp = send_from_directory(app.static_folder, path)
    low = path.lower()
    if low.endswith((".html", ".gif", ".jpg", ".jpeg", ".png", ".js", ".css")):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


if __name__ == "__main__":
    _repo_root = Path(__file__).resolve().parent.parent
    _load_default_deploy_env(_repo_root)

    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--env-file", default=os.environ.get("DASH_ENV_FILE", ""))
    _pre.add_argument("--bundle-root", default=os.environ.get("POPGEN_BUNDLE_ROOT"))
    _pre_args, _ = _pre.parse_known_args()
    if _pre_args.env_file:
        _load_deploy_env(Path(_pre_args.env_file).expanduser().resolve(), override=True)
    _bundle_hint = _resolve_bundle_root(_pre_args.bundle_root) if _pre_args.bundle_root else _resolve_bundle_root()
    if _bundle_hint and _bundle_hint != _repo_root:
        _load_default_deploy_env(_bundle_hint)

    ap = argparse.ArgumentParser(description="PopGen emissions dashboard API server")
    ap.add_argument(
        "--env-file",
        default=_pre_args.env_file or "",
        help="Optional deploy.env file (KEY=VALUE). Also auto-loads deploy.env or .env from bundle root.",
    )
    ap.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    ap.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "5051")),
        help="HTTP port (default: 5051, or PORT env)",
    )
    ap.add_argument("--db-host", default=os.environ.get("PGHOST"), help="PostgreSQL host")
    ap.add_argument("--db-port", default=os.environ.get("PGPORT"), help="PostgreSQL port")
    ap.add_argument("--db-name", default=os.environ.get("PGDATABASE", "od_dashboard"), help="PostgreSQL database name")
    ap.add_argument("--db-user", default=os.environ.get("PGUSER"), help="PostgreSQL user")
    ap.add_argument("--db-password", default=os.environ.get("PGPASSWORD"), help="PostgreSQL password")
    ap.add_argument(
        "--db-schema",
        default=os.environ.get("PGSCHEMA", "public"),
        help="PostgreSQL schema for dashboard tables (default: public, or PGSCHEMA env)",
    )
    ap.add_argument(
        "--bundle-root",
        default=os.environ.get("POPGEN_BUNDLE_ROOT"),
        help="Portable bundle folder (contains dashboard/, data/, scripts/). Auto-detected when manifest.json is present.",
    )
    ap.add_argument(
        "--url-prefix",
        default=os.environ.get("DASH_URL_PREFIX", ""),
        help="URL mount prefix behind a reverse proxy (e.g. /montreal-traffic-emissions-dashboard)",
    )
    ap.add_argument(
        "--api-prefix",
        default=os.environ.get("DASH_API_PREFIX", "/api"),
        help="API route prefix (default: /api; use /od-dashboard-api on shared hosts)",
    )
    ap.add_argument(
        "--show-boundary-button",
        default=_str_to_bool(os.environ.get("DASH_SHOW_BOUNDARY_BUTTON", "true")),
        type=_str_to_bool,
        metavar="BOOL",
        help="Show Boundaries nav link (default: true). Pass false to hide.",
    )
    ap.add_argument(
        "--offline",
        default=_str_to_bool(os.environ.get("DASH_OFFLINE", "false")),
        type=_str_to_bool,
        metavar="BOOL",
        help="Offline mode: bundled JS/CSS, no CDN fonts or map tiles (default: false).",
    )
    args = ap.parse_args()
    if args.bundle_root:
        bundle_root = _resolve_bundle_root(str(args.bundle_root))
        if bundle_root:
            dash_dir = _apply_bundle_layout(bundle_root)
            app.static_folder = str(dash_dir)
            print(f"Bundle mode: {bundle_root}", flush=True)
    configure_deployment(
        url_prefix=args.url_prefix,
        api_prefix=args.api_prefix,
        show_boundary_button=args.show_boundary_button,
        offline=args.offline,
    )
    _apply_db_cli(
        db_host=args.db_host,
        db_port=args.db_port,
        db_name=args.db_name,
        db_user=args.db_user,
        db_password=args.db_password,
        db_schema=args.db_schema,
    )
    try:
        _zc = get_conn()
        try:
            _cur = _zc.cursor()
            ensure_zones_geom_compat(_cur)
            _zc.commit()
            _zt = _zones_geom_table(_cur)
            if _zt:
                print(f"  Zone geometry table: {SCHEMA}.{_zt}", flush=True)
        finally:
            _zc.close()
    except Exception as _ze:
        print(f"  Zone geometry check skipped: {_ze}", flush=True)
    up = DEPLOY["url_prefix"]
    ap = DEPLOY["api_prefix"]
    base = f"http://127.0.0.1:{args.port}{up or ''}"
    print(f"Dashboard: {base}/")
    print(f"  API health: {base}{ap}/health")
    print(f"  URL prefix: {up or '(root)'}")
    print(f"  API prefix: {ap}")
    print(f"  Boundaries nav: {'on' if DEPLOY['show_boundary_button'] else 'off'}")
    print(f"  Offline mode: {'on' if DEPLOY['offline'] else 'off'}")
    print(
        f"  DB: {DB_PARAMS.get('user', '(default)')}@{DB_PARAMS.get('host', 'localhost')}:"
        f"{DB_PARAMS.get('port', '5432')}/{DB_PARAMS.get('dbname', 'od_dashboard')} schema={SCHEMA}"
    )
    print(f"  Buildings: {base}/od-buildings.html")
    print(f"  Flows: {base}/od-flows.html")
    if DEPLOY["show_boundary_button"]:
        print(f"  Boundaries: {base}/od-zones-boundary.html")
    app.run(host=args.host, port=args.port, debug=False)
