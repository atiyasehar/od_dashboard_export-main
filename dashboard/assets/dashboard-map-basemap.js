/** Basemap helper: Carto tiles online; plain grid background when DashConfig.offline(). */
(function (global) {
  var CARTO = {
    light_all: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
    light_nolabels: 'https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png',
    dark_all: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  };

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
    var url = CARTO[variant] || CARTO.light_all;
    var layer = global.L.tileLayer(url, Object.assign({
      attribution: '&copy; OpenStreetMap &copy; CARTO',
      subdomains: 'abcd',
      maxZoom: 20,
    }, opts));
    layer.addTo(map);
    return layer;
  }

  global.DashMapBasemap = { addTo: addTo, offline: offline };
})(typeof window !== 'undefined' ? window : globalThis);
