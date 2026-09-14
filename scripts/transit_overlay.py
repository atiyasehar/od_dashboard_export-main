"""Montreal-region transit overlay from official GTFS (STM, REM, exo)."""

from __future__ import annotations

import csv
import io
import json
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

CACHE_TTL_SEC = 7 * 24 * 3600
USER_AGENT = "od-dashboard/1.0 (transit-overlay; local dashboard)"

FEEDS: tuple[tuple[str, str], ...] = (
    ("stm", "https://www.stm.info/sites/default/files/gtfs/gtfs_stm.zip"),
    ("exo", "https://exo.quebec/xdata/trains/google_transit.zip"),
    ("rem", "https://gtfs.gpmmom.ca/gtfs/gtfs.zip"),
)

_lock = threading.Lock()
_memory: dict[str, dict[str, Any]] = {}


def _cache_dir(bundle_root: Path) -> Path:
    path = bundle_root / "data" / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _zip_path(bundle_root: Path, feed_id: str) -> Path:
    return _cache_dir(bundle_root) / f"gtfs_{feed_id}.zip"


def _geojson_path(bundle_root: Path, group: str) -> Path:
    return _cache_dir(bundle_root) / f"transit_{group}_v2.json"


def _fresh(path: Path) -> bool:
    return path.is_file() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SEC


def _download(url: str, dest: Path) -> None:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=90) as resp:
        dest.write_bytes(resp.read())


def _ensure_zip(bundle_root: Path, feed_id: str, url: str) -> Path:
    path = _zip_path(bundle_root, feed_id)
    if _fresh(path) and zipfile.is_zipfile(path):
        return path
    tmp = path.with_suffix(".zip.part")
    _download(url, tmp)
    tmp.replace(path)
    return path


def _gtfs_member(zf: zipfile.ZipFile, filename: str) -> io.TextIOWrapper | None:
    target = filename.lower()
    for name in zf.namelist():
        if name.replace("\\", "/").rsplit("/", 1)[-1].lower() == target:
            return io.TextIOWrapper(zf.open(name), encoding="utf-8-sig", newline="")
    return None


def _read_csv(zf: zipfile.ZipFile, filename: str) -> list[dict[str, str]]:
    fh = _gtfs_member(zf, filename)
    if fh is None:
        return []
    with fh:
        return list(csv.DictReader(fh))


def _mode_for(feed_id: str, route: dict[str, str]) -> str:
    rtype = str(route.get("route_type") or "").strip()
    if feed_id == "rem":
        return "rem"
    if rtype == "1":
        return "metro"
    if rtype == "2":
        return "train"
    if rtype in ("0", "5"):
        return "tram"
    if rtype in ("3", "11"):
        return "bus"
    if rtype == "12":
        return "rem"
    return "other"


def _colour(route: dict[str, str], mode: str) -> str:
    raw = (route.get("route_color") or "").strip().lstrip("#")
    if len(raw) == 6 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return f"#{raw}"
    return {
        "metro": "#00a651",
        "rem": "#00b2a9",
        "train": "#6d28d9",
        "tram": "#f59e0b",
        "bus": "#64748b",
    }.get(mode, "#64748b")


def _simplify(coords: list[list[float]], max_pts: int = 120) -> list[list[float]]:
    if len(coords) <= max_pts:
        return coords
    step = max(1, len(coords) // max_pts)
    out = coords[::step]
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out


def _shapes_by_id(zf: zipfile.ZipFile) -> dict[str, list[list[float]]]:
    rows = _read_csv(zf, "shapes.txt")
    buckets: dict[str, list[tuple[int, float, float]]] = {}
    for row in rows:
        sid = (row.get("shape_id") or "").strip()
        if not sid:
            continue
        try:
            lat = float(row["shape_pt_lat"])
            lon = float(row["shape_pt_lon"])
            seq = int(float(row.get("shape_pt_sequence") or 0))
        except (KeyError, TypeError, ValueError):
            continue
        buckets.setdefault(sid, []).append((seq, lon, lat))
    out: dict[str, list[list[float]]] = {}
    for sid, pts in buckets.items():
        pts.sort(key=lambda t: t[0])
        coords = [[lon, lat] for _, lon, lat in pts]
        if len(coords) >= 2:
            out[sid] = _simplify(coords)
    return out


def gtfs_zip_to_layers(zip_path: Path, feed_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with zipfile.ZipFile(zip_path) as zf:
        routes = {
            (r.get("route_id") or "").strip(): r
            for r in _read_csv(zf, "routes.txt")
            if (r.get("route_id") or "").strip()
        }
        trips = _read_csv(zf, "trips.txt")
        shapes = _shapes_by_id(zf)
        trip_meta: dict[str, tuple[str, str]] = {}
        route_shapes: dict[str, dict[str, list[list[float]]]] = {}
        for trip in trips:
            rid = (trip.get("route_id") or "").strip()
            tid = (trip.get("trip_id") or "").strip()
            sid = (trip.get("shape_id") or "").strip()
            if rid not in routes:
                continue
            mode = _mode_for(feed_id, routes[rid])
            if mode == "other":
                continue
            if tid:
                trip_meta[tid] = (mode, _colour(routes[rid], mode))
            if sid in shapes:
                route_shapes.setdefault(rid, {})[sid] = shapes[sid]
        stop_hits = _stop_hits_from_stop_times(zf, trip_meta)
        stops_idx = _stops_index(zf)
    line_features = _line_features(feed_id, routes, route_shapes)
    stop_features = _stop_features(feed_id, stops_idx, stop_hits)
    return line_features, stop_features


def _line_features(
    feed_id: str,
    routes: dict[str, dict[str, str]],
    route_shapes: dict[str, dict[str, list[list[float]]]],
) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for rid, shape_map in route_shapes.items():
        route = routes[rid]
        mode = _mode_for(feed_id, route)
        if mode == "other":
            continue
        parts = [coords for coords in shape_map.values() if len(coords) >= 2]
        if not parts:
            continue
        name = (route.get("route_short_name") or route.get("route_long_name") or rid).strip()
        long_name = (route.get("route_long_name") or "").strip()
        geom = (
            {"type": "MultiLineString", "coordinates": parts}
            if len(parts) > 1
            else {"type": "LineString", "coordinates": parts[0]}
        )
        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "mode": mode,
                    "name": long_name or name,
                    "ref": name,
                    "colour": _colour(route, mode),
                    "operator": feed_id.upper(),
                    "network": feed_id.upper(),
                    "route_id": rid,
                    "feed": feed_id,
                },
            }
        )
    return features


