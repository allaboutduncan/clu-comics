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

  // ── ComicVine volume modal (single-file context) ──────────────────────

  function _showCVVolumeModal(data, filePath, fileName) {
    // Show the inline refine row for single-file context
    var refineRow = document.getElementById('cvRefineSearchRow');
    if (refineRow) refineRow.style.display = '';

    var modalTitle = document.getElementById('comicVineVolumeModalLabel');
    if (modalTitle) {
      modalTitle.textContent = 'Select correct match (via ComicVine) - ' + data.possible_matches.length + ' Volume(s)';
    }

    // Populate parsed info
    var cvSeries = document.getElementById('cvParsedSeries');
    var cvIssue = document.getElementById('cvParsedIssue');
    var cvYear = document.getElementById('cvParsedYear');
    if (cvSeries && data.parsed_filename) cvSeries.textContent = data.parsed_filename.series_name || '';
    if (cvIssue && data.parsed_filename) cvIssue.textContent = data.parsed_filename.issue_number || '';
    if (cvYear && data.parsed_filename) cvYear.textContent = data.parsed_filename.year || 'Unknown';

    var volumeList = document.getElementById('cvVolumeList');
    volumeList.innerHTML = '';

    data.possible_matches.forEach(function (volume) {
      var volumeItem = document.createElement('div');
      volumeItem.className = 'list-group-item list-group-item-action d-flex align-items-start';
      volumeItem.style.cursor = 'pointer';

      var yearDisplay = volume.start_year || 'Unknown';
      var issueCount = volume.count_of_issues || 'Unknown';
      var descriptionPreview = volume.description ?
        '<small class="text-muted d-block mt-1">' + volume.description + '</small>' : '';

      var thumbnailHtml = volume.image_url ?
        '<img src="' + volume.image_url + '" class="img-thumbnail me-3" style="width: 80px; height: 120px; object-fit: cover;" alt="' + CLU.escapeHtml(volume.name) + ' cover">' :
        '<div class="me-3 d-flex align-items-center justify-content-center bg-secondary text-white" style="width: 80px; height: 120px; font-size: 10px;">No Cover</div>';

      volumeItem.innerHTML =
        thumbnailHtml +
        '<div class="flex-grow-1 d-flex justify-content-between align-items-start">' +
          '<div class="me-2">' +
            '<div class="fw-bold">' + CLU.escapeHtml(volume.name) + '</div>' +
            '<small class="text-muted">Publisher: ' + CLU.escapeHtml(volume.publisher_name || 'Unknown') + '<br>Issues: ' + issueCount + '</small>' +
            descriptionPreview +
          '</div>' +
          '<span class="badge bg-success rounded-pill">' + yearDisplay + '</span>' +
        '</div>';

      volumeItem.addEventListener('click', function () {
        var modal = bootstrap.Modal.getInstance(document.getElementById('comicVineVolumeModal'));
        modal.hide();

        CLU.searchMetadataWithSelection(filePath, fileName, {
          provider: 'comicvine',
          volume_id: volume.id,
          publisher_name: volume.publisher_name
        });
      });

      volumeList.appendChild(volumeItem);
    });

    // Wire up inline refine search
    var cvRefineInput = document.getElementById('cvRefineSearchInput');
    var cvRefineBtn = document.getElementById('cvRefineSearchBtn');
    if (cvRefineInput && data.parsed_filename) {
      cvRefineInput.value = data.parsed_filename.series_name || '';
    }
    if (cvRefineBtn) {
      var newRefineBtn = cvRefineBtn.cloneNode(true);
      cvRefineBtn.parentNode.replaceChild(newRefineBtn, cvRefineBtn);
      newRefineBtn.addEventListener('click', function () {
        var refinedTerm = (document.getElementById('cvRefineSearchInput').value || '').trim();
        if (!refinedTerm) return;
        newRefineBtn.disabled = true;
        newRefineBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span>Searching...';

        var requestBody = { file_path: filePath, file_name: fileName, search_term: refinedTerm };
        var libraryId = _getLibraryId();
        if (libraryId) requestBody.library_id = libraryId;

        fetch('/api/search-metadata', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requestBody)
        })
          .then(function (response) { return response.json(); })
          .then(function (newData) {
            newRefineBtn.disabled = false;
            newRefineBtn.innerHTML = '<i class="bi bi-search me-1"></i>Refine';

            if (newData.requires_selection && newData.provider === 'comicvine') {
              // Re-populate volume list in-place
              _showCVVolumeModal(newData, filePath, fileName);
            } else if (newData.success) {
              var modal = bootstrap.Modal.getInstance(document.getElementById('comicVineVolumeModal'));
              if (modal) modal.hide();
              CLU.showToast('Metadata Found', 'Metadata found via ' + newData.source, 'success');
              var contract = _getContract();
              if (typeof contract.onMetadataFound === 'function') {
                contract.onMetadataFound(filePath, newData);
              }
            } else {
              CLU.showToast('No Results', newData.error || 'No metadata found for refined search', 'warning');
            }
          })
          .catch(function (error) {
            newRefineBtn.disabled = false;
            newRefineBtn.innerHTML = '<i class="bi bi-search me-1"></i>Refine';
            CLU.showToast('Search Error', error.message || 'Failed to refine search', 'error');
          });
      });
    }

    var modal = new bootstrap.Modal(document.getElementById('comicVineVolumeModal'));
    modal.show();
  }

  // ── ComicVine volume modal (batch/directory context) ──────────────────

  function _showBatchCVVolumeModal(data, dirPath, dirName) {
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

    var volumeList = document.getElementById('cvVolumeList');
    volumeList.innerHTML = '';

    data.possible_matches.forEach(function (volume) {
      var volumeItem = document.createElement('div');
      volumeItem.className = 'list-group-item list-group-item-action d-flex align-items-start';
      volumeItem.style.cursor = 'pointer';

      var yearDisplay = volume.start_year || 'Unknown';
      var issueCount = volume.count_of_issues || 'Unknown';
      var descriptionPreview = volume.description ?
        '<small class="text-muted d-block mt-1">' + volume.description + '</small>' : '';

      var thumbnailHtml = volume.image_url ?
        '<img src="' + volume.image_url + '" class="img-thumbnail me-3" style="width: 80px; height: 120px; object-fit: cover;" alt="' + CLU.escapeHtml(volume.name) + ' cover">' :
        '<div class="me-3 d-flex align-items-center justify-content-center bg-secondary text-white" style="width: 80px; height: 120px; font-size: 10px;">No Cover</div>';

      volumeItem.innerHTML =
        thumbnailHtml +
        '<div class="flex-grow-1 d-flex justify-content-between align-items-start">' +
          '<div class="me-2">' +
            '<div class="fw-bold">' + CLU.escapeHtml(volume.name) + '</div>' +
            '<small class="text-muted">Publisher: ' + CLU.escapeHtml(volume.publisher_name || 'Unknown') + '<br>Issues: ' + issueCount + '</small>' +
            descriptionPreview +
          '</div>' +
          '<span class="badge bg-success rounded-pill">' + yearDisplay + '</span>' +
        '</div>';

      volumeItem.addEventListener('click', function () {
        volumeList.querySelectorAll('.list-group-item').forEach(function (item) {
          item.classList.remove('active');
        });
        volumeItem.classList.add('active');

        var modal = bootstrap.Modal.getInstance(document.getElementById('comicVineVolumeModal'));
        modal.hide();

        CLU.fetchDirectoryMetadataWithVolume(dirPath, dirName, volume.id);
      });

      volumeList.appendChild(volumeItem);
    });

    var modal = new bootstrap.Modal(document.getElementById('comicVineVolumeModal'));
    modal.show();
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
        if (data.requires_selection) {
          if (data.provider === 'comicvine') {
            _showCVVolumeModal(data, filePath, fileName);
          } else if (data.provider === 'gcd') {
            // Use page-level GCD modal if available, otherwise show info
            if (typeof showGCDSeriesSelectionModal === 'function') {
              showGCDSeriesSelectionModal(data, filePath, fileName);
              window._cascadeGCDSelection = { filePath: filePath, fileName: fileName, libraryId: libraryId };
            } else {
              CLU.showToast('GCD Selection', 'GCD series selection requires the Files page', 'warning');
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
      })
      .catch(function (error) {
        console.error('Metadata search error:', error);
        CLU.showToast('Metadata Error', error.message || 'Failed to search metadata', 'error');
        if (typeof contract.onMetadataError === 'function') {
          contract.onMetadataError(filePath, error.message);
        }
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
  CLU.fetchDirectoryMetadata = function (dirPath, dirName) {
    var libraryId = _getLibraryId();
    var contract = _getContract();
    var progressToast = _buildProgressToast();

    var requestBody = { directory: dirPath };
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
            if (data.requires_selection) {
              _removeToast(progressToast);
              _showBatchCVVolumeModal(data, dirPath, dirName);
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

})();
