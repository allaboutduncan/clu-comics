/**
 * CLU toasts  –  clu-toast.js
 *
 * The one toast. base.html loads this right after Bootstrap and renders the
 * container (partials/toast_container.html), so every page has both. Pages
 * must not define their own showToast: there were eight, four of them white
 * boxes that ignored the theme, and three different argument orders.
 *
 *   CLU.toast(message, type, opts)          message first (most callers)
 *   CLU.showToast(title, message, type, opts)  with a bold title line
 *   CLU.showSuccess(message) / CLU.showError(message)
 *
 *   type: 'success' | 'error' | 'warning' | 'info'  ('danger' = 'error')
 *   opts: a number (ms on screen; 0 = stays until closed) or
 *         { duration, title }
 *
 * Messages are text, never HTML. Errors and warnings are announced
 * assertively (role=alert); success and info politely (role=status). The same
 * message shown again while its toast is still up restarts that toast instead
 * of stacking a copy, so a failing poll cannot fill the screen.
 */
(function (window, document) {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  var KINDS = {
    success: { bg: 'success', icon: 'bi-check-circle-fill', label: 'Success', delay: 4000 },
    error: { bg: 'danger', icon: 'bi-exclamation-octagon-fill', label: 'Error', delay: 8000 },
    warning: { bg: 'warning', icon: 'bi-exclamation-triangle-fill', label: 'Warning', delay: 6000 },
    info: { bg: 'info', icon: 'bi-info-circle-fill', label: 'Note', delay: 5000 }
  };
  var MAX_VISIBLE = 4;

  function kindOf(type) {
    if (type === 'danger') type = 'error';
    return KINDS[type] ? type : 'info';
  }

  /** The page's toast container (created if a page renders without base.html). */
  CLU.toastContainer = function () {
    var c = document.getElementById('toast-container');
    if (!c) {
      c = document.createElement('div');
      c.id = 'toast-container';
      c.className = 'toast-container clu-toasts position-fixed bottom-0 end-0 p-3';
      document.body.appendChild(c);
    }
    return c;
  };

  function render(title, message, type, opts) {
    if (typeof opts === 'number') opts = { duration: opts };
    opts = opts || {};
    var key = kindOf(type);
    var kind = KINDS[key];
    var text = (message === null || message === undefined) ? '' : String(message);
    var heading = title || opts.title || '';
    var duration = typeof opts.duration === 'number' ? opts.duration : kind.delay;

    if (typeof bootstrap === 'undefined' || !bootstrap.Toast) {
      // No Bootstrap means no page chrome either; log instead of a native dialog.
      console.warn('[toast] ' + key + ': ' + (heading ? heading + ': ' : '') + text);
      return null;
    }

    var container = CLU.toastContainer();
    var sig = key + '\n' + heading + '\n' + text;

    // Same toast still on screen: restart it rather than stack a copy.
    var existing = Array.prototype.find.call(container.children, function (el) {
      return el._cluSig === sig && el.classList.contains('show');
    });
    if (existing) {
      // Toast.show() on a shown toast clears its hide timer and starts a new one.
      container.appendChild(existing);
      bootstrap.Toast.getOrCreateInstance(existing).show();
      return existing;
    }

    var el = document.createElement('div');
    el._cluSig = sig;
    el.className = 'toast clu-toast border-0 text-bg-' + kind.bg;
    var urgent = key === 'error' || key === 'warning';
    el.setAttribute('role', urgent ? 'alert' : 'status');
    el.setAttribute('aria-live', urgent ? 'assertive' : 'polite');
    el.setAttribute('aria-atomic', 'true');

    var row = document.createElement('div');
    row.className = 'd-flex align-items-start';

    var body = document.createElement('div');
    body.className = 'toast-body d-flex gap-2 flex-grow-1';
    var icon = document.createElement('i');
    icon.className = 'bi ' + kind.icon + ' clu-toast-icon';
    icon.setAttribute('aria-hidden', 'true');
    var textWrap = document.createElement('div');
    textWrap.className = 'clu-toast-text';
    if (heading) {
      var strong = document.createElement('strong');
      strong.className = 'd-block';
      strong.textContent = heading;
      textWrap.appendChild(strong);
    } else {
      // The icon carries the type on screen; say it for screen readers.
      var sr = document.createElement('span');
      sr.className = 'visually-hidden';
      sr.textContent = kind.label + ': ';
      textWrap.appendChild(sr);
    }
    textWrap.appendChild(document.createTextNode(text));
    body.appendChild(icon);
    body.appendChild(textWrap);

    // An icon in currentColor, not .btn-close: the themes pick dark or light
    // text for each text-bg-* fill, and the × should match whichever it got.
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'clu-toast-close';
    close.setAttribute('data-bs-dismiss', 'toast');
    close.setAttribute('aria-label', 'Close');
    var x = document.createElement('i');
    x.className = 'bi bi-x-lg';
    x.setAttribute('aria-hidden', 'true');
    close.appendChild(x);

    row.appendChild(body);
    row.appendChild(close);
    el.appendChild(row);

    // Keep the stack short: drop the oldest of our own self-closing toasts.
    // A toast that stays until closed (duration 0) is waiting on the user, and
    // a toast a page built itself (a confirm, a progress bar, a manual-download
    // link) is the page's to remove, so neither is ever evicted.
    var evictable = Array.prototype.filter.call(container.children, function (c) {
      return c._cluSig !== undefined && !c._cluSticky;
    });
    while (evictable.length && container.children.length >= MAX_VISIBLE) {
      container.removeChild(evictable.shift());
    }
    el._cluSticky = duration <= 0;
    container.appendChild(el);

    var toast = new bootstrap.Toast(el, { autohide: duration > 0, delay: duration || 5000 });
    el.addEventListener('hidden.bs.toast', function () {
      if (el.parentNode && !el.classList.contains('show')) el.parentNode.removeChild(el);
    });
    toast.show();
    return el;
  }

  CLU.toast = function (message, type, opts) {
    return render(null, message, type, opts);
  };

  CLU.showToast = function (title, message, type, opts) {
    return render(title, message, type, opts);
  };

  CLU.showSuccess = function (message) {
    return render(null, message, 'success');
  };

  CLU.showError = function (message) {
    return render(null, message, 'error');
  };
})(window, document);
