"""
PopGen dashboard API: KPIs, category charts, zone map, incoming and aggregate OD flows.

Table discovery (cached): building / micro / routes_only families.
Routes-only uses trip_routes_otp_a* or trip_routes_building* + route_emissions_g; map prefers
zone_emissions_route_assignment* or zone_emissions_rules* (legacy zone_emissions_meeting*);
when route_attributed_geo_id exists, /api/zone_map aggregates by that rules-attributed zone.
(Montreal island filter on /api/zone_map by default: uses tight Data/mtl_boundary_file.geojson for SQL;
padded buffer is not used for filtering because it overlaps Laval / South Shore. Choropleth polygons
are clipped to that outline when routes_only + island_only. Map outline API may still serve padded.)
/api/zone_map returns {"zones":[...], "geojson": FeatureCollection|null}
with polygons from popgen_zones_geom when available.

Env: DASHBOARD_EMISSIONS_TABLE, DASHBOARD_TRIPS_TABLE, DASHBOARD_ROUTES_TABLE, DASHBOARD_ZONE_TABLE,
     DASHBOARD_ZONE_EMISSIONS_ROUTE_TABLE, DASHBOARD_PURPOSE_ENRICHMENT_TABLE,
     DASHBOARD_FAMILY=routes_only (skip trip_emissions* and use trip_routes_* + route_emissions_g only).
     DASHBOARD_METRICS=weighted (default) | legs — weighted uses sum(leg_weight) on trips join
     (PM23-comparable); legs uses COUNT(*) of routed car legs.

Precompute zone totals for fast /api/zone_map (rules + destination tables):
  python scripts/preprocess_dashboard_zone_emissions.py --run-tag 100pct_ct
  # -> zone_emissions_rules_<tag>, zone_emissions_dest_<tag>,
  #    building_emissions_rules_<tag>, building_emissions_dest_<tag>

Routes-only /api/building_map: ``building_by=rules`` (default) or ``building_by=dest``.

Precompute inter-zonal OD pairs for fast /api/zone_incoming_flow and /api/od_flows:
  python scripts/build_od_flow_summary.py --routes-table trip_routes_building_100pct_ct
  # -> od_flows_route_assignment_100pct_ct (indexed on dest_geo_id)

/api/flows_bootstrap — rules zone_map + optional incoming flows in one request (flows page).

Routes-only /api/zone_map: ``zone_by=dest`` for destination rollup; default uses rules tables.
``zone_by=meeting`` is accepted as a legacy alias for rules.

Routes-only /api/building_map: ``building_by=dest`` for destination building rollup;
default uses rules (``route_attributed_building_id``). Requires precomputed
``building_emissions_*_<tag>`` tables from preprocess_dashboard_zone_emissions.py.

/api/by_purpose_motif — car legs with emissions, grouped by parallel trip_leg_purpose_enrichment* join on route leg keys.

/api/bootstrap — stats + by_category + by_purpose_motif in one JSON (one DB connection; faster first paint).

Run: python scripts/dashboard_server.py — open http://127.0.0.1:5055/
  (Default port 5055 avoids pgAdmin 4, which binds 127.0.0.1:5050 and returns 401.)
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2
from flask import Flask, jsonify, request, send_file, send_from_directory

from meeting_emissions_attribution import (  # noqa: E402
    motif_travel_reason_sql,
    routes_building_dest_aggregate_sql,
    routes_building_rules_aggregate_sql,
)
from popgen_constants import BUILDINGS_TABLE  # noqa: E402

DB_PARAMS: dict[str, str] = {"dbname": os.environ.get("PGDATABASE", "od_dashboard")}
for _pg_key, _pg_env in (
    ("host", "PGHOST"),
    ("port", "PGPORT"),
    ("user", "PGUSER"),
    ("password", "PGPASSWORD"),
):
    _pg_val = os.environ.get(_pg_env, "").strip()
    if _pg_val:
        DB_PARAMS[_pg_key] = _pg_val
SCHEMA = "public"
_ZONES_CSV = Path(__file__).resolve().parent.parent / "Data" / "popgen_inputs" / "zones.csv"
_GEO_SP23_CSV = Path(__file__).resolve().parent.parent / "Data" / "popgen_inputs" / "geo_zone_sp23.csv"
_ZONE_CODE_BY_GEO: dict[str, str] | None = None
_ZONE_GEO_BY_CODE: dict[str, str] | None = None
_ZONE_NAME_BY_GEO: dict[str, str] | None = None
ZONE_SHORT_PREFIX = "mtl"
CAR_MODE_GROUPS = "('1','1.0','10','11','10.0','11.0')"


def _zone_code_index() -> dict[str, str]:
    """PopGen geo_id -> census-tract zone_code (SR key from zones.csv)."""
    global _ZONE_CODE_BY_GEO, _ZONE_GEO_BY_CODE
    if _ZONE_CODE_BY_GEO is None:
        by_geo: dict[str, str] = {}
        by_code: dict[str, str] = {}
        if _ZONES_CSV.exists():
            import csv

            with _ZONES_CSV.open(encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    gid = str(row.get("geo_id", "")).strip()
                    zc = str(row.get("zone_code", "")).strip()
                    if not gid or not zc:
                        continue
                    by_geo[gid] = zc
                    by_code[zc] = gid
                    base = zc.split(".", 1)[0]
                    if base and base not in by_code:
                        by_code[base] = gid
        _ZONE_CODE_BY_GEO = by_geo
        _ZONE_GEO_BY_CODE = by_code
    return _ZONE_CODE_BY_GEO


def _zone_code_for(geo_id) -> str | None:
    if geo_id is None:
        return None
    return _zone_code_index().get(str(geo_id).strip())


def _zone_name_index() -> dict[str, str]:
    """PopGen geo_id -> ARTM zone label (nomsp from geo_zone_sp23.csv)."""
    global _ZONE_NAME_BY_GEO
    if _ZONE_NAME_BY_GEO is None:
        by_geo: dict[str, str] = {}
        if _GEO_SP23_CSV.exists():
            import csv

            with _GEO_SP23_CSV.open(encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    gid = str(row.get("geo_id", "")).strip()
                    nom = str(row.get("nomsp", "")).strip()
                    if gid and nom:
                        by_geo[gid] = nom
        _ZONE_NAME_BY_GEO = by_geo
    return _ZONE_NAME_BY_GEO


def _zone_name_for(geo_id) -> str | None:
    if geo_id is None:
        return None
    return _zone_name_index().get(str(geo_id).strip())


def _extract_zone_short_name(nomsp: str) -> str:
    """Borough / locality label from nomsp, e.g. 'Ville-Marie'."""
    import re

    t = str(nomsp or "").strip()
    if not t:
        return ""
    if ":" in t:
        t = t.split(":", 1)[1].strip()
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t).strip()
    if ";" in t:
        t = t.split(";", 1)[0].strip()
    return t


def _zone_short_name_for(geo_id) -> str | None:
    nom = _zone_name_for(geo_id)
    if not nom:
        return None
    short = _extract_zone_short_name(nom)
    return short or None


def _short_zone_id(geo_id) -> str | None:
    if geo_id is None:
        return None
    g = str(geo_id).strip()
    if not g:
        return None
    return f"{ZONE_SHORT_PREFIX}+{g}"


def _resolve_geo_id_query(raw: str) -> str | None:
    """Resolve search input: geo_id, mtl+id, or census-tract zone_code -> geo_id string."""
    q = str(raw or "").strip()
    if not q:
        return None
    q_low = q.lower()
    if q_low.startswith(f"{ZONE_SHORT_PREFIX}+"):
        rest = q[len(ZONE_SHORT_PREFIX) + 1 :].strip()
        if rest.isdigit():
            return rest
    if q.isdigit():
        return q
    idx = _zone_code_index()
    if q in idx:
        return q
    rev = _ZONE_GEO_BY_CODE or {}
    if q in rev:
        return rev[q]
    q_norm = q if "." in q else f"{q}.00"
    if q_norm in rev:
        return rev[q_norm]
    q_low = q.lower()
    name_id = re.match(r"^(.+?)\s+(\d+)$", q)
    if name_id:
        name_part, id_part = name_id.group(1).strip().lower(), name_id.group(2)
        short_for_id = (_zone_short_name_for(id_part) or "").lower()
        if short_for_id and short_for_id == name_part:
            return id_part
        if id_part.isdigit():
            return id_part
    for gid, nom in _zone_name_index().items():
        if _extract_zone_short_name(nom).lower() == q_low:
            return gid
    return None


def _attach_zone_code(row: dict, *, geo_id_key: str = "geo_id") -> dict:
    gid = row.get(geo_id_key)
    if gid is None:
        return row
    g = str(gid).strip()
    prefix = ""
    if geo_id_key != "geo_id" and geo_id_key.endswith("_geo_id"):
        prefix = geo_id_key[: -len("_geo_id")] + "_"
    zc = _zone_code_for(g)
    if zc:
        row["zone_code"] = zc
        if prefix:
            row[f"{prefix}zone_code"] = zc
    zn = _zone_name_for(g)
    if zn:
        row["zone_name"] = zn
        if prefix:
            row[f"{prefix}zone_name"] = zn
        zsn = _extract_zone_short_name(zn)
        if zsn:
            row["zone_short_name"] = zsn
            if prefix:
                row[f"{prefix}zone_short_name"] = zsn
    sid = _short_zone_id(g)
    if sid:
        row["short_id"] = sid
        if prefix:
            row[f"{prefix}short_id"] = sid
    return row
_RESOLVED: dict | None = None
REPO_DATA_DIR = Path(__file__).resolve().parent.parent / "Data"
MOTIF18_LABELS = {
    "1": "Work",
    "2": "Work+",
    "3": "School",
    "4": "Edu",
    "5": "Shop",
    "6": "Personal",
    "7": "Leis.",
    "8": "Visit",
    "9": "Health",
    "10": "Pick-up / drop-off",
    "11": "Home",
    "12": "Other",
    "13": "Other",
    "14": "Other",
}

MONTREAL_BOUNDS = [[45.35, -73.95], [45.75, -73.35]]
# Fallback map fit when zone extent cannot be read from PostGIS (full CMM study area).
CMM_BOUNDS = [[45.25, -74.15], [45.95, -73.05]]
MONTREAL_ISLAND_LAT_MIN, MONTREAL_ISLAND_LAT_MAX = 45.41, 45.58
MONTREAL_ISLAND_LON_MIN, MONTREAL_ISLAND_LON_MAX = -73.78, -73.50

# Cached GeoJSON geometry for /api/zone_map island filter (tight shoreline — NOT padded buffer).
_ISLAND_FILTER_GEOM_JSON: str | None = None


def _motif18_label_case_sql(expr: str) -> str:
    cases = " ".join(
        f"WHEN '{code}' THEN '{label.replace(chr(39), chr(39) + chr(39))}'"
        for code, label in MOTIF18_LABELS.items()
    )
    return (
        "CASE regexp_replace(trim(COALESCE("
        + expr
        + "::text, '')), '\\.0$', '') "
        + cases
        + " ELSE COALESCE(NULLIF(trim("
        + expr
        + "::text), ''), '(join missing)') END"
    )


def _montreal_island_geometry_geojson_for_postgis() -> str | None:
    """
    GeoJSON geometry for SQL island filter: use the **unpadded** île outline first.

    `mtl_boundary_file_padded.geojson` is the same polygon buffered ~1200 m outward (see
    buffer_mtl_boundary.py); ST_Intersects against it wrongly includes Laval / South Shore zones.
    The map outline API still prefers padded for a softer shoreline; filtering uses tight landmass.
    """
    global _ISLAND_FILTER_GEOM_JSON
    if _ISLAND_FILTER_GEOM_JSON is not None:
        return _ISLAND_FILTER_GEOM_JSON or None
    for fname in ("mtl_boundary_file.geojson", "mtl_boundary_file_padded.geojson"):
        path = REPO_DATA_DIR / fname
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        feats = raw.get("features") or []
        geoms: list[dict] = []
        for ft in feats:
            g = ft.get("geometry")
            if not g:
                continue
            gt = g.get("type")
            if gt in ("Polygon", "MultiPolygon"):
                geoms.append(g)
        if not geoms:
            continue
        if len(geoms) == 1:
            _ISLAND_FILTER_GEOM_JSON = json.dumps(geoms[0], separators=(",", ":"))
        else:
            _ISLAND_FILTER_GEOM_JSON = json.dumps(
                {"type": "GeometryCollection", "geometries": geoms},
                separators=(",", ":"),
            )
        return _ISLAND_FILTER_GEOM_JSON
    _ISLAND_FILTER_GEOM_JSON = ""
    return None


def _request_island_only(*, default: bool = True) -> bool:
    raw = request.args.get("island_only", "1" if default else "0")
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _dashboard_metrics_weighted() -> bool:
    """Primary KPI fields use sum(leg_weight) when trips.leg_weight exists (PM23-comparable)."""
    v = (os.environ.get("DASHBOARD_METRICS", "weighted") or "weighted").strip().lower()
    return v not in ("legs", "route", "unweighted", "0", "false", "no", "off")


def _trips_has_leg_weight(cur, trips_table: str | None) -> bool:
    tt = str(trips_table or "").strip()
    return bool(tt) and _table_exists(cur, tt) and _column_exists(cur, tt, "leg_weight")


def _pick_trips_table_for_weight(
    cur,
    routes_table: str | None,
    trips_table: str | None,
) -> str | None:
    """Resolve a trips table that carries leg_weight (building table often omits it)."""
    candidates: list[str] = []
    for c in (trips_table, os.environ.get("DASHBOARD_TRIPS_TABLE", "").strip()):
        if c:
            candidates.append(str(c).strip())
    rt = str(routes_table or "").strip()
    suffix = ""
    for prefix in ("trip_routes_building", "trip_routes_otp_a"):
        if rt.startswith(prefix):
            suffix = rt[len(prefix):]
            break
    candidates.extend(
        [
            f"popgen_trip_building{suffix}",
            f"popgen_trip_micro{suffix}",
            "popgen_trip_micro",
            "popgen_trip_building",
        ]
    )
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        if _trips_has_leg_weight(cur, cand):
            return cand
    return None


def _sql_routes_join_trips_table(r_alias: str, t_alias: str, trips_table: str) -> str:
    """Join trip_routes_* to popgen_trip_* on the standard leg key."""
    tt = str(trips_table).strip()
    return f"""
LEFT JOIN {SCHEMA}.{tt} {t_alias}
  ON {t_alias}.synthetic_person_id::text = {r_alias}.synthetic_person_id::text
 AND {t_alias}.orig_geo_id::int = {r_alias}.orig_geo_id::int
 AND {t_alias}.dest_geo_id::int = {r_alias}.dest_geo_id::int
 AND regexp_replace(trim(COALESCE({t_alias}.purpose::text, '')), '\\.0$', '') =
     regexp_replace(trim(COALESCE({r_alias}.purpose::text, '')), '\\.0$', '')
 AND (
   CASE WHEN trim(COALESCE({t_alias}.dep_time_bin::text, '')) = '' THEN NULL::numeric
        ELSE trim({t_alias}.dep_time_bin::text)::numeric
   END
 ) IS NOT DISTINCT FROM (
   CASE WHEN trim(COALESCE({r_alias}.dep_time_bin::text, '')) = '' THEN NULL::numeric
        ELSE trim({r_alias}.dep_time_bin::text)::numeric
   END
 )
 AND ({t_alias}.hh_id IS NULL OR {r_alias}.hh_id IS NULL
      OR {t_alias}.hh_id::text IS NOT DISTINCT FROM {r_alias}.hh_id::text)
"""


def _trip_weight_expr(t_alias: str = "t") -> str:
    return f"COALESCE({t_alias}.leg_weight, 1)::double precision"


def _promote_weighted_stats(row: dict) -> dict:
    """Expose weighted totals as primary trips/emissions/distance when configured."""
    if not row.get("metrics_weighted"):
        return row
    tw = row.get("trips_weighted")
    ew = row.get("total_emissions_g_weighted")
    dw = row.get("total_distance_km_weighted")
    if tw is None and ew is None and dw is None:
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
        row["total_emissions_tonnes"] = round(float(ew) / 1e6, 2)
    if dw is not None:
        row["total_distance_km"] = round(float(dw), 2)
    trips = float(row.get("trips") or 0)
    emis = float(row.get("total_emissions_g") or 0)
    if trips > 0 and emis >= 0:
        row["avg_emissions_g_per_trip"] = round(emis / trips, 2)
    return row


def _finish_stats_payload(
    *,
    trips_legs: int,
    total_emissions_g_legs: float,
    total_distance_km_legs: float,
    trips_weighted: float | None = None,
    total_emissions_g_weighted: float | None = None,
    total_distance_km_weighted: float | None = None,
    metrics_weighted: bool = False,
) -> dict:
    row = {
        "trips": trips_legs,
        "total_emissions_g": total_emissions_g_legs,
        "total_emissions_tonnes": round(total_emissions_g_legs / 1e6, 2),
        "total_distance_km": round(total_distance_km_legs, 2),
        "avg_emissions_g_per_trip": round(total_emissions_g_legs / trips_legs, 2) if trips_legs else 0,
        "metrics_weighted": metrics_weighted,
    }
    if metrics_weighted and trips_weighted is not None:
        row["trips_legs"] = trips_legs
        row["trips_weighted"] = float(trips_weighted)
        row["total_emissions_g_legs"] = total_emissions_g_legs
        row["total_emissions_g_weighted"] = float(total_emissions_g_weighted or 0)
        row["total_distance_km_legs"] = round(total_distance_km_legs, 2)
        row["total_distance_km_weighted"] = float(total_distance_km_weighted or 0)
        return _promote_weighted_stats(row)
    return row


def _routes_only_island_cte_and_predicates(island_on: bool) -> tuple[str, str, str, str, str, str, tuple]:
    """
    island_only: true île outline from GeoJSON (unpadded file preferred — padded is outward buffer).
    Returns (cte_prefix, ic, ig, i_ll, i_havg, i_route, extra_params). extra_params is () or (geojson,).
    """
    if not island_on:
        return "", "", "", "", "", "", ()
    gj = _montreal_island_geometry_geojson_for_postgis()
    if gj:
        cte = """WITH island AS (
            SELECT ST_MakeValid(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s)::geometry, 4326))) AS ig
        )
        """
        ic = """
              AND EXISTS (
                  SELECT 1 FROM island ix
                  WHERE ST_Intersects(
                      ix.ig,
                      ST_SetSRID(ST_MakePoint(c.lon::double precision, c.lat::double precision), 4326)
                  )
              )
        """
        ig = """
              AND EXISTS (
                  SELECT 1 FROM island ix
                  WHERE ST_Intersects(ix.ig, ST_MakeValid(ST_Force2D(g.geom::geometry)))
              )
        """
        i_ll = """
              AND EXISTS (
                  SELECT 1 FROM island ix
                  WHERE ST_Intersects(
                      ix.ig,
                      ST_SetSRID(ST_MakePoint(ll.lon::double precision, ll.lat::double precision), 4326)
                  )
              )
        """
        i_havg = """
              AND EXISTS (
                  SELECT 1 FROM island ix
                  WHERE ST_Intersects(
                      ix.ig,
                      ST_SetSRID(
                          ST_MakePoint(AVG(r.dest_lon)::double precision, AVG(r.dest_lat)::double precision),
                          4326
                      )
                  )
              )
        """
        # Per-route island filter: bbox (fast on 9M rows). Zone choropleth uses ic/ig (ST on centroids).
        i_route = f"""
              AND r.dest_lat IS NOT NULL AND r.dest_lon IS NOT NULL
              AND r.dest_lat::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
              AND r.dest_lon::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX}
        """
        return cte, ic, ig, i_ll, i_havg, i_route, (gj,)
    ic = f"""
              AND c.lat::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
              AND c.lon::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX}
        """
    ig = f"""
              AND ST_Contains(
                  ST_SetSRID(ST_MakeEnvelope(
                      {MONTREAL_ISLAND_LON_MIN}, {MONTREAL_ISLAND_LAT_MIN},
                      {MONTREAL_ISLAND_LON_MAX}, {MONTREAL_ISLAND_LAT_MAX}), 4326),
                  ST_Centroid(g.geom::geometry))
        """
    i_ll = f"""
              AND ll.lat::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
              AND ll.lon::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX}
        """
    i_havg = f"""
              AND AVG(r.dest_lat)::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
              AND AVG(r.dest_lon)::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX}
        """
    i_route = f"""
              AND r.dest_lat IS NOT NULL AND r.dest_lon IS NOT NULL
              AND r.dest_lat::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
              AND r.dest_lon::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX}
        """
    return "", ic, ig, i_ll, i_havg, i_route, ()


def _table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s LIMIT 1
        """,
        (SCHEMA, table_name),
    )
    return cur.fetchone() is not None


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = %s
        LIMIT 1
        """,
        (SCHEMA, table, column),
    )
    return cur.fetchone() is not None


def _pick_purpose_enrichment_table(cur, routes_table: str | None) -> str | None:
    """
    Optional popgen.trip_leg_purpose_enrichment[_TAG] joined to trip_routes on the route leg key.
    Override: DASHBOARD_PURPOSE_ENRICHMENT_TABLE.
    """
    ex = os.environ.get("DASHBOARD_PURPOSE_ENRICHMENT_TABLE", "").strip()
    if ex and _table_exists(cur, ex) and _column_exists(cur, ex, "purpose_enriched"):
        return ex
    if not routes_table:
        return None
    if routes_table.startswith("trip_routes_building"):
        suf = routes_table[len("trip_routes_building") :]
        cand = f"trip_leg_purpose_enrichment{suf}"
        if _table_exists(cur, cand) and _column_exists(cur, cand, "purpose_enriched"):
            return cand
    if routes_table.startswith("trip_routes_otp_a"):
        suf = routes_table[len("trip_routes_otp_a") :]
        cand = f"trip_leg_purpose_enrichment{suf}"
        if _table_exists(cur, cand) and _column_exists(cur, cand, "purpose_enriched"):
            return cand
    if _table_exists(cur, "trip_leg_purpose_enrichment") and _column_exists(
        cur, "trip_leg_purpose_enrichment", "purpose_enriched"
    ):
        return "trip_leg_purpose_enrichment"
    return None


def _sql_routes_join_purpose_enrichment(r_alias: str, pe_table: str) -> str:
    return f"""
