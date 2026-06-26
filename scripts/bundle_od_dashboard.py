"""Pack / unpack a **portable OD dashboard** folder for another machine.

Creates a self-contained directory with ``dashboard/``, ``scripts/``, ``data/`` (Postgres
dump + island boundary GeoJSON).

**pack** (source machine)::

  python scripts/bundle_od_dashboard.py pack
  python scripts/bundle_od_dashboard.py pack --out-dir D:/exports/od_dashboard --include-building-jobs

**unpack** (target machine) — restore the database dump, enable PostGIS::

  python scripts/bundle_od_dashboard.py unpack --bundle-dir ./od_dashboard_export

**run** (target machine, from bundle root)::

  pip install -r requirements.txt
  python scripts/run_dashboard.py --db-name od_dashboard

Requires ``pg_dump`` / ``pg_restore`` on PATH (or set ``PG_BIN``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text  # noqa: E402

from od_table_names import apply_run_tag  # noqa: E402


def _import_db_utils():
    """Pack-only helper (PopGen source tree). Unpack uses pg_restore and does not need this."""
    try:
        import db_utils  # noqa: WPS433

        return db_utils
    except ImportError as exc:
        raise SystemExit(
            "pack requires scripts/db_utils.py from the PopGen2023 repo.\n"
            "On a target machine, restore with: python scripts/bundle_od_dashboard.py unpack ..."
        ) from exc

_SCRIPTS_DIR = Path(__file__).resolve().parent
_CANDIDATE_BUNDLE = _SCRIPTS_DIR.parent
if (
    (_CANDIDATE_BUNDLE / "manifest.json").is_file()
    and (_CANDIDATE_BUNDLE / "dashboard").is_dir()
):
    REPO_ROOT = _CANDIDATE_BUNDLE
    DATA_DIR = _CANDIDATE_BUNDLE / "data"
    DEFAULT_BUNDLE_ROOT = _CANDIDATE_BUNDLE
else:
    REPO_ROOT = _SCRIPTS_DIR.parent
    DATA_DIR = REPO_ROOT / "Data"
    DEFAULT_BUNDLE_ROOT = DATA_DIR / "od_dashboard_export"
DUMP_NAME = "od_dashboard_tables.dump"
MANIFEST_NAME = "manifest.json"
RUN_TAG = "od10"

RUNTIME_SCRIPTS = (
    "run_dashboard.py",
    "dashboard_server.py",
    "popgen_constants.py",
    "meeting_emissions_attribution.py",
    "zone_map_anchors.py",
    "od_table_names.py",
)

DASHBOARD_PAGES = (
    "od-dashboard.html",
    "od.html",
    "od-buildings.html",
    "od-flows.html",
    "od-zones-boundary.html",
)

DASHBOARD_ASSETS = (
    "assets/dashboard-spa-od.js",
    "assets/dashboard-host-od.js",
    "assets/dashboard-nav.js",
    "assets/dashboard-nav.css",
    "assets/dashboard-shell.css",
    "assets/dashboard-spa.css",
    "assets/dashboard-embed.css",
    "assets/dashboard-embed.js",
    "assets/dashboard-zone-ui.js",
    "assets/dashboard-redirect-od.js",
    "assets/loading-overlay.js",
    "assets/loading-overlay.css",
    "assets/gif-loading1-static.jpg",
    "assets/loading-complete-static.jpg",
    "assets/original/gif-loading1.gif",
    "assets/original/loading-complete.gif",
)

BOUNDARY_FILES = (
    "mtl_boundary_file.geojson",
    "mtl_boundary_file_padded.geojson",
)

ZONE_INPUT_FILES = (
    "zones.csv",
    "geo_zone_sp23.csv",
)

REQUIREMENTS = """flask>=3.0
psycopg2-binary>=2.9
flask-cors>=4.0
"""

# Mandatory Postgres tables (privacy-safe precomputes; no person_key / d_id).
CORE_TABLES = (
    "popgen_zones_geom",
    apply_run_tag("zone_incoming_flows", RUN_TAG),
    apply_run_tag("zone_flow_anchors", RUN_TAG),
    apply_run_tag("zone_emissions_categories", RUN_TAG),
    "buildings_footprint",
    apply_run_tag("building_emissions", RUN_TAG),
    "trips_route_emissions",
)

# Unified zone table OR legacy rules+dest pair.
ZONE_UNIFIED = apply_run_tag("zone_emissions", RUN_TAG)
ZONE_RULES = apply_run_tag("zone_emissions_rules", RUN_TAG)
ZONE_DEST = apply_run_tag("zone_emissions_dest", RUN_TAG)
OPTIONAL_TABLES = ("building_jobs",)


def _apply_conn_config(args) -> None:
    db_utils = _import_db_utils()
    host = str(args.host)
    port = str(int(args.port))
    user = str(args.user)
    password = str(args.password)
    dbname = str(args.dbname)
    db_utils.DB_URL = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"
    params = {
        "dbname": dbname,
        "user": user,
        "password": password,
        "host": host,
        "port": port,
    }
    import dashboard_server as ds  # noqa: WPS433

    ds.DB_PARAMS = dict(params)


def _pg_conn_args(args) -> dict[str, str]:
    out: dict[str, str] = {"dbname": args.dbname}
    if getattr(args, "host", None):
        out["host"] = args.host
    if getattr(args, "port", None) is not None:
        out["port"] = str(args.port)
    if getattr(args, "user", None):
        out["user"] = args.user
    if getattr(args, "password", None):
        out["password"] = args.password
    return out


def _pg_tool_argv(conn: dict[str, str]) -> list[str]:
    """psql / pg_restore connection flags (omit unset values)."""
    argv: list[str] = []
    if conn.get("host"):
        argv.extend(["-h", conn["host"]])
    if conn.get("port"):
        argv.extend(["-p", conn["port"]])
    if conn.get("user"):
        argv.extend(["-U", conn["user"]])
    if conn.get("dbname"):
        argv.extend(["-d", conn["dbname"]])
    return argv


def _find_pg_tool(name: str) -> str:
    pg_bin = os.environ.get("PG_BIN", "").strip()
    if pg_bin:
        cand = Path(pg_bin) / (name + (".exe" if os.name == "nt" else ""))
        if cand.is_file():
            return str(cand)
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        for base in (
            Path(r"C:\Program Files\PostgreSQL"),
            Path(r"C:\Program Files (x86)\PostgreSQL"),
        ):
            if not base.is_dir():
                continue
            for ver in sorted(base.iterdir(), reverse=True):
                exe = ver / "bin" / f"{name}.exe"
                if exe.is_file():
                    return str(exe)
    raise FileNotFoundError(
        f"{name} not found. Install PostgreSQL client tools or set PG_BIN to the bin directory."
    )


def _run(cmd: list[str], *, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("  $", " ".join(cmd), flush=True)
    return subprocess.run(cmd, env=env, check=check)


def _table_exists(conn, table: str, *, schema: str) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=:s AND table_name=:t LIMIT 1"
            ),
            {"s": schema, "t": table},
        ).first()
    )


def _resolve_pack_tables(conn, *, include_building_jobs: bool, schema: str) -> list[str]:
    tables = list(CORE_TABLES)
    if _table_exists(conn, ZONE_UNIFIED, schema=schema):
        tables.append(ZONE_UNIFIED)
    else:
        missing = [t for t in (ZONE_RULES, ZONE_DEST) if not _table_exists(conn, t, schema=schema)]
        if missing:
            raise SystemExit(
                "Missing OD zone emissions table(s). Need either "
                f"{schema}.{ZONE_UNIFIED} or both "
                f"{ZONE_RULES} and {ZONE_DEST}.\n"
                "Run: python scripts/preprocess_dashboard_od10_zone_emissions.py"
            )
        tables.extend([ZONE_RULES, ZONE_DEST])
    if include_building_jobs and _table_exists(conn, "building_jobs", schema=schema):
        tables.append("building_jobs")
    missing_core = [t for t in CORE_TABLES if not _table_exists(conn, t, schema=schema)]
    if missing_core:
        raise SystemExit(
            "Missing required table(s):\n  "
            + "\n  ".join(f"{schema}.{t}" for t in missing_core)
            + "\n\nBuild OD dashboard precomputes first, then run pack again."
        )
    return tables


def _copy_dashboard(out_dir: Path) -> None:
    src_root = REPO_ROOT / "Data" / "dashboard" if (REPO_ROOT / "Data" / "dashboard").is_dir() else None
    if src_root is None or not src_root.is_dir():
        raise SystemExit(f"Dashboard source not found under {REPO_ROOT / 'Data' / 'dashboard'}")
    dash_dir = out_dir / "dashboard"
    dash_dir.mkdir(parents=True, exist_ok=True)
    for page in DASHBOARD_PAGES:
        src = src_root / page
        if not src.is_file():
            raise SystemExit(f"Missing dashboard page: {src}")
        shutil.copy2(src, dash_dir / page)
    for rel in DASHBOARD_ASSETS:
        src = src_root / rel
        if not src.is_file():
            raise SystemExit(f"Missing dashboard asset: {src}")
        dst = dash_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _copy_scripts(out_dir: Path) -> None:
    scripts_dir = out_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    src_scripts = REPO_ROOT / "scripts" if (REPO_ROOT / "scripts").is_dir() else _SCRIPTS_DIR
    for name in RUNTIME_SCRIPTS:
        src = src_scripts / name
        if not src.is_file():
            raise SystemExit(f"Missing runtime script: {src}")
        shutil.copy2(src, scripts_dir / name)
    shutil.copy2(src_scripts / "bundle_od_dashboard.py", scripts_dir / "bundle_od_dashboard.py")


def _copy_data_files(out_dir: Path) -> tuple[list[str], list[str]]:
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "db").mkdir(parents=True, exist_ok=True)
    repo_data = REPO_ROOT / "Data"
    boundaries: list[str] = []
    for fname in BOUNDARY_FILES:
        src = repo_data / fname
        if src.is_file():
            shutil.copy2(src, data_dir / fname)
            boundaries.append(fname)
    inputs_dir = data_dir / "popgen_inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    zone_files: list[str] = []
    for fname in ZONE_INPUT_FILES:
        src = repo_data / "popgen_inputs" / fname
        if not src.is_file():
            raise SystemExit(f"Missing zone label file: {src}")
        shutil.copy2(src, inputs_dir / fname)
        zone_files.append(fname)
    return boundaries, zone_files


def _write_readme(out_dir: Path, *, dbname: str) -> None:
    readme = out_dir / "README.md"
    readme.write_text(
        f"""# OD dashboard export

