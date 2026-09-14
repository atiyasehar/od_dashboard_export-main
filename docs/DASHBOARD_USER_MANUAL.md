# OD Dashboard — User Manual

This guide covers **running and using** the PopGen Actual OD survey (PM23) dashboard. For full installation, database restore, and developer settings, see the main [README](../README.md).

---

## 1. What the dashboard shows

The dashboard explores **car trip emissions** on the Montreal metropolitan area using precomputed survey data (OD10 / PM23):

| View | Purpose |
|------|---------|
| **Home** | Summary KPIs and charts (trips, CO₂, distance) |
| **Zone maps** | Choropleth maps of emissions by traffic zone |
| **Buildings** | Building footprints and emissions inside the selected zone(s) |
| **Flows** | Incoming trip flows into the selected destination zone(s) |
| **Boundaries** | Zone outline reference map (optional; can be hidden in config) |

You can select **more than one zone**. Click replaces the selection; **Ctrl/Cmd** or **Shift** click adds or removes. Sidebar KPIs, charts, buildings, and incoming flows **add up** across the selection.

Maps use OpenStreetMap (no API key). A **Transit overlay** control (bottom-left) shows STM metro, REM, exo trains, and STM buses. Data is read from a local PostgreSQL database (`od_dashboard`). The dashboard is **read-only** — you explore precomputed results; you do not edit the database from the UI.

---

## 2. Before you start

You need:

1. This project folder on your computer
2. **PostgreSQL** with the `od_dashboard` database restored (see [README](../README.md))
3. **Python 3.10+** with dependencies installed (`pip install -r requirements.txt`)
4. A configured **`deploy.env`** file in the project root (copy from `deploy.example.env`)

Minimum settings in `deploy.env`:

| Variable | Example | Meaning |
|----------|---------|---------|
| `PORT` | `5051` | Port for the web dashboard (your choice) |
| `PGHOST` | `localhost` | PostgreSQL host |
| `PGPORT` | `5433` | PostgreSQL port (must match your instance — `deploy.example.env` uses 5433) |
| `PGUSER` | `postgres` | Database user |
| `PGPASSWORD` | *(your password)* | Database password |
| `PGDATABASE` | `od_dashboard` | Database name |
| `PGSCHEMA` | `public` | Schema with dashboard tables |

Optional:

| Variable | When to use |
|----------|-------------|
| `DASH_SHOW_BOUNDARY_BUTTON=false` | Hide the **Boundaries** tab in the nav bar |
| `DASH_OFFLINE=true` | No internet (bundled maps/charts, no street basemap tiles) |

`deploy.env` is not committed to git. Keep your password private.

---

## 3. Starting the dashboard

Open a terminal in the **project root** (the folder that contains `deploy.env` and `dashboard/`).

### Windows (PowerShell)

```powershell
.\scripts\start_dashboard.ps1
```

If PowerShell refuses to run it (“not digitally signed”), either:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_dashboard.ps1
```

or start Python directly: `python scripts/run_dashboard.py --bundle-root .`

### Linux / macOS

```bash
./scripts/start_dashboard.sh
```

The script loads `deploy.env` and starts the server. You should see lines similar to:

```text
Loaded ...\deploy.env
  Boundaries nav: off
  Offline mode: off
  Listening on http://0.0.0.0:5051
```

Leave this terminal window **open** while you use the dashboard. Closing it stops the server.

### Alternative start command

```bash
python scripts/run_dashboard.py --bundle-root .
```

---

## 4. Opening the dashboard in your browser

Use the `PORT` from your `deploy.env`. Default is **5051** if `PORT` is empty.

| Page | URL |
|------|-----|
| **Home (recommended entry)** | `http://127.0.0.1:<PORT>/` |
| Zone maps only | `http://127.0.0.1:<PORT>/od.html` |
| Buildings | `http://127.0.0.1:<PORT>/od-buildings.html` |
| Flows | `http://127.0.0.1:<PORT>/od-flows.html` |

Replace `<PORT>` with your number, e.g. `http://127.0.0.1:5051/`.

