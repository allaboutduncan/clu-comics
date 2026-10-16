/**
 * CLU metadata tools  –  clu-metadata-tools.js
 *
 * Folder actions that sit on top of the bulk-metadata job:
 *   - Re-tag with provider (#626): show which providers tagged the folder, then
 *     re-tag from one chosen provider, optionally replacing existing tags.
 *   - Backfill missing fields (#579): fill only blank tags from the provider
 *     that tagged each file.
 * Provides: CLU.openRetagModal, CLU.openRetagFilesModal, CLU.openBackfillModal
 *
 * Depends on: clu-utils.js (CLU.showError), bulk-metadata-review.js
 *             (BulkMetadataReview.startJob)
 *
 * DOM contracts:
 *   #retagModal, #backfillModal  (from modal_metadata_tools.html)
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  var PROVIDER_LABELS = {
    metron: 'Metron',
    comicvine: 'ComicVine',
    comicvine_sqlite: 'ComicVine (Local DB)',
    gcd: 'GCD',
    gcd_api: 'GCD REST API',
    inducks: 'INDUCKS',
    anilist: 'AniList',
    bedetheque: 'Bedetheque',
    mangadex: 'MangaDex',
    mangaupdates: 'MangaUpdates',
    manual: 'Manual'
  };
  // Providers that can fill a series (a re-tag needs series + issue search).
  var UNTAGGED = 'untagged';

  function label(id) { return PROVIDER_LABELS[id] || id; }

  function el(id) { return document.getElementById(id); }

  function showModal(id) {
    var m = el(id);
    if (m) bootstrap.Modal.getOrCreateInstance(m).show();
  }

  function hideModal(id) {
    var m = el(id);
    var inst = m && bootstrap.Modal.getInstance(m);
    if (inst) inst.hide();
  }

  function addText(parent, tag, text, cls) {
    var node = document.createElement(tag);
    node.textContent = text;
    if (cls) node.className = cls;
    parent.appendChild(node);
    return node;
  }

  // ---- Re-tag ------------------------------------------------------------

  var _retag = null; // { path, libraryId, onDone, mix }

  function renderMix(mix) {
    var box = el('retagMix');
    box.textContent = '';
    var parts = [];
    Object.keys(mix.providers).sort().forEach(function (p) {
      parts.push([label(p), mix.providers[p]]);
    });
    if (mix.untagged) parts.push(['No recognised provider', mix.untagged]);
    if (mix.no_xml) parts.push(['No ComicInfo.xml', mix.no_xml]);
    if (mix.unscanned) parts.push(['Not scanned yet', mix.unscanned]);
    if (!parts.length) {
      addText(box, 'span', 'No comics found in this folder.', 'text-muted');
      return;
    }
    parts.forEach(function (pair) {
      var badge = addText(box, 'span', pair[0] + ' ', 'badge text-bg-secondary me-1');
      addText(badge, 'span', String(pair[1]), 'badge text-bg-light ms-1');
    });
    if (mix.unscanned) {
      addText(box, 'div', 'Files not scanned yet are still being indexed in the background.', 'form-text');
    }
  }

  function renderOnlyFrom(mix) {
    var wrap = el('retagOnlyFromWrap');
    var box = el('retagOnlyFrom');
    box.textContent = '';
    var ids = Object.keys(mix.providers).sort();
    if (mix.untagged) ids.push(UNTAGGED);
    wrap.style.display = ids.length > 1 ? '' : 'none';
    ids.forEach(function (id, i) {
      var div = document.createElement('div');
      div.className = 'form-check';
      var input = document.createElement('input');
      input.type = 'checkbox';
      input.className = 'form-check-input retag-only-from';
      input.id = 'retagOnly' + i;
      input.value = id;
      var lab = document.createElement('label');
      lab.className = 'form-check-label';
      lab.htmlFor = input.id;
      lab.textContent = id === UNTAGGED ? 'No recognised provider' : label(id);
      div.appendChild(input);
      div.appendChild(lab);
      box.appendChild(div);
    });
  }

  function updateEstimate() {
    if (!_retag || !_retag.mix) return;  // file-list mode has a fixed estimate
    var target = el('retagProvider').value;
    var only = selectedOnlyFrom();
    var mix = _retag.mix;
    var n = 0;
    Object.keys(mix.providers).forEach(function (p) {
      if (p === target) return;
      if (only.length && only.indexOf(p) < 0) return;
      n += mix.providers[p];
    });
    if (!only.length || only.indexOf(UNTAGGED) >= 0) n += mix.untagged + mix.no_xml;
    el('retagEstimate').textContent =
      'About ' + n + ' file(s) will be re-tagged, each one provider request.';
    el('retagConfirmBtn').disabled = !target;
  }

  function selectedOnlyFrom() {
    return Array.prototype.map.call(
      document.querySelectorAll('#retagOnlyFrom .retag-only-from:checked'),
      function (c) { return c.value; });
  }

  function resetRetagModal(name) {
    el('retagFolderName').textContent = name;
    el('retagMix').textContent = 'Loading…';
    el('retagProvider').textContent = '';
    el('retagOnlyFrom').textContent = '';
    el('retagOnlyFromWrap').style.display = 'none';
    el('retagEstimate').textContent = '';
    el('retagConfirmBtn').disabled = true;
    el('retagMixGroup').style.display = '';
  }

  function fillProviders(enabled) {
    var select = el('retagProvider');
    select.textContent = '';
    (enabled || []).forEach(function (p) {
      var opt = document.createElement('option');
      opt.value = p;
      opt.textContent = label(p);
      select.appendChild(opt);
    });
    if (!select.options.length) {
      el('retagEstimate').textContent = 'No metadata providers are enabled for this library.';
    }
  }

  CLU.openRetagModal = function (folderPath, folderName, libraryId, onDone) {
    _retag = { path: folderPath, files: null, libraryId: libraryId || null, onDone: onDone, mix: null };
    resetRetagModal(folderName || folderPath);
    showModal('retagModal');

    var url = '/api/bulk-metadata/provider-mix?path=' + encodeURIComponent(folderPath) +
      (libraryId ? '&library_id=' + encodeURIComponent(libraryId) : '');
    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      if (!data || !data.success) throw new Error((data && data.error) || 'Could not read the folder');
      _retag.mix = data.mix;
      _retag.libraryId = data.library_id || _retag.libraryId;
      renderMix(data.mix);
      renderOnlyFrom(data.mix);
      fillProviders(data.enabled_providers);
      updateEstimate();
    }).catch(function (e) {
      el('retagMix').textContent = '';
      addText(el('retagMix'), 'span', e.message || 'Could not read the folder', 'text-danger');
    });
  };

  // Re-tag an explicit list of files (one row, or a selection that may span
  // folders). `folderForProviders` is any library folder, used only to learn
  // which providers the library has enabled.
  CLU.openRetagFilesModal = function (paths, folderForProviders, libraryId, onDone) {
    _retag = { path: null, files: paths.slice(), libraryId: libraryId || null, onDone: onDone, mix: null };
    resetRetagModal(paths.length === 1 ? paths[0].split('/').pop() : paths.length + ' selected files');
    el('retagMixGroup').style.display = 'none';
    showModal('retagModal');

    var url = '/api/bulk-metadata/provider-mix?recursive=false&path=' + encodeURIComponent(folderForProviders) +
      (libraryId ? '&library_id=' + encodeURIComponent(libraryId) : '');
    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      if (!data || !data.success) throw new Error((data && data.error) || 'Could not read the library');
      _retag.libraryId = data.library_id || _retag.libraryId;
      fillProviders(data.enabled_providers);
      el('retagEstimate').textContent =
        'Up to ' + paths.length + ' file(s) will be re-tagged (files already tagged by the chosen provider are skipped).';
      el('retagConfirmBtn').disabled = !el('retagProvider').value;
    }).catch(function (e) {
      el('retagEstimate').textContent = e.message || 'Could not read the library';
    });
  };

  CLU.submitRetag = function () {
    if (!_retag) return;
    var provider = el('retagProvider').value;
    if (!provider) return;
    var state = _retag;
    var only = selectedOnlyFrom();
    hideModal('retagModal');
    if (typeof BulkMetadataReview === 'undefined' || !BulkMetadataReview.startJob) {
      CLU.showError('Bulk metadata is not available on this page');
      return;
    }
    BulkMetadataReview.startJob({
      scope: state.files ? 'files' : 'folder',
      paths: state.files || [state.path],
      library_id: state.libraryId,
      extra: {
        provider: provider,
        replace: el('retagReplace').checked,
        only_from: !state.files && only.length ? only : null
      },
      onDone: state.onDone
    });
  };

  // ---- Backfill ----------------------------------------------------------

  var _backfill = null;

  CLU.openBackfillModal = function (folderPath, folderName, libraryId, onDone) {
    _backfill = { path: folderPath, libraryId: libraryId || null, onDone: onDone };
    el('backfillFolderName').textContent = folderName || folderPath;
    el('backfillDryRun').checked = false;
    showModal('backfillModal');
  };

  CLU.submitBackfill = function () {
    if (!_backfill) return;
    var state = _backfill;
    var dry = el('backfillDryRun').checked;
    hideModal('backfillModal');
    if (typeof BulkMetadataReview === 'undefined' || !BulkMetadataReview.startJob) {
      CLU.showError('Bulk metadata is not available on this page');
      return;
    }
    BulkMetadataReview.startJob({
      scope: 'folder',
      paths: [state.path],
      library_id: state.libraryId,
      url: '/api/bulk-metadata/backfill',
      extra: { dry_run: dry },
      summarize: function (job) {
        var n = job.auto_accepted || 0;
        var tail = (job.skipped || 0) + ' unchanged or skipped, ' + (job.errors || 0) + ' errors.';
        return dry
          ? 'Preview: ' + n + ' file(s) would gain missing fields. ' + tail
          : 'Backfill complete: ' + n + ' file(s) updated. ' + tail;
      },
      onDone: state.onDone
    });
  };

  document.addEventListener('DOMContentLoaded', function () {
    var retagBtn = el('retagConfirmBtn');
    if (retagBtn) retagBtn.addEventListener('click', CLU.submitRetag);
    var provider = el('retagProvider');
    if (provider) provider.addEventListener('change', updateEstimate);
    var only = el('retagOnlyFrom');
    if (only) only.addEventListener('change', updateEstimate);
    var backfillBtn = el('backfillConfirmBtn');
    if (backfillBtn) backfillBtn.addEventListener('click', CLU.submitBackfill);
  });
})();
