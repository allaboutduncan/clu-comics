/**
 * CLU series.json  –  clu-series-json.js
 *
 * Create or update a folder's series.json with a chosen metadata provider.
 * Provides: CLU.openSeriesJsonModal, CLU.submitSeriesJson
 *
 * Depends on: clu-utils.js (CLU.showToast)
 *
 * External contract:
 *   window._cluSeriesJson = { onComplete(path, result) }
 *
 * DOM contracts:
 *   #seriesJsonModal  (from modal_series_json.html)
 *   #seriesJsonProvider, #seriesJsonFolderName, #seriesJsonConfirmBtn
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  var _currentPath = '';
  var _busy = false;

  var PROVIDER_LABELS = { metron: 'Metron', comicvine: 'ComicVine' };

  CLU.openSeriesJsonModal = function (folderPath, folderName) {
    _currentPath = folderPath;
    var nameEl = document.getElementById('seriesJsonFolderName');
    if (nameEl) nameEl.textContent = folderName || folderPath;
    var modalEl = document.getElementById('seriesJsonModal');
    if (modalEl) bootstrap.Modal.getOrCreateInstance(modalEl).show();
  };

  CLU.submitSeriesJson = function () {
    if (_busy) return;
    var providerEl = document.getElementById('seriesJsonProvider');
    var path = _currentPath;

    var modalEl = document.getElementById('seriesJsonModal');
    var inst = modalEl && bootstrap.Modal.getInstance(modalEl);
    if (inst) inst.hide();

    _busy = true;
    CLU.showToast('series.json', 'Fetching series data...', 'info');

    fetch('/api/series-json/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: path,
        provider: providerEl ? providerEl.value : 'auto',
        // Always rebuild from the provider: nobody hand-edits series.json.
        force: true
      })
    })
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (!result.success) {
          CLU.showToast('series.json', result.error || 'Could not write series.json', 'error');
          return;
        }
        var used = PROVIDER_LABELS[result.provider] || result.provider;
        var msg = 'Written using ' + used;
        if (result.description_from && result.description_from !== result.provider) {
          msg += ' (description from ' + (PROVIDER_LABELS[result.description_from] || result.description_from) + ')';
        } else if (!result.description_from) {
          msg += ' (no provider had a description)';
        }
        CLU.showToast('series.json', msg, 'success');
        var contract = window._cluSeriesJson || {};
        if (typeof contract.onComplete === 'function') contract.onComplete(path, result);
      })
      .catch(function (err) {
        CLU.showToast('series.json', err.message, 'error');
      })
      .finally(function () { _busy = false; });
  };

  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('seriesJsonConfirmBtn');
    if (btn) btn.addEventListener('click', CLU.submitSeriesJson);
  });

})();