def _stop_hits_from_stop_times(
    zf: zipfile.ZipFile,
    trip_meta: dict[str, tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    """stop_id -> {modes: set[str], colour: str}."""
    hits: dict[str, dict[str, Any]] = {}
    if not trip_meta:
        return hits
    fh = _gtfs_member(zf, "stop_times.txt")
    if fh is None:
        return hits
    with fh:
        for row in csv.DictReader(fh):
            tid = (row.get("trip_id") or "").strip()
            meta = trip_meta.get(tid)
            if not meta:
                continue
            sid = (row.get("stop_id") or "").strip()
            if not sid:
                continue
            mode, colour = meta
            rec = hits.get(sid)
            if rec is None:
                hits[sid] = {"modes": {mode}, "colour": colour}
            else:
                rec["modes"].add(mode)
    return hits


def _stops_index(zf: zipfile.ZipFile) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in _read_csv(zf, "stops.txt"):
        sid = (row.get("stop_id") or "").strip()
        if sid:
            out[sid] = row
    return out


def _resolve_station_id(stops_idx: dict[str, dict[str, str]], stop_id: str) -> str:
    row = stops_idx.get(stop_id) or {}
    parent = (row.get("parent_station") or "").strip()
    if parent:
        return parent
    return stop_id


def _stop_features(
    feed_id: str,
    stops_idx: dict[str, dict[str, str]],
    stop_hits: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for stop_id, hit in stop_hits.items():
        station_id = _resolve_station_id(stops_idx, stop_id)
        rec = merged.setdefault(
            station_id,
            {"modes": set(), "colour": hit.get("colour") or "#64748b"},
        )
        rec["modes"].update(hit.get("modes") or ())
        if "metro" in rec["modes"] or "rem" in rec["modes"]:
            rec["colour"] = hit.get("colour") or rec["colour"]
    features: list[dict[str, Any]] = []
    for station_id, rec in merged.items():
        row = stops_idx.get(station_id)
        if not row:
            continue
        loc = str(row.get("location_type") or "0").strip()
        if loc in ("2", "3", "4"):
            continue
        try:
            lat = float(row["stop_lat"])
            lon = float(row["stop_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        modes = sorted(rec["modes"])
        primary = "metro" if "metro" in rec["modes"] else (
            "rem" if "rem" in rec["modes"] else (
                "train" if "train" in rec["modes"] else modes[0]
            )
        )
        name = (row.get("stop_name") or station_id).strip()
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "mode": primary,
                    "modes": modes,
                    "name": name,
                    "ref": (row.get("stop_code") or "").strip(),
                    "colour": rec["colour"],
                    "operator": feed_id.upper(),
                    "network": feed_id.upper(),
                    "stop_id": station_id,
                    "feed": feed_id,
                },
            }
        )
    return features


def _empty_fc() -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": []}


def _as_fc(features: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features or []}


def _read_bundle(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    geojson = data.get("geojson") if isinstance(data.get("geojson"), dict) else None
    stops = data.get("stops") if isinstance(data.get("stops"), dict) else None
    if geojson is None and data.get("type") == "FeatureCollection":
        geojson = data
        stops = _empty_fc()
    if not geojson or geojson.get("type") != "FeatureCollection":
        return None
    if not stops or stops.get("type") != "FeatureCollection":
        stops = _empty_fc()
    return {
        "geojson": geojson,
        "stops": stops,
        "cached": True,
        "fetched_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        "age_sec": int(time.time() - path.stat().st_mtime),
        "feature_count": len(geojson.get("features") or []),
        "stop_count": len(stops.get("features") or []),
    }


def _build_from_gtfs(bundle_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lines: list[dict[str, Any]] = []
    stops: list[dict[str, Any]] = []
    errors: list[str] = []
    for feed_id, url in FEEDS:
        try:
            zpath = _ensure_zip(bundle_root, feed_id, url)
            line_feats, stop_feats = gtfs_zip_to_layers(zpath, feed_id)
            lines.extend(line_feats)
            stops.extend(stop_feats)
        except (URLError, TimeoutError, OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
            errors.append(f"{feed_id}: {exc}")
    if not lines:
        raise RuntimeError("No transit shapes loaded (" + "; ".join(errors) + ")")
    return lines, stops


def cache_is_warm(bundle_root: Path) -> bool:
    """True when rail+bus GeoJSON caches exist and are within TTL."""
    return _fresh(_geojson_path(bundle_root, "rail")) and _fresh(_geojson_path(bundle_root, "bus"))


def prefetch_transit_network(
    bundle_root: Path,
    *,
    allow_download: bool = True,
    background: bool = True,
) -> None:
    """Warm GTFS cache on dashboard start so the overlay is ready on first click.

    Downloads STM, exo, and REM zips only when the cache is missing or stale.
    Failures are logged; the HTTP server still starts.
    """

    def _run() -> None:
        try:
            if cache_is_warm(bundle_root):
                payload = get_transit_network(bundle_root, group="rail")
                print(
                    "Transit overlay: using cached GTFS "
                    f"({payload.get('feature_count', 0)} rail lines, "
                    f"{payload.get('stop_count', 0)} rail stops)",
                    flush=True,
                )
                return
            if not allow_download:
                print(
                    "Transit overlay: no cache yet (offline). "
                    "Turn the overlay on once while online, or copy data/cache/.",
                    flush=True,
                )
                return
            print("Transit overlay: first run — downloading STM, REM, exo GTFS…", flush=True)
            rail = get_transit_network(bundle_root, group="rail")
            bus = get_transit_network(bundle_root, group="bus")
            print(
                f"  Transit rail: {rail.get('feature_count', 0)} lines, "
                f"{rail.get('stop_count', 0)} stops",
                flush=True,
            )
            print(
                f"  Transit bus: {bus.get('feature_count', 0)} lines, "
                f"{bus.get('stop_count', 0)} stops",
                flush=True,
            )
        except Exception as exc:
            print(f"  Transit overlay prefetch skipped: {exc}", flush=True)

    if background:
        threading.Thread(target=_run, name="transit-prefetch", daemon=True).start()
    else:
        _run()


def get_transit_network(
    bundle_root: Path,
    *,
    group: str = "rail",
    refresh: bool = False,
) -> dict[str, Any]:
    group = "bus" if str(group).strip().lower() == "bus" else "rail"
    cache_file = _geojson_path(bundle_root, group)
    with _lock:
        if not refresh:
            mem = _memory.get(group)
            if mem and (time.time() - float(mem.get("_ts") or 0)) < CACHE_TTL_SEC:
                return {k: v for k, v in mem.items() if k != "_ts"}
            cached = _read_bundle(cache_file)
            if cached and int(cached.get("age_sec") or 0) < CACHE_TTL_SEC:
                cached["group"] = group
                cached["source"] = "gtfs"
                _memory[group] = {**cached, "_ts": time.time()}
                return cached
        try:
            lines, stops = _build_from_gtfs(bundle_root)
            now = datetime.now(timezone.utc).isoformat()
            built: dict[str, dict[str, Any]] = {}
            for g in ("rail", "bus"):
                line_feats = [
                    f for f in lines
                    if ((f.get("properties") or {}).get("mode") == "bus") == (g == "bus")
                ]
                stop_feats = [
                    f for f in stops
                    if ((f.get("properties") or {}).get("mode") == "bus") == (g == "bus")
                ]
                payload = {
                    "geojson": _as_fc(line_feats),
                    "stops": _as_fc(stop_feats),
                    "group": g,
                    "source": "gtfs",
                    "cached": False,
                    "fetched_at": now,
                    "feature_count": len(line_feats),
                    "stop_count": len(stop_feats),
                    "attribution": "STM, REM, exo",
                }
                _geojson_path(bundle_root, g).write_text(
                    json.dumps(
                        {"geojson": payload["geojson"], "stops": payload["stops"]},
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                built[g] = payload
                _memory[g] = {**payload, "_ts": time.time()}
            return built[group]
        except Exception:
            stale = _read_bundle(cache_file)
            if stale:
                stale["group"] = group
                stale["source"] = "gtfs"
                stale["stale"] = True
                return stale
            raise
