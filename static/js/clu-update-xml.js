/**
 * CLU Update XML  –  clu-update-xml.js
 *
 * Batch update a ComicInfo.xml field across all CBZ files in a directory.
 * Provides: CLU.openUpdateXmlModal, CLU.submitUpdateXml,
 *           CLU.updateXmlFieldChanged, CLU.updateXmlFieldConfig
 *
 * Depends on: clu-utils.js (CLU.showToast, CLU.showError)
 *
 * External contract:
 *   window._cluUpdateXml = { onUpdateComplete(field, value, result) }
 *
 * DOM contracts:
 *   #updateXmlModal  (from modal_update_xml.html)
 *   #updateXmlField, #updateXmlValue, #updateXmlHint,
 *   #updateXmlFolderName, #updateXmlConfirmBtn
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  function _getContract() { return window._cluUpdateXml || {}; }

  // ── Module state ──────────────────────────────────────────────────────────

  var _currentPath = '';

  // ── Field configuration ───────────────────────────────────────────────────

  CLU.updateXmlFieldConfig = {
    Volume: {
      hint: 'Enter a 4-digit year (e.g., 2024)',
      placeholder: 'Enter year',
      maxlength: 4,
      validate: function (v) { return /^\d{4}$/.test(v) ? null : 'Volume must be a 4-digit year'; }
    },
    Publisher: {
      hint: 'Enter the publisher name (e.g., Marvel Comics)',
      placeholder: 'Enter publisher',
      maxlength: null,
      validate: function (v) { return v ? null : 'Publisher cannot be empty'; }
    },
    Series: {
      hint: 'Enter the series name (e.g., The Amazing Spider-Man)',
      placeholder: 'Enter series',
      maxlength: null,
      validate: function (v) { return v ? null : 'Series cannot be empty'; }
    },
    SeriesGroup: {
      hint: 'Enter the series group (e.g., Spider-Man)',
      placeholder: 'Enter series group',
      maxlength: null,
      validate: function (v) { return v ? null : 'Series Group cannot be empty'; }
    }
  };

  // ── updateXmlFieldChanged ─────────────────────────────────────────────────

  CLU.updateXmlFieldChanged = function () {
    var field = document.getElementById('updateXmlField');
    if (!field) return;
    var cfg = CLU.updateXmlFieldConfig[field.value];
    if (!cfg) return;

    var input = document.getElementById('updateXmlValue');
    var hint = document.getElementById('updateXmlHint');
    if (input) {
      input.placeholder = cfg.placeholder;
      if (cfg.maxlength) { input.setAttribute('maxlength', cfg.maxlength); }
      else { input.removeAttribute('maxlength'); }
    }
    if (hint) hint.textContent = cfg.hint;
  };

  // ── openUpdateXmlModal ────────────────────────────────────────────────────

  CLU.openUpdateXmlModal = function (folderPath, folderName) {
    _currentPath = folderPath;
    var nameEl = document.getElementById('updateXmlFolderName');
    if (nameEl) nameEl.textContent = folderName;

    var valEl = document.getElementById('updateXmlValue');
    if (valEl) valEl.value = '';

    var fieldEl = document.getElementById('updateXmlField');
    if (fieldEl) fieldEl.value = 'Volume';

    CLU.updateXmlFieldChanged();

    var modal = new bootstrap.Modal(document.getElementById('updateXmlModal'));
    modal.show();
  };

  // ── submitUpdateXml ───────────────────────────────────────────────────────

  CLU.submitUpdateXml = function () {
    var fieldEl = document.getElementById('updateXmlField');
    var valueEl = document.getElementById('updateXmlValue');
    if (!fieldEl || !valueEl) return;

    var field = fieldEl.value;
    var value = valueEl.value.trim();

    var cfg = CLU.updateXmlFieldConfig[field];
    var err = cfg ? cfg.validate(value) : (!value ? 'Please enter a value' : null);
    if (err) {
      CLU.showToast('Validation Error', err, 'warning');
      return;
    }

    // Close modal
    var modalInst = bootstrap.Modal.getInstance(document.getElementById('updateXmlModal'));
    if (modalInst) modalInst.hide();

    CLU.showToast('Updating XML', 'Updating ' + field + ' in all CBZ files...', 'info');

    fetch('/api/update-xml', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ directory: _currentPath, field: field, value: value })
    })
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (result.error) {
          CLU.showToast('Update Error', result.error, 'error');
        } else {
          CLU.showToast('Update Complete',
            'Updated ' + result.updated + ' file(s), skipped ' + result.skipped,
            result.updated > 0 ? 'success' : 'info');

          var contract = _getContract();
          if (typeof contract.onUpdateComplete === 'function') {
            contract.onUpdateComplete(field, value, result);
          }
        }
      })
      .catch(function (error) {
        CLU.showToast('Update Error', error.message, 'error');
      });
  };

  // ── DOM wiring ────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('updateXmlConfirmBtn');
    if (btn) btn.addEventListener('click', CLU.submitUpdateXml);
  });

})();
