# OD dashboard export

Portable **PM23 survey** dashboard — CMM island-eligible car trips, zone maps, buildings, and flows.

**Runtime needs only:** this folder + PostgreSQL database **`od_dashboard`** (schema **`public`**). No PopGen2023 repo or `Synthetic2023` database on the target machine.

| Resource | Link |
|----------|------|
| **Full guide (HTML, diagrams)** | [`README.html`](README.html) |
| **GitHub** | [github.com/atiyasehar/od_dashboard_export-main](https://github.com/atiyasehar/od_dashboard_export-main) |
| **DB dump** | [`data/db/od_dashboard_tables.dump`](data/db/) — if missing, download from [OneDrive](https://liveconcordia-my.sharepoint.com/:u:/g/personal/atiya_atiya_concordia_ca/IQDAgc05pD40SK9YSnDwFZURAcydIl6xbQHGRnafPX5VfIE?e=j6PxkO) (~238 MB; not in git) |

Table row counts and bundle metadata: **`manifest.json`**.

**All connection settings** (HTTP port, PostgreSQL host/port/user/password, URL prefixes, offline mode) are configured in **`deploy.env`** — see [Configuration reference](#configuration-reference) and [Offline / air-gapped deployment](#offline--air-gapped-deployment). The README never assumes a specific port on your machine.

---

## Quick command reference (copy-paste)

Replace values in **`deploy.env`** first (`cp` or `copy deploy.example.env deploy.env`, then edit). All commands below read **`PGHOST`**, **`PGPORT`**, **`PORT`**, etc. from that file.

### Windows (PowerShell, from project root)

```powershell
# 1. Config
copy deploy.example.env deploy.env
# Edit deploy.env in your editor (PORT, PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, PGSCHEMA)
# Optional: DASH_OFFLINE=true for air-gapped deploy (no CDN or map tiles)

# 2. Python
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 3. Load deploy.env into this shell
Get-Content deploy.env | ForEach-Object {
  $line = $_.Trim()
  if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+)=(.*)$") {
    Set-Item -Path "env:$($matches[1].Trim())" -Value $matches[2].Trim().Trim('"').Trim("'")
  }
}

# 4. Restore DB (create od_dashboard in pgAdmin first, or: createdb via psql)
pg_restore -h $env:PGHOST -p $env:PGPORT -U $env:PGUSER -d $env:PGDATABASE `
  --no-owner --no-acl --clean --if-exists data/db/od_dashboard_tables.dump
psql -h $env:PGHOST -p $env:PGPORT -U $env:PGUSER -d $env:PGDATABASE `
  -c "CREATE EXTENSION IF NOT EXISTS postgis;"

# 5. Run
.\scripts\start_dashboard.ps1

# 6. Verify
Invoke-RestMethod "http://127.0.0.1:$env:PORT/api/health" | Select-Object ok, db_host, db_port, dbname, schema

# 7. Open in browser
Start-Process "http://127.0.0.1:$env:PORT/"
```

**Alternative restore (unpack script):**

```powershell
python scripts/bundle_od_dashboard.py unpack --bundle-dir . `
  --host $env:PGHOST --port $env:PGPORT --user $env:PGUSER `
  --password $env:PGPASSWORD --dbname $env:PGDATABASE
```

**Alternative run (manual):**

```powershell
python scripts/run_dashboard.py --bundle-root .
```

---

### Linux / macOS (bash, from project root)

```bash
# 1. Config
cp deploy.example.env deploy.env
# Edit deploy.env (PORT, PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, PGSCHEMA)
# Optional: DASH_OFFLINE=true for air-gapped deploy (no CDN or map tiles)

# 2. Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Load deploy.env into this shell
set -a
source <(grep -v '^#' deploy.env | grep -v '^[[:space:]]*$' | sed 's/^/export /')
set +a

# 4. Restore DB
createdb "$PGDATABASE" 2>/dev/null || true
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
  -c "CREATE EXTENSION IF NOT EXISTS postgis;"
pg_restore -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
  --no-owner --no-acl --clean --if-exists data/db/od_dashboard_tables.dump

# 5. Run
chmod +x scripts/start_dashboard.sh
./scripts/start_dashboard.sh

# 6. Verify
curl -s "http://127.0.0.1:${PORT}/api/health" | python3 -m json.tool

# 7. Open in browser (macOS)
open "http://127.0.0.1:${PORT}/"
# Linux: xdg-open "http://127.0.0.1:${PORT}/"
```

**Alternative restore (unpack script):**

```bash
python3 scripts/bundle_od_dashboard.py unpack --bundle-dir . \
  --host "$PGHOST" --port "$PGPORT" --user "$PGUSER" \
  --password "$PGPASSWORD" --dbname "$PGDATABASE"
```

**Alternative run (manual):**

```bash
python3 scripts/run_dashboard.py --bundle-root .
```

**Stop a stale server on your HTTP port:**

```bash
fuser -k "${PORT}"/tcp
# or: lsof -ti :"${PORT}" | xargs kill
```

```powershell
# Windows
Get-NetTCPConnection -LocalPort $env:PORT -State Listen | ForEach-Object {
  Stop-Process -Id $_.OwningProcess -Force
}
```

---

## Local deployment (step by step)

### 1. Prerequisites

| Requirement | Notes |
|-------------|--------|
| **Python 3.10+** | `python -m venv .venv` recommended |
| **PostgreSQL 14+** | With **PostGIS** extension |
| **`pg_restore` / `psql`** | On `PATH`, or set `PG_BIN` in `deploy.env` |
| **Internet** | Optional — set `DASH_OFFLINE=true` for air-gapped deploy (bundled JS/CSS, no map tiles) |

### 2. Get the code and database dump

```bash
git clone https://github.com/atiyasehar/od_dashboard_export-main.git
cd od_dashboard_export-main
mkdir -p data/db
# Copy od_dashboard_tables.dump into data/db/ (OneDrive link in table above)
```

### 3. Configure (`deploy.env`) — do this first

**Linux / macOS:**

```bash
cp deploy.example.env deploy.env
```

**Windows:**

```powershell
copy deploy.example.env deploy.env
```

Edit **`deploy.env`** and set at least:

| Variable | Your value |
|----------|------------|
| `PORT` | HTTP port for the dashboard (your choice) |
| `PGHOST` | PostgreSQL host |
| `PGPORT` | PostgreSQL port **your cluster uses** |
| `PGUSER` | PostgreSQL user |
| `PGPASSWORD` | PostgreSQL password |
| `PGDATABASE` | Database name (default `od_dashboard`) |
| `PGSCHEMA` | Schema with restored tables (default `public`) |
| `DASH_OFFLINE` | `true` for air-gapped deploy; omit or `false` for online (default) |

Optional: `DASH_URL_PREFIX`, `DASH_API_PREFIX`, `DASH_SHOW_BOUNDARY_BUTTON`, `DASH_OFFLINE`, `PG_BIN`.

Set **`DASH_OFFLINE=true`** when the target machine has no internet or blocks CDNs and map tile hosts. See [Offline / air-gapped deployment](#offline--air-gapped-deployment).

`deploy.env` is **git-ignored**. The server and start scripts load it automatically.

---

### Windows

#### 4. Python dependencies

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

#### 5. Restore PostgreSQL

Create database **`od_dashboard`** (or the name in `PGDATABASE`), then restore.

**Option A — pgAdmin:** Create DB → run PostGIS SQL (below) → Restore `data\db\od_dashboard_tables.dump` (Custom format; **Do not save** Owner/Privileges).

**Option B — command line** (load `deploy.env`, then use your variables):

```powershell
Get-Content deploy.env | ForEach-Object {
  $line = $_.Trim()
  if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+)=(.*)$") {
    Set-Item -Path "env:$($matches[1].Trim())" -Value $matches[2].Trim().Trim('"').Trim("'")
  }
}
pg_restore -h $env:PGHOST -p $env:PGPORT -U $env:PGUSER -d $env:PGDATABASE `
  --no-owner --no-acl --clean --if-exists data/db/od_dashboard_tables.dump
psql -h $env:PGHOST -p $env:PGPORT -U $env:PGUSER -d $env:PGDATABASE `
  -c "CREATE EXTENSION IF NOT EXISTS postgis;"
```

**Option C — unpack script** (reads the same flags from `deploy.env` if exported, or pass explicitly):

```powershell
python scripts/bundle_od_dashboard.py unpack --bundle-dir . `
  --host $env:PGHOST --port $env:PGPORT --user $env:PGUSER `
  --password $env:PGPASSWORD --dbname $env:PGDATABASE
```

PostGIS SQL (pgAdmin or psql):

```sql
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO postgres;
GRANT ALL ON SCHEMA public TO public;
CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public;
```

#### 6. Run

```powershell
.\scripts\start_dashboard.ps1
```

Or manually (settings from `deploy.env` are auto-loaded by `run_dashboard.py`):

```powershell
python scripts/run_dashboard.py --bundle-root .
```

---

### Linux / macOS

#### 4. Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

#### 5. Restore PostgreSQL

Install PostgreSQL + PostGIS if needed (Ubuntu: `postgresql` + `postgis`; macOS: `brew install postgresql postgis`).

Load **`deploy.env`**, then restore:

```bash
set -a
source <(grep -v '^#' deploy.env | grep -v '^[[:space:]]*$' | sed 's/^/export /')
set +a

createdb "$PGDATABASE" 2>/dev/null || true
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
  -c "CREATE EXTENSION IF NOT EXISTS postgis;"

pg_restore -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
  --no-owner --no-acl --clean --if-exists data/db/od_dashboard_tables.dump
```

Or unpack script:

```bash
python3 scripts/bundle_od_dashboard.py unpack --bundle-dir . \
  --host "$PGHOST" --port "$PGPORT" --user "$PGUSER" \
  --password "$PGPASSWORD" --dbname "$PGDATABASE"
```

If client tools are not on `PATH`, set **`PG_BIN`** in `deploy.env` to your PostgreSQL `bin` directory.

#### 6. Run

```bash
chmod +x scripts/start_dashboard.sh
./scripts/start_dashboard.sh
```

Or:

```bash
python3 scripts/run_dashboard.py --bundle-root .
```

---

### 7. Verify (all platforms)

Use the **`PORT`** from your `deploy.env`:

**Linux / macOS:**

```bash
source <(grep -v '^#' deploy.env | grep -v '^[[:space:]]*$' | sed 's/^/export /')
curl -s "http://127.0.0.1:${PORT}/api/health" | python3 -m json.tool
```

**Windows** (after loading `deploy.env` as in step 5):

```powershell
Invoke-RestMethod "http://127.0.0.1:$env:PORT/api/health" | Select-Object ok, db_host, db_port, dbname, schema
```

Expected:

- `"ok": true`
- `"db_port"` matches **`PGPORT`** in your `deploy.env`
- `"schema"` matches **`PGSCHEMA`**
- `"deploy"."offline"` is `true` when `DASH_OFFLINE=true`
- `building_emissions` shows ~924k rows

Verify tables (in `psql`, using your connection variables):

```sql
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
    'zone_emissions_od10', 'building_emissions_od10', 'buildings_footprint', 'popgen_zones_geom'
  )
ORDER BY table_name;
```

---

## Configuration reference

All settings: **`deploy.env`**, environment variables, or CLI flags on `run_dashboard.py`. CLI flags override env.

### HTTP server

| Setting | Env | CLI flag |
|---------|-----|----------|
| Listen port | `PORT` | `--port` |
| Bind address | — | `--host` |
| Bundle folder | `POPGEN_BUNDLE_ROOT` | `--bundle-root` |
| Env file path | `DASH_ENV_FILE` | `--env-file` |

### PostgreSQL

| Setting | Env | CLI flag |
|---------|-----|----------|
| Host | `PGHOST` | `--db-host` |
| Port | `PGPORT` | `--db-port` |
| Database | `PGDATABASE` | `--db-name` |
| User | `PGUSER` | `--db-user` |
| Password | `PGPASSWORD` | `--db-password` |
| Schema | `PGSCHEMA` | `--db-schema` |

### Reverse proxy / shared host

| Setting | Env | CLI flag |
|---------|-----|----------|
| URL mount prefix | `DASH_URL_PREFIX` | `--url-prefix` |
| API prefix | `DASH_API_PREFIX` | `--api-prefix` |
| Show Boundaries nav | `DASH_SHOW_BOUNDARY_BUTTON` | `--show-boundary-button` |
| Offline / air-gapped | `DASH_OFFLINE` | `--offline` |

See [Offline / air-gapped deployment](#offline--air-gapped-deployment) for full details.

Example behind NGINX (set prefixes in `deploy.env` or flags):

```bash
python scripts/run_dashboard.py --bundle-root . \
  --url-prefix /your-mount-path \
  --api-prefix /your-api-prefix \
  --show-boundary-button false
```

Health URL pattern: `https://your-host<url-prefix><api-prefix>/health`

### PostgreSQL tools (restore / dump)

| Setting | Purpose |
|---------|---------|
| `PG_BIN` | Directory containing `pg_dump`, `pg_restore`, `psql` if not on `PATH` |

### Advanced (table names / metrics; rarely needed)

| Env | Purpose |
|-----|---------|
| `OD10_RUN_TAG` | Suffix for table names (`zone_emissions_od10`, etc.) |
| `OD10_METRICS` | `weighted` or `legs` |
| `OD10_BUILDING_POPULATION_ALLOC` | Building capacity allocation mode |
| `OD10_BUILDING_EMISSIONS_TABLE` | Override building emissions table name |
| `OD10_ZONE_EMISSIONS_TABLE` | Override zone emissions table name |

---

## Offline / air-gapped deployment

Use offline mode when the dashboard runs on a machine **without internet access**, or when outbound HTTP to CDNs and map tile servers is blocked. The UI still talks to **your local PostgreSQL**; only external assets are removed.

### Enable

In **`deploy.env`**:

```env
DASH_OFFLINE=true
```

Or pass on the CLI (overrides env):

```bash
python scripts/run_dashboard.py --bundle-root . --offline true
```

Restart after changing the setting. The start scripts (`start_dashboard.ps1` / `start_dashboard.sh`) load `deploy.env` automatically.

### Online vs offline

| | Online (default) | Offline (`DASH_OFFLINE=true`) |
|--|------------------|-------------------------------|
| **Leaflet / Chart.js** | Loaded from unpkg / jsDelivr CDN | Bundled in `dashboard/assets/vendor/` |
| **Fonts** | Google Fonts (DM Sans, Outfit) | System UI fonts |
| **Map basemap** | Carto/OSM raster tiles | Dark grid background (no tile requests) |
| **Zone / building / flow layers** | Yes | Yes (same API data from PostgreSQL) |
| **Charts (dashboard, buildings)** | Yes | Yes |
| **Internet required for UI** | Yes (after first load, tiles refresh) | No |

Maps in offline mode show a plain grid behind zone polygons, building footprints, and flow arcs. All interactive features that depend on the database still work.

### What the server does

When offline is on, `run_dashboard.py`:

1. Rewrites HTML responses to swap CDN URLs for local vendor files.
2. Strips Google Fonts `<link>` tags and injects `dashboard/assets/dashboard-offline.css`.
3. Injects `offline: true` into the runtime deploy config (`__dashDeploy` / `/api/health`).
4. Map pages call `DashMapBasemap.addTo()` which skips Carto tiles when offline.

Bundled vendor files (included in the repo and in pack bundles):

- `dashboard/assets/vendor/leaflet.css`, `leaflet.js`, `leaflet-heat.js`
- `dashboard/assets/vendor/chart.umd.min.js`, `chartjs-plugin-datalabels.min.js`
- `dashboard/assets/vendor/images/` (Leaflet marker/layer icons)
- `dashboard/assets/dashboard-map-basemap.js`, `dashboard-offline.css`

### Verify offline mode

**1. Startup log** should include:

```text
Offline mode: on
```

**2. Health endpoint** (replace `<PORT>` with your `PORT`):

```powershell
Invoke-RestMethod "http://127.0.0.1:<PORT>/api/health" | Select-Object -ExpandProperty deploy
```

```bash
curl -s "http://127.0.0.1:${PORT}/api/health" | python3 -c "import sys,json; print(json.load(sys.stdin)['deploy'])"
```

Expected: `"offline": true`.

**3. Browser** — open a map page, hard refresh (Ctrl+Shift+R), then check DevTools → Network:

- No requests to `unpkg.com`, `cdn.jsdelivr.net`, `fonts.googleapis.com`, or `basemaps.cartocdn.com`
- Scripts load from `/assets/vendor/...` on your server
- Map canvas shows a dark grid; zones and buildings still appear when selected

**4. Optional air-gap test** — disconnect from the network (or block outbound HTTP), reload the dashboard. Pages should load; only the basemap stays as the grid (by design).

### When to stay online

Keep the default (`DASH_OFFLINE` unset or `false`) if you want:

- Street-style Carto basemap tiles
- Google Fonts typography matching the design preview

Online mode is fine for most local development and deployments with normal internet access.

---

## What is in the bundle

| Path | Purpose |
|------|---------|
| `dashboard/` | HTML / JS / CSS (zone maps, buildings, flows) |
| `dashboard/assets/vendor/` | Bundled Leaflet, Chart.js, icons (for offline mode) |
| `scripts/run_dashboard.py` | Flask API server (**use this to run**) |
| `data/db/od_dashboard_tables.dump` | PostgreSQL dump (you provide if not in repo) |
| `data/mtl_boundary_file.geojson` | Island outline for maps (optional but bundled) |
| `data/popgen_inputs/*.csv` | Zone label fallbacks (optional) |
| `deploy.example.env` | Configuration template → copy to `deploy.env` |
| `manifest.json` | Dump metadata and row counts |

**Buildings view** reads all footprint and emission data from PostgreSQL. Click a **zone** first, then a **building**.

---

## Move to another machine

1. Copy the whole project folder (include `data/db/od_dashboard_tables.dump` or download on target).
2. Copy `deploy.example.env` → `deploy.env` and set **that machine’s** `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PORT`.
3. If the target has **no internet**, set `DASH_OFFLINE=true` in `deploy.env` (vendor assets are in the repo).
4. Restore the dump (step 5 above).
5. `pip install -r requirements.txt` and `./scripts/start_dashboard.sh` or `.\scripts\start_dashboard.ps1`.
6. Confirm `/api/health` shows `"ok": true`, `"db_port"` matches your `PGPORT`, and `"deploy"."offline"` is `true` when offline is enabled.

---

## Refresh the database dump (maintainers)

Load **`deploy.env`**, then dump (table list matches `manifest.json`):

**Linux / macOS:**

```bash
set -a && source <(grep -v '^#' deploy.env | grep -v '^[[:space:]]*$' | sed 's/^/export /') && set +a
pg_dump -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -Fc --no-owner --no-acl \
  -f data/db/od_dashboard_tables.dump -n "$PGSCHEMA" \
  -t "$PGSCHEMA.popgen_zones_geom" -t "$PGSCHEMA.zone_emissions_od10" \
  -t "$PGSCHEMA.zone_incoming_flows_od10" -t "$PGSCHEMA.zone_flow_anchors_od10" \
  -t "$PGSCHEMA.zone_emissions_categories_od10" -t "$PGSCHEMA.building_emissions_od10" \
  -t "$PGSCHEMA.buildings_footprint" -t "$PGSCHEMA.trips_route_emissions"
```

**Windows** (after loading `deploy.env`):

```powershell
pg_dump -h $env:PGHOST -p $env:PGPORT -U $env:PGUSER -d $env:PGDATABASE -Fc --no-owner --no-acl `
  -f data/db/od_dashboard_tables.dump -n $env:PGSCHEMA `
  -t "$($env:PGSCHEMA).popgen_zones_geom" -t "$($env:PGSCHEMA).zone_emissions_od10" `
  -t "$($env:PGSCHEMA).zone_incoming_flows_od10" -t "$($env:PGSCHEMA).zone_flow_anchors_od10" `
  -t "$($env:PGSCHEMA).zone_emissions_categories_od10" -t "$($env:PGSCHEMA).building_emissions_od10" `
  -t "$($env:PGSCHEMA).buildings_footprint" -t "$($env:PGSCHEMA).trips_route_emissions"
```

Update `manifest.json` `created_at` after re-dumping.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Health shows wrong `db_port` | Stale server or `deploy.env` not loaded | Kill old process on your `PORT`; restart via start script; re-check health |
| `ok: false` in health | Wrong credentials or DB not running | Fix `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD` in `deploy.env` |
| 503 “No OD10 zone table found” | Dump not restored or wrong schema | Restore dump; set `PGSCHEMA` to match where tables live |
| HTTP 500 on maps | PostGIS missing | `CREATE EXTENSION postgis;` |
| Buildings map empty | No zone selected | Click a zone on the choropleth first |
| Building CO₂ shows 0 | Old dump or wrong table | Health → `building_emissions.rows` ≈ 924757 |
| UI looks outdated | Browser cache | Hard refresh (Ctrl+Shift+R) |
| `Address already in use` | Another process on your `PORT` | Change `PORT` in `deploy.env` or stop the other process |
| Blank map background | Offline mode or no network | Expected with `DASH_OFFLINE=true`; zones/buildings still render. Online mode needs Carto tile access |
| Leaflet/Chart failed to load | CDN blocked, offline not enabled | Set `DASH_OFFLINE=true` in `deploy.env` and restart |
| Opened HTML as `file://` | Not using Flask | Use `http://127.0.0.1:<PORT>/...` |

**Free your HTTP port** (replace `<PORT>` with `PORT` from `deploy.env`):

Linux / macOS:

```bash
fuser -k <PORT>/tcp
# or: lsof -ti :<PORT> | xargs kill
```

Windows:

```powershell
Get-NetTCPConnection -LocalPort $env:PORT -State Listen | ForEach-Object {
  Stop-Process -Id $_.OwningProcess -Force
}
```

---

## Layout

- `dashboard/` — SPA + map views
- `scripts/run_dashboard.py` — API server
- `scripts/start_dashboard.ps1` / `start_dashboard.sh` — load `deploy.env` and run
- `dashboard/assets/vendor/` — offline Leaflet/Chart.js bundles
- `data/db/` — dump location
- `docs/screenshots/` — README figures

See **`README.html`** for architecture diagrams, UI tour, and API list.
