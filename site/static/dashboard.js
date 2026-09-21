/* dashboard.js — draws the charts on company pages.

   Progressive enhancement only: every charted number is already in a server-rendered table, so
   a failure here degrades to a two-word status and never to a console exception.

   What this script expects of the page (scripts/build_site.py and site/templates/company.html
   provide it):
   - an element with [data-src] points at that company's JSON, relative to the page;
   - inside it, [data-charts="reported"] is the chart host for the filings, and
     [data-charts="outputs"] the one for the model, present only when a built model has
     outputs over more than one period (a single-period snapshot is a table, not a chart);
   - an optional <script type="application/json" data-company> holds the same JSON inline, used
     when fetch cannot run (file:// preview) or the data file is missing.
   Pages without a [data-src] element (index, writeups) do nothing.
   Chart.js 4 is loaded from cdnjs in base.html on company pages only. */
(function () {
  'use strict';

  // Which reported series to chart and how. Cash is a balance (a level), the rest are flows.
  var REPORTED = [
    { key: 'revenue', label: 'Revenue', kind: 'bar' },
    { key: 'capex', label: 'Capex', kind: 'bar' },
    { key: 'cfo', label: 'Cash from operations', kind: 'bar' },
    { key: 'cash', label: 'Cash', kind: 'line' }
  ];

  var FALLBACK_NOTE = 'Charts unavailable';

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function note(host, text) {
    if (!host) { return; }
    var p = document.createElement('p');
    p.className = 'note';
    p.textContent = text;
    host.appendChild(p);
  }

  // ---- number formatting -----------------------------------------------------------------
  //
  // A rule-for-rule port of fmt_value(value, unit, compact=True) in scripts/build_site.py, so a
  // tooltip or axis tick reads exactly like the table cell under the chart. The vectors below
  // (value | unit | display) are repeated verbatim in build_site.py; tests/test_build_site.py
  // pins the Python side to them and fails when the two copies drift apart. When a rule changes
  // there, change it here and update both tables.
  //
  //   fmt: 1.15e9 | USD | $1.1bn
  //   fmt: 1.25e9 | USD | $1.2bn
  //   fmt: 982000000 | USD | $982m
  //   fmt: 9.95e6 | USD | $9.9m
  //   fmt: 32000 | USD | $32k
  //   fmt: -100000000 | USD | -$100m
  //   fmt: 1500 | USD m | $1.5bn
  //   fmt: 123.456 | USD | $123
  //   fmt: 100.5 | USD | $100
  //   fmt: 12.5 | USD | $12.50
  //   fmt: 2.75 | USD | $2.75
  //   fmt: 1.5772 | USD | $1.58
  //   fmt: 0.2079 | USD | $0.21
  //   fmt: 0.08 | USD/kWh | $0.08
  //   fmt: 0.5 | USD/M tokens | $0.50
  //   fmt: 0.55 | % | 55.0%
  //   fmt: 0.6 | share | 60.0%
  //   fmt: 3.5 | x | 3.50x
  //   fmt: 0.125 | x | 0.12x
  //   fmt: 240000000 | shares | 240m
  //   fmt: 2500000 | tokens/s | 2.5m
  //   fmt: 2500 | tokens/s | 2,500
  //   fmt: 1250 | GPUs | 1,250
  //   fmt: -1250 | GPUs | -1,250
  //   fmt: 18.99485 | months | 18.99

  var MILLIONS_UNITS = ['usd m', 'usdm', 'usd mn', 'usd million', 'usd millions'];
  var FRACTION_UNITS = ['%', 'share', 'decimal', 'percent'];

  // Python's f"{x:.{decimals}f}" (and "," grouping when asked). toFixed rounds the exact binary
  // value just as Python does, except on an exact tie (1.25 to one decimal), where toFixed goes
  // up and Python goes to the even digit: "$1.2bn" in the table must not be "$1.3bn" here.
  // A double is such a tie only when x * 2^(decimals + 1) is an odd integer.
  function pyFixed(x, decimals, group) {
    var sign = x < 0 ? '-' : '';
    var magnitude = Math.abs(x);
    var halves = magnitude * Math.pow(2, decimals + 1);
    if (Math.floor(halves) === halves && halves % 2 === 1) {
      var scale = Math.pow(10, decimals);
      var lower = Math.floor(magnitude * scale);
      magnitude = (lower % 2 === 0 ? lower : lower + 1) / scale;
    }
    var text = magnitude.toFixed(decimals);
    if (group) {
      var parts = text.split('.');
      parts[0] = parts[0].replace(/\B(?=(\d{3})+$)/g, ',');
      text = parts.join('.');
    }
    return sign + text;
  }

  function isWhole(n) { return Math.floor(n) === n; }

  // _abbreviate: '1.2bn', '982m', '32k'; billions always keep one decimal, the others only
  // below ten units; under a thousand it is a plain number with cents unless whole or >= 100.
  function abbreviate(magnitude) {
    var steps = [[1e9, 'bn'], [1e6, 'm'], [1e3, 'k']];
    for (var i = 0; i < steps.length; i += 1) {
      if (magnitude >= steps[i][0]) {
        var scaled = magnitude / steps[i][0];
        var decimals = (steps[i][1] === 'bn' || scaled < 10) ? 1 : 0;
        return pyFixed(scaled, decimals, false) + steps[i][1];
      }
    }
    return pyFixed(magnitude, (magnitude >= 100 || isWhole(magnitude)) ? 0 : 2, true);
  }

  function formatValue(value, unit) {
    if (value === null || value === undefined || isNaN(value)) { return '—'; }
    var u = (unit || '').trim().toLowerCase();
    var v = Number(value);
    var sign = v < 0 ? '-' : '';
    if (u.indexOf('usd') === 0) {
      if (MILLIONS_UNITS.indexOf(u) !== -1) { v = v * 1e6; }
      return sign + '$' + abbreviate(Math.abs(v));
    }
    if (FRACTION_UNITS.indexOf(u) !== -1) { return pyFixed(v * 100, 1, false) + '%'; }
    if (u === 'x') { return pyFixed(v, 2, false) + 'x'; }
    if (u === 'shares' || Math.abs(v) >= 1e6) { return sign + abbreviate(Math.abs(v)); }
    return pyFixed(v, isWhole(v) ? 0 : 2, true);
  }

  // ---- data loading -----------------------------------------------------------------------

  function readInline(root) {
    var script = root.querySelector('script[type="application/json"]');
    if (!script || !script.textContent.trim()) { return null; }
    try {
      return JSON.parse(script.textContent);
    } catch (err) {
      return null;
    }
  }

  function loadData(root) {
    var src = root.getAttribute('data-src');
    var inline = readInline(root);
    // fetch() rejects file:// URLs in most browsers, so a local preview uses the inline copy.
    if (!src || typeof fetch !== 'function' || window.location.protocol === 'file:') {
      return Promise.resolve(inline);
    }
    return fetch(src, { cache: 'no-cache' })
      .then(function (response) {
        if (!response.ok) { throw new Error('HTTP ' + response.status); }
        return response.json();
      })
      .catch(function () { return inline; });
  }

  function seriesPoints(series) {
    var out = { labels: [], values: [] };
    var raw = (series && series.points) || [];
    for (var i = 0; i < raw.length; i += 1) {
      var p = raw[i];
      if (Array.isArray(p) && p.length >= 2) {
        out.labels.push(String(p[0]));
        out.values.push(p[1] === null ? null : Number(p[1]));
      }
    }
    return out;
  }

  // ---- drawing ----------------------------------------------------------------------------

  function draw(host, title, unit, series, kind, palette) {
    var pts = seriesPoints(series);
    if (!pts.values.length) { return; }

    var fig = document.createElement('figure');
    fig.className = 'chart';
    var cap = document.createElement('figcaption');
    cap.textContent = unit ? title + ' (' + unit + ')' : title;
    var box = document.createElement('div');
    box.className = 'chart-box';
    var canvas = document.createElement('canvas');
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', title + ' by period, chart');
    box.appendChild(canvas);
    fig.appendChild(cap);
    fig.appendChild(box);
    host.appendChild(fig);

    // Estimate periods ("2026E") are drawn lighter than actuals so fact and model read apart.
    var fills = pts.labels.map(function (label) {
      return /E$/.test(label) ? palette.accentSoft : palette.accent;
    });

    new Chart(canvas, {
      type: kind,
      data: {
        labels: pts.labels,
        datasets: [{
          label: title,
          data: pts.values,
          backgroundColor: kind === 'line' ? palette.accentSoft : fills,
          borderColor: palette.accent,
          borderWidth: kind === 'line' ? 2 : 0,
          pointRadius: 3,
          tension: 0.2,
          fill: kind === 'line',
          spanGaps: true
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (ctx) { return formatValue(ctx.parsed.y, unit); }
            }
          }
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: palette.muted, maxRotation: 0, autoSkip: true }
          },
          y: {
            grid: { color: palette.grid },
            ticks: {
              color: palette.muted,
              callback: function (v) { return formatValue(v, unit); }
            }
          }
        }
      }
    });
  }

  function render(root, data) {
    var reportedHost = root.querySelector('[data-charts="reported"]');
    var outputsHost = root.querySelector('[data-charts="outputs"]');
    // Without data the server-rendered page already says "No data"; a second note would repeat it.
    if (!data) { return; }
    if (typeof Chart === 'undefined') {
      note(reportedHost || root, FALLBACK_NOTE);
      return;
    }

    var palette = {
      accent: cssVar('--accent', '#2c5d8a'),
      accentSoft: cssVar('--accent-soft', 'rgba(44, 93, 138, 0.3)'),
      muted: cssVar('--muted', '#666666'),
      grid: cssVar('--grid', '#eeeeee')
    };
    if (Chart.defaults && Chart.defaults.font) {
      Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
      Chart.defaults.color = palette.muted;
    }

    var reported = data.reported || {};
    if (reportedHost) {
      REPORTED.forEach(function (s) {
        var series = reported[s.key];
        if (series) { draw(reportedHost, s.label, series.unit, series, s.kind, palette); }
      });
    }

    var outputs = data.outputs || {};
    if (outputsHost) {
      Object.keys(outputs).forEach(function (key) {
        var series = outputs[key] || {};
        draw(outputsHost, series.label || key, series.unit, series, 'bar', palette);
      });
    }
  }

  ready(function () {
    var roots = document.querySelectorAll('[data-src]');
    if (!roots.length) { return; } // index and writeup pages: nothing to draw
    Array.prototype.forEach.call(roots, function (root) {
      loadData(root)
        .then(function (data) {
          try {
            render(root, data);
          } catch (err) {
            note(root, FALLBACK_NOTE);
          }
        })
        .catch(function () { note(root, FALLBACK_NOTE); });
    });
  });
})();
