# OD Dashboard — API User Manual

This guide documents every **read-only JSON API** exposed by the PopGen OD dashboard server (`scripts/run_dashboard.py`). Map endpoints may include zone or building shapes as JSON. Use it to test endpoints with **curl**, **Insomnia**, **Postman**, **Swagger UI**, or any HTTP client.

For dashboard setup, see [Dashboard User Manual](DASHBOARD_USER_MANUAL.md) and [README](../README.md).

**HTML version:** [API_USER_MANUAL.html](API_USER_MANUAL.html) (same content, browser-friendly).

---

## 1. Before you call any API

### Start the server

```powershell
.\scripts\start_dashboard.ps1
```

Leave the terminal open. The server reads PostgreSQL connection settings from `deploy.env`.

### Base URL

Set one variable in your HTTP client:

| Variable | Example value | Purpose |
|----------|---------------|---------|
| `base_url` | `http://127.0.0.1:5051` | Server root — use your `deploy.env` host/port (default port **5051**) |

#### Insomnia

1. Press **Ctrl+E** → **Manage Environments**.
2. Select **Base Environment** (left sidebar).
3. Turn **Table View** on. Add **one** row only:

| Name | Type | Value |
|------|------|-------|
| `base_url` | **Text** | `http://127.0.0.1:5051` |

- Type the URL **without** quote characters.
- Do **not** add a second row with Type **JSON** — that breaks variable resolution.
4. Close the dialog. In the top-right dropdown, select **Base Environment**.
5. In the request URL field:

```http
{{ base_url }}/api/health
```

URL preview should show `http://127.0.0.1:5051/api/health`.

#### Postman, Bruno, and other clients

```json
{
  "base_url": "http://127.0.0.1:5051"
}
```

Use `{{ base_url }}/api/...` in every request below.

#### curl / PowerShell

Paste the full URL (e.g. `http://127.0.0.1:5051/api/health`) or set a shell variable.

Add **zone** and **building** IDs as **query parameters** per request (`geo_id`, `dest_geo_id`, `zone_geo_id`, `building_id`) — not in the environment.

### Conventions

| Topic | Detail |
|-------|--------|
| **Methods** | All documented endpoints are `GET`. |
| **Auth** | None (local read-only API). |
| **Content-Type** | Responses are `application/json`. |
| **Errors** | `400` = missing/invalid parameter; `404` = not found; `503` = database table missing. Body: `{"error": "...", "message": "..."}`. |
| **CMM zones** | Zone geometry and flows use **CMM zones only** (`cmm=1`, 936 zones). Full CMM mesh: `zones_boundary?island_only=0`. |

### Shared query parameters

These appear on several endpoints:

| Parameter | Values | Default | Used on |
|-----------|--------|---------|---------|
| `zone_by` | **`rules`** or **`dest`** only | `rules` | Zone map, flows, zone sidebar |
| `island_only` | `1` / `0`, `true` / `false` | `1` (island clip) | `zone_map`, `zones_boundary`, `building_map` |
| `include_geojson` | `1` / `0` | `1` | `zone_map`, `zone_maps` |
| `min_kg` | number ≥ 0 | `0` | `zone_map`, `building_map` (filters by emissions floor) |
| `max_kg` | number | *(none)* | `zone_map`, `building_map` |
| `building_by` | **`rules`** or **`dest`** only | `rules` | `building_map`, `building_detail`, `building_emission_scale` |
| `limit` | integer, or `all` | varies | Flows, building map, building outlines |

### `zone_by` and `building_by` — spell `rules` or `dest` in the URL

Several endpoints accept **`zone_by`** (zones, flows, sidebar) or **`building_by`** (buildings). Pass the value **exactly** as shown in query parameters — lowercase, no spaces:

| API value (use in URL) | Dashboard button | Meaning |
|------------------------|------------------|---------|
| **`rules`** | Rules-based | Trips and emissions counted using routing / meeting rules (**default** if you omit the parameter). |
| **`dest`** | Destination zone | Trips and emissions rolled up to where the trip ends. |

**Do not use** dashboard labels in the API call — these are **wrong**:

