#!/usr/bin/env python3
"""Load data/popgen_inputs/zones.csv into PostgreSQL (geo_id -> zone_code map).

Required before running scripts/sql/ct_zone_views.sql.

Usage (from project root):
  python scripts/load_geo_zone_map.py
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import psycopg2

from dashboard_server import SCHEMA, _zones_geom_table, ensure_zones_geom_compat

ROOT = Path(__file__).resolve().parents[1]
ZONES_CSV = ROOT / "data" / "popgen_inputs" / "zones.csv"


def _db_params() -> dict:
    env_file = ROOT / "deploy.env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, val)
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", ""),
        "dbname": os.environ.get("PGDATABASE", "od_dashboard"),
    }


def main() -> int:
    if not ZONES_CSV.exists():
        print(f"Missing {ZONES_CSV}", file=sys.stderr)
        return 1
    schema = os.environ.get("PGSCHEMA", "public").strip() or "public"
    conn = psycopg2.connect(**_db_params())
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {schema}.geo_id_zone_map (
              geo_id   integer PRIMARY KEY,
              zone_code text NOT NULL
            )
            """
        )
        cur.execute(f"TRUNCATE {schema}.geo_id_zone_map")
        rows: list[tuple[int, str]] = []
        with ZONES_CSV.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                gid = int(str(row["geo_id"]).strip())
                zc = str(row["zone_code"]).strip()
                rows.append((gid, zc))
        cur.executemany(
            f"INSERT INTO {schema}.geo_id_zone_map (geo_id, zone_code) VALUES (%s, %s)",
            rows,
        )
        conn.commit()
        print(f"Loaded {len(rows)} rows into {schema}.geo_id_zone_map from {ZONES_CSV.name}")
        ensure_zones_geom_compat(cur)
        conn.commit()
        zt = _zones_geom_table(cur)
        if zt:
            print(f"Zone geometry table: {schema}.{zt}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
