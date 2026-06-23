/** OD SPA host: preload zone / buildings / flows in iframes; instant tab switching. */
(function () {
  var PAGE_HINTS = {
    'od-zones': 'Rules-based zone map',
    'od-buildings': 'Click a zone to explore its buildings',
    'od-flows': 'Rules map · incoming flows',
  };

  var VIEW_BY_PAGE = {
    'od-zones': 'zones',
    'od-buildings': 'buildings',
    'od-flows': 'flows',
  };

  var PAGE_BY_VIEW = {
    zones: 'od-zones',
    buildings: 'od-buildings',
    flows: 'od-flows',
  };

  var FRAME_PATHS = {
    'od-zones': '/od.html',
    'od-buildings': '/od-buildings.html',
    'od-flows': '/od-flows.html',
  };

  var frames = {};
  var ready = {};
  var dataReady = {};
  var activePage = 'od-zones';
  var FRAME_CACHE_BUST = '20260617-stable-topbar';

  function parentAttributionParam() {
    try {
      var parentParams = new URLSearchParams(window.location.search);
      var attr = (parentParams.get('attribution') || parentParams.get('zone_by') || '').trim().toLowerCase();
      return attr === 'dest' ? 'dest' : '';
    } catch (_) {
      return '';
    }
  }

  function apiSuffix() {
    try {
      var api = (new URLSearchParams(window.location.search).get('od-dashboard-api') || '').trim();
      return api ? ('&api=' + encodeURIComponent(api.replace(/\/$/, ''))) : '';
    } catch (_) {
      return '';
    }
  }

  function qsViewPage() {
    try {
      var v = (new URLSearchParams(window.location.search).get('view') || '').trim().toLowerCase();
      return PAGE_BY_VIEW[v] || 'od-zones';
    } catch (_) {
      return 'od-zones';
    }
  }

  function parentAttributionZoneBy() {
    return parentAttributionParam() === 'dest' ? 'dest' : 'rules';
  }

  function flowsDestFromParentUrl() {
    try {
      return (new URLSearchParams(window.location.search).get('dest_geo_id') || '').trim();
    } catch (_) {
      return '';
    }
  }

  function notifyFlowsFrameDest(detail) {
    var frame = frames['od-flows'];
    if (!frame || !frame.contentWindow) return;
    var dest = detail && detail.dest_geo_id ? String(detail.dest_geo_id) : '';
    var zb = detail && detail.zone_by === 'dest' ? 'dest' : 'rules';
    try {
      frame.contentWindow.postMessage({
        type: 'dash-select-dest',
        dest_geo_id: dest,
        zone_by: zb,
      }, '*');
    } catch (_) { /* empty */ }
  }

  function openFlowsWithDest(detail) {
    var dest = detail && detail.dest_geo_id ? String(detail.dest_geo_id) : '';
    var zb = detail && detail.zone_by === 'dest' ? 'dest' : 'rules';
    syncAttributionFromMessage(zb);
    try {
      var params = new URLSearchParams(window.location.search);
      params.set('view', 'flows');
      if (dest) params.set('dest_geo_id', dest);
      else params.delete('dest_geo_id');
      if (zb === 'dest') params.set('attribution', 'dest');
      else params.delete('attribution');
      var url = '/' + (params.toString() ? '?' + params.toString() : '');
      if (window.history && window.history.pushState) {
        window.history.pushState({ dashPage: 'od-flows' }, '', url);
      }
    } catch (_) { /* empty */ }
    showPage('od-flows', false);
    notifyFlowsFrameDest({ dest_geo_id: dest, zone_by: zb });
    window.setTimeout(function () {
      notifyFlowsFrameDest({ dest_geo_id: dest, zone_by: zb });
    }, 200);
    window.setTimeout(function () {
      notifyFlowsFrameDest({ dest_geo_id: dest, zone_by: zb });
    }, 800);
  }

  function frameUrl(page) {
    var base = FRAME_PATHS[page] || '/od.html';
    var params = new URLSearchParams();
    params.set('embed', '1');
    params.set('v', FRAME_CACHE_BUST);
    try {
      var parentParams = new URLSearchParams(window.location.search);
      var api = (parentParams.get('od-dashboard-api') || '').trim();
      if (api) params.set('od-dashboard-api', api.replace(/\/$/, ''));
      var attr = parentAttributionParam();
      if (attr === 'dest') params.set('attribution', 'dest');
    } catch (_) { /* empty */ }
    return base + '?' + params.toString();
  }

  function setActiveNav(page) {
    document.body.dataset.dashPage = 'host';
    if (window.DashNav && typeof DashNav.markActiveNav === 'function') {
      DashNav.markActiveNav(page);
    }
  }

  function setLiveHint(text, page) {
    var el = document.getElementById('liveHint');
    if (!el) return;
    el.textContent = text || PAGE_HINTS[page || activePage] || '';
  }

  function readHintFromFrame(frame) {
    if (!frame || !frame.contentDocument) return '';
    var doc = frame.contentDocument;
    var live = doc.getElementById('liveHint');
    if (live && live.textContent) return live.textContent.trim();
    var hint = doc.getElementById('hint');
    if (hint && hint.textContent) return hint.textContent.trim();
    return '';
  }

  function notifyFrameShow(page) {
    var frame = frames[page];
    if (!frame || !frame.contentWindow) return;
    try {
      frame.contentWindow.postMessage({ type: 'dash-show' }, '*');
    } catch (_) { /* empty */ }
    window.setTimeout(function () {
      try { frame.contentWindow.postMessage({ type: 'dash-show' }, '*'); } catch (_2) { /* empty */ }
    }, 120);
    var hint = readHintFromFrame(frame);
    setLiveHint(hint || PAGE_HINTS[page], page);
  }

  function preloadAllFrames() {
    Object.keys(frames).forEach(function (page) {
      if (!frames[page].src) {
        frames[page].src = frameUrl(page);
      }
    });
  }

  function showPage(page, push) {
    if (!frames[page]) return;
    Object.keys(frames).forEach(function (key) {
      frames[key].classList.toggle('active', key === page);
    });
    if (!frames[page].src) {
      frames[page].src = frameUrl(page);
    }
    activePage = page;
    setActiveNav(page);
    if (window.DashHostOd && typeof DashHostOd.setActiveView === 'function') {
      DashHostOd.setActiveView(page);
    }
    if (push !== false) {
      var params = new URLSearchParams(window.location.search);
      var view = VIEW_BY_PAGE[page] || 'zones';
      params.set('view', view);
      var q = params.toString();
      var url = '/' + (q ? '?' + q : '');
      if (window.history && window.history.pushState) {
        window.history.pushState({ dashPage: page }, '', url);
      }
    }
    notifyFrameShow(page);
    if (page === 'od-flows') {
      var dest = flowsDestFromParentUrl();
      if (dest) {
        notifyFlowsFrameDest({
          dest_geo_id: dest,
          zone_by: parentAttributionZoneBy(),
        });
      }
    }
  }

  function onFrameReady(page) {
    ready[page] = true;
    if (page === activePage) notifyFrameShow(page);
  }

  function onFrameDataReady(page) {
    dataReady[page] = true;
  }

  function waitForFrame(page, timeoutMs) {
    return new Promise(function (resolve) {
      if (ready[page]) {
        resolve();
        return;
      }
      var done = false;
      function finish() {
        if (done) return;
        done = true;
        window.removeEventListener('message', onMsg);
        resolve();
      }
      var onMsg = function (e) {
        if (e.data && e.data.type === 'dash-ready' && e.data.page === page) {
          finish();
        }
      };
      window.addEventListener('message', onMsg);
      window.setTimeout(finish, timeoutMs == null ? 45000 : timeoutMs);
    });
  }

  function waitForFrameData(page, timeoutMs) {
    return new Promise(function (resolve) {
      if (dataReady[page]) {
        resolve();
        return;
      }
      var done = false;
      function finish() {
        if (done) return;
        done = true;
        window.removeEventListener('message', onMsg);
        resolve();
      }
      var onMsg = function (e) {
        if (e.data && e.data.type === 'dash-data-ready' && e.data.page === page) {
          finish();
        }
      };
      window.addEventListener('message', onMsg);
      window.setTimeout(finish, timeoutMs == null ? 180000 : timeoutMs);
    });
  }

  function runHostLoadingSequence() {
    if (!window.PageLoading) return Promise.resolve();
    PageLoading.init();
    PageLoading.show();
    setLiveHint('Loading dashboard…', activePage);
    if (window.DashHostOd && typeof DashHostOd.setKpiLoading === 'function') {
      DashHostOd.setKpiLoading(true);
    }
    preloadAllFrames();
    var sidebarP = window.DashHostOd ? DashHostOd.loadHostSidebar() : Promise.resolve();
    var activeDataP = waitForFrameData(activePage, 120000);
    var activeFrameP = waitForFrame(activePage, 45000);
    var allDataP = Promise.all(Object.keys(frames).map(function (p) {
      return waitForFrameData(p, 180000);
    }));
    var dismissP = Promise.race([
      Promise.all([sidebarP, activeFrameP, activeDataP]),
      new Promise(function (resolve) { window.setTimeout(resolve, 50000); }),
    ]);
    return dismissP
      .then(function () { return PageLoading.finish(350); })
      .catch(function (err) {
        console.error('od host loading:', err);
        PageLoading.hideNow();
      })
      .finally(function () {
        if (window.DashHostOd) DashHostOd.setKpiLoading(false);
        setLiveHint(PAGE_HINTS[activePage], activePage);
        allDataP.catch(function () { /* background preload */ });
      });
  }

  function syncAttributionFromMessage(attr) {
    var normalized = attr === 'dest' ? 'dest' : 'rules';
    try {
      if (window.DashNav && typeof DashNav.saveAttribution === 'function') {
        DashNav.saveAttribution(normalized);
        return;
      }
      var params = new URLSearchParams(window.location.search);
      if (normalized === 'dest') params.set('attribution', 'dest');
      else params.delete('attribution');
      var q = params.toString();
      var url = '/' + (q ? '?' + q : '');
      if (window.history && window.history.replaceState) {
        window.history.replaceState(window.history.state, '', url);
      }
      try {
        sessionStorage.setItem('dashAttribution', normalized);
      } catch (_) { /* empty */ }
    } catch (_) { /* empty */ }
  }

  function bindMessages() {
    window.addEventListener('message', function (e) {
      if (!e.data || typeof e.data !== 'object') return;
      if (e.data.type === 'dash-ready' && e.data.page) onFrameReady(e.data.page);
      if (e.data.type === 'dash-data-ready' && e.data.page) onFrameDataReady(e.data.page);
      if (e.data.type === 'dash-hint' && e.data.page === activePage && e.data.text) {
        setLiveHint(e.data.text, e.data.page);
      }
      if (e.data.type === 'dash-attribution') {
        syncAttributionFromMessage(e.data.attribution);
      }
      if (e.data.type === 'dash-open-flows') {
        openFlowsWithDest(e.data);
      }
    });
  }

  function bindHistory() {
    window.addEventListener('popstate', function () {
      showPage(qsViewPage(), false);
    });
  }

  function bindResize() {
    var resizeDebounce;
    window.addEventListener('resize', function () {
      clearTimeout(resizeDebounce);
      resizeDebounce = setTimeout(function () {
        Object.keys(frames).forEach(function (page) {
          notifyFrameShow(page);
        });
        if (window.DashHostOd && typeof DashHostOd.resizeCharts === 'function') {
          DashHostOd.resizeCharts();
        }
      }, 160);
    });
  }

  function init() {
    document.querySelectorAll('.dash-frame').forEach(function (frame) {
      var page = frame.dataset.page;
      if (page) frames[page] = frame;
    });
    bindMessages();
    bindHistory();
    bindResize();
    var initial = qsViewPage();
    activePage = initial;
    Object.keys(frames).forEach(function (key) {
      frames[key].classList.toggle('active', key === initial);
    });
    setActiveNav(initial);
    if (window.DashHostOd && typeof DashHostOd.setActiveView === 'function') {
      DashHostOd.setActiveView(initial);
    }
    preloadAllFrames();
    if (initial === 'od-flows') {
      var initialDest = flowsDestFromParentUrl();
      if (initialDest) {
        window.setTimeout(function () {
          notifyFlowsFrameDest({
            dest_geo_id: initialDest,
            zone_by: parentAttributionZoneBy(),
          });
        }, 600);
        window.setTimeout(function () {
          notifyFlowsFrameDest({
            dest_geo_id: initialDest,
            zone_by: parentAttributionZoneBy(),
          });
        }, 2200);
      }
    }
    runHostLoadingSequence();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.DashSpaOd = { showPage: showPage };
  window.DashSpa = window.DashSpaOd;
})();