- `Rules-based`, `rules-based`, `destination`, `Destination zone` → use **`rules`** or **`dest`** only.

**Correct examples:**

```http
?zone_by=rules
?zone_by=dest
?building_by=rules
?building_by=dest
```

If you omit `zone_by` or `building_by`, the server uses **`rules`**.

## 2. Health & reference

### `GET /api/health`

**Purpose:** Verify the server and database are reachable. Returns deploy config (`api_base`, `url_prefix`) and zone-label diagnostics.

**Parameters:** none

#### Example 1 — smoke test

```http
GET {{ base_url }}/api/health
```

```powershell
Invoke-RestMethod "{{ base_url }}/api/health"
```

**Response (abbreviated):** `"ok": true`, `"dbname": "od_dashboard"`, `"deploy": { "api_base": "/api", ... }`.

#### Example 2 — check zone labels loaded

Same URL; inspect `zone_labels.sample_label_562` and `zone_labels.zone_codes_loaded` in the JSON body.

---

### `GET /api/montreal_boundary.geojson`

**Purpose:** Montreal island outline (border used to clip or frame the map).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `buffer_m` | float | *(none)* | Optional outward buffer in metres |

#### Example 1 — island outline (dashboard default)

```http
GET {{ base_url }}/api/montreal_boundary.geojson
```

**Query parameters:** *(leave empty)*

#### Example 2 — buffered outline

```http
GET {{ base_url }}/api/montreal_boundary.geojson?buffer_m=500
```

**Query parameters:**

| name | value |
|------|-------|
| `buffer_m` | `500` |

**Response:** JSON with island polygon shape(s).

---

### `GET /api/od/zone_codes`

**Purpose:** Lookup tables mapping `geo_id` → zone code and zone name (for labels in clients).

**Parameters:** none

#### Example 1

```http
GET {{ base_url }}/api/od/zone_codes
```

#### Example 2 — with base URL variable

```http
GET {{ base_url }}/api/od/zone_codes
```

**Response:** `{ "zone_codes": { "562": "Z562", ... }, "zone_names": { "562": "...", ... } }`.

---

## 3. Zone maps & boundaries

### `GET /api/od/zones_boundary`

**Purpose:** Zone polygon outlines for map backgrounds. **CMM mesh** when `island_only=0`; island subset when `island_only=1`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `island_only` | `0` / `1` | `0` | `0` = full CMM (`cmm=1`) mesh; `1` = Montreal island clip |

#### Example 1 — full CMM zone mesh (flows map black boundaries)

```http
GET {{ base_url }}/api/od/zones_boundary?island_only=0
```

**Query parameters:**

| name | value |
|------|-------|
| `island_only` | `0` |

#### Example 2 — island-only outlines

```http
GET {{ base_url }}/api/od/zones_boundary?island_only=1
```

**Response:** `geojson` (zone shapes), `zone_count` (936), `bounds`, `island_only`.

---

### `GET /api/od/zone_map`

**Purpose:** Zone map data — per-zone trips, emissions, and distance (used to color each zone on the map); optional zone polygon shapes.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `zone_by` | `rules` \| `dest` | `rules` | Rules-based or Destination zone |
| `include_geojson` | `0` \| `1` | `1` | Include zone polygon shapes |
| `island_only` | `0` \| `1` | `1` | Clip to island zones |
| `min_kg` | float | `0` | Minimum zone emissions (kg CO₂e) |
| `max_kg` | float | *(none)* | Maximum zone emissions (kg) |
| `with_building_scale` | `0` \| `1` | `0` | Add `building_emission_scale` for legend |

#### Example 1 — `zone_by=dest` (no zone shapes)

Used by flows page for fast loading.

```http
GET {{ base_url }}/api/od/zone_map?zone_by=dest&island_only=1&include_geojson=0&min_kg=0
```

**Query parameters:**

| name | value |
|------|-------|
| `zone_by` | `dest` |
| `island_only` | `1` |
| `include_geojson` | `0` |
| `min_kg` | `0` |

#### Example 2 — `zone_by=rules` with zone shapes

