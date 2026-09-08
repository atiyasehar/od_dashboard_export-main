"""Map anchor lat/lon for PopGen zones (flow arcs + zone choropleth).

Polygons use ST_PointOnSurface (centroid can fall in water on hourglass zones).
Placeholder ST_Point zones (grid filler with no real boundary) use mean routed
trip endpoint coordinates instead of the synthetic grid point.
"""

from __future__ import annotations

from dashboard_server import (
    SCHEMA,
    _column_exists,
    _table_exists,
    _zones_geom_table,
    ensure_zones_geom_compat,
)

# Zones smaller than this are treated like point placeholders (km² on spheroid).
_DEGEN_AREA_M2 = 1000


def _zone_geom_expr(g_alias: str = "g") -> str:
    return f"ST_MakeValid(ST_Force2D({g_alias}.geom::geometry))"


def zone_geom_is_degenerate_sql(g_alias: str = "g") -> str:
    ge = _zone_geom_expr(g_alias)
    return f"""(
        ST_GeometryType({ge}) IN ('ST_Point', 'ST_MultiPoint')
        OR COALESCE(ST_Area({ge}::geography), 0) < {_DEGEN_AREA_M2}
    )"""


def zone_point_on_surface_lat_sql(g_alias: str = "g") -> str:
    ge = _zone_geom_expr(g_alias)
    return f"ST_Y(ST_PointOnSurface({ge}))::double precision"


def zone_point_on_surface_lon_sql(g_alias: str = "g") -> str:
    ge = _zone_geom_expr(g_alias)
    return f"ST_X(ST_PointOnSurface({ge}))::double precision"


def _routes_has_trip_coords(cur, routes_table: str) -> bool:
    if not routes_table or not _table_exists(cur, routes_table):
        return False
    return all(
        _column_exists(cur, routes_table, c)
        for c in ("orig_geo_id", "dest_geo_id", "orig_lat", "orig_lon", "dest_lat", "dest_lon")
    )


def create_trip_endpoint_centroids_temp(
    cur,
    routes_table: str,
    geo_ids: set[str] | None = None,
) -> bool:
    """Materialize tmp_zone_trip_orig_cent / tmp_zone_trip_dest_cent."""
    cur.execute("DROP TABLE IF EXISTS tmp_zone_trip_orig_cent")
    cur.execute("DROP TABLE IF EXISTS tmp_zone_trip_dest_cent")
    if not _routes_has_trip_coords(cur, routes_table):
        cur.execute(
            "CREATE TEMP TABLE tmp_zone_trip_orig_cent "
            "(geo_id text PRIMARY KEY, lat double precision, lon double precision)"
        )
        cur.execute(
            "CREATE TEMP TABLE tmp_zone_trip_dest_cent "
            "(geo_id text PRIMARY KEY, lat double precision, lon double precision)"
        )
        return False

    ids = sorted({str(g).strip() for g in (geo_ids or []) if g is not None and str(g).strip()})
    orig_filter = ""
    dest_filter = ""
    orig_params: list = []
    dest_params: list = []
    if ids:
        orig_filter = "AND split_part(trim(orig_geo_id::text), '.', 1) = ANY(%s)"
        dest_filter = "AND split_part(trim(dest_geo_id::text), '.', 1) = ANY(%s)"
        orig_params.append(ids)
        dest_params.append(ids)

    cur.execute(
        f"""
        CREATE TEMP TABLE tmp_zone_trip_orig_cent AS
        SELECT split_part(trim(orig_geo_id::text), '.', 1) AS geo_id,
               AVG(orig_lat)::double precision AS lat,
               AVG(orig_lon)::double precision AS lon
        FROM {SCHEMA}.{routes_table}
        WHERE orig_geo_id IS NOT NULL
          AND orig_lat IS NOT NULL AND orig_lon IS NOT NULL
          {orig_filter}
        GROUP BY 1
        """,
        tuple(orig_params),
    )
    cur.execute("CREATE INDEX ON tmp_zone_trip_orig_cent(geo_id)")
    cur.execute(
        f"""
        CREATE TEMP TABLE tmp_zone_trip_dest_cent AS
        SELECT split_part(trim(dest_geo_id::text), '.', 1) AS geo_id,
               AVG(dest_lat)::double precision AS lat,
               AVG(dest_lon)::double precision AS lon
        FROM {SCHEMA}.{routes_table}
        WHERE dest_geo_id IS NOT NULL
          AND dest_lat IS NOT NULL AND dest_lon IS NOT NULL
          {dest_filter}
        GROUP BY 1
        """,
        tuple(dest_params),
    )
    cur.execute("CREATE INDEX ON tmp_zone_trip_dest_cent(geo_id)")
    return True


