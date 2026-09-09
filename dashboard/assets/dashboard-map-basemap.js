/** Basemap helper: OSM streets (no API key), dimmed so overlays stay readable. */
(function (global) {
  var OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
  var OSM_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
  var ESRI_DARK = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}';

  var OSM_OPTS = {
    attribution: OSM_ATTR,
    className: 'dash-basemap-dim',
    maxNativeZoom: 19,
    maxZoom: 20,
  };

  var VARIANTS = {
    light_all: {
      url: OSM_URL,
      background: '#9aa19a',
      options: OSM_OPTS,
    },
    light_nolabels: {
      url: OSM_URL,
      background: '#9aa19a',
      options: OSM_OPTS,
    },
    dark_all: {
      url: ESRI_DARK,
      background: '#1a2436',
      options: {
        attribution: 'Tiles &copy; Esri',
        maxNativeZoom: 16,
        maxZoom: 20,
      },
    },
  };

  function ensureStyles() {
    if (typeof document === 'undefined' || document.getElementById('dash-basemap-styles')) return;
    var el = document.createElement('style');
    el.id = 'dash-basemap-styles';
    el.textContent = [
      '.leaflet-container { background: #9aa19a; }',
      /* Keep streets/parks/labels; pull down the white wash so flow/zone layers read. */
      '.dash-basemap-dim { filter: brightness(0.62) saturate(0.72) contrast(0.98); }',
    ].join('\n');
    document.head.appendChild(el);
  }

  function offline() {
    if (global.DashConfig && typeof global.DashConfig.offline === 'function') {
      return global.DashConfig.offline();
    }
    return !!(global.__dashDeploy && global.__dashDeploy.offline);
  }

  function markOfflineMap(map) {
    if (!map || !map.getContainer) return;
    map.getContainer().classList.add('dash-offline-basemap');
    if (document.documentElement) document.documentElement.classList.add('dash-offline');
    if (document.body) document.body.classList.add('dash-offline');
  }

  function addTo(map, variant, opts) {
    if (!map || !global.L) return null;
    variant = variant || 'light_all';
    opts = opts || {};
    if (offline()) {
      markOfflineMap(map);
      return null;
    }
    ensureStyles();
    var spec = VARIANTS[variant] || VARIANTS.light_all;
    var layer = global.L.tileLayer(spec.url, Object.assign({}, spec.options, opts));
    layer.addTo(map);
    var el = map.getContainer && map.getContainer();
    if (el && spec.background) el.style.background = spec.background;
    return layer;
  }

  global.DashMapBasemap = { addTo: addTo, offline: offline };
})(typeof window !== 'undefined' ? window : globalThis);