```http
GET {{ base_url }}/api/od/zone_map?zone_by=rules&island_only=1&include_geojson=1&min_kg=0&with_building_scale=1
```

**Response:** `{ "zones": [...], "geojson": {...}, "zone_by": "rules", ... }` — `zones` has the numbers; `geojson` has the shapes.

---

### `GET /api/od/zone_maps`

**Purpose:** Both **rules-based** and **destination** zone maps in one response (home page preload).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `include_geojson` | `0` \| `1` | `1` | Zone shapes for both maps |
| `island_only` | `0` \| `1` | `1` | Island clip |
| `min_kg` | float | `0` | Emissions floor (kg) |
| `max_kg` | float | *(none)* | Emissions ceiling (kg) |

#### Example 1 — both maps, no geometry (fast)

```http
GET {{ base_url }}/api/od/zone_maps?include_geojson=0&island_only=1
```

#### Example 2 — both maps with polygons

```http
GET {{ base_url }}/api/od/zone_maps?include_geojson=1&island_only=1&min_kg=0
```

**Response:** `{ "dest": { "zones": [...], "geojson": ... }, "rules": { ... }, "metrics_mode": "weighted" }`.

---

### `GET /api/od/flows_zones`

**Purpose:** Same zone metrics as `zone_map` but **never** returns polygons (thin wrapper for the flows map).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `zone_by` | `rules` \| `dest` | `rules` | Rules-based or destination zone map |

#### Example 1 — destination zones for flows UI

```http
GET {{ base_url }}/api/od/flows_zones?zone_by=dest
```

#### Example 2 — rules-based flows zones

```http
GET {{ base_url }}/api/od/flows_zones?zone_by=rules
```

**Response:** Same shape as `zone_map` with `include_geojson=0`.

---

## 4. Zone sidebar (KPIs / charts)

### `GET /api/od/zone_sidebar`

**Purpose:** KPIs and travel-reason breakdown for **one selected zone** (sidebar refresh on zone click).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `geo_id` | string | **yes** | Zone `geo_id` (e.g. `562`) |
| `zone_by` | `rules` \| `dest` | no | Rules-based or Destination zone (default `rules`) |
| `dest_geo_id` | string | no | Alternative name for `geo_id` |
| `attribution` | string | no | Alternative name for `zone_by` |

#### Example 1 — `zone_by=rules`

```http
GET {{ base_url }}/api/od/zone_sidebar?geo_id=562&zone_by=rules
```

**Query parameters:**

| name | value |
|------|-------|
| `geo_id` | `562` |
| `zone_by` | `rules` |

#### Example 2 — `zone_by=dest`

```http
GET {{ base_url }}/api/od/zone_sidebar?geo_id=562&zone_by=dest
```

**Response:** `{ "geo_id": "562", "zone_label": "...", "stats": { "trips": ..., "total_emissions_g": ... }, "by_category": [...] }`.

**Error:** `400` if `geo_id` is missing: `{"error": "geo_id is required"}`.

---

## 5. Incoming flows

### `GET /api/od/zone_incoming_flow`

**Purpose:** Top incoming origin zones for one **destination** zone, plus destination KPIs and intra-zone summary. Powers the **flows** map arcs and table.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `dest_geo_id` | string | **yes** | — | Destination zone |
| `zone_by` | `rules` \| `dest` | no | `rules` | Rules-based or Destination zone |
| `limit` | int or `all` | no | `10` | Max origin zones (`all` = no cap, max 500) |

#### Example 1 — top 10 incoming flows (default)

```http
GET {{ base_url }}/api/od/zone_incoming_flow?dest_geo_id=562
```

**Query parameters:**

| name | value |
|------|-------|
| `dest_geo_id` | `562` |

#### Example 2 — `zone_by=dest`, top 25

```http
GET {{ base_url }}/api/od/zone_incoming_flow?dest_geo_id=562&zone_by=dest&limit=25
```

**Response (key fields):** `flows[]` (orig_geo_id, trips, emissions, lat/lon), `dest_zone_trips`, `intra_zone`, `total_incoming_trips`, `dest_lat`, `dest_lon`.

