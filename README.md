# OD dashboard export

Portable **PM23 survey** dashboard — CMM island-eligible car trips, zone maps, buildings, and flows.

**Self-contained at runtime:** this folder + PostgreSQL database **`od_dashboard`** (schema **`public`**). No PopGen2023 repo, routing, or `Synthetic2023` database required on the target machine.

| Resource | Link |
|----------|------|
| **Full install guide (HTML)** | Open [`README.html`](README.html) in your browser |
| **GitHub** | [github.com/atiyasehar/od_dashboard_export-main](https://github.com/atiyasehar/od_dashboard_export-main) |
| **DB dump** | [OneDrive — od_dashboard_tables.dump](https://liveconcordia-my.sharepoint.com/:u:/g/personal/atiya_atiya_concordia_ca/IQDAgc05pD40SK9YSnDwFZURAcydIl6xbQHGRnafPX5VfIE?e=j6PxkO) — copy to `data/db/` |

See **`manifest.json`** for current table row counts (e.g. `building_emissions_od10` — capacity-allocated expanded trips per footprint).

## Quick start

To set up the application, follow these steps:

### 1. Database restore
You can either do it manually (section 1.1) or use an automated Python script (section 1.2). Target database: **`od_dashboard`** · Schema: **`public`** · 8 precomputed tables.

#### 1.1. pgAdmin (Windows)

##### 1.1.1.  Create database

pgAdmin → **Databases** → right-click → **Create** → **Database…** → name: **`od_dashboard`**

##### 1.1.2. Prepare schema and PostGIS

Query Tool on **`od_dashboard`**, run:

```sql
DROP SCHEMA IF EXISTS public CASCADE;

CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO postgres;
GRANT ALL ON SCHEMA public TO public;

CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public;
```

##### 1.1.3. Restore dump

Right-click **`od_dashboard`** → **Restore…**

- **General:** Format = `Custom or tar`, Filename = `data\db\od_dashboard_tables.dump`
- **Data Options** tab:

| Section | Option | Setting |
|---------|--------|---------|
| Sections | Pre-data, Post-data, Data | **On** |
| Type of objects | Only data, Only schema | Off |
| Do not save | **Owner**, **Privileges** | **On** |
| Do not save | Tablespaces, Comments, Publications, Subscriptions, Security labels, Table access methods | Off |

Click **Restore**.

##### 1.1.4. Verify

```sql
SELECT table_schema, table_name FROM information_schema.tables
WHERE table_schema = 'public' AND table_name LIKE '%od10%' OR table_name IN
('popgen_zones_geom','buildings_footprint','trips_route_emissions')
ORDER BY table_name;
```

See also **`PopGen2023/Data/db/README.md`** for the same steps (dump zip in repo).

#### 1.2. Python script
Run the code as shown below.

##### 1.2.1. Windows
```powershell
# 1. Download od_dashboard_tables.dump from OneDrive (link in README.html)
#    — or use Data/db/od_dashboard_tables.zip from the PopGen2023 repo
# 2. Copy into project
mkdir data\db
copy %USERPROFILE%\Downloads\od_dashboard_tables.dump data\db\

# 2. Python deps
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 3. Restore DB (create od_dashboard in pgAdmin first)
python scripts/bundle_od_dashboard.py unpack --bundle-dir . --dbname od_dashboard
```

##### 1.2.2. Linux
Note: you can either pass the environment variables before the Python script (as show below) or set them as environment variables in your OS. Also, before you run, make sure you are pointing to the right database and using the right credentials.
```sh
# In this example, we run the app in a custom port (1234), not in the default port
# 1. Download od_dashboard_tables.dump from OneDrive (link in README.html)
# 2. Copy into project
mkdir data/db
cp ~/Downloads/od_dashboard_tables.dump data/db/

# 2. Python deps
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 3. Restore DB (create od_dashboard in pgAdmin first)
PGHOST=localhost PGPORT=5432 PGUSER=yourusername PGPASSWORD=yourpassword PORT=1234 python scripts/bundle_od_dashboard.py unpack --bundle-dir . --dbname od_dashboard
```

### 2. Run

**Local (default — root URL, `/api` prefix):**

```powershell
python scripts/run_dashboard.py --bundle-root . --db-name od_dashboard
```

If PostgreSQL is not on default port/host (e.g. `localhost:5433`):

```powershell
python scripts/run_dashboard.py --bundle-root . --db-name od_dashboard `
  --db-host localhost --db-port 5433 --db-user postgres --db-password YOURPASS
```

Open **http://127.0.0.1:5051/** · Health: **http://127.0.0.1:5051/api/health**

**Shared host / reverse proxy (subpath + custom API prefix):**

When the dashboard is mounted under a subpath (e.g. NGCI) and `/api` is already used by another service:

```powershell
python scripts/run_dashboard.py --bundle-root . --db-name od_dashboard `
  --url-prefix /montreal-traffic-emissions-dashboard `
  --api-prefix /od-dashboard-api `
  --show-boundary-button false
```

| Flag | Purpose |
|------|---------|
| `--url-prefix` | Mount path for HTML and assets (e.g. `/montreal-traffic-emissions-dashboard`) |
| `--api-prefix` | API route prefix (default `/api`; use `/od-dashboard-api` on shared hosts) |
| `--show-boundary-button` | `true` (default) or `false` to hide the Boundaries nav link |

Environment variable equivalents: `DASH_URL_PREFIX`, `DASH_API_PREFIX`, `DASH_SHOW_BOUNDARY_BUTTON`.

Example health URL with the flags above:

`https://ngci.encs.concordia.ca/montreal-traffic-emissions-dashboard/od-dashboard-api/health`

The server injects these settings into `assets/dashboard-config.js` at runtime so links and API calls stay under the correct prefix.

## Move to another machine

1. Copy the whole **`od_dashboard_export-main/`** folder (include `data/db/od_dashboard_tables.dump` if using unpack).
2. On the target: install PostgreSQL + PostGIS, restore the dump into **`od_dashboard`**, `pip install -r requirements.txt`, run the server (see above).
3. Internet access is needed for map basemap tiles and CDN JavaScript libraries.

## Refresh the database dump (after PopGen updates)

When you rebuild precomputed tables on a dev machine (e.g. promote new `building_emissions_od10` from PopGen), refresh the portable dump:

```powershell
# Option A — re-dump od_dashboard in place
pg_dump -h localhost -p 5433 -U postgres -d od_dashboard -Fc --no-owner --no-acl `
  -f data/db/od_dashboard_tables.dump

# Option B — pack from PopGen2023 (updates manifest.json row counts)
cd path\to\PopGen2023
python scripts/bundle_od_dashboard.py pack --host localhost --port 5433 `
  --user postgres --password YOURPASS --dbname od_dashboard --schema public `
  --out-dir path\to\od_dashboard_export-main
```

Or copy a single table from PopGen into `od_dashboard`:

```powershell
pg_dump -h localhost -p 5433 -U postgres -d Synthetic2023 -t popgen.building_emissions_od10 --no-owner --no-acl -Fc -f building_emissions_od10.dump
pg_restore -h localhost -p 5433 -U postgres -d od_dashboard --no-owner --no-acl --clean --if-exists building_emissions_od10.dump
# then in od_dashboard: ALTER TABLE popgen.building_emissions_od10 SET SCHEMA public;  (if needed)
```

**Building trips in the UI** use **expanded PM23 trips** (`trips_weighted_*` in `building_emissions_od10`), allocated by zone purpose → building function/capacity — not raw leg counts.

## Layout

- `dashboard/` — SPA + map views (HTML / JS / CSS)
- `scripts/run_dashboard.py` — Flask API server
- `data/db/` — PostgreSQL dump location
- `data/` — island boundary GeoJSON
- `docs/screenshots/` — README figures

See **README.html** for architecture diagrams, UI tour, API list, shared-host deployment, and troubleshooting flowchart.