LEFT JOIN {SCHEMA}.{pe_table} pe
  ON pe.synthetic_person_id::text = {r_alias}.synthetic_person_id::text
 AND pe.orig_geo_id::int = {r_alias}.orig_geo_id::int
 AND pe.dest_geo_id::int = {r_alias}.dest_geo_id::int
 AND regexp_replace(trim(COALESCE(pe.purpose_canonical::text, '')), '\\.0$', '') =
     regexp_replace(trim(COALESCE({r_alias}.purpose::text, '')), '\\.0$', '')
 AND (
   CASE WHEN trim(COALESCE(pe.dep_time_bin::text, '')) = '' THEN NULL::numeric
        ELSE trim(pe.dep_time_bin::text)::numeric
   END
 ) IS NOT DISTINCT FROM (
   CASE WHEN trim(COALESCE({r_alias}.dep_time_bin::text, '')) = '' THEN NULL::numeric
        ELSE trim({r_alias}.dep_time_bin::text)::numeric
   END
 )
"""


def _build_zone_map_geojson(
    cur, zones_out: list[dict], *, island_clip_geojson: str | None = None
) -> dict | None:
    """Join zone rows to popgen_zones_geom for choropleth polygons (GeoJSON).

    When ``island_clip_geojson`` is set (routes_only + island_only), polygons are clipped to that
    outline so fills never extend past the shoreline (matches strict island heatmaps).
    """
    if not zones_out:
        return None
    if not _table_exists(cur, "popgen_zones_geom") or not _column_exists(cur, "popgen_zones_geom", "geom"):
        return None
    by_id = {str(z["geo_id"]): z for z in zones_out}
    ids = list(by_id.keys())
    if island_clip_geojson:
        cur.execute(
            f"""
            WITH bnd AS (
                SELECT ST_MakeValid(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s)::geometry, 4326))) AS ig
            )
            SELECT g.geo_id::text,
                   ST_AsGeoJSON(
                       ST_Multi(
                           ST_SimplifyPreserveTopology(
                               ST_CollectionExtract(
                                   ST_MakeValid(
                                       ST_Force2D(
                                           ST_Intersection(
                                               ST_MakeValid(ST_Force2D(g.geom::geometry)),
                                               bnd.ig
                                           )
                                       )
                                   ),
                                   3
                               ),
                               0.000012
                           )
                       )
                   )::text AS gj
            FROM {SCHEMA}.popgen_zones_geom g
            CROSS JOIN bnd
            WHERE g.geo_id::text = ANY(%s)
              AND ST_Intersects(ST_MakeValid(ST_Force2D(g.geom::geometry)), bnd.ig)
              AND NOT ST_IsEmpty(
                  ST_Intersection(ST_MakeValid(ST_Force2D(g.geom::geometry)), bnd.ig)
              )
            """,
            (island_clip_geojson, ids),
        )
    else:
        cur.execute(
            f"""
            SELECT g.geo_id::text,
                   ST_AsGeoJSON(
                       ST_Multi(
                           ST_SimplifyPreserveTopology(
                               ST_MakeValid(ST_Force2D(g.geom::geometry)), 0.000012
                           )
                       )
                   )::text AS gj
            FROM {SCHEMA}.popgen_zones_geom g
            WHERE g.geo_id::text = ANY(%s)
            """,
            (ids,),
        )
    features: list[dict] = []
    for geo_id, gj_text in cur.fetchall():
        z = by_id.get(str(geo_id))
        if not z or gj_text is None:
            continue
        try:
            geom = json.loads(gj_text) if isinstance(gj_text, str) else gj_text
        except (json.JSONDecodeError, TypeError):
            continue
        if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        if not geom.get("coordinates"):
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "geo_id": z["geo_id"],
                    "zone_code": z.get("zone_code") or _zone_code_for(z["geo_id"]),
                    "zone_name": z.get("zone_name") or _zone_name_for(z["geo_id"]),
                    "zone_short_name": z.get("zone_short_name") or _zone_short_name_for(z["geo_id"]),
                    "short_id": z.get("short_id") or _short_zone_id(z["geo_id"]),
                    "total_emissions_g": z["total_emissions_g"],
                    "trips": z["trips"],
                    "trips_legs": z.get("trips_legs", z["trips"]),
                    "trips_weighted": z.get("trips_weighted", z["trips"]),
                    "total_emissions_g_legs": z.get("total_emissions_g_legs", z["total_emissions_g"]),
                    "total_emissions_g_weighted": z.get(
                        "total_emissions_g_weighted", z["total_emissions_g"]
                    ),
                    "total_distance_km": z.get("total_distance_km", 0),
                    "total_distance_km_legs": z.get(
                        "total_distance_km_legs", z.get("total_distance_km", 0)
                    ),
                    "total_distance_km_weighted": z.get(
                        "total_distance_km_weighted", z.get("total_distance_km", 0)
                    ),
                    "lat": z["lat"],
                    "lon": z["lon"],
                },
            }
        )
    if not features:
        return None
    return {"type": "FeatureCollection", "features": features}


def _build_zones_boundary_geojson(cur, *, island_only: bool) -> tuple[dict | None, int, list | None]:
    """All zone polygons from popgen_zones_geom for the boundary reference map.

    Returns (geojson_fc, zone_count, bounds) where bounds is [[south, west], [north, east]].
    """
    if not _table_exists(cur, "popgen_zones_geom") or not _column_exists(cur, "popgen_zones_geom", "geom"):
        return None, 0, None
    use_centroids = _table_exists(cur, "geo_zone_centroids")
    island_gj = _montreal_island_geometry_geojson_for_postgis() if island_only else None
    centroid_join = (
        f"LEFT JOIN {SCHEMA}.geo_zone_centroids c ON c.geo_id::text = g.geo_id::text"
        if use_centroids
        else ""
    )
    centroid_cols = (
        ", c.lat::double precision AS lat, c.lon::double precision AS lon"
        if use_centroids
        else ", NULL::double precision AS lat, NULL::double precision AS lon"
    )
    if island_gj:
        cur.execute(
            f"""
            WITH island AS (
                SELECT ST_MakeValid(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s)::geometry, 4326))) AS ig
            )
            SELECT g.geo_id::text,
                   ST_AsGeoJSON(
                       ST_Multi(
                           ST_SimplifyPreserveTopology(
                               ST_CollectionExtract(
                                   ST_MakeValid(
                                       ST_Force2D(
                                           ST_Intersection(
                                               ST_MakeValid(ST_Force2D(g.geom::geometry)),
                                               island.ig
                                           )
                                       )
                                   ),
                                   3
                               ),
                               0.000015
                           )
                       )
                   )::text AS gj
                   {centroid_cols}
            FROM {SCHEMA}.popgen_zones_geom g
            CROSS JOIN island
            {centroid_join}
            WHERE ST_Intersects(ST_MakeValid(ST_Force2D(g.geom::geometry)), island.ig)
              AND NOT ST_IsEmpty(
                  ST_Intersection(ST_MakeValid(ST_Force2D(g.geom::geometry)), island.ig)
              )
            ORDER BY g.geo_id::text
            """,
            (island_gj,),
        )
    else:
        cur.execute(
            f"""
            SELECT g.geo_id::text,
                   ST_AsGeoJSON(
                       ST_Multi(
                           ST_SimplifyPreserveTopology(
                               ST_MakeValid(ST_Force2D(g.geom::geometry)), 0.000015
                           )
                       )
                   )::text AS gj
                   {centroid_cols}
            FROM {SCHEMA}.popgen_zones_geom g
            {centroid_join}
            ORDER BY g.geo_id::text
            """
        )
    features: list[dict] = []
    for row in cur.fetchall():
        geo_id, gj_text = row[0], row[1]
        lat = row[2] if len(row) > 2 else None
        lon = row[3] if len(row) > 3 else None
        if gj_text is None:
            continue
        try:
            geom = json.loads(gj_text) if isinstance(gj_text, str) else gj_text
        except (json.JSONDecodeError, TypeError):
            continue
        if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        if not geom.get("coordinates"):
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "geo_id": geo_id,
                    "zone_code": _zone_code_for(geo_id),
                    "lat": lat,
                    "lon": lon,
                },
            }
        )
    if not features:
        return None, 0, None
    bounds = _extent_bounds_from_geom_table(cur, island_only=island_only)
    return {"type": "FeatureCollection", "features": features}, len(features), bounds


def _extent_bounds_from_geom_table(cur, *, island_only: bool) -> list | None:
    """[[south, west], [north, east]] from popgen_zones_geom (optionally clipped to island)."""
    island_gj = _montreal_island_geometry_geojson_for_postgis() if island_only else None
    try:
        if island_gj:
            cur.execute(
                f"""
                WITH island AS (
                    SELECT ST_MakeValid(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s)::geometry, 4326))) AS ig
                )
                SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
                FROM (
                    SELECT ST_Extent(
                        ST_Intersection(ST_MakeValid(ST_Force2D(g.geom::geometry)), island.ig)
                    ) AS e
                    FROM {SCHEMA}.popgen_zones_geom g
                    CROSS JOIN island
                    WHERE ST_Intersects(ST_MakeValid(ST_Force2D(g.geom::geometry)), island.ig)
                ) x
                WHERE e IS NOT NULL
                """,
                (island_gj,),
            )
        else:
            cur.execute(
                f"""
                SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
                FROM (
                    SELECT ST_Extent(ST_MakeValid(ST_Force2D(geom::geometry))) AS e
                    FROM {SCHEMA}.popgen_zones_geom
                ) x
                WHERE e IS NOT NULL
                """
            )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        xmin, ymin, xmax, ymax = (float(row[i]) for i in range(4))
        if xmin >= xmax or ymin >= ymax:
            return None
        pad_lat = max((ymax - ymin) * 0.02, 0.01)
        pad_lon = max((xmax - xmin) * 0.02, 0.01)
        return [
            [ymin - pad_lat, xmin - pad_lon],
            [ymax + pad_lat, xmax + pad_lon],
        ]
    except Exception:
        return None


def _enrich_zones_distance_km(
    cur,
    routes_table: str,
    zones: list[dict],
    *,
    zone_by: str,
    has_route_attributed_geo_id: bool,
    has_route_dest_geo_id: bool,
) -> None:
    """Fill total_distance_km on zone rows from route distance_m when not in precompute tables."""
    if not zones or not _table_exists(cur, routes_table) or not _column_exists(cur, routes_table, "distance_m"):
        for z in zones:
            z.setdefault("total_distance_km", 0.0)
        return
    zone_geo_sql = _routes_map_zone_geo_id_sql(
        "r",
        zone_by=zone_by,
        has_route_attributed_geo_id=has_route_attributed_geo_id,
        has_route_dest_geo_id=has_route_dest_geo_id,
    )
    ids = [str(z["geo_id"]) for z in zones]
    cur.execute(
        f"""
        SELECT ({zone_geo_sql})::text AS geo_id,
               COALESCE(SUM(r.distance_m), 0)::double precision / 1000.0 AS total_distance_km
        FROM {SCHEMA}.{routes_table} AS r
        WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
          AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
          AND ({zone_geo_sql})::text = ANY(%s)
        GROUP BY 1
        """,
        (ids,),
    )
    by_id = {str(r[0]): float(r[1] or 0) for r in cur.fetchall()}
    for z in zones:
        z["total_distance_km"] = round(by_id.get(str(z["geo_id"]), 0.0), 2)


def _fetch_building_route_totals(
    cur, routes_table: str, building_id: str, *, building_by: str = "rules"
) -> tuple[float, int, float]:
    """(emissions_g, trips, distance_km) for one building from routes."""
    if not _table_exists(cur, routes_table):
        return 0.0, 0, 0.0
    if building_by == "dest":
        where_b = "r.dest_building_id::text = %s"
    else:
        where_b = "r.route_attributed_building_id::text = %s"
    dist_sql = (
        "COALESCE(SUM(r.distance_m), 0)::double precision / 1000.0"
        if _column_exists(cur, routes_table, "distance_m")
        else "0::double precision"
    )
    cur.execute(
        f"""
        SELECT COALESCE(SUM(r.route_emissions_g), 0)::double precision,
               COUNT(*)::bigint,
               {dist_sql}
        FROM {SCHEMA}.{routes_table} AS r
        WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
          AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
          AND {where_b}
        """,
        (building_id,),
    )
    row = cur.fetchone()
    if not row:
        return 0.0, 0, 0.0
    return float(row[0] or 0), int(row[1] or 0), round(float(row[2] or 0), 2)


def _pick_regex(cur, pattern: str) -> str | None:
    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_name ~ %s
        ORDER BY char_length(table_name), table_name
        LIMIT 1
        """,
        (SCHEMA, pattern),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _suffix_after_building_emissions(emissions_table: str) -> str:
    prefix = "trip_emissions_building"
    if emissions_table.startswith(prefix):
        return emissions_table[len(prefix) :]
    return ""


def _normalize_zone_by(raw: str) -> str:
    z = (raw or "").strip().lower()
    if z in ("meeting", "rules", ""):
        return "rules"
    return z


def _normalize_building_by(raw: str) -> str:
    b = (raw or "rules").strip().lower()
    if b in ("dest", "destination"):
        return "dest"
    return "rules"


def _pick_building_emissions_table(cur, routes_table: str, *, building_by: str = "rules") -> str:
    ex = os.environ.get("DASHBOARD_BUILDING_EMISSIONS_TABLE", "").strip()
    if ex and _table_exists(cur, ex):
        return ex
    if building_by == "dest":
        ex_dest = os.environ.get("DASHBOARD_BUILDING_DEST_TABLE", "").strip()
        if ex_dest and _table_exists(cur, ex_dest):
            return ex_dest
    else:
        ex_rules = os.environ.get("DASHBOARD_BUILDING_RULES_TABLE", "").strip()
        if ex_rules and _table_exists(cur, ex_rules):
            return ex_rules
    suf = _routes_table_suffix(routes_table)
    if building_by == "dest":
        candidates = (f"building_emissions_dest{suf}", "building_emissions_dest")
    else:
        candidates = (f"building_emissions_rules{suf}", "building_emissions_rules")
    for cand in candidates:
        if (
            _table_exists(cur, cand)
            and _column_exists(cur, cand, "building_id")
            and _column_exists(cur, cand, "emissions_g")
        ):
            return cand
    return ""


def _building_footprint_geom_wgs84_sql(b_alias: str = "b") -> str:
    """Footprint polygon in WGS84 (handles non-4326 storage)."""
    a = b_alias
    g = f"{a}.geometry::geometry"
    return (
        f"ST_MakeValid(ST_Force2D("
        f"CASE WHEN ST_SRID({g}) = 4326 THEN {g} ELSE ST_Transform({g}, 4326) END))"
    )


def _building_map_lon_sql(b_alias: str = "b") -> str:
    """Map longitude from footprint geometry — not OSRM route snap."""
    g = _building_footprint_geom_wgs84_sql(b_alias)
    return f"ST_X(ST_PointOnSurface({g}))"


def _building_map_lat_sql(b_alias: str = "b") -> str:
    """Map latitude from footprint geometry — not OSRM route snap."""
    g = _building_footprint_geom_wgs84_sql(b_alias)
    return f"ST_Y(ST_PointOnSurface({g}))"


def _building_footprint_geojson_sql(b_alias: str = "b") -> str:
    """Simplified footprint GeoJSON for map polygons (WGS84)."""
    g = _building_footprint_geom_wgs84_sql(b_alias)
    return f"ST_AsGeoJSON(ST_SimplifyPreserveTopology({g}, 1e-7))::text"


def _building_point_lon_sql(b_alias: str = "b") -> str:
    """Routing longitude: OSRM snap when available, else footprint centroid."""
    a = b_alias
    return f"COALESCE({a}.route_lon::double precision, ST_X(ST_Centroid({a}.geometry::geometry)))"


def _building_point_lat_sql(b_alias: str = "b") -> str:
    """Routing latitude: OSRM snap when available, else footprint centroid."""
    a = b_alias
    return f"COALESCE({a}.route_lat::double precision, ST_Y(ST_Centroid({a}.geometry::geometry)))"


def _building_island_point_predicate(lat_expr: str, lon_expr: str) -> str:
    """Fallback island filter on building points (axis-aligned bbox)."""
    return f"""
              AND ({lat_expr})::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
              AND ({lon_expr})::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX}
    """


def _building_island_cte_and_predicate(lat_expr: str, lon_expr: str) -> tuple[str, str, tuple]:
    """True île outline when GeoJSON is available; otherwise bbox fallback."""
    gj = _montreal_island_geometry_geojson_for_postgis()
    if gj:
        cte = """WITH island AS (
            SELECT ST_MakeValid(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s)::geometry, 4326))) AS ig
        )
        """
        pred = f"""
              AND EXISTS (
                  SELECT 1 FROM island ix
                  WHERE ST_Intersects(
                      ix.ig,
                      ST_SetSRID(
                          ST_MakePoint(({lon_expr})::double precision, ({lat_expr})::double precision),
                          4326
                      )
                  )
              )
        """
        return cte, pred, (gj,)
    return "", _building_island_point_predicate(lat_expr, lon_expr), ()


def _parse_building_grid_cell_deg(raw: str | None) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = float(raw)
    except Exception:
        return None
    if not (0.0005 <= v <= 0.05):
        return None
    return v


def _building_zone_filter_sql(
    cur, lat_expr: str, lon_expr: str, zone_geo_id: str | None, *, b_alias: str = "b"
) -> tuple[str, tuple]:
    """Filter buildings assigned to the PopGen zone (fast; used for zone drill-down)."""
    del cur, lat_expr, lon_expr  # kept for call-site compatibility
    zid = (zone_geo_id or "").strip()
    if not zid:
        return "", ()
    a = b_alias
    return f" AND split_part(trim({a}.zone_geo_id::text), '.', 1) = %s", (zid,)


def _pick_zone_emissions_route_assignment(
    cur, routes_table: str, *, zone_kind: str = "rules"
) -> str:
    ex = os.environ.get("DASHBOARD_ZONE_EMISSIONS_ROUTE_TABLE", "").strip()
    if ex and _table_exists(cur, ex):
        return ex
    if zone_kind == "dest":
        ex_dest = os.environ.get("DASHBOARD_ZONE_DEST_TABLE", "").strip()
        if ex_dest and _table_exists(cur, ex_dest):
            return ex_dest
    suf = ""
    if routes_table.startswith("trip_routes_building"):
        suf = routes_table[len("trip_routes_building") :]
        if zone_kind == "dest":
            candidates = (
                f"zone_emissions_dest{suf}",
                "zone_emissions_dest",
            )
        else:
            candidates = (
                f"zone_emissions_rules{suf}",
                f"zone_emissions_meeting{suf}",
                f"zone_emissions_route_assignment{suf}",
                "zone_emissions_rules",
                "zone_emissions_meeting",
                "zone_emissions_route_assignment",
            )
        for cand in candidates:
            if _table_exists(cur, cand) and _column_exists(cur, cand, "geo_id") and _column_exists(
                cur, cand, "emissions_g"
            ):
                return cand
        return ""
    if routes_table.startswith("trip_routes_otp_a"):
        suf = routes_table[len("trip_routes_otp_a") :]
    for cand in (f"zone_emissions_route_assignment{suf}", "zone_emissions_route_assignment"):
        if _table_exists(cur, cand) and _column_exists(cur, cand, "geo_id") and _column_exists(cur, cand, "emissions_g"):
            return cand
    return ""


def _routes_table_suffix(routes_table: str) -> str:
    if routes_table.startswith("trip_routes_building"):
        return routes_table[len("trip_routes_building") :]
    if routes_table.startswith("trip_routes_otp_a"):
        return routes_table[len("trip_routes_otp_a") :]
    return ""


def _od_flow_island_bbox_sql(alias: str) -> str:
    a = alias
    return f"""
      AND {a}.orig_lat IS NOT NULL AND {a}.orig_lon IS NOT NULL
      AND {a}.dest_lat IS NOT NULL AND {a}.dest_lon IS NOT NULL
      AND {a}.orig_lat::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
      AND {a}.orig_lon::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX}
      AND {a}.dest_lat::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
      AND {a}.dest_lon::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX}
    """


def _flow_coord(v) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _incoming_flow_row_dict(row) -> dict:
    return {
        "orig_geo_id": row[0],
        "trips": int(row[1] or 0),
        "total_emissions_g": float(row[2] or 0),
        "total_distance_km": float(row[3] or 0),
        "orig_lat": _flow_coord(row[4]),
        "orig_lon": _flow_coord(row[5]),
        "dest_lat": _flow_coord(row[6]),
        "dest_lon": _flow_coord(row[7]),
    }


def _zone_rules_totals_sql(zt: str, cur) -> tuple[str, str]:
    has_trips = _column_exists(cur, zt, "trips")
    trips_col = "z.trips" if has_trips else "NULL"
    if _column_exists(cur, zt, "emissions_g"):
        eg_col = "z.emissions_g"
    elif _column_exists(cur, zt, "total_emissions_g"):
        eg_col = "z.total_emissions_g"
    else:
        eg_col = "NULL"
    return trips_col, eg_col


def _zif_precompute_tables(cur, routes_table: str) -> tuple[str, str] | tuple[None, None]:
    """Return (top_table, totals_table) of precomputed incoming flows if both exist.

    Built by scripts/build_zone_incoming_flows_precompute.py (tour-based partner logic).
    """
    suf = _routes_table_suffix(routes_table) or "_custom"
    top_t = f"zone_incoming_flows_top{suf}"
    tot_t = f"zone_incoming_flows_totals{suf}"
    if (
        _table_exists(cur, top_t)
        and _table_exists(cur, tot_t)
        and _column_exists(cur, top_t, "dest_geo_id")
        and _column_exists(cur, tot_t, "total_incoming_trips")
    ):
        return top_t, tot_t
    return None, None


