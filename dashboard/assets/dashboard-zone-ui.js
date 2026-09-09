/** Zone map tooltips + parse helpers (versioned — bump ?v= when changing). */

(function (global) {

  var VERSION = '20260908-22';

  var _zoneCodeMap = null;
  var _zoneNameMap = null;
  var ZONE_SHORT_PREFIX = 'mtl';

  function setZoneCodeMap(map) {
    _zoneCodeMap = map && typeof map === 'object' ? map : null;
  }

  function setZoneNameMap(map) {
    _zoneNameMap = map && typeof map === 'object' ? map : null;
  }

  /** Compact id for UI/search, e.g. mtl+123. */
  function shortZoneId(geoId) {
    var g = String(geoId != null ? geoId : '').trim();
    return g ? ZONE_SHORT_PREFIX + '+' + g : '';
  }

  function zoneName(geoId, meta) {
    meta = meta || {};
    if (meta.zone_name) return String(meta.zone_name);
    if (meta.dest_zone_name && String(meta.dest_geo_id) === String(geoId)) {
      return String(meta.dest_zone_name);
    }
    if (meta.orig_zone_name) return String(meta.orig_zone_name);
    var g = String(geoId != null ? geoId : '');
    if (_zoneNameMap && _zoneNameMap[g]) return String(_zoneNameMap[g]);
    return '';
  }

  /** Borough / locality short name from full nomsp, e.g. "Ville-Marie". */
  function zoneShortName(geoId, meta) {
    meta = meta || {};
    if (meta.zone_short_name) return String(meta.zone_short_name);
    if (meta.dest_zone_short_name && String(meta.dest_geo_id) === String(geoId)) {
      return String(meta.dest_zone_short_name);
    }
    if (meta.orig_zone_short_name) return String(meta.orig_zone_short_name);
    var full = zoneName(geoId, meta);
    if (!full) return '';
    var t = String(full).trim();
    var colon = t.indexOf(':');
    if (colon >= 0) t = t.slice(colon + 1).trim();
    t = t.replace(/\s*\([^)]*\)\s*$/, '').trim();
    if (t.indexOf(';') >= 0) t = t.split(';')[0].trim();
    return t;
  }

  /** Primary zone label: "Ville-Marie - 123" (short name + geo_id). */
  function zoneLabel(geoId, meta) {
    meta = meta || {};
    if (meta.zone_label) return String(meta.zone_label);
    if (meta.dest_zone_label && String(meta.dest_geo_id) === String(geoId)) {
      return String(meta.dest_zone_label);
    }
    if (meta.orig_zone_label) return String(meta.orig_zone_label);
    var g = String(geoId != null ? geoId : '').trim();
    var short = zoneShortName(geoId, meta);
    if (short && g) return short + ' - ' + g;
    if (short) return short;
    if (g) return g;
    return '';
  }

  /** Census-tract zone_code (SR) for display; falls back to geo_id. */
  function ctLabel(geoId, meta) {
    meta = meta || {};
    if (meta.zone_code) return String(meta.zone_code);
    if (meta.dest_zone_code && String(meta.dest_geo_id) === String(geoId)) {
      return String(meta.dest_zone_code);
    }
    if (meta.orig_zone_code) return String(meta.orig_zone_code);
    var g = String(geoId != null ? geoId : '');
    if (_zoneCodeMap && _zoneCodeMap[g]) return String(_zoneCodeMap[g]);
    return g;
  }

  function resolveGeoIdQuery(raw) {
    var q = String(raw || '').trim();
    if (!q) return null;
    var qLow = q.toLowerCase();
    if (qLow.indexOf(ZONE_SHORT_PREFIX + '+') === 0) {
      var rest = q.slice(ZONE_SHORT_PREFIX.length + 1).trim();
      if (/^\d+$/.test(rest)) return rest;
    }
    var nameId = q.match(/^(.+?)\s*(?:-\s*)?(\d+)$/);
    if (nameId) {
      var namePart = nameId[1].trim().toLowerCase();
      var idPart = nameId[2];
      if (_zoneNameMap && _zoneNameMap[idPart]) {
        var shortForId = zoneShortName(idPart, { zone_name: _zoneNameMap[idPart] }).toLowerCase();
        if (shortForId === namePart) return idPart;
      }
      if (/^\d+$/.test(idPart)) return idPart;
    }
    if (_zoneNameMap) {
      var byName = null;
      Object.keys(_zoneNameMap).forEach(function (gid) {
        if (byName) return;
        if (zoneShortName(gid, { zone_name: _zoneNameMap[gid] }).toLowerCase() === qLow) byName = gid;
      });
      if (byName) return byName;
    }
    if (/^\d+$/.test(q) && (!_zoneCodeMap || _zoneCodeMap[q])) return q;
    if (_zoneCodeMap) {
      var hit = null;
      Object.keys(_zoneCodeMap).forEach(function (gid) {
        if (hit) return;
        var code = String(_zoneCodeMap[gid]);
        if (code === q) hit = gid;
        else if (q.indexOf('.') < 0 && code.split('.')[0] === q) hit = gid;
      });
      if (hit) return hit;
    }
    return /^\d+$/.test(q) ? q : null;
  }

  function buildZoneDistById(raw, zones) {

    var zoneDistById = {};

    var add = function (gid, km) {

      var n = Number(km);

      if (Number.isFinite(n) && n > 0) zoneDistById[String(gid)] = n;

    };

    var map = (raw && raw.zone_distance_by_id) ? raw.zone_distance_by_id : {};

    Object.keys(map).forEach(function (gid) { add(gid, map[gid]); });

    (zones || []).forEach(function (z) {

      if (z && z.geo_id != null) add(z.geo_id, z.total_distance_km || z.distance_km);

    });

    return zoneDistById;

  }



  function formatTrips(n) {

    n = Math.round(Number(n) || 0);

    if (!Number.isFinite(n) || n < 0) return '0';

    return n.toLocaleString(undefined, { maximumFractionDigits: 0 });

  }



  function formatTonnesFromG(g) {

    var t = (Number(g) || 0) / 1e6;

    if (!Number.isFinite(t) || t < 0) return '— t';

    if (t >= 1000) return t.toLocaleString(undefined, { maximumFractionDigits: 0 }) + ' t';

    if (t >= 100) return t.toFixed(1) + ' t';

    if (t >= 10) return t.toFixed(1) + ' t';

    return t.toFixed(2) + ' t';

  }



  function formatDistanceKm(km) {

    var n = Number(km);

    if (!Number.isFinite(n) || n < 0) return null;

    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M km';

    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k km';

    return n.toLocaleString(undefined, { maximumFractionDigits: 1 }) + ' km';

  }



  function resolveZoneRow(propsOrZone, zonesById) {

    var row = propsOrZone || {};

    if (zonesById && row.geo_id != null) {

      var z = zonesById[String(row.geo_id)];

      if (z) {

        row = {

          geo_id: row.geo_id != null ? row.geo_id : z.geo_id,

          total_emissions_g: row.total_emissions_g != null ? row.total_emissions_g : z.total_emissions_g,

          trips: row.trips != null ? row.trips : z.trips,

          trips_legs: row.trips_legs != null ? row.trips_legs : z.trips_legs,

          trips_weighted: row.trips_weighted != null ? row.trips_weighted : z.trips_weighted,

          total_distance_km: row.total_distance_km != null ? row.total_distance_km : z.total_distance_km,

          total_distance_km_legs: row.total_distance_km_legs != null
            ? row.total_distance_km_legs : z.total_distance_km_legs,

          total_distance_km_weighted: row.total_distance_km_weighted != null
            ? row.total_distance_km_weighted : z.total_distance_km_weighted,

        };

      }

    }

    return row;

  }



  function resolveZoneDistanceKm(propsOrZone, zonesById) {

    if (!propsOrZone) return null;

    var gid = propsOrZone.geo_id != null ? String(propsOrZone.geo_id) : '';

    function pickKm(obj) {

      if (!obj) return null;

      var v = obj.total_distance_km;

      if (v == null || v === '') v = obj.distance_km;

      if (v == null || v === '' || !Number.isFinite(Number(v))) return null;

      return Number(v);

    }

    var fromProps = pickKm(propsOrZone);

    var fromZone = gid && zonesById ? pickKm(zonesById[gid]) : null;

    if (fromProps != null && fromProps > 0) return fromProps;

    if (fromZone != null && fromZone > 0) return fromZone;

    return fromProps != null ? fromProps : fromZone;

  }



  function formatWeightedTripsLabel(row) {
    var trips = Number(row.trips_weighted != null ? row.trips_weighted : row.trips) || 0;
    return formatTrips(trips) + ' trips';
  }

  var formatExpandedTripsLabel = formatWeightedTripsLabel;

  /** One line: "968.8 t · 274.5k trips · 4.75M km" */

  function zoneTooltipStatsLine(propsOrZone, zonesById) {

    var row = resolveZoneRow(propsOrZone, zonesById);

    var parts = [formatTonnesFromG(row.total_emissions_g), formatWeightedTripsLabel(row)];

    var dist = formatDistanceKm(resolveZoneDistanceKm(row, zonesById));

    parts.push(dist || '— km');

    return parts.join(' · ');

  }



  function lookupZoneKm(gid, zoneDistById, zonesById) {

    var g = String(gid);

    if (zoneDistById && Number(zoneDistById[g]) > 0) return Number(zoneDistById[g]);

    var row = zonesById && zonesById[g] ? zonesById[g] : null;

    if (row) {

      var v = row.total_distance_km;

      if (v == null || v === '') v = row.distance_km;

      v = Number(v);

      if (Number.isFinite(v) && v > 0) return v;

    }

    return null;

  }

  function zoneTooltipHtml(zid, propsOrZone, zonesById, zoneDistById) {

    var gid = propsOrZone && propsOrZone.geo_id != null ? String(propsOrZone.geo_id) : String(zid);

    var km = lookupZoneKm(gid, zoneDistById, zonesById) || resolveZoneDistanceKm(propsOrZone, zonesById);

    var row = resolveZoneRow(propsOrZone, zonesById);

    var parts = [formatTonnesFromG(row.total_emissions_g), formatWeightedTripsLabel(row)];

    parts.push(formatDistanceKm(km) || '— km');

    var title = zoneLabel(gid, propsOrZone || row);

    return '<div style="font-size:11px"><strong>' + title + '</strong><br>' +

      parts.join(' · ') + '</div>';

  }



  function parseZoneMapResponse(raw) {

    if (Array.isArray(raw)) {

      return { zones: raw, geojson: null, zoneDistById: buildZoneDistById(null, raw) };

    }

    var zones = Array.isArray(raw.zones) ? raw.zones : [];

    var zoneDistById = buildZoneDistById(raw, zones);

    var gj = raw.geojson && raw.geojson.type === 'FeatureCollection' && Array.isArray(raw.geojson.features)

      ? raw.geojson

      : null;

    if (gj && zones.length) {

      var byId = {};

      zones.forEach(function (z) {

        if (z && z.geo_id != null) byId[String(z.geo_id)] = z;

      });

      gj.features.forEach(function (f) {

        if (!f.properties) return;

        var z = byId[String(f.properties.geo_id)];

        if (z) {

          var zkm = z.total_distance_km;

          if (zkm == null || zkm === '') zkm = z.distance_km;

          var pkm = f.properties.total_distance_km;

          if (zkm != null && Number(zkm) > 0 && (pkm == null || Number(pkm) === 0)) {

            f.properties.total_distance_km = zkm;

          } else if (zkm != null && pkm == null) {

            f.properties.total_distance_km = zkm;

          }

          if (f.properties.trips == null && z.trips != null) f.properties.trips = z.trips;

          if (f.properties.trips_legs == null && z.trips_legs != null) f.properties.trips_legs = z.trips_legs;

          if (f.properties.trips_weighted == null && z.trips_weighted != null) {
            f.properties.trips_weighted = z.trips_weighted;
          }

          if (f.properties.total_emissions_g == null && z.total_emissions_g != null) {

            f.properties.total_emissions_g = z.total_emissions_g;

          }

          if (f.properties.total_distance_km_legs == null && z.total_distance_km_legs != null) {
            f.properties.total_distance_km_legs = z.total_distance_km_legs;
          }

          if (f.properties.total_distance_km_weighted == null && z.total_distance_km_weighted != null) {
            f.properties.total_distance_km_weighted = z.total_distance_km_weighted;
          }

          if (f.properties.zone_code == null && z.zone_code != null) {
            f.properties.zone_code = z.zone_code;
          }

          if (f.properties.zone_name == null && z.zone_name != null) {
            f.properties.zone_name = z.zone_name;
          }

          if (f.properties.short_id == null && z.short_id != null) {
            f.properties.short_id = z.short_id;
          }

          if (f.properties.zone_short_name == null && z.zone_short_name != null) {
            f.properties.zone_short_name = z.zone_short_name;
          }

          if (f.properties.zone_label == null && z.zone_label != null) {
            f.properties.zone_label = z.zone_label;
          }

        }

      });

    }

    return { zones: zones, geojson: gj, zoneDistById: zoneDistById };

  }



  function zonesByIdFromList(zones) {

    var m = {};

    (zones || []).forEach(function (z) {

      if (z && z.geo_id != null) m[String(z.geo_id)] = z;

    });

    return m;

  }

  function parseGeoIdList(raw) {
    var out = [];
    var seen = {};
    function add(v) {
      var g = String(v == null ? '' : v).trim();
      if (!g || seen[g]) return;
      seen[g] = true;
      out.push(g);
    }
    if (raw == null || raw === '') return out;
    if (Array.isArray(raw)) {
      raw.forEach(add);
      return out;
    }
    String(raw).split(/[,;\s]+/).forEach(add);
    return out;
  }

  function formatGeoIdList(ids) {
    return parseGeoIdList(ids).join(',');
  }

  function toggleGeoId(ids, geoId) {
    var list = parseGeoIdList(ids);
    var gid = String(geoId == null ? '' : geoId).trim();
    if (!gid) return list;
    var idx = list.indexOf(gid);
    if (idx >= 0) list.splice(idx, 1);
    else list.push(gid);
    return list;
  }

  function clickIsMultiToggle(ev) {
    var e = ev && (ev.originalEvent || ev);
    return !!(e && (e.ctrlKey || e.metaKey || e.shiftKey));
  }

  function selectionLabel(ids, labelForId) {
    var list = parseGeoIdList(ids);
    if (!list.length) return '';
    if (list.length === 1) {
      return labelForId ? String(labelForId(list[0]) || list[0]) : list[0];
    }
    return list.length + ' zones';
  }

  function aggregateZoneStats(rows) {
    var trips = 0;
    var tripsW = 0;
    var emis = 0;
    var emisW = 0;
    var km = 0;
    var kmW = 0;
    (rows || []).forEach(function (r) {
      r = r || {};
      var t = Number(r.trips) || 0;
      trips += t;
      tripsW += Number(r.trips_weighted != null ? r.trips_weighted : t) || 0;
      var e = Number(r.total_emissions_g) || 0;
      emis += e;
      emisW += Number(r.total_emissions_g_weighted != null ? r.total_emissions_g_weighted : e) || 0;
      var d = Number(r.total_distance_km) || 0;
      km += d;
      kmW += Number(r.total_distance_km_weighted != null ? r.total_distance_km_weighted : d) || 0;
    });
    return {
      trips: trips,
      trips_weighted: tripsW,
      total_emissions_g: emis,
      total_emissions_g_weighted: emisW,
      total_distance_km: km,
      total_distance_km_weighted: kmW,
      avg_emissions_g_per_trip: trips > 0 ? emis / trips : 0,
      kpi_scope: 'zone',
      zone_count: (rows || []).length,
    };
  }

  function mergeCategoryRows(lists) {
    var byCat = {};
    (lists || []).forEach(function (rows) {
      (rows || []).forEach(function (c) {
        if (!c) return;
        var k = String(c.category || '');
        if (!byCat[k]) {
          byCat[k] = {
            category: c.category,
            trips: 0,
            trips_weighted: 0,
            total_emissions_g: 0,
            total_emissions_g_weighted: 0,
            total_distance_km: 0,
            total_distance_km_weighted: 0,
          };
        }
        var acc = byCat[k];
        var t = Number(c.trips) || 0;
        acc.trips += t;
        acc.trips_weighted += Number(c.trips_weighted != null ? c.trips_weighted : t) || 0;
        var e = Number(c.total_emissions_g) || 0;
        acc.total_emissions_g += e;
        acc.total_emissions_g_weighted += Number(
          c.total_emissions_g_weighted != null ? c.total_emissions_g_weighted : e
        ) || 0;
        var d = Number(c.total_distance_km) || 0;
        acc.total_distance_km += d;
        acc.total_distance_km_weighted += Number(
          c.total_distance_km_weighted != null ? c.total_distance_km_weighted : d
        ) || 0;
      });
    });
    return Object.keys(byCat).map(function (k) { return byCat[k]; })
      .sort(function (a, b) { return (b.total_emissions_g || 0) - (a.total_emissions_g || 0); });
  }

  function mergeIncomingPayloads(destIds, payloads) {
    var ids = parseGeoIdList(destIds);
    var destSet = {};
    ids.forEach(function (id) { destSet[id] = true; });
    var byOrig = {};
    var lats = [];
    var lons = [];
    var zoneTrips = 0;
    var zoneEmis = 0;
    var zoneKm = 0;
    var base = null;
    (payloads || []).forEach(function (p) {
      if (!p) return;
      if (!base) base = p;
      if (Number.isFinite(Number(p.dest_lat))) lats.push(Number(p.dest_lat));
      if (Number.isFinite(Number(p.dest_lon))) lons.push(Number(p.dest_lon));
      zoneTrips += Number(p.dest_zone_trips != null ? p.dest_zone_trips : p.dest_rules_trips) || 0;
      zoneEmis += Number(p.dest_zone_emissions_g != null ? p.dest_zone_emissions_g : p.dest_rules_emissions_g) || 0;
      zoneKm += Number(p.dest_zone_distance_km) || 0;
      (p.flows || []).forEach(function (f) {
        var oid = String(f && f.orig_geo_id != null ? f.orig_geo_id : '');
        if (!oid || destSet[oid]) return;
        if (!byOrig[oid]) {
          byOrig[oid] = {
            orig_geo_id: oid,
            trips: 0,
            total_emissions_g: 0,
            total_distance_km: 0,
            orig_lat: f.orig_lat,
            orig_lon: f.orig_lon,
            orig_zone_code: f.orig_zone_code || f.zone_code,
            orig_zone_name: f.orig_zone_name || f.zone_name,
            orig_zone_label: f.orig_zone_label || f.zone_label,
            zone_code: f.zone_code,
            zone_name: f.zone_name,
            zone_label: f.zone_label,
          };
        }
        var acc = byOrig[oid];
        acc.trips += Number(f.trips) || 0;
        acc.total_emissions_g += Number(f.total_emissions_g) || 0;
        acc.total_distance_km += Number(f.total_distance_km) || 0;
        if (acc.orig_lat == null && f.orig_lat != null) acc.orig_lat = f.orig_lat;
        if (acc.orig_lon == null && f.orig_lon != null) acc.orig_lon = f.orig_lon;
      });
    });
    var destLat = lats.length ? lats.reduce(function (a, b) { return a + b; }, 0) / lats.length : null;
    var destLon = lons.length ? lons.reduce(function (a, b) { return a + b; }, 0) / lons.length : null;
    var flows = Object.keys(byOrig).map(function (k) {
      var f = byOrig[k];
      f.dest_lat = destLat;
      f.dest_lon = destLon;
      return f;
    }).sort(function (a, b) { return (b.total_emissions_g || 0) - (a.total_emissions_g || 0); });
    var extTrips = 0;
    var extEmis = 0;
    var extKm = 0;
    flows.forEach(function (f) {
      extTrips += Number(f.trips) || 0;
      extEmis += Number(f.total_emissions_g) || 0;
      extKm += Number(f.total_distance_km) || 0;
    });
    var out = Object.assign({}, base || {});
    out.dest_geo_id = ids[0] || '';
    out.dest_geo_ids = ids;
    out.dest_lat = destLat;
    out.dest_lon = destLon;
    out.total_incoming_trips = extTrips;
    out.total_incoming_emissions_g = extEmis;
    out.total_incoming_distance_km = Math.round(extKm * 100) / 100;
    out.origin_zone_count = flows.length;
    out.flow_count = flows.length;
    out.flows = flows;
    out.dest_zone_trips = zoneTrips;
    out.dest_zone_emissions_g = zoneEmis;
    out.dest_zone_distance_km = zoneKm;
    out.dest_rules_trips = zoneTrips;
    out.dest_rules_emissions_g = zoneEmis;
    if (ids.length > 1) {
      out.dest_zone_label = ids.length + ' zones';
      out.zone_label = ids.length + ' zones';
    }
    var intraTrips = Math.max(0, zoneTrips - extTrips);
    var intraEmis = Math.max(0, zoneEmis - extEmis);
    var intraKm = Math.max(0, zoneKm - extKm);
    if (intraTrips > 0 || intraEmis > 0) {
      out.intra_zone = {
        orig_geo_id: ids[0] || '',
        dest_geo_id: ids[0] || '',
        is_intra_zone: true,
        trips: intraTrips,
        total_emissions_g: intraEmis,
        total_distance_km: Math.round(intraKm * 100) / 100,
        orig_lat: destLat,
        orig_lon: destLon,
        dest_lat: destLat,
        dest_lon: destLon,
      };
    } else {
      out.intra_zone = null;
    }
    return out;
  }

  function renderSelectionChips(host, ids, labelForId, onRemove) {
    if (!host) return;
    host.innerHTML = '';
    parseGeoIdList(ids).forEach(function (gid) {
      var chip = document.createElement('span');
      chip.className = 'zone-sel-chip';
      var lab = document.createElement('span');
      lab.textContent = labelForId ? (labelForId(gid) || gid) : gid;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.setAttribute('aria-label', 'Remove zone ' + gid);
      btn.textContent = '\u00d7';
      btn.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        if (onRemove) onRemove(gid);
      });
      chip.appendChild(lab);
      chip.appendChild(btn);
      host.appendChild(chip);
    });
  }



  global.DashZoneUi = {

    VERSION: VERSION,

    setZoneCodeMap: setZoneCodeMap,

    setZoneNameMap: setZoneNameMap,

    shortZoneId: shortZoneId,

    zoneName: zoneName,

    zoneShortName: zoneShortName,

    zoneLabel: zoneLabel,

    ctLabel: ctLabel,

    resolveGeoIdQuery: resolveGeoIdQuery,

    formatTrips: formatTrips,

    formatTonnesFromG: formatTonnesFromG,

    formatDistanceKm: formatDistanceKm,

    resolveZoneDistanceKm: resolveZoneDistanceKm,

    buildZoneDistById: buildZoneDistById,

    lookupZoneKm: lookupZoneKm,

    zoneTooltipStatsLine: zoneTooltipStatsLine,

    zoneTooltipHtml: zoneTooltipHtml,

    parseZoneMapResponse: parseZoneMapResponse,

    zonesByIdFromList: zonesByIdFromList,

    parseGeoIdList: parseGeoIdList,

    formatGeoIdList: formatGeoIdList,

    toggleGeoId: toggleGeoId,

    clickIsMultiToggle: clickIsMultiToggle,

    selectionLabel: selectionLabel,

    aggregateZoneStats: aggregateZoneStats,

    mergeCategoryRows: mergeCategoryRows,

    mergeIncomingPayloads: mergeIncomingPayloads,

    renderSelectionChips: renderSelectionChips,

  };

})(typeof window !== 'undefined' ? window : globalThis);

