/**
 * CLU CBZ Dupe Viewer  –  clu-cbz-dupe.js
 *
 * Read-only, side-by-side comparison of duplicate copies, for deciding which
 * one to keep. A trimmed sibling of clu-cbz-info.js: no editing, no clearing,
 * and only the fields that tell two copies apart.
 * Provides: CLU.showCBZDupe
 *
 * Depends on: clu-utils.js (CLU.escapeHtml, CLU.formatFileSize)
 *
 * CLU.showCBZDupe(files, options)
 *   files    – 1 or 2 objects {path, name, folder, size, modified_at, status,
 *              provider}; two are shown side by side with differences tinted
 *   options  – { keepIndex: column currently chosen as the keeper (or -1),
 *                onKeep(index): called by "Keep this one"; no buttons without it }
 *
 * DOM contract:
 *   #cbzDupeModal, #cbzDupeModalLabel, #cbzDupeContent  (modal_cbz_dupe.html)
 *
 * Buttons carry a column index, never a path: the HTML parser rewrites some
 * bytes inside attribute values (see static/js/problem_files.js).
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  // ── Module state ──────────────────────────────────────────────────────────

  var _token = 0;          // bumped per open; stale responses are dropped
  var _cols = [];          // per column: { path, count, page }
  var _onKeep = null;

  // Same keys and labels as the matching groups in clu-cbz-info.js.
  var _metaSections = [
    {
      title: 'Basic Information',
      fields: [
        { key: 'Title', label: 'Title' },
        { key: 'Series', label: 'Series' },
        { key: 'Number', label: 'Number' },
        { key: 'Count', label: 'Count' },
        { key: 'Volume', label: 'Volume' },
        { key: 'AlternateSeries', label: 'Alternate Series' },
        { key: 'AlternateNumber', label: 'Alternate Number' },
        { key: 'AlternateCount', label: 'Alternate Count' }
      ]
    },
    {
      title: 'Publication Details',
      fields: [
        { key: 'Year', label: 'Year' },
        { key: 'Month', label: 'Month' },
        { key: 'Day', label: 'Day' },
        { key: 'Publisher', label: 'Publisher' },
        { key: 'Imprint', label: 'Imprint' },
        { key: 'Format', label: 'Format' },
        { key: 'PageCount', label: 'Page Count' },
        { key: 'LanguageISO', label: 'Language' },
        { key: 'MetronId', label: 'Metron ID' }
      ]
    }
  ];

  // ── Helpers ───────────────────────────────────────────────────────────────

  function _esc(text) { return CLU.escapeHtml(text === null || text === undefined ? '' : String(text)); }

  function _encodePathForReader(path) {
    var clean = path.charAt(0) === '/' ? path.substring(1) : path;
    return clean.split('/').map(function (c) { return encodeURIComponent(c); }).join('/');
  }

  function _isBlank(v) {
    return v === undefined || v === null || String(v).trim() === '';
  }

  /** Comparison key: trimmed, and numeric when it looks like a number (1 == 001). */
  function _norm(v) {
    if (_isBlank(v)) return '';
    var s = String(v).trim();
    return /^-?\d+(\.\d+)?$/.test(s) ? String(parseFloat(s)) : s;
  }

  function _letter(i) { return 'Copy ' + String.fromCharCode(65 + i); }

  // ── Loading ───────────────────────────────────────────────────────────────

  function _fetchJson(url) {
    return fetch(url).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        return { ok: r.ok, status: r.status, data: d || {} };
      });
    });
  }

  /** {meta, metaError, unsupported, info} for one file; never rejects. */
  function _loadFile(f) {
    var isZip = /\.(cbz|zip)$/i.test(f.name || f.path || '');
    var metaP = isZip
      ? _fetchJson('/cbz-metadata?path=' + encodeURIComponent(f.path)).then(function (res) {
          return res.ok ? { meta: res.data } : { metaError: res.data.error || ('HTTP ' + res.status) };
        }).catch(function () { return { metaError: 'Could not reach the server' }; })
      : Promise.resolve({ unsupported: true });
    var infoP = _fetchJson('/api/read/' + _encodePathForReader(f.path) + '/info').then(function (res) {
      return res.ok && res.data.success ? res.data : null;
    }).catch(function () { return null; });

    return Promise.all([metaP, infoP]).then(function (parts) {
      var out = parts[0];
      out.info = parts[1];
      return out;
    });
  }

  // ── Column header (cover, pages, keep) ────────────────────────────────────

  function _badges(f, i, keepIndex) {
    var out = '';
    if (i === keepIndex) out += '<span class="badge bg-primary me-1"><i class="bi bi-check2 me-1"></i>Selected to keep</span>';
    if (f.status === 'damaged') out += '<span class="badge bg-danger me-1">Damaged</span>';
    out += f.has_comicinfo
      ? '<span class="badge bg-success me-1">Tagged</span>'
      : '<span class="badge bg-secondary me-1">No ComicInfo</span>';
    if (f.provider) out += '<span class="badge bg-light text-dark border me-1">' + _esc(f.provider) + '</span>';
    return out;
  }

  function _columnNote(r) {
    if (r.unsupported) {
      return '<div class="alert alert-info small py-1 px-2 mb-2">ComicInfo.xml can only be read from a .cbz.</div>';
    }
    if (r.metaError) {
      return '<div class="alert alert-danger small py-1 px-2 mb-2 text-break">Could not read this file: ' +
        _esc(r.metaError) + '</div>';
    }
    if (r.meta && !r.meta.comicinfo) {
      return '<div class="alert alert-secondary small py-1 px-2 mb-2">No ComicInfo.xml in this file.</div>';
    }
    return '';
  }

  function _renderColumn(f, r, i, total, keepIndex) {
    var colClass = total > 1 ? 'col-md-6' : 'col-md-8 col-lg-6 mx-auto';
    var html = '<div class="' + colClass + ' mb-3"><div class="card h-100"><div class="card-body d-flex flex-column">';
    if (total > 1) html += '<div class="small text-muted text-uppercase">' + _letter(i) + '</div>';
    html += '<div class="fw-bold text-break">' + _esc(f.name) + '</div>' +
      '<div class="small text-muted text-break mb-1">' + _esc(f.folder) + '</div>' +
      '<div class="mb-2">' + _badges(f, i, keepIndex) + '</div>' +
      _columnNote(r);

    html += '<div class="text-center flex-grow-1 d-flex align-items-center justify-content-center" ' +
      'id="cbzDupeCover' + i + '" style="min-height: 200px;">';
    if (r.info && r.info.page_count > 0) {
      html += '<div class="spinner-border spinner-border-sm" role="status"><span class="visually-hidden">Loading...</span></div>';
    } else {
      html += '<p class="text-muted mb-0">Preview not available</p>';
    }
    html += '</div>';

    if (r.info && r.info.page_count > 1) {
      html += '<div class="d-flex justify-content-center align-items-center gap-2 mt-2">' +
        '<button type="button" class="btn btn-sm btn-outline-secondary" data-dupe-act="prev" data-col="' + i + '" ' +
          'title="Previous page" aria-label="Previous page"><i class="bi bi-chevron-left"></i></button>' +
        '<span class="small text-muted" id="cbzDupePageLabel' + i + '"></span>' +
        '<button type="button" class="btn btn-sm btn-outline-secondary" data-dupe-act="next" data-col="' + i + '" ' +
          'title="Next page" aria-label="Next page"><i class="bi bi-chevron-right"></i></button>' +
      '</div>';
    }

    if (_onKeep) {
      html += '<div class="mt-3"><button type="button" class="btn btn-sm w-100 ' +
        (i === keepIndex ? 'btn-success' : 'btn-outline-success') + '" data-dupe-act="keep" data-col="' + i + '">' +
        '<i class="bi bi-check2-circle me-1"></i>Keep this one</button></div>';
    }
    html += '</div></div></div>';
    return html;
  }

  // ── Comparison table ──────────────────────────────────────────────────────

  // A cell is { value } (raw, for comparing), optional { html } for display,
  // and { unknown: true } when the column could not supply it at all.
  function _cellHtml(cell) {
    if (cell.unknown || _isBlank(cell.value)) return '<span class="text-muted">—</span>';
    return cell.html !== undefined ? cell.html : _esc(cell.value);
  }

  function _rowHtml(row, total) {
    var visible = row.cells.some(function (c) { return !c.unknown && !_isBlank(c.value); });
    if (!visible) return null;
    var differs = false;
    if (total === 2 && !row.cells.some(function (c) { return c.unknown; })) {
      differs = _norm(row.cells[0].value) !== _norm(row.cells[1].value);
    }
    return {
      differs: differs,
      html: '<tr' + (differs ? ' class="table-warning"' : '') + '>' +
        '<th scope="row" class="small fw-normal text-muted">' + _esc(row.label) + '</th>' +
        row.cells.map(function (c) {
          return '<td class="small text-break"' + (c.title ? ' title="' + _esc(c.title) + '"' : '') + '>' +
            _cellHtml(c) + '</td>';
        }).join('') +
        '</tr>'
    };
  }

  function _metaCell(r, key) {
    if (!r.meta) return { unknown: true };
    var ci = r.meta.comicinfo || {};
    return { value: ci[key] };
  }

  function _integrity(status) {
    if (status === 'damaged') return 'Damaged';
    if (status === 'ok') return 'Passed';
    return 'Not checked yet';
  }

  function _fileRows(files, results) {
    function each(fn) { return files.map(function (f, i) { return fn(f, results[i]); }); }
    return [
      { label: 'Name', cells: each(function (f) { return { value: f.name }; }) },
      { label: 'Folder', cells: each(function (f) { return { value: f.folder }; }) },
      { label: 'Size', cells: each(function (f) {
          var n = Number(f.size || 0);
          return { value: n, html: _esc(CLU.formatFileSize(n)), title: n.toLocaleString() + ' bytes' };
        }) },
      { label: 'Modified', cells: each(function (f) {
          if (!f.modified_at) return { value: null };
          return { value: f.modified_at, html: _esc(new Date(f.modified_at * 1000).toLocaleString()) };
        }) },
      { label: 'Pages', cells: each(function (f, r) {
          return r.info ? { value: r.info.page_count } : { unknown: true };
        }) },
      { label: 'Image files', cells: each(function (f, r) {
          return r.meta ? { value: r.meta.image_files } : { unknown: true };
        }) },
      { label: 'Archive entries', cells: each(function (f, r) {
          return r.meta ? { value: r.meta.total_files } : { unknown: true };
        }) },
      { label: 'Integrity', cells: each(function (f) { return { value: _integrity(f.status) }; }) },
      { label: 'Tagged by', cells: each(function (f) {
          return { value: f.provider === 'manual' ? 'Manual' : f.provider };
        }) }
    ];
  }

  function _firstFileRows(results) {
    var longest = 0;
    results.forEach(function (r) {
      if (r.meta && r.meta.file_list) longest = Math.max(longest, r.meta.file_list.length);
    });
    var rows = [];
    for (var n = 0; n < longest; n++) {
      rows.push({
        label: String(n + 1),
        cells: results.map(function (r) {
          if (!r.meta) return { unknown: true };
          var name = (r.meta.file_list || [])[n];
          return { value: name, html: '<code>' + _esc(name) + '</code>' };
        })
      });
    }
    return rows;
  }

  function _renderTable(files, results) {
    var total = files.length;
    var sections = _metaSections.map(function (s) {
      return {
        title: s.title,
        rows: s.fields.map(function (f) {
          return { label: f.label, cells: results.map(function (r) { return _metaCell(r, f.key); }) };
        })
      };
    });
    sections.push({ title: 'File Information', rows: _fileRows(files, results) });
    sections.push({ title: 'First Files', rows: _firstFileRows(results) });

    var diffCount = 0;
    var body = '';
    sections.forEach(function (s) {
      var rendered = s.rows.map(function (row) { return _rowHtml(row, total); }).filter(Boolean);
      if (!rendered.length) return;
      body += '<tr><th colspan="' + (total + 1) + '" class="pt-3 border-0"><h6 class="mb-0">' +
        _esc(s.title) + '</h6></th></tr>';
      rendered.forEach(function (r) {
        if (r.differs) diffCount++;
        body += r.html;
      });
    });

    var head = '<thead><tr><th style="width: 20%;" class="small">Field</th>' +
      files.map(function (f, i) {
        return '<th class="small">' + (total > 1 ? _letter(i) : 'Value') + '</th>';
      }).join('') + '</tr></thead>';

    var legend = '';
    if (total === 2) {
      legend = '<p class="small text-muted mb-2">' + (diffCount
        ? '<span class="badge bg-warning text-dark me-1">' + diffCount + '</span>' +
          (diffCount === 1 ? 'row differs' : 'rows differ') + ' between the two copies (highlighted).'
        : 'No differences in the fields shown.') +
        ' Fields empty in both copies are hidden.</p>';
    }

    return legend + '<div class="table-responsive"><table class="table table-sm align-middle mb-0" ' +
      'style="table-layout: fixed; min-width: ' + (total > 1 ? '560px' : '0') + ';">' +
      head + '<tbody>' + body + '</tbody></table></div>';
  }

  // ── Page viewer (one per column) ──────────────────────────────────────────

  function _loadPage(ci, page) {
    var col = _cols[ci];
    if (!col || page < 0 || page >= col.count) return;
    col.page = page;
    var token = _token;

    var label = document.getElementById('cbzDupePageLabel' + ci);
    if (label) label.textContent = 'Page ' + (page + 1) + ' / ' + col.count;

    var img = new Image();
    img.className = 'img-fluid';
    img.style.maxHeight = '450px';
    img.alt = _letter(ci) + ', page ' + (page + 1);
    img.onload = function () {
      if (token !== _token || col.page !== page) return;   // superseded
      var holder = document.getElementById('cbzDupeCover' + ci);
      if (!holder) return;
      holder.innerHTML = '';
      holder.appendChild(img);
    };
    img.onerror = function () {
      if (token !== _token || col.page !== page) return;
      var holder = document.getElementById('cbzDupeCover' + ci);
      if (holder) holder.innerHTML = '<p class="text-muted mb-0">Page not available</p>';
    };
    img.src = '/api/read/' + _encodePathForReader(col.path) + '/page/' + page;
  }

  function _step(ci, delta) {
    var col = _cols[ci];
    if (!col) return;
    var next = Math.max(0, Math.min(col.count - 1, col.page + delta));
    if (next !== col.page) _loadPage(ci, next);
  }

  /** Arrow keys and Space move every column together, so pages stay aligned. */
  function _handleKeydown(e) {
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
              t.tagName === 'SELECT' || t.isContentEditable)) return;
    var delta = 0;
    if (e.key === 'ArrowLeft') delta = -1;
    else if (e.key === 'ArrowRight') delta = 1;
    else if (e.code === 'Space' && !(t && t.tagName === 'BUTTON')) delta = 1;
    if (!delta) return;
    e.preventDefault();
    _cols.forEach(function (c, i) { _step(i, delta); });
  }

  function _onContentClick(e) {
    var btn = e.target.closest('[data-dupe-act]');
    if (!btn) return;
    var ci = parseInt(btn.getAttribute('data-col'), 10);
    var act = btn.getAttribute('data-dupe-act');
    if (act === 'prev') _step(ci, -1);
    else if (act === 'next') _step(ci, 1);
    else if (act === 'keep' && typeof _onKeep === 'function') _onKeep(ci);
  }

  // ── showCBZDupe ───────────────────────────────────────────────────────────

  CLU.showCBZDupe = function (files, options) {
    options = options || {};
    var modalElement = document.getElementById('cbzDupeModal');
    var content = document.getElementById('cbzDupeContent');
    if (!modalElement || !content || !files || !files.length) return;
    files = files.slice(0, 2);

    var token = ++_token;
    _cols = [];
    _onKeep = typeof options.onKeep === 'function' ? options.onKeep : null;
    var keepIndex = typeof options.keepIndex === 'number' ? options.keepIndex : -1;

    var title = document.getElementById('cbzDupeModalLabel');
    if (title) title.textContent = files.length > 1 ? 'Compare copies' : 'File details';

    content.innerHTML =
      '<div class="text-center">' +
        '<div class="spinner-border" role="status"><span class="visually-hidden">Loading...</span></div>' +
        '<p class="mt-2">Loading file information...</p>' +
      '</div>';

    var modal = bootstrap.Modal.getOrCreateInstance(modalElement);
    if (!modalElement.classList.contains('show')) modal.show();

    Promise.all(files.map(_loadFile)).then(function (results) {
      if (token !== _token) return;   // reopened on another group meanwhile
      _cols = files.map(function (f, i) {
        var count = results[i].info ? (results[i].info.page_count || 0) : 0;
        return { path: f.path, count: count, page: 0 };
      });

      content.innerHTML =
        '<div class="row">' + files.map(function (f, i) {
          return _renderColumn(f, results[i], i, files.length, keepIndex);
        }).join('') + '</div>' +
        _renderTable(files, results);

      _cols.forEach(function (c, i) { if (c.count > 0) _loadPage(i, 0); });
    }).catch(function (err) {
      if (token !== _token) return;
      content.innerHTML = '<div class="alert alert-danger">Error loading file information: ' +
        _esc(err && err.message) + '</div>';
    });
  };

  // ── DOM wiring ────────────────────────────────────────────────────────────

  function _init() {
    var modal = document.getElementById('cbzDupeModal');
    if (modal) {
      modal.addEventListener('shown.bs.modal', function () {
        document.addEventListener('keydown', _handleKeydown);
      });
      modal.addEventListener('hidden.bs.modal', function () {
        document.removeEventListener('keydown', _handleKeydown);
        _token++;      // drop any page or metadata still loading
        _cols = [];
        _onKeep = null;
      });
    }
    var content = document.getElementById('cbzDupeContent');
    if (content) content.addEventListener('click', _onContentClick);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _init);
  } else {
    _init();
  }

})();
