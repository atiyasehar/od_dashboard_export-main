/** Public-transport overlay from official GTFS (STM, REM, exo). */
(function (global) {
  var STORAGE_ON = 'dash.transit.on';
  var STORAGE_MODES = 'dash.transit.modes';
  var DEFAULT_MODES = { metro: true, rem: true, train: true, tram: true, bus: true };
  var MODE_LABEL = {
    metro: 'Metro',
    rem: 'REM',
    train: 'Train',
    tram: 'Tram',
    bus: 'Bus',
  };
  var MODE_FALLBACK = {
    metro: '#00a651',
    rem: '#00b2a9',
    train: '#6d28d9',
    tram: '#f59e0b',
    bus: '#64748b',
  };
  var RAIL_MODES = ['metro', 'rem', 'train', 'tram'];

  var BUS_STOP_MIN_ZOOM = 13;
  var state = {
    on: false,
    modes: Object.assign({}, DEFAULT_MODES),
    railFc: null,
    busFc: null,
    railStops: null,
    busStops: null,
    railPromise: null,
    busPromise: null,
    status: '',
  };
  var maps = [];
  var listeners = [];

  function loadState() {
    try {
      state.on = global.localStorage.getItem(STORAGE_ON) === '1';
      var raw = global.localStorage.getItem(STORAGE_MODES);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
          Object.keys(DEFAULT_MODES).forEach(function (k) {
            if (typeof parsed[k] === 'boolean') state.modes[k] = parsed[k];
          });
        }
      }
    } catch (_) { /* ignore */ }
  }

  function persist() {
    try {
      global.localStorage.setItem(STORAGE_ON, state.on ? '1' : '0');
      global.localStorage.setItem(STORAGE_MODES, JSON.stringify(state.modes));
    } catch (_) { /* ignore */ }
  }

  function notify() {
    listeners.forEach(function (fn) {
      try { fn(); } catch (_) { /* ignore */ }
    });
  }

  function apiUrl(path) {
    if (global.DashConfig && typeof global.DashConfig.apiUrl === 'function') {
      return global.DashConfig.apiUrl(path);
    }
    return path;
  }

  function ensureStyles() {
    if (typeof document === 'undefined' || document.getElementById('dash-transit-styles')) return;
    var el = document.createElement('style');
    el.id = 'dash-transit-styles';
    el.textContent = [
      '.dash-transit-ctrl {',
      '  background: rgba(12,17,25,0.92); color: #e8eef7; border: 1px solid rgba(94,129,172,0.28);',
      '  border-radius: 12px; box-shadow: 0 12px 32px rgba(0,0,0,0.35); padding: 0.45rem 0.6rem;',
      '  font: 600 12px/1.3 "DM Sans", system-ui, sans-serif; min-width: 168px;',
      '}',
      '.dash-transit-ctrl label { display: flex; align-items: center; gap: 0.4rem; cursor: pointer; user-select: none; }',
      '.dash-transit-ctrl input { accent-color: #5eead4; margin: 0; }',
      '.dash-transit-title { font-weight: 700; letter-spacing: 0.02em; }',
      '.dash-transit-modes { margin-top: 0.35rem; display: grid; gap: 0.2rem; }',
      '.dash-transit-modes label { font-weight: 500; color: #c5d0e0; font-size: 11px; }',
      '.dash-transit-swatch { width: 10px; height: 10px; border-radius: 2px; flex: 0 0 10px; }',
      '.dash-transit-status { margin-top: 0.3rem; font-size: 10px; font-weight: 500; color: #8b9cb8; max-width: 180px; }',
      '.dash-transit-status.err { color: #fda4af; }',
      '.leaflet-bottom.leaflet-left .dash-transit-ctrl { margin-bottom: 8px; }',
      '.dash-transit-tooltip { font: 600 11px/1.3 "DM Sans", system-ui, sans-serif; }',
      '.dash-transit-pin { background: none !important; border: none !important; }',
      '.dash-transit-pin svg { display: block; filter: drop-shadow(0 1px 1px rgba(0,0,0,0.35)); }',
    ].join('\n');
    document.head.appendChild(el);
  }

  function styleFor(props) {
    var mode = (props && props.mode) || 'train';
    var colour = (props && props.colour) || MODE_FALLBACK[mode] || '#64748b';
    var weight = mode === 'bus' ? 1.6 : (mode === 'metro' ? 4.2 : 3.2);
    var opacity = mode === 'bus' ? 0.55 : 0.92;
    return {
      color: colour,
      weight: weight,
      opacity: opacity,
      lineJoin: 'round',
      lineCap: 'round',
      interactive: false,
    };
  }

  function featureVisible(props) {
    if (!state.on) return false;
    var mode = (props && props.mode) || '';
    return !!state.modes[mode];
  }

  function stopVisible(props, zoom) {
    if (!featureVisible(props)) return false;
    var mode = (props && props.mode) || '';
    var modes = (props && props.modes) || [mode];
    var busOnly = modes.length === 1 && modes[0] === 'bus';
    if (busOnly && zoom < BUS_STOP_MIN_ZOOM) return false;
    return true;
  }

  function ensurePane(map) {
    if (!map.getPane('transitPane')) {
      map.createPane('transitPane');
    }
    var pane = map.getPane('transitPane');
    pane.style.zIndex = 500;
    pane.style.pointerEvents = 'none';
    if (!map.getPane('transitStopPane')) {
      map.createPane('transitStopPane');
    }
    var stopPane = map.getPane('transitStopPane');
    stopPane.style.zIndex = 530;
    stopPane.style.pointerEvents = 'auto';
    return pane;
  }

  function addCollection(map, rec, fc, renderer) {
    if (!fc || !fc.features || !fc.features.length) return;
    rec.layer = global.L.geoJSON(fc, {
      pane: 'transitPane',
      renderer: renderer,
      filter: function (feature) {
        return featureVisible(feature.properties || {});
      },
      style: function (feature) {
        return styleFor(feature.properties || {});
      },
    });
    rec.layer.addTo(map);
  }

  function pinHtml(colour, busOnly) {
    var fill = colour || '#111111';
    if (!/^#[0-9A-Fa-f]{6}$/.test(fill)) fill = '#111111';
    var w = busOnly ? 14 : 22;
    var h = busOnly ? 22 : 34;
    return (
      '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h + '" viewBox="0 0 24 36" aria-hidden="true">' +
        '<ellipse cx="12" cy="33.2" rx="5.8" ry="1.7" fill="#111" opacity="0.4"/>' +
        '<path fill-rule="evenodd" fill="' + fill + '" d="' +
          'M12 1.4C7.15 1.4 3.2 5.35 3.2 10.2c0 7.35 8.8 18.3 8.8 18.3s8.8-10.95 8.8-18.3C20.8 5.35 16.85 1.4 12 1.4z' +
          'M12 13.55a3.35 3.35 0 1 0 0-6.7 3.35 3.35 0 0 0 0 6.7z"/>' +
      '</svg>'
    );
  }

  function stopMarker(feature, latlng) {
    var props = feature.properties || {};
    var mode = props.mode || 'train';
    var busOnly = ((props.modes || [mode]).length === 1 && mode === 'bus');
    var colour = '#111111';
    var size = busOnly ? [14, 22] : [22, 34];
    if (busOnly) {
      var marker = global.L.circleMarker(latlng, {
        pane: 'transitStopPane',
        radius: 3.2,
        color: '#111111',
        weight: 1,
        fillColor: '#111111',
        fillOpacity: 0.9,
        interactive: false,
      });
      marker.bindTooltip(props.name || 'Stop', {
        className: 'dash-transit-tooltip',
        direction: 'top',
        offset: [0, -4],
        opacity: 0.95,
      });
      return marker;
    }
    var marker = global.L.marker(latlng, {
      pane: 'transitStopPane',
      interactive: !busOnly,
      keyboard: false,
      icon: global.L.divIcon({
        className: 'dash-transit-pin',
        html: pinHtml(colour, busOnly),
        iconSize: size,
        iconAnchor: [size[0] / 2, size[1] - 2],
        tooltipAnchor: [0, -size[1] + 8],
      }),
    });
    var label = props.name || 'Stop';
    if (props.ref) label += ' · ' + props.ref;
    marker.bindTooltip(label, {
      className: 'dash-transit-tooltip',
      direction: 'top',
      offset: [0, -4],
      opacity: 0.95,
    });
    return marker;
  }

  function addStops(map, rec, fc, zoom) {
    if (!fc || !fc.features || !fc.features.length) return;
    rec.stopLayer = global.L.geoJSON(fc, {
      pane: 'transitStopPane',
      filter: function (feature) {
        return stopVisible(feature.properties || {}, zoom);
      },
      pointToLayer: stopMarker,
    });
    rec.stopLayer.addTo(map);
  }

  function clearRec(rec) {
    ['layer', 'busLayer', 'stopLayer', 'busStopLayer'].forEach(function (key) {
      if (rec[key] && rec.map) rec.map.removeLayer(rec[key]);
      rec[key] = null;
    });
  }

  function redrawMap(rec) {
    if (!rec || !rec.map) return;
    ensurePane(rec.map);
    clearRec(rec);
    if (!state.on) return;
    var zoom = rec.map.getZoom();
    var renderer = global.L.canvas ? global.L.canvas({ padding: 0.4, pane: 'transitPane' }) : undefined;
    if (state.railFc) addCollection(rec.map, rec, state.railFc, renderer);
    if (state.modes.bus && state.busFc) {
      rec.busLayer = global.L.geoJSON(state.busFc, {
        pane: 'transitPane',
        renderer: renderer,
        filter: function (feature) {
          return featureVisible(feature.properties || {});
        },
        style: function (feature) {
          return styleFor(feature.properties || {});
        },
      });
      rec.busLayer.addTo(rec.map);
    }
    if (state.railStops) addStops(rec.map, rec, state.railStops, zoom);
    if (state.modes.bus && state.busStops) {
      rec.busStopLayer = global.L.geoJSON(state.busStops, {
        pane: 'transitStopPane',
        filter: function (feature) {
          return stopVisible(feature.properties || {}, zoom);
        },
        pointToLayer: stopMarker,
      });
      rec.busStopLayer.addTo(rec.map);
    }
  }

  function redrawAll() {
    maps.forEach(redrawMap);
    notify();
  }

  function fetchGroup(group) {
    return fetch(apiUrl('/api/od/transit_network?group=' + encodeURIComponent(group)))
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error((body && body.message) || ('HTTP ' + r.status));
          return body;
        });
      })
      .then(function (body) {
        return {
          geojson: (body && body.geojson) || { type: 'FeatureCollection', features: [] },
          stops: (body && body.stops) || { type: 'FeatureCollection', features: [] },
        };
      });
  }

  function setStatus(text, isErr) {
    state.status = text || '';
    state.statusErr = !!isErr;
    notify();
  }

  function ensureRail() {
    if (state.railFc) return Promise.resolve(state.railFc);
    if (state.railPromise) return state.railPromise;
    setStatus('Loading STM / REM / exo…');
    state.railPromise = fetchGroup('rail').then(function (payload) {
      state.railFc = payload.geojson;
      state.railStops = payload.stops;
      var n = (payload.geojson.features || []).length;
      var s = (payload.stops.features || []).length;
      setStatus(n ? (n + ' lines · ' + s + ' stations') : 'No rail lines found');
      return payload.geojson;
    }).catch(function (err) {
      state.railPromise = null;
      setStatus(err.message || 'Transit overlay unavailable', true);
      throw err;
    });
    return state.railPromise;
  }

  function ensureBus() {
    if (state.busFc) return Promise.resolve(state.busFc);
    if (state.busPromise) return state.busPromise;
    setStatus('Loading bus routes…');
    state.busPromise = fetchGroup('bus').then(function (payload) {
      state.busFc = payload.geojson;
      state.busStops = payload.stops;
      var n = (payload.geojson.features || []).length;
      setStatus(n ? (n + ' bus routes') : 'No bus routes found');
      return payload.geojson;
    }).catch(function (err) {
      state.busPromise = null;
      setStatus(err.message || 'Bus overlay unavailable', true);
      throw err;
    });
    return state.busPromise;
  }

  function applyEnabled(on) {
    state.on = !!on;
    persist();
    if (!state.on) {
      redrawAll();
      setStatus('');
      return;
    }
    ensureRail().then(function () {
      return state.modes.bus ? ensureBus() : Promise.resolve();
    }).then(function () {
      var railN = (state.railFc && state.railFc.features) ? state.railFc.features.length : 0;
      var stopN = (state.railStops && state.railStops.features) ? state.railStops.features.length : 0;
      var busN = (state.busFc && state.busFc.features) ? state.busFc.features.length : 0;
      if (state.modes.bus) setStatus(railN + ' rail · ' + stopN + ' stations · ' + busN + ' bus');
      else setStatus(railN + ' rail · ' + stopN + ' stations');
      redrawAll();
    }).catch(redrawAll);
  }

  function applyMode(mode, on) {
    if (!Object.prototype.hasOwnProperty.call(state.modes, mode)) return;
    state.modes[mode] = !!on;
    persist();
    if (!state.on) {
      notify();
      return;
    }
    if (mode === 'bus' && state.modes.bus) {
      ensureBus().then(redrawAll).catch(redrawAll);
    } else {
      redrawAll();
    }
  }

  function bindControl(container) {
    var onBox = container.querySelector('[data-transit-on]');
    if (onBox) {
      onBox.checked = state.on;
      onBox.addEventListener('change', function () {
        applyEnabled(onBox.checked);
      });
    }
    container.querySelectorAll('[data-transit-mode]').forEach(function (box) {
      var mode = box.getAttribute('data-transit-mode');
      box.checked = !!state.modes[mode];
      box.addEventListener('change', function () {
        applyMode(mode, box.checked);
      });
    });
    var modesEl = container.querySelector('.dash-transit-modes');
    var statusEl = container.querySelector('.dash-transit-status');
    listeners.push(function () {
      if (onBox) onBox.checked = state.on;
      if (modesEl) modesEl.hidden = !state.on;
      container.querySelectorAll('[data-transit-mode]').forEach(function (box) {
        box.checked = !!state.modes[box.getAttribute('data-transit-mode')];
      });
      if (statusEl) {
        statusEl.textContent = state.status || '';
        statusEl.classList.toggle('err', !!state.statusErr);
        statusEl.hidden = !state.status;
      }
    });
  }

  function buildControlHtml() {
    var modes = ['metro', 'rem', 'train', 'bus'].map(function (mode) {
      return (
        '<label>' +
          '<input type="checkbox" data-transit-mode="' + mode + '">' +
          '<span class="dash-transit-swatch" style="background:' + MODE_FALLBACK[mode] + '"></span>' +
          MODE_LABEL[mode] +
        '</label>'
      );
    }).join('');
    return (
      '<div class="dash-transit-ctrl">' +
        '<label class="dash-transit-title">' +
          '<input type="checkbox" data-transit-on>' +
          'Transit overlay' +
        '</label>' +
        '<div class="dash-transit-modes" hidden>' + modes + '</div>' +
        '<div class="dash-transit-status" hidden></div>' +
      '</div>'
    );
  }

  function attach(map) {
    if (!map || !global.L) return null;
    loadState();
    ensureStyles();
    ensurePane(map);
    var rec = { map: map, layer: null, busLayer: null, stopLayer: null, busStopLayer: null };
    maps.push(rec);
    if (!rec._zoomBound) {
      rec._zoomBound = true;
      map.on('zoomend', function () {
        if (state.on) redrawMap(rec);
      });
    }

    var Control = global.L.Control.extend({
      options: { position: 'bottomleft' },
      onAdd: function () {
        var wrap = global.L.DomUtil.create('div', 'dash-transit-wrap');
        wrap.innerHTML = buildControlHtml();
        global.L.DomEvent.disableClickPropagation(wrap);
        global.L.DomEvent.disableScrollPropagation(wrap);
        bindControl(wrap);
        return wrap;
      },
    });
    rec.control = new Control();
    rec.control.addTo(map);

    if (state.on) {
      applyEnabled(true);
    } else {
      notify();
    }

    if (!attach._storageBound && global.addEventListener) {
      attach._storageBound = true;
      global.addEventListener('storage', function (ev) {
        if (!ev || (ev.key !== STORAGE_ON && ev.key !== STORAGE_MODES)) return;
        loadState();
        if (state.on) applyEnabled(true);
        else applyEnabled(false);
      });
    }
    return rec;
  }

  loadState();
  global.DashMapTransit = {
    attach: attach,
    MODE_LABEL: MODE_LABEL,
    RAIL_MODES: RAIL_MODES,
  };
})(typeof window !== 'undefined' ? window : globalThis);
