/**
 * CLU CBZ Edit Operations  –  clu-cbz-edit.js
 *
 * Pending-changes edit modal: drag to reorder, click to rename, delete to flag.
 * Nothing hits the server until SAVE is clicked.
 *
 * Provides: CLU.generateCardHTML, CLU.sortInlineEditCards, CLU.deleteCardImage,
 *           CLU.enableFilenameEdit, CLU.performRename, CLU.saveEditedCBZ,
 *           CLU.setupEditModalDropZone, CLU.handleEditModalUpload,
 *           CLU.initEditModalReorder
 *
 * Depends on: clu-utils.js (CLU.showToast, CLU.showError, CLU.showSuccess),
 *             Sortable.js (loaded by templates/partials/modal_cbz_edit.html)
 *
 * External contract:
 *   window._cluCbzEdit = { onSaveComplete(filePath) }
 *
 * DOM contracts:
 *   #editCBZModal  (from modal_cbz_edit.html)
 *   #editInlineContainer, #editInlineFolderName, #editInlineZipFilePath,
 *   #editInlineOriginalFilePath, #editInlineSaveForm,
 *   #editInlinePendingDeletes, #editInlinePendingOrder
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  function _getContract() { return window._cluCbzEdit || {}; }

  // ── Per-session state ───────────────────────────────────────────────────

  CLU._editState = null;

  function _newState() {
    return {
      baseFolder: '',
      originalOrder: [],          // rel_paths sorted alphabetically at load
      pendingDeletes: new Set(),  // rel_paths to delete on save
      pendingRenames: new Map(),  // rel_path -> user-typed new filename
      dragged: false,             // any drag-reorder happened
      dirty: false,
      sortable: null,
      closeGuardAttached: false
    };
  }

  function _markDirty() {
    if (!CLU._editState) return;
    CLU._editState.dirty = true;
    var modal = document.getElementById('editCBZModal');
    if (modal) modal.classList.add('cbz-edit-dirty');
  }

  function _clearDirty() {
    if (!CLU._editState) return;
    CLU._editState.dirty = false;
    var modal = document.getElementById('editCBZModal');
    if (modal) modal.classList.remove('cbz-edit-dirty');
  }

  // ── Path / extension helpers ────────────────────────────────────────────

  function _splitExt(name) {
    var i = name.lastIndexOf('.');
    if (i <= 0) return { stem: name, ext: '' };
    return { stem: name.substring(0, i), ext: name.substring(i) };
  }

  function _basename(p) {
    var s = String(p || '');
    var i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
    return i < 0 ? s : s.substring(i + 1);
  }

  function _padNum(n, width) {
    var s = String(n);
    while (s.length < width) s = '0' + s;
    return s;
  }

  // ── generateCardHTML ────────────────────────────────────────────────────

  CLU.generateCardHTML = function (relPath, imageData) {
    var filenameOnly = _basename(relPath);
    var safeName = CLU.escapeHtml(filenameOnly);
    var safeRel = CLU.escapeHtml(relPath);
    return '<div class="col" data-rel-path="' + safeRel + '">' +
      '<div class="card h-100 shadow-sm cbz-edit-card">' +
        '<div class="cbz-edit-thumb-wrap">' +
          '<img src="' + imageData + '" class="cbz-edit-thumb" alt="' + safeName + '">' +
        '</div>' +
        '<div class="card-body d-flex flex-column p-2">' +
          '<p class="card-text small text-break mb-2 cbz-edit-filename-wrap">' +
            '<span class="editable-filename" data-rel-path="' + safeRel + '" onclick="CLU.enableFilenameEdit(this)">' +
              safeName +
            '</span>' +
            '<input type="text" class="form-control d-none filename-input form-control-sm" value="' + safeName + '" data-rel-path="' + safeRel + '">' +
          '</p>' +
          '<div class="btn-group btn-group-sm w-100 mt-auto" role="group">' +
            '<button type="button" class="btn btn-outline-primary" onclick="CLU.cropImageFreeForm(this)" title="Free Form Crop"><i class="bi bi-crop"></i></button>' +
            '<button type="button" class="btn btn-outline-primary" onclick="CLU.cropImageLeft(this)" title="Crop Left"><i class="bi bi-arrow-bar-left"></i></button>' +
            '<button type="button" class="btn btn-outline-primary" onclick="CLU.cropImageCenter(this)" title="Crop Center"><i class="bi bi-bounding-box"></i></button>' +
            '<button type="button" class="btn btn-outline-primary" onclick="CLU.cropImageRight(this)" title="Crop Right"><i class="bi bi-arrow-bar-right"></i></button>' +
            '<button type="button" class="btn btn-outline-danger" onclick="CLU.deleteCardImage(this)" title="Delete"><i class="bi bi-trash"></i></button>' +
          '</div>' +
        '</div>' +
      '</div>' +
    '</div>';
  };

  // ── sortInlineEditCards (initial-load + post-upload alphabetical sort) ──

  CLU.sortInlineEditCards = function () {
    var container = document.getElementById('editInlineContainer');
    if (!container) return;

    var cards = Array.from(container.children);
    var re = /^[a-z0-9]/i;
    var collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

    cards.sort(function (a, b) {
      var iA = a.querySelector('.filename-input');
      var iB = b.querySelector('.filename-input');
      var fA = iA ? iA.value : '';
      var fB = iB ? iB.value : '';
      var aOk = re.test(fA), bOk = re.test(fB);
      if (!aOk && bOk) return -1;
      if (aOk && !bOk) return 1;
      return collator.compare(fA, fB);
    });

    cards.forEach(function (c) { container.appendChild(c); });
  };

  // ── renumberDisplay — refresh visible filenames to NNN.ext in DOM order ─

  CLU.renumberDisplay = function () {
    var container = document.getElementById('editInlineContainer');
    if (!container) return;
    var cards = Array.from(container.children);
    var width = String(Math.max(cards.length, 1)).length;
    if (width < 3) width = 3;

    cards.forEach(function (card, idx) {
      var span = card.querySelector('.editable-filename');
      var input = card.querySelector('.filename-input');
      if (!span || !input) return;

      var relPath = card.getAttribute('data-rel-path') || span.getAttribute('data-rel-path');
      if (!relPath) return;

      // If the user typed a custom rename for this file, don't overwrite it.
      if (CLU._editState && CLU._editState.pendingRenames.has(relPath)) return;

      var ext = _splitExt(_basename(relPath)).ext;
      var newName = _padNum(idx + 1, width) + ext;
      span.textContent = newName;
      input.value = newName;
    });
  };

  // ── deleteCardImage (deferred) ──────────────────────────────────────────

  CLU.deleteCardImage = function (buttonElement) {
    var col = buttonElement.closest('.col');
    if (!col) { console.error('Unable to locate column container.'); return; }
    var relPath = col.getAttribute('data-rel-path');
    if (!relPath) {
      var span = col.querySelector('.editable-filename');
      relPath = span ? span.getAttribute('data-rel-path') : null;
    }
    if (!relPath) { console.error('No rel_path on card; cannot defer delete.'); return; }

    if (CLU._editState) {
      CLU._editState.pendingDeletes.add(relPath);
      CLU._editState.pendingRenames.delete(relPath);
      _markDirty();
    }

    col.classList.add('fade-out');
    setTimeout(function () {
      col.remove();
      if (CLU._editState && CLU._editState.dragged) {
        CLU.renumberDisplay();
      } else {
        // Even without a drag, removing a card shifts the implicit numbering
        // shown to the user. Refresh so positions look right.
        CLU.renumberDisplay();
      }
    }, 200);
  };

  // ── enableFilenameEdit ──────────────────────────────────────────────────

  CLU.enableFilenameEdit = function (element) {
    var input = element.nextElementSibling;
    if (!input) { console.error('No adjacent input found.'); return; }
    element.classList.add('d-none');
    input.classList.remove('d-none');
    input.focus();
    input.select();

    var done = false;
    function process() {
      if (done) return;
      done = true;
      CLU.performRename(input);
    }

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); process(); input.blur(); }
      if (e.key === 'Escape') { e.preventDefault(); done = true; _cancelEdit(input); input.blur(); }
    });
    input.addEventListener('blur', process, { once: true });
  };

  // ── performRename (deferred — updates state + UI, no fetch) ─────────────

  CLU.performRename = function (input) {
    var newFilename = input.value.trim();
    var relPath = input.getAttribute('data-rel-path');
    if (!relPath) { _cancelEdit(input); return; }

    var oldFilename = _basename(relPath);
    // If state has a prior rename, the displayed name might differ from oldFilename.
    if (CLU._editState && CLU._editState.pendingRenames.has(relPath)) {
      oldFilename = CLU._editState.pendingRenames.get(relPath);
    }

    if (!newFilename || newFilename === oldFilename) {
      _cancelEdit(input);
      return;
    }

    // Basic client-side guard — backend validates definitively.
    if (newFilename.indexOf('/') !== -1 || newFilename.indexOf('\\') !== -1 || newFilename === '..' || newFilename.indexOf('\0') !== -1) {
      CLU.showError('Invalid filename: ' + newFilename);
      _cancelEdit(input);
      return;
    }

    var span = input.previousElementSibling;
    if (span) span.textContent = newFilename;
    input.value = newFilename;
    input.classList.add('d-none');
    if (span) span.classList.remove('d-none');

    if (CLU._editState) {
      CLU._editState.pendingRenames.set(relPath, newFilename);
      _markDirty();
    }
  };

  function _cancelEdit(input) {
    input.classList.add('d-none');
    if (input.previousElementSibling) input.previousElementSibling.classList.remove('d-none');
    // Restore input value to the visible span text in case the user typed and then escaped.
    if (input.previousElementSibling) input.value = input.previousElementSibling.textContent.trim();
  }

  // ── setupEditModalDropZone (upload drop zone, unchanged behavior) ───────

  CLU.setupEditModalDropZone = function () {
    var modal = document.getElementById('editCBZModal');
    var body = modal ? modal.querySelector('.modal-body') : null;
    if (!body) return;
    if (body.dataset.dropzoneSetup) return;
    body.dataset.dropzoneSetup = 'true';

    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(function (evt) {
      body.addEventListener(evt, function (e) {
        // Don't intercept events that originate from a Sortable drag operation.
        // Sortable uses its own internal drag pipeline; the file drop zone only
        // wants OS-level file drops (e.dataTransfer.files).
        if (evt === 'drop' && (!e.dataTransfer || !e.dataTransfer.files || e.dataTransfer.files.length === 0)) {
          return;
        }
        e.preventDefault();
        e.stopPropagation();
      });
    });
    ['dragenter', 'dragover'].forEach(function (evt) {
      body.addEventListener(evt, function (e) {
        if (e.dataTransfer && e.dataTransfer.types && Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') !== -1) {
          body.classList.add('drag-over');
        }
      });
    });
    ['dragleave', 'drop'].forEach(function (evt) {
      body.addEventListener(evt, function () { body.classList.remove('drag-over'); });
    });
    body.addEventListener('drop', function (e) {
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        CLU.handleEditModalUpload(e.dataTransfer.files);
      }
    });
  };

  // ── handleEditModalUpload — uploads immediately, appends to end ─────────

  CLU.handleEditModalUpload = function (files) {
    var folder = document.getElementById('editInlineFolderName');
    var folderName = folder ? folder.value : '';
    if (!folderName) { CLU.showError('Cannot upload: No target folder'); return; }

    var allowed = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'];
    var valid = Array.from(files).filter(function (f) {
      return allowed.indexOf('.' + f.name.split('.').pop().toLowerCase()) !== -1;
    });
    if (valid.length === 0) { CLU.showError('No valid image files. Allowed: ' + allowed.join(', ')); return; }

    CLU.showToast('Uploading', 'Uploading ' + valid.length + ' file(s)...', 'info');

    var fd = new FormData();
    fd.append('target_dir', folderName);
    valid.forEach(function (f) { fd.append('files', f); });

    fetch('/upload-to-folder', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success && data.uploaded && data.uploaded.length > 0) {
          CLU.showSuccess('Uploaded ' + data.uploaded.length + ' file(s)');
          data.uploaded.forEach(function (file) {
            fetch('/get-image-data', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ target: file.path })
            })
              .then(function (r2) { return r2.json(); })
              .then(function (img) {
                if (!img.success) return;
                var c = document.getElementById('editInlineContainer');
                if (!c) return;
                var relPath = _relativize(file.path, folderName);
                c.insertAdjacentHTML('beforeend', CLU.generateCardHTML(relPath, img.imageData));
                if (CLU._editState && CLU._editState.originalOrder.indexOf(relPath) === -1) {
                  CLU._editState.originalOrder.push(relPath);
                }
                CLU.renumberDisplay();
              })
              .catch(function (e) { console.error('Error loading uploaded image:', e); });
          });
        } else {
          CLU.showError('Upload failed: ' + (data.error || 'Unknown error'));
        }
      })
      .catch(function (err) {
        console.error('Upload error:', err);
        CLU.showError('Upload failed: ' + err.message);
      });
  };

  function _relativize(absPath, baseFolder) {
    if (!absPath) return '';
    if (!baseFolder) return absPath;
    // Normalize trailing slash on base
    var base = baseFolder.endsWith('/') || baseFolder.endsWith('\\') ? baseFolder : (baseFolder + '/');
    if (absPath.indexOf(base) === 0) return absPath.substring(base.length);
    // Try with the alternate separator just in case
    var altBase = baseFolder.replace(/\\/g, '/');
    var altPath = absPath.replace(/\\/g, '/');
    if (altPath.indexOf(altBase + '/') === 0) return altPath.substring(altBase.length + 1);
    return _basename(absPath); // fallback
  }

  // ── setEditModalThumbSize ───────────────────────────────────────────────

  var THUMB_SIZE_KEY = 'cbzEditThumbSize';
  var THUMB_SIZE_GRID = {
    large: ['row-cols-2', 'row-cols-sm-3', 'row-cols-md-4'],
    small: ['row-cols-3', 'row-cols-sm-4', 'row-cols-md-6', 'row-cols-lg-8']
  };
  var ALL_ROW_COLS = (function () {
    var set = new Set();
    Object.keys(THUMB_SIZE_GRID).forEach(function (k) {
      THUMB_SIZE_GRID[k].forEach(function (c) { set.add(c); });
    });
    return Array.from(set);
  })();

  CLU.setEditModalThumbSize = function (size) {
    if (size !== 'large' && size !== 'small') size = 'large';

    var modal = document.getElementById('editCBZModal');
    if (modal) modal.classList.toggle('cbz-edit-size-small', size === 'small');

    var container = document.getElementById('editInlineContainer');
    if (container) {
      ALL_ROW_COLS.forEach(function (c) { container.classList.remove(c); });
      THUMB_SIZE_GRID[size].forEach(function (c) { container.classList.add(c); });
    }

    document.querySelectorAll('.cbz-edit-size-btn').forEach(function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-thumb-size') === size);
    });

    try { localStorage.setItem(THUMB_SIZE_KEY, size); } catch (e) { /* private mode etc. */ }
  };

  // ── initEditModalReorder — call after cards render on modal open ────────

  CLU.initEditModalReorder = function () {
    var container = document.getElementById('editInlineContainer');
    if (!container) return;
    if (typeof Sortable === 'undefined') {
      console.error('Sortable.js not loaded — drag-reorder disabled');
      return;
    }

    // Fresh state every modal open.
    if (CLU._editState && CLU._editState.sortable) {
      try { CLU._editState.sortable.destroy(); } catch (e) {}
    }
    CLU._editState = _newState();

    var folderEl = document.getElementById('editInlineFolderName');
    CLU._editState.baseFolder = folderEl ? folderEl.value : '';

    // Snapshot initial order in DOM order (post-sortInlineEditCards alphabetical).
    Array.from(container.children).forEach(function (card) {
      var rel = card.getAttribute('data-rel-path');
      if (!rel) {
        var span = card.querySelector('.editable-filename');
        rel = span ? span.getAttribute('data-rel-path') : null;
      }
      if (rel) {
        // Promote data-rel-path onto the .col itself so deletes/drags can read it.
        card.setAttribute('data-rel-path', rel);
        CLU._editState.originalOrder.push(rel);
      }
    });

    // Reset hidden form fields.
    var pd = document.getElementById('editInlinePendingDeletes');
    var po = document.getElementById('editInlinePendingOrder');
    if (pd) pd.value = '[]';
    if (po) po.value = '[]';

    // Attach Sortable.
    CLU._editState.sortable = new Sortable(container, {
      animation: 150,
      handle: '.cbz-edit-thumb-wrap',
      ghostClass: 'cbz-edit-ghost',
      chosenClass: 'cbz-edit-chosen',
      onEnd: function (evt) {
        if (evt.oldIndex === evt.newIndex) return;
        CLU._editState.dragged = true;
        _markDirty();
        CLU.renumberDisplay();
      }
    });

    _attachCloseGuard();
    _clearDirty();

    // Restore last-used thumbnail size preference.
    var stored = null;
    try { stored = localStorage.getItem(THUMB_SIZE_KEY); } catch (e) { /* ignore */ }
    CLU.setEditModalThumbSize(stored === 'small' ? 'small' : 'large');
  };

  // ── Close guard — warn before discarding pending changes ────────────────

  function _attachCloseGuard() {
    var modalEl = document.getElementById('editCBZModal');
    if (!modalEl || !CLU._editState || CLU._editState.closeGuardAttached) return;
    CLU._editState.closeGuardAttached = true;

    modalEl.addEventListener('hide.bs.modal', function (e) {
      if (!CLU._editState || !CLU._editState.dirty) return;
      var ok = window.confirm('You have unsaved changes. Discard them?');
      if (!ok) {
        e.preventDefault();
        return;
      }
      _clearDirty();
    });
  }

  // ── saveEditedCBZ — populate hidden fields and POST ─────────────────────

  CLU.saveEditedCBZ = function () {
    var form = document.getElementById('editInlineSaveForm');
    if (!form) { CLU.showError('Form not found'); return; }

    if (!CLU._editState) {
      // Safety: should be set by initEditModalReorder; if not, build a minimal state.
      CLU._editState = _newState();
    }

    var container = document.getElementById('editInlineContainer');
    var cards = container ? Array.from(container.children) : [];
    var width = String(Math.max(cards.length, 1)).length;
    if (width < 3) width = 3;

    var order = [];
    cards.forEach(function (card, idx) {
      var rel = card.getAttribute('data-rel-path');
      if (!rel) return;
      var finalName;
      if (CLU._editState.pendingRenames.has(rel)) {
        finalName = CLU._editState.pendingRenames.get(rel);
      } else if (CLU._editState.dragged) {
        var ext = _splitExt(_basename(rel)).ext;
        finalName = _padNum(idx + 1, width) + ext;
      } else {
        finalName = _basename(rel);
      }
      order.push({ rel_path: rel, final_name: finalName });
    });

    var deletes = Array.from(CLU._editState.pendingDeletes);

    var pd = document.getElementById('editInlinePendingDeletes');
    var po = document.getElementById('editInlinePendingOrder');
    if (pd) pd.value = JSON.stringify(deletes);
    if (po) po.value = JSON.stringify(order);

    CLU.showToast('Saving', 'Saving CBZ file...', 'info');
    var fd = new FormData(form);
    var origPath = document.getElementById('editInlineOriginalFilePath');
    var filePath = origPath ? origPath.value : '';

    fetch('/save', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          CLU.showSuccess('CBZ file saved successfully!');
          _clearDirty();
          var modal = bootstrap.Modal.getInstance(document.getElementById('editCBZModal'));
          if (modal) modal.hide();

          var contract = _getContract();
          if (typeof contract.onSaveComplete === 'function') {
            contract.onSaveComplete(filePath);
          }
        } else {
          CLU.showError(data.error || 'Failed to save CBZ file');
        }
      })
      .catch(function (err) {
        console.error('Save error:', err);
        CLU.showError(err.message);
      });
  };

})();
