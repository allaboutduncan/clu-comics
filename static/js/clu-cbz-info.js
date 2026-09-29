/**
 * CLU CBZ Info Viewer  –  clu-cbz-info.js
 *
 * CBZ metadata viewer modal with optional navigation and page preview.
 * Provides: CLU.showCBZInfo, CLU.navigateCBZPrev, CLU.navigateCBZNext,
 *           CLU.cbzPagePrev, CLU.cbzPageNext
 *
 * Depends on: clu-utils.js (CLU.showToast, CLU.showError, CLU.showSuccess,
 *             CLU.formatFileSize, CLU.escapeHtml, CLU.splitCreditList)
 *
 * External contract:
 *   window._cluCbzInfo = {
 *     onClearComplete(path)      // called after successful ComicInfo.xml removal
 *     onMetadataSaved(path, ci)  // optional; called after hand-edited metadata is saved
 *   }
 *
 * DOM contracts:
 *   #cbzInfoModal  (from modal_cbz_info.html)
 *   #cbzInfoContent, #cbzNavButtons, #cbzPrevBtn, #cbzNextBtn
 *   #clearComicInfoConfirmModal  (same partial) – required before ComicInfo.xml
 *   can be deleted; #clearComicInfoFileName, #confirmClearComicInfoBtn
 *   #cbzDiscardEditsModal  (same partial) – asked before Prev/Next drops staged
 *   metadata edits; #cbzDiscardEditsCount, #cbzConfirmDiscardEditsBtn
 *
 * Metadata editing: every value in the Comic Information card is click-to-edit.
 * Edits are staged in _pendingEdits and written in ONE archive rebuild by
 * POST /cbz-update-comicinfo (a blank value removes the tag).
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  function _getContract() { return window._cluCbzInfo || {}; }

  // ── Module state ──────────────────────────────────────────────────────────

  var _currentFilePath = '';
  var _currentDirectory = '';
  var _currentFileList = [];
  var _currentIndex = -1;

  // Page viewer state
  var _viewerPath = null;
  var _viewerPageCount = 0;
  var _viewerCurrentPage = 0;
  var _viewerPreloadedPages = {};

  // Metadata editing state
  var _currentComicInfo = {};     // tags as last read from the archive
  var _hasComicInfo = false;      // archive has a ComicInfo.xml at all
  var _pendingEdits = {};         // key -> staged value ('' removes the tag)
  var _saving = false;
  var _afterDiscard = null;       // continuation for #cbzDiscardEditsModal
  var _SHOW_EMPTY_KEY = 'cluCbzShowEmptyFields';
  var _showEmptyFields = false;
  try { _showEmptyFields = localStorage.getItem(_SHOW_EMPTY_KEY) === '1'; } catch (e) { /* storage unavailable */ }

  // ── Helpers ────────────────────────────────────────────────────────────────

  function _encodePathForReader(path) {
    var clean = path.charAt(0) === '/' ? path.substring(1) : path;
    return clean.split('/').map(function (c) { return encodeURIComponent(c); }).join('/');
  }

  // ── Field groups definition ───────────────────────────────────────────────
  //
  // `input` picks the editor: 'number' (a text box with a numeric keypad, so a
  // non-numeric value such as Volume "v2" is never blanked by the browser),
  // 'textarea', or 'select' with `options`. Default is a plain text box.
  // Keys must stay within EDITABLE_COMICINFO_FIELDS in routes/metadata.py.

  var _YES_NO_OPTIONS = ['', 'Unknown', 'No', 'Yes'];
  var _MANGA_OPTIONS = ['', 'Unknown', 'No', 'Yes', 'YesAndRightToLeft'];
  var _AGE_RATING_OPTIONS = [
    '', 'Unknown', 'Adults Only 18+', 'Early Childhood', 'Everyone', 'Everyone 10+',
    'G', 'Kids to Adults', 'M', 'MA15+', 'Mature 17+', 'PG', 'R18+',
    'Rating Pending', 'Teen', 'X18+'
  ];

  var _fieldGroups = [
    {
      title: 'Basic Information',
      fields: [
        { key: 'Title', label: 'Title' },
        { key: 'Series', label: 'Series' },
        { key: 'Number', label: 'Number' },
        { key: 'Count', label: 'Count', input: 'number' },
        { key: 'Volume', label: 'Volume', input: 'number' },
        { key: 'AlternateSeries', label: 'Alternate Series' },
        { key: 'AlternateNumber', label: 'Alternate Number' },
        { key: 'AlternateCount', label: 'Alternate Count', input: 'number' }
      ]
    },
    {
      title: 'Publication Details',
      fields: [
        { key: 'Year', label: 'Year', input: 'number' },
        { key: 'Month', label: 'Month', input: 'number' },
        { key: 'Day', label: 'Day', input: 'number' },
        { key: 'Publisher', label: 'Publisher' },
        { key: 'Imprint', label: 'Imprint' },
        { key: 'Format', label: 'Format' },
        { key: 'PageCount', label: 'Page Count', input: 'number' },
        { key: 'LanguageISO', label: 'Language' },
        { key: 'MetronId', label: 'Metron ID' }
      ]
    },
    {
      title: 'Creative Team',
      fields: [
        { key: 'Writer', label: 'Writer', browse: 'writer' },
        { key: 'Penciller', label: 'Penciller', browse: 'penciller' },
        { key: 'Inker', label: 'Inker', list: true },
        { key: 'Colorist', label: 'Colorist', list: true },
        { key: 'Letterer', label: 'Letterer', list: true },
        { key: 'CoverArtist', label: 'Cover Artist', list: true },
        { key: 'Editor', label: 'Editor', list: true }
      ]
    },
    {
      title: 'Content Details',
      fields: [
        { key: 'Genre', label: 'Genre', list: true },
        { key: 'Characters', label: 'Characters', browse: 'characters' },
        { key: 'Teams', label: 'Teams', list: true },
        { key: 'Locations', label: 'Locations', list: true },
        { key: 'StoryArc', label: 'Story Arc' },
        { key: 'SeriesGroup', label: 'Series Group' },
        { key: 'MainCharacterOrTeam', label: 'Main Character/Team' },
        { key: 'AgeRating', label: 'Age Rating', input: 'select', options: _AGE_RATING_OPTIONS }
      ]
    },
    {
      title: 'Additional Information',
      fields: [
        { key: 'Summary', label: 'Summary', input: 'textarea' },
        { key: 'Notes', label: 'Notes', input: 'textarea' },
        { key: 'Web', label: 'Web' },
        { key: 'ScanInformation', label: 'Scan Information', input: 'textarea' },
        { key: 'Review', label: 'Review', input: 'textarea' },
        { key: 'CommunityRating', label: 'Community Rating', input: 'number' },
        { key: 'BlackAndWhite', label: 'Black & White', input: 'select', options: _YES_NO_OPTIONS },
        { key: 'Manga', label: 'Manga', input: 'select', options: _MANGA_OPTIONS }
      ],
      fullWidth: true
    }
  ];

  var _fieldByKey = {};
  _fieldGroups.forEach(function (g) {
    g.fields.forEach(function (f) { _fieldByKey[f.key] = f; });
  });

  // ── Navigation ────────────────────────────────────────────────────────────

  function _updateNavButtons() {
    var navButtons = document.getElementById('cbzNavButtons');
    var prevBtn = document.getElementById('cbzPrevBtn');
    var nextBtn = document.getElementById('cbzNextBtn');
    if (!navButtons) return;

    if (_currentFileList.length <= 1) {
      navButtons.style.display = 'none';
      return;
    }

    navButtons.style.display = 'flex';
    prevBtn.style.visibility = _currentIndex > 0 ? 'visible' : 'hidden';
    nextBtn.style.visibility = _currentIndex < _currentFileList.length - 1 ? 'visible' : 'hidden';
  }

  /** Run `proceed` now, or after the user agrees to drop staged metadata edits. */
  function _guardPendingEdits(proceed) {
    var count = Object.keys(_pendingEdits).length;
    var el = document.getElementById('cbzDiscardEditsModal');
    if (count === 0 || !el) { proceed(); return; }

    var countEl = document.getElementById('cbzDiscardEditsCount');
    if (countEl) countEl.textContent = count + ' unsaved change' + (count === 1 ? '' : 's');
    _afterDiscard = proceed;

    var modal = bootstrap.Modal.getInstance(el);
    if (!modal) modal = new bootstrap.Modal(el);
    modal.show();
  }

  function _doDiscardAndProceed() {
    var el = document.getElementById('cbzDiscardEditsModal');
    var modal = el && bootstrap.Modal.getInstance(el);
    if (modal) modal.hide();
    _pendingEdits = {};
    var proceed = _afterDiscard;
    _afterDiscard = null;
    if (typeof proceed === 'function') proceed();
  }

  CLU.navigateCBZPrev = function () {
    if (_currentIndex > 0) _guardPendingEdits(_navigatePrev);
  };

  CLU.navigateCBZNext = function () {
    if (_currentIndex < _currentFileList.length - 1) _guardPendingEdits(_navigateNext);
  };

  function _navigatePrev() {
    if (_currentIndex > 0) {
      _currentIndex--;
      var fn = _currentFileList[_currentIndex];
      CLU.showCBZInfo(_currentDirectory + '/' + fn, fn, {
        directoryPath: _currentDirectory,
        fileList: _currentFileList
      });
    }
  }

  function _navigateNext() {
    if (_currentIndex < _currentFileList.length - 1) {
      _currentIndex++;
      var fn = _currentFileList[_currentIndex];
      CLU.showCBZInfo(_currentDirectory + '/' + fn, fn, {
        directoryPath: _currentDirectory,
        fileList: _currentFileList
      });
    }
  }

  // ── Page viewer ───────────────────────────────────────────────────────────

  function _loadCbzPage(pageNum) {
    if (!_viewerPath || pageNum < 0 || pageNum >= _viewerPageCount) return;

    var container = document.getElementById('cbzPreviewContainer');
    var encoded = _encodePathForReader(_viewerPath);
    var imageUrl = '/api/read/' + encoded + '/page/' + pageNum;

    // Build wrapper if needed
    if (!container.querySelector('.cbz-preview-wrapper')) {
      container.innerHTML =
        '<div class="cbz-preview-wrapper">' +
          '<div class="cbz-spinner text-center py-2">' +
            '<div class="spinner-border spinner-border-sm text-primary" role="status"></div>' +
          '</div>' +
          '<div class="cbz-image-container" style="display: none;"></div>' +
          '<div class="cbz-image-info text-center mt-2 small text-muted"></div>' +
        '</div>';
    }

    var spinnerEl = container.querySelector('.cbz-spinner');
    var imageContainer = container.querySelector('.cbz-image-container');
    var imageInfo = container.querySelector('.cbz-image-info');

    if (spinnerEl) { spinnerEl.style.display = 'block'; }
    if (imageContainer) { imageContainer.style.display = 'none'; }

    var img = new Image();
    img.src = imageUrl;
    img.className = 'img-fluid';
    img.style.maxWidth = '100%';
    img.style.maxHeight = '500px';
    img.style.opacity = '0';
    img.style.transition = 'opacity 0.2s ease-in';
    img.alt = 'Page ' + (pageNum + 1);

    img.onload = function () {
      if (spinnerEl) spinnerEl.style.display = 'none';
      if (imageContainer) {
        imageContainer.style.display = 'block';
        imageContainer.innerHTML = '';
        imageContainer.appendChild(img);
        img.offsetHeight; // trigger reflow
        img.style.opacity = '1';
      }
      _viewerCurrentPage = pageNum;
      _updatePageButtons();
      if (imageInfo) _fetchPageInfo(pageNum, img.naturalWidth, img.naturalHeight, imageInfo);
    };

    img.onerror = function () {
      if (spinnerEl) spinnerEl.style.display = 'none';
      if (imageContainer) {
        imageContainer.style.display = 'block';
        imageContainer.innerHTML = '<div class="text-danger">Failed to load page</div>';
      }
      if (imageInfo) imageInfo.innerHTML = '';
    };
  }

  function _fetchPageInfo(pageNum, width, height, el) {
    var encoded = _encodePathForReader(_viewerPath);
    fetch('/api/read/' + encoded + '/page/' + pageNum + '/info')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          var fn = data.file_name || ('Page ' + (pageNum + 1));
          var fs = data.file_size ? CLU.formatFileSize(data.file_size) : '';
          el.innerHTML = '<div><strong>' + fn + '</strong></div>' +
            '<div>' + width + ' \u00d7 ' + height + (fs ? ' \u2022 ' + fs : '') + '</div>';
        } else {
          el.innerHTML = '<div>' + width + ' \u00d7 ' + height + '</div>';
        }
      })
      .catch(function () {
        el.innerHTML = '<div>' + width + ' \u00d7 ' + height + '</div>';
      });
  }

  function _preloadPages(currentPage) {
    [currentPage + 1, currentPage + 2, currentPage - 1].forEach(function (pn) {
      if (pn >= 0 && pn < _viewerPageCount && !_viewerPreloadedPages[pn]) {
        var img = new Image();
        img.src = '/api/read/' + _encodePathForReader(_viewerPath) + '/page/' + pn;
        _viewerPreloadedPages[pn] = true;
      }
    });
  }

  function _updatePageButtons() {
    var prevBtn = document.querySelector('.cbz-page-prev');
    var nextBtn = document.querySelector('.cbz-page-next');
    if (prevBtn) prevBtn.disabled = _viewerCurrentPage <= 0;
    if (nextBtn) nextBtn.disabled = _viewerCurrentPage >= _viewerPageCount - 1;
  }

  function _initPageViewer(filePath) {
    var encoded = _encodePathForReader(filePath);
    fetch('/api/read/' + encoded + '/info')
      .then(function (r) { return r.json(); })
      .then(function (info) {
        _viewerPath = filePath;
        _viewerPageCount = info.page_count || 0;
        _viewerCurrentPage = 0;
        _viewerPreloadedPages = {};

        var pageNav = document.getElementById('cbzPageNav');
        if (_viewerPageCount > 1 && pageNav) {
          pageNav.style.display = 'flex';
          _loadCbzPage(0);
          _preloadPages(0);
        } else if (pageNav) {
          pageNav.style.display = 'none';
        }
      })
      .catch(function (err) {
        console.error('Error initializing page viewer:', err);
      });
  }

  function _resetPageViewer() {
    _viewerPath = null;
    _viewerPageCount = 0;
    _viewerCurrentPage = 0;
    _viewerPreloadedPages = {};
    var pageNav = document.getElementById('cbzPageNav');
    if (pageNav) pageNav.style.display = 'none';
  }

  function _handleKeydown(e) {
    var modal = document.getElementById('cbzInfoModal');
    if (!modal || !modal.classList.contains('show')) return;
    // Don't flip pages behind the clear-ComicInfo confirmation stacked on top
    var confirmModal = document.getElementById('clearComicInfoConfirmModal');
    if (confirmModal && confirmModal.classList.contains('show')) return;
    var discardModal = document.getElementById('cbzDiscardEditsModal');
    if (discardModal && discardModal.classList.contains('show')) return;
    // Arrows and Space belong to a metadata editor while one has focus --
    // otherwise typing a space into Title flips the preview page.
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
              t.tagName === 'SELECT' || t.isContentEditable)) return;
    if (e.key === 'ArrowLeft') { CLU.cbzPagePrev(); }
    else if (e.key === 'ArrowRight' || e.code === 'Space') { e.preventDefault(); CLU.cbzPageNext(); }
  }

  CLU.cbzPagePrev = function () {
    if (_viewerCurrentPage > 0) {
      _loadCbzPage(_viewerCurrentPage - 1);
      _preloadPages(_viewerCurrentPage - 1);
    }
  };

  CLU.cbzPageNext = function () {
    if (_viewerCurrentPage < _viewerPageCount - 1) {
      _loadCbzPage(_viewerCurrentPage + 1);
      _preloadPages(_viewerCurrentPage + 1);
    }
  };

  // ── Clear ComicInfo.xml ───────────────────────────────────────────────────

  /** Open the confirmation modal; the delete itself runs from its confirm button. */
  function _promptClearComicInfoXml() {
    if (!_currentFilePath) {
      CLU.showError('No CBZ file is currently selected.');
      return;
    }

    var el = document.getElementById('clearComicInfoConfirmModal');
    if (!el) {
      // The partial that owns the confirmation is missing — refuse rather than
      // delete metadata unconfirmed.
      console.error('#clearComicInfoConfirmModal not found; include partials/modal_cbz_info.html');
      CLU.showError('Unable to confirm this deletion.');
      return;
    }

    var nameEl = document.getElementById('clearComicInfoFileName');
    if (nameEl) nameEl.textContent = _currentFilePath.split('/').pop();

    var modal = bootstrap.Modal.getInstance(el);
    if (!modal) modal = new bootstrap.Modal(el);
    modal.show();
  }

  function _doClearComicInfoXml() {
    if (!_currentFilePath) return;

    var el = document.getElementById('clearComicInfoConfirmModal');
    var modal = el && bootstrap.Modal.getInstance(el);
    if (modal) modal.hide();

    fetch('/cbz-clear-comicinfo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: _currentFilePath })
    })
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (result.success) {
          CLU.showSuccess('ComicInfo.xml has been successfully deleted.');
          var fn = _currentFilePath.split('/').pop();
          var opts = {};
          if (_currentDirectory && _currentFileList.length > 0) {
            opts.directoryPath = _currentDirectory;
            opts.fileList = _currentFileList;
          }
          CLU.showCBZInfo(_currentFilePath, fn, opts);

          var contract = _getContract();
          if (typeof contract.onClearComplete === 'function') {
            contract.onClearComplete(_currentFilePath);
          }
        } else {
          CLU.showError(result.error || 'Failed to delete ComicInfo.xml');
        }
      })
      .catch(function (err) {
        console.error('Error clearing ComicInfo.xml:', err);
        CLU.showError('An error occurred while trying to delete ComicInfo.xml.');
      });
  }

  // ── Render helpers ────────────────────────────────────────────────────────

  function _isBlank(value) {
    return value === undefined || value === null || String(value).trim() === '';
  }

  /** The raw tag text as the archive has it ('' when absent). */
  function _originalValue(key) {
    var v = _currentComicInfo[key];
    return _isBlank(v) ? '' : String(v);
  }

  /** The value the user currently sees: a staged edit wins over the archive. */
  function _effectiveValue(key) {
    return Object.prototype.hasOwnProperty.call(_pendingEdits, key)
      ? _pendingEdits[key] : _originalValue(key);
  }

  function _escapeAttr(text) {
    return CLU.escapeHtml(String(text)).replace(/"/g, '&quot;');
  }

  /** Display HTML for one value (never the editor's seed -- that stays raw). */
  function _formatValue(field, value) {
    if (field.key === 'PageCount') {
      var n = parseInt(value, 10);
      return CLU.escapeHtml(isNaN(n) ? value : String(n));
    }
    if (field.key === 'BlackAndWhite' || field.key === 'Manga') {
      if (value === 'YesAndRightToLeft') return 'Yes (Right to Left)';
      if (value === 'Yes' || value === 'No') return value;
      return 'Unknown';
    }
    if (field.key === 'CommunityRating' && parseFloat(value) > 0) {
      return CLU.escapeHtml(value) + '/5';
    }
    // Comma-separated name lists: drop the tagger's "[3258]" provider IDs so a
    // 20-character cast reads as names rather than noise, and link the ones
    // /browse/<category>/<name> can resolve.
    if (field.browse || field.list) {
      var names = CLU.splitCreditList(value);
      var html = names.map(function (n) {
        if (!field.browse) return CLU.escapeHtml(n);
        return '<a href="/browse/' + field.browse + '/' +
          encodeURIComponent(n) + '">' + CLU.escapeHtml(n) + '</a>';
      }).join(', ');
      // Nothing left once IDs were stripped: show the raw text so the field
      // can still be seen and edited.
      return html || CLU.escapeHtml(value);
    }
    var escaped = CLU.escapeHtml(value);
    if (field.input === 'textarea') {
      escaped = '<span style="white-space: pre-line;">' + escaped + '</span>';
    }
    return escaped;
  }

  function _renderRow(field) {
    var value = _effectiveValue(field.key);
    var dirty = Object.prototype.hasOwnProperty.call(_pendingEdits, field.key);
    var display = _isBlank(value)
      ? '<span class="cbz-ci-empty">—</span>'
      : _formatValue(field, value);
    return '<li class="cbz-ci-row' + (dirty ? ' cbz-ci-dirty' : '') + '" data-key="' + field.key + '">' +
      '<strong>' + CLU.escapeHtml(field.label) + ':</strong> ' +
      '<span class="cbz-ci-value" data-key="' + field.key + '" tabindex="0" role="button" ' +
        'title="Click to edit" aria-label="Edit ' + _escapeAttr(field.label) + '">' +
        display + '<i class="bi bi-pencil cbz-ci-pencil" aria-hidden="true"></i>' +
      '</span>' +
      '</li>';
  }

  function _renderFieldGroups(showEmpty) {
    var html = '';
    _fieldGroups.forEach(function (group) {
      var visible = group.fields.filter(function (f) {
        return showEmpty || !_isBlank(_effectiveValue(f.key)) ||
          Object.prototype.hasOwnProperty.call(_pendingEdits, f.key);
      });
      if (visible.length === 0) return;

      var colClass = group.fullWidth ? 'col-md-12' : 'col-md-9';
      html += '<div class="' + colClass + ' mb-3">' +
        '<h6 class="text-muted small">' + group.title + '</h6>' +
        '<ul class="list-unstyled small">';
      visible.forEach(function (field) { html += _renderRow(field); });
      html += '</ul></div>';
    });
    return html;
  }

  function _pendingCount() { return Object.keys(_pendingEdits).length; }

  function _saveBarHtml() {
    var n = _pendingCount();
    return '<div id="cbzCiSaveBar" class="btn-group btn-group-sm"' + (n ? '' : ' style="display: none;"') + '>' +
      '<button type="button" class="btn btn-outline-secondary" data-cbz-action="discard"' +
        (_saving ? ' disabled' : '') + '>Discard</button>' +
      '<button type="button" class="btn btn-primary" data-cbz-action="save"' +
        (_saving ? ' disabled' : '') + '>' +
        (_saving
          ? '<span class="spinner-border spinner-border-sm me-1" role="status"></span>Saving…'
          : '<i class="bi bi-save me-1"></i>Save changes (' + n + ')') +
      '</button>' +
    '</div>';
  }

  /** Inner HTML of #cbzComicInfoSection: header, notice, and the field card. */
  function _renderComicInfoSection() {
    var showEmpty = !_hasComicInfo || _showEmptyFields;
    var html =
      '<div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-2">' +
        '<h6 class="mb-0">Comic Information</h6>' +
        '<div class="d-flex align-items-center flex-wrap gap-2">' +
          _saveBarHtml();
    if (_hasComicInfo) {
      html +=
          '<div class="form-check form-switch mb-0 small" title="Show every field, including empty ones">' +
            '<input class="form-check-input" type="checkbox" id="cbzShowEmptyFields"' +
              (_showEmptyFields ? ' checked' : '') + '>' +
            '<label class="form-check-label" for="cbzShowEmptyFields">Show empty fields</label>' +
          '</div>' +
          '<button type="button" class="btn btn-outline-danger btn-sm" id="clearComicInfoBtn" ' +
            'data-cbz-action="clear" title="Clear ComicInfo.xml">' +
            '<i class="bi bi-eraser"></i>' +
          '</button>';
    }
    html += '</div></div>';

    if (!_hasComicInfo) {
      html += '<div class="alert alert-info small py-2 mb-2">' +
        '<i class="bi bi-info-circle me-1"></i>No ComicInfo.xml in this file — ' +
        'click any field to add metadata.' +
        '</div>';
    }

    html += '<div class="card"><div class="card-body"><div class="row">' +
      _renderFieldGroups(showEmpty) +
      '</div></div></div>';
    return html;
  }

  function _refreshComicInfoSection() {
    var section = document.getElementById('cbzComicInfoSection');
    if (section) section.innerHTML = _renderComicInfoSection();
  }

  /**
   * Update the save bar in place. It must not be replaced: an editor's blur
   * refreshes it on mousedown of Save, and swapping the button out between
   * mousedown and mouseup would swallow that click.
   */
  function _refreshSaveBar() {
    var bar = document.getElementById('cbzCiSaveBar');
    if (!bar) return;
    var tmp = document.createElement('div');
    tmp.innerHTML = _saveBarHtml();
    var fresh = tmp.firstChild;
    bar.style.display = fresh.style.display;
    var oldBtns = bar.querySelectorAll('button');
    var newBtns = fresh.querySelectorAll('button');
    for (var i = 0; i < oldBtns.length && i < newBtns.length; i++) {
      oldBtns[i].disabled = newBtns[i].disabled;
      if (oldBtns[i].innerHTML !== newBtns[i].innerHTML) oldBtns[i].innerHTML = newBtns[i].innerHTML;
    }
  }

  // ── Click-to-edit ─────────────────────────────────────────────────────────

  function _buildEditor(field, value) {
    var el;
    if (field.input === 'select') {
      el = document.createElement('select');
      el.className = 'form-select form-select-sm cbz-ci-editor';
      var opts = field.options.slice();
      // Keep a non-standard value the file already has selectable.
      if (value && opts.indexOf(value) === -1) opts.push(value);
      opts.forEach(function (o) {
        var opt = document.createElement('option');
        opt.value = o;
        opt.textContent = o === '' ? '(none)' : (o === 'YesAndRightToLeft' ? 'Yes (Right to Left)' : o);
        el.appendChild(opt);
      });
    } else if (field.input === 'textarea') {
      el = document.createElement('textarea');
      el.className = 'form-control form-control-sm cbz-ci-editor';
      el.rows = 4;
    } else {
      el = document.createElement('input');
      el.type = 'text';
      el.className = 'form-control form-control-sm cbz-ci-editor';
      if (field.input === 'number') el.inputMode = 'decimal';
    }
    el.value = value;
    el.setAttribute('aria-label', field.label);
    return el;
  }

  function _startEdit(span) {
    if (_saving) return;
    var key = span.getAttribute('data-key');
    var field = _fieldByKey[key];
    var row = span.closest('.cbz-ci-row');
    if (!field || !row || row.querySelector('.cbz-ci-editor')) return;

    var editor = _buildEditor(field, _effectiveValue(key));
    var done = false;
    // Typing stages live, so Escape has to put the staged state back.
    var hadPending = Object.prototype.hasOwnProperty.call(_pendingEdits, key);
    var prevPending = _pendingEdits[key];

    function finish(commit) {
      if (done) return;
      done = true;
      if (commit) {
        _stageEdit(key, editor.value);
      } else if (hadPending) {
        _pendingEdits[key] = prevPending;
      } else {
        delete _pendingEdits[key];
      }
      var fresh = document.createElement('div');
      fresh.innerHTML = _renderRow(field);
      var newRow = fresh.firstChild;
      if (row.parentNode) row.parentNode.replaceChild(newRow, row);
      _refreshSaveBar();
      return newRow;
    }

    editor.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        // Keep Bootstrap from closing the whole modal.
        e.preventDefault();
        e.stopPropagation();
        var r = finish(false);
        var s = r && r.querySelector('.cbz-ci-value');
        if (s) s.focus();
      } else if (e.key === 'Enter' && (field.input !== 'textarea' || e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        var r2 = finish(true);
        var s2 = r2 && r2.querySelector('.cbz-ci-value');
        if (s2) s2.focus();
      }
    });
    // Stage as the user types so the Save bar is already there to click.
    editor.addEventListener('input', function () {
      _stageEdit(key, editor.value);
      _refreshSaveBar();
    });
    editor.addEventListener('blur', function () { finish(true); });
    if (field.input === 'select') {
      editor.addEventListener('change', function () { finish(true); });
    }

    span.replaceWith(editor);
    editor.focus();
    if (editor.select && field.input !== 'select') editor.select();
  }

  function _stageEdit(key, value) {
    var v = String(value == null ? '' : value).trim();
    if (v === _originalValue(key)) delete _pendingEdits[key];
    else _pendingEdits[key] = v;
  }

  function _saveEdits() {
    if (_saving || _pendingCount() === 0 || !_currentFilePath) return;
    var path = _currentFilePath;
    var updates = {};
    Object.keys(_pendingEdits).forEach(function (k) { updates[k] = _pendingEdits[k]; });

    _saving = true;
    _refreshSaveBar();

    fetch('/cbz-update-comicinfo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path, updates: updates })
    })
      .then(function (r) {
        return r.json().catch(function () { return { success: false, error: 'HTTP ' + r.status }; });
      })
      .then(function (result) {
        _saving = false;
        if (path !== _currentFilePath) return;   // navigated away mid-save
        if (result.success) {
          _pendingEdits = {};
          _currentComicInfo = result.comicinfo || {};
          _hasComicInfo = Object.keys(_currentComicInfo).length > 0;
          _refreshComicInfoSection();
          CLU.showSuccess('Metadata saved');
          var contract = _getContract();
          if (typeof contract.onMetadataSaved === 'function') {
            contract.onMetadataSaved(path, _currentComicInfo);
          }
        } else {
          _refreshSaveBar();
          CLU.showError(result.error || 'Failed to save metadata');
        }
      })
      .catch(function (err) {
        _saving = false;
        _refreshSaveBar();
        console.error('Error saving ComicInfo.xml:', err);
        CLU.showError('An error occurred while saving metadata.');
      });
  }

  /** Delegated handlers for #cbzInfoContent (bound once; content is re-rendered). */
  function _onContentClick(e) {
    var actionEl = e.target.closest('[data-cbz-action]');
    if (actionEl) {
      var action = actionEl.getAttribute('data-cbz-action');
      if (action === 'save') _saveEdits();
      else if (action === 'discard') { _pendingEdits = {}; _refreshComicInfoSection(); }
      else if (action === 'clear') _promptClearComicInfoXml();
      return;
    }
    if (e.target.closest('a')) return;   // browse links still navigate
    var span = e.target.closest('.cbz-ci-value');
    if (span) _startEdit(span);
  }

  function _onContentKeydown(e) {
    var span = e.target.closest && e.target.closest('.cbz-ci-value');
    if (span && e.target === span && (e.key === 'Enter' || e.key === 'F2')) {
      e.preventDefault();
      _startEdit(span);
    }
  }

  function _onContentChange(e) {
    if (e.target && e.target.id === 'cbzShowEmptyFields') {
      _showEmptyFields = e.target.checked;
      try { localStorage.setItem(_SHOW_EMPTY_KEY, _showEmptyFields ? '1' : '0'); } catch (err) { /* ignore */ }
      _refreshComicInfoSection();
    }
  }

  // ── showCBZInfo ───────────────────────────────────────────────────────────

  /**
   * @param {string} filePath  – full path to CBZ file
   * @param {string} fileName  – display filename
   * @param {Object} [options] – { directoryPath, fileList } enables navigation
   */
  CLU.showCBZInfo = function (filePath, fileName, options) {
    options = options || {};
    var modalElement = document.getElementById('cbzInfoModal');
    var content = document.getElementById('cbzInfoContent');
    if (!modalElement || !content) return;

    _currentFilePath = filePath;
    _currentComicInfo = {};
    _hasComicInfo = false;
    _pendingEdits = {};
    _saving = false;

    // Navigation context
    if (options.directoryPath && options.fileList && options.fileList.length > 0) {
      _currentDirectory = options.directoryPath;
      _currentFileList = options.fileList;
      _currentIndex = options.fileList.indexOf(fileName);
    } else {
      _currentDirectory = '';
      _currentFileList = [];
      _currentIndex = -1;
    }
    _updateNavButtons();

    // Reset content (spinner)
    content.innerHTML =
      '<div class="text-center">' +
        '<div class="spinner-border" role="status"><span class="visually-hidden">Loading...</span></div>' +
        '<p class="mt-2">Loading CBZ information...</p>' +
      '</div>';

    // Get or create modal instance
    var modal = bootstrap.Modal.getInstance(modalElement);
    if (!modal) modal = new bootstrap.Modal(modalElement);
    if (!modalElement.classList.contains('show')) modal.show();

    // Fetch metadata
    fetch('/cbz-metadata?path=' + encodeURIComponent(filePath))
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (filePath !== _currentFilePath) return;   // superseded by Prev/Next
        _currentComicInfo = data.comicinfo || {};
        _hasComicInfo = !!data.comicinfo;

        var html = '<div class="row"><div class="col-md-7">' +
          '<div id="cbzComicInfoSection">' + _renderComicInfoSection() + '</div>';

        // Preview column with optional page viewer
        html += '</div><div class="col-md-5"><h6>Preview</h6>' +
          '<div id="cbzPageViewer" class="position-relative">' +
            '<div id="cbzPreviewContainer" class="text-center">' +
              '<div class="spinner-border spinner-border-sm" role="status"><span class="visually-hidden">Loading...</span></div>' +
            '</div>' +
            '<div id="cbzPageNav" class="cbz-page-nav" style="display: none;">' +
              '<button class="cbz-page-btn cbz-page-prev" onclick="CLU.cbzPagePrev()" title="Previous (\u2190)">' +
                '<i class="bi bi-chevron-left"></i>' +
              '</button>' +
              '<button class="cbz-page-btn cbz-page-next" onclick="CLU.cbzPageNext()" title="Next (\u2192 or Space)">' +
                '<i class="bi bi-chevron-right"></i>' +
              '</button>' +
            '</div>' +
          '</div>' +
          '</div></div>';

        // File information
        html += '<div class="row mt-4"><div class="col-12">' +
          '<h6>File Information</h6><ul class="list-unstyled">' +
          '<li><strong>Name:</strong> ' + CLU.escapeHtml(fileName) + '</li>' +
          '<li><strong>Path:</strong> <code style="word-break: break-all;">' + CLU.escapeHtml(filePath) + '</code></li>' +
          '<li><strong>Size:</strong> ' + CLU.formatFileSize(data.file_size) + '</li>' +
          '<li><strong>Total Files:</strong> ' + data.total_files + '</li>' +
          '<li><strong>Image Files:</strong> ' + data.image_files + '</li>' +
          '</ul>';

        // First files list
        html += '<h6 class="mt-4">First Files</h6><ul class="list-unstyled small">';
        if (data.file_list && data.file_list.length > 0) {
          data.file_list.forEach(function (f) {
            html += '<li><code>' + CLU.escapeHtml(f) + '</code></li>';
          });
        }
        html += '</ul></div></div>';

        content.innerHTML = html;
        // Edit, save, discard and clear are delegated from #cbzInfoContent
        // (bound once in DOMContentLoaded).

        // Load preview
        fetch('/cbz-preview?path=' + encodeURIComponent(filePath) + '&size=large')
          .then(function (r) { return r.json(); })
          .then(function (pData) {
            var pc = document.getElementById('cbzPreviewContainer');
            if (!pc) return;
            if (pData.success) {
              pc.innerHTML =
                '<div class="cbz-preview-wrapper">' +
                  '<div class="cbz-spinner text-center py-2">' +
                    '<div class="spinner-border spinner-border-sm text-primary" role="status"></div>' +
                  '</div>' +
                  '<div class="cbz-image-container" style="display: none;"></div>' +
                  '<div class="cbz-image-info text-center mt-2 small text-muted"></div>' +
                '</div>';

              var spinner = pc.querySelector('.cbz-spinner');
              var imgCont = pc.querySelector('.cbz-image-container');
              var imgInfo = pc.querySelector('.cbz-image-info');

              var img = new Image();
              img.src = pData.preview;
              img.className = 'img-fluid';
              img.style.maxWidth = '100%';
              img.style.maxHeight = '500px';
              img.style.opacity = '0';
              img.style.transition = 'opacity 0.2s ease-in';
              img.alt = 'CBZ Preview';

              img.onload = function () {
                if (spinner) spinner.style.display = 'none';
                if (imgCont) {
                  imgCont.style.display = 'block';
                  imgCont.appendChild(img);
                  img.offsetHeight;
                  img.style.opacity = '1';
                }
                if (imgInfo) {
                  var fn = pData.file_name || 'Preview';
                  var w = pData.original_size ? pData.original_size.width : img.naturalWidth;
                  var h = pData.original_size ? pData.original_size.height : img.naturalHeight;
                  var extra = pData.total_images ? ' \u2022 ' + pData.total_images + ' images' : '';
                  imgInfo.innerHTML = '<div><strong>' + fn + '</strong></div>' +
                    '<div>' + w + ' \u00d7 ' + h + extra + '</div>';
                }
              };

              img.onerror = function () {
                if (spinner) spinner.style.display = 'none';
                if (imgCont) {
                  imgCont.style.display = 'block';
                  imgCont.innerHTML = '<p class="text-muted">Preview not available</p>';
                }
              };

              // Initialize page viewer
              _initPageViewer(filePath);
            } else {
              pc.innerHTML = '<p class="text-muted">Preview not available</p>';
            }
          })
          .catch(function () {
            var pc = document.getElementById('cbzPreviewContainer');
            if (pc) pc.innerHTML = '<p class="text-danger">Error loading preview</p>';
          });
      })
      .catch(function (err) {
        content.innerHTML = '<div class="alert alert-danger">Error loading CBZ information: ' + err.message + '</div>';
      });
  };

  // ── DOM wiring (DOMContentLoaded) ─────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    var prevBtn = document.getElementById('cbzPrevBtn');
    var nextBtn = document.getElementById('cbzNextBtn');

    if (prevBtn) prevBtn.addEventListener('click', CLU.navigateCBZPrev);
    if (nextBtn) nextBtn.addEventListener('click', CLU.navigateCBZNext);

    var cbzInfoModal = document.getElementById('cbzInfoModal');
    if (cbzInfoModal) {
      cbzInfoModal.addEventListener('shown.bs.modal', function () {
        document.addEventListener('keydown', _handleKeydown);
      });
      cbzInfoModal.addEventListener('hidden.bs.modal', function () {
        document.removeEventListener('keydown', _handleKeydown);
        _resetPageViewer();
        // Closing the modal abandons staged metadata edits.
        _pendingEdits = {};
      });
    }

    var content = document.getElementById('cbzInfoContent');
    if (content) {
      content.addEventListener('click', _onContentClick);
      content.addEventListener('keydown', _onContentKeydown);
      content.addEventListener('change', _onContentChange);
    }

    var discardBtn = document.getElementById('cbzConfirmDiscardEditsBtn');
    if (discardBtn) discardBtn.addEventListener('click', _doDiscardAndProceed);
    var discardModal = document.getElementById('cbzDiscardEditsModal');
    if (discardModal) {
      discardModal.addEventListener('hidden.bs.modal', function () { _afterDiscard = null; });
    }

    // Stacking on top of #cbzInfoModal (focus, body.modal-open) is handled
    // generically in clu-utils.js.
    var confirmBtn = document.getElementById('confirmClearComicInfoBtn');
    if (confirmBtn) confirmBtn.addEventListener('click', _doClearComicInfoXml);
  });

})();