**Error:** `400` if `dest_geo_id` missing.

---

### `GET /api/od/zone_incoming_flows_all`

**Purpose:** Bulk prefetch — top flows for **every** destination zone (flows page cache). Heavy query; use a `limit` in testing.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `zone_by` | `rules` \| `dest` | `rules` | Rules-based or Destination zone |
| `limit` | int or `all` | `all` | Top-N origins per destination |

#### Example 1 — top 5 origins per zone (testing)

```http
GET {{ base_url }}/api/od/zone_incoming_flows_all?zone_by=dest&limit=5
```

**Query parameters:**

| name | value |
|------|-------|
| `zone_by` | `dest` |
| `limit` | `5` |

#### Example 2 — `zone_by=rules`, all origins

```http
GET {{ base_url }}/api/od/zone_incoming_flows_all?zone_by=rules
```

**Response:** `{ "zones": { "<dest_geo_id>": { "flows": [...], "dest_geo_id": "...", ... }, ... }, "zone_by": "dest" }`.

---

## 6. Buildings

Building endpoints use **`building_by`**. Values must be spelled **`rules`** or **`dest`** (same as `zone_by`; see above).

### `GET /api/od/building_emission_scale`

**Purpose:** Min/max building emissions (grams) for the buildings map colour legend.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `building_by` | `rules` \| `dest` | `rules` | Rules-based or Destination zone |

#### Example 1 — `building_by=rules`

```http
GET {{ base_url }}/api/od/building_emission_scale?building_by=rules
```

#### Example 2 — `building_by=dest`

```http
GET {{ base_url }}/api/od/building_emission_scale?building_by=dest
```

**Response:** `{ "building_by": "rules", "min_g": 0, "max_g": 12345.6, "building_count": 927305 }`.

---

### `GET /api/od/building_map`

**Purpose:** Buildings with emissions inside a zone (points or grid clusters; optional footprints).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `zone_geo_id` | string | — | **Required** for data (empty → empty list + hint) |
| `building_by` | `rules` \| `dest` | `rules` | Rules-based or Destination zone |
| `min_kg` | float | `0` | Emissions floor (kg) |
| `max_kg` | float | *(none)* | Emissions ceiling (kg) |
| `limit` | int | `50000` | Max rows (100–500000) |
| `island_only` | `0` \| `1` | `1` | Island filter |
| `include_footprints` | `0` \| `1` | auto | Polygon geometry per building |
| `grid_cell_deg` | float | *(none)* | Cluster to grid (0.0005–0.05 degrees) |

#### Example 1 — `building_by=rules`, zone 562

```http
GET {{ base_url }}/api/od/building_map?zone_geo_id=562&building_by=rules&limit=500
```

**Query parameters:**

| name | value |
|------|-------|
| `zone_geo_id` | `562` |
| `building_by` | `rules` |
| `limit` | `500` |

#### Example 2 — `building_by=dest`, zone 562

```http
GET {{ base_url }}/api/od/building_map?zone_geo_id=562&building_by=dest&min_kg=1&limit=100
```

**Response:** `{ "buildings": [...], "zone_geo_id": "562", "truncated": false, "metrics_mode": "weighted" }`.

---

### `GET /api/od/zone_building_fabric`

**Purpose:** All building outline shapes in a zone (no emissions filter). Used as grey backgrounds under the colored buildings layer.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `zone_geo_id` | string | **yes** | — | Zone to load |
| `limit` | int | no | `50000` | Max footprints |

#### Example 1

```http
GET {{ base_url }}/api/od/zone_building_fabric?zone_geo_id=562
```

#### Example 2 — smaller sample for testing

```http
GET {{ base_url }}/api/od/zone_building_fabric?zone_geo_id=562&limit=200
```

**Response:** `footprint_fc` (building shapes), `fabric_truncated` (true if results were cut off).

**Error:** `400` if `zone_geo_id` missing.

---

### `GET /api/od/building_footprint`

**Purpose:** Single building outline shape by ID.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `building_id` | string | **yes** | Building primary key |

