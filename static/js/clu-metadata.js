/**
 * CLU Metadata Operations  –  clu-metadata.js
 *
 * Shared metadata fetch for single files and directory batches.
 * Provides: CLU.searchMetadata, CLU.searchMetadataWithSelection,
 *           CLU.fetchDirectoryMetadata, CLU.fetchDirectoryMetadataWithVolume
 *
 * Depends on: clu-utils.js  (CLU.showToast, CLU.escapeHtml, etc.)
 *
 * External contract (set by page before calling):
 *   window._cluMetadata = {
 *     getLibraryId: function() { return null; },      // current library ID
 *     onMetadataFound: function(filePath, data) {},    // single-file success
 *     onMetadataError: function(filePath, error) {},   // single-file error (optional)
 *     onBatchComplete: function(dirPath, result) {}    // directory batch complete
 *   }
 *
 * DOM contracts:
 *   #comicVineVolumeModal, #gcdSeriesModal  (from partials/modal_metadata_select.html)
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  // ── Contract helpers ──────────────────────────────────────────────────

  function _getContract() {
    return window._cluMetadata || {};
  }

  function _getLibraryId() {
    var c = _getContract();
    return typeof c.getLibraryId === 'function' ? c.getLibraryId() : null;
  }

  // ── Skip-to-next-provider helpers ─────────────────────────────────────

  /**
   * Providers that remain to be tried after the current one.
   * providerOrder is the (already skip-filtered) order returned by the backend.
   */
  function _remainingProviders(providerOrder, currentProvider, skipProviders) {
    if (!Array.isArray(providerOrder)) return [];
    var skip = (skipProviders || []).slice();
    if (currentProvider) skip.push(currentProvider);
    var idx = providerOrder.indexOf(currentProvider);
    var rest = idx >= 0 ? providerOrder.slice(idx + 1) : providerOrder.slice();
    return rest.filter(function (p) { return skip.indexOf(p) === -1; });
  }

  /**
   * Show/hide and wire a modal's "Skip to next provider" button.
   * Hides it when no further providers remain. Clicking it closes the
   * containing modal and asks the backend for the next provider.
   */
  function _wireSkipButton(btnId, data, filePath, fileName, skipProviders, onSkip) {
    var btn = document.getElementById(btnId);
    if (!btn) return;

    var remaining = _remainingProviders(data.provider_order, data.provider, skipProviders);
    if (!remaining.length) {
      btn.style.display = 'none';
      return;
    }
    btn.style.display = '';

    // Replace-clone to clear any listener from a previous open.
    var newBtn = btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn, btn);
    newBtn.addEventListener('click', function () {
      var modalEl = newBtn.closest('.modal');
      if (modalEl) {
        var m = bootstrap.Modal.getInstance(modalEl);
        if (m) m.hide();
      }
      if (typeof onSkip === 'function') {
        onSkip();
      } else {
        CLU.skipToNextProvider(filePath, fileName, data.provider, skipProviders);
      }
    });
  }

  // Exposed so page-level modals (files.js: GCD MySQL, Manga) can wire their
  // own "Skip to next provider" buttons with the same logic.
  CLU.wireSkipButton = _wireSkipButton;

  // ── Progress toast builder ────────────────────────────────────────────

  function _buildProgressToast() {
    var toast = document.createElement('div');
    toast.className = 'toast show position-fixed';
    toast.style.cssText = 'z-index: 1200; top: 60px; right: 1rem;';
    toast.innerHTML =
      '<div class="toast-header bg-primary text-white">' +
        '<strong class="me-auto">Fetching Metadata</strong>' +
        '<small class="batch-progress-count">0/0</small>' +
      '</div>' +
      '<div class="toast-body">' +
        '<div class="d-flex align-items-center">' +
          '<div class="spinner-border spinner-border-sm me-2" role="status">' +
            '<span class="visually-hidden">Loading...</span>' +
          '</div>' +
          '<span class="batch-progress-file text-truncate" style="max-width: 250px;">Starting...</span>' +
        '</div>' +
      '</div>';
    document.body.appendChild(toast);
    return toast;
  }

  function _removeToast(toast) {
    if (toast && toast.parentNode) {
      toast.parentNode.removeChild(toast);
    }
  }

  // ── Batch result summary builder ──────────────────────────────────────

  function _buildSummary(result) {
    var parts = [];
    if (result.cvinfo_created) parts.push('cvinfo created');
    if (result.metron_id_added) parts.push('Metron ID added');
    if (result.cv_id_missing_warning) parts.push('ComicVine ID not available');
    if (result.processed > 0) parts.push(result.processed + ' file' + (result.processed !== 1 ? 's' : '') + ' updated');
    if (result.renamed > 0) parts.push(result.renamed + ' renamed');
    if (result.skipped > 0) parts.push(result.skipped + ' skipped');
    if (result.errors > 0) parts.push(result.errors + ' error' + (result.errors !== 1 ? 's' : ''));
    return parts.length > 0 ? parts.join(', ') : 'No changes made';
  }

  // ── SSE stream processor ──────────────────────────────────────────────

  function _processSSEStream(response, progressToast, onComplete) {
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';
    var countEl = progressToast.querySelector('.batch-progress-count');
    var fileEl = progressToast.querySelector('.batch-progress-file');

    function read() {
      return reader.read().then(function (result) {
        if (result.done) return;

        buffer += decoder.decode(result.value, { stream: true });
        var lines = buffer.split('\n');
        buffer = lines.pop();

        for (var i = 0; i < lines.length; i++) {
          var line = lines[i];
          if (line.indexOf('data: ') === 0) {
            try {
              var data = JSON.parse(line.slice(6));

              if (data.type === 'progress') {
                countEl.textContent = data.current + '/' + data.total;
                fileEl.textContent = data.file;
                fileEl.title = data.file;
              } else if (data.type === 'complete') {
                _removeToast(progressToast);
                if (typeof onComplete === 'function') {
                  onComplete(data.result);
                }
                return;
              }
            } catch (e) {
              console.error('Error parsing SSE data:', e);
            }
          }
        }

        return read();
      });
    }

    return read();
  }

  // ── Refine search modal (no-match fallback) ─────────────────────────

  function _showRefineSearchModal(data, filePath, fileName) {
    var termInput = document.getElementById('refineSearchTerm');
    var issueInput = document.getElementById('refineIssueNumber');
    var searchBtn = document.getElementById('refineSearchBtn');

    if (termInput && data.parsed_filename) {
      termInput.value = data.parsed_filename.series_name || '';
    }
    if (issueInput && data.parsed_filename) {
      issueInput.value = data.parsed_filename.issue_number || '';
    }

    // Replace button to remove old listeners
    if (searchBtn) {
      var newBtn = searchBtn.cloneNode(true);
      searchBtn.parentNode.replaceChild(newBtn, searchBtn);
      newBtn.addEventListener('click', function () {
        var refinedTerm = (document.getElementById('refineSearchTerm').value || '').trim();
        if (!refinedTerm) return;
        var modal = bootstrap.Modal.getInstance(document.getElementById('refineSearchModal'));
        if (modal) modal.hide();
        CLU.searchMetadata(filePath, fileName, refinedTerm);
      });
    }

    var modal = new bootstrap.Modal(document.getElementById('refineSearchModal'));
    modal.show();
  }

  // ── ComicVine volume modal: shared state & helpers ──────────────────

  var _cvVolumes = [];
  var _cvClickHandler = null;
  var _cvSortNameAsc = true;
  var _cvSortYearAsc = true;
  var _cvFilterFn = null;  // Override for custom filtering (e.g., GCD API language filter)

  function _renderCVVolumeList(volumes) {
    var volumeList = document.getElementById('cvVolumeList');
    volumeList.innerHTML = '';

    volumes.forEach(function (volume) {
      var volumeItem = document.createElement('div');
      volumeItem.className = 'list-group-item list-group-item-action d-flex align-items-start';
      volumeItem.style.cursor = 'pointer';

      var yearDisplay = volume.start_year || 'Unknown';
      var issueCount = volume.count_of_issues || 'Unknown';
      var descriptionPreview = volume.description ?
        '<small class="text-muted d-block mt-1">' + volume.description + '</small>' : '';

      // GCD (comics.org API) series results carry no cover — show a loading
      // placeholder and fetch it lazily (see below). Other providers already
      // include image_url, and non-GCD misses fall back to "No Cover".
      var thumbnailHtml;
      if (volume.image_url) {
        thumbnailHtml = '<img src="' + volume.image_url + '" class="img-thumbnail me-3" style="width: 80px; height: 120px; object-fit: cover;" alt="' + CLU.escapeHtml(volume.name) + ' cover">';
      } else if (volume._gcdApi && volume.id) {
        thumbnailHtml = '<div class="img-thumbnail me-3 d-flex align-items-center justify-content-center text-muted" data-gcd-cover="' + CLU.escapeHtml(String(volume.id)) + '" style="width: 80px; height: 120px;"><span class="spinner-border spinner-border-sm"></span></div>';
      } else {
        thumbnailHtml = '<div class="me-3 d-flex align-items-center justify-content-center bg-secondary text-white" style="width: 80px; height: 120px; font-size: 10px;">No Cover</div>';
      }

      var langBadge = volume.language ?
        '<span class="badge bg-info rounded-pill ms-1">' + CLU.escapeHtml((volume.language || '').toUpperCase()) + '</span>' : '';

      volumeItem.innerHTML =
        thumbnailHtml +
        '<div class="flex-grow-1 d-flex justify-content-between align-items-start">' +
          '<div class="me-2">' +
            '<div class="fw-bold">' + CLU.escapeHtml(volume.name) + '</div>' +
            '<small class="text-muted">Publisher: ' + CLU.escapeHtml(volume.publisher_name || 'Unknown') + '<br>Issues: ' + issueCount + '</small>' +
            descriptionPreview +
          '</div>' +
          '<div class="text-end">' +
            '<span class="badge bg-success rounded-pill">' + yearDisplay + '</span>' +
            langBadge +
          '</div>' +
        '</div>';

      volumeItem.addEventListener('click', function () {
        _cvClickHandler(volume);
      });

      volumeList.appendChild(volumeItem);

      // Lazily fill GCD covers once the card scrolls into view; cache the URL
      // back onto the volume so re-renders (sort/filter) skip the refetch.
      if (!volume.image_url && volume._gcdApi && volume.id && window.CLU && CLU.lazyLoadGcdCover) {
        var ph = volumeItem.querySelector('[data-gcd-cover]');
        if (ph) {
          CLU.lazyLoadGcdCover(ph, volume.id, volume._gcdIssue, function (url) {
            volume.image_url = url;
            var nodes = volumeList.querySelectorAll('[data-gcd-cover="' + CSS.escape(String(volume.id)) + '"]');
            nodes.forEach(function (node) {
              var img = document.createElement('img');
              img.src = url;
              img.className = 'img-thumbnail me-3';
              img.style.cssText = 'width: 80px; height: 120px; object-fit: cover;';
              img.alt = (volume.name || '') + ' cover';
              if (node.parentNode) node.parentNode.replaceChild(img, node);
            });
          });
        }
      }
    });
  }

  function _getFilteredVolumes() {
    var filterInput = document.getElementById('cvFilterInput');
    var filterText = (filterInput && filterInput.value || '').toLowerCase();
    if (!filterText) return _cvVolumes;
    return _cvVolumes.filter(function (v) {
      return (v.name || '').toLowerCase().indexOf(filterText) !== -1;
    });
  }

  function _wireCVSortAndFilter() {
    var nameBtn = document.getElementById('cvSortByName');
    var yearBtn = document.getElementById('cvSortByYear');
    var filterInput = document.getElementById('cvFilterInput');

    // Reset UI state
    if (filterInput) filterInput.value = '';
    if (nameBtn) {
      nameBtn.className = 'btn btn-outline-secondary btn-sm';
    }
    if (yearBtn) {
      yearBtn.className = 'btn btn-outline-secondary btn-sm';
    }

    // Reset sort direction
    _cvSortNameAsc = true;
    _cvSortYearAsc = true;

    // Clone buttons to remove old listeners
    if (nameBtn) {
      var newNameBtn = nameBtn.cloneNode(true);
      nameBtn.parentNode.replaceChild(newNameBtn, nameBtn);
      newNameBtn.addEventListener('click', function () {
        var dir = _cvSortNameAsc ? 1 : -1;
        _cvVolumes.sort(function (a, b) {
          return dir * (a.name || '').toLowerCase().localeCompare((b.name || '').toLowerCase());
        });
        newNameBtn.className = 'btn btn-secondary btn-sm';
        newNameBtn.querySelector('i').className = _cvSortNameAsc ? 'bi bi-sort-alpha-down me-1' : 'bi bi-sort-alpha-up me-1';
        _cvSortNameAsc = !_cvSortNameAsc;
        var otherBtn = document.getElementById('cvSortByYear');
        otherBtn.className = 'btn btn-outline-secondary btn-sm';
        otherBtn.querySelector('i').className = 'bi bi-sort-numeric-down me-1';
        _cvSortYearAsc = true;
        _renderCVVolumeList((_cvFilterFn || _getFilteredVolumes)());
      });
    }
    if (yearBtn) {
      var newYearBtn = yearBtn.cloneNode(true);
      yearBtn.parentNode.replaceChild(newYearBtn, yearBtn);
      newYearBtn.addEventListener('click', function () {
        var dir = _cvSortYearAsc ? 1 : -1;
        _cvVolumes.sort(function (a, b) {
          return dir * ((parseInt(a.start_year) || 0) - (parseInt(b.start_year) || 0));
        });
        newYearBtn.className = 'btn btn-secondary btn-sm';
        newYearBtn.querySelector('i').className = _cvSortYearAsc ? 'bi bi-sort-numeric-down me-1' : 'bi bi-sort-numeric-up me-1';
        _cvSortYearAsc = !_cvSortYearAsc;
        var otherBtn = document.getElementById('cvSortByName');
        otherBtn.className = 'btn btn-outline-secondary btn-sm';
        otherBtn.querySelector('i').className = 'bi bi-sort-alpha-down me-1';
        _cvSortNameAsc = true;
        _renderCVVolumeList((_cvFilterFn || _getFilteredVolumes)());
      });
    }
    if (filterInput) {
      var newFilterInput = filterInput.cloneNode(true);
      filterInput.parentNode.replaceChild(newFilterInput, filterInput);
      newFilterInput.addEventListener('input', function () {
        _renderCVVolumeList((_cvFilterFn || _getFilteredVolumes)());
      });
    }
  }

  // ── ComicVine volume modal (single-file context) ──────────────────────

  function _removeGCDApiLangFilter() {
    var el = document.getElementById('gcdApiLangFilter');
    if (el) el.parentNode.removeChild(el);
    _cvFilterFn = null;
    _gcdApiLangFilter = '';
  }

  /**
   * Wire the inline "Refine" row (cvRefineSearchInput/cvRefineSearchBtn) to
   * re-search a single provider with a revised series name. Shared by the
   * ComicVine/Metron modals and the GCD API results modal so the user can fix
   * a mis-parsed series name (e.g. "Demis" → "Demi's") rather than only the year.
   */
  function _wireInlineRefine(provider, filePath, fileName, skipProviders, parsedSeries, extraBody) {
    var refineRow = document.getElementById('cvRefineSearchRow');
    if (refineRow) refineRow.style.display = '';
    var input = document.getElementById('cvRefineSearchInput');
    var btn = document.getElementById('cvRefineSearchBtn');
    if (input) input.value = parsedSeries || '';
    if (!btn) return;

    // Replace-clone to clear any listener from a previous open.
    var newBtn = btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn, btn);
    newBtn.addEventListener('click', function () {
      var term = (document.getElementById('cvRefineSearchInput').value || '').trim();
      if (!term) return;
      newBtn.disabled = true;
      newBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span>Searching...';

      var requestBody = {
        file_path: filePath,
        file_name: fileName,
        search_term: term,
        only_provider: provider
      };
      var libraryId = _getLibraryId();
      if (libraryId) requestBody.library_id = libraryId;
      if (skipProviders && skipProviders.length) requestBody.skip_providers = skipProviders;
      if (extraBody) {
        Object.keys(extraBody).forEach(function (k) { requestBody[k] = extraBody[k]; });
      }

      fetch('/api/search-metadata', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody)
      })
        .then(function (response) { return response.json(); })
        .then(function (newData) {
          newBtn.disabled = false;
          newBtn.innerHTML = '<i class="bi bi-search me-1"></i>Refine';
          if (newData.success) {
            var m = bootstrap.Modal.getInstance(document.getElementById('comicVineVolumeModal'));
            if (m) m.hide();
          }
          _handleSearchResponse(newData, filePath, fileName, skipProviders);
        })
        .catch(function (error) {
          newBtn.disabled = false;
          newBtn.innerHTML = '<i class="bi bi-search me-1"></i>Refine';
          CLU.showToast('Search Error', error.message || 'Failed to refine search', 'error');
        });
    });
  }

  /**
   * Shared renderer for providers that use the ComicVine volume modal UI
   * (ComicVine and Metron). config = { provider, titlePrefix, unit, onSelect }.
   */
  function _showCVStyleModal(data, filePath, fileName, skipProviders, config) {
    _removeGCDApiLangFilter();
    // Show the inline refine row for single-file context
    var refineRow = document.getElementById('cvRefineSearchRow');
    if (refineRow) refineRow.style.display = '';

    var modalTitle = document.getElementById('comicVineVolumeModalLabel');
    if (modalTitle) {
      modalTitle.textContent = 'Select correct match (via ' + config.titlePrefix + ') - ' +
        data.possible_matches.length + ' ' + config.unit + '(s)';
    }

    // Populate parsed info
    var cvSeries = document.getElementById('cvParsedSeries');
    var cvIssue = document.getElementById('cvParsedIssue');
    var cvYear = document.getElementById('cvParsedYear');
    if (cvSeries && data.parsed_filename) cvSeries.textContent = data.parsed_filename.series_name || '';
    if (cvIssue && data.parsed_filename) cvIssue.textContent = data.parsed_filename.issue_number || '';
    if (cvYear && data.parsed_filename) cvYear.textContent = data.parsed_filename.year || 'Unknown';

    // Store volumes and set click handler for single-file context
    _cvVolumes = data.possible_matches.slice();
    _cvClickHandler = function (volume) {
      var modal = bootstrap.Modal.getInstance(document.getElementById('comicVineVolumeModal'));
      modal.hide();
      config.onSelect(volume);
    };

    _wireCVSortAndFilter();
    _renderCVVolumeList(_cvVolumes);
    _wireSkipButton('cvSkipProviderBtn', data, filePath, fileName, skipProviders);

    // Wire up inline refine search (scoped to this provider via only_provider)
    var parsedSeries = (data.parsed_filename && data.parsed_filename.series_name) || '';
    _wireInlineRefine(config.provider, filePath, fileName, skipProviders, parsedSeries);

    var modal = new bootstrap.Modal(document.getElementById('comicVineVolumeModal'));
    modal.show();
  }

  function _showCVVolumeModal(data, filePath, fileName, skipProviders) {
    _showCVStyleModal(data, filePath, fileName, skipProviders, {
      provider: 'comicvine',
      titlePrefix: 'ComicVine',
      unit: 'Volume',
      onSelect: function (volume) {
        CLU.searchMetadataWithSelection(filePath, fileName, {
          provider: 'comicvine',
          volume_id: volume.id,
          publisher_name: volume.publisher_name
        });
      }
    });
  }

  function _showMetronVolumeModal(data, filePath, fileName, skipProviders) {
    _showCVStyleModal(data, filePath, fileName, skipProviders, {
      provider: 'metron',
      titlePrefix: 'Metron',
      unit: 'Series',
      onSelect: function (volume) {
        CLU.searchMetadataWithSelection(filePath, fileName, {
          provider: 'metron',
          series_id: volume.id
        });
      }
    });
  }

  // ── GCD API series modal (reuses ComicVine volume modal UI) ───────────

  // Track active GCD API language/country filter
  var _gcdApiLangFilter = '';

  function _showGCDApiVolumeModal(data, filePath, fileName, skipProviders) {
    // The inline refine row lets the user fix a mis-parsed series name and
    // re-search GCD API (wired below).
    var refineRow = document.getElementById('cvRefineSearchRow');
    if (refineRow) refineRow.style.display = '';

    // Populate parsed info
    var cvSeries = document.getElementById('cvParsedSeries');
    var cvIssue = document.getElementById('cvParsedIssue');
    var cvYear = document.getElementById('cvParsedYear');
    if (cvSeries && data.parsed_filename) cvSeries.textContent = data.parsed_filename.series_name || '';
    if (cvIssue && data.parsed_filename) cvIssue.textContent = data.parsed_filename.issue_number || '';
    if (cvYear && data.parsed_filename) cvYear.textContent = data.parsed_filename.year || 'Unknown';

    // Store volumes and set click handler. Tag each as GCD API and stash the
    // parsed issue so _renderCVVolumeList can lazily fetch a cover thumbnail.
    var _gcdIssue = (data.parsed_filename && data.parsed_filename.issue_number) || '1';
    _cvVolumes = data.possible_matches.slice().map(function (v) {
      v._gcdApi = true;
      v._gcdIssue = _gcdIssue;
      return v;
    });
    _gcdApiLangFilter = '';
    _cvClickHandler = function (volume) {
      var modal = bootstrap.Modal.getInstance(document.getElementById('comicVineVolumeModal'));
      if (modal) modal.hide();

      CLU.searchMetadataWithSelection(filePath, fileName, {
        provider: 'gcd_api',
        series_id: volume.id
      });
    };

    // Set custom filter function for GCD API (includes language filtering)
    _cvFilterFn = _getGCDApiFilteredVolumes;

    _wireCVSortAndFilter();

    // Build unique language/country values for the filter dropdown
    var langSet = {};
    _cvVolumes.forEach(function (v) {
      var lang = (v.language || '').toUpperCase();
      var country = (v.country || '').toUpperCase();
      var key = lang || country || '';
      if (key) langSet[key] = (langSet[key] || 0) + 1;
    });

    // Insert language filter dropdown after the existing filter controls
    var existingLangFilter = document.getElementById('gcdApiLangFilter');
    if (existingLangFilter) existingLangFilter.parentNode.removeChild(existingLangFilter);

    if (Object.keys(langSet).length > 1) {
      var filterContainer = document.getElementById('cvFilterInput');
      if (filterContainer && filterContainer.parentNode) {
        var selectEl = document.createElement('select');
        selectEl.id = 'gcdApiLangFilter';
        selectEl.className = 'form-select form-select-sm';
        selectEl.style.width = '120px';
        selectEl.innerHTML = '<option value="">All languages</option>';
        Object.keys(langSet).sort().forEach(function (key) {
          selectEl.innerHTML += '<option value="' + key + '">' + key + ' (' + langSet[key] + ')</option>';
        });
        filterContainer.parentNode.insertBefore(selectEl, filterContainer);

        selectEl.addEventListener('change', function () {
          _gcdApiLangFilter = selectEl.value;
          _renderCVVolumeList(_getGCDApiFilteredVolumes());
        });
      }
    }

    // Update title with count
    var modalTitle = document.getElementById('comicVineVolumeModalLabel');
    if (modalTitle) {
      modalTitle.textContent = 'Select correct match (via GCD API) — ' + data.possible_matches.length + ' Series';
    }

    _renderCVVolumeList(_cvVolumes);
    _wireSkipButton('cvSkipProviderBtn', data, filePath, fileName, skipProviders);

    // Allow revising the series name and re-searching GCD API.
    var gcdParsedSeries = (data.parsed_filename && data.parsed_filename.series_name) || '';
    _wireInlineRefine('gcd_api', filePath, fileName, skipProviders, gcdParsedSeries);

    var modal = new bootstrap.Modal(document.getElementById('comicVineVolumeModal'));
    modal.show();
  }

  function _getGCDApiFilteredVolumes() {
    // Apply text filter
    var filtered = _getFilteredVolumes();
    // Apply language/country filter
    if (_gcdApiLangFilter) {
      var f = _gcdApiLangFilter.toLowerCase();
      filtered = filtered.filter(function (v) {
        return (v.language || '').toLowerCase() === f || (v.country || '').toLowerCase() === f;
      });
    }
    return filtered;
  }

  // ── GCD API start year prompt ─────────────────────────────────────────

  function _showGCDApiStartYearPrompt(data, filePath, fileName, skipProviders) {
    var seriesName = (data.parsed_filename && data.parsed_filename.series_name) || 'Unknown';
    var message = data.message || ('No series found for "' + seriesName + '". Enter the year the series started.');

    // Reuse the ComicVine volume modal as a start year prompt
    var modalTitle = document.getElementById('comicVineVolumeModalLabel');
    if (modalTitle) {
      modalTitle.textContent = 'GCD API - Series Start Year Required';
    }

    var cvSeries = document.getElementById('cvParsedSeries');
    var cvIssue = document.getElementById('cvParsedIssue');
    var cvYear = document.getElementById('cvParsedYear');
    if (cvSeries) cvSeries.textContent = seriesName;
    if (cvIssue && data.parsed_filename) cvIssue.textContent = data.parsed_filename.issue_number || '';
    if (cvYear && data.parsed_filename) cvYear.textContent = data.parsed_filename.year || 'Unknown';

    // Hide refine search row
    var refineRow = document.getElementById('cvRefineSearchRow');
    if (refineRow) refineRow.style.display = 'none';

    var volumeList = document.getElementById('cvVolumeList');
    volumeList.innerHTML =
      '<div class="p-3">' +
        '<p class="text-muted">' + CLU.escapeHtml(message) + '</p>' +
        '<label class="form-label small mb-1">Series name</label>' +
        '<input type="text" id="gcdApiSeriesInput" class="form-control mb-2" placeholder="Series name">' +
        '<p class="text-muted small mb-1">If the name was mis-parsed (e.g. a missing apostrophe), correct it ' +
          'above. The GCD API filters by the year a series <strong>started</strong>, not the issue ' +
          'publication year — leave the year blank to search by name only.</p>' +
        '<label class="form-label small mb-1">Series start year <span class="text-muted">(optional)</span></label>' +
        '<div class="input-group mb-2">' +
          '<input type="number" id="gcdApiStartYearInput" class="form-control" placeholder="e.g. 2025" min="1900" max="2100">' +
          '<button class="btn btn-primary" type="button" id="gcdApiStartYearSearchBtn">' +
            '<i class="bi bi-search me-1"></i>Search' +
          '</button>' +
        '</div>' +
        '<button class="btn btn-outline-secondary btn-sm" type="button" id="gcdApiNoYearSearchBtn">' +
          'Search without year filter' +
        '</button>' +
      '</div>';

    // Wire up search with year
    var searchBtn = document.getElementById('gcdApiStartYearSearchBtn');
    var yearInput = document.getElementById('gcdApiStartYearInput');
    var noYearBtn = document.getElementById('gcdApiNoYearSearchBtn');
    var seriesInput = document.getElementById('gcdApiSeriesInput');
    if (seriesInput && seriesName && seriesName !== 'Unknown') seriesInput.value = seriesName;

    function doSearch(startYear) {
      var currentSeries = (seriesInput && seriesInput.value.trim()) || seriesName;
      searchBtn.disabled = true;
      noYearBtn.disabled = true;
      searchBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Searching...';

      var requestBody = {
        file_path: filePath,
        file_name: fileName,
        search_term: currentSeries,
        only_provider: 'gcd_api',
        gcd_api_start_year: startYear || null
      };
      if (skipProviders && skipProviders.length) requestBody.skip_providers = skipProviders;
      var libraryId = _getLibraryId();
      if (libraryId) requestBody.library_id = libraryId;

      fetch('/api/search-metadata', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody)
      })
        .then(function (response) { return response.json(); })
        .then(function (newData) {
          searchBtn.disabled = false;
          noYearBtn.disabled = false;
          searchBtn.innerHTML = '<i class="bi bi-search me-1"></i>Search';

          if (newData.requires_selection && newData.provider === 'gcd_api' && !newData.requires_start_year) {
            // Got results — close and show selection modal
            var cvModal = bootstrap.Modal.getInstance(document.getElementById('comicVineVolumeModal'));
            if (cvModal) cvModal.hide();
            _showGCDApiVolumeModal(newData, filePath, fileName, skipProviders);
          } else if (newData.success) {
            var cvModal = bootstrap.Modal.getInstance(document.getElementById('comicVineVolumeModal'));
            if (cvModal) cvModal.hide();
            CLU.showToast('Metadata Found', 'Metadata found via ' + newData.source, 'success');
            var contract = _getContract();
            if (typeof contract.onMetadataFound === 'function') {
              contract.onMetadataFound(filePath, newData);
            }
          } else if (newData.requires_start_year) {
            CLU.showToast('No Results', 'No series found with that year. Try a different year.', 'warning');
          } else {
            CLU.showToast('No Results', newData.error || 'No metadata found', 'warning');
          }
        })
        .catch(function (error) {
          searchBtn.disabled = false;
          noYearBtn.disabled = false;
          searchBtn.innerHTML = '<i class="bi bi-search me-1"></i>Search';
          CLU.showToast('Search Error', error.message || 'Search failed', 'error');
        });
    }

    searchBtn.addEventListener('click', function () {
      var yr = parseInt(yearInput.value, 10);
      if (!yr || yr < 1900 || yr > 2100) {
        CLU.showToast('Invalid Year', 'Please enter a valid year (1900-2100)', 'warning');
        return;
      }
      doSearch(yr);
    });

    yearInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        searchBtn.click();
      }
    });

    if (seriesInput) {
      seriesInput.addEventListener('keydown', function (e) {
        // Enter with no year → search by name only; with a year → use it.
        if (e.key === 'Enter') {
          var yr = parseInt(yearInput.value, 10);
          if (yr && yr >= 1900 && yr <= 2100) doSearch(yr);
          else doSearch(null);
        }
      });
    }

    noYearBtn.addEventListener('click', function () {
      doSearch(null);
    });

    // Allow skipping straight to the next provider instead of supplying a year.
    _wireSkipButton('cvSkipProviderBtn', data, filePath, fileName, skipProviders);

    var modal = new bootstrap.Modal(document.getElementById('comicVineVolumeModal'));
    modal.show();

    // Focus the series-name input after modal is shown (correcting the name is
    // the common case when GCD API returns nothing).
    document.getElementById('comicVineVolumeModal').addEventListener('shown.bs.modal', function handler() {
      if (seriesInput) seriesInput.focus(); else yearInput.focus();
      document.getElementById('comicVineVolumeModal').removeEventListener('shown.bs.modal', handler);
    });
  }

  // ── ComicVine volume modal (batch/directory context) ──────────────────

  function _showBatchCVVolumeModal(data, dirPath, dirName, skipProviders) {
    skipProviders = skipProviders || [];
    _removeGCDApiLangFilter();
    // Hide refine row for batch context
    var refineRow = document.getElementById('cvRefineSearchRow');
    if (refineRow) refineRow.style.display = 'none';

    // Populate parsed info
    var cvSeries = document.getElementById('cvParsedSeries');
    var cvIssue = document.getElementById('cvParsedIssue');
    var cvYear = document.getElementById('cvParsedYear');
    if (cvSeries) cvSeries.textContent = data.parsed_filename.series_name;
    if (cvIssue) cvIssue.textContent = data.parsed_filename.issue_number + ' files in folder';
    if (cvYear) cvYear.textContent = data.parsed_filename.year || 'Unknown';

    var modalTitle = document.getElementById('comicVineVolumeModalLabel');
    if (modalTitle) {
      modalTitle.textContent = 'Found ' + data.possible_matches.length + ' Volume(s) - Select Correct One';
    }

    // Store volumes and set click handler for batch context
    _cvVolumes = data.possible_matches.slice();
    _cvClickHandler = function (volume) {
      var volumeList = document.getElementById('cvVolumeList');
      volumeList.querySelectorAll('.list-group-item').forEach(function (item) {
        item.classList.remove('active');
      });
      // Find and highlight the clicked item (by matching volume id)
      var items = volumeList.querySelectorAll('.list-group-item');
      items.forEach(function (item) { item.classList.remove('active'); });

      var modal = bootstrap.Modal.getInstance(document.getElementById('comicVineVolumeModal'));
      modal.hide();

      CLU.fetchDirectoryMetadataWithVolume(dirPath, dirName, volume.id);
    };

    _wireCVSortAndFilter();
    _renderCVVolumeList(_cvVolumes);

    // Skip-to-next-provider for batch: re-run the folder fetch excluding the
    // current provider so the remaining providers (e.g. GCD API) process the
    // folder per-file.
    _wireSkipButton('cvSkipProviderBtn', data, dirPath, dirName, skipProviders, function () {
      var newSkip = skipProviders.slice();
      if (data.provider && newSkip.indexOf(data.provider) === -1) {
        newSkip.push(data.provider);
      }
      CLU.showToast('Skipping Provider', 'Trying the next metadata provider for this folder...', 'info');
      CLU.fetchDirectoryMetadata(dirPath, dirName, newSkip);
    });

    var modal = new bootstrap.Modal(document.getElementById('comicVineVolumeModal'));
    modal.show();
  }

  // ── Shared response router ────────────────────────────────────────────

  /**
   * Route a /api/search-metadata response to the right modal/handler.
   * Shared by the initial search, the skip-to-next-provider flow, and the
   * inline refine. skipProviders carries the providers already skipped so the
   * selection modals can decide whether more remain.
   */
  function _handleSearchResponse(data, filePath, fileName, skipProviders) {
    var contract = _getContract();
    var libraryId = _getLibraryId();
    skipProviders = skipProviders || [];

    if (data.requires_selection) {
      if (data.provider === 'metron') {
        _showMetronVolumeModal(data, filePath, fileName, skipProviders);
      } else if (data.provider === 'comicvine') {
        _showCVVolumeModal(data, filePath, fileName, skipProviders);
      } else if (data.provider === 'gcd') {
        // Use page-level GCD modal if available, otherwise show info
        if (typeof showGCDSeriesSelectionModal === 'function') {
          showGCDSeriesSelectionModal(data, filePath, fileName, skipProviders);
          window._cascadeGCDSelection = { filePath: filePath, fileName: fileName, libraryId: libraryId };
        } else {
          CLU.showToast('GCD Selection', 'GCD series selection requires the Files page', 'warning');
        }
      } else if (data.provider === 'gcd_api') {
        if (data.requires_start_year) {
          _showGCDApiStartYearPrompt(data, filePath, fileName, skipProviders);
        } else {
          _showGCDApiVolumeModal(data, filePath, fileName, skipProviders);
        }
      } else if (['mangadex', 'mangaupdates', 'anilist'].indexOf(data.provider) !== -1) {
        if (typeof showMangaSeriesSelectionModal === 'function') {
          showMangaSeriesSelectionModal(data, filePath, fileName, libraryId, skipProviders);
        } else {
          CLU.showToast('Series Selection', 'Series selection requires the Files page', 'warning');
        }
      }
      return;
    }

    if (data.success) {
      CLU.showToast('Metadata Found', 'Metadata found via ' + data.source, 'success');
      if (typeof contract.onMetadataFound === 'function') {
        contract.onMetadataFound(filePath, data);
      }
      return;
    }

    if (data.parsed_filename) {
      _showRefineSearchModal(data, filePath, fileName);
    } else {
      CLU.showToast('No Metadata', data.error || 'No metadata found from any provider', 'warning');
    }
    if (typeof contract.onMetadataError === 'function') {
      contract.onMetadataError(filePath, data.error || 'No metadata found');
    }
  }

  // ── Public API: Single-file metadata search ───────────────────────────

  /**
   * Search metadata for a single file using library providers.
   * @param {string} filePath  Full path to the CBZ file
   * @param {string} fileName  Display name of the file
   */
  CLU.searchMetadata = function (filePath, fileName, searchTerm) {
    var libraryId = _getLibraryId();
    var contract = _getContract();

    CLU.showToast('Searching Metadata', 'Searching metadata for \'' + fileName + '\'...', 'info');

    var requestBody = { file_path: filePath, file_name: fileName };
    if (libraryId) {
      requestBody.library_id = libraryId;
    }
    if (searchTerm) {
      requestBody.search_term = searchTerm;
    }

    fetch('/api/search-metadata', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody)
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        _handleSearchResponse(data, filePath, fileName, []);
      })
      .catch(function (error) {
        console.error('Metadata search error:', error);
        CLU.showToast('Metadata Error', error.message || 'Failed to search metadata', 'error');
        if (typeof contract.onMetadataError === 'function') {
          contract.onMetadataError(filePath, error.message);
        }
      });
  };

  // ── Public API: Skip to next provider ─────────────────────────────────

  /**
   * Re-run the metadata search excluding the providers already shown/skipped.
   * Called from the "Skip to next provider" button on the selection modals.
   * @param {string} filePath
   * @param {string} fileName
   * @param {string} currentProvider  Provider whose modal was just dismissed
   * @param {string[]} skipProviders  Providers skipped before this one
   */
  CLU.skipToNextProvider = function (filePath, fileName, currentProvider, skipProviders) {
    var libraryId = _getLibraryId();
    var newSkip = (skipProviders || []).slice();
    if (currentProvider && newSkip.indexOf(currentProvider) === -1) {
      newSkip.push(currentProvider);
    }

    CLU.showToast('Skipping Provider', 'Trying the next metadata provider...', 'info');

    var requestBody = { file_path: filePath, file_name: fileName, skip_providers: newSkip };
    if (libraryId) {
      requestBody.library_id = libraryId;
    }

    fetch('/api/search-metadata', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody)
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        _handleSearchResponse(data, filePath, fileName, newSkip);
      })
      .catch(function (error) {
        console.error('Skip provider error:', error);
        CLU.showToast('Metadata Error', error.message || 'Failed to search next provider', 'error');
      });
  };

  // ── Public API: Single-file with user selection ───────────────────────

  /**
   * Follow-up search after user picks a volume/series.
   * @param {string} filePath
   * @param {string} fileName
   * @param {Object} selectedMatch  { provider, volume_id, publisher_name }
   */
  CLU.searchMetadataWithSelection = function (filePath, fileName, selectedMatch) {
    var libraryId = _getLibraryId();
    var contract = _getContract();

    CLU.showToast('Fetching Metadata', 'Fetching metadata from ' + selectedMatch.provider + '...', 'info');

    var requestBody = {
      file_path: filePath,
      file_name: fileName,
      selected_match: selectedMatch
    };
    if (libraryId) {
      requestBody.library_id = libraryId;
    }

    fetch('/api/search-metadata', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody)
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.success) {
          CLU.showToast('Metadata Found', 'Metadata found via ' + data.source, 'success');

          if (typeof contract.onMetadataFound === 'function') {
            contract.onMetadataFound(filePath, data);
          }
        } else {
          CLU.showToast('Metadata Error', data.error || 'No metadata found for selection', 'error');
          if (typeof contract.onMetadataError === 'function') {
            contract.onMetadataError(filePath, data.error);
          }
        }
      })
      .catch(function (error) {
        console.error('Metadata selection error:', error);
        CLU.showToast('Metadata Error', error.message || 'Failed to fetch metadata', 'error');
        if (typeof contract.onMetadataError === 'function') {
          contract.onMetadataError(filePath, error.message);
        }
      });
  };

  // ── Public API: Directory batch metadata ──────────────────────────────

  /**
   * Fetch metadata for all files in a directory via SSE streaming.
   * @param {string} dirPath   Full path to the directory
   * @param {string} dirName   Display name of the directory
   */
  CLU.fetchDirectoryMetadata = function (dirPath, dirName, skipProviders) {
    var libraryId = _getLibraryId();
    var contract = _getContract();
    var progressToast = _buildProgressToast();
    skipProviders = skipProviders || [];

    var requestBody = { directory: dirPath };
    if (libraryId) {
      requestBody.library_id = libraryId;
    }
    if (skipProviders.length) {
      requestBody.skip_providers = skipProviders;
    }

    fetch('/api/batch-metadata', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody)
    })
      .then(function (response) {
        var contentType = response.headers.get('content-type');
        if (contentType && contentType.indexOf('application/json') !== -1) {
          return response.json().then(function (data) {
            if (data.requires_selection) {
              _removeToast(progressToast);
              _showBatchCVVolumeModal(data, dirPath, dirName, skipProviders);
              return;
            }
            if (data.error) {
              throw new Error(data.error);
            }
          });
        }

        return _processSSEStream(response, progressToast, function (result) {
          var summary = _buildSummary(result);
          var toastType = result.errors > 0 ? 'warning' : (result.processed > 0 || result.cvinfo_created ? 'success' : 'info');
          CLU.showToast('Metadata Fetch Complete', summary, toastType);

          if (typeof contract.onBatchComplete === 'function') {
            contract.onBatchComplete(dirPath, result);
          }
        });
      })
      .catch(function (error) {
        _removeToast(progressToast);
        CLU.showToast('Metadata Error', 'Error fetching metadata: ' + error.message, 'error');
      });
  };

  // ── Public API: Directory batch with pre-selected volume ──────────────

  /**
   * Fetch metadata for all files in a directory with a pre-selected volume.
   * @param {string} dirPath
   * @param {string} dirName
   * @param {string|number} volumeId
   */
  CLU.fetchDirectoryMetadataWithVolume = function (dirPath, dirName, volumeId) {
    var libraryId = _getLibraryId();
    var contract = _getContract();
    var progressToast = _buildProgressToast();

    var requestBody = { directory: dirPath, volume_id: volumeId };
    if (libraryId) {
      requestBody.library_id = libraryId;
    }

    fetch('/api/batch-metadata', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody)
    })
      .then(function (response) {
        var contentType = response.headers.get('content-type');
        if (contentType && contentType.indexOf('application/json') !== -1) {
          return response.json().then(function (data) {
            if (data.error) {
              throw new Error(data.error);
            }
          });
        }

        return _processSSEStream(response, progressToast, function (result) {
          var summary = _buildSummary(result);
          var toastType = result.errors > 0 ? 'warning' : 'success';
          CLU.showToast(
            result.errors > 0 ? 'Metadata Complete (with errors)' : 'Metadata Complete',
            summary.length > 0 ? summary : 'No changes needed',
            toastType
          );

          if (typeof contract.onBatchComplete === 'function') {
            contract.onBatchComplete(dirPath, result);
          }
        });
      })
      .catch(function (error) {
        _removeToast(progressToast);
        CLU.showToast('Metadata Error', error.message, 'error');
      });
  };

  // ── Shared rename-after-metadata ──────────────────────────────────────
  //
  // A single-file metadata fetch (/api/search-metadata) applies ComicInfo.xml
  // server-side but does NOT rename the file — it returns a `rename_config`
  // and leaves renaming to the client. These helpers centralize that logic so
  // every page (Files, Source Wall, Collection) renames identically. Honors
  // the same gate as the legacy Files-page flow: rename only when auto_rename
  // is enabled.

  var MONTH_NAMES = ['', 'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'];

  CLU.padIssueNumber = function (numStr, width) {
    width = width || 3;
    numStr = String(numStr).trim();
    if (!numStr) return '';
    if (numStr.indexOf('.') !== -1) {
      var parts = numStr.split('.');
      return parts[0].padStart(width, '0') + '.' + parts.slice(1).join('.');
    }
    return numStr.padStart(width, '0');
  };

  /**
   * Build the suggested filename from fetched metadata and the rename config.
   * @returns {string|null}  New filename, or null when it would not change.
   */
  CLU.buildRenamedName = function (metadata, renameConfig, fileName) {
    var suggestedName;
    var extMatch = fileName.match(/\.(cbz|cbr)$/i);
    var ext = extMatch ? extMatch[0] : '.cbz';

    if (renameConfig && renameConfig.enabled && renameConfig.pattern) {
      var pattern = renameConfig.pattern;

      var series = metadata.Series || '';
      series = series.replace(/:/g, ' -');          // colon -> dash (Windows)
      series = series.replace(/[<>"/\\|?*]/g, '');   // strip invalid chars

      var issueNumber = CLU.padIssueNumber(metadata.Number);
      var year = metadata.Year || '';
      var volumeNumber = '';  // ComicVine uses year as Volume, not a volume number
      var issueYear = metadata.Year || '';
      // {volume_year} = series/volume start year (ComicInfo Volume field). GCD stores
      // a volume number there, so guard to a 4-digit year and fall back to issue year.
      var volRaw = String(metadata.Volume || '').trim();
      var volumeYear = /^\d{4}$/.test(volRaw) ? volRaw : '';

      // {issue_month_M}/{issue_month_m} from ComicInfo Month (the cover month)
      var monthNum = parseInt(metadata.Month, 10);
      var issueMonthName = '', issueMonthPadded = '';
      if (monthNum >= 1 && monthNum <= 12) {
        issueMonthName = MONTH_NAMES[monthNum];
        issueMonthPadded = String(monthNum).padStart(2, '0');
      }

      var issueTitle = metadata.Title || '';
      issueTitle = issueTitle.replace(/:/g, ' -');
      issueTitle = issueTitle.replace(/[<>"/\\|?*]/g, '');
      issueTitle = issueTitle.replace(/[\x00-\x1f]/g, '');
      issueTitle = issueTitle.replace(/^[.\s]+|[.\s]+$/g, '');

      var result = pattern;
      result = result.replace(/{series_name}/gi, series);
      result = result.replace(/{issue_number}/gi, issueNumber);
      result = result.replace(/{issue_year}/gi, issueYear);
      // Month variants are case-sensitive: {..._M} (name) vs {..._m} (padded)
      result = result.replace(/{issue_month_M}/g, issueMonthName);
      result = result.replace(/{issue_month_m}/g, issueMonthPadded);
      result = result.replace(/{volume_year}/gi, volumeYear || year);
      result = result.replace(/{YYYY}/g, year);
      result = result.replace(/{volume_number}/gi, volumeNumber);
      result = result.replace(/{issue_title}/gi, issueTitle);

      // Drop any leftover {token} we don't recognize (mirrors apply_custom_pattern)
      result = result.replace(/\{[A-Za-z_][^}]*\}/g, '');

      result = result.replace(/\s+/g, ' ').trim();
      // Remove a separator left dangling against a parenthesis boundary
      // (e.g. "(2010-)" -> "(2010)", "(, 2026)" -> "(2026)") when a token resolved empty
      result = result.replace(/\(\s*[-,]\s*/g, '(');
      result = result.replace(/\s*[-,]\s*\)/g, ')');
      // Remove empty parentheses
      result = result.replace(/\s*\(\s*\)/g, '').trim();
      // Remove orphaned separators (e.g. trailing " - " when issue_title is empty)
      result = result.replace(/\s*-\s*(?=\(|$)/g, ' ').replace(/\s+/g, ' ').trim();

      suggestedName = result + ext;
    } else {
      // Default pattern: "Series Number.ext"
      var s = (metadata.Series || '');
      s = s.replace(/:/g, ' -');
      s = s.replace(/[<>"/\\|?*]/g, '');
      s = s.replace(/\s+/g, ' ').trim();
      suggestedName = s + ' ' + CLU.padIssueNumber(metadata.Number) + ext;
    }

    if (suggestedName === fileName) return null;
    return suggestedName;
  };

  /**
   * Rename a file after metadata via POST /rename.
   * @param {string}   filePath   Current full path of the file
   * @param {string}   oldName    Current filename (for messaging)
   * @param {string}   newName    New filename
   * @param {function} onRenamed  Called (oldPath, newPath, newName) on success
   */
  CLU.renameAfterMetadata = function (filePath, oldName, newName, onRenamed) {
    var directory = filePath.substring(0, filePath.lastIndexOf('/'));
    var newPath = directory + '/' + newName;

    fetch('/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old: filePath, new: newPath })
    })
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (err) {
            throw new Error(err.error || 'Rename failed');
          });
        }
        return response.json();
      })
      .then(function (data) {
        if (data.success) {
          CLU.showToast('File Renamed', 'Successfully renamed to: ' + newName, 'success');
          if (typeof onRenamed === 'function') {
            onRenamed(filePath, newPath, newName);
          }
        } else {
          CLU.showToast('Rename Failed', data.error || 'Failed to rename file', 'error');
        }
      })
      .catch(function (error) {
        console.error('Rename error:', error);
        CLU.showToast('Rename Error', error.message, 'error');
      });
  };

  /**
   * Inspect a single-file metadata response and rename when configured.
   * Safe no-op unless rename is enabled, auto_rename is on, and the name changes.
   * @param {string}   filePath   Path passed to onMetadataFound
   * @param {string}   fileName   Original filename
   * @param {Object}   data       /api/search-metadata success payload
   * @param {function} onRenamed  Called (oldPath, newPath, newName) on success
   */
  CLU.maybeRenameAfterMetadata = function (filePath, fileName, data, onRenamed) {
    if (!data || !data.metadata || !data.rename_config || !data.rename_config.enabled) {
      return;
    }
    // If the file was auto-moved, rename the file at its new location.
    var actualPath = (data.moved && data.new_file_path) ? data.new_file_path : filePath;
    var newName = CLU.buildRenamedName(data.metadata, data.rename_config, fileName);
    if (!newName) return;
    // Match the Files-page gate: only rename automatically when auto_rename is set.
    if (!data.rename_config.auto_rename) return;
    CLU.renameAfterMetadata(actualPath, fileName, newName, onRenamed);
  };

})();
