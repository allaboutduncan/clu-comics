/**
 * Shared personalization widgets: the Bootswatch theme picker preview and the
 * dashboard layout editor.
 *
 * Used by both the owner-only site defaults on /config and the per-user
 * overrides on /account, so element ids are passed in rather than hardcoded —
 * the two pages must be able to render these side by side without colliding.
 *
 * These helpers only render and read the DOM. Saving stays in the page, because
 * the two pages post to different endpoints (/api/config/* vs /api/account/*).
 */
(function (global) {
  'use strict';

  const CLU = (global.CLU = global.CLU || {});

  // ---------------------------------------------------------------------------
  // Theme picker preview
  // ---------------------------------------------------------------------------

  /**
   * Wire a theme <select> to its thumbnail + name preview.
   * @param {{selectId: string, imgId: string, nameId: string}} opts
   */
  CLU.initThemePreview = function (opts) {
    const select = document.getElementById(opts.selectId);
    if (!select) return;

    function update() {
      const img = document.getElementById(opts.imgId);
      const name = document.getElementById(opts.nameId);
      if (!img || !name) return;

      const theme = select.value;
      const label = select.options[select.selectedIndex].text;

      img.src = 'https://bootswatch.com/' + theme + '/thumbnail.png';
      // Drop the "(Dark)" suffix — the preview image already shows that.
      name.textContent = label.replace(' (Dark)', '');
    }

    select.addEventListener('change', update);
    update();
  };

  // ---------------------------------------------------------------------------
  // Dashboard layout editor
  // ---------------------------------------------------------------------------

  /**
   * Turn a <ul> of section <li>s into a reorderable list: numbered badges,
   * up/down buttons, and HTML5 drag-and-drop. Idempotent — safe to re-run.
   * @param {string} listId
   */
  function renderDashboardList(listId) {
    const list = document.getElementById(listId);
    if (!list) return;

    const items = Array.from(list.children);
    list.innerHTML = '';

    items.forEach(function (item, idx) {
      const oldBtnGroup = item.querySelector('.dashboard-btn-group');
      if (oldBtnGroup) oldBtnGroup.remove();

      let badge = item.querySelector('.dashboard-order-badge');
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'badge bg-secondary me-2 dashboard-order-badge';
        item
          .querySelector('div')
          .insertBefore(badge, item.querySelector('.form-check-input'));
      }
      badge.textContent = idx + 1;

      const btnGroup = document.createElement('div');
      btnGroup.className = 'dashboard-btn-group';
      if (idx > 0) {
        const upBtn = document.createElement('button');
        upBtn.type = 'button';
        upBtn.className = 'btn btn-sm btn-outline-secondary me-1';
        upBtn.title = 'Move up';
        upBtn.innerHTML = '<i class="bi bi-arrow-up"></i>';
        upBtn.onclick = function () { moveDashboardSection(listId, idx, idx - 1); };
        btnGroup.appendChild(upBtn);
      }
      if (idx < items.length - 1) {
        const downBtn = document.createElement('button');
        downBtn.type = 'button';
        downBtn.className = 'btn btn-sm btn-outline-secondary';
        downBtn.title = 'Move down';
        downBtn.innerHTML = '<i class="bi bi-arrow-down"></i>';
        downBtn.onclick = function () { moveDashboardSection(listId, idx, idx + 1); };
        btnGroup.appendChild(downBtn);
      }
      item.appendChild(btnGroup);

      item.draggable = true;
      item.addEventListener('dragstart', function (e) {
        e.dataTransfer.setData('text/plain', idx);
        item.classList.add('opacity-50');
      });
      item.addEventListener('dragend', function () {
        item.classList.remove('opacity-50');
      });
      item.addEventListener('dragover', function (e) {
        e.preventDefault();
        item.classList.add('border-primary');
      });
      item.addEventListener('dragleave', function () {
        item.classList.remove('border-primary');
      });
      item.addEventListener('drop', function (e) {
        e.preventDefault();
        item.classList.remove('border-primary');
        const fromIdx = parseInt(e.dataTransfer.getData('text/plain'), 10);
        moveDashboardSection(listId, fromIdx, idx);
      });

      list.appendChild(item);
    });
  }

  function moveDashboardSection(listId, fromIdx, toIdx) {
    const list = document.getElementById(listId);
    if (!list) return;
    const items = Array.from(list.children);
    const [moved] = items.splice(fromIdx, 1);
    items.splice(toIdx, 0, moved);
    list.innerHTML = '';
    items.forEach(function (item) { list.appendChild(item); });
    renderDashboardList(listId);
  }

  /** Render/refresh the editor for the given list. */
  CLU.initDashboardEditor = function (opts) {
    renderDashboardList(typeof opts === 'string' ? opts : opts.listId);
  };

  /**
   * Read the current selection out of the DOM.
   * @returns {{order: string[], hidden: string[]}}
   */
  CLU.readDashboardSelection = function (listId) {
    const order = [];
    const hidden = [];
    document.querySelectorAll('#' + listId + ' li').forEach(function (item) {
      const id = item.dataset.sectionId;
      if (!id) return;
      order.push(id);
      const check = item.querySelector('.dashboard-visible-check');
      if (check && !check.checked) hidden.push(id);
    });
    return { order: order, hidden: hidden };
  };
})(window);
