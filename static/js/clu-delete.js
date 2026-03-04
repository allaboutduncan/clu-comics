/**
 * CLU Delete  –  clu-delete.js
 *
 * Single-file and bulk delete confirmation modals with API integration.
 * Provides: CLU.showDeleteConfirmation, CLU.confirmDelete,
 *           CLU.showBulkDeleteConfirmation, CLU.confirmBulkDelete
 *
 * Depends on: clu-utils.js (CLU.showToast, CLU.showError, CLU.showSuccess,
 *             CLU.formatFileSize, CLU.showProgressIndicator, CLU.hideProgressIndicator)
 *
 * External contract:
 *   window._cluDelete = {
 *     // Single delete
 *     deleteEndpoint: '/api/delete-file',        // default
 *     deletePayload:  function(path) {},          // default: { path: path }
 *     onDeleteComplete(path, result),
 *     onDeleteError(path, error),
 *
 *     // Bulk delete
 *     onBulkDeleteComplete(paths, results),
 *     onBulkDeleteError(paths, error)
 *   }
 *
 * DOM contracts:
 *   #deleteConfirmModal  (from modal_delete_confirm.html)
 *   #deleteFileName, #deleteFileDetails, #deleteFileSize, #deleteFilePath,
 *   #confirmDeleteBtn
 *   #deleteMultipleModal
 *   #deleteMultipleCount, #deleteMultipleFileList, #confirmDeleteMultipleBtn
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  function _getContract() { return window._cluDelete || {}; }

  // ── Module state ──────────────────────────────────────────────────────────

  var _currentDeletePath = '';
  var _currentDeleteName = '';
  var _currentBulkPaths = [];

  // ── showDeleteConfirmation ────────────────────────────────────────────────

  /**
   * Show a single-file delete confirmation modal.
   * @param {string} path  - Full file path
   * @param {string} name  - Display name
   * @param {Object} [options] - { size: number, type: string, showDetails: boolean }
   */
  CLU.showDeleteConfirmation = function (path, name, options) {
    _currentDeletePath = path;
    _currentDeleteName = name;
    var opts = options || {};

    var nameEl = document.getElementById('deleteFileName');
    if (nameEl) nameEl.textContent = name;

    // Detail section (size + path) — shown when showDetails is true
    var detailsEl = document.getElementById('deleteFileDetails');
    if (detailsEl) {
      if (opts.showDetails) {
        detailsEl.style.display = '';
        var sizeEl = document.getElementById('deleteFileSize');
        if (sizeEl) {
          sizeEl.textContent = opts.type === 'folder'
            ? 'Folder'
            : CLU.formatFileSize(opts.size || 0);
        }
        var pathEl = document.getElementById('deleteFilePath');
        if (pathEl) pathEl.textContent = path;
      } else {
        detailsEl.style.display = 'none';
      }
    }

    var modal = new bootstrap.Modal(document.getElementById('deleteConfirmModal'));
    modal.show();
  };

  // ── confirmDelete ─────────────────────────────────────────────────────────

  CLU.confirmDelete = function () {
    var contract = _getContract();
    var endpoint = contract.deleteEndpoint || '/api/delete-file';
    var payload = typeof contract.deletePayload === 'function'
      ? contract.deletePayload(_currentDeletePath)
      : { path: _currentDeletePath };

    // Close modal
    var modalEl = document.getElementById('deleteConfirmModal');
    var modalInst = bootstrap.Modal.getInstance(modalEl);
    if (modalInst) modalInst.hide();

    fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (result.success) {
          if (typeof updateTrashBadge === 'function') updateTrashBadge();
          if (typeof contract.onDeleteComplete === 'function') {
            contract.onDeleteComplete(_currentDeletePath, result);
          } else {
            CLU.showSuccess('File moved to trash');
          }
        } else {
          var errMsg = result.error || 'Unknown error';
          if (typeof contract.onDeleteError === 'function') {
            contract.onDeleteError(_currentDeletePath, errMsg);
          } else {
            CLU.showError('Error deleting file: ' + errMsg);
          }
        }
      })
      .catch(function (err) {
        if (typeof contract.onDeleteError === 'function') {
          contract.onDeleteError(_currentDeletePath, err.message);
        } else {
          CLU.showError('Delete failed: ' + err.message);
        }
      });
  };

  // ── showBulkDeleteConfirmation ────────────────────────────────────────────

  /**
   * Show a bulk delete confirmation modal with a file list.
   * @param {string[]} paths - Array of file paths to delete
   */
  CLU.showBulkDeleteConfirmation = function (paths) {
    _currentBulkPaths = paths;

    var countEl = document.getElementById('deleteMultipleCount');
    if (countEl) countEl.textContent = paths.length;

    var listEl = document.getElementById('deleteMultipleFileList');
    if (listEl) {
      listEl.innerHTML = '';
      paths.forEach(function (p) {
        var li = document.createElement('li');
        li.className = 'list-group-item';
        li.textContent = p.split('/').pop();
        listEl.appendChild(li);
      });
    }

    var modal = new bootstrap.Modal(document.getElementById('deleteMultipleModal'));
    modal.show();
  };

  // ── confirmBulkDelete ─────────────────────────────────────────────────────

  CLU.confirmBulkDelete = function () {
    var contract = _getContract();
    var paths = _currentBulkPaths;
    if (!paths || paths.length === 0) return;

    var modalInst = bootstrap.Modal.getInstance(
      document.getElementById('deleteMultipleModal')
    );
    if (modalInst) modalInst.hide();

    fetch('/api/delete-multiple', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ targets: paths })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.results) {
          CLU.showError('Unexpected response from server');
          return;
        }

        if (typeof updateTrashBadge === 'function') updateTrashBadge();

        if (typeof contract.onBulkDeleteComplete === 'function') {
          contract.onBulkDeleteComplete(paths, data.results);
        } else {
          var successes = data.results.filter(function (r) { return r.success; });
          var failures = data.results.filter(function (r) { return !r.success; });
          if (successes.length > 0) {
            CLU.showSuccess('Deleted ' + successes.length + ' file(s)');
          }
          if (failures.length > 0) {
            CLU.showError(failures.length + ' file(s) failed to delete');
          }
        }
      })
      .catch(function (err) {
        if (typeof contract.onBulkDeleteError === 'function') {
          contract.onBulkDeleteError(paths, err.message);
        } else {
          CLU.showError('Error deleting files: ' + err.message);
        }
      });
  };

  // ── DOM wiring ────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    // Single delete confirm button
    var confirmBtn = document.getElementById('confirmDeleteBtn');
    if (confirmBtn) confirmBtn.addEventListener('click', CLU.confirmDelete);

    // Bulk delete confirm button
    var bulkBtn = document.getElementById('confirmDeleteMultipleBtn');
    if (bulkBtn) bulkBtn.addEventListener('click', CLU.confirmBulkDelete);

    // Keyboard support: Enter key to confirm in single delete modal
    var deleteModal = document.getElementById('deleteConfirmModal');
    if (deleteModal) {
      deleteModal.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          CLU.confirmDelete();
        }
      });
    }

    // Keyboard support: Enter key to confirm in bulk delete modal
    var bulkModal = document.getElementById('deleteMultipleModal');
    if (bulkModal) {
      bulkModal.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          CLU.confirmBulkDelete();
        }
      });
    }
  });

})();