**Important:** Always open pages through `http://127.0.0.1:...`. Do **not** double-click HTML files (`file://`); maps and API calls will not work.

---

## 5. Checking that the server is healthy

### Windows

```powershell
Invoke-RestMethod "http://127.0.0.1:<PORT>/api/health"
```

### Linux / macOS

```bash
curl -s "http://127.0.0.1:<PORT>/api/health" | python3 -m json.tool
```

Look for:

- `"ok": true`
- `"db_port"` matches your `PGPORT` in `deploy.env`
- `"building_emissions"` shows roughly **924,000** rows

If `"ok": false`, check PostgreSQL is running and `deploy.env` credentials are correct.

---

## 6. Stopping the dashboard

In the terminal where the server is running, press **Ctrl+C**.

If the port stays in use (old server still running):

**Windows:**

```powershell
Get-NetTCPConnection -LocalPort <PORT> -State Listen | ForEach-Object {
  Stop-Process -Id $_.OwningProcess -Force
}
```

**Linux / macOS:**

```bash
fuser -k <PORT>/tcp
```

---

## 7. Navigation

The top navigation bar appears on every page:

| Tab | Description |
|-----|-------------|
| **Zone maps** | Regional choropleth views |
| **Buildings** | Building footprints and detail for the selected zone(s) |
| **Flows** | Origin-to-destination flow arcs for the selected destination(s) |
| **Boundaries** | Zone polygon outlines (hidden when `DASH_SHOW_BOUNDARY_BUTTON=false`) |

The **home page** (`/`) embeds Zone maps, Buildings, and Flows in one shell with a sidebar of KPIs and charts.

---

## 8. Using each view

### 8.1 Home page

- **Left sidebar:** Total trips, CO₂ (kg), distance (km), and average emissions per trip — shown for both **rules-based** and **destination zone** attribution.
- **Charts:** Emissions by category, trip purpose, distance bands, and related breakdowns.
- **Main area:** Embedded map views; use the nav tabs to switch between Zone maps, Buildings, and Flows without leaving the home page.

### 8.2 Zone maps

Two attribution modes (toolbar buttons):

| Mode | Meaning |
|------|---------|
| **Rules-based** | Emissions attributed by routing / meeting rules (default) |
| **Destination zone** | Emissions rolled up to the trip destination zone |

**How to use:**

1. Wait for the map to load (street basemap unless offline mode is on). No map API key is required.
2. Switch between **Rules-based** and **Destination zone** to compare attributions.
3. **Click a zone** to select it. **Ctrl/Cmd+click** or **Shift+click** adds or removes zones. Chips above the map show the selection; the × on a chip drops that zone.
4. KPIs and charts in the sidebar **sum** the selected zones. The label becomes `N zones` when more than one is selected.
5. Use the emission filter (minimum kg CO₂) if available to thin low-emission zones.
6. From a selection you can jump to **Flows** or **Buildings** (the same zone ids are kept).
7. **Transit overlay** (bottom-left on this map, and on Buildings / Flows / Boundaries) turns on STM metro, REM, exo trains, and STM buses. The server downloads GTFS when it starts (first run, while online) and caches it under `data/cache/`. Station pins have names on hover. Bus stops only appear after you zoom in — there are thousands of them.

Map tabs may include multiple choropleth variants (e.g. emissions intensity, trip counts). Use the map tab row above the map canvas.

### 8.3 Buildings

Buildings load for the **selected zone(s)** — pick at least one zone first.

**How to use:**

1. Open **Buildings** from the nav (or select zones on the zone map and navigate here).
2. Click a **zone** on the choropleth if none is selected. Ctrl/Cmd or Shift click to add more. The map loads footprints for the whole selection.
3. **Click a building** polygon to open detail: trips, CO₂ (shown to **2 decimal places** in kg), distance, and charts for that building.
4. Toggle **Rules-based** / **Destination zone** attribution to match the zone map logic.
5. Use filters (minimum emissions, etc.) to reduce clutter on dense zones.

