/**
 * CLU confirm  –  clu-confirm.js
 *
 * The one generic "are you sure?" modal. base.html loads this right after
 * Bootstrap, so every page has it. Never use window.confirm(): it blocks the
 * page, ignores the theme and cannot be styled or tested.
 *
 *   const ok = await CLU.confirm({
 *     title: 'Delete library',             // default 'Are you sure?'
 *     message: 'Delete the library "X"?',   // text, never HTML
 *     detail: 'No files are deleted.',      // optional second, muted line
 *     confirmText: 'Delete',                // default 'Confirm'
 *     cancelText: 'Cancel',
 *     variant: 'danger'                     // Bootstrap button variant
 *   });
 *   if (!ok) return;
 *
 * It resolves true only when the confirm button is pressed; Cancel, the close
 * button, Escape and a click on the backdrop all resolve false. Deleting a
 * file or folder is CLU.showDeleteConfirmation (clu-delete.js), which says
 * where the file goes; this is for everything else.
 *
 * The modal is built on first use and reused. A second call while one is open
 * resolves the first as false.
 */
(function (window, document) {
  'use strict';

  var CLU = window.CLU = window.CLU || {};
  var MODAL_ID = 'cluConfirmModal';
  var pending = null;   // resolve() of the confirmation on screen
  var answered = false; // the confirm button was pressed; the modal is closing

  function build() {
    var el = document.createElement('div');
    el.id = MODAL_ID;
    el.className = 'modal fade';
    el.tabIndex = -1;
    el.setAttribute('aria-labelledby', MODAL_ID + 'Title');
    el.setAttribute('aria-hidden', 'true');
    el.innerHTML =
      '<div class="modal-dialog modal-dialog-centered">' +
        '<div class="modal-content">' +
          '<div class="modal-header">' +
            '<h5 class="modal-title" id="' + MODAL_ID + 'Title"></h5>' +
            '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>' +
          '</div>' +
          '<div class="modal-body">' +
            '<p class="mb-0" data-clu-confirm="message" style="white-space: pre-line;"></p>' +
            '<p class="text-muted small mt-2 mb-0" data-clu-confirm="detail"></p>' +
          '</div>' +
          '<div class="modal-footer">' +
            '<button type="button" class="btn btn-secondary" data-bs-dismiss="modal" data-clu-confirm="cancel"></button>' +
            '<button type="button" class="btn" data-clu-confirm="ok"></button>' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(el);

    el.querySelector('[data-clu-confirm="ok"]').addEventListener('click', function () {
      answered = true;
      settle(true);
      window.bootstrap.Modal.getOrCreateInstance(el).hide();
    });
    // Cancel, the close button, Escape and the backdrop all end here. After
    // the confirm button, "hidden" is only the close animation finishing: a
    // confirmation requested in the meantime (two questions in a row) must not
    // be answered "no" by it, and Bootstrap ignored its show() mid-transition,
    // so it is shown now.
    el.addEventListener('hidden.bs.modal', function () {
      if (answered) {
        answered = false;
        if (pending) window.bootstrap.Modal.getOrCreateInstance(el).show();
        return;
      }
      settle(false);
    });
    // Asked from inside another modal (the CBZ editor's unsaved-changes
    // guard): sit above it, backdrop included.
    el.addEventListener('shown.bs.modal', function () {
      var backdrops = document.querySelectorAll('.modal-backdrop');
      if (document.querySelectorAll('.modal.show').length > 1 && backdrops.length) {
        el.style.zIndex = '1065';
        backdrops[backdrops.length - 1].style.zIndex = '1060';
      } else {
        el.style.zIndex = '';
      }
    });
    return el;
  }

  function settle(value) {
    var resolve = pending;
    pending = null;
    if (resolve) resolve(value);
  }

  CLU.confirm = function (options) {
    var opts = typeof options === 'string' ? { message: options } : (options || {});
    return new Promise(function (resolve) {
      if (!window.bootstrap || !window.bootstrap.Modal) {
        // No Bootstrap, no modal: refuse rather than act unconfirmed.
        resolve(false);
        return;
      }
      settle(false);
      var el = document.getElementById(MODAL_ID) || build();
      el.querySelector('.modal-title').textContent = opts.title || 'Are you sure?';
      el.querySelector('[data-clu-confirm="message"]').textContent = opts.message || '';
      var detail = el.querySelector('[data-clu-confirm="detail"]');
      detail.textContent = opts.detail || '';
      detail.hidden = !opts.detail;
      el.querySelector('[data-clu-confirm="cancel"]').textContent = opts.cancelText || 'Cancel';
      var ok = el.querySelector('[data-clu-confirm="ok"]');
      ok.textContent = opts.confirmText || 'Confirm';
      ok.className = 'btn btn-' + (opts.variant || 'danger');
      pending = resolve;
      window.bootstrap.Modal.getOrCreateInstance(el).show();
    });
  };
})(window, document);
