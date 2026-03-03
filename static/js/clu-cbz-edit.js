/**
 * CLU CBZ Edit Operations  –  clu-cbz-edit.js
 *
 * Edit modal: reorder pages, upload images, rename filenames, delete images.
 * Provides: CLU.generateCardHTML, CLU.sortInlineEditCards, CLU.deleteCardImage,
 *           CLU.enableFilenameEdit, CLU.saveEditedCBZ, CLU.setupEditModalDropZone,
 *           CLU.handleEditModalUpload
 *
 * Depends on: clu-utils.js (CLU.showToast, CLU.showError, CLU.showSuccess)
 *
 * External contract:
 *   window._cluCbzEdit = { onSaveComplete(filePath) }
 *
 * DOM contracts:
 *   #editCBZModal  (from modal_cbz_edit.html)
 *   #editInlineContainer, #editInlineFolderName, #editInlineZipFilePath,
 *   #editInlineOriginalFilePath, #editInlineSaveForm
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  function _getContract() { return window._cluCbzEdit || {}; }

  // ── Path resolution (shared with clu-cbz-crop.js) ──────────────────────

  function _resolveFullPath(span) {
    var fullPath = span.dataset.fullPath || span.getAttribute('data-full-path');
    if (fullPath) return fullPath;

    var relPath = span.dataset.relPath || span.getAttribute('data-rel-path');
    if (!relPath) return null;

    if (relPath.startsWith('/')) return relPath;

    var folder = document.getElementById('editInlineFolderName');
    if (!folder || !folder.value) return null;
    return folder.value + '/' + relPath;
  }

  // ── generateCardHTML ────────────────────────────────────────────────────

  CLU.generateCardHTML = function (imagePath, imageData) {
    var filenameOnly = imagePath.split('/').pop();
    return '<div class="col">' +
      '<div class="card h-100 shadow-sm">' +
        '<div class="row g-0">' +
          '<div class="col-3">' +
            '<img src="' + imageData + '" class="img-fluid rounded-start object-fit-scale border rounded" alt="' + CLU.escapeHtml(filenameOnly) + '">' +
          '</div>' +
          '<div class="col-9">' +
            '<div class="card-body">' +
              '<p class="card-text small">' +
                '<span class="editable-filename" data-full-path="' + CLU.escapeHtml(imagePath) + '" onclick="CLU.enableFilenameEdit(this)">' +
                  CLU.escapeHtml(filenameOnly) +
                '</span>' +
                '<input type="text" class="form-control d-none filename-input form-control-sm" value="' + CLU.escapeHtml(filenameOnly) + '" data-full-path="' + CLU.escapeHtml(imagePath) + '">' +
              '</p>' +
              '<div class="d-flex justify-content-end">' +
                '<div class="btn-group" role="group">' +
                  '<button type="button" class="btn btn-outline-primary btn-sm" onclick="CLU.cropImageFreeForm(this)" title="Free Form Crop"><i class="bi bi-crop"></i> Free</button>' +
                  '<button type="button" class="btn btn-outline-primary btn-sm" onclick="CLU.cropImageLeft(this)" title="Crop Left"><i class="bi bi-arrow-bar-left"></i> Left</button>' +
                  '<button type="button" class="btn btn-outline-primary" onclick="CLU.cropImageCenter(this)" title="Crop Center">Middle</button>' +
                  '<button type="button" class="btn btn-outline-primary btn-sm" onclick="CLU.cropImageRight(this)" title="Crop Right">Right <i class="bi bi-arrow-bar-right"></i></button>' +
                  '<button type="button" class="btn btn-outline-danger btn-sm" onclick="CLU.deleteCardImage(this)"><i class="bi bi-trash"></i></button>' +
                '</div>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>' +
    '</div>';
  };

  // ── sortInlineEditCards ─────────────────────────────────────────────────

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

  // ── deleteCardImage ─────────────────────────────────────────────────────

  CLU.deleteCardImage = function (buttonElement) {
    var col = buttonElement.closest('.col');
    if (!col) { console.error('Unable to locate column container.'); return; }
    var span = col.querySelector('.editable-filename');
    if (!span) { console.error('No file reference found.'); return; }

    var fullPath = _resolveFullPath(span);
    if (!fullPath) return;

    console.log('Deleting file:', fullPath);

    fetch('/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: fullPath })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          col.classList.add('fade-out');
          setTimeout(function () { col.remove(); }, 300);
        } else {
          CLU.showError('Error deleting image: ' + data.error);
        }
      })
      .catch(function (err) {
        console.error('Error:', err);
        CLU.showError('An error occurred while deleting the image.');
      });
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
    });
    input.addEventListener('blur', process, { once: true });
  };

  // ── performRename ───────────────────────────────────────────────────────

  CLU.performRename = function (input) {
    var newFilename = input.value.trim();
    var oldPath = input.dataset.fullPath || input.getAttribute('data-full-path');
    var oldFilename, newPath;

    if (oldPath) {
      oldFilename = oldPath.substring(oldPath.lastIndexOf('/') + 1);
      if (newFilename === oldFilename) { _cancelEdit(input); return; }
      var dirPath = oldPath.substring(0, oldPath.lastIndexOf('/'));
      newPath = dirPath + '/' + newFilename;
    } else {
      var oldRelPath = input.dataset.relPath || input.getAttribute('data-rel-path');
      if (!oldRelPath) { console.error('No path found.'); return; }

      if (oldRelPath.startsWith('/')) {
        oldFilename = oldRelPath.substring(oldRelPath.lastIndexOf('/') + 1);
        if (newFilename === oldFilename) { _cancelEdit(input); return; }
        var dp = oldRelPath.substring(0, oldRelPath.lastIndexOf('/'));
        oldPath = oldRelPath;
        newPath = dp + '/' + newFilename;
      } else {
        var folder = document.getElementById('editInlineFolderName');
        var folderName = folder ? folder.value : '';
        oldFilename = oldRelPath.includes('/')
          ? oldRelPath.substring(oldRelPath.lastIndexOf('/') + 1) : oldRelPath;
        if (newFilename === oldFilename) { _cancelEdit(input); return; }
        var relDir = oldRelPath.includes('/')
          ? oldRelPath.substring(0, oldRelPath.lastIndexOf('/')) : '';
        var newRel = relDir ? relDir + '/' + newFilename : newFilename;
        oldPath = folderName + '/' + oldRelPath;
        newPath = folderName + '/' + newRel;
      }
    }

    fetch('/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old: oldPath, new: newPath })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          var span = input.previousElementSibling;
          span.textContent = newFilename;
          if (input.dataset.fullPath || input.getAttribute('data-full-path')) {
            span.setAttribute('data-full-path', newPath);
            input.setAttribute('data-full-path', newPath);
          } else {
            var nr = newPath.substring(newPath.indexOf('/') + 1);
            span.setAttribute('data-rel-path', nr);
            input.setAttribute('data-rel-path', nr);
          }
          span.classList.remove('d-none');
          input.classList.add('d-none');
          CLU.sortInlineEditCards();
        } else {
          CLU.showError('Error renaming file: ' + data.error);
          _cancelEdit(input);
        }
      })
      .catch(function (err) {
        console.error('Error:', err);
        CLU.showError('An error occurred while renaming.');
        _cancelEdit(input);
      });
  };

  function _cancelEdit(input) {
    input.classList.add('d-none');
    if (input.previousElementSibling) input.previousElementSibling.classList.remove('d-none');
  }

  // ── setupEditModalDropZone ──────────────────────────────────────────────

  CLU.setupEditModalDropZone = function () {
    var modal = document.getElementById('editCBZModal');
    var body = modal ? modal.querySelector('.modal-body') : null;
    if (!body) return;
    if (body.dataset.dropzoneSetup) return;
    body.dataset.dropzoneSetup = 'true';

    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(function (evt) {
      body.addEventListener(evt, function (e) { e.preventDefault(); e.stopPropagation(); });
    });
    ['dragenter', 'dragover'].forEach(function (evt) {
      body.addEventListener(evt, function () { body.classList.add('drag-over'); });
    });
    ['dragleave', 'drop'].forEach(function (evt) {
      body.addEventListener(evt, function () { body.classList.remove('drag-over'); });
    });
    body.addEventListener('drop', function (e) {
      if (e.dataTransfer.files.length > 0) CLU.handleEditModalUpload(e.dataTransfer.files);
    });
  };

  // ── handleEditModalUpload ───────────────────────────────────────────────

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
                if (img.success) {
                  var c = document.getElementById('editInlineContainer');
                  if (c) {
                    c.insertAdjacentHTML('beforeend', CLU.generateCardHTML(file.path, img.imageData));
                    CLU.sortInlineEditCards();
                  }
                }
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

  // ── saveEditedCBZ ───────────────────────────────────────────────────────

  CLU.saveEditedCBZ = function () {
    var form = document.getElementById('editInlineSaveForm');
    if (!form) { CLU.showError('Form not found'); return; }

    CLU.showToast('Saving', 'Saving CBZ file...', 'info');
    var fd = new FormData(form);
    var origPath = document.getElementById('editInlineOriginalFilePath');
    var filePath = origPath ? origPath.value : '';

    fetch('/save', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          CLU.showSuccess('CBZ file saved successfully!');
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