Portable folder for the PM23 survey dashboard (`run_dashboard.py`).

## Layout

- `dashboard/` — static HTML / JS / CSS (SPA + zone / buildings / flows views)
- `scripts/` — Flask API server and dependencies
- `data/` — island boundary GeoJSON
- `data/db/` — PostgreSQL dump (`{DUMP_NAME}`)

## Target machine setup

1. Install **Python 3.10+**, **PostgreSQL 14+** with **PostGIS**.
2. Create database `{dbname}` (default: **od_dashboard**) with PostGIS enabled.
3. Restore the dump::

     python scripts/bundle_od_dashboard.py unpack --bundle-dir . --dbname {dbname}

4. Install Python deps::

     pip install -r requirements.txt

5. Start the server (from this folder)::

     python scripts/run_dashboard.py --db-name {dbname}

   Use your normal PostgreSQL connection settings (environment variables or libpq); only the database name is prescribed.

6. Open **http://127.0.0.1:5051/**

## Notes

- The dump contains **precomputed aggregates only** (no `person_key` / `d_id`).
- Island map filter uses `data/mtl_boundary_file.geojson`.
""",
        encoding="utf-8",
    )


def cmd_pack(args) -> int:
    db_utils = _import_db_utils()
    _apply_conn_config(args)
    out_dir = Path(args.out_dir).resolve() if args.out_dir else DEFAULT_BUNDLE_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    dump_path = out_dir / "data" / "db" / DUMP_NAME

    print(f"=== Pack OD dashboard bundle ===", flush=True)
    print(f"  -> {out_dir}", flush=True)

    engine = db_utils.get_engine()
    schema = db_utils.DB_SCHEMA
    with engine.connect() as conn:
        tables = _resolve_pack_tables(
            conn, include_building_jobs=args.include_building_jobs, schema=schema
        )
        counts: dict[str, int] = {}
        for t in tables:
            n = conn.execute(text(f'SELECT COUNT(*) FROM "{schema}"."{t}"')).scalar()
            counts[t] = int(n or 0)
            print(f"  {schema}.{t}: {counts[t]:,} rows", flush=True)

    pg_dump = _find_pg_tool("pg_dump")
    conn_args = _pg_conn_args(args)
    env = os.environ.copy()
    if conn_args["password"]:
        env["PGPASSWORD"] = conn_args["password"]

    dump_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        pg_dump,
        "-h", conn_args["host"],
        "-p", conn_args["port"],
        "-U", conn_args["user"],
        "-d", conn_args["dbname"],
        "-Fc",
        "--no-owner",
        "--no-acl",
        "-f", str(dump_path),
        "-n", schema,
    ]
    for t in tables:
        cmd.extend(["-t", f"{schema}.{t}"])
    print(f"Writing {dump_path} ...", flush=True)
    _run(cmd, env=env)

    _copy_dashboard(out_dir)
    _copy_scripts(out_dir)
    boundaries, zone_files = _copy_data_files(out_dir)
    (out_dir / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    _write_readme(out_dir, dbname=conn_args["dbname"])

    manifest = {
        "bundle_type": "od_dashboard",
        "run_tag": RUN_TAG,
        "schema": schema,
        "dbname": conn_args["dbname"],
        "tables": [f"{schema}.{t}" for t in tables],
        "row_counts": counts,
        "zone_table_mode": "unified" if ZONE_UNIFIED in tables else "split",
        "boundaries": boundaries,
        "zone_input_files": zone_files,
        "dump_file": f"data/db/{DUMP_NAME}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = out_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {manifest_path}", flush=True)

    if args.zip:
        zip_path = shutil.make_archive(str(out_dir), "zip", root_dir=out_dir.parent, base_dir=out_dir.name)
        print(f"Wrote {zip_path}", flush=True)

    size_mb = dump_path.stat().st_size / (1024 * 1024)
    print(f"\nBundle ready ({size_mb:.1f} MB dump).", flush=True)
    print("Copy the whole folder to the target machine, then:", flush=True)
    print(f"  python scripts/bundle_od_dashboard.py unpack --bundle-dir {out_dir.as_posix()}", flush=True)
    print(f"  python scripts/run_dashboard.py --bundle-root {out_dir.as_posix()}", flush=True)
    return 0


def _ensure_postgis(args) -> None:
    psql = _find_pg_tool("psql")
    conn = _pg_conn_args(args)
    env = os.environ.copy()
    if conn["password"]:
        env["PGPASSWORD"] = conn["password"]
    for sql in (
        "CREATE EXTENSION IF NOT EXISTS postgis",
    ):
        _run(
            [
                psql,
                *_pg_tool_argv(conn),
                "-v", "ON_ERROR_STOP=1",
                "-c", sql,
            ],
            env=env,
        )
    print("PostGIS extension OK.", flush=True)


def cmd_unpack(args) -> int:
    bundle_dir = Path(args.bundle_dir).resolve()
    manifest_path = bundle_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SystemExit(f"manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dump_rel = str(manifest.get("dump_file") or f"data/db/{DUMP_NAME}")
    dump_path = bundle_dir / dump_rel
    if not dump_path.is_file():
        raise SystemExit(f"dump not found: {dump_path}")

    print(f"=== Unpack OD dashboard bundle ===", flush=True)
    print(f"  from {bundle_dir}", flush=True)

    pg_restore = _find_pg_tool("pg_restore")
    conn = _pg_conn_args(args)
    env = os.environ.copy()
    if conn["password"]:
        env["PGPASSWORD"] = conn["password"]

    print("Restoring database dump ...", flush=True)
    restore_cmd = [
        pg_restore,
        *_pg_tool_argv(conn),
        "--no-owner",
        "--no-acl",
        "--clean",
        "--if-exists",
        str(dump_path),
    ]
    _run(restore_cmd, env=env, check=False)

    _ensure_postgis(args)

    print("\n=== Done ===", flush=True)
    print("Start the dashboard from the bundle folder:", flush=True)
    print(f"  cd {bundle_dir.as_posix()}", flush=True)
    print("  pip install -r requirements.txt", flush=True)
    print(
        f"  python scripts/run_dashboard.py --bundle-root . --db-name {conn['dbname']}",
        flush=True,
    )
    print("  -> http://127.0.0.1:5051/", flush=True)
    return 0


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else None


def _add_conn_flags(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--host", default=os.environ.get("PGHOST", ""), help="PostgreSQL host (or PGHOST)")
    ap.add_argument(
        "--port",
        type=int,
        default=_env_int("PGPORT"),
        help="PostgreSQL port (or PGPORT)",
    )
    ap.add_argument("--user", default=os.environ.get("PGUSER", ""), help="PostgreSQL user (or PGUSER)")
    ap.add_argument(
        "--password",
        default=os.environ.get("PGPASSWORD", ""),
        help="PostgreSQL password (or PGPASSWORD)",
    )
    ap.add_argument(
        "--dbname",
        default=os.environ.get("PGDATABASE", "od_dashboard"),
        help="PostgreSQL database name (default: od_dashboard)",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    pack = sub.add_parser("pack", help="Build portable dashboard/ + scripts/ + data/ folder.")
    pack.add_argument(
        "--out-dir",
        default="",
        help=f"Output folder (default: {DEFAULT_BUNDLE_ROOT.as_posix()}).",
    )
    pack.add_argument(
        "--include-building-jobs",
        action="store_true",
        help="Also dump popgen.building_jobs when present (optional buildings sidebar).",
    )
    pack.add_argument("--zip", action="store_true", help="Also create a .zip next to the bundle folder.")
    _add_conn_flags(pack)

    unpack = sub.add_parser("unpack", help="Restore data/db/*.dump into PostgreSQL.")
    unpack.add_argument(
        "--bundle-dir",
        required=True,
        help="Folder produced by pack (contains manifest.json).",
    )
    _add_conn_flags(unpack)

    args = ap.parse_args(argv)
    if args.command == "pack":
        return cmd_pack(args)
    if args.command == "unpack":
        return cmd_unpack(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