def _snap_lat_lon_sql(lat_expr: str, lon_expr: str, *, geom_table: str) -> tuple[str, str]:
    """Snap (lat, lon) to the nearest real zone polygon when the point is not on land."""
    pt = f"ST_SetSRID(ST_MakePoint(({lon_expr})::double precision, ({lat_expr})::double precision), 4326)"
    poly = f"""
        ST_GeometryType(ST_MakeValid(ST_Force2D(p.geom::geometry)))
            IN ('ST_Polygon', 'ST_MultiPolygon')
        AND COALESCE(ST_Area(p.geom::geography), 0) >= {_DEGEN_AREA_M2}
    """
    nearest_lat = f"""
        (SELECT CASE
            WHEN ST_Contains(p.geom, {pt}) THEN ({lat_expr})::double precision
            ELSE ST_Y(ST_ClosestPoint(ST_MakeValid(ST_Force2D(p.geom::geometry)), {pt}))::double precision
         END
         FROM {SCHEMA}.{geom_table} p
         WHERE {poly} AND p.geom && ST_Expand({pt}, 0.08)
         ORDER BY p.geom <-> {pt}
         LIMIT 1)
    """
    nearest_lon = f"""
        (SELECT CASE
            WHEN ST_Contains(p.geom, {pt}) THEN ({lon_expr})::double precision
            ELSE ST_X(ST_ClosestPoint(ST_MakeValid(ST_Force2D(p.geom::geometry)), {pt}))::double precision
         END
         FROM {SCHEMA}.{geom_table} p
         WHERE {poly} AND p.geom && ST_Expand({pt}, 0.08)
         ORDER BY p.geom <-> {pt}
         LIMIT 1)
    """
    snap_lat = f"COALESCE({nearest_lat}, ({lat_expr})::double precision)"
    snap_lon = f"COALESCE({nearest_lon}, ({lon_expr})::double precision)"
    return snap_lat, snap_lon


def _snap_point(cur, lat: float | None, lon: float | None, *, geom_table: str) -> tuple[float | None, float | None]:
    """Snap one WGS84 point to the nearest real zone polygon if it is not on land."""
    if lat is None or lon is None:
        return lat, lon
    poly = f"""
        ST_GeometryType(ST_MakeValid(ST_Force2D(p.geom::geometry)))
            IN ('ST_Polygon', 'ST_MultiPolygon')
        AND COALESCE(ST_Area(p.geom::geography), 0) >= {_DEGEN_AREA_M2}
    """
    cur.execute(
        f"""
        WITH pt AS (
            SELECT ST_SetSRID(ST_MakePoint(%s::double precision, %s::double precision), 4326) AS g
        )
        SELECT
            CASE
              WHEN ST_Contains(p.geom, pt.g) THEN %s::double precision
              ELSE ST_Y(ST_ClosestPoint(ST_MakeValid(ST_Force2D(p.geom::geometry)), pt.g))::double precision
            END,
            CASE
              WHEN ST_Contains(p.geom, pt.g) THEN %s::double precision
              ELSE ST_X(ST_ClosestPoint(ST_MakeValid(ST_Force2D(p.geom::geometry)), pt.g))::double precision
            END
        FROM {SCHEMA}.{geom_table} p
        CROSS JOIN pt
        WHERE {poly}
          AND p.geom && ST_Expand(pt.g, 0.08)
        ORDER BY p.geom <-> pt.g
        LIMIT 1
        """,
        (float(lon), float(lat), float(lat), float(lon)),
    )
    row = cur.fetchone()
    if not row:
        return lat, lon
    return row[0], row[1]


def _anchor_from_trip_sql(
    trip_lat: str,
    trip_lon: str,
    fallback_lat: str,
    fallback_lon: str,
    *,
    snap: bool,
    geom_table: str,
) -> tuple[str, str]:
    raw_lat = f"COALESCE({trip_lat}, {fallback_lat})"
    raw_lon = f"COALESCE({trip_lon}, {fallback_lon})"
    if not snap:
        return raw_lat, raw_lon
    return _snap_lat_lon_sql(raw_lat, raw_lon, geom_table=geom_table)


