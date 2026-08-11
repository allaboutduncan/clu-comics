/**
 * Add to Pull List — shared "Scan Library for one folder" flow.
 *
 * Entry point: CLU.addFolderToPullList(folderPath, folderName)
 *
 * Posts the folder to /api/pull-list/add-folder, which resolves it from its
 * series.json / cvinfo sidecars exactly like the Scan Library button on the
 * Pull List page. A folder with no usable sidecar comes back as `needs_match`,
 * and this opens the picker modal so the user can choose the series by hand.
 *
 * Requires: clu-utils.js (CLU.showToast) and, for the manual pick,
 * partials/modal_add_to_pull_list.html included in the page.
 */
(function () {
  var CLU = window.CLU = window.CLU || {};

  var pickerModal = null;
  var pendingFolder = null;
  var pendingName = null;

  function folderLabel(folderPath, folderName) {
    if (folderName) return folderName;
    var parts = String(folderPath || '').replace(/\/+$/, '').split('/');
    return parts[parts.length - 1] || folderPath;
  }

  function esc(value) {
    var d = document.createElement('div');
    d.textContent = (value == null ? '' : String(value));
    return d.innerHTML;
  }

  /**
   * Flag a folder as a tracked series on the Pull List.
   *
   * @param {string} folderPath - Absolute path to the series folder.
   * @param {string} [folderName] - Display name for toasts (defaults to the leaf).
   */
  CLU.addFolderToPullList = function (folderPath, folderName) {
    if (!folderPath) return;
    pendingFolder = folderPath;
    pendingName = folderLabel(folderPath, folderName);
    CLU.showToast('Pull List', 'Checking ' + pendingName + '…', 'info');
    submit({ folder: folderPath });
  };

  function submit(body, onFailure) {
    return fetch('/api/pull-list/add-folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json(); })
      .then(function (data) { handleResult(data, onFailure); })
      .catch(function (error) {
        console.error('Error adding folder to Pull List:', error);
        CLU.showError('Failed to add to Pull List');
        if (onFailure) onFailure();
      });
  }

  function handleResult(data, onFailure) {
    var name = data.series_name || pendingName;

    switch (data.status) {
      case 'applied':
        hidePicker();
        CLU.showSuccess(name + ' added to your Pull List — syncing issues in the background');
        break;

      case 'already_mapped':
        hidePicker();
        CLU.showToast('Pull List', (name || pendingName) + ' is already tracked', 'info');
        break;

      case 'conflict':
        CLU.showToast(
          'Already mapped',
          name + ' is already mapped to ' + data.mapped_to,
          'warning'
        );
        if (onFailure) onFailure();
        break;

      case 'needs_match':
        openPicker(data);
        break;

      default:
        CLU.showError(data.error || 'Failed to add to Pull List');
        if (onFailure) onFailure();
    }
  }

  // ── Manual series picker ──────────────────────────────────────────────────

  function openPicker(data) {
    var modalEl = document.getElementById('addToPullListModal');
    if (!modalEl) {
      // Page didn't include the partial — report the reason rather than
      // silently doing nothing.
      CLU.showError(data.reason || 'No series.json or cvinfo in this folder');
      return;
    }

    var input = document.getElementById('pullListSearchInput');
    var reason = document.getElementById('pullListAddReason');

    if (reason) {
      reason.innerHTML = esc(pendingFolder) + ' — ' +
        esc(data.reason || 'No series.json or cvinfo in this folder') +
        '. Pick the series to track it.';
    }
    resetResults();
    if (input) input.value = data.suggested_name || pendingName || '';

    if (!pickerModal) {
      pickerModal = new bootstrap.Modal(modalEl);
      wirePicker();
    }
    pickerModal.show();
    if (input) {
      // Focus once the modal has finished animating in.
      modalEl.addEventListener('shown.bs.modal', function once() {
        modalEl.removeEventListener('shown.bs.modal', once);
        input.focus();
        input.select();
      });
    }
  }

  function hidePicker() {
    if (pickerModal) pickerModal.hide();
  }

  function wirePicker() {
    var btn = document.getElementById('pullListSearchBtn');
    var input = document.getElementById('pullListSearchInput');
    if (btn) btn.onclick = runSearch;
    if (input) {
      input.addEventListener('keypress', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          runSearch();
        }
      });
    }
  }

  function resetResults() {
    var table = document.getElementById('pullListResultsTable');
    var body = document.getElementById('pullListResultsBody');
    var status = document.getElementById('pullListSearchStatus');
    if (table) table.classList.add('d-none');
    if (body) body.innerHTML = '';
    if (status) status.textContent = '';
  }

  function runSearch() {
    var input = document.getElementById('pullListSearchInput');
    var status = document.getElementById('pullListSearchStatus');
    var query = input ? input.value.trim() : '';
    if (!query) return;

    resetResults();
    if (status) status.textContent = 'Searching…';

    fetch('/api/series/search?q=' + encodeURIComponent(query))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.success) {
          if (status) status.textContent = data.error || 'Search failed';
          return;
        }
        renderResults(data.series || []);
      })
      .catch(function (error) {
        console.error('Pull List series search failed:', error);
        if (status) status.textContent = 'Search failed';
      });
  }

  function renderResults(series) {
    var table = document.getElementById('pullListResultsTable');
    var body = document.getElementById('pullListResultsBody');
    var status = document.getElementById('pullListSearchStatus');
    if (!body) return;

    if (!series.length) {
      if (status) status.textContent = 'No matches — try a different name.';
      return;
    }
    if (status) status.textContent = '';

    body.innerHTML = series.map(function (s, i) {
      var volume = s.volume ? ' <span class="text-muted">Vol. ' + esc(s.volume) + '</span>' : '';
      var tracked = s.subscribed
        ? ' <span class="badge bg-secondary ms-1">Tracked</span>'
        : '';
      return '<tr>' +
        '<td class="fw-bold">' + esc(s.name) + volume + tracked + '</td>' +
        '<td>' + esc(s.publisher || '-') + '</td>' +
        '<td>' + esc(s.year_began || '-') + '</td>' +
        '<td class="text-center">' + esc(s.issue_count == null ? '-' : s.issue_count) + '</td>' +
        '<td class="text-end">' +
          '<button type="button" class="btn btn-sm btn-primary pull-list-pick" data-idx="' + i + '">' +
            '<i class="bi bi-plus-lg me-1"></i>Add' +
          '</button>' +
        '</td>' +
      '</tr>';
    }).join('');

    if (table) table.classList.remove('d-none');

    body.querySelectorAll('.pull-list-pick').forEach(function (btn) {
      btn.onclick = function () {
        var chosen = series[parseInt(btn.dataset.idx, 10)];
        if (!chosen || !pendingFolder) return;
        var original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
        submit({
          folder: pendingFolder,
          series_id: chosen.id,
          series_name: chosen.name,
          publisher_name: chosen.publisher,
          year: chosen.year_began,
          status: chosen.status
        }, function () {
          btn.disabled = false;
          btn.innerHTML = original;
        });
      };
    });
  }
})();