def _zif_dest_precompute_tables(cur, routes_table: str) -> tuple[str, str] | tuple[None, None]:
    """Return (top_table, totals_table) for physical-destination incoming flows if both exist."""
    suf = _routes_table_suffix(routes_table) or "_custom"
    top_t = f"zone_dest_incoming_flows_top{suf}"
    tot_t = f"zone_dest_incoming_flows_totals{suf}"
    if (
        _table_exists(cur, top_t)
        and _table_exists(cur, tot_t)
        and _column_exists(cur, top_t, "dest_geo_id")
        and _column_exists(cur, tot_t, "total_incoming_trips")
    ):
        return top_t, tot_t
    return None, None


def _zif_tour_partner_sql(r_alias: str = "r") -> tuple[str, str, str]:
    """Rules-attributed zone + origin/dest split for tour-based incoming flows."""
    attr = (
        f"split_part(trim(COALESCE(NULLIF(btrim({r_alias}.route_attributed_geo_id::text),''),"
        f"{r_alias}.dest_geo_id::text)),'.',1)"
    )
    orig = f"split_part(trim({r_alias}.orig_geo_id::text),'.',1)"
    dest = f"split_part(trim({r_alias}.dest_geo_id::text),'.',1)"
    return attr, orig, dest


def _fetch_incoming_distance_km_for_dest(cur, routes_table: str, dest_id: str) -> float:
    """Sum route distance (km) for inter-zonal partners attributed to ``dest_id``."""
    if not _table_exists(cur, routes_table) or not _column_exists(cur, routes_table, "distance_m"):
        return 0.0
    attr, orig, dest = _zif_tour_partner_sql("r")
    cur.execute(
        f"""
        SELECT COALESCE(SUM(COALESCE(r.distance_m, 0)), 0)::double precision / 1000.0
        FROM {SCHEMA}.{routes_table} r
        WHERE r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
          AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
          AND NOT (({orig}) = ({attr}) AND ({dest}) = ({attr}))
          AND ({attr})::text = %s
        """,
        (dest_id,),
    )
    row = cur.fetchone()
    return round(float(row[0] or 0), 2) if row else 0.0


