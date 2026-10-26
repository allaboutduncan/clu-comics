/**
 * Guided Setup wizard (templates/setup.html, readme/guided-setup.md).
 *
 * The step list is preflight -> usecase -> the sections the chosen use cases
 * need (plus the `always` ones), in canonical order -> summary. Only sections
 * that have a step are in
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
  // "fresh" is the "No, I'm starting fresh" answer: every built section except
  // the `existing` ones, which only make sense for a collection CLU can read.
  // `always` sections are on every path (and need no use case).
  const SECTIONS = [
    { id: 'libraries', label: 'Libraries',
      usecases: ['repair', 'metadata', 'mylar', 'pull_list', 'download', 'read'] },
    { id: 'metadata', label: 'Metadata',
      usecases: ['metadata', 'mylar', 'pull_list', 'download'] },
    { id: 'mylar_import', label: 'Mylar3 Import', usecases: ['mylar'], existing: true },
    { id: 'pull_list_scan', label: 'Pull List Scan', usecases: ['pull_list'], existing: true },
    { id: 'folder_monitoring', label: 'Folder Monitoring', usecases: ['repair', 'download'] },
    { id: 'downloads', label: 'Downloads', usecases: ['download', 'mylar'] },
    { id: 'schedules', label: 'Schedules', usecases: ['download'] },
    { id: 'users', label: 'Users', usecases: ['read', 'download'] },
    { id: 'notifications', label: 'Notifications', usecases: ['download', 'repair'] },
    { id: 'personalization', label: 'Personalization', usecases: [], always: true },
    { id: 'repair', label: 'Repair Tour', usecases: ['repair'] },
  ];
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

  function stepNode(id) { return document.querySelector('.setup-step[data-step="' + id + '"]'); }

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
      return (fresh && !s.existing) || s.always || s.usecases.some(function (u) { return path.indexOf(u) !== -1; });
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

  // Folder picker over /api/setup/browse. `onSelect(path)` is set by
  // whichever step opened it.
  const browser = { current: null, modal: null, onSelect: null };

  function openBrowser(onSelect) {
    browser.onSelect = onSelect;
    browser.modal.show();
    browseTo('');
  }

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
      openBrowser(function (path) { addLibrary(path); });
    });
    $('browse-select').addEventListener('click', function () {
      if (!browser.current || !browser.onSelect) return;
      browser.modal.hide();
      browser.onSelect(browser.current);
    });
  }

  // ── Metadata ───────────────────────────────────────────────────────────

  let providers = {};

  function providerBox(type) {
    return document.querySelector('.setup-step[data-step="metadata"] [data-provider="' + type + '"]');
  }

  /** The credential fields the user typed. */
  function collectFields(type) {
    const box = providerBox(type);
    const body = {};
    box.querySelectorAll('[data-field]').forEach(function (input) {
      const v = input.value.trim();
      if (v) body[input.dataset.field] = v;
    });
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
      if (type === 'metron') {
        // A username saved before mokkari 5 no longer signs in; say why.
        $('metron-basic-auth-note').classList.toggle('d-none', !(masked.username && !masked.api_token));
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

  // ── Wizard settings (/api/setup/settings) ─────────────────────────────
  //
  // Any `.setup-setting` input on any step, keyed by data-setting. The value
  // follows the input: a checkbox is a bool, type=number an int, anything
  // else a string. Each change saves that one key; a refused value reverts.

  let settingValues = {};

  function settingValue(input) {
    if (input.type === 'checkbox') return input.checked;
    if (input.type === 'number') return parseInt(input.value, 10);
    return input.value;
  }

  function showSetting(input, value) {
    if (input.type === 'checkbox') { input.checked = !!value; return; }
    value = value == null ? '' : String(value);
    if (input.tagName === 'SELECT' && value &&
        !Array.prototype.some.call(input.options, function (o) { return o.value === value; })) {
      // A stored value the list does not offer: show it rather than a blank.
      input.appendChild(new Option(value, value));
    }
    input.value = value;
  }

  /** Prefill the step's settings. Resolves once the inputs hold saved values. */
  async function loadWizardSettings(scope) {
    try {
      settingValues = (await api(URLS.settings)).settings || {};
      scope.querySelectorAll('.setup-setting').forEach(function (input) {
        showSetting(input, settingValues[input.dataset.setting]);
      });
    } catch (err) {
      showError('Could not load settings: ' + err.message);
    }
  }

  function initSettings() {
    document.querySelectorAll('.setup-setting').forEach(function (input) {
      input.addEventListener('change', async function () {
        const key = input.dataset.setting;
        const body = {};
        body[key] = settingValue(input);
        try {
          const data = await api(URLS.settings, { method: 'POST', body: body });
          settingValues = data.settings || settingValues;
        } catch (err) {
          showSetting(input, settingValues[key]);
          showError('Could not save: ' + err.message);
        }
        input.dispatchEvent(new CustomEvent('setup:saved', { bubbles: true }));
      });
    });
  }

  function initMetadata() {
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
  }

  function enterMetadata() {
    const needsMetron = state.path.some(function (u) { return NEEDS_METRON.indexOf(u) !== -1; });
    $('meta-metron-required').classList.toggle('d-none', !needsMetron);
    loadProviders();
    loadCvStatus();
    loadWizardSettings(stepNode('metadata'));
  }

  // ── Folder Monitoring ──────────────────────────────────────────────────

  // The WATCH/TARGET values as last saved ('' = the default applies).
  const folders = { saved: { watch: '', target: '' }, defaults: {} };

  function foldersDirty() {
    return $('fm-watch').value.trim() !== folders.saved.watch ||
      $('fm-target').value.trim() !== folders.saved.target;
  }

  function syncFolderControls() {
    const dirty = foldersDirty();
    $('fm-save-paths').disabled = !dirty;
    // The monitor watches the *saved* WATCH, so switching it on over unsaved
    // edits would watch the wrong folder.
    $('fm-monitor').disabled = dirty;
    $('fm-monitor-note').classList.toggle('d-none', !dirty);
  }

  function renderMonitor(st) {
    $('fm-monitor').checked = !!st.enabled;
    const b = $('fm-monitor-badge');
    if (!st.enabled) { b.className = 'badge ms-2 d-none'; return; }
    if (st.running) { b.className = 'badge ms-2 bg-success'; b.textContent = 'Running'; }
    else { b.className = 'badge ms-2 bg-warning text-dark'; b.textContent = 'Not running'; }
  }

  async function loadMonitorStatus() {
    try {
      renderMonitor(await api(URLS.folderMonitor));
    } catch (err) {
      showError('Could not read the monitor status: ' + err.message);
    }
  }

  function syncRenamePattern() {
    const on = $('set-enable_custom_rename').checked;
    $('set-custom_rename_pattern').disabled = !on;
    $('fm-pattern-box').classList.toggle('opacity-50', !on);
    $('fm-rename-preview').textContent =
      CLU.renamePreviewText($('set-custom_rename_pattern').value, CFG.renameCleanup);
  }

  async function enterFolderMonitoring() {
    try {
      const data = await api(URLS.folderPaths);
      folders.defaults = data.defaults || {};
      folders.saved = { watch: data.watch || '', target: data.target || '' };
      $('fm-watch').value = folders.saved.watch;
      $('fm-target').value = folders.saved.target;
      $('fm-watch').placeholder = folders.defaults.watch || '';
      $('fm-target').placeholder = folders.defaults.target || '';
      $('fm-defaults').textContent = 'Leave a folder blank to use its default (' +
        [folders.defaults.watch, folders.defaults.target].filter(Boolean).join(' and ') + ').';
    } catch (err) {
      showError('Could not load the folders: ' + err.message);
    }
    syncFolderControls();
    await loadWizardSettings(stepNode('folder_monitoring'));
    syncRenamePattern();
    loadMonitorStatus();
  }

  async function saveFolders() {
    const btn = $('fm-save-paths');
    const result = $('fm-paths-result');
    btn.disabled = true;
    result.className = 'align-self-center text-muted';
    result.textContent = 'Saving...';
    try {
      const data = await api(URLS.folderPaths, { method: 'POST', body: {
        watch: $('fm-watch').value.trim(), target: $('fm-target').value.trim() } });
      folders.saved = { watch: data.watch || '', target: data.target || '' };
      $('fm-watch').value = folders.saved.watch;
      $('fm-target').value = folders.saved.target;
      result.className = 'align-self-center text-success';
      result.textContent = 'Saved.';
      (data.warnings || []).forEach(showWarning);
      refreshStatus();
    } catch (err) {
      result.className = 'align-self-center text-danger';
      result.textContent = err.message;
    }
    syncFolderControls();
  }

  async function toggleMonitor() {
    const input = $('fm-monitor');
    input.disabled = true;
    try {
      renderMonitor(await api(URLS.folderMonitor, { method: 'POST', body: { enabled: input.checked } }));
      // The supervisor starts monitor.py in the background; look again once
      // it has had time to come up (or fail to).
      setTimeout(function () { loadMonitorStatus().then(refreshStatus); }, 4000);
    } catch (err) {
      input.checked = !input.checked;
      showError('Could not change folder monitoring: ' + err.message);
    }
    syncFolderControls();
  }

  function initFolderMonitoring() {
    ['fm-watch', 'fm-target'].forEach(function (id) {
      $(id).addEventListener('input', syncFolderControls);
    });
    stepNode('folder_monitoring').querySelectorAll('.fm-browse').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const input = $(btn.dataset.target);
        openBrowser(function (path) {
          input.value = path;
          syncFolderControls();
        });
      });
    });
    $('fm-save-paths').addEventListener('click', saveFolders);
    $('fm-monitor').addEventListener('change', toggleMonitor);
    $('set-custom_rename_pattern').addEventListener('input', syncRenamePattern);
    $('set-enable_custom_rename').addEventListener('change', syncRenamePattern);
  }

  // ── Downloads ──────────────────────────────────────────────────────────
  //
  // download_source_priority is both the order and the on/off switch: a
  // source not in it is never tried by Auto-download. It and the GetComics
  // mirror order are wizard settings (comma-separated); clients and indexers
  // have their own routes, and each is tested before it can be saved.

  const SOURCE_LABELS = { getcomics: 'GetComics', usenet: 'Usenet', dcpp: 'DC++' };
  const MIRROR_LABELS = { pixeldrain: 'PixelDrain', download_now: 'GetComics main server', mega: 'MEGA' };
  const CLIENT_FIELD_LABELS = {
    host: 'Host', port: 'Port', api_key: 'API key', username: 'Username', password: 'Password',
    category: 'Category', priority: 'Priority', url_base: 'URL base', use_ssl: 'Use SSL',
    target_directory: 'Target Directory (as AirDC++ sees it)',
    local_target_directory: 'Target Directory (as CLU sees it)',
    hub_urls: 'Hub URLs (optional, comma-separated)',
  };
  const CLIENT_SECRETS = ['api_key', 'password'];
  const dl = { clients: {}, sources: [], tested: {} };

  function splitList(value) {
    return String(value || '').split(',').map(function (s) { return s.trim(); }).filter(Boolean);
  }

  async function saveSetting(key, value) {
    const body = {};
    body[key] = value;
    const data = await api(URLS.settings, { method: 'POST', body: body });
    settingValues = data.settings || settingValues;
    return settingValues[key];
  }

  /**
   * A list of switches, the ones that are on first and in order (with up and
   * down buttons), then the ones that are off. `onChange(order)` gets the new
   * order of the ones that are on.
   */
  function renderOrderedSwitches(container, labels, order, onChange) {
    container.replaceChildren();
    const off = Object.keys(labels).filter(function (k) { return order.indexOf(k) === -1; });
    order.concat(off).forEach(function (key) {
      const on = order.indexOf(key) !== -1;
      const i = order.indexOf(key);
      const item = el('div', 'list-group-item d-flex align-items-center gap-2 py-1');
      const sw = el('input', 'form-check-input m-0');
      sw.type = 'checkbox';
      sw.setAttribute('role', 'switch');
      sw.checked = on;
      sw.setAttribute('aria-label', labels[key]);
      sw.addEventListener('change', function () {
        const next = order.filter(function (k) { return k !== key; });
        if (sw.checked) next.push(key);
        if (!next.length) {
          sw.checked = true;
          showError('At least one must stay on.');
          return;
        }
        onChange(next);
      });
      item.appendChild(sw);
      item.appendChild(el('span', 'flex-grow-1' + (on ? '' : ' text-muted'),
        on ? (i + 1) + '. ' + labels[key] : labels[key] + ' (off)'));
      if (on) {
        [[-1, 'bi-arrow-up', 'Move up'], [1, 'bi-arrow-down', 'Move down']].forEach(function (m) {
          const b = el('button', 'btn btn-sm btn-outline-secondary py-0');
          b.type = 'button';
          b.setAttribute('aria-label', m[2] + ': ' + labels[key]);
          b.appendChild(el('i', 'bi ' + m[1]));
          const j = i + m[0];
          b.disabled = j < 0 || j >= order.length;
          b.addEventListener('click', function () {
            const next = order.slice();
            next[i] = next[j];
            next[j] = key;
            onChange(next);
          });
          item.appendChild(b);
        });
      }
      container.appendChild(item);
    });
  }

  function renderSources() {
    document.querySelectorAll('.dl-src').forEach(function (box) {
      box.checked = dl.sources.indexOf(box.value) !== -1;
    });
    document.querySelectorAll('[data-source-panel]').forEach(function (p) {
      p.classList.toggle('d-none', dl.sources.indexOf(p.dataset.sourcePanel) === -1);
    });
    renderOrderedSwitches($('dl-order'), SOURCE_LABELS, dl.sources, saveSources);
    const st = state.status.downloads;
    const attention = st && st.status === 'needs_attention' ? st.message : '';
    $('dl-attention').classList.toggle('d-none', !attention);
    $('dl-attention').textContent = attention;
  }

  async function saveSources(order) {
    try {
      dl.sources = splitList(await saveSetting('download_source_priority', order.join(',')));
    } catch (err) {
      showError('Could not save the download sources: ' + err.message);
    }
    await refreshStatus();
    renderSources();
  }

  function renderMirrors() {
    renderOrderedSwitches($('dl-mirrors'), MIRROR_LABELS,
      splitList(settingValues.download_provider_priority), async function (order) {
        try {
          await saveSetting('download_provider_priority', order.join(','));
        } catch (err) {
          showError('Could not save the mirror order: ' + err.message);
        }
        renderMirrors();
      });
  }

  // ── clients (Usenet: SABnzbd/NZBGet, DC++: AirDC++) ──

  function clientBox(group) {
    return document.querySelector('[data-source-panel="' + group + '"]');
  }

  function groupPart(group, cls) {
    return clientBox(group).querySelector('.' + cls + '[data-group="' + group + '"]');
  }

  /** The client type the group's form is for. */
  function clientType(group) {
    if (group === 'dcpp') return 'airdcpp';
    const checked = document.querySelector('input[name="dl-usenet-type"]:checked');
    return checked ? checked.value : 'sabnzbd';
  }

  function clientResult(group, ok, text) {
    const r = groupPart(group, 'dl-client-result');
    r.className = 'align-self-center dl-client-result ' + (ok ? 'text-success' : 'text-danger');
    r.textContent = text || '';
  }

  function renderClientBadge(group) {
    const b = groupPart(group, 'dl-client-badge');
    const active = Object.values(dl.clients).find(function (c) {
      return c.client_group === group && c.is_active;
    });
    if (active) { b.className = 'badge ms-2 dl-client-badge bg-success'; b.textContent = active.name + ' in use'; }
    else { b.className = 'badge ms-2 dl-client-badge bg-secondary'; b.textContent = 'Not set'; }
  }

  function clientField(client, field) {
    const id = 'dl-' + client.type + '-' + field;
    const cfg = client.config || {};
    const label = CLIENT_FIELD_LABELS[field] || field;
    if (field === 'use_ssl') {
      const wrap = el('div', 'form-check mb-2');
      const box = el('input', 'form-check-input');
      box.type = 'checkbox';
      box.id = id;
      box.dataset.field = field;
      box.checked = !!cfg.use_ssl;
      wrap.appendChild(box);
      const l = el('label', 'form-check-label small', label);
      l.htmlFor = id;
      wrap.appendChild(l);
      return wrap;
    }
    const wrap = el('div', 'mb-2');
    const l = el('label', 'form-label small mb-1', label);
    l.htmlFor = id;
    wrap.appendChild(l);
    const group = el('div', 'input-group input-group-sm');
    const input = el('input', 'form-control');
    input.id = id;
    input.dataset.field = field;
    input.autocomplete = 'off';
    const secret = CLIENT_SECRETS.indexOf(field) !== -1;
    input.type = secret ? 'password' : (field === 'port' || field === 'priority' ? 'number' : 'text');
    if (secret) {
      // Secrets never come back from the server; blank keeps the saved one.
      if ((client.secrets_set || []).indexOf(field) !== -1) input.placeholder = 'Saved (leave blank to keep)';
    } else if (cfg[field] != null) {
      input.value = String(cfg[field]);
    }
    group.appendChild(input);
    if (field === 'local_target_directory') {
      const browse = el('button', 'btn btn-outline-secondary');
      browse.type = 'button';
      browse.appendChild(el('i', 'bi bi-folder2-open'));
      browse.appendChild(el('span', 'visually-hidden', 'Browse'));
      browse.addEventListener('click', function () {
        openBrowser(function (path) {
          input.value = path;
          input.dispatchEvent(new Event('input', { bubbles: true }));
        });
      });
      group.appendChild(browse);
    }
    wrap.appendChild(group);
    return wrap;
  }

  function renderClientForm(group) {
    const type = clientType(group);
    const client = dl.clients[type];
    const fields = groupPart(group, 'dl-client-fields');
    fields.replaceChildren();
    if (!client) {
      fields.appendChild(el('div', 'small text-danger mb-2', 'This client is not available.'));
      return;
    }
    const row = el('div', 'row g-2');
    client.config_fields.forEach(function (f) {
      const col = el('div', f === 'use_ssl' || f.indexOf('target_directory') !== -1 || f === 'hub_urls'
        ? 'col-12' : 'col-sm-6');
      col.appendChild(clientField(client, f));
      row.appendChild(col);
    });
    fields.appendChild(row);
    dl.tested[group] = false;
    groupPart(group, 'dl-client-save').disabled = true;
    clientResult(group, true, '');
  }

  /** The values in the group's form; a blank field keeps the stored value. */
  function collectClient(group) {
    const body = {};
    groupPart(group, 'dl-client-fields').querySelectorAll('[data-field]').forEach(function (input) {
      if (input.type === 'checkbox') { body[input.dataset.field] = input.checked; return; }
      const v = input.value.trim();
      if (v) body[input.dataset.field] = input.type === 'number' ? Number(v) : v;
    });
    return body;
  }

  async function loadClients() {
    try {
      dl.clients = {};
      ((await api(URLS.downloadClients)).clients || []).forEach(function (c) { dl.clients[c.type] = c; });
    } catch (err) {
      showError('Could not load the download clients: ' + err.message);
    }
    // Show the client in use, if there is one.
    const usenet = Object.values(dl.clients).find(function (c) {
      return c.client_group === 'usenet' && c.is_active;
    });
    if (usenet && $('dl-usenet-' + usenet.type)) $('dl-usenet-' + usenet.type).checked = true;
    ['usenet', 'dcpp'].forEach(function (g) {
      renderClientForm(g);
      renderClientBadge(g);
    });
  }

  async function testClient(group) {
    const btn = groupPart(group, 'dl-client-test');
    btn.disabled = true;
    clientResult(group, true, 'Testing...');
    try {
      const data = await api(url('clientTest', { __TYPE__: clientType(group) }),
        { method: 'POST', body: collectClient(group), allowError: true });
      dl.tested[group] = !!data.valid;
      clientResult(group, !!data.valid,
        data.valid ? 'Connected. Save to use it.' : (data.error || 'The test failed.'));
    } catch (err) {
      dl.tested[group] = false;
      clientResult(group, false, err.message);
    } finally {
      btn.disabled = false;
      groupPart(group, 'dl-client-save').disabled = !dl.tested[group];
    }
  }

  async function saveClient(group) {
    const type = clientType(group);
    const save = groupPart(group, 'dl-client-save');
    save.disabled = true;
    try {
      await api(url('clientConfig', { __TYPE__: type }), { method: 'POST', body: collectClient(group) });
      await api(url('clientActivate', { __TYPE__: type }), { method: 'POST' });
      // It passed a test just now; record that against the saved row too.
      await api(url('clientTest', { __TYPE__: type }), { method: 'POST', allowError: true });
      showSuccess((dl.clients[type] ? dl.clients[type].name : type) + ' saved and in use.');
      await loadClients();
      clientResult(group, true, 'Saved.');
    } catch (err) {
      clientResult(group, false, err.message);
      save.disabled = false;
    }
    await refreshStatus();
    renderSources();
  }

  // ── indexers ──

  function indexerResult(ok, text) {
    const r = $('dl-idx-result');
    r.className = 'align-self-center ' + (ok ? 'text-success' : 'text-danger');
    r.textContent = text || '';
  }

  function indexerBody() {
    return { name: $('dl-idx-name').value.trim(), url: $('dl-idx-url').value.trim(),
             api_key: $('dl-idx-key').value.trim(), categories: $('dl-idx-cats').value.trim() };
  }

  async function loadIndexers() {
    const list = $('dl-indexers');
    try {
      const indexers = (await api(URLS.indexers)).indexers || [];
      list.replaceChildren();
      if (!indexers.length) list.appendChild(el('div', 'list-group-item small text-muted', 'No indexers yet.'));
      indexers.forEach(function (ix) {
        const item = el('div', 'list-group-item d-flex gap-2 align-items-center');
        const ok = ix.enabled && ix.is_valid;
        item.appendChild(el('i', 'bi ' + (ok ? 'bi-check-circle-fill text-success'
          : 'bi-exclamation-triangle-fill text-warning')));
        const body = el('div', 'flex-grow-1');
        body.appendChild(el('div', 'fw-semibold', ix.name + (ix.enabled ? '' : ' (disabled)')));
        body.appendChild(el('div', 'small text-muted text-break', ix.url));
        item.appendChild(body);
        list.appendChild(item);
      });
    } catch (err) {
      list.replaceChildren(el('div', 'list-group-item small text-danger',
        'Could not load the indexers: ' + err.message));
    }
  }

  async function testIndexer() {
    const body = indexerBody();
    if (!body.url) { indexerResult(false, 'Enter the indexer URL.'); return; }
    const btn = $('dl-idx-test');
    btn.disabled = true;
    indexerResult(true, 'Testing...');
    try {
      const data = await api(URLS.indexerTest, { method: 'POST', body: body, allowError: true });
      if (data.valid) indexerResult(true, 'Connected. Add it to keep it.');
      else indexerResult(false, data.error || 'The test failed.');
      $('dl-idx-add').disabled = !data.valid;
    } catch (err) {
      indexerResult(false, err.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function addIndexer() {
    const body = indexerBody();
    if (!body.name) body.name = body.url.replace(/^https?:\/\//, '').replace(/\/.*$/, '');
    const btn = $('dl-idx-add');
    btn.disabled = true;
    try {
      const data = await api(URLS.indexers, { method: 'POST', body: body });
      // It passed a test a moment ago: run the saved test so the row says so.
      if (data.id) {
        await api(URLS.indexers + '/' + encodeURIComponent(data.id) + '/test',
          { method: 'POST', allowError: true });
      }
      ['dl-idx-name', 'dl-idx-url', 'dl-idx-key', 'dl-idx-cats'].forEach(function (id) { $(id).value = ''; });
      indexerResult(true, 'Added.');
      showSuccess('Indexer ' + body.name + ' added.');
    } catch (err) {
      indexerResult(false, err.message);
      btn.disabled = false;
    }
    await loadIndexers();
    await refreshStatus();
    renderSources();
  }

  // ── GetComics dry run ──

  async function runSimulation() {
    const btn = $('dl-simulate');
    const out = $('dl-simulate-result');
    btn.disabled = true;
    out.className = 'small mt-2 text-muted';
    out.textContent = 'Searching...';
    try {
      const data = await api(URLS.simulate, { method: 'POST', body: { limit: 1 } });
      const s = data.summary || {};
      out.className = 'small mt-2';
      out.replaceChildren();
      const series = (data.simulation || [])[0];
      out.appendChild(el('div', null, (series ? series.series + ': ' : '') +
        (s.total_issues_searched || 0) + ' missing issue(s) searched, ' +
        (s.accept_count || 0) + ' would download, ' +
        (s.fallback_count || 0) + ' only as part of a pack, ' +
        (s.no_match_count || 0) + ' found nothing usable.'));
      if (!s.total_issues_searched) {
        out.appendChild(el('div', 'text-muted', 'Nothing is missing from the first subscribed series.'));
      }
    } catch (err) {
      out.className = 'small mt-2 text-danger';
      out.textContent = 'The dry run failed: ' + err.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function enterDownloads() {
    refreshStatus().then(renderSources);
    await loadWizardSettings(stepNode('downloads'));
    dl.sources = splitList(settingValues.download_source_priority);
    renderSources();
    renderMirrors();
    loadClients();
    loadIndexers();
  }

  function initDownloads() {
    document.querySelectorAll('.dl-src').forEach(function (box) {
      box.addEventListener('change', function () {
        const order = dl.sources.filter(function (s) { return s !== box.value; });
        if (box.checked) order.push(box.value);
        if (!order.length) {
          box.checked = true;
          showError('At least one download source must stay on.');
          return;
        }
        saveSources(order);
      });
    });
    document.querySelectorAll('input[name="dl-usenet-type"]').forEach(function (r) {
      r.addEventListener('change', function () { renderClientForm('usenet'); });
    });
    ['usenet', 'dcpp'].forEach(function (group) {
      groupPart(group, 'dl-client-test').addEventListener('click', function () { testClient(group); });
      groupPart(group, 'dl-client-save').addEventListener('click', function () { saveClient(group); });
      // Editing the form means the last test no longer applies to it.
      groupPart(group, 'dl-client-fields').addEventListener('input', function () {
        dl.tested[group] = false;
        groupPart(group, 'dl-client-save').disabled = true;
        clientResult(group, true, '');
      });
      groupPart(group, 'dl-client-fields').addEventListener('change', function (e) {
        if (e.target.type !== 'checkbox') return;
        dl.tested[group] = false;
        groupPart(group, 'dl-client-save').disabled = true;
      });
    });
    ['dl-idx-url', 'dl-idx-key', 'dl-idx-cats'].forEach(function (id) {
      $(id).addEventListener('input', function () { $('dl-idx-add').disabled = true; indexerResult(true, ''); });
    });
    $('dl-idx-test').addEventListener('click', testIndexer);
    $('dl-idx-add').addEventListener('click', addIndexer);
    if ($('dl-simulate')) $('dl-simulate').addEventListener('click', runSimulation);
  }

  // ── Schedules ──────────────────────────────────────────────────────────

  // Every row stores its time in UTC; the inputs show the user's offset
  // (CLU.tzOffsetHours() reads the #timezone select on this step).
  const SCHEDULES = [
    { id: 'rebuild', label: 'Rebuild the file index', get: 'schedRebuildGet', save: 'schedRebuildSave',
      recommended: true },
    { id: 'sync', label: 'Check pull list series for new issues (needs Metron)',
      get: 'schedSyncGet', save: 'schedSyncSave', recommended: true },
    { id: 'scrape', label: 'Refresh the GetComics search index', get: 'schedScrapeGet', save: 'schedScrapeSave' },
    { id: 'getcomics', label: 'Download missing issues from GetComics',
      get: 'schedGetcomicsGet', save: 'schedGetcomicsSave' },
    { id: 'reading_list', label: 'Sync reading lists from Metron',
      get: 'schedReadingListGet', save: 'schedReadingListSave' },
  ];
  const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
  const schedules = {};  // id -> {frequency, time (UTC), weekday}

  function scheduleRow(def) {
    const tr = el('tr');
    tr.dataset.schedule = def.id;
    const name = el('td');
    name.appendChild(el('div', null, def.label));
    name.appendChild(el('div', 'small text-muted sched-next'));
    tr.appendChild(name);

    const freq = el('select', 'form-select form-select-sm sched-frequency');
    freq.setAttribute('aria-label', def.label + ': how often');
    [['disabled', 'Off'], ['daily', 'Daily'], ['weekly', 'Weekly']].forEach(function (o) {
      freq.appendChild(new Option(o[1], o[0]));
    });
    const time = el('input', 'form-control form-control-sm sched-time');
    time.type = 'time';
    time.setAttribute('aria-label', def.label + ': time');
    const day = el('select', 'form-select form-select-sm sched-weekday d-none');
    day.setAttribute('aria-label', def.label + ': day');
    freq.style.minWidth = '6.5rem';
    time.style.minWidth = '7.5rem';
    day.style.minWidth = '8rem';
    WEEKDAYS.forEach(function (d, i) { day.appendChild(new Option(d, String(i))); });
    [freq, time, day].forEach(function (input) {
      const td = el('td');
      td.appendChild(input);
      tr.appendChild(td);
      input.addEventListener('change', function () { saveSchedule(def, tr); });
    });
    tr.appendChild(el('td', 'small sched-result text-nowrap'));
    return tr;
  }

  function renderSchedule(def, tr, data) {
    const s = data.schedule || {};
    schedules[def.id] = { frequency: s.frequency || 'disabled', time: s.time || '03:00',
                          weekday: Number(s.weekday) || 0 };
    const st = schedules[def.id];
    tr.querySelector('.sched-frequency').disabled = false;
    tr.querySelector('.sched-weekday').disabled = false;
    tr.querySelector('.sched-frequency').value = st.frequency;
    tr.querySelector('.sched-time').value = CLU.utcToLocal(st.time, CLU.tzOffsetHours());
    tr.querySelector('.sched-weekday').value = String(st.weekday);
    tr.querySelector('.sched-time').disabled = st.frequency === 'disabled';
    tr.querySelector('.sched-weekday').classList.toggle('d-none', st.frequency !== 'weekly');
    tr.querySelector('.sched-next').textContent =
      st.frequency === 'disabled' ? '' : 'Next run: ' + (data.next_run || 'not scheduled');
  }

  async function loadSchedule(def) {
    const tr = $('sched-rows').querySelector('tr[data-schedule="' + def.id + '"]');
    try {
      renderSchedule(def, tr, await api(URLS[def.get]));
    } catch (err) {
      // Not knowing the stored time, a save from this row could only guess it.
      tr.querySelectorAll('select, input').forEach(function (i) { i.disabled = true; });
      tr.querySelector('.sched-next').textContent = 'Could not load this schedule: ' + err.message;
    }
  }

  function loadSchedules() {
    return Promise.all(SCHEDULES.map(loadSchedule));
  }

  async function postSchedule(def, body) {
    await api(URLS[def.save], { method: 'POST', body: body });
    await loadSchedule(def);
  }

  async function saveSchedule(def, tr) {
    const result = tr.querySelector('.sched-result');
    const local = tr.querySelector('.sched-time').value;
    if (!/^\d{2}:\d{2}$/.test(local)) {
      result.className = 'small sched-result text-danger';
      result.textContent = 'Enter a time.';
      return;
    }
    result.className = 'small sched-result text-muted';
    result.textContent = 'Saving...';
    try {
      await postSchedule(def, {
        frequency: tr.querySelector('.sched-frequency').value,
        time: CLU.localToUtc(local, CLU.tzOffsetHours()),
        weekday: parseInt(tr.querySelector('.sched-weekday').value, 10) || 0,
      });
      result.className = 'small sched-result text-success';
      result.textContent = 'Saved';
      refreshStatus();
    } catch (err) {
      result.className = 'small sched-result text-danger';
      result.textContent = '';
      showError('Could not save the schedule: ' + err.message);
      loadSchedule(def);
    }
  }

  /** Turn the recommended jobs on daily, at the time each already has. */
  async function applyRecommendedSchedules() {
    const btn = $('sched-recommended');
    btn.disabled = true;
    try {
      for (const def of SCHEDULES.filter(function (d) { return d.recommended; })) {
        const st = schedules[def.id];
        if (!st) throw new Error('"' + def.label + '" could not be loaded');
        // st.time is already UTC: no conversion, so a seeded off-peak time is kept.
        await postSchedule(def, { frequency: 'daily', time: st.time, weekday: st.weekday });
      }
      showSuccess('The file index rebuild and series sync now run daily.');
      refreshStatus();
    } catch (err) {
      showError('Could not apply the defaults: ' + err.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function loadWeeklyPacksNote() {
    try {
      const cfg = (await api(URLS.weeklyPacks)).config || {};
      $('sched-packs-note').classList.toggle('d-none', !cfg.enabled);
    } catch (err) {
      /* the note is advice; leave it hidden */
    }
  }

  async function enterSchedules() {
    // The timezone first: every time below is converted with it.
    await loadWizardSettings(stepNode('schedules'));
    loadSchedules();
    loadWeeklyPacksNote();
  }

  function initSchedules() {
    const rows = $('sched-rows');
    SCHEDULES.forEach(function (def) { rows.appendChild(scheduleRow(def)); });
    $('sched-recommended').addEventListener('click', applyRecommendedSchedules);
    // A new offset changes what every row shows (and its next run), not what
    // is stored: reload from the server once the timezone save settles.
    $('timezone').addEventListener('setup:saved', loadSchedules);
  }

  // ── Users ──────────────────────────────────────────────────────────────

  function usersDetails() {
    return (state.status.users && state.status.users.details) || {};
  }

  function renderUsers() {
    const d = usersDetails();
    const info = $('users-state');
    const msg = state.status.users && state.status.users.message;
    info.classList.toggle('d-none', !msg);
    info.textContent = msg || '';
    if (d.count > 1) $('users-others-yes').checked = true;
    const shared = $('users-others-yes').checked;
    $('users-solo').classList.toggle('d-none', !$('users-others-no').checked);
    $('users-shared').classList.toggle('d-none', !shared);
    // Login with a passwordless owner would lock everyone out, so the second
    // account waits for the owner's password (admin.create_user_route agrees).
    $('users-owner-box').classList.toggle('d-none', !d.needs_setup);
    $('users-add').disabled = !!d.needs_setup;
    if (d.needs_setup && !$('users-owner-name').value) $('users-owner-name').value = d.username || '';
  }

  async function enterUsers() {
    await refreshStatus();
    renderUsers();
  }

  async function saveOwner() {
    const username = $('users-owner-name').value.trim();
    const password = $('users-owner-pass').value;
    if (!password) { showError('Enter a password.'); return; }
    const btn = $('users-owner-save');
    btn.disabled = true;
    try {
      await api(URLS.setupOwner, { method: 'POST', body: { username: username, password: password } });
      $('users-owner-pass').value = '';
      showSuccess('Owner account saved.');
    } catch (err) {
      showError('Could not save the owner account: ' + err.message);
    } finally {
      btn.disabled = false;
    }
    await enterUsers();
  }

  async function addAccount() {
    const username = $('users-new-name').value.trim();
    const password = $('users-new-pass').value;
    if (!username || !password) { showError('Enter a username and a password.'); return; }
    const btn = $('users-add');
    btn.disabled = true;
    try {
      const data = await api(URLS.adminUsers, { method: 'POST', body: {
        username: username, password: password, role: $('users-new-role').value } });
      $('users-new-name').value = '';
      $('users-new-pass').value = '';
      showSuccess('Account ' + ((data.user && data.user.username) || username) +
        ' added. Login is now on; you stay logged in on this browser.');
    } catch (err) {
      showError('Could not add the account: ' + err.message);
    }
    await enterUsers();
  }

  function initUsers() {
    document.querySelectorAll('input[name="users-others"]').forEach(function (r) {
      r.addEventListener('change', renderUsers);
    });
    $('users-owner-save').addEventListener('click', saveOwner);
    $('users-add').addEventListener('click', addAccount);
  }

  // ── Notifications ──────────────────────────────────────────────────────

  function notifyResult(ok, text) {
    const r = $('notify-result');
    r.className = 'align-self-center ' + (ok ? 'text-success' : 'text-danger');
    r.textContent = text || '';
  }

  async function testNotifications() {
    const btn = $('notify-test');
    btn.disabled = true;
    notifyResult(true, 'Sending...');
    try {
      const data = await api(URLS.notifyTest, { method: 'POST', body: { urls: $('notify-urls').value } });
      notifyResult(true, data.message || 'Sent.');
    } catch (err) {
      notifyResult(false, err.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function saveNotifications() {
    // The endpoint rewrites all three settings, so all three are sent.
    const events = {};
    document.querySelectorAll('.notify-event').forEach(function (b) {
      events[b.dataset.eventId] = b.checked;
    });
    const btn = $('notify-save');
    btn.disabled = true;
    try {
      const data = await api(URLS.notifySave, { method: 'POST', body: {
        enabled: $('notify-enabled').checked, urls: $('notify-urls').value, events: events } });
      notifyResult(true, data.message || 'Saved.');
      refreshStatus();
    } catch (err) {
      notifyResult(false, err.message);
    } finally {
      btn.disabled = false;
    }
  }

  function initNotifications() {
    $('notify-test').addEventListener('click', testNotifications);
    $('notify-save').addEventListener('click', saveNotifications);
  }

  // ── Personalization ────────────────────────────────────────────────────

  async function saveTheme() {
    const btn = $('theme-save');
    btn.disabled = true;
    try {
      await api(URLS.styling, { method: 'POST', body: { bootstrapTheme: $('setup-theme').value } });
      showSuccess('Theme saved.');
      $('theme-reload').classList.remove('d-none');
      refreshStatus();
    } catch (err) {
      showError('Could not save the theme: ' + err.message);
    } finally {
      btn.disabled = false;
    }
  }

  function initPersonalization() {
    CLU.initThemePreview({ selectId: 'setup-theme', imgId: 'setup-theme-img', nameId: 'setup-theme-name' });
    $('theme-save').addEventListener('click', saveTheme);
    $('theme-reload').addEventListener('click', async function () {
      // Come back to this step, not to wherever Next last pointed.
      await saveState({ last_step: 'personalization' });
      window.location.reload();
    });
  }

  // ── Mylar3 import ──────────────────────────────────────────────────────

  function prereq(id, ok) {
    const icon = document.querySelector('#mylar-prereqs [data-prereq="' + id + '"] i');
    if (!icon) return;
    icon.className = 'bi me-1 ' + (ok ? 'bi-check-circle-fill text-success'
      : 'bi-exclamation-triangle-fill text-warning');
  }

  async function enterMylarImport() {
    await refreshStatus();
    const libs = state.status.libraries;
    prereq('libraries', libs && libs.status === 'configured');
    const meta = state.status.metadata;
    prereq('metadata', meta && meta.status === 'configured');
    const s = state.status.mylar_import;
    const b = $('mylar-last-badge');
    if (!s || !s.status) return;
    if (s.status === 'unconfigured') {
      b.className = 'badge bg-secondary';
      b.textContent = 'Not run';
    } else badge(b, s.status);
    let message = s.message || '';
    const finished = s.details && s.details.finished_at;
    if (finished) message += ' (' + new Date(finished * 1000).toLocaleString() + ')';
    $('mylar-last-message').textContent = message;
  }

  function initMylarImport() {
    $('mylar-open').addEventListener('click', async function () {
      // The importer's "Back to Guided Setup" lands on whatever last_step says.
      this.disabled = true;
      await saveState({ last_step: 'mylar_import' });
      window.location.href = URLS.mylarPage;
    });
  }

  // ── Pull List scan ─────────────────────────────────────────────────────

  async function enterPullListScan() {
    await refreshStatus();
    const s = state.status.pull_list_scan;
    const current = $('pls-current');
    current.textContent = s ? s.message : '';
    current.className = 'small mb-3 ' + (s && s.status === 'needs_attention' ? 'text-warning' : 'text-muted');
  }

  function renderPullScanResult(opId, result) {
    const counts = $('pls-counts');
    counts.replaceChildren();
    const r = result || {};
    const review = r.review_total || 0;
    [
      [(r.applied || []).length, 'added to the Pull List'],
      [review, 'need review'],
      [r.skipped_total || 0, 'skipped'],
      [r.errors_total || 0, 'could not be read'],
    ].forEach(function (pair) {
      counts.appendChild(el('li', null, pair[0] + ' folder' + (pair[0] === 1 ? '' : 's') + ' ' + pair[1]));
    });
    $('pls-review').classList.toggle('d-none', !review);
    $('pls-review-link').href = URLS.pullList + '?automap_op=' + encodeURIComponent(opId);
    $('pls-result').classList.remove('d-none');
  }

  async function startPullScan() {
    const btn = $('pls-start');
    btn.disabled = true;
    $('pls-result').classList.add('d-none');
    $('pls-progress-wrap').classList.remove('d-none');
    $('pls-bar').style.width = '0%';
    $('pls-detail').textContent = 'Starting...';
    let opId;
    try {
      opId = (await api(URLS.pullScan, { method: 'POST' })).op_id;
    } catch (err) {
      btn.disabled = false;
      $('pls-progress-wrap').classList.add('d-none');
      showError('Could not start the scan: ' + err.message);
      return;
    }
    function finish() {
      stopPoll('pullscan');
      btn.disabled = false;
      $('pls-progress-wrap').classList.add('d-none');
    }
    // Not a registered operation: the scan keeps its own job store.
    stopPoll('pullscan');
    state.pollers.pullscan = CLU.startPoll(async function (signal) {
      const data = await api(URLS.pullScanStatus + '?op_id=' + encodeURIComponent(opId),
        { signal: signal, allowError: true });
      if (!data.success) {
        // 404: the finished job has been pruned.
        finish();
        showWarning(data.error || 'The scan finished; see the Pull List for the result.');
        enterPullListScan();
        return;
      }
      if (data.status === 'done') {
        finish();
        renderPullScanResult(opId, data.result);
        enterPullListScan();
      } else if (data.status === 'error') {
        finish();
        showError('The scan failed: ' + (data.error || 'unknown error'));
      } else {
        $('pls-bar').style.width = (data.total ? Math.round(100 * data.current / data.total) : 0) + '%';
        $('pls-detail').textContent = data.total
          ? data.current + ' / ' + data.total + ' - ' + (data.detail || '')
          : (data.detail || 'Scanning...');
      }
    }, 1500);
  }

  function initPullListScan() {
    $('pls-start').addEventListener('click', startPullScan);
  }

  // ── Repair tour ────────────────────────────────────────────────────────

  const HEALTH_LEVEL = {
    green: { text: 'Healthy', cls: 'bg-success' },
    amber: { text: 'Some issues', cls: 'bg-warning text-dark' },
    red:   { text: 'Damaged files found', cls: 'bg-danger' },
  };

  function renderHealth(s) {
    $('repair-enabled').checked = !!s.enabled;
    $('repair-interval').value = s.interval_hours || '';
    if (!s.last_run_at) { $('repair-summary').classList.add('d-none'); return; }
    const level = HEALTH_LEVEL[s.level] || HEALTH_LEVEL.green;
    const b = $('repair-level');
    b.className = 'badge ' + level.cls;
    b.textContent = level.text;
    const dups = s.duplicates || {};
    const cov = s.coverage || {};
    const counts = $('repair-counts');
    counts.replaceChildren();
    [
      'Checked so far: ' + (cov.checked || 0) + ' of ' + (cov.total || 0) + ' comics (' + (cov.pct || 0) + '%)',
      'Damaged or unreadable: ' + (s.integrity_open || 0),
      'Orphaned files: ' + (s.orphans_open || 0),
      'Empty folders: ' + (s.empty_folders_open || 0),
      'series.json problems: ' + (s.series_json_open || 0),
      'Duplicate groups: ' + (dups.exact || 0) + ' exact, ' + (dups.likely || 0) + ' likely',
    ].forEach(function (line) { counts.appendChild(el('li', null, line)); });
    $('repair-summary').classList.remove('d-none');
  }

  async function loadHealth() {
    try {
      const s = await api(URLS.healthSummary);
      renderHealth(s);
      return s;
    } catch (err) {
      showError('Could not read the health scan: ' + err.message);
      return null;
    }
  }

  async function saveHealth(field, value) {
    const body = {};
    body[field] = value;
    try {
      const data = await api(URLS.healthSettings, { method: 'POST', body: body });
      $('repair-enabled').checked = !!data.enabled;
      $('repair-interval').value = data.interval_hours;
      refreshStatus();
    } catch (err) {
      showError('Could not save: ' + err.message);
      loadHealth();
    }
  }

  function scanRunning(running) {
    $('repair-scan').disabled = running;
    $('repair-progress-wrap').classList.toggle('d-none', !running);
  }

  /** Follow a scan this step did not start (the schedule, Problem Files). */
  function followHealthSummary() {
    scanRunning(true);
    $('repair-detail').textContent = 'A scan is running...';
    stopPoll('health');
    state.pollers.health = CLU.startPoll(async function (signal) {
      const s = await api(URLS.healthSummary, { signal: signal });
      if (!s.running) {
        stopPoll('health');
        scanRunning(false);
        renderHealth(s);
        refreshStatus();
      }
    }, 3000);
  }

  async function runHealthScan() {
    scanRunning(true);
    $('repair-detail').textContent = 'Starting...';
    let data;
    try {
      data = await api(URLS.healthScan, { method: 'POST', allowError: true });
    } catch (err) {
      scanRunning(false);
      showError('Could not start the scan: ' + err.message);
      return;
    }
    if (!data.success) {
      // A 409 means a scan is already running: follow that one instead.
      const s = await loadHealth();
      if (s && s.running) { followHealthSummary(); return; }
      scanRunning(false);
      showError('Could not start the scan: ' + (data.error || 'unknown error'));
      return;
    }
    followOperation('health', data.op_id, function (op) {
      $('repair-detail').textContent = op.detail || 'Scanning...';
    }, function (ok) {
      scanRunning(false);
      if (!ok) showError('The health scan failed. See app.log.');
      loadHealth();
      refreshStatus();
    });
  }

  async function enterRepair() {
    const s = await loadHealth();
    if (s && s.running && !state.pollers.health) followHealthSummary();
  }

  function initRepair() {
    $('repair-enabled').addEventListener('change', function () {
      saveHealth('enabled', this.checked);
    });
    $('repair-interval').addEventListener('change', function () {
      const n = parseInt(this.value, 10);
      if (!(n >= 1 && n <= 168)) {
        showError('Enter a number of hours from 1 to 168.');
        loadHealth();
        return;
      }
      saveHealth('interval_hours', n);
    });
    $('repair-scan').addEventListener('click', runHealthScan);
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
          const section = SECTIONS.find(function (x) { return x.id === s.id; });
          if (stepIdx === -1 && section && section.usecases.length) {
            // Not on this path: add it, keeping the canonical order.
            const usecase = section.usecases[0];
            state.path = state.path.concat(usecase);
            // Tick it too, or the next change on the use-case step drops it again.
            const box = document.querySelector('#usecase-options input[value="' + usecase + '"]');
            if (box) box.checked = true;
            computeSteps();
            saveState({ path: state.path });
          }
          if (state.steps.indexOf(s.id) !== -1) goTo(state.steps.indexOf(s.id));
        });
        actions.appendChild(go);
      }
      // A section that lives on its own page links there rather than to Settings.
      const a = el('a', 'btn btn-sm btn-outline-secondary', s.link ? 'Open page' : 'Settings');
      a.href = s.link || settingsLink(s);
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
    mylar_import: enterMylarImport,
    pull_list_scan: enterPullListScan,
    folder_monitoring: enterFolderMonitoring,
    downloads: enterDownloads,
    schedules: enterSchedules,
    users: enterUsers,
    repair: enterRepair,
    summary: enterSummary,
  };

  // ── boot ───────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    computeSteps();
    initUsecase();
    initBrowser();
    initSettings();
    initMetadata();
    initMylarImport();
    initPullListScan();
    initFolderMonitoring();
    initDownloads();
    initSchedules();
    initUsers();
    initNotifications();
    initPersonalization();
    initRepair();

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