def create_zone_flow_anchors_temp(cur, *, routes_table: str, has_geom: bool) -> str:
    """
    Build tmp_zone_flow_anchors (geo_id, orig_lat, orig_lon, dest_lat, dest_lon, map_lat, map_lon).
    Returns temp table name.
    """
    table = "tmp_zone_flow_anchors"
    cur.execute(f"DROP TABLE IF EXISTS {table}")
    create_trip_endpoint_centroids_temp(cur, routes_table)

    zgeom = ensure_zones_geom_compat(cur) or _zones_geom_table(cur)
    degen = zone_geom_is_degenerate_sql("g")
    pos_lat = zone_point_on_surface_lat_sql("g")
    pos_lon = zone_point_on_surface_lon_sql("g")
    orig_lat_raw, orig_lon_raw = _anchor_from_trip_sql(
        "tco.lat", "tco.lon", pos_lat, pos_lon, snap=False, geom_table=zgeom or "zones_geom"
    )
    dest_lat_raw, dest_lon_raw = _anchor_from_trip_sql(
        "tcd.lat", "tcd.lon", pos_lat, pos_lon, snap=False, geom_table=zgeom or "zones_geom"
    )
    map_lat_raw = f"CASE WHEN {degen} THEN COALESCE(tco.lat, tcd.lat, {pos_lat}) ELSE {pos_lat} END"
    map_lon_raw = f"CASE WHEN {degen} THEN COALESCE(tco.lon, tcd.lon, {pos_lon}) ELSE {pos_lon} END"
    gt = zgeom or "zones_geom"
    orig_lat, orig_lon = _snap_lat_lon_sql(
        f"CASE WHEN {degen} THEN {orig_lat_raw} ELSE {pos_lat} END",
        f"CASE WHEN {degen} THEN {orig_lon_raw} ELSE {pos_lon} END",
        geom_table=gt,
    )
    dest_lat, dest_lon = _snap_lat_lon_sql(
        f"CASE WHEN {degen} THEN {dest_lat_raw} ELSE {pos_lat} END",
        f"CASE WHEN {degen} THEN {dest_lon_raw} ELSE {pos_lon} END",
        geom_table=gt,
    )
    map_lat, map_lon = _snap_lat_lon_sql(map_lat_raw, map_lon_raw, geom_table=gt)

    if has_geom and zgeom:
        cur.execute(
            f"""
            CREATE TEMP TABLE {table} AS
            SELECT g.geo_id::text AS geo_id,
                   {orig_lat} AS orig_lat,
                   {orig_lon} AS orig_lon,
                   {dest_lat} AS dest_lat,
                   {dest_lon} AS dest_lon,
                   {map_lat} AS map_lat,
                   {map_lon} AS map_lon
            FROM {SCHEMA}.{zgeom} g
            LEFT JOIN tmp_zone_trip_orig_cent tco ON tco.geo_id = g.geo_id::text
            LEFT JOIN tmp_zone_trip_dest_cent tcd ON tcd.geo_id = g.geo_id::text
            """
        )
    else:
        cur.execute(
            f"""
            CREATE TEMP TABLE {table} AS
            SELECT geo_id,
                   lat AS orig_lat, lon AS orig_lon,
                   lat AS dest_lat, lon AS dest_lon,
                   lat AS map_lat, lon AS map_lon
            FROM tmp_zone_trip_orig_cent
            UNION
            SELECT d.geo_id,
                   COALESCE(o.lat, d.lat), COALESCE(o.lon, d.lon),
                   d.lat, d.lon,
                   COALESCE(o.lat, d.lat), COALESCE(o.lon, d.lon)
            FROM tmp_zone_trip_dest_cent d
            LEFT JOIN tmp_zone_trip_orig_cent o ON o.geo_id = d.geo_id
            WHERE NOT EXISTS (SELECT 1 FROM tmp_zone_trip_orig_cent o2 WHERE o2.geo_id = d.geo_id)
            """
        )
    cur.execute(f"CREATE INDEX ON {table}(geo_id)")
    return table


def materialize_zone_flow_anchors_table(
    cur,
    *,
    routes_table: str,
    table_name: str = "zone_flow_anchors_od10",
) -> None:
    """Persist anchors for fast zone_map / flows reads (built during --flows-only)."""
    has_geom = bool(_zones_geom_table(cur))
    create_zone_flow_anchors_temp(cur, routes_table=routes_table, has_geom=has_geom)
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{table_name}")
    cur.execute(
        f"""
        CREATE TABLE {SCHEMA}.{table_name} AS
        SELECT geo_id, orig_lat, orig_lon, dest_lat, dest_lon, map_lat, map_lon
        FROM tmp_zone_flow_anchors
        """
    )
    cur.execute(f"CREATE INDEX ON {SCHEMA}.{table_name}(geo_id)")