def _read_zif_total_incoming_distance_km(
    cur, tot_t: str, dest_id: str, routes_table: str
) -> float:
    if _column_exists(cur, tot_t, "total_incoming_distance_km"):
        cur.execute(
            f"""
            SELECT total_incoming_distance_km::double precision
            FROM {SCHEMA}.{tot_t}
            WHERE dest_geo_id::text = %s
            LIMIT 1
            """,
            (dest_id,),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            return round(float(row[0]), 2)
    return _fetch_incoming_distance_km_for_dest(cur, routes_table, dest_id)


def _pick_od_flow_summary_table(cur, routes_table: str) -> str:
    suf = _routes_table_suffix(routes_table)
    for cand in (f"od_flows_route_assignment{suf}", "od_flows_route_assignment"):
        if (
            _table_exists(cur, cand)
            and _column_exists(cur, cand, "orig_geo_id")
            and _column_exists(cur, cand, "dest_geo_id")
            and _column_exists(cur, cand, "trips")
            and _column_exists(cur, cand, "total_emissions_g")
            and _column_exists(cur, cand, "orig_lat")
            and _column_exists(cur, cand, "orig_lon")
            and _column_exists(cur, cand, "dest_lat")
            and _column_exists(cur, cand, "dest_lon")
        ):
            return cand
    return ""


def _routes_map_zone_geo_id_sql(
    r_alias: str,
    *,
    zone_by: str = "rules",
    has_route_attributed_geo_id: bool = False,
    has_route_dest_geo_id: bool = False,
) -> str:
    """Zone id for choropleth: island/rules attribution, or destination CT only."""
    if zone_by == "dest":
        if has_route_dest_geo_id:
            return (
                f"split_part(trim(COALESCE("
                f"NULLIF(btrim({r_alias}.route_dest_geo_id::text), ''), "
                f"{r_alias}.dest_geo_id::text)), '.', 1)"
            )
        return f"split_part(trim({r_alias}.dest_geo_id::text), '.', 1)"
    if not has_route_attributed_geo_id:
        return f"split_part(trim({r_alias}.dest_geo_id::text), '.', 1)"
    return (
        f"split_part(trim(COALESCE("
        f"NULLIF(btrim({r_alias}.route_attributed_geo_id::text), ''), "
        f"{r_alias}.dest_geo_id::text)), '.', 1)"
    )


def _try_routes_emissions_family(cur) -> dict | None:
    explicit = os.environ.get("DASHBOARD_ROUTES_TABLE", "").strip()
    if explicit and _table_exists(cur, explicit) and _column_exists(cur, explicit, "route_emissions_g"):
        candidates = [explicit]
    else:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = %s
              AND (table_name ~ %s OR table_name ~ %s)
            ORDER BY char_length(table_name) DESC, table_name DESC
            """,
            (SCHEMA, r"^trip_routes_otp_a(_.*)?$", r"^trip_routes_building(_.*)?$"),
        )
        candidates = [r[0] for r in cur.fetchall()]
    for rt in candidates:
        if not _column_exists(cur, rt, "route_emissions_g"):
            continue
        cur.execute(
            f"""
            SELECT EXISTS (
                SELECT 1 FROM {SCHEMA}.{rt} r
                WHERE r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                LIMIT 1
            )
            """
        )
        if not cur.fetchone()[0]:
            continue
        zn = _pick_zone_emissions_route_assignment(cur, rt)
        pe = _pick_purpose_enrichment_table(cur, rt)
        return {
            "ok": True,
            "family": "routes_only",
            "emissions": None,
            "trips": None,
            "routes": rt,
            "zone": zn,
            "purpose_enrichment": pe,
            "routes_join": False,
        }
    return None


def resolve_tables() -> dict:
    global _RESOLVED
    if _RESOLVED is not None:
        return _RESOLVED

    conn = get_conn()
    cur = conn.cursor()
    try:
        if os.environ.get("DASHBOARD_FAMILY", "").strip().lower() == "routes_only":
            fr = _try_routes_emissions_family(cur)
            if fr:
                _RESOLVED = fr
                return _RESOLVED
            _RESOLVED = {
                "ok": False,
                "message": "DASHBOARD_FAMILY=routes_only but no trip_routes_otp_a* / trip_routes_building* with route_emissions_g.",
                "hints": [],
            }
            return _RESOLVED
        em = os.environ.get("DASHBOARD_EMISSIONS_TABLE", "").strip()
        family = ""
        if em and not _table_exists(cur, em):
            em = ""
        if em:
            family = "building" if em.startswith("trip_emissions_building") else "micro"
        else:
            em = _pick_regex(cur, rf"^{re.escape('trip_emissions_building')}(_.*)?$") or ""
            if em:
                family = "building"
                # Fast path: route-level emissions on trip_routes_building_* (100pct_ct pipeline).
                if not os.environ.get("DASHBOARD_EMISSIONS_TABLE", "").strip():
                    fr = _try_routes_emissions_family(cur)
                    if fr:
                        _RESOLVED = fr
                        return _RESOLVED
            if not em:
                if _table_exists(cur, "trip_emissions"):
                    em, family = "trip_emissions", "micro"
                else:
                    alt = _pick_regex(cur, r"^trip_emissions(_(?!building).*)?$")
                    if alt:
                        em, family = alt, "micro"

        if not em:
            fr = _try_routes_emissions_family(cur)
            if fr:
                _RESOLVED = fr
                return _RESOLVED
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name LIMIT 120",
                (SCHEMA,),
            )
            hints = [r[0] for r in cur.fetchall()]
            _RESOLVED = {
                "ok": False,
                "message": "No trip_emissions* or usable trip_routes_otp_a* / trip_routes_building* with route_emissions_g.",
                "hints": hints,
            }
            return _RESOLVED

        if family == "micro":
            tr = os.environ.get("DASHBOARD_TRIPS_TABLE", "").strip() or _pick_regex(cur, r"^popgen_trip_micro(_.*)?$")
            if not tr and _table_exists(cur, "popgen_trip_micro"):
                tr = "popgen_trip_micro"
            if not tr:
                _RESOLVED = {"ok": False, "message": f"Found {em} but no popgen_trip_micro*.", "hints": []}
                return _RESOLVED
            rt = os.environ.get("DASHBOARD_ROUTES_TABLE", "").strip()
            if not rt:
                rt = next((n for n in ["trip_routes_otp_a"] if _table_exists(cur, n)), None) or _pick_regex(
                    cur, r"^trip_routes_otp_a(_.*)?$"
                )
            routes_join = bool(rt and _table_exists(cur, rt))
            _RESOLVED = {
                "ok": True,
                "family": "micro",
                "emissions": em,
                "trips": tr,
                "routes": rt,
                "zone": "zone_emissions_rules",
                "purpose_enrichment": _pick_purpose_enrichment_table(cur, rt) if rt else None,
                "routes_join": routes_join,
            }
            return _RESOLVED

        suf = _suffix_after_building_emissions(em)
        tr = os.environ.get("DASHBOARD_TRIPS_TABLE", "").strip()
        if not tr:
            tries = []
            if suf:
                tries.extend([f"popgen_trip_building{suf}_dedup", f"popgen_trip_building{suf}"])
            tries.append("popgen_trip_building")
            tr = next((n for n in tries if _table_exists(cur, n)), None) or _pick_regex(
                cur, rf"^{re.escape('popgen_trip_building')}(_.*)?$"
            ) or "popgen_trip_building"
        rt = os.environ.get("DASHBOARD_ROUTES_TABLE", "").strip()
        if not rt:
            tries = []
            if suf:
                tries.extend([f"trip_routes_building{suf}", f"trip_routes_otp_a{suf}"])
            tries.extend(["trip_routes_building", "trip_routes_otp_a"])
            rt = next((n for n in tries if _table_exists(cur, n)), None) or _pick_regex(
                cur, rf"^{re.escape('trip_routes_building')}(_.*)?$"
            ) or _pick_regex(cur, rf"^{re.escape('trip_routes_otp_a')}(_.*)?$") or "trip_routes_otp_a"
        zn = os.environ.get("DASHBOARD_ZONE_TABLE", "").strip()
        if not zn:
            tries = []
            if suf:
                tries.append(f"zone_emissions_rules{suf}")
                tries.append(f"zone_emissions_meeting{suf}")
            tries.extend(["zone_emissions_rules", "zone_emissions_meeting"])
            zn = next((n for n in tries if _table_exists(cur, n)), None) or _pick_regex(
                cur, rf"^{re.escape('zone_emissions_rules')}(_.*)?$"
            ) or _pick_regex(cur, rf"^{re.escape('zone_emissions_meeting')}(_.*)?$") or "zone_emissions_rules"
        routes_join = bool(rt and _table_exists(cur, rt))
        pe = _pick_purpose_enrichment_table(cur, rt) if rt else None
        _RESOLVED = {
            "ok": True,
            "family": "building",
            "emissions": em,
            "trips": tr,
            "routes": rt,
            "zone": zn,
            "purpose_enrichment": pe,
            "routes_join": routes_join,
        }
        return _RESOLVED
    finally:
        cur.close()
        conn.close()


def _tables_from_request() -> dict | tuple:
    base = resolve_tables()
    if not base.get("ok"):
        return (
            jsonify(
                {
                    "error": "dashboard_tables",
                    "message": base["message"],
                    "hints": base.get("hints") or [],
                }
            ),
            503,
        )

    def _q(name: str, fallback):
        v = request.args.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            return fallback
        return v.strip() if isinstance(v, str) else v

    return {
        "emissions": _q("emissions_table", base.get("emissions")),
        "trips": _q("trips_table", base.get("trips")),
        "routes": _q("routes_table", base.get("routes")),
        "zone": _q("zone_table", base.get("zone")),
        "purpose_enrichment": _q("purpose_enrichment_table", base.get("purpose_enrichment")),
        "family": base.get("family", "building"),
        "routes_join": base.get("routes_join", True),
    }


def get_conn():
    return psycopg2.connect(**DB_PARAMS)


app = Flask(
    __name__,
    static_folder=str(REPO_DATA_DIR / "dashboard"),
    static_url_path="/static",
)
app.config["JSON_SORT_KEYS"] = False


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    if request.path.startswith("/od-dashboard-api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/od-dashboard-api/health")
def api_health():
    dist_probe: dict = {}
    try:
        conn = get_conn()
        cur = conn.cursor()
        t = resolve_tables()
        if isinstance(t, dict) and t.get("family") == "routes_only":
            zt = (t.get("zone") or "").strip()
            rt = (t.get("routes") or "").strip()
            if zt and _table_exists(cur, zt) and _column_exists(cur, zt, "distance_km"):
                cur.execute(
                    f"""
                    SELECT geo_id::text, distance_km, emissions_g
                    FROM {SCHEMA}.{zt}
                    WHERE geo_id::text IN ('135', '217')
                    ORDER BY geo_id
                    """
                )
                dist_probe["precompute"] = {
                    str(r[0]): {"distance_km": float(r[1] or 0), "emissions_g": float(r[2] or 0)}
                    for r in cur.fetchall()
                }
            if rt and _table_exists(cur, rt):
                has_attr = _column_exists(cur, rt, "route_attributed_geo_id")
                zsql = _routes_map_zone_geo_id_sql(
                    "r", zone_by="rules", has_route_attributed_geo_id=has_attr, has_route_dest_geo_id=False
                )
                cur.execute(
                    f"""
                    SELECT ({zsql})::text,
                           COALESCE(SUM(r.distance_m), 0) / 1000.0
                    FROM {SCHEMA}.{rt} r
                    WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                      AND r.route_emissions_g > 0
                      AND ({zsql})::text IN ('135', '217')
                    GROUP BY 1
                    """,
                )
                dist_probe["routes_live"] = {str(r[0]): round(float(r[1] or 0), 2) for r in cur.fetchall()}
        cur.close()
        conn.close()
    except Exception as exc:
        dist_probe["error"] = str(exc)
    return jsonify(
        {
            "ok": True,
            "service": "popgen-dashboard",
            "version": "2026-05-27-zone-distance",
            "features": {
                "building_footprint": True,
                "building_map_footprints": True,
                "building_map_point_source": "footprint",
                "zone_map_distance": True,
            },
            "ui": {"zone_tooltips": "20260527-9"},
            "distance_probe": dist_probe,
        }
    )


@app.route("/od-dashboard-api/montreal_boundary.geojson")
def api_montreal_boundary():
    for fname in ("mtl_boundary_file_padded.geojson", "mtl_boundary_file.geojson"):
        p = REPO_DATA_DIR / fname
        if p.is_file():
            return send_file(p, mimetype="application/geo+json")
    return jsonify({"type": "FeatureCollection", "features": []})


@app.route("/od-dashboard-api/zones_boundary")
def api_zones_boundary():
    """Zone polygon outlines for the CMM boundary reference map (no emissions join)."""
    island_only = _request_island_only(default=False)
    conn = get_conn()
    cur = conn.cursor()
    try:
        geojson, zone_count, bounds = _build_zones_boundary_geojson(cur, island_only=island_only)
        if bounds is None:
            bounds = MONTREAL_BOUNDS if island_only else CMM_BOUNDS
        return jsonify(
            {
                "geojson": geojson,
                "zone_count": zone_count,
                "bounds": bounds,
                "island_only": island_only,
            }
        )
    finally:
        cur.close()
        conn.close()


CATEGORY_NAMES = {
    "0": "BEV",
    "1": "HEV",
    "2": "Gas C.",
    "3": "Gas M.",
    "4": "SUV",
    "5": "PU/Van",
}
ROUTE_ASSIGNMENT_LABELS = {
    "work": "Work",
    "home": "Home",
    "education": "Edu",
    "shop": "Shop",
    "shopping": "Shop",
    "personal_business": "Personal",
    "leisure": "Leis.",
    "visit_social": "Visit",
    "health": "Health",
    "pickup_dropoff": "Pick-up / drop-off",
    "return_home": "Home",
    "other": "Other",
}


def _routes_travel_reason_bucket_sql(r_alias: str = "r", *, has_purpose: bool = True) -> str:
    """Travel reason for charts: assignment label, else trip purpose (work, education, …)."""
    assign = (
        f"LOWER(NULLIF(trim(split_part(COALESCE({r_alias}.route_emission_assignment::text, ''), ':', 1)), ''))"
    )
    if not has_purpose:
        return assign
    purpose_reason = motif_travel_reason_sql(purpose_expr=f"{r_alias}.purpose")
    return f"COALESCE({assign}, {purpose_reason})"


def _fetch_stats_from_zone_table(cur, zone_table: str, *, island_only: bool) -> dict | None:
    """Fast KPI totals from precomputed zone_emissions_* (island filter on zone centroids)."""
    zt = str(zone_table or "").strip()
    if not zt or not _table_exists(cur, zt) or not _column_exists(cur, zt, "emissions_g"):
        return None
    r_cte, ic, _, _, _, _, isl_params = _routes_only_island_cte_and_predicates(island_only)
    if island_only and _table_exists(cur, "geo_zone_centroids"):
        cur.execute(
            f"""
            SELECT COALESCE(SUM(z.trips), 0)::bigint,
                   COALESCE(SUM(z.emissions_g), 0)::double precision
            FROM {SCHEMA}.{zt} z
            JOIN {SCHEMA}.geo_zone_centroids c ON c.geo_id::text = z.geo_id::text
            WHERE z.emissions_g > 0
            """
        )
    elif _table_exists(cur, "geo_zone_centroids"):
        cur.execute(
            r_cte
            + f"""
            SELECT COALESCE(SUM(z.trips), 0)::bigint,
                   COALESCE(SUM(z.emissions_g), 0)::double precision
            FROM {SCHEMA}.{zt} z
            JOIN {SCHEMA}.geo_zone_centroids c ON c.geo_id::text = z.geo_id::text
            WHERE z.emissions_g > 0 {ic}
            """,
            isl_params,
        )
    else:
        cur.execute(
            f"""
            SELECT COALESCE(SUM(trips), 0)::bigint,
                   COALESCE(SUM(emissions_g), 0)::double precision
            FROM {SCHEMA}.{zt}
            WHERE emissions_g > 0
            """
        )
    row = cur.fetchone()
    if not row or int(row[0] or 0) <= 0:
        return None
    trips = int(row[0])
    total_g = float(row[1] or 0)
    return {
        "trips": trips,
        "total_emissions_g": total_g,
        "total_emissions_tonnes": round(total_g / 1e6, 2),
        "total_distance_km": 0.0,
        "avg_emissions_g_per_trip": round(total_g / trips, 2) if trips else 0,
    }


def _routes_only_route_distance_km(cur, routes_table: str, *, island_only: bool) -> float:
    rt = str(routes_table).strip()
    _, _, _, _, _, i_route, isl_params = _routes_only_island_cte_and_predicates(island_only)
    cur.execute(
        f"""
        SELECT COALESCE(SUM(r.distance_m), 0)::double precision / 1000.0
        FROM {SCHEMA}.{rt} r
        WHERE r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
          AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
          {i_route}
        """,
        isl_params,
    )
    return float((cur.fetchone() or (0,))[0] or 0)


def _route_bucket_category(ft: str) -> str:
    """Short chart label for routes_only bucket (travel_reason from route_emission_assignment)."""
    if ft is None or not str(ft).strip():
        return "Unassigned"
    key = str(ft).strip().lower()
    return ROUTE_ASSIGNMENT_LABELS.get(key) or (key[:10] + "..." if len(key) > 10 else key)


def _fetch_stats_payload(cur, t: dict, *, island_only: bool = True) -> dict:
    table = t.get("emissions")
    trips_t = t.get("trips")
    routes_t = t.get("routes")
    fam = t.get("family", "building")
    routes_join = t.get("routes_join", True) and routes_t
    weight_trips_t = _pick_trips_table_for_weight(cur, routes_t, trips_t)
    use_weighted = _dashboard_metrics_weighted() and weight_trips_t is not None
    if fam == "routes_only":
        rt = t["routes"]
        zt = (t.get("zone") or "").strip() or _pick_zone_emissions_route_assignment(
            cur, rt, zone_kind="rules"
        )
        if island_only and zt and not use_weighted:
            fast = _fetch_stats_from_zone_table(cur, zt, island_only=True)
            if fast:
                fast["total_distance_km"] = round(
                    _routes_only_route_distance_km(cur, rt, island_only=True), 2
                )
                return fast
        r_cte, _, _, _, _, i_route, isl_params = _routes_only_island_cte_and_predicates(island_only)
        if use_weighted:
            w = _trip_weight_expr("t")
            cur.execute(
                r_cte
                + f"""
                SELECT COUNT(*)::bigint,
                       COALESCE(SUM({w}), 0)::double precision,
                       COALESCE(SUM(r.route_emissions_g * {w}), 0)::double precision,
                       COALESCE(SUM(r.distance_m * {w}), 0)::double precision / 1000.0,
                       COALESCE(SUM(r.route_emissions_g), 0)::double precision
                FROM {SCHEMA}.{rt} r
                {_sql_routes_join_trips_table("r", "t", weight_trips_t)}
                WHERE r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                  AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                  {i_route}
                """,
                isl_params,
            )
            row = cur.fetchone()
            return _finish_stats_payload(
                trips_legs=int(row[0] or 0),
                total_emissions_g_legs=float(row[4] or 0),
                total_distance_km_legs=float(
                    _routes_only_route_distance_km(cur, rt, island_only=island_only)
                ),
                trips_weighted=float(row[1] or 0),
                total_emissions_g_weighted=float(row[2] or 0),
                total_distance_km_weighted=float(row[3] or 0),
                metrics_weighted=True,
            )
        cur.execute(
            r_cte
            + f"""
            SELECT COUNT(*)::bigint,
                   COALESCE(SUM(r.route_emissions_g), 0)::double precision,
                   COALESCE(SUM(r.distance_m), 0)::double precision / 1000.0
            FROM {SCHEMA}.{rt} r
            WHERE r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
              AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
              {i_route}
            """,
            isl_params,
        )
    elif fam == "micro":
        cur.execute(
            f"""
            SELECT COUNT(*), COALESCE(SUM(e.emissions_g), 0), COALESCE(SUM(e.distance_m), 0) / 1000.0
            FROM {SCHEMA}.{table} e
            WHERE trim(e.mode_group::text) IN {CAR_MODE_GROUPS} AND e.emissions_g > 0
            """
        )
    elif not routes_join:
        cur.execute(
            f"""
            SELECT COUNT(*),
                   COALESCE(SUM(COALESCE(e.emissions_g_pair, e.emissions_g)), 0),
                   COALESCE(SUM(e.distance_m), 0) / 1000.0
            FROM {SCHEMA}.{table} e
            LEFT JOIN {SCHEMA}.{trips_t} t ON t.synthetic_person_id::text = e.synthetic_person_id::text
             AND t.orig_geo_id::int = e.orig_geo_id::int AND t.dest_geo_id::int = e.dest_geo_id::int
             AND COALESCE(t.purpose::text,'') = COALESCE(e.purpose::text,'')
             AND COALESCE(t.dep_time_bin::text,'') = COALESCE(e.dep_time_bin::text,'')
             AND (e.hh_id IS NULL OR t.hh_id::text IS NOT DISTINCT FROM e.hh_id::text)
            WHERE trim(COALESCE(e.mode_group::text, t.mode_group::text)) IN {CAR_MODE_GROUPS}
              AND COALESCE(e.emissions_g_pair, e.emissions_g) > 0
            """
        )
    else:
        cur.execute(
            f"""
            SELECT COUNT(*),
                   COALESCE(SUM(COALESCE(e.emissions_g_pair, e.emissions_g)), 0),
                   COALESCE(SUM(COALESCE(e.distance_m, r.distance_m)), 0) / 1000.0
            FROM {SCHEMA}.{table} e
            LEFT JOIN {SCHEMA}.{trips_t} t ON t.synthetic_person_id::text = e.synthetic_person_id::text
             AND t.orig_geo_id::int = e.orig_geo_id::int AND t.dest_geo_id::int = e.dest_geo_id::int
             AND COALESCE(t.purpose::text,'') = COALESCE(e.purpose::text,'')
             AND COALESCE(t.dep_time_bin::text,'') = COALESCE(e.dep_time_bin::text,'')
             AND (e.hh_id IS NULL OR t.hh_id::text IS NOT DISTINCT FROM e.hh_id::text)
            LEFT JOIN {SCHEMA}.{routes_t} r ON r.synthetic_person_id::text = e.synthetic_person_id::text
             AND r.orig_geo_id = e.orig_geo_id::int AND r.dest_geo_id = e.dest_geo_id::int
             AND COALESCE(r.purpose,'') = COALESCE(e.purpose::text,'')
             AND COALESCE(r.dep_time_bin,'') = COALESCE(e.dep_time_bin::text,'')
            WHERE trim(COALESCE(e.mode_group::text, t.mode_group::text)) IN {CAR_MODE_GROUPS}
              AND COALESCE(e.emissions_g_pair, e.emissions_g) > 0
            """
        )
    row = cur.fetchone()
    trips = int(row[0] or 0)
    total_g = float(row[1] or 0)
    distance_km = float(row[2] or 0)
    return _finish_stats_payload(
        trips_legs=trips,
        total_emissions_g_legs=total_g,
        total_distance_km_legs=distance_km,
        metrics_weighted=False,
    )


def _fetch_by_category_payload(cur, t: dict, *, island_only: bool = True) -> list[dict]:
    table = t.get("emissions")
    trips_t = t.get("trips")
    routes_t = t.get("routes")
    fam = t.get("family", "building")
    routes_join = t.get("routes_join", True) and routes_t
    if fam == "routes_only":
        rt = t["routes"]
        has_assign = _column_exists(cur, rt, "route_emission_assignment")
        has_purpose = _column_exists(cur, rt, "purpose")
        if has_assign or has_purpose:
            bucket_sql = _routes_travel_reason_bucket_sql("r", has_purpose=has_purpose)
        else:
            bucket_sql = "NULLIF(trim(r.mode_group::text), '')"
        r_cte, _, _, _, _, i_route, isl_params = _routes_only_island_cte_and_predicates(island_only)
        cur.execute(
            r_cte
            + f"""
            SELECT {bucket_sql} AS bucket,
                   SUM(r.route_emissions_g)::double precision,
                   SUM(r.distance_m) / 1000.0,
                   COUNT(*)::bigint
            FROM {SCHEMA}.{rt} r
            WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
              AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
              {i_route}
            GROUP BY 1
            ORDER BY 2 DESC NULLS LAST
            """,
            isl_params,
        )
    elif fam == "micro":
        cur.execute(
            f"""
            SELECT e.emissions_factor_type, SUM(e.emissions_g), SUM(e.distance_m)/1000.0, COUNT(*)
            FROM {SCHEMA}.{table} e
            WHERE trim(e.mode_group::text) IN {CAR_MODE_GROUPS} AND e.emissions_g > 0
            GROUP BY e.emissions_factor_type ORDER BY e.emissions_factor_type
            """
        )
    elif not routes_join:
        cur.execute(
            f"""
            SELECT e.emissions_factor_type,
                   SUM(COALESCE(e.emissions_g_pair, e.emissions_g)),
                   SUM(e.distance_m)/1000.0, COUNT(*)
            FROM {SCHEMA}.{table} e
            LEFT JOIN {SCHEMA}.{trips_t} t ON t.synthetic_person_id::text = e.synthetic_person_id::text
             AND t.orig_geo_id::int = e.orig_geo_id::int AND t.dest_geo_id::int = e.dest_geo_id::int
             AND COALESCE(t.purpose::text,'') = COALESCE(e.purpose::text,'')
             AND COALESCE(t.dep_time_bin::text,'') = COALESCE(e.dep_time_bin::text,'')
             AND (e.hh_id IS NULL OR t.hh_id::text IS NOT DISTINCT FROM e.hh_id::text)
            WHERE trim(COALESCE(e.mode_group::text, t.mode_group::text)) IN {CAR_MODE_GROUPS}
              AND COALESCE(e.emissions_g_pair, e.emissions_g) > 0
            GROUP BY e.emissions_factor_type ORDER BY e.emissions_factor_type
            """
        )
    else:
        cur.execute(
            f"""
            SELECT e.emissions_factor_type,
                   SUM(COALESCE(e.emissions_g_pair, e.emissions_g)),
                   SUM(COALESCE(e.distance_m, r.distance_m))/1000.0, COUNT(*)
            FROM {SCHEMA}.{table} e
            LEFT JOIN {SCHEMA}.{trips_t} t ON t.synthetic_person_id::text = e.synthetic_person_id::text
             AND t.orig_geo_id::int = e.orig_geo_id::int AND t.dest_geo_id::int = e.dest_geo_id::int
             AND COALESCE(t.purpose::text,'') = COALESCE(e.purpose::text,'')
             AND COALESCE(t.dep_time_bin::text,'') = COALESCE(e.dep_time_bin::text,'')
             AND (e.hh_id IS NULL OR t.hh_id::text IS NOT DISTINCT FROM e.hh_id::text)
            LEFT JOIN {SCHEMA}.{routes_t} r ON r.synthetic_person_id::text = e.synthetic_person_id::text
             AND r.orig_geo_id = e.orig_geo_id::int AND r.dest_geo_id = e.dest_geo_id::int
             AND COALESCE(r.purpose,'') = COALESCE(e.purpose::text,'')
             AND COALESCE(r.dep_time_bin,'') = COALESCE(e.dep_time_bin::text,'')
            WHERE trim(COALESCE(e.mode_group::text, t.mode_group::text)) IN {CAR_MODE_GROUPS}
              AND COALESCE(e.emissions_g_pair, e.emissions_g) > 0
            GROUP BY e.emissions_factor_type ORDER BY e.emissions_factor_type
            """
        )
    rows = cur.fetchall()
    out: list[dict] = []
    for r in rows:
        ft = str(r[0]).strip() if r[0] is not None else ""
        if fam == "routes_only":
            cat = _route_bucket_category(ft)
        else:
            cat = CATEGORY_NAMES.get(ft, f"T{ft}" if ft else "?")
        out.append(
            {
                "emissions_factor_type": ft,
                "category": cat,
                "total_emissions_g": float(r[1] or 0),
                "total_emissions_tonnes": round(float(r[1] or 0) / 1e6, 2),
                "total_distance_km": round(float(r[2] or 0), 2),
                "trips": r[3],
            }
        )
    return out


def _fetch_purpose_motif_payload(cur, t: dict, lim: int) -> dict:
    fam = t.get("family", "building")
    rt = t.get("routes")
    pe = t.get("purpose_enrichment")
    routes_join = t.get("routes_join", True) and rt
    if not pe:
        return {
            "available": False,
            "rows": [],
            "message": "No trip_leg_purpose_enrichment* table (or DASHBOARD_PURPOSE_ENRICHMENT_TABLE) for this routes suffix.",
        }
    if not _table_exists(cur, pe) or not _column_exists(cur, pe, "purpose_enriched"):
        return {"available": False, "rows": [], "message": f"Table {pe!r} missing or has no purpose_enriched."}
    join_pe = _sql_routes_join_purpose_enrichment("r", pe)
    motif_expr = (
        "COALESCE(NULLIF(trim(pe.purpose_enriched_label::text), ''), "
        + _motif18_label_case_sql("pe.purpose_enriched")
        + ")"
        if _column_exists(cur, pe, "purpose_enriched_label")
        else _motif18_label_case_sql("pe.purpose_enriched")
    )
    if fam == "routes_only":
        if not rt or not _table_exists(cur, rt):
            return {"available": False, "rows": [], "message": "No routes table."}
        cur.execute(
            f"""
            SELECT {motif_expr} AS motif,
                   SUM(r.route_emissions_g)::double precision,
                   SUM(r.distance_m)::double precision / 1000.0,
                   COUNT(*)::bigint
            FROM {SCHEMA}.{rt} r
            {join_pe}
            WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
              AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
            GROUP BY 1
            ORDER BY 2 DESC NULLS LAST
            LIMIT %s
            """,
            (lim,),
        )
    elif not routes_join or not rt:
        return {
            "available": False,
            "rows": [],
            "message": "Purpose motif breakdown needs a routes table joined to emissions (building/micro with routes).",
        }
    else:
        em = t.get("emissions")
        trips_t = t.get("trips")
        if not em or not trips_t or not _table_exists(cur, em) or not _table_exists(cur, trips_t):
            return {"available": False, "rows": [], "message": "Missing emissions or trips table."}
        cur.execute(
            f"""
            SELECT {motif_expr} AS motif,
                   SUM(COALESCE(e.emissions_g_pair, e.emissions_g))::double precision,
                   SUM(COALESCE(e.distance_m, r.distance_m))::double precision / 1000.0,
                   COUNT(*)::bigint
            FROM {SCHEMA}.{em} e
            LEFT JOIN {SCHEMA}.{trips_t} t ON t.synthetic_person_id::text = e.synthetic_person_id::text
             AND t.orig_geo_id::int = e.orig_geo_id::int AND t.dest_geo_id::int = e.dest_geo_id::int
             AND COALESCE(t.purpose::text,'') = COALESCE(e.purpose::text,'')
             AND COALESCE(t.dep_time_bin::text,'') = COALESCE(e.dep_time_bin::text,'')
             AND (e.hh_id IS NULL OR t.hh_id::text IS NOT DISTINCT FROM e.hh_id::text)
            LEFT JOIN {SCHEMA}.{rt} r ON r.synthetic_person_id::text = e.synthetic_person_id::text
             AND r.orig_geo_id = e.orig_geo_id::int AND r.dest_geo_id = e.dest_geo_id::int
             AND COALESCE(r.purpose,'') = COALESCE(e.purpose::text,'')
             AND COALESCE(r.dep_time_bin,'') = COALESCE(e.dep_time_bin::text,'')
            {join_pe}
            WHERE trim(COALESCE(e.mode_group::text, t.mode_group::text)) IN {CAR_MODE_GROUPS}
              AND COALESCE(e.emissions_g_pair, e.emissions_g) > 0
            GROUP BY 1
            ORDER BY 2 DESC NULLS LAST
            LIMIT %s
            """,
            (lim,),
        )
    rows = cur.fetchall()
    out = []
    for r in rows:
        g = float(r[1] or 0)
        out.append(
            {
                "motif": str(r[0]) if r[0] is not None else "(join missing)",
                "total_emissions_g": g,
                "total_emissions_tonnes": round(g / 1e6, 4),
                "total_distance_km": round(float(r[2] or 0), 2),
                "trips": int(r[3] or 0),
            }
        )
    return {
        "available": True,
        "routes_table": rt,
        "purpose_enrichment_table": pe,
        "limit": lim,
        "rows": out,
    }


@app.route("/od-dashboard-api/stats")
def api_stats():
    t = _tables_from_request()
    if isinstance(t, tuple):
        return t
    conn = get_conn()
    cur = conn.cursor()
    try:
        return jsonify(_fetch_stats_payload(cur, t, island_only=_request_island_only()))
    finally:
        cur.close()
        conn.close()


@app.route("/od-dashboard-api/by_category")
def api_by_category():
    t = _tables_from_request()
    if isinstance(t, tuple):
        return t
    conn = get_conn()
    cur = conn.cursor()
    try:
        return jsonify(_fetch_by_category_payload(cur, t, island_only=_request_island_only()))
    finally:
        cur.close()
        conn.close()


@app.route("/od-dashboard-api/by_purpose_motif")
def api_by_purpose_motif():
    """Aggregate emissions (and distance) by enriched OD motif; LEFT JOIN enrichment on route leg key."""
    t = _tables_from_request()
    if isinstance(t, tuple):
        return t
    try:
        lim = int(request.args.get("limit", "24") or "24")
    except ValueError:
        lim = 24
    lim = max(5, min(lim, 60))
    conn = get_conn()
    cur = conn.cursor()
    try:
        return jsonify(_fetch_purpose_motif_payload(cur, t, lim))
    finally:
        cur.close()
        conn.close()


@app.route("/od-dashboard-api/zone_codes")
def api_zone_codes():
    return jsonify({"zone_codes": _zone_code_index(), "zone_names": _zone_name_index()})


@app.route("/od-dashboard-api/bootstrap")
def api_bootstrap():
    """Single round-trip: stats + by_category + by_purpose_motif (one DB connection)."""
    t = _tables_from_request()
    if isinstance(t, tuple):
        return t
    try:
        lim = int(request.args.get("motif_limit", "24") or "24")
    except ValueError:
        lim = 24
    lim = max(5, min(lim, 60))
    conn = get_conn()
    cur = conn.cursor()
    try:
        isl = _request_island_only()
        stats = _fetch_stats_payload(cur, t, island_only=isl)
        by_category = _fetch_by_category_payload(cur, t, island_only=isl)
        purpose_motif = _fetch_purpose_motif_payload(cur, t, lim)
        return jsonify(
            {
                "stats": stats,
                "by_category": by_category,
                "purpose_motif": purpose_motif,
                "zone_codes": _zone_code_index(),
                "zone_names": _zone_name_index(),
            }
        )
    finally:
        cur.close()
        conn.close()


@app.route("/od-dashboard-api/zone_map")
def api_zone_map():
    t = _tables_from_request()
    if isinstance(t, tuple):
        return t
    fam = t.get("family", "building")
    em = t.get("emissions")
    routes_for_dist = (t.get("routes") or "").strip()
    zone_by_for_dist = "rules"
    use_z_agg_dist = True
    has_attr_geo = False
    has_dest_geo_col = False
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
    conn = get_conn()
    cur = conn.cursor()
    use_geo_c = _table_exists(cur, "geo_zone_centroids")
    island_only_zone_map = (request.args.get("island_only", "1") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    if fam == "micro":
        if use_geo_c:
            base_from = f"""
            SELECT e.dest_geo_id::text AS geo_id, c.lat::double precision AS lat, c.lon::double precision AS lon,
                   SUM(e.emissions_g)::double precision AS total_emissions_g, COUNT(*)::bigint AS trips
            FROM {SCHEMA}.{em} e
            JOIN {SCHEMA}.geo_zone_centroids c ON c.geo_id::text = e.dest_geo_id::text
            WHERE trim(e.mode_group::text) IN {CAR_MODE_GROUPS} AND e.emissions_g > 0
            GROUP BY e.dest_geo_id, c.lat, c.lon
            """
        elif em and _column_exists(cur, em, "dest_lat") and _column_exists(cur, em, "dest_lon"):
            base_from = f"""
            SELECT e.dest_geo_id::text AS geo_id,
                   AVG(e.dest_lat)::double precision AS lat, AVG(e.dest_lon)::double precision AS lon,
                   SUM(e.emissions_g)::double precision AS total_emissions_g, COUNT(*)::bigint AS trips
            FROM {SCHEMA}.{em} e
            WHERE trim(e.mode_group::text) IN {CAR_MODE_GROUPS} AND e.emissions_g > 0
              AND e.dest_lat IS NOT NULL AND e.dest_lon IS NOT NULL
            GROUP BY e.dest_geo_id
            """
        else:
            base_from = "SELECT NULL::text, NULL::float8, NULL::float8, NULL::float8, NULL::bigint WHERE false"
        if "WHERE false" in base_from:
            cur.execute(base_from)
        elif max_g is not None and max_g >= min_g:
            cur.execute(base_from + " HAVING SUM(e.emissions_g) >= %s AND SUM(e.emissions_g) <= %s", (min_g, max_g))
        else:
            cur.execute(base_from + " HAVING SUM(e.emissions_g) >= %s", (min_g,))

    elif fam == "routes_only":
        rt = t["routes"]
        routes_for_dist = rt
        zone_by = _normalize_zone_by(request.args.get("zone_by") or "")
        zone_by_for_dist = zone_by
        use_dest_zone = zone_by == "dest"
        has_attr_geo = _column_exists(cur, rt, "route_attributed_geo_id")
        has_dest_geo_col = _column_exists(cur, rt, "route_dest_geo_id")
        zone_geo_sql = _routes_map_zone_geo_id_sql(
            "r",
            zone_by="dest" if use_dest_zone else "rules",
            has_route_attributed_geo_id=has_attr_geo and not use_dest_zone,
            has_route_dest_geo_id=has_dest_geo_col,
        )
        zt_req = (request.args.get("zone_table") or "").strip()
        if zt_req:
            zt = zt_req
        elif use_dest_zone:
            zt = _pick_zone_emissions_route_assignment(cur, rt, zone_kind="dest") or (
                t.get("zone") or ""
            ).strip()
        else:
            zt = (t.get("zone") or "").strip() or _pick_zone_emissions_route_assignment(
                cur, rt, zone_kind="rules"
            )
        r_cte, ic, ig, i_ll, i_havg, _i_route, isl_params = _routes_only_island_cte_and_predicates(
            island_only_zone_map
        )
        use_z_agg = bool(
            zt
            and (
                zt.startswith("zone_emissions_route_assignment")
                or zt.startswith("zone_emissions_rules")
                or zt.startswith("zone_emissions_meeting")
                or zt.startswith("zone_emissions_dest")
            )
            and _table_exists(cur, zt)
            and _column_exists(cur, zt, "geo_id")
            and _column_exists(cur, zt, "emissions_g")
        )
        if use_z_agg:
            # If the zone table exists but is empty (common before refresh), fall back to
            # on-the-fly aggregation from routes so the map still renders.
            cur.execute(
                f"SELECT EXISTS (SELECT 1 FROM {SCHEMA}.{zt} z WHERE z.emissions_g IS NOT NULL AND z.emissions_g > 0 LIMIT 1)"
            )
            if not cur.fetchone()[0]:
                use_z_agg = False
        use_z_agg_dist = False
        has_z_dist = False
        dist_z_sql = "0::double precision AS total_distance_km"
        if use_z_agg:
            has_trips = _column_exists(cur, zt, "trips")
            trips_sql = "z.trips::bigint AS trips" if has_trips else "0::bigint AS trips"
            has_z_dist = _column_exists(cur, zt, "distance_km")
            dist_z_sql = (
                "z.distance_km::double precision AS total_distance_km"
                if has_z_dist
                else "0::double precision AS total_distance_km"
            )
            use_z_agg_dist = has_z_dist
            # Precompute zone_emissions_* tables are island-scoped at build time.
            _zagg_skip_island = island_only_zone_map
            _zagg_cte = "" if _zagg_skip_island else r_cte
            _zagg_ic = "" if _zagg_skip_island else ic
            _zagg_ig = "" if _zagg_skip_island else ig
            _zagg_i_ll = "" if _zagg_skip_island else i_ll
            _zagg_params = () if _zagg_skip_island else isl_params
            if use_geo_c:
                if max_g is not None and max_g >= min_g:
                    cur.execute(
                        _zagg_cte
                        + f"""
                        SELECT z.geo_id::text, c.lat::double precision AS lat, c.lon::double precision AS lon,
                               z.emissions_g::double precision AS total_emissions_g, {trips_sql}, {dist_z_sql}
                        FROM {SCHEMA}.{zt} z
                        JOIN {SCHEMA}.geo_zone_centroids c ON c.geo_id::text = z.geo_id::text
                        WHERE z.emissions_g >= %s AND z.emissions_g <= %s {_zagg_ic}
                        """,
                        _zagg_params + (min_g, max_g),
                    )
                else:
                    cur.execute(
                        _zagg_cte
                        + f"""
                        SELECT z.geo_id::text, c.lat::double precision AS lat, c.lon::double precision AS lon,
                               z.emissions_g::double precision AS total_emissions_g, {trips_sql}, {dist_z_sql}
                        FROM {SCHEMA}.{zt} z
                        JOIN {SCHEMA}.geo_zone_centroids c ON c.geo_id::text = z.geo_id::text
                        WHERE z.emissions_g >= %s {_zagg_ic}
                        """,
                        _zagg_params + (min_g,),
                    )
            elif _table_exists(cur, "popgen_zones_geom") and _column_exists(cur, "popgen_zones_geom", "geom"):
                if max_g is not None and max_g >= min_g:
                    cur.execute(
                        _zagg_cte
                        + f"""
                        SELECT z.geo_id::text,
                               ST_Y(ST_Centroid(g.geom::geometry))::double precision AS lat,
                               ST_X(ST_Centroid(g.geom::geometry))::double precision AS lon,
                               z.emissions_g::double precision AS total_emissions_g, {trips_sql}, {dist_z_sql}
                        FROM {SCHEMA}.{zt} z
                        JOIN {SCHEMA}.popgen_zones_geom g ON g.geo_id::text = z.geo_id::text
                        WHERE z.emissions_g >= %s AND z.emissions_g <= %s {_zagg_ig}
                        """,
                        _zagg_params + (min_g, max_g),
                    )
                else:
                    cur.execute(
                        _zagg_cte
                        + f"""
                        SELECT z.geo_id::text,
                               ST_Y(ST_Centroid(g.geom::geometry))::double precision AS lat,
                               ST_X(ST_Centroid(g.geom::geometry))::double precision AS lon,
                               z.emissions_g::double precision AS total_emissions_g, {trips_sql}, {dist_z_sql}
                        FROM {SCHEMA}.{zt} z
                        JOIN {SCHEMA}.popgen_zones_geom g ON g.geo_id::text = z.geo_id::text
                        WHERE z.emissions_g >= %s {_zagg_ig}
                        """,
                        _zagg_params + (min_g,),
                    )
            elif _column_exists(cur, rt, "dest_lat") and _column_exists(cur, rt, "dest_lon"):
                if max_g is not None and max_g >= min_g:
                    cur.execute(
                        _zagg_cte
                        + f"""
                        SELECT z.geo_id::text, ll.lat::double precision AS lat, ll.lon::double precision AS lon,
                               z.emissions_g::double precision AS total_emissions_g, {trips_sql}, {dist_z_sql}
                        FROM {SCHEMA}.{zt} z
                        INNER JOIN (
                            SELECT ({zone_geo_sql})::text AS gid,
                                   AVG(r.dest_lat)::double precision AS lat,
                                   AVG(r.dest_lon)::double precision AS lon
                            FROM {SCHEMA}.{rt} r
                            WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                              AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                              AND r.dest_lat IS NOT NULL AND r.dest_lon IS NOT NULL
                            GROUP BY 1
                        ) ll ON ll.gid = z.geo_id::text
                        WHERE z.emissions_g >= %s AND z.emissions_g <= %s {_zagg_i_ll}
                        """,
                        _zagg_params + (min_g, max_g),
                    )
                else:
                    cur.execute(
                        _zagg_cte
                        + f"""
                        SELECT z.geo_id::text, ll.lat::double precision AS lat, ll.lon::double precision AS lon,
                               z.emissions_g::double precision AS total_emissions_g, {trips_sql}, {dist_z_sql}
                        FROM {SCHEMA}.{zt} z
                        INNER JOIN (
                            SELECT ({zone_geo_sql})::text AS gid,
                                   AVG(r.dest_lat)::double precision AS lat,
                                   AVG(r.dest_lon)::double precision AS lon
                            FROM {SCHEMA}.{rt} r
                            WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                              AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                              AND r.dest_lat IS NOT NULL AND r.dest_lon IS NOT NULL
                            GROUP BY 1
                        ) ll ON ll.gid = z.geo_id::text
                        WHERE z.emissions_g >= %s {_zagg_i_ll}
                        """,
                        _zagg_params + (min_g,),
                    )
            else:
                cur.execute(
                    "SELECT NULL::text, NULL::float8, NULL::float8, NULL::float8, NULL::bigint WHERE false"
                )
        elif use_geo_c:
            base_from = f"""
            SELECT ({zone_geo_sql})::text AS geo_id, c.lat::double precision AS lat, c.lon::double precision AS lon,
                   SUM(r.route_emissions_g)::double precision AS total_emissions_g, COUNT(*)::bigint AS trips,
                   COALESCE(SUM(r.distance_m), 0)::double precision / 1000.0 AS total_distance_km
            FROM {SCHEMA}.{rt} r
            JOIN {SCHEMA}.geo_zone_centroids c ON c.geo_id::text = ({zone_geo_sql})
            WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
              AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0 {ic}
            GROUP BY 1, 2, 3
            """
            if max_g is not None and max_g >= min_g:
                cur.execute(
                    r_cte + base_from + " HAVING SUM(r.route_emissions_g) >= %s AND SUM(r.route_emissions_g) <= %s",
                    isl_params + (min_g, max_g),
                )
            else:
                cur.execute(r_cte + base_from + " HAVING SUM(r.route_emissions_g) >= %s", isl_params + (min_g,))
        elif _column_exists(cur, rt, "dest_lat") and _column_exists(cur, rt, "dest_lon"):
            base_from = f"""
            SELECT ({zone_geo_sql})::text AS geo_id,
                   AVG(r.dest_lat)::double precision AS lat, AVG(r.dest_lon)::double precision AS lon,
                   SUM(r.route_emissions_g)::double precision AS total_emissions_g, COUNT(*)::bigint AS trips,
                   COALESCE(SUM(r.distance_m), 0)::double precision / 1000.0 AS total_distance_km
            FROM {SCHEMA}.{rt} r
            WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
              AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
              AND r.dest_lat IS NOT NULL AND r.dest_lon IS NOT NULL
            GROUP BY 1
            """
            if max_g is not None and max_g >= min_g:
                cur.execute(
                    r_cte
                    + base_from
                    + " HAVING SUM(r.route_emissions_g) >= %s AND SUM(r.route_emissions_g) <= %s"
                    + i_havg,
                    isl_params + (min_g, max_g),
                )
            else:
                cur.execute(
                    r_cte + base_from + " HAVING SUM(r.route_emissions_g) >= %s" + i_havg,
                    isl_params + (min_g,),
                )
        else:
            cur.execute(
                "SELECT NULL::text, NULL::float8, NULL::float8, NULL::float8, NULL::bigint WHERE false"
            )

    else:
        table = request.args.get("zone_table", t["zone"])
        if use_geo_c:
            if max_g is not None and max_g >= min_g:
                cur.execute(
                    f"""
                    SELECT z.geo_id, c.lat, c.lon, z.emissions_g AS total_emissions_g, z.trips AS trips
                    FROM {SCHEMA}.{table} z
                    JOIN {SCHEMA}.geo_zone_centroids c ON c.geo_id::text = z.geo_id::text
                    WHERE z.emissions_g >= %s AND z.emissions_g <= %s
                    """,
                    (min_g, max_g),
                )
            else:
                cur.execute(
                    f"""
                    SELECT z.geo_id, c.lat, c.lon, z.emissions_g AS total_emissions_g, z.trips AS trips
                    FROM {SCHEMA}.{table} z
                    JOIN {SCHEMA}.geo_zone_centroids c ON c.geo_id::text = z.geo_id::text
                    WHERE z.emissions_g >= %s
                    """,
                    (min_g,),
                )
        elif _table_exists(cur, "popgen_zones_geom") and _column_exists(cur, "popgen_zones_geom", "geom"):
            if max_g is not None and max_g >= min_g:
                cur.execute(
                    f"""
                    SELECT z.geo_id::text,
                           ST_Y(ST_Centroid(g.geom::geometry))::double precision AS lat,
                           ST_X(ST_Centroid(g.geom::geometry))::double precision AS lon,
                           z.emissions_g AS total_emissions_g, z.trips AS trips
                    FROM {SCHEMA}.{table} z
                    JOIN {SCHEMA}.popgen_zones_geom g ON g.geo_id::text = z.geo_id::text
                    WHERE z.emissions_g >= %s AND z.emissions_g <= %s
                    """,
                    (min_g, max_g),
                )
            else:
                cur.execute(
                    f"""
                    SELECT z.geo_id::text,
                           ST_Y(ST_Centroid(g.geom::geometry))::double precision AS lat,
                           ST_X(ST_Centroid(g.geom::geometry))::double precision AS lon,
                           z.emissions_g AS total_emissions_g, z.trips AS trips
                    FROM {SCHEMA}.{table} z
                    JOIN {SCHEMA}.popgen_zones_geom g ON g.geo_id::text = z.geo_id::text
                    WHERE z.emissions_g >= %s
                    """,
                    (min_g,),
                )
        else:
            cur.execute(
                "SELECT NULL::text, NULL::float8, NULL::float8, NULL::float8, NULL::bigint WHERE false"
            )

    rows = cur.fetchall()
    out = []
    for r in rows:
        if r[0] is None or r[1] is None or r[2] is None:
            continue
        out.append(
            _attach_zone_code(
                {
                    "geo_id": r[0],
                    "lat": float(r[1]),
                    "lon": float(r[2]),
                    "total_emissions_g": float(r[3] or 0),
                    "trips": r[4],
                    "total_distance_km": round(float(r[5] or 0), 2) if len(r) > 5 else 0.0,
                }
            )
        )
    if fam == "routes_only" and out and routes_for_dist:
        # Precompute tables with distance_km are fast; only scan routes when missing or zero.
        if use_z_agg_dist:
            need_dist = [z for z in out if not float(z.get("total_distance_km") or 0)]
            if need_dist:
                _enrich_zones_distance_km(
                    cur,
                    routes_for_dist,
                    need_dist,
                    zone_by=zone_by_for_dist,
                    has_route_attributed_geo_id=has_attr_geo,
                    has_route_dest_geo_id=has_dest_geo_col,
                )
        else:
            _enrich_zones_distance_km(
                cur,
                routes_for_dist,
                out,
                zone_by=zone_by_for_dist,
                has_route_attributed_geo_id=has_attr_geo,
                has_route_dest_geo_id=has_dest_geo_col,
            )
    include_geojson = (request.args.get("include_geojson", "1") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    geojson_fc = None
    try:
        if include_geojson and out:
            clip_gj = None
            if fam == "routes_only" and island_only_zone_map:
                clip_gj = _montreal_island_geometry_geojson_for_postgis()
            geojson_fc = _build_zone_map_geojson(cur, out, island_clip_geojson=clip_gj)
    finally:
        cur.close()
        conn.close()
    zone_distance_by_id = {
        str(z["geo_id"]): round(float(z.get("total_distance_km") or 0), 2) for z in out
    }
    return jsonify(
        {
            "zones": out,
            "geojson": geojson_fc,
            "zone_distance_by_id": zone_distance_by_id,
        }
    )


@app.route("/od-dashboard-api/building_map")
def api_building_map():
    """Point emissions heatmap data aggregated by building (rules or destination)."""
    t = _tables_from_request()
    if isinstance(t, tuple):
        return t
    fam = t.get("family", "building")
    if fam != "routes_only":
        return jsonify(
            {
                "error": "building_map_unavailable",
                "message": "Building heatmaps require routes_only family (trip_routes_building* + route_emissions_g).",
            }
        ), 400

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
        row_limit = int(request.args.get("limit", "400000") or "400000")
    except Exception:
        row_limit = 400000
    row_limit = max(1000, min(row_limit, 500000))
    grid_cell_deg = _parse_building_grid_cell_deg(request.args.get("grid_cell_deg"))
    zone_geo_id = (request.args.get("zone_geo_id") or "").strip()

    building_by = _normalize_building_by(request.args.get("building_by") or "rules")
    island_only = _request_island_only(default=True)
    rt = t["routes"]
    buildings_rel = BUILDINGS_TABLE

    conn = get_conn()
    cur = conn.cursor()
    bt = _pick_building_emissions_table(cur, rt, building_by=building_by)
    lat_sql = _building_map_lat_sql("b")
    lon_sql = _building_map_lon_sql("b")
    include_footprints = bool(zone_geo_id)
    geom_sql = _building_footprint_geojson_sql("b") if include_footprints else "NULL::text"
    island_cte, island_pred, island_params = ("", "", ())
    # Zone drill-down already scopes to one PopGen zone; skip costly per-row island ST filter.
    if island_only and not zone_geo_id:
        island_cte, island_pred, island_params = _building_island_cte_and_predicate(lat_sql, lon_sql)
    zone_pred, zone_params = _building_zone_filter_sql(cur, lat_sql, lon_sql, zone_geo_id)

    use_precomputed = bool(bt and _table_exists(cur, bt))
    if use_precomputed:
        cur.execute(
            f"SELECT EXISTS (SELECT 1 FROM {SCHEMA}.{bt} z WHERE z.emissions_g IS NOT NULL AND z.emissions_g > 0 LIMIT 1)"
        )
        if not cur.fetchone()[0]:
            use_precomputed = False

    truncated = False
    if grid_cell_deg is not None:
        cell = float(grid_cell_deg)
        emax = ""
        params: list = list(island_params) + [min_g]
        if max_g is not None and max_g >= min_g:
            emax = " AND e.emissions_g <= %s"
            params.append(max_g)
        params.extend(zone_params)
        if use_precomputed:
            has_trips = _column_exists(cur, bt, "trips")
            trips_sql = "e.trips::bigint" if has_trips else "0::bigint"
            cur.execute(
                island_cte
                + f"""
                SELECT ((FLOOR(({lat_sql})::double precision / {cell}) * {cell}) + ({cell} / 2.0))::double precision AS lat,
                       ((FLOOR(({lon_sql})::double precision / {cell}) * {cell}) + ({cell} / 2.0))::double precision AS lon,
                       SUM(e.emissions_g)::double precision AS total_emissions_g,
                       SUM({trips_sql})::bigint AS trips,
                       COUNT(*)::bigint AS building_count
                FROM {SCHEMA}.{bt} AS e
                JOIN {SCHEMA}.{buildings_rel} AS b ON b.id::text = e.building_id
                WHERE e.emissions_g >= %s{emax}
                  AND b.geometry IS NOT NULL{island_pred}{zone_pred}
                GROUP BY FLOOR(({lat_sql})::double precision / {cell}), FLOOR(({lon_sql})::double precision / {cell})
                ORDER BY SUM(e.emissions_g) DESC
                """,
                tuple(params),
            )
        else:
            agg_sql = (
                routes_building_dest_aggregate_sql(rt, min_emissions_g=min_g)
                if building_by == "dest"
                else routes_building_rules_aggregate_sql(rt, min_emissions_g=min_g)
            )
            where = "WHERE b.geometry IS NOT NULL"
            qparams: list = list(island_params)
            if max_g is not None and max_g >= min_g:
                where += " AND e.emissions_g <= %s"
                qparams.append(max_g)
            if island_only:
                where += island_pred
            where += zone_pred
            qparams.extend(zone_params)
            cur.execute(
                island_cte
                + f"""
                SELECT ((FLOOR(({lat_sql})::double precision / {cell}) * {cell}) + ({cell} / 2.0))::double precision AS lat,
                       ((FLOOR(({lon_sql})::double precision / {cell}) * {cell}) + ({cell} / 2.0))::double precision AS lon,
                       SUM(e.emissions_g)::double precision AS total_emissions_g,
                       SUM(e.trips)::bigint AS trips,
                       COUNT(*)::bigint AS building_count
                FROM ({agg_sql}) AS e
                JOIN {SCHEMA}.{buildings_rel} AS b ON b.id::text = e.building_id::text
                {where}
                GROUP BY FLOOR(({lat_sql})::double precision / {cell}), FLOOR(({lon_sql})::double precision / {cell})
                ORDER BY SUM(e.emissions_g) DESC
                """,
                tuple(qparams),
            )
        rows = cur.fetchall()
        cells = []
        total_buildings = 0
        for r in rows:
            if r[0] is None or r[1] is None:
                continue
            bc = int(r[4] or 0)
            total_buildings += bc
            cells.append(
                {
                    "lat": float(r[0]),
                    "lon": float(r[1]),
                    "total_emissions_g": float(r[2] or 0),
                    "trips": r[3],
                    "building_count": bc,
                }
            )
        cur.close()
        conn.close()
        return jsonify(
            {
                "cells": cells,
                "mode": "grid",
                "grid_cell_deg": grid_cell_deg,
                "building_by": building_by,
                "source_table": bt if use_precomputed else rt,
                "truncated": False,
                "building_count": total_buildings,
                "zone_geo_id": zone_geo_id or None,
            }
        )

    if use_precomputed:
        has_trips = _column_exists(cur, bt, "trips")
        trips_sql = "e.trips::bigint AS trips" if has_trips else "0::bigint AS trips"
        has_b_dist = _column_exists(cur, bt, "distance_km")
        dist_b_sql = (
            "e.distance_km::double precision AS distance_km"
            if has_b_dist
            else "0::double precision AS distance_km"
        )
        emax = ""
        params: list = list(island_params) + [min_g]
        if max_g is not None and max_g >= min_g:
            emax = " AND e.emissions_g <= %s"
            params.append(max_g)
        params.extend(zone_params)
        island_sql = island_pred if island_only else ""
        zone_sql = zone_pred
        filtered_cte = (
            island_cte.rstrip() + ",\n            filtered AS (\n"
            if island_cte
            else "WITH filtered AS (\n"
        )
        cur.execute(
            filtered_cte
            + f"""
                SELECT e.building_id::text AS building_id,
                       e.emissions_g::double precision AS emissions_g,
                       {trips_sql},
                       {dist_b_sql}
                FROM {SCHEMA}.{bt} AS e
                JOIN {SCHEMA}.{buildings_rel} AS b ON b.id::text = e.building_id
                WHERE e.emissions_g >= %s{emax}
                  AND b.geometry IS NOT NULL{island_sql}{zone_sql}
            )
            SELECT t.building_id,
                   {lat_sql}::double precision AS lat,
                   {lon_sql}::double precision AS lon,
                   t.emissions_g AS total_emissions_g,
                   t.trips,
                   split_part(trim(b.zone_geo_id::text), '.', 1) AS zone_geo_id,
                   t.distance_km AS total_distance_km,
                   {geom_sql} AS geom_json
            FROM filtered AS t
            JOIN {SCHEMA}.{buildings_rel} AS b ON b.id::text = t.building_id
            WHERE b.geometry IS NOT NULL
            ORDER BY t.emissions_g DESC
            LIMIT %s
            """,
            tuple(params + [row_limit + 1]),
        )
        rows = cur.fetchall()
        if len(rows) > row_limit:
            truncated = True
            rows = rows[:row_limit]
    else:
        agg_sql = (
            routes_building_dest_aggregate_sql(rt, min_emissions_g=min_g)
            if building_by == "dest"
            else routes_building_rules_aggregate_sql(rt, min_emissions_g=min_g)
        )
        where = "WHERE b.geometry IS NOT NULL"
        params: list = list(island_params)
        if max_g is not None and max_g >= min_g:
            where += " AND e.emissions_g <= %s"
            params.append(max_g)
        if island_only:
            where += island_pred
        where += zone_pred
        params.extend(zone_params)
        cur.execute(
            island_cte
            + f"""
            SELECT e.building_id::text,
                   {lat_sql}::double precision AS lat,
                   {lon_sql}::double precision AS lon,
                   e.emissions_g::double precision AS total_emissions_g,
                   e.trips::bigint AS trips,
                   split_part(trim(b.zone_geo_id::text), '.', 1) AS zone_geo_id,
                   e.distance_km::double precision AS total_distance_km,
                   {geom_sql} AS geom_json
            FROM ({agg_sql}) AS e
            JOIN {SCHEMA}.{buildings_rel} AS b ON b.id::text = e.building_id::text
            {where}
            ORDER BY e.emissions_g DESC
            LIMIT %s
            """,
            tuple(params + [row_limit + 1]),
        )
        rows = cur.fetchall()
        if len(rows) > row_limit:
            truncated = True
            rows = rows[:row_limit]

    out = []
    footprint_features: list[dict] = []
    for r in rows:
        if r[0] is None or r[1] is None or r[2] is None:
            continue
        item = {
            "building_id": r[0],
            "lat": float(r[1]),
            "lon": float(r[2]),
            "total_emissions_g": float(r[3] or 0),
            "trips": r[4],
            "zone_geo_id": r[5],
            "total_distance_km": round(float(r[6] or 0), 2) if len(r) > 6 else 0.0,
        }
        out.append(item)
        if include_footprints and len(r) > 7 and r[7]:
            feat = _geom_json_to_feature(
                r[7],
                {
                    "building_id": item["building_id"],
                    "zone_geo_id": item["zone_geo_id"],
                    "total_emissions_g": item["total_emissions_g"],
                    "trips": item["trips"],
                    "total_distance_km": item["total_distance_km"],
                },
            )
            if feat:
                footprint_features.append(feat)
    cur.close()
    conn.close()
    payload = {
        "buildings": out,
        "building_by": building_by,
        "source_table": bt if use_precomputed else rt,
        "truncated": truncated,
        "limit": row_limit,
        "zone_geo_id": zone_geo_id or None,
        "point_source": "footprint",
    }
    if include_footprints:
        payload["footprint_fc"] = {"type": "FeatureCollection", "features": footprint_features}
    return jsonify(payload)


def _building_footprint_geom_sql(b_alias: str = "b") -> str:
    """WGS84 GeoJSON for building footprint polygon (handles non-4326 storage)."""
    return _building_footprint_geojson_sql(b_alias)


_GEOCODE_CACHE: dict[tuple[float, float], str | None] = {}
_LAST_NOMINATIM_TS = 0.0


def _reverse_geocode_enabled() -> bool:
    return os.environ.get("DASHBOARD_REVERSE_GEOCODE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _format_nominatim_address(payload: dict) -> str:
    if not payload:
        return ""
    addr = payload.get("address") or {}
    civic = str(addr.get("house_number") or addr.get("house_name") or "").strip()
    road = str(
        addr.get("road") or addr.get("pedestrian") or addr.get("footway") or ""
    ).strip()
    line1 = f"{civic} {road}".strip() if civic or road else ""
    place = str(
        addr.get("suburb")
        or addr.get("neighbourhood")
        or addr.get("town")
        or addr.get("city")
        or addr.get("municipality")
        or ""
    ).strip()
    if line1 and place:
        return f"{line1}, {place}"
    if line1:
        return line1
    display = str(payload.get("display_name") or "").strip()
    if display:
        parts = [p.strip() for p in display.split(",") if p.strip()]
        return ", ".join(parts[:3]) if len(parts) > 3 else display
    return ""


def _reverse_geocode_address(lat: float | None, lon: float | None) -> str | None:
    if lat is None or lon is None or not _reverse_geocode_enabled():
        return None
    key = (round(float(lat), 5), round(float(lon), 5))
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    global _LAST_NOMINATIM_TS
    now = time.monotonic()
    wait = 1.05 - (now - _LAST_NOMINATIM_TS)
    if wait > 0:
        time.sleep(wait)
    url = (
        "https://nominatim.openstreetmap.org/reverse?"
        + urllib.parse.urlencode(
            {
                "lat": key[0],
                "lon": key[1],
                "format": "jsonv2",
                "zoom": 18,
                "addressdetails": 1,
            }
        )
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "PopGen2023-Dashboard/1.0 (building-info)"},
    )
    address: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        formatted = _format_nominatim_address(payload)
        address = formatted or None
    except Exception:
        address = None
    finally:
        _LAST_NOMINATIM_TS = time.monotonic()
    _GEOCODE_CACHE[key] = address
    return address


def _building_address_from_footprint_row(row: tuple) -> str | None:
    """Resolve street address from DB column or OSM reverse-geocode of footprint centroid."""
    stored = row[5]
    if stored and str(stored).strip():
        return str(stored).strip()
    lat_v, lon_v = row[10], row[11]
    if lat_v is None or lon_v is None:
        return None
    return _reverse_geocode_address(float(lat_v), float(lon_v))


def _fetch_building_footprint_row(cur, building_id: str) -> tuple | None:
    buildings_rel = BUILDINGS_TABLE
    lat_sql = _building_map_lat_sql("b")
    lon_sql = _building_map_lon_sql("b")
    geom_sql = _building_footprint_geom_sql("b")

    def col(alias: str, name: str, expr: str | None = None) -> str:
        if _column_exists(cur, buildings_rel, name):
            return (expr or f"b.{name}::text") + f" AS {alias}"
        return f"NULL::text AS {alias}"

    def num_col(alias: str, name: str) -> str:
        if _column_exists(cur, buildings_rel, name):
            return f"b.{name}::double precision AS {alias}"
        return f"NULL::double precision AS {alias}"

    cur.execute(
        f"""
        SELECT {col("building_id", "id", "b.id::text")},
               split_part(trim(b.zone_geo_id::text), '.', 1) AS zone_geo_id,
               {col("building_type", "type")},
               {col("use_class", "use_class")},
               {col("name", "name")},
               {col("address", "address")},
               {col("csdname", "csdname")},
               {num_col("units", "units")},
               {num_col("floors", "floors")},
               {num_col("sq_ft", "sq_ft")},
               {lat_sql}::double precision AS lat,
               {lon_sql}::double precision AS lon,
               {geom_sql} AS geom_json
        FROM {SCHEMA}.{buildings_rel} AS b
        WHERE b.id::text = %s AND b.geometry IS NOT NULL
        LIMIT 1
        """,
        (building_id,),
    )
    return cur.fetchone()


def _geom_json_to_feature(geom_json: str | None, props: dict) -> dict | None:
    if not geom_json:
        return None
    try:
        geometry = json.loads(geom_json)
    except json.JSONDecodeError:
        return None
    if not geometry or not geometry.get("type"):
        return None
    return {"type": "Feature", "geometry": geometry, "properties": props}


@app.route("/od-dashboard-api/building_footprint")
def api_building_footprint():
    """Building footprint polygon only (for map highlight)."""
    building_id = (request.args.get("building_id") or "").strip()
    if not building_id:
        return jsonify({"error": "building_id_required", "message": "building_id is required"}), 400

    conn = get_conn()
    cur = conn.cursor()
    row = _fetch_building_footprint_row(cur, building_id)
    cur.close()
    conn.close()
    if not row:
        return jsonify({"error": "not_found", "message": f"Building {building_id} not found"}), 404

    feature = _geom_json_to_feature(
        row[12],
        {"building_id": row[0], "zone_geo_id": row[1]},
    )
    if not feature:
        return jsonify({"error": "no_geometry", "message": "Building footprint geometry unavailable"}), 404
    return jsonify({"building_id": row[0], "geojson": feature})


@app.route("/od-dashboard-api/building_detail")
def api_building_detail():
    """Footprint geometry + metadata for one building (map click / highlight)."""
    building_id = (request.args.get("building_id") or "").strip()
    if not building_id:
        return jsonify({"error": "building_id_required", "message": "building_id is required"}), 400

    t = _tables_from_request()
    if isinstance(t, tuple):
        return t

    building_by = _normalize_building_by(request.args.get("building_by") or "rules")

    conn = get_conn()
    cur = conn.cursor()
    row = _fetch_building_footprint_row(cur, building_id)
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "not_found", "message": f"Building {building_id} not found"}), 404

    trips = 0
    emissions_g = 0.0
    distance_km = 0.0
    fam = t.get("family", "building")
    if fam == "routes_only":
        rt = t["routes"]
        bt = _pick_building_emissions_table(cur, rt, building_by=building_by)
        if bt and _table_exists(cur, bt):
            has_trips = _column_exists(cur, bt, "trips")
            has_b_dist = _column_exists(cur, bt, "distance_km")
            trips_sql = "trips::bigint" if has_trips else "0::bigint"
            dist_sql = (
                "distance_km::double precision"
                if has_b_dist
                else "0::double precision"
            )
            cur.execute(
                f"SELECT emissions_g::double precision, {trips_sql}, {dist_sql} "
                f"FROM {SCHEMA}.{bt} WHERE building_id::text = %s",
                (building_id,),
            )
            er = cur.fetchone()
            if er:
                emissions_g = float(er[0] or 0)
                trips = int(er[1] or 0)
                distance_km = float(er[2] or 0)
        if distance_km <= 0 and rt:
            _eg, _tr, distance_km = _fetch_building_route_totals(
                cur, rt, building_id, building_by=building_by
            )
            if emissions_g <= 0:
                emissions_g = _eg
            if trips <= 0:
                trips = _tr

    cur.close()
    conn.close()

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
        "trips": trips,
        "total_emissions_g": emissions_g,
        "total_distance_km": round(distance_km, 2),
    }
    geojson_fc = _geom_json_to_feature(
        row[12],
        {"building_id": building["building_id"], "zone_geo_id": building["zone_geo_id"]},
    )
    return jsonify({"building": building, "geojson": geojson_fc})


@app.route("/od-dashboard-api/od_flows")
def api_od_flows():
    try:
        limit = int(request.args.get("limit", "150") or "150")
    except Exception:
        limit = 150
    limit = max(10, min(limit, 500))
    weight_by = (request.args.get("weight_by", "emissions") or "emissions").strip().lower()
    if weight_by not in ("emissions", "trips"):
        weight_by = "emissions"

    t = _tables_from_request()
    if isinstance(t, tuple):
        return t
    fam = t.get("family", "building")

    conn = get_conn()
    cur = conn.cursor()
    use_geo_c = _table_exists(cur, "geo_zone_centroids")
    isl = _request_island_only(default=False)
    # Fast island filter for OD flows: bbox on both endpoints.
    bbox = ""
    if isl:
        bbox = f"""
                  AND r.orig_lat IS NOT NULL AND r.orig_lon IS NOT NULL
                  AND r.dest_lat IS NOT NULL AND r.dest_lon IS NOT NULL
                  AND r.orig_lat::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
                  AND r.orig_lon::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX}
                  AND r.dest_lat::double precision BETWEEN {MONTREAL_ISLAND_LAT_MIN} AND {MONTREAL_ISLAND_LAT_MAX}
                  AND r.dest_lon::double precision BETWEEN {MONTREAL_ISLAND_LON_MIN} AND {MONTREAL_ISLAND_LON_MAX}
        """

    if fam == "routes_only":
        rt = t["routes"]
        summary_t = _pick_od_flow_summary_table(cur, rt)
        if summary_t:
            cur.execute(
                f"""
                SELECT s.orig_geo_id::text, s.dest_geo_id::text, s.trips::bigint,
                       s.total_emissions_g::double precision,
                       s.total_distance_km::double precision,
                       s.orig_lat::double precision, s.orig_lon::double precision,
                       s.dest_lat::double precision, s.dest_lon::double precision,
                       (COUNT(*) OVER())::bigint AS total_flow_pairs,
                       (COALESCE(SUM(s.trips) OVER(), 0))::bigint AS total_interzonal_trips
                FROM {SCHEMA}.{summary_t} s
                WHERE s.orig_lat IS NOT NULL AND s.orig_lon IS NOT NULL
                  AND s.dest_lat IS NOT NULL AND s.dest_lon IS NOT NULL
                  {"AND s.orig_lat::double precision BETWEEN " + str(MONTREAL_ISLAND_LAT_MIN) + " AND " + str(MONTREAL_ISLAND_LAT_MAX) if isl else ""}
                  {"AND s.orig_lon::double precision BETWEEN " + str(MONTREAL_ISLAND_LON_MIN) + " AND " + str(MONTREAL_ISLAND_LON_MAX) if isl else ""}
                  {"AND s.dest_lat::double precision BETWEEN " + str(MONTREAL_ISLAND_LAT_MIN) + " AND " + str(MONTREAL_ISLAND_LAT_MAX) if isl else ""}
                  {"AND s.dest_lon::double precision BETWEEN " + str(MONTREAL_ISLAND_LON_MIN) + " AND " + str(MONTREAL_ISLAND_LON_MAX) if isl else ""}
                ORDER BY CASE WHEN %s = 'trips'
                              THEN s.trips::double precision
                              ELSE s.total_emissions_g::double precision
                         END DESC
                LIMIT %s
                """,
                (weight_by, limit),
            )
            rows = cur.fetchall()
            totals = (rows[0][9], rows[0][10]) if rows else (0, 0)
            cur.close()
            conn.close()
            flows = [
                {
                    "orig_geo_id": r[0],
                    "dest_geo_id": r[1],
                    "trips": int(r[2] or 0),
                    "total_emissions_g": float(r[3] or 0),
                    "total_distance_km": float(r[4] or 0),
                    "orig_lat": float(r[5]),
                    "orig_lon": float(r[6]),
                    "dest_lat": float(r[7]),
                    "dest_lon": float(r[8]),
                }
                for r in rows
            ]
            return jsonify(
                {
                    "weight_by": weight_by,
                    "limit": limit,
                    "summary_table": summary_t,
                    "total_interzonal_trips": int(totals[1] or 0),
                    "total_flow_pairs": int(totals[0] or 0),
                    "flows_shown_trips": sum(f["trips"] for f in flows),
                    "flow_count": len(flows),
                    "flows": flows,
                }
            )
        totals_from_rows = False
        if use_geo_c:
            flow_sql = f"""
            WITH agg AS (
                SELECT r.orig_geo_id::text AS orig_geo_id,
                       r.dest_geo_id::text AS dest_geo_id,
                       COUNT(*)::bigint AS trips,
                       COALESCE(SUM(r.route_emissions_g), 0)::double precision AS total_emissions_g,
                       COALESCE(SUM(r.distance_m), 0)::double precision / 1000.0 AS total_distance_km
                FROM {SCHEMA}.{rt} r
                WHERE r.orig_geo_id::text IS DISTINCT FROM r.dest_geo_id::text
                  AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                  AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                  {bbox}
                GROUP BY r.orig_geo_id, r.dest_geo_id
            )
            SELECT a.orig_geo_id, a.dest_geo_id, a.trips, a.total_emissions_g, a.total_distance_km,
                   co.lat AS orig_lat, co.lon AS orig_lon, cd.lat AS dest_lat, cd.lon AS dest_lon,
                   (COUNT(*) OVER())::bigint AS total_flow_pairs,
                   (COALESCE(SUM(a.trips) OVER(), 0))::bigint AS total_interzonal_trips
            FROM agg a
            JOIN {SCHEMA}.geo_zone_centroids co ON co.geo_id::text = a.orig_geo_id::text
            JOIN {SCHEMA}.geo_zone_centroids cd ON cd.geo_id::text = a.dest_geo_id::text
            ORDER BY CASE WHEN %s = 'trips' THEN a.trips::double precision ELSE a.total_emissions_g END DESC
            LIMIT %s
            """
            totals_from_rows = True
        else:
            flow_sql = f"""
            WITH orig_ll AS (
                SELECT r.orig_geo_id::text AS gid,
                       AVG(r.orig_lat)::double precision AS lat,
                       AVG(r.orig_lon)::double precision AS lon
                FROM {SCHEMA}.{rt} r
                WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                  AND r.orig_lat IS NOT NULL AND r.orig_lon IS NOT NULL
                GROUP BY r.orig_geo_id
            ),
            dest_ll AS (
                SELECT r.dest_geo_id::text AS gid,
                       AVG(r.dest_lat)::double precision AS lat,
                       AVG(r.dest_lon)::double precision AS lon
                FROM {SCHEMA}.{rt} r
                WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                  AND r.dest_lat IS NOT NULL AND r.dest_lon IS NOT NULL
                GROUP BY r.dest_geo_id
            ),
            agg AS (
                SELECT r.orig_geo_id::text AS orig_geo_id,
                       r.dest_geo_id::text AS dest_geo_id,
                       COUNT(*)::bigint AS trips,
                       COALESCE(SUM(r.route_emissions_g), 0)::double precision AS total_emissions_g,
                       COALESCE(SUM(r.distance_m), 0)::double precision / 1000.0 AS total_distance_km
                FROM {SCHEMA}.{rt} r
                WHERE r.orig_geo_id::text IS DISTINCT FROM r.dest_geo_id::text
                  AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                  AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                  {bbox}
                GROUP BY r.orig_geo_id, r.dest_geo_id
            )
            SELECT a.orig_geo_id, a.dest_geo_id, a.trips, a.total_emissions_g, a.total_distance_km,
                   co.lat AS orig_lat, co.lon AS orig_lon, cd.lat AS dest_lat, cd.lon AS dest_lon
            FROM agg a
            JOIN orig_ll co ON co.gid = a.orig_geo_id
            JOIN dest_ll cd ON cd.gid = a.dest_geo_id
            ORDER BY CASE WHEN %s = 'trips' THEN a.trips::double precision ELSE a.total_emissions_g END DESC
            LIMIT %s
            """
        cur.execute(flow_sql, (weight_by, limit))
        rows = cur.fetchall()
        if totals_from_rows:
            totals = (rows[0][9], rows[0][10]) if rows else (0, 0)
        else:
            cur.execute(
                f"""
                WITH agg AS (
                    SELECT r.orig_geo_id::text AS orig_geo_id,
                           r.dest_geo_id::text AS dest_geo_id,
                           COUNT(*)::bigint AS trips
                    FROM {SCHEMA}.{rt} r
                    WHERE r.orig_geo_id::text IS DISTINCT FROM r.dest_geo_id::text
                      AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                      AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                    GROUP BY r.orig_geo_id, r.dest_geo_id
                )
                SELECT COUNT(*)::bigint, COALESCE(SUM(trips), 0)::bigint FROM agg
                """
            )
            totals = cur.fetchone()
    else:
        em_t, trips_t, routes_t = t["emissions"], t["trips"], t["routes"]
        routes_join = bool(t.get("routes_join", True) and routes_t)
        rsql = (
            f"""
                LEFT JOIN {SCHEMA}.{routes_t} r
                  ON r.synthetic_person_id::text = e.synthetic_person_id::text
                 AND r.orig_geo_id = e.orig_geo_id::int AND r.dest_geo_id = e.dest_geo_id::int
                 AND COALESCE(r.purpose,'') = COALESCE(e.purpose::text,'')
                 AND COALESCE(r.dep_time_bin,'') = COALESCE(e.dep_time_bin::text,'')
            """
            if routes_join
            else ""
        )
        dsum = (
            "COALESCE(SUM(COALESCE(e.distance_m, r.distance_m)), 0)::double precision / 1000.0 AS total_distance_km"
            if routes_join
            else "COALESCE(SUM(e.distance_m), 0)::double precision / 1000.0 AS total_distance_km"
        )
        olat_expr = "COALESCE(r.orig_lat, t.orig_lat)" if routes_join else "t.orig_lat"
        olon_expr = "COALESCE(r.orig_lon, t.orig_lon)" if routes_join else "t.orig_lon"
        dlat_expr = "COALESCE(r.dest_lat, t.dest_lat)" if routes_join else "t.dest_lat"
        dlon_expr = "COALESCE(r.dest_lon, t.dest_lon)" if routes_join else "t.dest_lon"
        if fam == "micro":
            tot_eg = "COALESCE(SUM(e.emissions_g), 0)::double precision AS total_emissions_g"
            eg_where = "e.emissions_g > 0"
        else:
            tot_eg = "COALESCE(SUM(COALESCE(e.emissions_g_pair, e.emissions_g)), 0)::double precision AS total_emissions_g"
            eg_where = "COALESCE(e.emissions_g_pair, e.emissions_g) > 0"

        agg_cte = f"""
            WITH agg AS (
                SELECT t.orig_geo_id::text AS orig_geo_id,
                       t.dest_geo_id::text AS dest_geo_id,
                       COUNT(*)::bigint AS trips,
                       {tot_eg}, {dsum},
                       AVG({olat_expr})::double precision AS trip_orig_lat,
                       AVG({olon_expr})::double precision AS trip_orig_lon,
                       AVG({dlat_expr})::double precision AS trip_dest_lat,
                       AVG({dlon_expr})::double precision AS trip_dest_lon
                FROM {SCHEMA}.{trips_t} t
                INNER JOIN {SCHEMA}.{em_t} e ON t.synthetic_person_id::text = e.synthetic_person_id::text
                 AND t.orig_geo_id::int = e.orig_geo_id::int AND t.dest_geo_id::int = e.dest_geo_id::int
                 AND COALESCE(t.purpose::text,'') = COALESCE(e.purpose::text,'')
                 AND COALESCE(t.dep_time_bin::text,'') = COALESCE(e.dep_time_bin::text,'')
                 AND (e.hh_id IS NULL OR t.hh_id::text IS NOT DISTINCT FROM e.hh_id::text)
                {rsql}
                WHERE t.orig_geo_id::text IS DISTINCT FROM t.dest_geo_id::text
                  AND trim(COALESCE(e.mode_group::text, t.mode_group::text)) IN {CAR_MODE_GROUPS}
                  AND {eg_where}
                GROUP BY t.orig_geo_id, t.dest_geo_id
            )
        """
        if use_geo_c:
            tail = f"""
            SELECT a.orig_geo_id, a.dest_geo_id, a.trips, a.total_emissions_g, a.total_distance_km,
                   COALESCE(co.lat, a.trip_orig_lat) AS orig_lat,
                   COALESCE(co.lon, a.trip_orig_lon) AS orig_lon,
                   COALESCE(cd.lat, a.trip_dest_lat) AS dest_lat,
                   COALESCE(cd.lon, a.trip_dest_lon) AS dest_lon
            FROM agg a
            LEFT JOIN {SCHEMA}.geo_zone_centroids co ON co.geo_id::text = a.orig_geo_id::text
            LEFT JOIN {SCHEMA}.geo_zone_centroids cd ON cd.geo_id::text = a.dest_geo_id::text
            WHERE COALESCE(co.lat, a.trip_orig_lat) IS NOT NULL
              AND COALESCE(co.lon, a.trip_orig_lon) IS NOT NULL
              AND COALESCE(cd.lat, a.trip_dest_lat) IS NOT NULL
              AND COALESCE(cd.lon, a.trip_dest_lon) IS NOT NULL
            ORDER BY CASE WHEN %s = 'trips' THEN a.trips::double precision ELSE a.total_emissions_g END DESC
            LIMIT %s
            """
        else:
            tail = f"""
            SELECT a.orig_geo_id, a.dest_geo_id, a.trips, a.total_emissions_g, a.total_distance_km,
                   co.lat AS orig_lat, co.lon AS orig_lon, cd.lat AS dest_lat, cd.lon AS dest_lon
            FROM agg a
            JOIN (
                SELECT t.orig_geo_id::text AS gid,
                       AVG(t.orig_lat)::double precision AS lat,
                       AVG(t.orig_lon)::double precision AS lon
                FROM {SCHEMA}.{trips_t} t
                WHERE trim(t.mode_group::text) IN {CAR_MODE_GROUPS}
                  AND t.orig_lat IS NOT NULL AND t.orig_lon IS NOT NULL
                GROUP BY t.orig_geo_id
            ) co ON co.gid = a.orig_geo_id
            JOIN (
                SELECT t.dest_geo_id::text AS gid,
                       AVG(t.dest_lat)::double precision AS lat,
                       AVG(t.dest_lon)::double precision AS lon
                FROM {SCHEMA}.{trips_t} t
                WHERE trim(t.mode_group::text) IN {CAR_MODE_GROUPS}
                  AND t.dest_lat IS NOT NULL AND t.dest_lon IS NOT NULL
                GROUP BY t.dest_geo_id
            ) cd ON cd.gid = a.dest_geo_id
            ORDER BY CASE WHEN %s = 'trips' THEN a.trips::double precision ELSE a.total_emissions_g END DESC
            LIMIT %s
            """
        cur.execute(agg_cte + tail, (weight_by, limit))
        rows = cur.fetchall()
        cur.execute(
            agg_cte
            + """
            SELECT COUNT(*)::bigint, COALESCE(SUM(trips), 0)::bigint FROM agg
            """
        )
        totals = cur.fetchone()

    cur.close()
    conn.close()
    flows = [
        {
            "orig_geo_id": r[0],
            "dest_geo_id": r[1],
            "trips": int(r[2] or 0),
            "total_emissions_g": float(r[3] or 0),
            "total_distance_km": float(r[4] or 0),
            "orig_lat": float(r[5]),
            "orig_lon": float(r[6]),
            "dest_lat": float(r[7]),
            "dest_lon": float(r[8]),
        }
        for r in rows
    ]
    return jsonify(
        {
            "weight_by": weight_by,
            "limit": limit,
            "total_interzonal_trips": int(totals[1] or 0) if totals else 0,
            "total_flow_pairs": int(totals[0] or 0) if totals else 0,
            "flows_shown_trips": sum(f["trips"] for f in flows),
            "flow_count": len(flows),
            "flows": flows,
        }
    )


@app.route("/od-dashboard-api/zone_incoming_flow")
def api_zone_incoming_flow():
    try:
        return _api_zone_incoming_flow_impl()
    except Exception as exc:
        import traceback

        traceback.print_exc()
        return jsonify({"error": "zone_incoming_flow", "message": str(exc)}), 500


def _api_zone_incoming_flow_impl():
    dest_id = (request.args.get("dest_geo_id", "") or "").strip()
    if not dest_id:
        return jsonify({"error": "dest_geo_id is required"}), 400
    try:
        limit = int(request.args.get("limit", "80") or "80")
    except Exception:
        limit = 80
    limit = max(1, min(limit, 300))
    t = _tables_from_request()
    if isinstance(t, tuple):
        return t
    fam = t.get("family", "building")

    if fam == "routes_only":
        rt = t["routes"]
        zone_by = _normalize_zone_by(request.args.get("zone_by") or "rules")
        use_dest_zone = zone_by == "dest"
        conn = get_conn()
        cur = conn.cursor()

        if use_dest_zone:
            top_t, tot_t = _zif_dest_precompute_tables(cur, rt)
            if top_t and tot_t:
                cur.execute(
                    f"""
                    SELECT orig_geo_id, trips, total_emissions_g, total_distance_km, orig_lat, orig_lon
                    FROM {SCHEMA}.{top_t}
                    WHERE dest_geo_id = %s AND rank <= %s
                    ORDER BY rank
                    """,
                    (dest_id, limit),
                )
                frows = cur.fetchall()
                cur.execute(
                    f"""
                    SELECT total_incoming_trips, total_incoming_emissions_g, origin_zone_count,
                           dest_lat, dest_lon, dest_rules_trips, dest_rules_emissions_g
                    FROM {SCHEMA}.{tot_t} WHERE dest_geo_id = %s
                    """,
                    (dest_id,),
                )
                trow = cur.fetchone()
                inc_km = _read_zif_total_incoming_distance_km(cur, tot_t, dest_id, rt) if trow else 0.0
                cur.close()
                conn.close()
                dest_lat = _flow_coord(trow[3]) if trow else None
                dest_lon = _flow_coord(trow[4]) if trow else None
                flows = [
                    {
                        "orig_geo_id": str(r[0]),
                        "trips": int(r[1] or 0),
                        "total_emissions_g": float(r[2] or 0),
                        "total_distance_km": float(r[3] or 0),
                        "orig_lat": _flow_coord(r[4]),
                        "orig_lon": _flow_coord(r[5]),
                        "dest_lat": dest_lat,
                        "dest_lon": dest_lon,
                    }
                    for r in frows
                ]
                return jsonify(
                    {
                        "dest_geo_id": dest_id,
                        "zone_by": "dest",
                        "source": rt,
                        "precomputed": True,
                        "dest_lat": dest_lat,
                        "dest_lon": dest_lon,
                        "total_incoming_trips": int(trow[0] or 0) if trow else 0,
                        "total_incoming_emissions_g": float(trow[1] or 0) if trow else 0.0,
                        "total_incoming_distance_km": inc_km,
                        "origin_zone_count": int(trow[2] or 0) if trow else 0,
                        "dest_rules_trips": int(trow[5] or 0) if trow and trow[5] is not None else None,
                        "dest_rules_emissions_g": float(trow[6] or 0) if trow and trow[6] is not None else None,
                        "flows_shown_trips": sum(f["trips"] for f in flows),
                        "flow_count": len(flows),
                        "flows": flows,
                    }
                )

        # Fast path: precomputed tour-based incoming flows (rules view only).
        if not use_dest_zone:
            top_t, tot_t = _zif_precompute_tables(cur, rt)
            if top_t and tot_t:
                cur.execute(
                    f"""
                    SELECT orig_geo_id, trips, total_emissions_g, total_distance_km, orig_lat, orig_lon
                    FROM {SCHEMA}.{top_t}
                    WHERE dest_geo_id = %s AND rank <= %s
                    ORDER BY rank
                    """,
                    (dest_id, limit),
                )
                frows = cur.fetchall()
                cur.execute(
                    f"""
                    SELECT total_incoming_trips, total_incoming_emissions_g, origin_zone_count,
                           dest_lat, dest_lon, dest_rules_trips, dest_rules_emissions_g
                    FROM {SCHEMA}.{tot_t} WHERE dest_geo_id = %s
                    """,
                    (dest_id,),
                )
                trow = cur.fetchone()
                inc_km = _read_zif_total_incoming_distance_km(cur, tot_t, dest_id, rt) if trow else 0.0
                cur.close()
                conn.close()
                dest_lat = _flow_coord(trow[3]) if trow else None
                dest_lon = _flow_coord(trow[4]) if trow else None
                flows = [
                    {
                        "orig_geo_id": str(r[0]),
                        "trips": int(r[1] or 0),
                        "total_emissions_g": float(r[2] or 0),
                        "total_distance_km": float(r[3] or 0),
                        "orig_lat": _flow_coord(r[4]),
                        "orig_lon": _flow_coord(r[5]),
                        "dest_lat": dest_lat,
                        "dest_lon": dest_lon,
                    }
                    for r in frows
                ]
                return jsonify(
                    {
                        "dest_geo_id": dest_id,
                        "zone_by": "rules",
                        "source": rt,
                        "precomputed": True,
                        "dest_lat": dest_lat,
                        "dest_lon": dest_lon,
                        "total_incoming_trips": int(trow[0] or 0) if trow else 0,
                        "total_incoming_emissions_g": float(trow[1] or 0) if trow else 0.0,
                        "total_incoming_distance_km": inc_km,
                        "origin_zone_count": int(trow[2] or 0) if trow else 0,
                        "dest_rules_trips": int(trow[5] or 0) if trow and trow[5] is not None else None,
                        "dest_rules_emissions_g": float(trow[6] or 0) if trow and trow[6] is not None else None,
                        "flows_shown_trips": sum(f["trips"] for f in flows),
                        "flow_count": len(flows),
                        "flows": flows,
                    }
                )

        use_geo_c = _table_exists(cur, "geo_zone_centroids")
        isl = _request_island_only(default=False)
        summary_t = _pick_od_flow_summary_table(cur, rt) if use_dest_zone else ""
        if summary_t:
            isl_sql = _od_flow_island_bbox_sql("s") if isl else ""
            coord_ok = """
                  AND s.orig_lat IS NOT NULL AND s.orig_lon IS NOT NULL
                  AND s.dest_lat IS NOT NULL AND s.dest_lon IS NOT NULL
            """
            dist_sql = (
                "s.total_distance_km::double precision"
                if _column_exists(cur, summary_t, "total_distance_km")
                else "0::double precision"
            )
            cur.execute(
                f"""
                SELECT s.orig_geo_id::text, s.trips::bigint, s.total_emissions_g::double precision,
                       {dist_sql},
                       s.orig_lat::double precision, s.orig_lon::double precision,
                       s.dest_lat::double precision, s.dest_lon::double precision
                FROM {SCHEMA}.{summary_t} s
                WHERE s.dest_geo_id::text = %s
                  AND s.orig_geo_id::text IS DISTINCT FROM s.dest_geo_id::text
                  {coord_ok}
                  {isl_sql}
                ORDER BY s.total_emissions_g DESC
                LIMIT %s
                """,
                (dest_id, limit),
            )
            rows = cur.fetchall()
            cur.execute(
                f"""
                SELECT COUNT(*)::bigint AS origin_zones,
                       COALESCE(SUM(s.trips), 0)::bigint AS total_incoming_trips,
                       COALESCE(SUM(s.total_emissions_g), 0)::double precision AS total_incoming_emissions_g
                FROM {SCHEMA}.{summary_t} s
                WHERE s.dest_geo_id::text = %s
                  AND s.orig_geo_id::text IS DISTINCT FROM s.dest_geo_id::text
                  {isl_sql}
                """,
                (dest_id,),
            )
            tot = cur.fetchone()
            dest_rules_trips = None
            dest_rules_emissions_g = None
            zt = (t.get("zone") or "").strip() or _pick_zone_emissions_route_assignment(
                cur, rt, zone_kind="rules"
            )
            if zt and _table_exists(cur, zt):
                trips_col, eg_col = _zone_rules_totals_sql(zt, cur)
                cur.execute(
                    f"""
                    SELECT {trips_col}::bigint, {eg_col}::double precision
                    FROM {SCHEMA}.{zt} z
                    WHERE z.geo_id::text = %s
                    LIMIT 1
                    """,
                    (dest_id,),
                )
                zr = cur.fetchone()
                if zr:
                    dest_rules_trips = int(zr[0] or 0) if zr[0] is not None else None
                    dest_rules_emissions_g = float(zr[1] or 0)
            cur.close()
            conn.close()
            origin_zones = int(tot[0] or 0) if tot else 0
            total_incoming = int(tot[1] or 0) if tot else 0
            total_incoming_g = float(tot[2] or 0) if tot else 0.0
            flows = [_incoming_flow_row_dict(r) for r in rows]
            return jsonify(
                {
                    "dest_geo_id": dest_id,
                    "source": summary_t,
                    "dest_rules_trips": dest_rules_trips,
                    "dest_rules_emissions_g": dest_rules_emissions_g,
                    "total_incoming_trips": total_incoming,
                    "total_incoming_emissions_g": total_incoming_g,
                    "origin_zone_count": origin_zones,
                    "flows_shown_trips": sum(f["trips"] for f in flows),
                    "flow_count": len(flows),
                    "flows": flows,
                }
            )

        has_attr_geo = _column_exists(cur, rt, "route_attributed_geo_id")
        has_dest_geo_col = _column_exists(cur, rt, "route_dest_geo_id")
        # Destination side: the zone shown on the map. For rules view this is the
        # rules-attributed zone; for dest view it is the raw destination CT.
        dest_zone_sql = _routes_map_zone_geo_id_sql(
            "r",
            zone_by="dest" if use_dest_zone else "rules",
            has_route_attributed_geo_id=has_attr_geo and not use_dest_zone,
            has_route_dest_geo_id=has_dest_geo_col,
        )
        # Origin side is always the trip's real origin zone; rules attribution assigns a
        # single zone per route, so using it on both sides would make origin == dest.
        orig_zone_sql = "split_part(trim(r.orig_geo_id::text), '.', 1)"
        if use_dest_zone:
            dest_match = "r.dest_geo_id::text = %s"
            orig_distinct = "r.orig_geo_id::text IS DISTINCT FROM r.dest_geo_id::text"
            agg_orig_sel = "r.orig_geo_id::text AS orig_geo_id"
            agg_group_by = "r.orig_geo_id"
        else:
            dest_match = f"({dest_zone_sql})::text = %s"
            orig_distinct = f"({orig_zone_sql})::text IS DISTINCT FROM ({dest_zone_sql})::text"
            agg_orig_sel = f"({orig_zone_sql})::text AS orig_geo_id"
            agg_group_by = f"({orig_zone_sql})::text"

        # Top-N incoming origins are ranked regardless of the island boundary so off-island
        # origins (Laval, South Shore) appear. Destination is still the clicked island zone.
        bbox = ""
        if use_geo_c:
            flow_sql = f"""
            WITH agg AS (
                SELECT {agg_orig_sel}, COUNT(*)::bigint AS trips,
                       COALESCE(SUM(r.route_emissions_g), 0)::double precision AS total_emissions_g,
                       COALESCE(SUM(r.distance_m), 0)::double precision / 1000.0 AS total_distance_km,
                       AVG(r.orig_lat)::double precision AS route_orig_lat,
                       AVG(r.orig_lon)::double precision AS route_orig_lon,
                       AVG(r.dest_lat)::double precision AS route_dest_lat,
                       AVG(r.dest_lon)::double precision AS route_dest_lon
                FROM {SCHEMA}.{rt} r
                WHERE {dest_match} AND {orig_distinct}
                  AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                  AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                  {bbox}
                GROUP BY {agg_group_by}
            )
            SELECT a.orig_geo_id, a.trips, a.total_emissions_g, a.total_distance_km,
                   COALESCE(co.lat, a.route_orig_lat) AS orig_lat,
                   COALESCE(co.lon, a.route_orig_lon) AS orig_lon,
                   COALESCE(cd.lat, a.route_dest_lat) AS dest_lat,
                   COALESCE(cd.lon, a.route_dest_lon) AS dest_lon
            FROM agg a
            LEFT JOIN {SCHEMA}.geo_zone_centroids co ON co.geo_id::text = a.orig_geo_id::text
            LEFT JOIN {SCHEMA}.geo_zone_centroids cd ON cd.geo_id::text = %s
            WHERE COALESCE(co.lat, a.route_orig_lat) IS NOT NULL
              AND COALESCE(co.lon, a.route_orig_lon) IS NOT NULL
              AND COALESCE(cd.lat, a.route_dest_lat) IS NOT NULL
              AND COALESCE(cd.lon, a.route_dest_lon) IS NOT NULL
            ORDER BY a.total_emissions_g DESC LIMIT %s
            """
        else:
            flow_sql = f"""
            WITH dest_ll AS (
                SELECT AVG(r.dest_lat)::double precision AS lat, AVG(r.dest_lon)::double precision AS lon
                FROM {SCHEMA}.{rt} r
                WHERE {dest_match} AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                  AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                  AND r.dest_lat IS NOT NULL AND r.dest_lon IS NOT NULL
            ),
            orig_ll AS (
                SELECT r.orig_geo_id::text AS gid, AVG(r.orig_lat)::double precision AS lat,
                       AVG(r.orig_lon)::double precision AS lon
                FROM {SCHEMA}.{rt} r
                WHERE trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                  AND r.orig_lat IS NOT NULL AND r.orig_lon IS NOT NULL
                GROUP BY r.orig_geo_id
            ),
            agg AS (
                SELECT {agg_orig_sel}, COUNT(*)::bigint AS trips,
                       COALESCE(SUM(r.route_emissions_g), 0)::double precision AS total_emissions_g,
                       COALESCE(SUM(r.distance_m), 0)::double precision / 1000.0 AS total_distance_km
                FROM {SCHEMA}.{rt} r
                WHERE {dest_match} AND {orig_distinct}
                  AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                  AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                  {bbox}
                GROUP BY {agg_group_by}
            )
            SELECT a.orig_geo_id, a.trips, a.total_emissions_g, a.total_distance_km,
                   co.lat AS orig_lat, co.lon AS orig_lon, dd.lat AS dest_lat, dd.lon AS dest_lon
            FROM agg a
            JOIN orig_ll co ON co.gid = a.orig_geo_id
            CROSS JOIN dest_ll dd
            ORDER BY a.total_emissions_g DESC LIMIT %s
            """
        cur.execute(flow_sql, (dest_id, dest_id, limit))
        rows = cur.fetchall()
        cur.execute(
            f"""
            SELECT COUNT(*)::bigint FROM {SCHEMA}.{rt} r
            WHERE {dest_match} AND {orig_distinct}
              AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
              AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
              {bbox}
            """,
            (dest_id,),
        )
        total_row = cur.fetchone()
        total_incoming = int(total_row[0] or 0) if total_row else 0
        cur.execute(
            f"""
            SELECT COALESCE(SUM(r.route_emissions_g), 0)::double precision
            FROM {SCHEMA}.{rt} r
            WHERE {dest_match} AND {orig_distinct}
              AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
              AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
              {bbox}
            """,
            (dest_id,),
        )
        total_g_row = cur.fetchone()
        total_incoming_g = float(total_g_row[0] or 0) if total_g_row else 0.0
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT ({orig_zone_sql})::text)::bigint
            FROM {SCHEMA}.{rt} r
            WHERE {dest_match} AND {orig_distinct}
              AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
              AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
              {bbox}
            """,
            (dest_id,),
        )
        origin_zones = int((cur.fetchone() or [0])[0] or 0)
        dest_lat = dest_lon = None
        if use_geo_c:
            cur.execute(
                f"""
                SELECT lat::double precision, lon::double precision
                FROM {SCHEMA}.geo_zone_centroids
                WHERE geo_id::text = %s
                LIMIT 1
                """,
                (dest_id,),
            )
            ll = cur.fetchone()
            if ll:
                dest_lat, dest_lon = _flow_coord(ll[0]), _flow_coord(ll[1])
        cur.close()
        conn.close()
        flows = [_incoming_flow_row_dict(r) for r in rows]
        total_incoming_km = round(sum(float(f.get("total_distance_km") or 0) for f in flows), 2)
        return jsonify(
            {
                "dest_geo_id": dest_id,
                "zone_by": zone_by,
                "source": rt,
                "dest_lat": dest_lat,
                "dest_lon": dest_lon,
                "total_incoming_trips": total_incoming,
                "total_incoming_emissions_g": total_incoming_g,
                "total_incoming_distance_km": total_incoming_km,
                "origin_zone_count": origin_zones,
                "flows_shown_trips": sum(f["trips"] for f in flows),
                "flow_count": len(flows),
                "flows": flows,
            }
        )

    em_t, trips_t, routes_t = t["emissions"], t["trips"], t["routes"]
    routes_join = bool(t.get("routes_join", True) and routes_t)
    rsql = (
        f"""
            LEFT JOIN {SCHEMA}.{routes_t} r
              ON r.synthetic_person_id::text = e.synthetic_person_id::text
             AND r.orig_geo_id = e.orig_geo_id::int AND r.dest_geo_id = e.dest_geo_id::int
             AND COALESCE(r.purpose,'') = COALESCE(e.purpose::text,'')
             AND COALESCE(r.dep_time_bin,'') = COALESCE(e.dep_time_bin::text,'')
        """
        if routes_join
        else ""
    )
    dsum = (
        "COALESCE(SUM(COALESCE(e.distance_m, r.distance_m)), 0)::double precision / 1000.0 AS total_distance_km"
        if routes_join
        else "COALESCE(SUM(e.distance_m), 0)::double precision / 1000.0 AS total_distance_km"
    )
    olat_expr = "COALESCE(r.orig_lat, t.orig_lat)" if routes_join else "t.orig_lat"
    olon_expr = "COALESCE(r.orig_lon, t.orig_lon)" if routes_join else "t.orig_lon"
    dlat_expr = "COALESCE(r.dest_lat, t.dest_lat)" if routes_join else "t.dest_lat"
    dlon_expr = "COALESCE(r.dest_lon, t.dest_lon)" if routes_join else "t.dest_lon"
    if fam == "micro":
        tot_eg = "COALESCE(SUM(e.emissions_g), 0)::double precision AS total_emissions_g"
        eg_where = "e.emissions_g > 0"
    else:
        tot_eg = "COALESCE(SUM(COALESCE(e.emissions_g_pair, e.emissions_g)), 0)::double precision AS total_emissions_g"
        eg_where = "COALESCE(e.emissions_g_pair, e.emissions_g) > 0"

    conn = get_conn()
    cur = conn.cursor()
    use_geo_c = _table_exists(cur, "geo_zone_centroids")
    agg_cte = f"""
        WITH agg AS (
            SELECT t.orig_geo_id::text AS orig_geo_id, COUNT(*)::bigint AS trips,
                   {tot_eg}, {dsum},
                   AVG({olat_expr})::double precision AS trip_orig_lat,
                   AVG({olon_expr})::double precision AS trip_orig_lon,
                   AVG({dlat_expr})::double precision AS trip_dest_lat,
                   AVG({dlon_expr})::double precision AS trip_dest_lon
            FROM {SCHEMA}.{trips_t} t
            INNER JOIN {SCHEMA}.{em_t} e ON t.synthetic_person_id::text = e.synthetic_person_id::text
             AND t.orig_geo_id::int = e.orig_geo_id::int AND t.dest_geo_id::int = e.dest_geo_id::int
             AND COALESCE(t.purpose::text,'') = COALESCE(e.purpose::text,'')
             AND COALESCE(t.dep_time_bin::text,'') = COALESCE(e.dep_time_bin::text,'')
             AND (e.hh_id IS NULL OR t.hh_id::text IS NOT DISTINCT FROM e.hh_id::text)
            {rsql}
            WHERE t.dest_geo_id::text = %s AND t.orig_geo_id::text IS DISTINCT FROM t.dest_geo_id::text
              AND trim(COALESCE(e.mode_group::text, t.mode_group::text)) IN {CAR_MODE_GROUPS}
              AND {eg_where}
            GROUP BY t.orig_geo_id
        )
    """
    if use_geo_c:
        tail = f"""
        SELECT a.orig_geo_id, a.trips, a.total_emissions_g, a.total_distance_km,
               COALESCE(co.lat, a.trip_orig_lat) AS orig_lat,
               COALESCE(co.lon, a.trip_orig_lon) AS orig_lon,
               COALESCE(cd.lat, a.trip_dest_lat) AS dest_lat,
               COALESCE(cd.lon, a.trip_dest_lon) AS dest_lon
        FROM agg a
        LEFT JOIN {SCHEMA}.geo_zone_centroids co ON co.geo_id::text = a.orig_geo_id::text
        LEFT JOIN {SCHEMA}.geo_zone_centroids cd ON cd.geo_id::text = %s
        WHERE COALESCE(co.lat, a.trip_orig_lat) IS NOT NULL
          AND COALESCE(co.lon, a.trip_orig_lon) IS NOT NULL
          AND COALESCE(cd.lat, a.trip_dest_lat) IS NOT NULL
          AND COALESCE(cd.lon, a.trip_dest_lon) IS NOT NULL
        ORDER BY a.total_emissions_g DESC LIMIT %s
        """
    else:
        tail = f"""
        SELECT a.orig_geo_id, a.trips, a.total_emissions_g, a.total_distance_km,
               co.lat AS orig_lat, co.lon AS orig_lon, cd.lat AS dest_lat, cd.lon AS dest_lon
        FROM agg a
        JOIN (
            SELECT t.orig_geo_id::text AS gid, AVG(t.orig_lat)::double precision AS lat,
                   AVG(t.orig_lon)::double precision AS lon
            FROM {SCHEMA}.{trips_t} t
            WHERE trim(t.mode_group::text) IN {CAR_MODE_GROUPS}
              AND t.orig_lat IS NOT NULL AND t.orig_lon IS NOT NULL
            GROUP BY t.orig_geo_id
        ) co ON co.gid = a.orig_geo_id
        CROSS JOIN (
            SELECT AVG(t.dest_lat)::double precision AS lat, AVG(t.dest_lon)::double precision AS lon
            FROM {SCHEMA}.{trips_t} t
            WHERE t.dest_geo_id::text = %s AND trim(t.mode_group::text) IN {CAR_MODE_GROUPS}
              AND t.dest_lat IS NOT NULL AND t.dest_lon IS NOT NULL
        ) cd
        ORDER BY a.total_emissions_g DESC LIMIT %s
        """
    cur.execute(agg_cte + tail, (dest_id, dest_id, limit))
    rows = cur.fetchall()
    cur.execute(
        f"""
        SELECT COUNT(*)::bigint FROM {SCHEMA}.{trips_t} t
        INNER JOIN {SCHEMA}.{em_t} e ON t.synthetic_person_id::text = e.synthetic_person_id::text
         AND t.orig_geo_id::int = e.orig_geo_id::int AND t.dest_geo_id::int = e.dest_geo_id::int
         AND COALESCE(t.purpose::text,'') = COALESCE(e.purpose::text,'')
         AND COALESCE(t.dep_time_bin::text,'') = COALESCE(e.dep_time_bin::text,'')
         AND (e.hh_id IS NULL OR t.hh_id::text IS NOT DISTINCT FROM e.hh_id::text)
        WHERE t.dest_geo_id::text = %s AND t.orig_geo_id::text IS DISTINCT FROM t.dest_geo_id::text
          AND trim(COALESCE(e.mode_group::text, t.mode_group::text)) IN {CAR_MODE_GROUPS}
          AND {eg_where}
        """,
        (dest_id,),
    )
    total_row = cur.fetchone()
    cur.close()
    conn.close()
    flows = [_incoming_flow_row_dict(r) for r in rows]
    return jsonify(
        {
            "dest_geo_id": dest_id,
            "total_incoming_trips": int(total_row[0] or 0) if total_row else 0,
            "flows_shown_trips": sum(f["trips"] for f in flows),
            "flow_count": len(flows),
            "flows": flows,
        }
    )


