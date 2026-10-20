/**
 * Guided Setup wizard (templates/setup.html, readme/guided-setup.md).
 *
 * The step list is preflight -> usecase -> the sections the chosen use cases
 * need, in canonical order -> summary. Only sections that have a step are in
 * SECTIONS; the rest are reported on the Summary with a Settings link. Every
 * step saves as it goes, so there is no "save configuration" step.
 *
 * All URLs come from window.SETUP_CONFIG (built with url_for), because CLU
 * can run behind a subpath proxy. Messages are toasts, never alert().
 */
(function () {
  'use strict';

  const CFG = window.SETUP_CONFIG || {};
  const URLS = CFG.urls || {};

  // Built sections, in canonical order, with the use cases that pull them in.
  // "fresh" is the "No, I'm starting fresh" answer: every built section.
  const SECTIONS = [
    { id: 'libraries', label: 'Libraries',
      usecases: ['repair', 'metadata', 'mylar', 'pull_list', 'download', 'read'] },
    { id: 'metadata', label: 'Metadata',
      usecases: ['metadata', 'mylar', 'pull_list', 'download'] },
  ];
  // Sections a use case needs that the wizard does not cover yet.
  const LATER = {
    repair: ['File Processing (Settings)', 'Problem Files'],
    mylar: ['the Mylar3 importer (Settings, top right)'],
    pull_list: ['the folder scan on the Pull List page'],
    download: ['Folder Monitoring', 'Download sources', 'Schedules', 'Notifications'],
    read: ['Users and sharing'],
  };
  const NEEDS_METRON = ['mylar', 'pull_list', 'download', 'fresh'];

  const STATUS_STYLE = {
    ok:   { icon: 'bi-check-circle-fill', cls: 'text-success' },
    warn: { icon: 'bi-exclamation-triangle-fill', cls: 'text-warning' },
    fail: { icon: 'bi-x-octagon-fill', cls: 'text-danger' },
    skip: { icon: 'bi-dash-circle', cls: 'text-muted' },
  };
  const SECTION_BADGE = {
    configured:      { text: 'Configured', cls: 'bg-success' },
    needs_attention: { text: 'Needs attention', cls: 'bg-warning text-dark' },
    unconfigured:    { text: 'Not set up', cls: 'bg-secondary' },
    skipped:         { text: 'Skipped', cls: 'bg-secondary' },
  };

  const state = {
    path: Array.isArray(CFG.state && CFG.state.path) ? CFG.state.path.slice() : [],
    steps: [],
    index: 0,
    preflight: null,
    status: {},
    skipped: new Set(),
    pollers: {},
  };

  // ── helpers ────────────────────────────────────────────────────────────

  function $(id) { return document.getElementById(id); }

  // CLU.showToast renders its message as HTML; these carry server text and paths.
  function showError(msg) { CLU.showError(CLU.escapeHtml(String(msg))); }
  function showSuccess(msg) { CLU.showSuccess(CLU.escapeHtml(String(msg))); }
  function showWarning(msg) { CLU.showToast('Saved', CLU.escapeHtml(String(msg)), 'warning'); }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function url(name, params) {
    let u = URLS[name] || '';
    Object.keys(params || {}).forEach(function (k) {
      u = u.replace(k, encodeURIComponent(params[k]));
    });
    return u;
  }

  async function api(u, opts) {
    opts = opts || {};
    const init = { method: opts.method || 'GET', headers: {}, signal: opts.signal };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    const resp = await fetch(u, init);
    let data = {};
    try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
    if (!resp.ok && !opts.allowError) {
      throw new Error(data.error || ('HTTP ' + resp.status));
    }
    return data;
  }

  function saveState(fields) {
    return api(URLS.state, { method: 'POST', body: fields }).catch(function (err) {
      showError('Could not save setup progress: ' + err.message);
    });
  }

  function badge(node, key) {
    const b = SECTION_BADGE[key];
    node.className = 'badge ' + (b ? b.cls : 'bg-secondary');
    node.textContent = b ? b.text : '';
  }

  /** Follow an operation until it finishes. A missing op means finished. */
  function followOperation(key, opId, onUpdate, onDone) {
    stopPoll(key);
    state.pollers[key] = CLU.startPoll(async function (signal) {
      const data = await api(url('operation', { __ID__: opId }), { signal: signal });
      const op = data.operation;
      if (!op || op.status === 'completed' || op.status === 'error') {
        stopPoll(key);
        onDone(op ? op.status !== 'error' : true, op);
      } else {
        onUpdate(op);
      }
    }, 1500);
  }

  function stopPoll(key) {
    if (state.pollers[key]) {
      clearInterval(state.pollers[key]);
      delete state.pollers[key];
    }
  }

  // ── step list and navigation ───────────────────────────────────────────

  function computeSteps() {
    const path = state.path;
    const fresh = path.indexOf('fresh') !== -1;
    const sections = SECTIONS.filter(function (s) {
      return fresh || s.usecases.some(function (u) { return path.indexOf(u) !== -1; });
    });
    state.steps = ['preflight', 'usecase']
      .concat(sections.map(function (s) { return s.id; }))
      .concat(['summary']);
  }

  function stepLabel(id) {
    if (id === 'preflight') return 'System Check';
    if (id === 'usecase') return 'How you use CLU';
    if (id === 'summary') return 'Summary';
    const s = SECTIONS.find(function (x) { return x.id === id; });
    return s ? s.label : id;
  }

  function renderRail() {
    const rail = $('setup-rail');
    rail.replaceChildren();
    state.steps.forEach(function (id, i) {
      const item = el('button', 'list-group-item list-group-item-action d-flex align-items-center');
      item.type = 'button';
      if (i === state.index) item.classList.add('active');
      // Sections can only be reached once the use-case question is answered.
      if (i > 1 && !state.path.length) item.classList.add('disabled');
      item.appendChild(el('span', 'me-auto', (i + 1) + '. ' + stepLabel(id)));
      const st = state.skipped.has(id) && (!state.status[id] || state.status[id].status !== 'configured')
        ? 'skipped' : (state.status[id] && state.status[id].status);
      if (st) {
        const b = el('span', 'rail-badge');
        badge(b, st);
        item.appendChild(b);
      }
      item.addEventListener('click', function () {
        if (!item.classList.contains('disabled')) goTo(i);
      });
      rail.appendChild(item);
    });
  }

  function goTo(i) {
    i = Math.max(0, Math.min(i, state.steps.length - 1));
    state.index = i;
    const id = state.steps[i];
    document.querySelectorAll('.setup-step').forEach(function (s) {
      s.classList.toggle('d-none', s.dataset.step !== id);
    });
    $('setup-back').classList.toggle('invisible', i === 0);
    $('setup-next').classList.toggle('d-none', id === 'summary');
    $('setup-skip-step').classList.toggle('d-none',
      id === 'summary' || id === 'preflight' || id === 'usecase');
    updateNextEnabled();
    renderRail();
    window.scrollTo({ top: 0, behavior: 'smooth' });
    const enter = ENTER[id];
    if (enter) enter();
  }

  function updateNextEnabled() {
    const id = state.steps[state.index];
    let ok = true;
    if (id === 'preflight') ok = !(state.preflight && state.preflight.blocking);
    if (id === 'usecase') ok = state.path.length > 0;
    $('setup-next').disabled = !ok;
  }

  function next(skip) {
    const id = state.steps[state.index];
    if (skip) state.skipped.add(id);
    const target = state.steps[state.index + 1];
    const fields = { last_step: target };
    if (id === 'usecase') fields.path = state.path;
    saveState(fields);
    goTo(state.index + 1);
  }

  // ── System Check ───────────────────────────────────────────────────────

  function renderChecks(data, outputId, statusId) {
    const output = $(outputId);
    const status = $(statusId);
    output.replaceChildren();
    (data.checks || []).forEach(function (c) {
      const style = STATUS_STYLE[c.status] || STATUS_STYLE.skip;
      const item = el('div', 'list-group-item d-flex gap-3 align-items-start');
      item.appendChild(el('i', 'bi ' + style.icon + ' ' + style.cls + ' fs-5'));
      const body = el('div', 'flex-grow-1');
      body.appendChild(el('div', 'fw-semibold', c.label));
      if (c.message) body.appendChild(el('div', 'small', c.message));
      if (c.fix && c.status !== 'ok') body.appendChild(el('div', 'small text-muted mt-1', c.fix));
      if (c.docs && c.status !== 'ok') {
        const a = el('a', 'small', 'Documentation');
        a.href = c.docs; a.target = '_blank'; a.rel = 'noopener';
        body.appendChild(a);
      }
      item.appendChild(body);
      output.appendChild(item);
    });
    const s = data.summary || {};
    if (s.fail) { status.textContent = s.fail + ' failed'; status.className = 'badge bg-danger ms-2'; }
    else if (s.warn) {
      status.textContent = s.warn + ' warning' + (s.warn === 1 ? '' : 's');
      status.className = 'badge bg-warning text-dark ms-2';
    } else { status.textContent = 'All OK'; status.className = 'badge bg-success ms-2'; }
  }

  async function runPreflight(outputId, statusId) {
    const status = $(statusId);
    status.textContent = 'Running...';
    status.className = 'badge bg-secondary ms-2';
    $(outputId).replaceChildren(el('div', 'list-group-item text-muted', 'Running checks...'));
    try {
      const data = await api(URLS.preflight);
      state.preflight = data;
      renderChecks(data, outputId, statusId);
    } catch (err) {
      status.textContent = 'Error';
      status.className = 'badge bg-danger ms-2';
      $(outputId).replaceChildren(
        el('div', 'list-group-item text-danger', 'Could not run the checks: ' + err.message));
    }
    return state.preflight;
  }

  async function enterPreflight() {
    if (state.preflight) return;
    await runPreflight('check-output', 'check-status');
    $('check-blocking').classList.toggle('d-none', !(state.preflight && state.preflight.blocking));
    updateNextEnabled();
  }

  // ── Use case ───────────────────────────────────────────────────────────

  function initUsecase() {
    const yes = $('has-collection-yes');
    const no = $('has-collection-no');
    const boxes = document.querySelectorAll('#usecase-options input[type=checkbox]');
    if (state.path.indexOf('fresh') !== -1) no.checked = true;
    else if (state.path.length) yes.checked = true;
    boxes.forEach(function (b) { b.checked = state.path.indexOf(b.value) !== -1; });

    function sync() {
      $('usecase-list').classList.toggle('d-none', !yes.checked);
      $('usecase-fresh').classList.toggle('d-none', !no.checked);
      if (no.checked) state.path = ['fresh'];
      else if (yes.checked) {
        state.path = Array.prototype.filter.call(boxes, function (b) { return b.checked; })
          .map(function (b) { return b.value; });
      } else state.path = [];
      const later = [];
      state.path.forEach(function (u) {
        (LATER[u] || []).forEach(function (s) { if (later.indexOf(s) === -1) later.push(s); });
      });
      const note = $('usecase-later');
      note.classList.toggle('d-none', !later.length);
      note.textContent = later.length
        ? 'Also relevant, but not part of this walkthrough yet: ' + later.join(', ') +
          '. The Summary links to each one.'
        : '';
      computeSteps();
      updateNextEnabled();
      renderRail();
    }
    [yes, no].forEach(function (r) { r.addEventListener('change', sync); });
    boxes.forEach(function (b) { b.addEventListener('change', sync); });
    sync();
  }

  // ── Libraries ──────────────────────────────────────────────────────────

  function basename(p) {
    const parts = String(p).replace(/\/+$/, '').split('/');
    return parts[parts.length - 1] || p;
  }

  async function enterLibraries() {
    const current = $('lib-current');
    const found = $('lib-found');
    current.replaceChildren(el('div', 'list-group-item text-muted', 'Loading...'));
    found.replaceChildren();
    let libs = [];
    try {
      libs = (await api(URLS.libraries + '?all=true')).libraries || [];
    } catch (err) {
      current.replaceChildren(el('div', 'list-group-item text-danger', 'Could not load libraries: ' + err.message));
    }
    if (!state.preflight) await runPreflight('check-output', 'check-status');

    current.replaceChildren();
    if (!libs.length) {
      current.appendChild(el('div', 'list-group-item text-muted', 'No libraries yet. Add one below.'));
    }
    const checks = {};
    ((state.preflight && state.preflight.checks) || []).forEach(function (c) { checks[c.id] = c; });
    libs.forEach(function (lib) {
      const item = el('div', 'list-group-item d-flex gap-2 align-items-center');
      const check = checks['library:' + lib.id];
      const style = STATUS_STYLE[check ? check.status : 'skip'] || STATUS_STYLE.skip;
      item.appendChild(el('i', 'bi ' + style.icon + ' ' + style.cls));
      const body = el('div', 'flex-grow-1');
      body.appendChild(el('div', 'fw-semibold', lib.name + (lib.enabled === 0 || lib.enabled === false ? ' (disabled)' : '')));
      body.appendChild(el('div', 'small text-muted text-break', lib.path));
      if (check && check.status !== 'ok') body.appendChild(el('div', 'small', check.message));
      item.appendChild(body);
      current.appendChild(item);
    });

    const have = new Set(libs.map(function (l) { return l.path; }));
    const mount = checks.data_mount && checks.data_mount.details || {};
    const candidates = (mount.subfolders || []).concat(mount.other_mounts || [])
      .filter(function (p) { return !have.has(p); });
    if (!candidates.length) {
      found.appendChild(el('div', 'list-group-item text-muted small',
        'No unused folders found. If you expected some, check the volume mapping, or browse below.'));
    }
    candidates.forEach(function (p) {
      const item = el('div', 'list-group-item d-flex gap-2 align-items-center');
      item.appendChild(el('i', 'bi bi-folder2 text-muted'));
      item.appendChild(el('span', 'flex-grow-1 text-break', p));
      const btn = el('button', 'btn btn-sm btn-outline-primary', 'Add as library');
      btn.type = 'button';
      btn.addEventListener('click', function () { addLibrary(p, btn); });
      item.appendChild(btn);
      found.appendChild(item);
    });
  }

  async function addLibrary(path, btn) {
    if (btn) btn.disabled = true;
    try {
      const data = await api(URLS.libraries, { method: 'POST', body: { name: basename(path), path: path } });
      showSuccess(data.message || 'Library added.');
      if (data.op_id) {
        $('lib-progress').classList.remove('d-none');
        $('lib-progress-label').textContent = 'Indexing ' + basename(path) + '...';
        followOperation('library', data.op_id, function (op) {
          $('lib-progress-label').textContent = op.detail || 'Indexing...';
        }, function (ok) {
          $('lib-progress').classList.add('d-none');
          if (ok) showSuccess('Indexing of ' + basename(path) + ' finished.');
          else showError('Indexing of ' + basename(path) + ' failed. See the logs.');
          refreshStatus();
        });
      }
      state.preflight = null;  // the library checks changed
      await enterLibraries();
      refreshStatus();
    } catch (err) {
      if (btn) btn.disabled = false;
      showError('Could not add the library: ' + err.message);
    }
  }

  // Folder picker over /api/setup/browse.
  const browser = { current: null, modal: null };

  async function browseTo(path) {
    const list = $('browse-list');
    list.replaceChildren(el('div', 'list-group-item text-muted', 'Loading...'));
    $('browse-select').disabled = true;
    $('browse-readonly').classList.add('d-none');
    let data;
    try {
      data = await api(URLS.browse + (path ? '?path=' + encodeURIComponent(path) : ''));
    } catch (err) {
      list.replaceChildren(el('div', 'list-group-item text-danger', err.message));
      return;
    }
    browser.current = data.current_path;
    $('browse-current').textContent = data.current_path || 'Mapped volumes';
    list.replaceChildren();
    if (data.current_path) {
      const up = el('button', 'list-group-item list-group-item-action');
      up.type = 'button';
      up.appendChild(el('i', 'bi bi-arrow-90deg-up me-2'));
      up.appendChild(document.createTextNode('Up'));
      up.addEventListener('click', function () { browseTo(data.parent || ''); });
      list.appendChild(up);
      $('browse-select').disabled = false;
      $('browse-readonly').classList.toggle('d-none', data.writable !== false);
    }
    const entries = data.current_path ? (data.directories || [])
      : (data.roots || []).filter(function (r) { return r.exists; }).map(function (r) {
          return { name: r.path, path: r.path };
        });
    entries.forEach(function (d) {
      const item = el('button', 'list-group-item list-group-item-action text-break');
      item.type = 'button';
      item.appendChild(el('i', 'bi bi-folder2 me-2'));
      item.appendChild(document.createTextNode(d.name));
      item.addEventListener('click', function () { browseTo(d.path); });
      list.appendChild(item);
    });
    if (!entries.length) list.appendChild(el('div', 'list-group-item text-muted small', 'No folders here.'));
    if (data.truncated) list.appendChild(el('div', 'list-group-item text-muted small', 'Only the first 1000 folders are shown.'));
  }

  function initBrowser() {
    browser.modal = new bootstrap.Modal($('browseModal'));
    $('lib-browse').addEventListener('click', function () {
      browser.modal.show();
      browseTo('');
    });
    $('browse-select').addEventListener('click', function () {
      if (!browser.current) return;
      browser.modal.hide();
      addLibrary(browser.current);
    });
  }

  // ── Metadata ───────────────────────────────────────────────────────────

  let providers = {};

  function providerBox(type) {
    return document.querySelector('.setup-step[data-step="metadata"] [data-provider="' + type + '"]');
  }

  function metronMode() {
    const checked = document.querySelector('input[name="metron-mode"]:checked');
    return checked ? checked.value : 'token';
  }

  function syncMetronMode() {
    const box = providerBox('metron');
    box.querySelectorAll('[data-mode]').forEach(function (d) {
      d.classList.toggle('d-none', d.dataset.mode !== metronMode());
    });
  }

  /** The credential fields the user typed (visible auth mode only). */
  function collectFields(type) {
    const box = providerBox(type);
    const body = {};
    box.querySelectorAll('[data-field]').forEach(function (input) {
      const mode = input.closest('[data-mode]');
      if (mode && mode.classList.contains('d-none')) return;
      const v = input.value.trim();
      if (v) body[input.dataset.field] = v;
    });
    if (type === 'metron' && Object.keys(body).length) body.auth_mode = metronMode();
    return body;
  }

  function setResult(box, ok, text) {
    const r = box.querySelector('.provider-result');
    if (!r) return;
    r.className = 'provider-result align-self-center ' + (ok ? 'text-success' : 'text-danger');
    r.textContent = text || '';
  }

  function renderProviderBadges() {
    Object.keys(providers).forEach(function (type) {
      const box = providerBox(type);
      if (!box) return;
      const p = providers[type];
      const b = box.querySelector('.provider-badge');
      if (b) {
        if (p.auth_blocked) { b.className = 'badge ms-2 provider-badge bg-danger'; b.textContent = 'Rejected'; }
        else if (p.has_credentials && p.is_valid) { b.className = 'badge ms-2 provider-badge bg-success'; b.textContent = 'Connected'; }
        else if (p.has_credentials) { b.className = 'badge ms-2 provider-badge bg-warning text-dark'; b.textContent = 'Not verified'; }
        else { b.className = 'badge ms-2 provider-badge bg-secondary'; b.textContent = 'Not set'; }
      }
      const masked = p.credentials_masked || {};
      box.querySelectorAll('[data-field]').forEach(function (input) {
        const v = masked[input.dataset.field];
        if (!v) return;
        // A path is shown as-is; a secret only as a hint that one is saved.
        if (input.dataset.field === 'database_path') { if (!input.value) input.value = v; }
        else input.placeholder = 'Saved (' + v + ')';
      });
      if (type === 'metron' && masked.username && !masked.api_token) {
        $('metron-mode-basic').checked = true;
        syncMetronMode();
      }
    });
  }

  async function loadProviders() {
    try {
      const data = await api(URLS.providers);
      providers = {};
      (data.providers || []).forEach(function (p) { providers[p.type] = p; });
      renderProviderBadges();
    } catch (err) {
      showError('Could not load the metadata providers: ' + err.message);
    }
  }

  async function testProvider(type, box) {
    const body = collectFields(type);
    const btn = box.querySelector('.provider-test');
    btn.disabled = true;
    setResult(box, true, 'Testing...');
    try {
      // An empty form tests what is already saved; typed fields are tested unsaved.
      const opts = { method: 'POST', allowError: true };
      if (Object.keys(body).length) opts.body = body;
      const data = await api(url('providerTest', { __TYPE__: type }), opts);
      if (data.valid) {
        setResult(box, true, Object.keys(body).length ? 'Connected. Save to keep these.' : 'Connected.');
        const save = box.querySelector('.provider-save');
        if (save) save.disabled = !Object.keys(body).length;
      } else {
        setResult(box, false, data.error || 'The test failed.');
      }
    } catch (err) {
      setResult(box, false, err.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function saveProvider(type, box) {
    const body = collectFields(type);
    if (!Object.keys(body).length) { showError('Enter the details first.'); return; }
    const save = box.querySelector('.provider-save, .provider-save-plain');
    save.disabled = true;
    try {
      const data = await api(url('providerCreds', { __TYPE__: type }), { method: 'POST', body: body });
      if (data.valid === false) {
        setResult(box, false, data.error || data.message);
        showWarning(data.message);
      } else {
        setResult(box, true, 'Saved.');
        showSuccess(data.message || 'Saved.');
      }
      box.querySelectorAll('[data-field]').forEach(function (i) {
        if (i.dataset.field !== 'database_path') i.value = '';
      });
      await loadProviders();
      if (type === 'comicvine_sqlite') loadCvStatus();
      refreshStatus();
    } catch (err) {
      showError('Could not save: ' + err.message);
    } finally {
      if (save.classList.contains('provider-save-plain')) save.disabled = false;
    }
  }

  async function loadCvStatus() {
    try {
      const s = (await api(URLS.cvUpdateStatus)).status || {};
      $('cv-auto-update').checked = !!s.enabled;
      $('cv-download').disabled = !s.path_configured || s.running;
      const parts = [];
      if (!s.path_configured) parts.push('Save a path first.');
      else if (s.database_present) parts.push('Database present (' + s.database_size_human + ').');
      else parts.push('No database at that path yet.');
      if (s.last_error) parts.push('Last error: ' + s.last_error);
      $('cv-status').textContent = parts.join(' ');
      if (s.running) $('cv-progress').classList.remove('d-none');
    } catch (err) {
      $('cv-status').textContent = 'Could not read the database status: ' + err.message;
    }
  }

  async function downloadCv() {
    $('cv-download').disabled = true;
    try {
      const data = await api(URLS.cvUpdate, { method: 'POST', body: {} });
      $('cv-progress').classList.remove('d-none');
      followOperation('cvdb', data.op_id, function (op) {
        $('cv-status').textContent = op.detail || 'Downloading...';
      }, function (ok, op) {
        $('cv-progress').classList.add('d-none');
        if (ok) showSuccess('ComicVine database downloaded.');
        else showError('The ComicVine download failed' + (op && op.detail ? ': ' + op.detail : '.'));
        loadCvStatus();
        loadProviders();
      });
    } catch (err) {
      $('cv-download').disabled = false;
      showError('Could not start the download: ' + err.message);
    }
  }

  async function loadWizardSettings() {
    try {
      const values = (await api(URLS.settings)).settings || {};
      document.querySelectorAll('.setup-setting').forEach(function (input) {
        input.checked = !!values[input.dataset.setting];
      });
    } catch (err) {
      showError('Could not load settings: ' + err.message);
    }
  }

  function initMetadata() {
    document.querySelectorAll('input[name="metron-mode"]').forEach(function (r) {
      r.addEventListener('change', syncMetronMode);
    });
    document.querySelectorAll('.setup-step[data-step="metadata"] [data-provider]').forEach(function (box) {
      const type = box.dataset.provider;
      const test = box.querySelector('.provider-test');
      const save = box.querySelector('.provider-save');
      const plain = box.querySelector('.provider-save-plain');
      if (test) test.addEventListener('click', function () { testProvider(type, box); });
      if (save) save.addEventListener('click', function () { saveProvider(type, box); });
      if (plain) plain.addEventListener('click', function () { saveProvider(type, box); });
      // Typing new values means the last test no longer applies to them.
      box.querySelectorAll('[data-field]').forEach(function (i) {
        i.addEventListener('input', function () {
          if (save) save.disabled = true;
          setResult(box, true, '');
        });
      });
    });
    $('cv-download').addEventListener('click', downloadCv);
    $('cv-auto-update').addEventListener('change', async function () {
      const input = this;
      try {
        const data = await api(URLS.cvAutoUpdate, { method: 'POST', body: { enabled: input.checked } });
        input.checked = !!data.enabled;
      } catch (err) {
        input.checked = !input.checked;
        showError('Could not save: ' + err.message);
      }
    });
    document.querySelectorAll('.setup-setting').forEach(function (input) {
      input.addEventListener('change', async function () {
        const body = {};
        body[input.dataset.setting] = input.checked;
        try {
          await api(URLS.settings, { method: 'POST', body: body });
        } catch (err) {
          input.checked = !input.checked;
          showError('Could not save: ' + err.message);
        }
      });
    });
    syncMetronMode();
  }

  function enterMetadata() {
    const needsMetron = state.path.some(function (u) { return NEEDS_METRON.indexOf(u) !== -1; });
    $('meta-metron-required').classList.toggle('d-none', !needsMetron);
    loadProviders();
    loadCvStatus();
    loadWizardSettings();
  }

  // ── Summary ────────────────────────────────────────────────────────────

  async function refreshStatus() {
    try {
      const data = await api(URLS.status);
      state.statusList = data.sections || [];
      state.status = {};
      state.statusList.forEach(function (s) { state.status[s.id] = s; });
      renderRail();
    } catch (err) {
      /* the rail simply shows no badges */
    }
  }

  function settingsLink(section) {
    if (section.settings_tab) return URLS.config + '#' + section.settings_tab;
    if (section.id === 'schedules') return URLS.schedules;
    if (section.id === 'users') return URLS.users;
    return URLS.config;
  }

  async function enterSummary() {
    const rows = $('summary-rows');
    rows.replaceChildren(el('tr', null));
    rows.firstChild.appendChild(el('td', 'text-muted', 'Loading...'));
    await refreshStatus();
    rows.replaceChildren();
    (state.statusList || []).forEach(function (s) {
      const tr = el('tr');
      tr.appendChild(el('td', 'fw-semibold', s.label));
      const td = el('td');
      const b = el('span');
      let key = s.status;
      if (!s.built) key = null;
      else if (state.skipped.has(s.id) && s.status !== 'configured') key = 'skipped';
      if (key) badge(b, key);
      else { b.className = 'badge bg-light text-dark border'; b.textContent = 'Configure in Settings'; }
      td.appendChild(b);
      if (s.message) td.appendChild(el('div', 'small text-muted', s.message));
      tr.appendChild(td);
      const actions = el('td', 'text-end text-nowrap');
      const stepIdx = state.steps.indexOf(s.id);
      if (s.built) {
        const go = el('button', 'btn btn-sm btn-outline-primary me-1', 'Open step');
        go.type = 'button';
        go.addEventListener('click', function () {
          if (stepIdx === -1) {
            // Not on this path: add it, keeping the canonical order.
            const usecase = SECTIONS.find(function (x) { return x.id === s.id; }).usecases[0];
            state.path = state.path.concat(usecase);
            // Tick it too, or the next change on the use-case step drops it again.
            const box = document.querySelector('#usecase-options input[value="' + usecase + '"]');
            if (box) box.checked = true;
            computeSteps();
            saveState({ path: state.path });
          }
          goTo(state.steps.indexOf(s.id));
        });
        actions.appendChild(go);
      }
      const a = el('a', 'btn btn-sm btn-outline-secondary', 'Settings');
      a.href = settingsLink(s);
      actions.appendChild(a);
      tr.appendChild(actions);
      rows.appendChild(tr);
    });
    runPreflight('summary-check-output', 'summary-check-status');
  }

  function downloadSummary() {
    const payload = {
      generated_at: new Date().toISOString(),
      setup: { path: state.path, skipped: Array.from(state.skipped) },
      sections: state.statusList || [],
      preflight: state.preflight,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'clu-setup-summary.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  }

  const ENTER = {
    preflight: enterPreflight,
    libraries: enterLibraries,
    metadata: enterMetadata,
    summary: enterSummary,
  };

  // ── boot ───────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    computeSteps();
    initUsecase();
    initBrowser();
    initMetadata();

    $('setup-back').addEventListener('click', function () { goTo(state.index - 1); });
    $('setup-next').addEventListener('click', function () { next(false); });
    $('setup-skip-step').addEventListener('click', function () { next(true); });
    $('check-rerun').addEventListener('click', async function () {
      state.preflight = null;
      await enterPreflight();
    });
    $('setup-download-summary').addEventListener('click', downloadSummary);

    $('setup-skip').addEventListener('click', async function () {
      this.disabled = true;
      try {
        await api(URLS.state, { method: 'POST', body: { state: 'skipped' } });
        window.location.href = URLS.index;
      } catch (err) {
        this.disabled = false;
        showError('Could not skip setup: ' + err.message);
      }
    });
    $('setup-finish').addEventListener('click', async function () {
      this.disabled = true;
      try {
        await api(URLS.state, { method: 'POST', body: { state: 'completed', last_step: '' } });
        window.location.href = URLS.index;
      } catch (err) {
        this.disabled = false;
        showError('Could not finish setup: ' + err.message);
      }
    });

    refreshStatus();
    const resume = state.steps.indexOf((CFG.state && CFG.state.last_step) || '');
    goTo(resume > 0 ? resume : 0);
  });
})();
