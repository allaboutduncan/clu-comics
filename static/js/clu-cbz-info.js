/**
 * CLU CBZ Info Viewer  –  clu-cbz-info.js
 *
 * CBZ metadata viewer modal with optional navigation and page preview.
 * Provides: CLU.showCBZInfo, CLU.navigateCBZPrev, CLU.navigateCBZNext,
 *           CLU.cbzPagePrev, CLU.cbzPageNext
 *
 * Depends on: clu-utils.js (CLU.showToast, CLU.showError, CLU.showSuccess,
 *             CLU.formatFileSize, CLU.escapeHtml)
 *
 * External contract:
 *   window._cluCbzInfo = {
 *     onClearComplete(path)      // called after successful ComicInfo.xml removal
 *   }
 *
 * DOM contracts:
 *   #cbzInfoModal  (from modal_cbz_info.html)
 *   #cbzInfoContent, #cbzNavButtons, #cbzPrevBtn, #cbzNextBtn
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

  // ── Helpers ────────────────────────────────────────────────────────────────

  function _encodePathForReader(path) {
    var clean = path.charAt(0) === '/' ? path.substring(1) : path;
    return clean.split('/').map(function (c) { return encodeURIComponent(c); }).join('/');
  }

  // ── Field groups definition ───────────────────────────────────────────────

  var _fieldGroups = [
    {
      title: 'Basic Information',
      fields: [
        { key: 'Title', label: 'Title' },
        { key: 'Series', label: 'Series' },
        { key: 'Number', label: 'Number' },
        { key: 'Count', label: 'Count' },
        { key: 'Volume', label: 'Volume' },
        { key: 'AlternateSeries', label: 'Alternate Series' },
        { key: 'AlternateNumber', label: 'Alternate Number' },
        { key: 'AlternateCount', label: 'Alternate Count' }
      ]
    },
    {
      title: 'Publication Details',
      fields: [
        { key: 'Year', label: 'Year' },
        { key: 'Month', label: 'Month' },
        { key: 'Day', label: 'Day' },
        { key: 'Publisher', label: 'Publisher' },
        { key: 'Imprint', label: 'Imprint' },
        { key: 'Format', label: 'Format' },
        { key: 'PageCount', label: 'Page Count' },
        { key: 'LanguageISO', label: 'Language' },
        { key: 'MetronId', label: 'Metron ID' }
      ]
    },
    {
      title: 'Creative Team',
      fields: [
        { key: 'Writer', label: 'Writer' },
        { key: 'Penciller', label: 'Penciller' },
        { key: 'Inker', label: 'Inker' },
        { key: 'Colorist', label: 'Colorist' },
        { key: 'Letterer', label: 'Letterer' },
        { key: 'CoverArtist', label: 'Cover Artist' },
        { key: 'Editor', label: 'Editor' }
      ]
    },
    {
      title: 'Content Details',
      fields: [
        { key: 'Genre', label: 'Genre' },
        { key: 'Characters', label: 'Characters' },
        { key: 'Teams', label: 'Teams' },
        { key: 'Locations', label: 'Locations' },
        { key: 'StoryArc', label: 'Story Arc' },
        { key: 'SeriesGroup', label: 'Series Group' },
        { key: 'MainCharacterOrTeam', label: 'Main Character/Team' },
        { key: 'AgeRating', label: 'Age Rating' }
      ]
    },
    {
      title: 'Additional Information',
      fields: [
        { key: 'Summary', label: 'Summary' },
        { key: 'Notes', label: 'Notes' },
        { key: 'Web', label: 'Web' },
        { key: 'ScanInformation', label: 'Scan Information' },
        { key: 'Review', label: 'Review' },
        { key: 'CommunityRating', label: 'Community Rating' },
        { key: 'BlackAndWhite', label: 'Black & White' },
        { key: 'Manga', label: 'Manga' }
      ],
      fullWidth: true
    }
  ];

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

  CLU.navigateCBZPrev = function () {
    if (_currentIndex > 0) {
      _currentIndex--;
      var fn = _currentFileList[_currentIndex];
      CLU.showCBZInfo(_currentDirectory + '/' + fn, fn, {
        directoryPath: _currentDirectory,
        fileList: _currentFileList
      });
    }
  };

  CLU.navigateCBZNext = function () {
    if (_currentIndex < _currentFileList.length - 1) {
      _currentIndex++;
      var fn = _currentFileList[_currentIndex];
      CLU.showCBZInfo(_currentDirectory + '/' + fn, fn, {
        directoryPath: _currentDirectory,
        fileList: _currentFileList
      });
    }
  };

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

  function _clearComicInfoXml() {
    if (!_currentFilePath) {
      CLU.showError('No CBZ file is currently selected.');
      return;
    }

    if (!confirm('Are you sure you want to delete ComicInfo.xml? This cannot be undone.')) return;

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

  function _renderFieldGroups(comicInfo) {
    var html = '';
    _fieldGroups.forEach(function (group) {
      var hasFields = group.fields.some(function (f) { return comicInfo[f.key]; });
      if (!hasFields) return;

      var colClass = group.fullWidth ? 'col-md-12' : 'col-md-9';
      html += '<div class="' + colClass + ' mb-3">' +
        '<h6 class="text-muted small">' + group.title + '</h6>' +
        '<ul class="list-unstyled small">';

      group.fields.forEach(function (field) {
        if (comicInfo[field.key] && comicInfo[field.key] !== '' && comicInfo[field.key] !== -1) {
          var value = comicInfo[field.key];
          if (field.key === 'PageCount') value = parseInt(value);
          if (field.key === 'BlackAndWhite' || field.key === 'Manga') {
            if (value === 'YesAndRightToLeft') value = 'Yes (Right to Left)';
            else if (value !== 'Yes' && value !== 'No') value = 'Unknown';
          }
          if (field.key === 'CommunityRating' && value > 0) value = value + '/5';
          html += '<li><strong>' + field.label + ':</strong> ' + value + '</li>';
        }
      });

      html += '</ul></div>';
    });
    return html;
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
        var html = '<div class="row"><div class="col-md-7">';

        if (data.comicinfo) {
          html +=
            '<div class="d-flex justify-content-between align-items-center mb-2">' +
              '<h6 class="mb-0">Comic Information</h6>' +
              '<button type="button" class="btn btn-outline-danger btn-sm" id="clearComicInfoBtn" title="Clear ComicInfo.xml">' +
                '<i class="bi bi-eraser"></i>' +
              '</button>' +
            '</div>' +
            '<div class="card"><div class="card-body"><div class="row">' +
            _renderFieldGroups(data.comicinfo) +
            '</div></div></div>';
        } else {
          html += '<p class="text-muted">No ComicInfo.xml found</p>';
        }

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

        // Attach clear button handler
        var clearBtn = document.getElementById('clearComicInfoBtn');
        if (clearBtn) clearBtn.addEventListener('click', _clearComicInfoXml);

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
      });
    }
  });

})();