If the map is empty, confirm a zone is selected and the health check shows building data loaded.

### 8.4 Flows

Shows **incoming** car trips: where travelers came **from** when **entering** the selected destination zone(s). Origins that sit inside the selection are treated as intra-region and are not drawn as incoming arcs.

**How to use:**

1. Open **Flows**.
2. **Click any zone** on the map. Arcs appear from origin zones toward the destination. Ctrl/Cmd or Shift click to add destinations; the table and arcs merge.
3. The sidebar lists top origin zones (trips, CO₂, share of incoming).
4. Use the **rank range** control (e.g. 1–10, 11–20) to page through origin zones when many flows exist.
5. Switch **Rules-based** / **Destination zone** to change how trip weights are counted.

Hover arcs and zones for tooltips. A plain click replaces the destination set.

### 8.5 Boundaries (optional)

A reference map of zone polygon outlines on a dark basemap. Useful for checking zone IDs and shapes. Transit overlay works here too. This tab is omitted when `DASH_SHOW_BOUNDARY_BUTTON=false` in `deploy.env`.

---

## 9. Rules-based vs destination attribution

Throughout the dashboard you will see two parallel metrics:

| Attribution | Short label in UI | Use when |
|-------------|-------------------|----------|
| Rules-based | Rules-based | Emissions follow route / meeting attribution rules |
| Destination zone | Destination zone | Emissions assigned to the zone where the trip ends |

KPIs, zone colors, building totals, and flow widths all respect the active attribution mode. Compare both modes to see how attribution choice affects results.

---

## 10. Offline mode

If `DASH_OFFLINE=true` in `deploy.env`:

- Maps use a **plain grid background** instead of street tiles.
- JavaScript and fonts load from the local server (no CDN).
- Transit overlay still works if `data/cache/` already has a GTFS extract; the first download needs the network.
- All data features still work; only the basemap and typography differ.

Restart the server after changing `deploy.env`.

---

## 11. Troubleshooting

| Problem | What to try |
|---------|-------------|
| Browser shows “connection refused” | Start the server (`start_dashboard.ps1` / `.sh`). Check `PORT`. |
| Maps blank, sidebar shows errors | Open `/api/health`. Fix database connection in `deploy.env`. |
| Wrong database / empty maps | Confirm dump was restored; `db_port` in health must match `PGPORT`. |
| Buildings map empty | **Select a zone** on the map first (Ctrl/Cmd+click to add more). |
| UI looks old or broken | Hard refresh: **Ctrl+Shift+R** (Windows/Linux) or **Cmd+Shift+R** (Mac). |
| `Address already in use` | Another program (or old dashboard) uses your `PORT`. Stop it or change `PORT`. |
| PowerShell “not digitally signed” | `powershell -ExecutionPolicy Bypass -File .\scripts\start_dashboard.ps1` or `python scripts/run_dashboard.py --bundle-root .` |
| Transit overlay missing | First load downloads STM/REM/exo GTFS (no API key). Stay online once, or check `data/cache/`. |
| Opened HTML file directly | Use `http://127.0.0.1:<PORT>/` instead of `file://`. |
| Leaflet / Chart failed to load | Enable `DASH_OFFLINE=true` if CDNs are blocked. |
| Street map missing (offline) | Expected in offline mode; zones and buildings still draw on the grid. |

---

## 12. Quick reference

| Task | Command / URL |
|------|----------------|
| Start | `.\scripts\start_dashboard.ps1` or `./scripts/start_dashboard.sh` |
| Open app | `http://127.0.0.1:<PORT>/` |
| Health check | `http://127.0.0.1:<PORT>/api/health` |
| Stop | Ctrl+C in the server terminal |
| Config file | `deploy.env` (project root) |

---

## 13. Related documents

| Document | Contents |
|----------|----------|
| [README](../README.md) | Full install, database restore, configuration, tests |
| [API User Manual](API_USER_MANUAL.md) | REST endpoints and client examples |
| [README.html](../README.html) | Architecture diagrams and API list |