def fetch_zone_flow_anchors(cur, routes_table: str, geo_ids: set[str]) -> dict[str, dict]:
    """Lookup anchors for a set of geo_ids (used to patch precomputed flow tables at read time)."""
    ids = sorted({str(g).strip() for g in geo_ids if g is not None and str(g).strip()})
    if not ids:
        return {}
    zgeom = ensure_zones_geom_compat(cur) or _zones_geom_table(cur)
    if not zgeom:
        return {}
    create_trip_endpoint_centroids_temp(cur, routes_table, geo_ids=set(ids))
    degen = zone_geom_is_degenerate_sql("g")
    pos_lat = zone_point_on_surface_lat_sql("g")
    pos_lon = zone_point_on_surface_lon_sql("g")
    orig_lat_raw, orig_lon_raw = _anchor_from_trip_sql(
        "tco.lat", "tco.lon", pos_lat, pos_lon, snap=False, geom_table=zgeom
    )
    dest_lat_raw, dest_lon_raw = _anchor_from_trip_sql(
        "tcd.lat", "tcd.lon", pos_lat, pos_lon, snap=False, geom_table=zgeom
    )
    map_lat_raw = f"CASE WHEN {degen} THEN COALESCE(tco.lat, tcd.lat, {pos_lat}) ELSE {pos_lat} END"
    map_lon_raw = f"CASE WHEN {degen} THEN COALESCE(tco.lon, tcd.lon, {pos_lon}) ELSE {pos_lon} END"
    cur.execute(
        f"""
        SELECT g.geo_id::text AS geo_id,
               ({degen}) AS is_degen,
               CASE WHEN {degen} THEN {orig_lat_raw} ELSE {pos_lat} END AS orig_lat,
               CASE WHEN {degen} THEN {orig_lon_raw} ELSE {pos_lon} END AS orig_lon,
               CASE WHEN {degen} THEN {dest_lat_raw} ELSE {pos_lat} END AS dest_lat,
               CASE WHEN {degen} THEN {dest_lon_raw} ELSE {pos_lon} END AS dest_lon,
               {map_lat_raw} AS map_lat,
               {map_lon_raw} AS map_lon
        FROM {SCHEMA}.{zgeom} g
        LEFT JOIN tmp_zone_trip_orig_cent tco ON tco.geo_id = g.geo_id::text
        LEFT JOIN tmp_zone_trip_dest_cent tcd ON tcd.geo_id = g.geo_id::text
        WHERE g.geo_id::text = ANY(%s)
        """,
        (ids,),
    )
    out: dict[str, dict] = {}
    for geo_id, is_degen, ola, olo, dla, dlo, mla, mlo in cur.fetchall():
        if is_degen:
            ola, olo = _snap_point(cur, ola, olo, geom_table=zgeom)
            dla, dlo = _snap_point(cur, dla, dlo, geom_table=zgeom)
            mla, mlo = _snap_point(cur, mla, mlo, geom_table=zgeom)
        out[str(geo_id)] = {
            "orig_lat": ola,
            "orig_lon": olo,
            "dest_lat": dla,
            "dest_lon": dlo,
            "map_lat": mla,
            "map_lon": mlo,
        }
    return out


def patch_flow_payload_anchors(payload: dict, anchors: dict[str, dict]) -> None:
    """Mutate incoming-flow JSON payload in place."""
    dest_id = str(payload.get("dest_geo_id") or "")
    da = anchors.get(dest_id)
    if da:
        dlat = da.get("map_lat")
        dlon = da.get("map_lon")
        if dlat is None or dlon is None:
            dlat = da.get("dest_lat")
            dlon = da.get("dest_lon")
        if dlat is not None and dlon is not None:
            payload["dest_lat"] = dlat
            payload["dest_lon"] = dlon
    for flow in payload.get("flows") or []:
        oid = str(flow.get("orig_geo_id") or "")
        oa = anchors.get(oid)
        if not oa:
            continue
        if oa.get("orig_lat") is not None and oa.get("orig_lon") is not None:
            flow["orig_lat"] = oa["orig_lat"]
            flow["orig_lon"] = oa["orig_lon"]
        flow["dest_lat"] = payload.get("dest_lat")
        flow["dest_lon"] = payload.get("dest_lon")