@app.route("/od-dashboard-api/zone_incoming_flows_all")
def api_zone_incoming_flows_all():
    """Precompute top-N incoming flows for *every* destination zone in one query.

    The flows page fetches this once on load and serves zone clicks from memory,
    avoiding a per-click DB round trip. Only the fast ``routes_only`` family is
    supported in bulk; other families return ``supported=False`` so the client
    falls back to the per-zone endpoint.
    """
    try:
        return _api_zone_incoming_flows_all_impl()
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(exc), "zones": {}, "supported": False}), 200


def _api_zone_incoming_flows_all_impl():
    try:
        top_n = int(request.args.get("limit", "10") or "10")
    except Exception:
        top_n = 10
    top_n = max(1, min(top_n, 50))
    t = _tables_from_request()
    if isinstance(t, tuple):
        return t
    fam = t.get("family", "building")
    if fam != "routes_only":
        return jsonify(
            {"zones": {}, "supported": False, "reason": f"family {fam} not supported in bulk"}
        )

    rt = t["routes"]
    zone_by = _normalize_zone_by(request.args.get("zone_by") or "rules")
    use_dest_zone = zone_by == "dest"
    conn = get_conn()
    cur = conn.cursor()

    if use_dest_zone:
        top_t, tot_t = _zif_dest_precompute_tables(cur, rt)
        if top_t and tot_t:
            has_tot_dist = _column_exists(cur, tot_t, "total_incoming_distance_km")
            dist_by_dest: dict[str, float] = {}
            if not has_tot_dist:
                orig = "split_part(trim(r.orig_geo_id::text), '.', 1)"
                dest = "split_part(trim(r.dest_geo_id::text), '.', 1)"
                cur.execute(
                    f"""
                    SELECT ({dest})::text AS dest_geo_id,
                           COALESCE(SUM(COALESCE(r.distance_m, 0)), 0)::double precision / 1000.0 AS km
                    FROM {SCHEMA}.{rt} r
                    WHERE r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                      AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                      AND ({orig}) IS DISTINCT FROM ({dest})
                    GROUP BY 1
                    """
                )
                dist_by_dest = {str(r[0]): round(float(r[1] or 0), 2) for r in cur.fetchall()}
            dist_col = (
                "z.total_incoming_distance_km::double precision"
                if has_tot_dist
                else "NULL::double precision"
            )
            cur.execute(
                f"""
                SELECT f.dest_geo_id, f.rank, f.orig_geo_id, f.trips,
                       f.total_emissions_g, f.total_distance_km, f.orig_lat, f.orig_lon,
                       z.total_incoming_trips, z.total_incoming_emissions_g, {dist_col},
                       z.origin_zone_count,
                       z.dest_lat, z.dest_lon, z.dest_rules_trips, z.dest_rules_emissions_g
                FROM {SCHEMA}.{top_t} f
                JOIN {SCHEMA}.{tot_t} z ON z.dest_geo_id = f.dest_geo_id
                WHERE f.rank <= %s
                ORDER BY f.dest_geo_id, f.rank
                """,
                (top_n,),
            )
            rows = cur.fetchall()
            cur.close()
            conn.close()
            zones: dict[str, dict] = {}
            for row in rows:
                did = str(row[0])
                z = zones.get(did)
                if z is None:
                    inc_km = row[10]
                    if inc_km is None:
                        inc_km = dist_by_dest.get(did, 0.0)
                    z = {
                        "dest_geo_id": did,
                        "zone_by": "dest",
                        "source": rt,
                        "dest_lat": _flow_coord(row[12]),
                        "dest_lon": _flow_coord(row[13]),
                        "total_incoming_trips": int(row[8] or 0),
                        "total_incoming_emissions_g": float(row[9] or 0),
                        "total_incoming_distance_km": round(float(inc_km or 0), 2),
                        "origin_zone_count": int(row[11] or 0),
                        "dest_rules_trips": int(row[14] or 0) if row[14] is not None else None,
                        "dest_rules_emissions_g": float(row[15] or 0) if row[15] is not None else None,
                        "flows": [],
                    }
                    zones[did] = z
                z["flows"].append(
                    {
                        "orig_geo_id": str(row[2]),
                        "trips": int(row[3] or 0),
                        "total_emissions_g": float(row[4] or 0),
                        "total_distance_km": float(row[5] or 0),
                        "orig_lat": _flow_coord(row[6]),
                        "orig_lon": _flow_coord(row[7]),
                        "dest_lat": _flow_coord(row[12]),
                        "dest_lon": _flow_coord(row[13]),
                    }
                )
            for z in zones.values():
                z["flow_count"] = len(z["flows"])
                z["flows_shown_trips"] = sum(f["trips"] for f in z["flows"])
            return jsonify(
                {
                    "supported": True,
                    "zone_by": "dest",
                    "source": rt,
                    "limit": top_n,
                    "precomputed": True,
                    "zone_count": len(zones),
                    "zones": zones,
                }
            )

    # Fast path: serve from precomputed tour-based tables when available (rules view only).
    if not use_dest_zone:
        top_t, tot_t = _zif_precompute_tables(cur, rt)
        if top_t and tot_t:
            has_tot_dist = _column_exists(cur, tot_t, "total_incoming_distance_km")
            dist_by_dest: dict[str, float] = {}
            if not has_tot_dist:
                attr, orig, dest = _zif_tour_partner_sql("r")
                cur.execute(
                    f"""
                    SELECT ({attr})::text AS dest_geo_id,
                           COALESCE(SUM(COALESCE(r.distance_m, 0)), 0)::double precision / 1000.0 AS km
                    FROM {SCHEMA}.{rt} r
                    WHERE r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
                      AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
                      AND NOT (({orig}) = ({attr}) AND ({dest}) = ({attr}))
                    GROUP BY 1
                    """
                )
                dist_by_dest = {str(r[0]): round(float(r[1] or 0), 2) for r in cur.fetchall()}
            dist_col = (
                "z.total_incoming_distance_km::double precision"
                if has_tot_dist
                else "NULL::double precision"
            )
            cur.execute(
                f"""
                SELECT f.dest_geo_id, f.rank, f.orig_geo_id, f.trips,
                       f.total_emissions_g, f.total_distance_km, f.orig_lat, f.orig_lon,
                       z.total_incoming_trips, z.total_incoming_emissions_g, {dist_col},
                       z.origin_zone_count,
                       z.dest_lat, z.dest_lon, z.dest_rules_trips, z.dest_rules_emissions_g
                FROM {SCHEMA}.{top_t} f
                JOIN {SCHEMA}.{tot_t} z ON z.dest_geo_id = f.dest_geo_id
                WHERE f.rank <= %s
                ORDER BY f.dest_geo_id, f.rank
                """,
                (top_n,),
            )
            rows = cur.fetchall()
            cur.close()
            conn.close()
            zones: dict[str, dict] = {}
            for row in rows:
                did = str(row[0])
                z = zones.get(did)
                if z is None:
                    inc_km = row[10]
                    if inc_km is None:
                        inc_km = dist_by_dest.get(did, 0.0)
                    z = {
                        "dest_geo_id": did,
                        "zone_by": "rules",
                        "source": rt,
                        "dest_lat": _flow_coord(row[12]),
                        "dest_lon": _flow_coord(row[13]),
                        "total_incoming_trips": int(row[8] or 0),
                        "total_incoming_emissions_g": float(row[9] or 0),
                        "total_incoming_distance_km": round(float(inc_km or 0), 2),
                        "origin_zone_count": int(row[11] or 0),
                        "dest_rules_trips": int(row[14] or 0) if row[14] is not None else None,
                        "dest_rules_emissions_g": float(row[15] or 0) if row[15] is not None else None,
                        "flows": [],
                    }
                    zones[did] = z
                z["flows"].append(
                    {
                        "orig_geo_id": str(row[2]),
                        "trips": int(row[3] or 0),
                        "total_emissions_g": float(row[4] or 0),
                        "total_distance_km": float(row[5] or 0),
                        "orig_lat": _flow_coord(row[6]),
                        "orig_lon": _flow_coord(row[7]),
                        "dest_lat": _flow_coord(row[12]),
                        "dest_lon": _flow_coord(row[13]),
                    }
                )
            for z in zones.values():
                z["flow_count"] = len(z["flows"])
                z["flows_shown_trips"] = sum(f["trips"] for f in z["flows"])
            return jsonify(
                {
                    "supported": True,
                    "zone_by": "rules",
                    "source": rt,
                    "limit": top_n,
                    "precomputed": True,
                    "zone_count": len(zones),
                    "zones": zones,
                }
            )

    isl = _request_island_only(default=True)
    has_attr_geo = _column_exists(cur, rt, "route_attributed_geo_id")
    has_dest_geo_col = _column_exists(cur, rt, "route_dest_geo_id")
    dest_zone_sql = _routes_map_zone_geo_id_sql(
        "r",
        zone_by="dest" if use_dest_zone else "rules",
        has_route_attributed_geo_id=has_attr_geo and not use_dest_zone,
        has_route_dest_geo_id=has_dest_geo_col,
    )
    if use_dest_zone:
        orig_zone_sql = "split_part(trim(r.orig_geo_id::text), '.', 1)"
        dest_zone_sql = "split_part(trim(r.dest_geo_id::text), '.', 1)"
    else:
        orig_zone_sql = "split_part(trim(r.orig_geo_id::text), '.', 1)"

    # Flows rank the true top-N origin zones for each destination regardless of the island
    # boundary — off-island origins (Laval, South Shore) are included. The destination is still
    # the clicked island zone (matched by zone id), so we only require valid coordinates here.
    bbox = ""

    sql = f"""
        WITH agg AS (
            SELECT ({dest_zone_sql})::text AS dest_geo_id,
                   ({orig_zone_sql})::text AS orig_geo_id,
                   COUNT(*)::bigint AS trips,
                   COALESCE(SUM(r.route_emissions_g), 0)::double precision AS total_emissions_g,
                   COALESCE(SUM(r.distance_m), 0)::double precision / 1000.0 AS total_distance_km,
                   AVG(r.orig_lat)::double precision AS orig_lat,
                   AVG(r.orig_lon)::double precision AS orig_lon
            FROM {SCHEMA}.{rt} r
            WHERE ({orig_zone_sql})::text IS DISTINCT FROM ({dest_zone_sql})::text
              AND r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
              AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
              AND r.orig_lat IS NOT NULL AND r.orig_lon IS NOT NULL
              {bbox}
            GROUP BY 1, 2
        ),
        dest_ll AS (
            SELECT ({dest_zone_sql})::text AS dest_geo_id,
                   AVG(r.dest_lat)::double precision AS dest_lat,
                   AVG(r.dest_lon)::double precision AS dest_lon
            FROM {SCHEMA}.{rt} r
            WHERE r.route_emissions_g IS NOT NULL AND r.route_emissions_g > 0
              AND trim(r.mode_group::text) IN {CAR_MODE_GROUPS}
              AND r.dest_lat IS NOT NULL AND r.dest_lon IS NOT NULL
              {bbox}
            GROUP BY 1
        ),
        totals AS (
            SELECT dest_geo_id,
                   SUM(trips)::bigint AS total_incoming_trips,
                   SUM(total_emissions_g)::double precision AS total_incoming_emissions_g,
                   COUNT(*)::bigint AS origin_zone_count
            FROM agg
            GROUP BY dest_geo_id
        ),
        ranked AS (
            SELECT a.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY a.dest_geo_id ORDER BY a.total_emissions_g DESC
                   ) AS rn
            FROM agg a
        )
        SELECT r.dest_geo_id, r.orig_geo_id, r.trips, r.total_emissions_g,
               r.total_distance_km, r.orig_lat, r.orig_lon,
               d.dest_lat, d.dest_lon,
               t.total_incoming_trips, t.total_incoming_emissions_g, t.origin_zone_count
        FROM ranked r
        JOIN totals t ON t.dest_geo_id = r.dest_geo_id
        LEFT JOIN dest_ll d ON d.dest_geo_id = r.dest_geo_id
        WHERE r.rn <= %s
        ORDER BY r.dest_geo_id, r.total_emissions_g DESC
    """
    cur.execute(sql, (top_n,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    zones: dict[str, dict] = {}
    for row in rows:
        did = str(row[0])
        z = zones.get(did)
        if z is None:
            z = {
                "dest_geo_id": did,
                "zone_by": zone_by,
                "source": rt,
                "dest_lat": _flow_coord(row[7]),
                "dest_lon": _flow_coord(row[8]),
                "total_incoming_trips": int(row[9] or 0),
                "total_incoming_emissions_g": float(row[10] or 0),
                "origin_zone_count": int(row[11] or 0),
                "flows": [],
            }
            zones[did] = z
        z["flows"].append(
            {
                "orig_geo_id": str(row[1]),
                "trips": int(row[2] or 0),
                "total_emissions_g": float(row[3] or 0),
                "total_distance_km": float(row[4] or 0),
                "orig_lat": _flow_coord(row[5]),
                "orig_lon": _flow_coord(row[6]),
                "dest_lat": _flow_coord(row[7]),
                "dest_lon": _flow_coord(row[8]),
            }
        )
    for z in zones.values():
        z["flow_count"] = len(z["flows"])
        z["flows_shown_trips"] = sum(f["trips"] for f in z["flows"])
    return jsonify(
        {
            "supported": True,
            "zone_by": zone_by,
            "source": rt,
            "limit": top_n,
            "island_only": bool(isl),
            "zone_count": len(zones),
            "zones": zones,
        }
    )


def _response_json(resp):
    if isinstance(resp, tuple):
        body, code = resp[0], resp[1] if len(resp) > 1 else 200
        data = body.get_json() if hasattr(body, "get_json") else {}
        if code and int(code) >= 400:
            return None, data, int(code)
        return data, None, int(code)
    if hasattr(resp, "get_json"):
        return resp.get_json(), None, getattr(resp, "status_code", 200) or 200
    return {}, None, 200


@app.route("/od-dashboard-api/flows_zones")
def api_flows_zones():
    """Fast zone list for flows.html (no GeoJSON polygons)."""
    zone_by = _normalize_zone_by(request.args.get("zone_by") or "rules")
    with app.test_request_context(
        f"/od-dashboard-api/zone_map?zone_by={zone_by}&min_kg=0&island_only=1&include_geojson=0",
        method="GET",
    ):
        return api_zone_map()


@app.route("/buildings")
@app.route("/buildings.html")
def buildings_page():
    d = Path(app.static_folder)
    if d.exists():
        resp = send_from_directory(d, "buildings.html")
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp
    return "<p>buildings.html not found.</p>", 404


@app.route("/flows")
@app.route("/flows.html")
def flows_page():
    d = Path(app.static_folder)
    if d.exists():
        resp = send_from_directory(d, "flows.html")
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp
    return "<p>flows.html not found.</p>", 404


@app.route("/zones-boundary")
@app.route("/zones-boundary.html")
def zones_boundary_page():
    d = Path(app.static_folder)
    if d.exists():
        resp = send_from_directory(d, "zones-boundary.html")
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp
    return "<p>zones-boundary.html not found.</p>", 404


@app.route("/od-dashboard-api/flows_bootstrap")
def api_flows_bootstrap():
    """One round-trip for flows page: rules zone_map + optional incoming flows."""
    dest_id = (request.args.get("dest_geo_id", "") or "").strip()
    try:
        limit = int(request.args.get("limit", "200") or "200")
    except Exception:
        limit = 200
    limit = max(1, min(limit, 300))
    q = request.query_string.decode("utf-8") if request.query_string else ""
    zone_q = "zone_by=rules&min_kg=0&island_only=1&include_geojson=0"
    if q:
        for part in q.split("&"):
            if part.startswith("api="):
                zone_q += "&" + part
                break
    with app.test_request_context("/od-dashboard-api/zone_map?" + zone_q, method="GET"):
        zone_resp = api_zone_map()
    zone_data, zone_err, zone_code = _response_json(zone_resp)
    if zone_err:
        return jsonify({"error": "zone_map", "message": zone_err, "status": zone_code}), zone_code
    incoming = None
    if dest_id:
        inc_q = f"dest_geo_id={dest_id}&limit={limit}&island_only=1"
        if q:
            for part in q.split("&"):
                if part.startswith("api="):
                    inc_q += "&" + part
                    break
        with app.test_request_context("/od-dashboard-api/zone_incoming_flow?" + inc_q, method="GET"):
            inc_resp = api_zone_incoming_flow()
        incoming, inc_err, inc_code = _response_json(inc_resp)
        if inc_err:
            return jsonify(
                {"zone_map": zone_data, "incoming_error": inc_err, "incoming_status": inc_code}
            ), inc_code
    return jsonify({"zone_map": zone_data or {}, "incoming": incoming})


@app.route("/od-dashboard-api/bounds")
def api_bounds():
    extent = (request.args.get("extent") or "island").strip().lower()
    if extent in ("cmm", "full", "all"):
        conn = get_conn()
        cur = conn.cursor()
        try:
            bounds = _extent_bounds_from_geom_table(cur, island_only=False)
        finally:
            cur.close()
            conn.close()
        return jsonify({"bounds": bounds or CMM_BOUNDS, "extent": "cmm"})
    return jsonify({"bounds": MONTREAL_BOUNDS, "extent": "island"})


@app.route("/")
def index():
    d = Path(app.static_folder)
    if d.exists():
        # Prefer SPA shell when present; fall back to zones page for older deployments.
        if (d / "dashboard.html").is_file():
            return send_from_directory(d, "dashboard.html")
        return send_from_directory(d, "index.html")
    return "<p>Dashboard not found.</p>", 404


@app.route("/<path:path>")
def static_file(path):
    if path.startswith("od-dashboard-api/"):
        return jsonify(
            {
                "error": "not_found",
                "message": f"Unknown API route /{path}. Restart dashboard_server.py to load latest API routes.",
            }
        ), 404
    resp = send_from_directory(app.static_folder, path)
    low = path.lower()
    if low.endswith((".html", ".js", ".css")):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    elif low.endswith((".gif", ".jpg", ".jpeg", ".png")):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


def _warn_localhost_port_conflict(port: int) -> None:
    """pgAdmin 4 binds 127.0.0.1:5050; localhost then gets 401 instead of this dashboard."""
    if port != 5050:
        return
    try:
        import urllib.error
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/od-dashboard-api/health", timeout=2) as resp:
            body = resp.read(256)
            if b'"ok"' in body or b"health" in body:
                return
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            return
    except OSError:
        return
    print(
        "WARNING: http://127.0.0.1:5050/ is pgAdmin (401 Unauthorized), not this dashboard.\n"
        "  Stop pgAdmin or run:  $env:PORT='5055'; python scripts/dashboard_server.py",
        file=__import__("sys").stderr,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    _warn_localhost_port_conflict(port)
    print(f"Dashboard: http://127.0.0.1:{port}/")
    try:
        d = resolve_tables()
        if d.get("ok"):
            fam = d.get("family")
            print(
                "Resolved:",
                fam,
                d.get("emissions"),
                d.get("trips"),
                d.get("routes"),
                repr(d.get("zone")),
            )
            if fam == "routes_only":
                print("  (routes_only: KPIs/zone map read trip_routes_* + route_emissions_g; fast.)")
            elif fam == "building" and not os.environ.get("DASHBOARD_EMISSIONS_TABLE", "").strip():
                print(
                    "  Tip: for 100pct_ct route pipeline use routes_only:\n"
                    "    $env:DASHBOARD_FAMILY='routes_only'\n"
                    "    $env:DASHBOARD_ROUTES_TABLE='trip_routes_building_100pct_ct'"
                )
        else:
            print("WARNING:", d.get("message", ""))
    except Exception as ex:
        print("resolve_tables:", ex)
    print("  Building map: footprint polygons + /od-dashboard-api/building_footprint (restart required after code updates).")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
