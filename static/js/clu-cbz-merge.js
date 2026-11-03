/**
 * CLU CBZ Merge Pages  –  clu-cbz-merge.js
 *
 * Builds one CBZ from two copies of the same issue: the base file's pages,
 * plus whichever pages of the other (donor) file the user adds, in the order
 * they arrange. The result is written over one of the two files; its original
 * goes to the Trash.
 *
 * Nothing is extracted to disk. Pages are listed by /api/merge-pages/inspect
 * and thumbnailed lazily, one entry at a time, by /api/merge-pages/page.
 *
 * Provides: CLU.showCBZMerge(pathA, pathB, options)
 *   options.onComplete(result)  called after a successful merge
 *
 * Depends on: clu-utils.js (CLU.escapeHtml, CLU.formatFileSize),
 *             clu-toast.js (CLU.showSuccess, CLU.showError),
 *             Sortable.js (loaded by templates/partials/modal_cbz_merge.html)
 *
 * DOM contract: #mergeCBZModal (templates/partials/modal_cbz_merge.html)
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  // A page this much smaller (by area) than the base median is flagged.
  var LOW_RES_RATIO = 0.9;

  var S = null;            // per-open state
  var _sortables = [];

  function _esc(s) {
    return CLU.escapeHtml ? CLU.escapeHtml(s == null ? '' : String(s)) : String(s);
  }

  function _size(bytes) {
    return CLU.formatFileSize ? CLU.formatFileSize(bytes) : (bytes + ' B');
  }

  function _el(id) { return document.getElementById(id); }

  function _baseName(path) { return String(path).split(/[\\/]/).pop(); }

  function _dirName(path) {
    var parts = String(path).split(/[\\/]/);
    parts.pop();
    return parts.join('/');
  }

  function _other(key) { return key === 'A' ? 'B' : 'A'; }

  function _area(dims) { return dims ? dims[0] * dims[1] : 0; }

  function _thumbUrl(path, entry, large) {
    return '/api/merge-pages/page?path=' + encodeURIComponent(path) +
      '&entry=' + encodeURIComponent(entry) + (large ? '&size=large' : '');
  }

  function _pageInfo(key, entry) {
    var f = S.files[key];
    return f.byEntry[entry];
  }

  // ── State ───────────────────────────────────────────────────────────────

  /** Start the final list over: every base page, in order, included. */
  function _resetFinal() {
    S.final = S.files[S.baseKey].pages.map(function (p) {
      return { key: S.baseKey, entry: p.entry, included: true };
    });
  }

  /** Insert a donor page where it sat in its own file, relative to the base:
   *  donor page i goes before the base page at round(i * baseCount / donorCount). */
  function _autoPlace(entry) {
    var donorKey = _other(S.baseKey);
    var donor = S.files[donorKey];
    var base = S.files[S.baseKey];
    var i = donor.index[entry];
    var basePos = donor.pages.length ? Math.round(i * base.pages.length / donor.pages.length) : 0;

    // Before the final item holding that base page; after any donor pages
    // already placed in front of it, so donor runs keep their own order.
    var at = S.final.length;
    for (var k = 0; k < S.final.length; k++) {
      var it = S.final[k];
      if (it.key === S.baseKey && base.index[it.entry] >= basePos) { at = k; break; }
      if (it.key === donorKey && donor.index[it.entry] > i) { at = k; break; }
    }
    S.final.splice(at, 0, { key: donorKey, entry: entry, included: true });
  }

  function _addedDonorEntries() {
    var donorKey = _other(S.baseKey);
    return S.final.filter(function (it) { return it.key === donorKey; })
      .map(function (it) { return it.entry; });
  }

  // ── Rendering ───────────────────────────────────────────────────────────

  function _renderFileCards() {
    var html = ['A', 'B'].map(function (key) {
      var f = S.files[key];
      var isBase = key === S.baseKey;
      var dims = f.median ? (f.median[0] + ' × ' + f.median[1]) : 'unknown';
      return '<div class="col-md-6">' +
        '<div class="card merge-file-card h-100' + (isBase ? ' is-base' : ' is-donor') + '">' +
        '<div class="card-body py-2 px-3 small">' +
        '<div class="d-flex justify-content-between align-items-start gap-2">' +
        '<div><span class="badge ' + (isBase ? 'text-bg-primary' : 'text-bg-info') + ' me-1">' + key + '</span>' +
        '<strong>' + _esc(f.name) + '</strong>' +
        (f.in_library ? ' <span class="badge text-bg-secondary">Library</span>' : '') +
        (key === S.targetKey
          ? ' <span class="badge text-bg-danger" title="Merge writes over this file; its original goes to the Trash">' +
            '<i class="bi bi-exclamation-triangle me-1"></i>Will be overwritten</span>'
          : '') + '</div>' +
        (isBase
          ? '<span class="badge text-bg-primary">Base</span>'
          : '<button type="button" class="btn btn-sm btn-outline-primary py-0" data-merge-make-base="' + key + '">Use as base</button>') +
        '</div>' +
        '<div class="text-muted merge-file-path">' + _esc(_dirName(f.path)) + '</div>' +
        '<div>' + _size(f.file_size) + ' &middot; ' + f.page_count + ' pages &middot; ' + dims +
        (f.has_comicinfo ? ' &middot; ComicInfo.xml' : '') + '</div>' +
        '</div></div></div>';
    }).join('');
    _el('mergeFileCards').innerHTML = html;
    _el('mergeDonorName').textContent = S.files[_other(S.baseKey)].name;
  }

  function _pageCard(key, entry, opts) {
    var f = S.files[key];
    var p = _pageInfo(key, entry);
    var baseMedian = _area(S.files[S.baseKey].median);
    var area = p.width && p.height ? p.width * p.height : 0;
    var lowRes = baseMedian && area && area < baseMedian * LOW_RES_RATIO;
    var classes = 'card merge-page-card h-100';
    if (key !== S.baseKey) classes += ' from-donor';
    if (opts.excluded) classes += ' is-excluded';
    if (opts.added) classes += ' is-added';

    var badges = '<span class="badge ' + (key === S.baseKey ? 'text-bg-primary' : 'text-bg-info') + '">' + key + '</span>';
    if (lowRes) badges += '<span class="badge text-bg-warning" title="Lower resolution than the base file">low-res</span>';
    if (opts.added) badges += '<span class="badge text-bg-success">Added</span>';

    var action = opts.action || '';
    return '<div class="col" data-key="' + key + '" data-entry="' + _esc(entry) + '"' +
      (opts.finalIdx != null ? ' data-final-idx="' + opts.finalIdx + '"' : '') + '>' +
      '<div class="' + classes + '">' +
      '<div class="merge-page-badges">' + badges + '</div>' +
      '<div class="merge-thumb-wrap" data-merge-preview="1">' +
      '<img loading="lazy" alt="" src="' + _esc(_thumbUrl(f.path, entry)) + '"></div>' +
      '<div class="card-body p-1">' +
      (opts.number != null ? '<div class="merge-page-meta fw-semibold">' + opts.number + '</div>' : '') +
      '<div class="merge-page-meta merge-page-name" title="' + _esc(entry) + '">' + _esc(p.basename) + '</div>' +
      '<div class="merge-page-meta text-muted">' +
      (p.width ? p.width + '×' + p.height : '?') + ' &middot; ' + _size(p.size) + '</div>' +
      action +
      '</div></div></div>';
  }

  function _renderFinal() {
    var n = 0;
    var html = S.final.map(function (it, idx) {
      var number = it.included ? ++n : null;
      var btn;
      if (it.key === S.baseKey) {
        btn = '<button type="button" class="btn btn-sm btn-outline-secondary w-100 py-0 mt-1" data-merge-toggle="' + idx + '">' +
          (it.included ? '<i class="bi bi-eye-slash"></i> Leave out' : '<i class="bi bi-eye"></i> Include') + '</button>';
      } else {
        btn = '<button type="button" class="btn btn-sm btn-outline-danger w-100 py-0 mt-1" data-merge-remove="' + idx + '">' +
          '<i class="bi bi-x-lg"></i> Remove</button>';
      }
      return _pageCard(it.key, it.entry, {
        finalIdx: idx, number: number, excluded: !it.included, action: btn
      });
    }).join('');
    _el('mergeFinalGrid').innerHTML = html;
    _el('mergeFinalCount').textContent = '(' + n + ' pages)';
  }

  function _renderDonor() {
    var donorKey = _other(S.baseKey);
    var added = {};
    _addedDonorEntries().forEach(function (e) { added[e] = true; });
    var html = S.files[donorKey].pages.map(function (p) {
      var isAdded = !!added[p.entry];
      var btn = isAdded
        ? '<button type="button" class="btn btn-sm btn-outline-secondary w-100 py-0 mt-1" disabled>Added</button>'
        : '<button type="button" class="btn btn-sm btn-outline-info w-100 py-0 mt-1" data-merge-add="' + _esc(p.entry) + '">' +
          '<i class="bi bi-plus-lg"></i> Add</button>';
      return _pageCard(donorKey, p.entry, { added: isAdded, action: btn });
    }).join('');
    _el('mergeDonorGrid').innerHTML = html;
  }

  function _renderFooter() {
    var choices = ['A', 'B'].map(function (key) {
      var id = 'mergeTarget' + key;
      return '<div class="form-check mb-0">' +
        '<input class="form-check-input" type="radio" name="mergeTarget" id="' + id + '" value="' + key + '"' +
        (S.targetKey === key ? ' checked' : '') + '>' +
        '<label class="form-check-label" for="' + id + '"><span class="badge text-bg-secondary me-1">' + key + '</span>' +
        _esc(S.files[key].name) + '</label></div>';
    }).join('');
    _el('mergeTargetChoices').innerHTML = choices;
    _el('mergeOtherName').textContent = S.files[_other(S.targetKey)].name;
    _updateSummary();
  }

  function _updateSummary() {
    var count = S.final.filter(function (it) { return it.included; }).length;
    var added = _addedDonorEntries().length;
    var target = S.files[S.targetKey];
    _el('mergeSummary').textContent = count + ' pages (' + added + ' added from ' + _other(S.baseKey) +
      ') will overwrite ' + S.targetKey + ': ' + target.path;
    var btn = _el('mergeCommitBtn');
    btn.innerHTML = _commitLabel();
    btn.title = 'Overwrite ' + target.path;
    btn.disabled = count === 0;
  }

  function _commitLabel() {
    return '<i class="bi bi-layers me-1"></i>Merge &amp; overwrite ' + (S ? S.targetKey : '');
  }

  function _render() {
    _renderFileCards();
    _renderFinal();
    _renderDonor();
    _renderFooter();
    _bindSortables();
  }

  function _bindSortables() {
    _sortables.forEach(function (s) { s.destroy(); });
    _sortables = [];
    if (typeof Sortable === 'undefined') return;

    _sortables.push(Sortable.create(_el('mergeFinalGrid'), {
      group: { name: 'cluMerge', pull: false, put: true },
      animation: 150,
      ghostClass: 'merge-ghost',
      chosenClass: 'merge-chosen',
      filter: 'button',
      preventOnFilter: false,
      onEnd: function () {
        var cols = _el('mergeFinalGrid').querySelectorAll('[data-final-idx]');
        var next = [];
        cols.forEach(function (c) { next.push(S.final[parseInt(c.getAttribute('data-final-idx'), 10)]); });
        S.final = next;
        _renderFinal();
        _updateSummary();
      },
      onAdd: function (evt) {
        // A donor page dropped into the final grid: place it at the drop index.
        var entry = evt.item.getAttribute('data-entry');
        var at = evt.newIndex;
        evt.item.parentNode.removeChild(evt.item);
        if (_addedDonorEntries().indexOf(entry) === -1) {
          S.final.splice(at, 0, { key: _other(S.baseKey), entry: entry, included: true });
        }
        // Sortable is bound to the grids, not the cards, so a re-render keeps it.
        _renderFinal();
        _renderDonor();
        _updateSummary();
      }
    }));

    _sortables.push(Sortable.create(_el('mergeDonorGrid'), {
      group: { name: 'cluMerge', pull: 'clone', put: false },
      sort: false,
      animation: 150,
      filter: 'button, .is-added',
      preventOnFilter: false,
      ghostClass: 'merge-ghost'
    }));
  }

  // ── Events ──────────────────────────────────────────────────────────────

  function _onClick(e) {
    var t = e.target.closest('[data-merge-toggle],[data-merge-remove],[data-merge-add],[data-merge-make-base],[data-merge-preview]');
    if (!t || !S) return;

    if (t.hasAttribute('data-merge-toggle')) {
      var it = S.final[parseInt(t.getAttribute('data-merge-toggle'), 10)];
      it.included = !it.included;
      _renderFinal();
      _updateSummary();
    } else if (t.hasAttribute('data-merge-remove')) {
      S.final.splice(parseInt(t.getAttribute('data-merge-remove'), 10), 1);
      _renderFinal();
      _renderDonor();
      _updateSummary();
    } else if (t.hasAttribute('data-merge-add')) {
      _autoPlace(t.getAttribute('data-merge-add'));
      _renderFinal();
      _renderDonor();
      _updateSummary();
    } else if (t.hasAttribute('data-merge-make-base')) {
      _setBase(t.getAttribute('data-merge-make-base'));
    } else if (t.hasAttribute('data-merge-preview')) {
      var col = t.closest('[data-entry]');
      _showPreview(S.files[col.getAttribute('data-key')].path, col.getAttribute('data-entry'));
    }
  }

  function _onChange(e) {
    if (!S) return;
    if (e.target.name === 'mergeTarget') {
      S.targetKey = e.target.value;
      S.targetTouched = true;
      _el('mergeOtherName').textContent = S.files[_other(S.targetKey)].name;
      _renderFileCards();
      _updateSummary();
    }
  }

  function _setBase(key) {
    if (key === S.baseKey) return;
    // Base pages the user left out and donor pages they added belong to the
    // old base, so a swap starts the page list again from the new base.
    S.baseKey = key;
    if (!S.targetTouched) S.targetKey = _defaultTarget();
    _resetFinal();
    _render();
  }

  function _showPreview(path, entry) {
    var box = _el('mergePreview');
    box.querySelector('img').src = _thumbUrl(path, entry, true);
    box.style.display = '';
  }

  /** Replace the library copy; when both or neither are in a library, the donor. */
  function _defaultTarget() {
    var a = S.files.A.in_library, b = S.files.B.in_library;
    if (a && !b) return 'A';
    if (b && !a) return 'B';
    return _other(S.baseKey);
  }

  function _commit() {
    var pages = S.final.filter(function (it) { return it.included; }).map(function (it) {
      return { src: it.key === S.baseKey ? 'base' : 'donor', entry: it.entry };
    });
    if (!pages.length) return;
    var btn = _el('mergeCommitBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Merging&hellip;';

    var body = {
      base: S.files[S.baseKey].path,
      donor: S.files[_other(S.baseKey)].path,
      target: S.targetKey === S.baseKey ? 'base' : 'donor',
      pages: pages,
      delete_other: _el('mergeDeleteOther').checked
    };
    fetch('/api/merge-pages/commit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, data: d }; });
    }).then(function (res) {
      if (!res.ok || !res.data.success) throw new Error(res.data.error || 'Merge failed');
      if (res.data.warning) CLU.showError(res.data.warning);
      else CLU.showSuccess('Merged ' + res.data.page_count + ' pages into ' + _baseName(res.data.path));
      var cb = S.onComplete;
      bootstrap.Modal.getOrCreateInstance(_el('mergeCBZModal')).hide();
      if (typeof cb === 'function') cb(res.data);
    }).catch(function (err) {
      CLU.showError(err.message || String(err));
    }).finally(function () {
      btn.disabled = false;
      btn.innerHTML = _commitLabel();
    });
  }

  var _bound = false;
  function _bindOnce() {
    if (_bound) return;
    _bound = true;
    var modal = _el('mergeCBZModal');
    modal.addEventListener('click', _onClick);
    modal.addEventListener('change', _onChange);
    _el('mergePreview').addEventListener('click', function () { this.style.display = 'none'; });
    _el('mergeCommitBtn').addEventListener('click', _commit);
    modal.addEventListener('hidden.bs.modal', function () {
      _sortables.forEach(function (s) { s.destroy(); });
      _sortables = [];
      _el('mergeFinalGrid').innerHTML = '';
      _el('mergeDonorGrid').innerHTML = '';
      S = null;
    });
  }

  function _indexFile(f) {
    f.index = {};
    f.byEntry = {};
    f.pages.forEach(function (p, i) { f.index[p.entry] = i; f.byEntry[p.entry] = p; });
    return f;
  }

  // ── Public ──────────────────────────────────────────────────────────────

  CLU.showCBZMerge = function (pathA, pathB, options) {
    options = options || {};
    var modalEl = _el('mergeCBZModal');
    if (!modalEl) return;
    _bindOnce();

    _el('mergeLoading').style.display = '';
    _el('mergeContent').style.display = 'none';
    _el('mergePreview').style.display = 'none';
    _el('mergeDeleteOther').checked = false;
    _el('mergeSummary').textContent = '';
    _el('mergeTargetChoices').innerHTML = '';
    _el('mergeCommitBtn').disabled = true;
    bootstrap.Modal.getOrCreateInstance(modalEl).show();

    fetch('/api/merge-pages/inspect?base=' + encodeURIComponent(pathA) + '&donor=' + encodeURIComponent(pathB))
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (!res.ok || !res.data.success) throw new Error(res.data.error || 'Could not read the files');
        S = {
          files: { A: _indexFile(res.data.base), B: _indexFile(res.data.donor) },
          onComplete: options.onComplete,
          targetTouched: false
        };
        // The sharper scan is the natural base.
        S.baseKey = _area(S.files.B.median) > _area(S.files.A.median) ? 'B' : 'A';
        S.targetKey = _defaultTarget();
        _resetFinal();
        _el('mergeLoading').style.display = 'none';
        _el('mergeContent').style.display = '';
        _render();
      })
      .catch(function (err) {
        bootstrap.Modal.getOrCreateInstance(modalEl).hide();
        CLU.showError(err.message || String(err));
      });
  };
})();