#### Example 1

```http
GET {{ base_url }}/api/od/building_footprint?building_id=123456
```

*(Replace `123456` with an ID from `building_map`.)*

#### Example 2 — set `building_id` as a query parameter

```http
GET {{ base_url }}/api/od/building_footprint?building_id=123456
```

**Query parameters:**

| name | value |
|------|-------|
| `building_id` | `123456` |

**Response:** `building_id`, `geojson` (building shape).

---

### `GET /api/od/building_detail`

**Purpose:** KPI block for one building (trips, emissions, distance, zone).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `building_id` | string | **yes** | — | Building ID |
| `building_by` | `rules` \| `dest` | no | `rules` | Rules-based or Destination zone |

#### Example 1 — `building_by=rules`

```http
GET {{ base_url }}/api/od/building_detail?building_id=123456&building_by=rules
```

#### Example 2 — `building_by=dest`

```http
GET {{ base_url }}/api/od/building_detail?building_id=123456&building_by=dest
```

**Response:** `{ "building": { "building_id", "zone_geo_id", "trips", "total_emissions_g", ... }, "geojson": {...} }`.

---

## 7. Suggested test order

Run these in order when validating a fresh install:

1. `GET {{ base_url }}/api/health`
2. `GET {{ base_url }}/api/od/zone_map?zone_by=dest&include_geojson=0`
3. `GET {{ base_url }}/api/od/zone_sidebar?geo_id=562&zone_by=rules`
4. `GET {{ base_url }}/api/od/zone_incoming_flow?dest_geo_id=562&limit=10`
5. `GET {{ base_url }}/api/od/building_map?zone_geo_id=562&limit=50`
6. `GET {{ base_url }}/api/od/zones_boundary?island_only=0`

---

## 8. Troubleshooting

| Symptom | Check |
|---------|--------|
| `Failed to render environment variables: base_url` or `bad/illegal format` | **Insomnia:** one **Text** row only (`base_url` = `http://127.0.0.1:5051`); delete extra JSON rows; select **Base Environment** |
| Connection refused | Server not running — run `start_dashboard.ps1` |
| `"ok": false` in health | PostgreSQL / `deploy.env` — see dashboard manual |
| `503 missing_table` | Restore DB dump into `od_dashboard` (see Dashboard User Manual; dump filename may vary) |
| Empty `zones` or `flows` | Try `zone_by=dest` if you used `rules`, or the other way around |
| Huge slow response | Set `include_geojson=0` on map endpoints; add `limit` on flows/buildings |
| `400 geo_id required` | Add `geo_id` or `dest_geo_id` query parameter |

---

## 9. Quick reference

| Endpoint | Example with parameters |
|----------|---------------------------|
| Health | `{{ base_url }}/api/health` |
| Island boundary | `{{ base_url }}/api/montreal_boundary.geojson?buffer_m=500` |
| Zone codes | `{{ base_url }}/api/od/zone_codes` |
| CMM boundaries | `{{ base_url }}/api/od/zones_boundary?island_only=0` |
| Zone map | `{{ base_url }}/api/od/zone_map?zone_by=dest&include_geojson=0` |
| Both zone maps | `{{ base_url }}/api/od/zone_maps?include_geojson=0` |
| Zone sidebar | `{{ base_url }}/api/od/zone_sidebar?geo_id=562&zone_by=rules` |
| Incoming flows | `{{ base_url }}/api/od/zone_incoming_flow?dest_geo_id=562&limit=10` |
| All flows (bulk) | `{{ base_url }}/api/od/zone_incoming_flows_all?limit=5` |
| Building scale | `{{ base_url }}/api/od/building_emission_scale?building_by=rules` |
| Buildings in zone | `{{ base_url }}/api/od/building_map?zone_geo_id=562&limit=500` |
| Zone building outlines | `{{ base_url }}/api/od/zone_building_fabric?zone_geo_id=562` |
| One footprint | `{{ base_url }}/api/od/building_footprint?building_id=...` |
| Building KPIs | `{{ base_url }}/api/od/building_detail?building_id=...&building_by=rules` |
