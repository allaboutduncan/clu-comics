/**
 * CLU CBZ Crop Operations  –  clu-cbz-crop.js
 *
 * Image cropping: left/center/right/freeform with interactive selection.
 * Provides: CLU.cropImageLeft, CLU.cropImageCenter, CLU.cropImageRight,
 *           CLU.cropImageFreeForm, CLU.confirmFreeFormCrop
 *
 * Depends on: clu-utils.js  (CLU.showToast, CLU.showError, CLU.showSuccess)
 *
 * DOM contracts:
 *   #freeFormCropModal  (from modal_freeform_crop.html)
 *   #editInlineContainer, #editInlineFolderName  (edit-mode cards)
 *
 * External functions expected (from clu-cbz-edit.js or page JS):
 *   generateCardHTML(imagePath, imageData) — returns card HTML string
 *   sortInlineEditCards()                  — re-sorts the card grid
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  // ── Crop state ──────────────────────────────────────────────────────────

  var cropData = {
    imagePath: null,
    startX: 0, startY: 0, endX: 0, endY: 0,
    isDragging: false,
    imageElement: null,
    colElement: null,
    isPanning: false,
    panStartX: 0, panStartY: 0,
    selectionLeft: 0, selectionTop: 0,
    spacebarPressed: false,
    wasDrawingBeforePan: false,
    savedWidth: 0, savedHeight: 0
  };

  // ── Path resolution helper ──────────────────────────────────────────────

  function _resolveFullPath(span) {
    // Check for full path first (newly created files from JS)
    var fullPath = span.dataset.fullPath || span.getAttribute('data-full-path');
    if (fullPath) return fullPath;

    var relPath = span.dataset.relPath || span.getAttribute('data-rel-path');
    if (!relPath) {
      console.error('No path found in span:', span);
      return null;
    }

    // If relPath is actually a full path (starts with /)
    if (relPath.startsWith('/')) return relPath;

    // Construct full path from folder name
    var folderElement = document.getElementById('editInlineFolderName');
    if (!folderElement || !folderElement.value) {
      console.error('Folder name input not found or empty.');
      return null;
    }
    return folderElement.value + '/' + relPath;
  }

  // ── Left / Center / Right crop ──────────────────────────────────────────

  CLU.cropImageLeft = function (buttonElement) {
    _processCropImage(buttonElement, 'left');
  };

  CLU.cropImageCenter = function (buttonElement) {
    _processCropImage(buttonElement, 'center');
  };

  CLU.cropImageRight = function (buttonElement) {
    _processCropImage(buttonElement, 'right');
  };

  function _processCropImage(buttonElement, cropType) {
    var colElement = buttonElement.closest('.col');
    if (!colElement) { console.error('Unable to locate column container.'); return; }

    var span = colElement.querySelector('.editable-filename');
    if (!span) { console.error('No file reference found in column:', colElement); return; }

    var fullPath = _resolveFullPath(span);
    if (!fullPath) return;

    fetch('/crop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: fullPath, cropType: cropType })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          var container = document.getElementById('editInlineContainer');
          colElement.remove();

          if (data.html) {
            container.insertAdjacentHTML('beforeend', data.html);
          } else {
            var html = CLU.generateCardHTML
              ? CLU.generateCardHTML(data.newImagePath, data.newImageData) : '';
            if (html) container.insertAdjacentHTML('beforeend', html);
          }

          if (CLU.sortInlineEditCards) CLU.sortInlineEditCards();
        } else {
          CLU.showError('Error cropping image: ' + data.error);
        }
      })
      .catch(function (error) {
        console.error('Error:', error);
        CLU.showError('An error occurred while cropping the image.');
      });
  }

  // ── Free-form crop ──────────────────────────────────────────────────────

  CLU.cropImageFreeForm = function (buttonElement) {
    var colElement = buttonElement.closest('.col');
    if (!colElement) { console.error('Unable to locate column container.'); return; }

    var span = colElement.querySelector('.editable-filename');
    if (!span) { console.error('No file reference found in column:', colElement); return; }

    var fullPath = _resolveFullPath(span);
    if (!fullPath) return;

    cropData.imagePath = fullPath;
    cropData.colElement = colElement;

    var cardImg = colElement.querySelector('img');
    if (!cardImg) { console.error('No image found in card'); return; }

    var cropImage = document.getElementById('cropImage');
    var cropModal = new bootstrap.Modal(document.getElementById('freeFormCropModal'));

    var cropSelection = document.getElementById('cropSelection');
    cropSelection.style.display = 'none';
    document.getElementById('confirmCropBtn').disabled = true;

    fetch('/get-image-data', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: fullPath })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          cropImage.src = data.imageData;
          cropImage.onload = function () {
            _setupCropHandlers();
            cropModal.show();
          };
        } else {
          CLU.showError(data.error || 'Failed to load image');
        }
      })
      .catch(function (error) {
        console.error('Error loading image:', error);
        CLU.showError('Failed to load image for cropping');
      });
  };

  // ── Setup interactive crop handlers ─────────────────────────────────────

  function _setupCropHandlers() {
    var cropImage = document.getElementById('cropImage');
    var cropContainer = document.getElementById('cropImageContainer');
    var cropSelection = document.getElementById('cropSelection');
    var confirmBtn = document.getElementById('confirmCropBtn');

    // Replace image to remove old listeners
    var newCropImage = cropImage.cloneNode(true);
    cropImage.parentNode.replaceChild(newCropImage, cropImage);
    cropData.imageElement = newCropImage;

    function handleKeyDown(e) {
      if (e.key === ' ' || e.code === 'Space') {
        e.preventDefault();
        if (cropData.spacebarPressed) return;
        cropData.spacebarPressed = true;
        cropContainer.style.cursor = 'move';
        if (cropData.isDragging) {
          cropData.wasDrawingBeforePan = true;
          cropData.isDragging = false;
          cropData.isPanning = false;
          cropData.savedWidth = Math.abs(cropData.endX - cropData.startX);
          cropData.savedHeight = Math.abs(cropData.endY - cropData.startY);
        }
      }
    }

    function handleKeyUp(e) {
      if (e.key === ' ' || e.code === 'Space') {
        e.preventDefault();
        cropData.spacebarPressed = false;
        cropContainer.style.cursor = 'crosshair';
        if (cropData.isPanning) cropData.isPanning = false;
        if (cropData.wasDrawingBeforePan) {
          cropData.isDragging = true;
          cropData.wasDrawingBeforePan = false;
        }
      }
    }

    function startPan(e) {
      e.preventDefault();
      e.stopPropagation();
      cropData.isPanning = true;
      cropData.panStartX = e.clientX;
      cropData.panStartY = e.clientY;
      cropData.selectionLeft = parseInt(cropSelection.style.left) || 0;
      cropData.selectionTop = parseInt(cropSelection.style.top) || 0;
      document.addEventListener('mousemove', updatePan);
      document.addEventListener('mouseup', endPan);
    }

    function updatePan(e) {
      if (!cropData.isPanning) return;
      e.preventDefault();
      var dX = e.clientX - cropData.panStartX;
      var dY = e.clientY - cropData.panStartY;
      var nL = cropData.selectionLeft + dX;
      var nT = cropData.selectionTop + dY;
      var cR = cropContainer.getBoundingClientRect();
      var sW = parseInt(cropSelection.style.width) || 0;
      var sH = parseInt(cropSelection.style.height) || 0;
      var cL = Math.max(0, Math.min(nL, cR.width - sW));
      var cT = Math.max(0, Math.min(nT, cR.height - sH));
      cropSelection.style.left = cL + 'px';
      cropSelection.style.top = cT + 'px';
      cropData.startX = cL;
      cropData.startY = cT;
      cropData.endX = cL + sW;
      cropData.endY = cT + sH;
    }

    function endPan() {
      cropData.isPanning = false;
      document.removeEventListener('mousemove', updatePan);
      document.removeEventListener('mouseup', endPan);
    }

    function startCrop(e) {
      if (e.target === cropSelection && cropData.spacebarPressed) { startPan(e); return; }
      if (cropData.spacebarPressed && cropSelection.style.display !== 'none') { startPan(e); return; }
      if (e.button !== 0) return;
      e.preventDefault();

      cropData.isDragging = true;
      var iR = newCropImage.getBoundingClientRect();
      var cR = newCropImage.parentElement.getBoundingClientRect();
      var oX = iR.left - cR.left;
      var oY = iR.top - cR.top;

      var sX = Math.max(oX, Math.min(e.clientX - cR.left, oX + iR.width));
      var sY = Math.max(oY, Math.min(e.clientY - cR.top, oY + iR.height));
      cropData.startX = sX;
      cropData.startY = sY;

      cropSelection.style.left = sX + 'px';
      cropSelection.style.top = sY + 'px';
      cropSelection.style.width = '0px';
      cropSelection.style.height = '0px';
      cropSelection.style.display = 'block';
      confirmBtn.disabled = true;
    }

    function updateCrop(e) {
      // Pan mode while drawing
      if (cropData.spacebarPressed && cropSelection.style.display !== 'none') {
        if (!cropData.isPanning) {
          cropData.isPanning = true;
          cropData.panStartX = e.clientX;
          cropData.panStartY = e.clientY;
          cropData.selectionLeft = parseInt(cropSelection.style.left) || 0;
          cropData.selectionTop = parseInt(cropSelection.style.top) || 0;
        }
        e.preventDefault();
        var dX = e.clientX - cropData.panStartX;
        var dY = e.clientY - cropData.panStartY;
        var nL = cropData.selectionLeft + dX;
        var nT = cropData.selectionTop + dY;
        var iR2 = newCropImage.getBoundingClientRect();
        var cR2 = cropContainer.getBoundingClientRect();
        var oX2 = iR2.left - cR2.left;
        var oY2 = iR2.top - cR2.top;
        var sW2 = parseInt(cropSelection.style.width) || 0;
        var sH2 = parseInt(cropSelection.style.height) || 0;
        var cL2 = Math.max(oX2, Math.min(nL, oX2 + iR2.width - sW2));
        var cT2 = Math.max(oY2, Math.min(nT, oY2 + iR2.height - sH2));
        cropSelection.style.left = cL2 + 'px';
        cropSelection.style.top = cT2 + 'px';
        cropData.startX = cL2;
        cropData.startY = cT2;
        cropData.endX = cL2 + sW2;
        cropData.endY = cT2 + sH2;
        return;
      }

      if (!cropData.isDragging) return;
      e.preventDefault();

      var cRect = newCropImage.parentElement.getBoundingClientRect();
      var iRect = newCropImage.getBoundingClientRect();
      var imgOX = iRect.left - cRect.left;
      var imgOY = iRect.top - cRect.top;

      var curX = Math.max(imgOX, Math.min(e.clientX - cRect.left, imgOX + iRect.width));
      var curY = Math.max(imgOY, Math.min(e.clientY - cRect.top, imgOY + iRect.height));

      var w = curX - cropData.startX;
      var h = curY - cropData.startY;

      // Shift → constrain to 2:3 aspect ratio
      if (e.shiftKey) {
        var ar = 2 / 3;
        if (Math.abs(w / h) > ar) {
          w = h * ar;
          curX = cropData.startX + w;
          curX = w > 0
            ? Math.min(curX, imgOX + iRect.width)
            : Math.max(curX, imgOX);
          w = curX - cropData.startX;
        } else {
          h = w / ar;
          curY = cropData.startY + h;
          curY = h > 0
            ? Math.min(curY, imgOY + iRect.height)
            : Math.max(curY, imgOY);
          h = curY - cropData.startY;
        }
      }

      var fL, fT, fW, fH;
      if (w < 0) {
        fL = Math.max(imgOX, cropData.startX + w);
        fW = cropData.startX - fL;
        cropData.endX = fL;
      } else {
        fL = cropData.startX;
        fW = Math.min(w, (imgOX + iRect.width) - cropData.startX);
        cropData.endX = fL + fW;
      }
      if (h < 0) {
        fT = Math.max(imgOY, cropData.startY + h);
        fH = cropData.startY - fT;
        cropData.endY = fT;
      } else {
        fT = cropData.startY;
        fH = Math.min(h, (imgOY + iRect.height) - cropData.startY);
        cropData.endY = fT + fH;
      }

      cropSelection.style.left = fL + 'px';
      cropSelection.style.top = fT + 'px';
      cropSelection.style.width = fW + 'px';
      cropSelection.style.height = fH + 'px';
    }

    function endCrop() {
      if (!cropData.isDragging) return;
      cropData.isDragging = false;
      var w = Math.abs(cropData.endX - cropData.startX);
      var h = Math.abs(cropData.endY - cropData.startY);
      if (w > 10 && h > 10) {
        confirmBtn.disabled = false;
      } else {
        cropSelection.style.display = 'none';
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    document.addEventListener('keyup', handleKeyUp);
    cropContainer.addEventListener('mousedown', startCrop);
    document.addEventListener('mousemove', updateCrop);
    document.addEventListener('mouseup', endCrop);

    cropSelection.addEventListener('mousedown', function (e) {
      if (cropData.spacebarPressed) startPan(e);
    });

    var modal = document.getElementById('freeFormCropModal');
    modal.addEventListener('hidden.bs.modal', function cleanup() {
      document.removeEventListener('keydown', handleKeyDown);
      document.removeEventListener('keyup', handleKeyUp);
      document.removeEventListener('mousemove', updateCrop);
      document.removeEventListener('mouseup', endCrop);
      cropContainer.removeEventListener('mousedown', startCrop);
      modal.removeEventListener('hidden.bs.modal', cleanup);
    });
  }

  // ── Confirm free-form crop ──────────────────────────────────────────────

  CLU.confirmFreeFormCrop = function () {
    var cropImage = document.getElementById('cropImage');
    var cropContainer = document.getElementById('cropImageContainer');
    var iR = cropImage.getBoundingClientRect();
    var cR = cropContainer.getBoundingClientRect();

    var oX = iR.left - cR.left;
    var oY = iR.top - cR.top;

    var scaleX = cropImage.naturalWidth / cropImage.width;
    var scaleY = cropImage.naturalHeight / cropImage.height;

    var dX = Math.min(cropData.startX, cropData.endX);
    var dY = Math.min(cropData.startY, cropData.endY);
    var dW = Math.abs(cropData.endX - cropData.startX);
    var dH = Math.abs(cropData.endY - cropData.startY);

    var aX = Math.max(0, Math.min((dX - oX) * scaleX, cropImage.naturalWidth));
    var aY = Math.max(0, Math.min((dY - oY) * scaleY, cropImage.naturalHeight));
    var aW = Math.min(dW * scaleX, cropImage.naturalWidth - aX);
    var aH = Math.min(dH * scaleY, cropImage.naturalHeight - aY);

    console.log('Actual crop coordinates:', { x: aX, y: aY, width: aW, height: aH });

    fetch('/crop-freeform', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: cropData.imagePath, x: aX, y: aY, width: aW, height: aH })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          var cropModalEl = document.getElementById('freeFormCropModal');
          var cropModal = bootstrap.Modal.getInstance(cropModalEl);
          if (cropModal) cropModal.hide();

          var cardImg = cropData.colElement.querySelector('img');
          if (cardImg && data.newImageData) cardImg.src = data.newImageData;

          if (data.backupImagePath && data.backupImageData) {
            var container = document.getElementById('editInlineContainer');
            console.log('Backup image path:', data.backupImagePath);
            var html = CLU.generateCardHTML
              ? CLU.generateCardHTML(data.backupImagePath, data.backupImageData) : '';
            if (html) container.insertAdjacentHTML('beforeend', html);
            if (CLU.sortInlineEditCards) CLU.sortInlineEditCards();
          }

          CLU.showSuccess('Free form crop completed successfully!');
        } else {
          CLU.showError(data.error || 'Failed to crop image');
        }
      })
      .catch(function (error) {
        console.error('Error:', error);
        CLU.showError('An error occurred while cropping the image');
      });
  };

})();
