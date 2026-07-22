/**
 * CLU CBZ Split Operations  –  clu-cbz-split.js
 *
 * Splits one multi-issue CBZ into several single-issue CBZs. Issue boundaries
 * are auto-detected server-side from the page filenames; the user adjusts them
 * client-side (no server round-trip until Split is clicked).
 *
 * Provides: CLU.renderSplitGroups, CLU.splitAt, CLU.mergeGroupUp,
 *           CLU.updateGroupName, CLU.saveSplitCBZ
 *
 * Depends on: clu-utils.js (CLU.showToast, CLU.showError, CLU.showSuccess)
 *
 * External contract:
 *   window._cluCbzSplit = { onSaveComplete(filePath) }
 *
 * DOM contracts:
 *   #splitCBZModal (from modal_cbz_split.html)
 *   #splitInlineContainer, #splitInlineFolderName, #splitInlineRootFolder,
 *   #splitInlineOriginalFilePath, #splitOutputFolderName, #splitSummary
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  function _getContract() { return window._cluCbzSplit || {}; }

  // ── Per-session state ───────────────────────────────────────────────────
  CLU._splitState = null;

  function _escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  CLU.renderSplitGroups = function (data) {
    CLU._splitState = {
      folderName: data.folder_name || '',
      rootFolder: data.root_folder || '',
      originalPath: data.original_file_path || '',
      groups: (data.groups || []).map(function (g) {
        return {
          name: g.suggested_name || '',
          pages: (g.pages || []).map(function (p) {
            return { rel_path: p.rel_path, filename: p.filename, img_data: p.img_data };
          })
        };
      })
    };

    document.getElementById('splitInlineFolderName').value = data.folder_name || '';
    document.getElementById('splitInlineRootFolder').value = data.root_folder || '';
    document.getElementById('splitInlineOriginalFilePath').value = data.original_file_path || '';
    var folderInput = document.getElementById('splitOutputFolderName');
    if (folderInput && !folderInput.value) folderInput.value = data.suggested_folder || '';

    _render();
  };

  function _render() {
    var st = CLU._splitState;
    var container = document.getElementById('splitInlineContainer');
    if (!st || !container) return;

    var totalPages = 0;
    var html = '';
    st.groups.forEach(function (g, gi) {
      totalPages += g.pages.length;
      html += '<div class="split-group card mb-3" data-group="' + gi + '">';
      html += '  <div class="card-header d-flex align-items-center gap-2 flex-wrap">';
      html += '    <span class="badge bg-primary">Issue ' + (gi + 1) + '</span>';
      html += '    <input type="text" class="form-control form-control-sm split-group-name" ' +
              'style="max-width: 360px;" value="' + _escapeHtml(g.name) + '" ' +
              'onchange="CLU.updateGroupName(' + gi + ', this.value)">';
      html += '    <span class="text-muted small">' + g.pages.length + ' page' +
              (g.pages.length === 1 ? '' : 's') + '</span>';
      if (gi > 0) {
        html += '    <button type="button" class="btn btn-sm btn-outline-secondary ms-auto" ' +
                'onclick="CLU.mergeGroupUp(' + gi + ')" title="Merge into previous issue">' +
                '<i class="bi bi-arrow-up-square"></i> Merge into previous</button>';
      }
      html += '  </div>';
      html += '  <div class="card-body"><div class="row row-cols-2 row-cols-sm-3 row-cols-md-5 g-2">';
      g.pages.forEach(function (p, pi) {
        html += '<div class="col" data-rel-path="' + _escapeHtml(p.rel_path) + '">';
        html += '  <div class="card h-100 split-page-card">';
        html += '    <div class="split-thumb-wrap">';
        html += p.img_data
          ? '<img src="' + p.img_data + '" class="split-thumb" alt="' + _escapeHtml(p.filename) + '">'
          : '<span class="text-muted small p-2">no preview</span>';
        html += '    </div>';
        html += '    <div class="card-body p-1">';
        html += '      <p class="small text-break mb-1">' + _escapeHtml(p.filename) + '</p>';
        if (pi > 0) {
          html += '      <button type="button" class="btn btn-sm btn-outline-primary w-100" ' +
                  'onclick="CLU.splitAt(' + gi + ',' + pi + ')" title="Start a new issue here">' +
                  '<i class="bi bi-scissors"></i> New issue here</button>';
        } else {
          html += '      <span class="badge bg-success w-100">issue start</span>';
        }
        html += '    </div>';
        html += '  </div>';
        html += '</div>';
      });
      html += '  </div></div>';
      html += '</div>';
    });

    container.innerHTML = html;

    var summary = document.getElementById('splitSummary');
    if (summary) {
      summary.textContent = st.groups.length + ' issue' +
        (st.groups.length === 1 ? '' : 's') + ' · ' + totalPages + ' pages';
    }
  }

  CLU.updateGroupName = function (gi, value) {
    var st = CLU._splitState;
    if (st && st.groups[gi]) st.groups[gi].name = value;
  };

  // Move the page at pageIdx (and everything after it) into a new issue.
  CLU.splitAt = function (groupIdx, pageIdx) {
    var st = CLU._splitState;
    if (!st || !st.groups[groupIdx] || pageIdx <= 0) return;
    var g = st.groups[groupIdx];
    var moved = g.pages.splice(pageIdx);
    var folderInput = document.getElementById('splitOutputFolderName');
    var base = (folderInput && folderInput.value) || 'Issue';
    st.groups.splice(groupIdx + 1, 0, {
      name: base + ' ' + (st.groups.length + 1),
      pages: moved
    });
    _render();
  };

  CLU.mergeGroupUp = function (groupIdx) {
    var st = CLU._splitState;
    if (!st || groupIdx <= 0 || !st.groups[groupIdx]) return;
    st.groups[groupIdx - 1].pages =
      st.groups[groupIdx - 1].pages.concat(st.groups[groupIdx].pages);
    st.groups.splice(groupIdx, 1);
    _render();
  };

  CLU.saveSplitCBZ = function () {
    var st = CLU._splitState;
    if (!st) return;

    var invalid = st.groups.filter(function (g) { return !g.name || !g.name.trim(); });
    if (invalid.length) {
      CLU.showToast('Missing name', 'Every issue needs an output filename.', 'error');
      return;
    }

    var payload = {
      folder_name: st.folderName,
      root_folder: st.rootFolder,
      original_file_path: st.originalPath,
      output_folder_name: (document.getElementById('splitOutputFolderName') || {}).value || '',
      groups: st.groups.map(function (g) {
        return {
          output_name: g.name.trim(),
          rel_paths: g.pages.map(function (p) { return p.rel_path; })
        };
      })
    };

    var btn = document.getElementById('splitCommitBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Splitting…'; }

    fetch('/api/split-cbz/commit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (!res.ok || !res.data.success) {
          throw new Error((res.data && res.data.error) || 'Split failed.');
        }
        CLU.showSuccess
          ? CLU.showSuccess('Split complete', 'Created ' + res.data.count + ' issue file(s).')
          : CLU.showToast('Success', 'Created ' + res.data.count + ' issue file(s).', 'success');
        var modalEl = document.getElementById('splitCBZModal');
        var modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();
        var contract = _getContract();
        if (typeof contract.onSaveComplete === 'function') {
          contract.onSaveComplete(st.originalPath);
        }
      })
      .catch(function (err) {
        CLU.showToast('Error', err.message, 'error');
      })
      .finally(function () {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-scissors"></i> Split'; }
      });
  };
})();
