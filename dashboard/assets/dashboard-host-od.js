/** Sidebar KPIs + charts for the OD SPA host (loaded once). */
(function () {
  var CATEGORY_COLORS = ['#4ade80', '#5eead4', '#38bdf8', '#fbbf24', '#fb7185', '#a78bfa'];
  var CHART_ANIM = { duration: 280, easing: 'easeOutQuart' };
  var chartEmissions, chartTrips, chartDonut;
  var cachedStats = null;
  var cachedByCategory = null;
  var activeView = 'zones';
  var zoneSidebarReq = 0;
  var selectedZoneByCategory = null;
  var selectedZoneLabel = '';

  function apiBase() {
    try {
      var u = (new URLSearchParams(window.location.search).get('api') || '').trim();
      if (u) return u.replace(/\/$/, '');
    } catch (_) { /* empty */ }
    if (window.DashConfig && typeof DashConfig.apiBase === 'function') {
      return DashConfig.apiBase();
    }
    if (window.location.protocol === 'file:') return 'http://127.0.0.1:5051';
    return '';
  }

  function fetchJson(path, timeoutMs) {
    var ms = timeoutMs == null ? 25000 : timeoutMs;
    var url = (window.DashConfig && typeof DashConfig.apiUrl === 'function')
      ? DashConfig.apiUrl(path)
      : ((apiBase() || '') + path);
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = ctrl ? window.setTimeout(function () { ctrl.abort(); }, ms) : null;
    return fetch(url, { cache: 'no-store', signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) {
        return r.text().then(function (text) {
          if (!r.ok) throw new Error('HTTP ' + r.status + ': ' + (text || r.statusText));
          return text ? JSON.parse(text) : {};
        });
      })
      .finally(function () {
        if (timer) window.clearTimeout(timer);
      });
  }

  function formatNum(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return n.toLocaleString();
  }

  function formatTripsNum(n) {
    if (window.DashZoneUi && DashZoneUi.formatTrips) return DashZoneUi.formatTrips(n);
    return String(Math.round(Number(n) || 0));
  }

  function shortChartLabel(s, maxLen) {
    if (s == null || s === '') return '';
    var t = String(s).trim().replace(/_/g, ' ');
    if (t.length <= maxLen) return t;
    return t.slice(0, Math.max(1, maxLen - 1)) + '…';
  }

  function sidebarWidth() {
    var sb = document.querySelector('.dash-spa-host .sidebar');
    return sb ? sb.offsetWidth : window.innerWidth;
  }

  function compactCharts() {
    return sidebarWidth() < 400;
  }

  function setCardHeight(canvasId, px) {
    var el = document.getElementById(canvasId);
    var card = el && el.closest ? el.closest('.card') : null;
    if (card) {
      var h = Math.round(px);
      card.style.height = h + 'px';
      card.style.setProperty('--card-h', h + 'px');
    }
  }

  function totalEmissionsKg(statsOrG) {
    if (statsOrG && typeof statsOrG === 'object') {
      var g = Number(statsOrG.total_emissions_g_weighted != null
        ? statsOrG.total_emissions_g_weighted
        : statsOrG.total_emissions_g);
      if (Number.isFinite(g)) return Math.round(g / 1000);
      return null;
    }
    return Math.round((Number(statsOrG) || 0) / 1000);
  }

  function emissionsGramsFromRow(rowOrG) {
    if (rowOrG && typeof rowOrG === 'object') {
      var g = Number(rowOrG.total_emissions_g_weighted != null
        ? rowOrG.total_emissions_g_weighted
        : rowOrG.total_emissions_g);
      return Number.isFinite(g) ? g : 0;
    }
    var n = Number(rowOrG);
    return Number.isFinite(n) ? n : 0;
  }

  function emissionsChartValue(rowOrG) {
    return emissionsGramsFromRow(rowOrG) / 1000;
  }

  function formatEmissionsChart(v) {
    var n = Number(v);
    if (!Number.isFinite(n) || n <= 0) return '0 kg CO₂e';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M kg CO₂e';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k kg CO₂e';
    return Math.round(n).toLocaleString() + ' kg CO₂e';
  }

  function emissionsDatasetLabel() {
    return 'kg CO₂e';
  }

  function formatTripsChart(n) {
    var v = Number(n);
    if (!Number.isFinite(v) || v < 0) return '0';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1000) return (v / 1000).toFixed(1) + 'k';
    return Math.round(v).toLocaleString();
  }

  function donutTooltipLabel(ctx) {
    var label = ctx.label || '';
    var t = Number(ctx.parsed) || 0;
    var arr = ctx.chart.data.datasets[0].data;
    var total = arr.reduce(function (a, b) { return a + b; }, 0);
    var pct = total ? ((t / total) * 100).toFixed(1) : '0';
    return label + ': ' + formatEmissionsChart(t) + ' (' + pct + '%)';
  }

  function avgEmissionsKgPerTrip(avgG) {
    var g = Number(avgG);
    return Number.isFinite(g) ? Math.round(g / 1000) : null;
  }

  function legStatsFrom(stats) {
    var legs = Number(stats.trips_legs);
    if (!Number.isFinite(legs) || legs <= 0) {
      var funnel = stats.survey_funnel;
      if (funnel) legs = Number(funnel.island_touch_legs) || 0;
    }
    var expanded = Number(stats.trips_weighted != null ? stats.trips_weighted : stats.trips) || 0;
    var emisLegs = Number(stats.total_emissions_g_legs);
    if (!Number.isFinite(emisLegs)) emisLegs = Number(stats.total_emissions_g) || 0;
    var kmLegs = Number(stats.total_distance_km_legs);
    if (!Number.isFinite(kmLegs)) kmLegs = Number(stats.total_distance_km) || 0;
    return { legs: legs, expanded: expanded, emisLegs: emisLegs, kmLegs: kmLegs };
  }

  function applyKpiStats(stats, view) {
    if (!stats) return;
    var legsInfo = legStatsFrom(stats);
    var expanded = legsInfo.expanded || Number(stats.trips) || 0;
    var kg = totalEmissionsKg(stats);
    var km = Number(stats.total_distance_km_weighted != null
      ? stats.total_distance_km_weighted
      : stats.total_distance_km);
    var avgKg = avgEmissionsKgPerTrip(stats.avg_emissions_g_per_trip);
    var labelEl = document.getElementById('kpi-trips-label');
    var el = document.getElementById('kpi-trips');
    var subEl = document.getElementById('kpi-trips-sub');

    if (labelEl) labelEl.textContent = stats.kpi_scope === 'zone' ? 'Car trips · zone' : 'Car trips';
    if (el) el.textContent = formatTripsNum(expanded);
    if (subEl) {
      if (stats.zone_label) {
        subEl.textContent = stats.zone_label;
      } else if (stats.kpi_scope === 'island_eligible') {
        subEl.textContent = 'CMM residents · island-touch';
      } else if (stats.kpi_scope === 'zone') {
        subEl.textContent = 'Selected zone';
      } else {
        subEl.textContent = '';
      }
    }

    el = document.getElementById('kpi-co2');
    if (el) el.textContent = kg != null ? formatNum(kg) : '—';
    el = document.getElementById('kpi-km');
    if (el) el.textContent = formatNum(km);
    el = document.getElementById('kpi-avg');
    if (el) el.textContent = avgKg != null ? formatNum(avgKg) : '—';
  }

  function scaleCategoriesForZone(islandCats, zoneStats) {
    if (!islandCats || !islandCats.length || !zoneStats) return [];
    var zEm = Number(zoneStats.total_emissions_g) || 0;
    var zTrips = Number(zoneStats.trips) || 0;
    var zKm = Number(zoneStats.total_distance_km) || 0;
    var islandEm = islandCats.reduce(function (a, c) { return a + (Number(c.total_emissions_g) || 0); }, 0);
    var islandTrips = islandCats.reduce(function (a, c) { return a + (Number(c.trips) || 0); }, 0);
    var islandKm = islandCats.reduce(function (a, c) { return a + (Number(c.total_distance_km) || 0); }, 0);
    if (islandEm <= 0 || zEm <= 0) return [];
    var emisRatio = zEm / islandEm;
    var tripsRatio = islandTrips > 0 ? (zTrips / islandTrips) : emisRatio;
    var kmRatio = islandKm > 0 ? (zKm / islandKm) : emisRatio;
    return islandCats.map(function (c) {
      return {
        category: c.category,
        trips: (Number(c.trips) || 0) * tripsRatio,
        total_emissions_g: (Number(c.total_emissions_g) || 0) * emisRatio,
        total_distance_km: (Number(c.total_distance_km) || 0) * kmRatio,
      };
    });
  }

  function updateChartTitles(zoneLabel) {
    var suffix = zoneLabel ? (' · ' + zoneLabel) : '';
    var titles = [
      { sel: '#chart-emissions', base: 'Emissions by travel reason (kg CO₂e)' },
      { sel: '#chart-donut', base: 'Emissions share' },
      { sel: '#chart-trips', base: 'Trips by travel reason' },
      { sel: '#chart-distance', base: 'Distance by travel reason (km)' },
    ];
    titles.forEach(function (item) {
      var canvas = document.querySelector(item.sel);
      var card = canvas && canvas.closest ? canvas.closest('.card') : null;
      var h = card && card.querySelector ? card.querySelector('h3') : null;
      if (h) h.textContent = item.base + suffix;
    });
  }

  function tripCountFromCategory(d) {
    if (d.trips_weighted != null && Number(d.trips_weighted) > 0) return d.trips_weighted;
    return d.trips;
  }

  function applySidebarCharts(byCategory, zoneStats, zoneLabel) {
    var cats = (byCategory && byCategory.length)
      ? byCategory
      : scaleCategoriesForZone(cachedByCategory, zoneStats);
    selectedZoneLabel = zoneLabel || '';
    if (cats && cats.length) {
      selectedZoneByCategory = cats;
      initCharts(cats);
      updateChartTitles(zoneLabel || '');
    }
  }

  function setActiveView(page) {
    var view = 'zones';
    if (page === 'od-buildings') view = 'buildings';
    else if (page === 'od-flows') view = 'flows';
    activeView = view;
    if (cachedStats) applyKpiStats(cachedStats, view);
  }

  function barChartOptions(compact, total, horizontal, chartKind) {
    var kind = chartKind || 'emissions';
    var valueFormatter = kind === 'trips' ? formatTripsChart : formatEmissionsChart;
    var valueAxis = {
      beginAtZero: true,
      ticks: {
        color: '#8b9cb8',
        font: { size: compact ? 9 : 10 },
        callback: valueFormatter,
      },
      grid: { color: 'rgba(148,163,184,0.08)' },
    };
    var categoryAxis = {
      ticks: {
        color: '#8b9cb8',
        font: { size: compact ? 9 : 10 },
        autoSkip: false,
        maxRotation: horizontal ? 0 : (compact ? 0 : 35),
        minRotation: 0,
      },
      grid: { display: horizontal, color: 'rgba(148,163,184,0.08)' },
    };
    return {
      indexAxis: horizontal ? 'y' : 'x',
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              var label = ctx.label || '';
              var raw = horizontal ? ctx.parsed.x : ctx.parsed.y;
              if (kind === 'trips') return label + ': ' + formatTripsChart(raw) + ' trips';
              return label + ': ' + formatEmissionsChart(raw);
            },
          },
        },
        datalabels: compact ? { display: false } : {
          anchor: horizontal ? 'end' : 'end',
          align: horizontal ? 'end' : 'end',
          color: '#e8eef7',
          font: { size: 9, weight: '500' },
          formatter: function (value) {
            if (kind === 'trips') return formatTripsChart(value);
            return !total ? '' : ((value / total) * 100).toFixed(1) + '%';
          },
        },
      },
      scales: horizontal
        ? { x: valueAxis, y: categoryAxis }
        : { x: categoryAxis, y: valueAxis },
      responsive: true,
      maintainAspectRatio: false,
      animation: CHART_ANIM,
    };
  }

  function initCharts(byCategory) {
    if (typeof Chart === 'undefined') return;
    if (!byCategory || !byCategory.length) return;
    var compact = compactCharts();
    var sorted = byCategory.slice().sort(function (a, b) {
      return (b.total_emissions_g || 0) - (a.total_emissions_g || 0);
    });
    var labelLen = compact ? 11 : 18;
    var labels = sorted.map(function (d) { return shortChartLabel(d.category, labelLen); });
    var emissionsData = sorted.map(function (d) { return emissionsChartValue(d); });
    var tripsData = sorted.map(function (d) { return tripCountFromCategory(d); });
    var totalEmissions = emissionsData.reduce(function (a, b) { return a + b; }, 0);
    var totalTrips = tripsData.reduce(function (a, b) { return a + b; }, 0);
    var bgColors = CATEGORY_COLORS.slice(0, labels.length);
    var horizontal = compact;
    var barCardH = horizontal
      ? Math.max(150, labels.length * 24 + 46)
      : 220;

    var ce = document.getElementById('chart-emissions');
    if (ce) {
      setCardHeight('chart-emissions', barCardH);
      if (chartEmissions) chartEmissions.destroy();
      chartEmissions = new Chart(ce, {
        type: 'bar',
        data: { labels: labels, datasets: [{ label: emissionsDatasetLabel(), data: emissionsData, backgroundColor: bgColors, borderRadius: 6 }] },
        options: barChartOptions(compact, totalEmissions, horizontal, 'emissions'),
        plugins: [ChartDataLabels],
      });
    }
    var ct = document.getElementById('chart-trips');
    if (ct) {
      setCardHeight('chart-trips', barCardH);
      if (chartTrips) chartTrips.destroy();
      chartTrips = new Chart(ct, {
        type: 'bar',
        data: { labels: labels, datasets: [{ label: 'Trips', data: tripsData, backgroundColor: bgColors, borderRadius: 6 }] },
        options: barChartOptions(compact, totalTrips, horizontal, 'trips'),
        plugins: [ChartDataLabels],
      });
    }
    var cd = document.getElementById('chart-donut');
    if (cd) {
      setCardHeight('chart-donut', compact ? 210 : 220);
      if (chartDonut) chartDonut.destroy();
      chartDonut = new Chart(cd, {
        type: 'doughnut',
        data: { labels: labels, datasets: [{ data: emissionsData, backgroundColor: bgColors, borderWidth: 0 }] },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: CHART_ANIM,
          cutout: compact ? '52%' : '58%',
          plugins: {
            legend: {
              position: compact ? 'bottom' : 'right',
              labels: {
                color: '#8b9cb8',
                boxWidth: 10,
                font: { size: compact ? 9 : 10 },
                padding: compact ? 8 : 12,
              },
            },
            tooltip: {
              callbacks: { label: donutTooltipLabel },
            },
            datalabels: compact ? { display: false } : {
              color: '#e8eef7',
              font: { size: 9 },
              formatter: function (value, ctx) {
                var arr = ctx.chart.data.datasets[0].data;
                var total = arr.reduce(function (a, b) { return a + b; }, 0);
                return !total ? '' : ((value / total) * 100).toFixed(1) + '%';
              },
            },
          },
        },
        plugins: [ChartDataLabels],
      });
    }
  }

  function setKpiLoading(loading) {
    if (!loading) return;
    ['kpi-trips', 'kpi-co2', 'kpi-km', 'kpi-avg'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.textContent = '…';
    });
  }

  function applyInstantZoneSidebar(stats, zoneLabel, geoId) {
    if (!stats) return;
    var statsNow = Object.assign({}, stats, {
      zone_label: zoneLabel || geoId || stats.geo_id || '',
      kpi_scope: 'zone',
    });
    applyKpiStats(statsNow, activeView);
    applySidebarCharts(null, statsNow, statsNow.zone_label);
  }

  function loadHostSidebar() {
    return fetchJson('/api/od/home', 120000).then(function (boot) {
      var stats = (boot && boot.stats_island_eligible)
        || (boot && (boot.stats_rules || boot.stats))
        || null;
      cachedStats = stats;
      cachedByCategory = (boot && boot.by_category) || null;
      if (stats) applyKpiStats(stats, activeView);
      if (cachedByCategory) initCharts(cachedByCategory);
    }).catch(function (err) {
      console.error('od host sidebar:', err);
    });
  }

  function clearZoneSidebar() {
    zoneSidebarReq += 1;
    selectedZoneByCategory = null;
    selectedZoneLabel = '';
    if (cachedStats) {
      var stats = Object.assign({}, cachedStats);
      delete stats.zone_label;
      applyKpiStats(stats, activeView);
    }
    if (cachedByCategory) {
      initCharts(cachedByCategory);
      updateChartTitles('');
    }
  }

  function loadZoneSidebar(geoId, zoneBy, instant) {
    var gid = String(geoId || '').trim();
    if (!gid) {
      clearZoneSidebar();
      return Promise.resolve();
    }
    var seq = ++zoneSidebarReq;
    var instantStats = instant && instant.stats;
    var instantLabel = (instant && instant.zone_label) || '';
    if (instantStats) {
      applyInstantZoneSidebar(instantStats, instantLabel, gid);
    } else {
      setKpiLoading(true);
    }
    var q = new URLSearchParams({
      geo_id: gid,
      zone_by: zoneBy === 'dest' ? 'dest' : 'rules',
    });
    return fetchJson('/api/od/zone_sidebar?' + q.toString(), 60000).then(function (data) {
      if (seq !== zoneSidebarReq) return;
      var stats = null;
      if (data && data.stats) {
        stats = Object.assign({}, data.stats, {
          zone_label: data.zone_label || data.geo_id || gid,
          kpi_scope: 'zone',
        });
        applyKpiStats(stats, activeView);
      }
      applySidebarCharts(
        data && data.by_category,
        stats || instantStats || (data && data.stats),
        (data && data.zone_label) || instantLabel || gid
      );
    }).catch(function (err) {
      if (seq !== zoneSidebarReq) return;
      console.warn('zone sidebar:', err);
      if (!instantStats && cachedStats) applyKpiStats(cachedStats, activeView);
    }).finally(function () {
      if (seq === zoneSidebarReq) setKpiLoading(false);
    });
  }

  function resizeCharts() {
    var cats = selectedZoneByCategory || cachedByCategory;
    if (cats) {
      initCharts(cats);
      return;
    }
    [chartEmissions, chartTrips, chartDonut].forEach(function (c) {
      if (c) c.resize();
    });
  }

  window.DashHostOd = {
    loadHostSidebar: loadHostSidebar,
    loadZoneSidebar: loadZoneSidebar,
    clearZoneSidebar: clearZoneSidebar,
    applyKpiStats: applyKpiStats,
    setKpiLoading: setKpiLoading,
    setActiveView: setActiveView,
    resizeCharts: resizeCharts,
  };
})();
