/** Deploy config + URL helpers (defaults; runtime values from server /assets/dashboard-config.js). */
(function (global) {
  function normPrefix(p) {
    p = String(p == null ? '' : p).trim().replace(/\/+$/, '');
    if (p && p.charAt(0) !== '/') p = '/' + p;
    return p;
  }

  function apiOverrideFromQuery() {
    try {
      return (new URLSearchParams(global.location.search).get('api') || '').trim().replace(/\/+$/, '');
    } catch (_) {
      return '';
    }
  }

  var state = {
    urlPrefix: '',
    apiPrefix: '/api',
    apiBase: '',
    showBoundaryButton: true,
  };

  function apiBase() {
    var override = apiOverrideFromQuery();
    if (override) return override;
    if (state.apiBase) return normPrefix(state.apiBase);
    return (state.urlPrefix || '') + state.apiPrefix;
  }

  function dashUrl(path) {
    path = String(path == null ? '' : path);
    if (!path || path === '/') return (state.urlPrefix || '') + '/';
    if (path.charAt(0) === '?') return (state.urlPrefix || '') + '/' + path;
    if (path.charAt(0) !== '/') path = '/' + path;
    return (state.urlPrefix || '') + path;
  }

  function apiUrl(path) {
    path = String(path == null ? '' : path);
    if (path.indexOf('/api/') === 0) path = path.slice(4);
    else if (path === '/api') path = '';
    if (path && path.charAt(0) !== '/') path = '/' + path;
    return apiBase() + path;
  }

  function homeUrl() {
    return dashUrl('/');
  }

  function spaHistoryUrl(params) {
    var q = params && typeof params.toString === 'function' ? params.toString() : '';
    return (state.urlPrefix || '') + '/' + (q ? '?' + q : '');
  }

  function showBoundaryButton() {
    return state.showBoundaryButton !== false;
  }

  function applyBoundaryNav() {
    var hide = !showBoundaryButton();
    if (document.body) {
      if (hide) document.body.setAttribute('data-hide-boundary-nav', 'true');
      else document.body.removeAttribute('data-hide-boundary-nav');
    }
    if (!hide) return;
    document.querySelectorAll(
      '.dash-nav-link[data-page="od-boundaries"], .dash-nav-link[data-page="boundaries"], .dash-nav-boundaries'
    ).forEach(function (a) {
      a.style.display = 'none';
      a.setAttribute('aria-hidden', 'true');
      a.tabIndex = -1;
    });
  }

  function scheduleBoundaryNav() {
    if (showBoundaryButton()) return;
    function run() {
      applyBoundaryNav();
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', run);
    } else {
      run();
    }
  }

  function applyDeploy(cfg) {
    cfg = cfg || {};
    state.urlPrefix = normPrefix(cfg.urlPrefix);
    state.apiPrefix = normPrefix(cfg.apiPrefix || '/api') || '/api';
    state.apiBase = cfg.apiBase ? normPrefix(cfg.apiBase) : '';
    state.showBoundaryButton = cfg.showBoundaryButton !== false;
    global.DashConfig = {
      urlPrefix: state.urlPrefix,
      apiPrefix: state.apiPrefix,
      dashUrl: dashUrl,
      apiUrl: apiUrl,
      apiBase: apiBase,
      homeUrl: homeUrl,
      spaHistoryUrl: spaHistoryUrl,
      showBoundaryButton: showBoundaryButton,
      applyBoundaryNav: applyBoundaryNav,
    };
    scheduleBoundaryNav();
  }

  global.DashApplyDeploy = applyDeploy;
  applyDeploy(global.DashConfig || global.__dashDeploy || {});
})(typeof window !== 'undefined' ? window : globalThis);
