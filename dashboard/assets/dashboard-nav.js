/** Preserve ?api=, nav routing (SPA host + full-page), attribution sub-tabs. */
(function () {
  var ATTRIBUTION_KEY = 'dashAttribution';

  function normalizeAttribution(value) {
    var v = (value == null ? '' : String(value)).trim().toLowerCase();
    if (v === 'meeting') return 'rules';
    return v === 'dest' ? 'dest' : 'rules';
  }

  function isEmbedFrame() {
    try {
      return new URLSearchParams(window.location.search).get('embed') === '1';
    } catch (_) {
      return false;
    }
  }

  function readAttributionFromUrl(search) {
    try {
      var params = new URLSearchParams(search != null ? search : window.location.search);
      var v = (params.get('attribution') || params.get('zone_by') || '').trim().toLowerCase();
      if (v === 'dest') return 'dest';
      if (v === 'rules' || v === 'meeting') return 'rules';
      if (search == null && isEmbedFrame() && window.parent !== window) {
        try {
          return readAttributionFromUrl(window.parent.location.search);
        } catch (_) { /* cross-origin */ }
      }
    } catch (_) { /* empty */ }
    return null;
  }

  function persistAttributionSession(value) {
    try {
      sessionStorage.setItem(ATTRIBUTION_KEY, normalizeAttribution(value) === 'dest' ? 'dest' : 'rules');
    } catch (_) { /* empty */ }
  }

  function syncAttributionToUrl(value) {
    var attr = normalizeAttribution(value);
    try {
      if (isEmbedFrame() && window.parent !== window) {
        window.parent.postMessage({ type: 'dash-attribution', attribution: attr }, '*');
      }
      var params = new URLSearchParams(window.location.search);
      if (attr === 'dest') params.set('attribution', 'dest');
      else params.delete('attribution');
      params.delete('zone_by');
      var q = params.toString();
      var url = window.location.pathname + (q ? '?' + q : '') + window.location.hash;
      if (window.history && window.history.replaceState) {
        window.history.replaceState(window.history.state, '', url);
      }
    } catch (_) { /* empty */ }
  }

  function preservedQueryParams() {
    var params = new URLSearchParams();
    try {
      var current = new URLSearchParams(window.location.search);
      var api = (current.get('od-dashboard-api') || '').trim();
      if (api) params.set('od-dashboard-api', api.replace(/\/$/, ''));
      var attr = readAttributionFromUrl();
      if (!attr) {
        try {
          if (sessionStorage.getItem(ATTRIBUTION_KEY) === 'dest') attr = 'dest';
        } catch (_) { /* empty */ }
      }
      if (attr === 'dest') params.set('attribution', 'dest');
    } catch (_) { /* empty */ }
    return params;
  }

  function appendPreservedQuery(base) {
    var extra = preservedQueryParams().toString();
    if (!extra) return base;
    return base + (base.indexOf('?') >= 0 ? '&' : '?') + extra;
  }

  function apiQuerySuffix() {
    try {
      var api = (new URLSearchParams(window.location.search).get('od-dashboard-api') || '').trim();
      return api ? ('?api=' + encodeURIComponent(api.replace(/\/$/, ''))) : '';
    } catch (_) {
      return '';
    }
  }

  function isSpaHost() {
    return document.body && (
      document.body.classList.contains('dash-spa-host') ||
      document.body.classList.contains('dash-spa-host-od')
    );
  }

  function isEmbedView() {
    return window.DashEmbed && DashEmbed.isEmbed;
  }

  function markActiveNav(activePage) {
    var page = activePage || (document.body && document.body.dataset.dashPage) || '';
    if (page === 'host') return;

    document.querySelectorAll('.dash-nav-link').forEach(function (a) {
      var href = a.getAttribute('href');
      if (href && href.indexOf('?') === -1 && href.indexOf('#') !== 0) {
        a.setAttribute('href', appendPreservedQuery(href));
      }
      if (page && a.dataset.page === page) {
        a.classList.add('active');
      } else {
        a.classList.remove('active');
      }
    });
  }

  function navigateWithTransition(url) {
    if (document.startViewTransition) {
      document.startViewTransition(function () {
        window.location.href = url;
      });
      return;
    }
    var main = document.querySelector('.main');
    if (main) {
      main.classList.add('dash-page-leaving');
      window.setTimeout(function () {
        window.location.href = url;
      }, 200);
      return;
    }
    window.location.href = url;
  }

  function spaShowPage(page) {
    if (window.DashSpaOd && typeof DashSpaOd.showPage === 'function') {
      DashSpaOd.showPage(page);
      return true;
    }
    if (window.DashSpa && typeof DashSpa.showPage === 'function') {
      DashSpa.showPage(page);
      return true;
    }
    return false;
  }

  var OD_PAGE_PATHS = {
    'od-zones': '/?view=zones',
    'od-buildings': '/?view=buildings',
    'od-flows': '/?view=flows',
    'od-boundaries': '/od-zones-boundary.html',
  };

  function appendApiQuery(base) {
    return appendPreservedQuery(base);
  }

  function fullPageUrl(page) {
    var base;
    if (OD_PAGE_PATHS[page]) base = OD_PAGE_PATHS[page];
    else if (page === 'zones') base = '/';
    else if (page === 'boundaries') base = '/zones-boundary.html';
    else base = '/' + page + '.html';
    return appendPreservedQuery(base);
  }

  function bindNavLinks() {
    if (isEmbedView()) return;

    document.querySelectorAll('.dash-nav-link').forEach(function (a) {
      a.addEventListener('click', function (e) {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;

        var page = (a.dataset.page || '').trim();
        if (!page) return;

        if (isSpaHost()) {
          e.preventDefault();
          if (page === 'od-boundaries' || page === 'boundaries') {
            navigateWithTransition(fullPageUrl(page));
          } else {
            spaShowPage(page);
          }
          return;
        }

        var href = a.getAttribute('href');
        if (!href || href.indexOf('#') === 0) return;

        var target;
        try {
          target = new URL(href, window.location.href);
        } catch (_) {
          return;
        }
        if (target.origin !== window.location.origin) return;

        e.preventDefault();
        navigateWithTransition(fullPageUrl(page));
      });
    });
  }

  function readAttribution(defaultValue) {
    var fromUrl = readAttributionFromUrl();
    if (fromUrl) {
      persistAttributionSession(fromUrl);
      return fromUrl;
    }
    try {
      var v = sessionStorage.getItem(ATTRIBUTION_KEY);
      return v === 'dest' ? 'dest' : (defaultValue || 'rules');
    } catch (_) {
      return defaultValue || 'rules';
    }
  }

  function saveAttribution(value) {
    var attr = normalizeAttribution(value);
    persistAttributionSession(attr);
    syncAttributionToUrl(attr);
  }

  function applyStoredAttributionPanels() {
    var nav = document.querySelector('[data-attribution-nav]');
    if (!nav) return;
    var panels = document.querySelectorAll('[data-attribution-panel]');
    var attr = readAttribution('rules');
    nav.querySelectorAll('.dash-subnav-link').forEach(function (btn) {
      btn.classList.toggle('active', btn.dataset.attribution === attr);
    });
    panels.forEach(function (panel) {
      var show = panel.dataset.attributionPanel === attr;
      panel.classList.toggle('active', show);
      panel.hidden = !show;
    });
  }

  function seedHostAttributionUrl() {
    if (!isSpaHost()) return;
    try {
      var fromUrl = readAttributionFromUrl();
      if (fromUrl) {
        persistAttributionSession(fromUrl);
        return;
      }
      if (sessionStorage.getItem(ATTRIBUTION_KEY) === 'dest') {
        syncAttributionToUrl('dest');
      }
    } catch (_) { /* empty */ }
  }

  function initAttributionTabs(opts) {
    opts = opts || {};
    var nav = document.querySelector('[data-attribution-nav]');
    if (!nav) return;

    var panels = document.querySelectorAll('[data-attribution-panel]');
    var mapViews = opts.mapViews || null;
    var rulesKey = opts.rulesKey || 'rules';
    var destKey = opts.destKey || 'dest';
    var onSwitch = typeof opts.onSwitch === 'function' ? opts.onSwitch : null;
    var ensureMap = typeof opts.ensureMap === 'function' ? opts.ensureMap : null;

    function panelKey(attr) {
      return attr === 'dest' ? destKey : rulesKey;
    }

    function switchTo(attr, skipSave) {
      var key = panelKey(attr);
      nav.querySelectorAll('.dash-subnav-link').forEach(function (btn) {
        btn.classList.toggle('active', btn.dataset.attribution === attr);
      });
      panels.forEach(function (panel) {
        var show = panel.dataset.attributionPanel === attr;
        panel.classList.toggle('active', show);
        panel.hidden = !show;
      });
      if (!skipSave) saveAttribution(attr);
      if (ensureMap) ensureMap(key, attr);
      if (onSwitch) onSwitch(key, attr);
      if (mapViews && mapViews[key] && mapViews[key].map) {
        window.setTimeout(function () {
          mapViews[key].map.invalidateSize(true);
        }, 60);
        window.setTimeout(function () {
          mapViews[key].map.invalidateSize(true);
        }, 320);
      }
    }

    nav.addEventListener('click', function (e) {
      var btn = e.target.closest('.dash-subnav-link[data-attribution]');
      if (!btn || btn.classList.contains('active')) return;
      switchTo(btn.dataset.attribution);
    });

    switchTo(readAttribution('rules'), true);
  }

  markActiveNav();
  bindNavLinks();
  seedHostAttributionUrl();

  window.DashNav = {
    initAttributionTabs: initAttributionTabs,
    applyStoredAttributionPanels: applyStoredAttributionPanels,
    readAttribution: readAttribution,
    readAttributionFromUrl: readAttributionFromUrl,
    saveAttribution: saveAttribution,
    appendPreservedQuery: appendPreservedQuery,
    navigateWithTransition: navigateWithTransition,
    markActiveNav: markActiveNav,
  };
})();
