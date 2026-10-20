/* Mylar3 importer page (templates/import_mylar.html).
 *
 * Load -> preview -> apply -> poll. Every URL comes from data attributes
 * rendered with url_for, so the page works behind a subpath proxy. The
 * preview never carries a secret: the server reports only whether a key or
 * password is set.
 */
(function () {
  'use strict';

  var root = document.getElementById('mylar-import');
  if (!root) return;
  var urls = {
    load: root.dataset.loadUrl,
    preview: root.dataset.previewUrl,
    apply: root.dataset.applyUrl,
    status: root.dataset.statusUrl,
    drop: root.dataset.dropUrl
  };
  var esc = CLU.escapeHtml;
  var state = { importId: null, preview: null, selected: {} };

  var STATE_LABELS = {
    exists: ['Folder found', 'success'],
    will_create: ['Folder will be created', 'info'],
    already_mapped: ['Already on Pull List', 'secondary'],
    path_taken: ['Folder mapped to another series', 'warning'],
    duplicate: ['Duplicate folder', 'warning'],
    invalid: ['No usable folder', 'danger']
  };
  var IMPORTABLE = { exists: true, will_create: true };
  var SETTING_STATES = {
    'new': ['New', 'success'],
    same: ['Already set', 'secondary'],
    differs: ['Differs from CLU', 'warning']
  };

  function $(id) { return document.getElementById(id); }

  function badge(text, type) {
    return '<span class="badge text-bg-' + type + '">' + esc(text) + '</span>';
  }

  function postJson(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    });
  }

  function handleResult(res) {
    if (!res.ok || !res.data.success) {
      CLU.showError((res.data && res.data.error) || 'Request failed');
      return null;
    }
    return res.data;
  }

  // ── Step 1: load ──────────────────────────────────────────────────────

  function load(formData, button) {
    button.disabled = true;
    fetch(urls.load, { method: 'POST', body: formData })
      .then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, data: data }; });
      })
      .then(function (res) {
        var data = handleResult(res);
        if (data) showPreview(data, true);
      })
      .catch(function () { CLU.showError('Could not load the files'); })
      .finally(function () { button.disabled = false; });
  }

  $('mi-upload-form').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var db = $('mi-db').files[0];
    var ini = $('mi-ini').files[0];
    if (!db && !ini) {
      CLU.showError('Choose mylar.db and/or config.ini');
      return;
    }
    var fd = new FormData();
    if (db) fd.append('mylar_db', db);
    if (ini) fd.append('config_ini', ini);
    load(fd, $('mi-upload-btn'));
  });

  $('mi-drop-btn').addEventListener('click', function () {
    var fd = new FormData();
    fd.append('source', 'drop-folder');
    load(fd, $('mi-drop-btn'));
  });

  $('mi-drop-refresh').addEventListener('click', function () {
    fetch(urls.drop).then(function (r) { return r.json(); }).then(function (data) {
      $('mi-drop-status').textContent =
        'mylar.db ' + (data.mylar_db ? 'found' : 'not found') +
        ', config.ini ' + (data.config_ini ? 'found' : 'not found');
      $('mi-drop-btn').disabled = !(data.mylar_db || data.config_ini);
    }).catch(function () { CLU.showError('Could not check the folder'); });
  });

  // ── Step 2: preview ───────────────────────────────────────────────────

  function showPreview(data, fresh) {
    state.importId = data.import_id;
    state.preview = data;
    if (fresh) state.selected = {};
    data.series.forEach(function (row) {
      if (!IMPORTABLE[row.state]) {
        delete state.selected[row.cv_id];
      } else if (fresh || !(row.cv_id in state.selected)) {
        state.selected[row.cv_id] = true;
      }
    });
    $('mi-step-load').classList.add('d-none');
    $('mi-step-review').classList.remove('d-none');
    renderMapping();
    renderSeries();
    if (fresh) renderSettings();
    renderSkipped();
  }

  function renderMapping() {
    var p = state.preview;
    $('mi-mapping-card').classList.toggle('d-none', !p.has_db);
    $('mi-roots').innerHTML = (p.library_roots || []).map(function (r) {
      return '<option value="' + esc(r) + '"></option>';
    }).join('');
    $('mi-mapping-body').innerHTML = (p.mapping || []).map(function (m, i) {
      var found = (m.total === undefined) ? '' :
        (m.found + ' of ' + m.total + (m.found === 0 && m.total ? ' ' + badge('check this', 'warning') : ''));
      return '<tr>' +
        '<td><input type="text" class="form-control form-control-sm" data-map="mylar" data-i="' + i +
        '" value="' + esc(m.mylar_root || '') + '"></td>' +
        '<td><input type="text" class="form-control form-control-sm" list="mi-roots" data-map="clu" data-i="' + i +
        '" value="' + esc(m.clu_root || '') + '"></td>' +
        '<td class="small text-nowrap">' + found + '</td></tr>';
    }).join('');
  }

  $('mi-mapping-apply').addEventListener('click', function () {
    var rows = {};
    document.querySelectorAll('#mi-mapping-body input').forEach(function (input) {
      var i = input.dataset.i;
      rows[i] = rows[i] || {};
      rows[i][input.dataset.map === 'mylar' ? 'mylar_root' : 'clu_root'] = input.value;
    });
    var mapping = Object.keys(rows).map(function (k) { return rows[k]; });
    var btn = this;
    btn.disabled = true;
    postJson(urls.preview, { import_id: state.importId, mapping: mapping })
      .then(function (res) {
        var data = handleResult(res);
        if (data) {
          showPreview(data, false);
          CLU.showSuccess('Preview updated');
        }
      })
      .catch(function () { CLU.showError('Could not update the preview'); })
      .finally(function () { btn.disabled = false; });
  });

  function renderSeries() {
    var p = state.preview;
    $('mi-series-card').classList.toggle('d-none', !p.has_db);
    var filter = ($('mi-series-filter').value || '').toLowerCase();
    var html = p.series.filter(function (row) {
      return !filter || row.name.toLowerCase().indexOf(filter) !== -1;
    }).map(function (row) {
      var label = STATE_LABELS[row.state] || [row.state, 'secondary'];
      var importable = !!IMPORTABLE[row.state];
      var marks = [];
      if (row.paused) marks.push(badge('Paused', 'secondary'));
      if (row.skipped_count) marks.push(row.skipped_count + ' skipped');
      if (row.owned_count) marks.push(row.owned_count + ' archived');
      return '<tr' + (importable ? '' : ' class="text-muted"') + '>' +
        '<td><input type="checkbox" class="form-check-input" data-cv="' + row.cv_id + '"' +
        (state.selected[row.cv_id] ? ' checked' : '') + (importable ? '' : ' disabled') +
        ' aria-label="Import ' + esc(row.name) + '"></td>' +
        '<td>' + esc(row.name) + (row.year ? ' <span class="text-muted">(' + row.year + ')</span>' : '') +
        (row.publisher ? '<div class="small text-muted">' + esc(row.publisher) + '</div>' : '') + '</td>' +
        '<td class="small text-break">' + esc(row.path || row.mylar_location || '') + '</td>' +
        '<td>' + badge(label[0], label[1]) +
        (row.reason ? '<div class="small text-muted">' + esc(row.reason) + '</div>' : '') + '</td>' +
        '<td class="small">' + marks.join(', ') + '</td></tr>';
    }).join('');
    $('mi-series-body').innerHTML = html ||
      '<tr><td colspan="5" class="text-center text-muted py-3">No series</td></tr>';
    updateSummary();
  }

  function updateSummary() {
    var p = state.preview;
    var count = Object.keys(state.selected).length;
    $('mi-series-summary').textContent = '(' + count + ' of ' + p.series.length + ' selected)';
  }

  $('mi-series-body').addEventListener('change', function (ev) {
    var box = ev.target;
    if (!box.dataset.cv) return;
    if (box.checked) state.selected[box.dataset.cv] = true;
    else delete state.selected[box.dataset.cv];
    updateSummary();
  });

  $('mi-series-filter').addEventListener('input', renderSeries);

  function selectVisible(on) {
    document.querySelectorAll('#mi-series-body input[data-cv]:not(:disabled)').forEach(function (box) {
      box.checked = on;
      if (on) state.selected[box.dataset.cv] = true;
      else delete state.selected[box.dataset.cv];
    });
    updateSummary();
  }
  $('mi-select-all').addEventListener('click', function () { selectVisible(true); });
  $('mi-select-none').addEventListener('click', function () { selectVisible(false); });

  function settingRow(id, checked, disabled, title, detail, stateKey) {
    var s = SETTING_STATES[stateKey] || [stateKey, 'secondary'];
    return '<div class="form-check mb-2">' +
      '<input class="form-check-input" type="checkbox" id="' + id + '"' +
      (checked ? ' checked' : '') + (disabled ? ' disabled' : '') + '>' +
      '<label class="form-check-label" for="' + id + '">' + esc(title) + ' ' + badge(s[0], s[1]) +
      (detail ? '<div class="small text-muted">' + esc(detail) + '</div>' : '') + '</label></div>';
  }

  function renderSettings() {
    var p = state.preview;
    var s = p.settings || {};
    var parts = [];
    if (!p.has_config) {
      parts.push('<p class="text-muted mb-0">No config.ini was loaded, so no settings will be imported.</p>');
    } else {
      if (s.comicvine) {
        parts.push(settingRow('mi-set-cv', s.comicvine.state === 'new', s.comicvine.state === 'same',
          'ComicVine API key', s.comicvine.state === 'differs' ? 'Tick to replace the key CLU has.' : '',
          s.comicvine.state));
      }
      if (s.client) {
        var c = s.client;
        var name = c.type === 'sabnzbd' ? 'SABnzbd' : 'NZBGet';
        var where = (c.use_ssl ? 'https://' : 'http://') + c.host + (c.port ? ':' + c.port : '') +
          (c.url_base ? '/' + c.url_base : '');
        var detail = where + (c.category ? ', category ' + c.category : '') +
          (c.has_api_key ? ', API key set' : '') + (c.has_password ? ', password set' : '') +
          '. This is the address Mylar used; it is tested from CLU after saving.';
        parts.push(settingRow('mi-set-client', c.state === 'new', c.state === 'same',
          name + ' download client', detail, c.state));
      }
      (s.indexers || []).forEach(function (ix) {
        var detail = ix.url + ', categories ' + ix.categories +
          (ix.has_api_key ? ', API key set' : '') + (ix.enabled ? '' : ', disabled in Mylar');
        parts.push(settingRow('mi-set-ix-' + ix.index, ix.state === 'new', ix.state === 'same',
          'Indexer: ' + ix.name, detail, ix.state));
      });
      if (parts.length === 0) {
        parts.push('<p class="text-muted mb-0">Mylar\'s config.ini has no settings CLU can import.</p>');
      } else {
        parts.push('<p class="small text-muted mb-0">If a Usenet client and an enabled indexer are set up ' +
          'afterwards, Usenet is added to the download source priority.</p>');
      }
    }
    $('mi-settings-body').innerHTML = parts.join('');
  }

  function renderSkipped() {
    var items = state.preview.not_imported || [];
    $('mi-skipped-card').classList.toggle('d-none', items.length === 0);
    $('mi-skipped-body').innerHTML = items.map(function (text) {
      return '<li class="list-group-item">' + esc(text) + '</li>';
    }).join('');
  }

  $('mi-start-over').addEventListener('click', function () {
    state = { importId: null, preview: null, selected: {} };
    $('mi-upload-form').reset();
    $('mi-step-review').classList.add('d-none');
    $('mi-step-load').classList.remove('d-none');
  });

  // ── Step 3: apply and poll ────────────────────────────────────────────

  function checked(id) {
    var el = $(id);
    return !!(el && el.checked && !el.disabled);
  }

  $('mi-apply-btn').addEventListener('click', function () {
    var p = state.preview;
    var indexers = ((p.settings || {}).indexers || []).filter(function (ix) {
      return checked('mi-set-ix-' + ix.index);
    }).map(function (ix) { return ix.index; });
    var body = {
      import_id: state.importId,
      series: Object.keys(state.selected).map(Number),
      settings: { comicvine: checked('mi-set-cv'), client: checked('mi-set-client'), indexers: indexers }
    };
    if (!body.series.length && !body.settings.comicvine && !body.settings.client && !indexers.length) {
      CLU.showError('Nothing is selected to import');
      return;
    }
    var btn = this;
    btn.disabled = true;
    postJson(urls.apply, body).then(function (res) {
      var data = handleResult(res);
      if (!data) { btn.disabled = false; return; }
      showRun();
      poll(data.op_id);
    }).catch(function () {
      btn.disabled = false;
      CLU.showError('Could not start the import');
    });
  });

  function showRun() {
    $('mi-step-load').classList.add('d-none');
    $('mi-step-review').classList.add('d-none');
    $('mi-step-run').classList.remove('d-none');
    $('mi-run-note').classList.remove('d-none');
    $('mi-result-links').classList.add('d-none');
    $('mi-result').innerHTML = '';
    var bar = $('mi-progress');
    bar.classList.add('progress-bar-animated');
    bar.classList.remove('bg-danger');
    bar.style.width = '0%';
  }

  // A finished import is shown again on return until the user starts a new
  // one; remembering that choice is a per-browser convenience only.
  var DISMISSED_KEY = 'clu.mylarImport.dismissed';
  function dismissed(opId) {
    try { return window.localStorage.getItem(DISMISSED_KEY) === opId; } catch (e) { return false; }
  }

  $('mi-new-import').addEventListener('click', function () {
    try { window.localStorage.setItem(DISMISSED_KEY, root.dataset.latestJob || ''); } catch (e) { /* ignore */ }
    $('mi-step-run').classList.add('d-none');
    $('mi-step-load').classList.remove('d-none');
    $('mi-apply-btn').disabled = false;
    state = { importId: null, preview: null, selected: {} };
    $('mi-upload-form').reset();
  });

  // Coming back to the page: pick up the import that is running, or show the
  // last one's result, instead of offering to start again.
  (function resume() {
    var opId = root.dataset.latestJob;
    if (!opId || dismissed(opId)) return;
    showRun();
    poll(opId);
  })();

  function poll(opId) {
    root.dataset.latestJob = opId;
    var timer = CLU.startPoll(function (signal) {
      return fetch(urls.status + '?op_id=' + encodeURIComponent(opId), { signal: signal })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.success) {
            clearInterval(timer);
            CLU.showError(data.error || 'The import status was lost');
            return;
          }
          var job = data.job;
          var pct = job.total ? Math.round(100 * job.current / job.total) : 0;
          $('mi-progress').style.width = pct + '%';
          $('mi-progress-detail').textContent = job.detail || '';
          if (job.status === 'running') return;
          clearInterval(timer);
          $('mi-run-note').classList.add('d-none');
          $('mi-progress').classList.remove('progress-bar-animated');
          if (job.status === 'error') {
            $('mi-progress').classList.add('bg-danger');
            CLU.showError('The import failed: ' + (job.error || 'unknown error'));
            $('mi-result-links').classList.remove('d-none');
            return;
          }
          $('mi-progress').style.width = '100%';
          renderResult(job.result || {});
          CLU.showSuccess('Mylar3 import finished');
        });
    }, 2000);
  }

  function resultList(title, items, total, truncated, type) {
    if (!total) return '';
    return '<h6 class="mt-3">' + badge(total, type) + ' ' + esc(title) + '</h6>' +
      '<ul class="small mb-0">' + items.map(function (it) {
        return '<li>' + esc(it.name) + (it.reason ? ' <span class="text-muted">- ' + esc(it.reason) + '</span>' : '') + '</li>';
      }).join('') + (truncated ? '<li class="text-muted">and ' + (total - items.length) + ' more</li>' : '') +
      '</ul>';
  }

  function testLine(label, r) {
    if (!r) return '';
    if (!r.saved) return '<li>' + esc(label) + ': ' + badge('not saved', 'danger') + ' ' + esc(r.error || '') + '</li>';
    var test = r.valid === true ? badge('connection OK', 'success') :
      r.valid === false ? badge('test failed', 'warning') + ' <span class="text-muted">' + esc(r.error || '') + '</span>' : '';
    return '<li>' + esc(label) + ': saved ' + test + (r.activated ? ' ' + badge('active', 'info') : '') + '</li>';
  }

  function renderResult(r) {
    var sources = r.source_counts || {};
    var notes = [];
    if (sources.metron) notes.push(sources.metron + ' from Metron');
    if (sources.mylar) notes.push(sources.mylar + ' from Mylar\'s ComicVine data');
    if (sources.comicvine) notes.push(sources.comicvine + ' from ComicVine');
    var html = '<p class="mb-1"><strong>' + (r.mapped_total || 0) + '</strong> series added to the Pull List' +
      (notes.length ? ' (' + notes.join(', ') + ')' : '') +
      (r.folders_created ? ', ' + r.folders_created + ' folder(s) created' : '') +
      (r.sidecars_written ? ', ' + r.sidecars_written + ' cvinfo/series.json file(s) written' : '') + '.</p>';
    if (sources.mylar) {
      html += '<p class="small text-muted mb-1">Series added from Mylar\'s data use ComicVine-only ids and ' +
        'are as current as Mylar was. Use Sync on a series page to refresh one from ComicVine.</p>';
    }

    var s = r.settings || {};
    var lines = [];
    if (s.comicvine) lines.push(s.comicvine.saved ? '<li>ComicVine API key: saved</li>' :
      '<li>ComicVine API key: ' + badge('not saved', 'danger') + '</li>');
    if (s.client) lines.push(testLine(s.client.type === 'sabnzbd' ? 'SABnzbd' : 'NZBGet', s.client));
    (s.indexers || []).forEach(function (ix) { lines.push(testLine('Indexer ' + ix.name, ix)); });
    if (s.source_priority) lines.push('<li>Usenet added to the download source priority</li>');
    if (lines.length) html += '<ul class="small">' + lines.join('') + '</ul>';

    html += resultList('deferred: run the import again later', r.deferred, r.deferred_total, r.deferred_truncated, 'warning');
    html += resultList('could not be identified', r.unresolved, r.unresolved_total, r.unresolved_truncated, 'secondary');
    html += resultList('skipped', r.skipped, r.skipped_total, r.skipped_truncated, 'secondary');
    html += resultList('failed', r.failed, r.failed_total, r.failed_truncated, 'danger');
    $('mi-result').innerHTML = html;
    $('mi-result-links').classList.remove('d-none');
  }
})();
