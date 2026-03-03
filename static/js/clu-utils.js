/**
 * CLU Shared Utilities  –  clu-utils.js
 *
 * Foundation module for Comic Library Utilities shared modules.
 * Provides: CLU.escapeHtml, CLU.formatFileSize, CLU.showToast,
 *           CLU.showSuccess, CLU.showError, CLU.showProgressIndicator,
 *           CLU.hideProgressIndicator, CLU.updateProgress
 *
 * DOM contracts (optional – functions degrade gracefully):
 *   - .toast-container          dynamic toasts are appended here
 *   - #successToast / #successToastBody   pre-built success toast
 *   - #errorToast   / #errorToastBody     pre-built error toast
 *   - #progress-container / #progress-bar / #progress-text
 *
 * Must be loaded before any other clu-*.js module.
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  // ── escapeHtml ──────────────────────────────────────────────────────────

  CLU.escapeHtml = function (text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  };

  // ── formatFileSize ──────────────────────────────────────────────────────

  CLU.formatFileSize = function (bytes) {
    if (bytes === 0) return '0 B';
    var k = 1024;
    var sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  // ── Toast helpers ───────────────────────────────────────────────────────

  /**
   * Show a toast notification.
   *
   * Tries these strategies in order:
   *   1. Dynamic creation into .toast-container  (files.js style)
   *   2. Pre-built #successToast / #errorToast   (collection.js style)
   *   3. Standalone #swToast element             (source_wall.js style)
   *   4. alert() fallback
   *
   * @param {string} title   - Header text (ignored when using pre-built toasts)
   * @param {string} message - Body text
   * @param {string} [type='info'] - 'info' | 'success' | 'warning' | 'error'
   */
  CLU.showToast = function (title, message, type) {
    type = type || 'info';

    // Strategy 1 — dynamic toast into .toast-container
    var container = document.querySelector('.toast-container');
    if (container && typeof bootstrap !== 'undefined' && bootstrap.Toast) {
      var bgClass = type === 'error' ? 'danger'
                  : type === 'success' ? 'success'
                  : type === 'warning' ? 'warning'
                  : 'info';
      var textClass = type === 'warning' ? '' : 'text-white';

      var toastEl = document.createElement('div');
      toastEl.className = 'toast bg-' + bgClass + ' ' + textClass;
      toastEl.setAttribute('role', 'alert');
      toastEl.setAttribute('aria-live', 'assertive');
      toastEl.setAttribute('aria-atomic', 'true');
      toastEl.innerHTML =
        '<div class="toast-header bg-' + bgClass + ' ' + textClass + '">' +
          '<strong class="me-auto">' + title + '</strong>' +
          '<button type="button" class="btn-close' + (textClass ? ' btn-close-white' : '') + '" data-bs-dismiss="toast" aria-label="Close"></button>' +
        '</div>' +
        '<div class="toast-body">' + message + '</div>';

      container.appendChild(toastEl);

      try {
        var toast = new bootstrap.Toast(toastEl);
        toast.show();
      } catch (e) {
        container.removeChild(toastEl);
        alert(title + ': ' + message);
        return;
      }

      toastEl.addEventListener('hidden.bs.toast', function () {
        if (toastEl.parentNode === container) {
          container.removeChild(toastEl);
        }
      });
      return;
    }

    // Strategy 2 — pre-built success/error toasts (collection.html)
    if ((type === 'success' || type === 'error') && typeof bootstrap !== 'undefined' && bootstrap.Toast) {
      var elId = type === 'success' ? 'successToast' : 'errorToast';
      var bodyId = type === 'success' ? 'successToastBody' : 'errorToastBody';
      var el = document.getElementById(elId);
      var body = document.getElementById(bodyId);
      if (el && body) {
        body.textContent = message;
        var t = new bootstrap.Toast(el, { autohide: true, delay: type === 'success' ? 3000 : 5000 });
        t.show();
        return;
      }
    }

    // Strategy 3 — swToast (source_wall.html)
    if (typeof bootstrap !== 'undefined' && bootstrap.Toast) {
      var swEl = document.getElementById('swToast');
      var swBody = document.getElementById('swToastBody');
      if (swEl && swBody) {
        var cls = type === 'error' ? 'text-bg-danger'
                : type === 'success' ? 'text-bg-success'
                : type === 'warning' ? 'text-bg-warning'
                : 'text-bg-info';
        swEl.className = 'toast align-items-center border-0 ' + cls;
        swBody.textContent = message;
        bootstrap.Toast.getOrCreateInstance(swEl, { delay: 3000 }).show();
        return;
      }
    }

    // Strategy 4 — fallback
    alert(title + ': ' + message);
  };

  /**
   * Convenience: show a success toast.
   * @param {string} message
   */
  CLU.showSuccess = function (message) {
    CLU.showToast('Success', message, 'success');
  };

  /**
   * Convenience: show an error toast.
   * @param {string} message
   */
  CLU.showError = function (message) {
    CLU.showToast('Error', message, 'error');
  };

  // ── Progress indicator ──────────────────────────────────────────────────

  CLU.showProgressIndicator = function () {
    var el = document.getElementById('progress-container');
    if (el) el.style.display = 'block';
  };

  CLU.hideProgressIndicator = function () {
    var el = document.getElementById('progress-container');
    if (el) el.style.display = 'none';
  };

  /**
   * Update the progress bar and text.
   * @param {number} percent  0-100
   * @param {string} [text]   status message
   */
  CLU.updateProgress = function (percent, text) {
    var bar = document.getElementById('progress-bar');
    var txt = document.getElementById('progress-text');
    if (bar) {
      bar.style.width = percent + '%';
      bar.textContent = Math.round(percent) + '%';
      bar.setAttribute('aria-valuenow', String(percent));
    }
    if (txt && text !== undefined) {
      txt.textContent = text;
    }
  };

  /**
   * Reset the progress bar to 0% with an optional status class.
   * @param {string} [statusClass]  e.g. 'bg-danger' for error state
   */
  CLU.resetProgress = function (statusClass) {
    var bar = document.getElementById('progress-bar');
    var txt = document.getElementById('progress-text');
    if (bar) {
      bar.style.width = '0%';
      bar.textContent = '0%';
      bar.setAttribute('aria-valuenow', '0');
      bar.className = 'progress-bar progress-bar-striped progress-bar-animated';
      if (statusClass) {
        bar.className = 'progress-bar ' + statusClass;
      }
    }
    if (txt) {
      txt.textContent = 'Initializing...';
    }
  };

})();
