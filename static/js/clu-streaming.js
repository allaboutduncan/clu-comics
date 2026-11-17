/**
 * CLU Streaming Operations  –  clu-streaming.js
 *
 * SSE-based script execution with progress tracking.
 * Provides: CLU.executeStreamingOp (single file), CLU.executeDirectoryOp (directory)
 *
 * Depends on: clu-utils.js  (CLU.showToast, CLU.updateProgress, etc.)
 *
 * External contract (set by page before calling):
 *   window._cluStreaming = {
 *     onComplete(scriptType, path)            – called after successful completion
 *     onError(scriptType, path, errorMsg)     – called on error (optional)
 *   }
 *
 * DOM contracts (optional):
 *   #progress-container / #progress-bar / #progress-text  (managed via CLU utils)
 */
(function () {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  // ── Helpers ─────────────────────────────────────────────────────────────

  function _getContract() {
    return window._cluStreaming || {};
  }

  function _filename(path) {
    return path ? path.split('/').pop() : '';
  }

  // ── Single-file streaming op ────────────────────────────────────────────

  var OP_LABELS = {
    crop: 'Crop Cover',
    remove: 'Remove 1st Image',
    single_file: 'Rebuild',
    enhance_single: 'Enhance',
    add: 'Add Blank to End',
    'delete': 'Delete'
  };

  // ── Busy overlay on the file being worked on ────────────────────────────
  //
  // The header indicator is out of sight once the page is scrolled, so the
  // file itself also carries a spinner wherever it is listed. Two sources
  // feed it: actions started on this page (marked at once) and the server's
  // operations list (every poll), which is what re-marks a file after the
  // list is redrawn or the page is reopened mid-action.

  var _localBusy = {};    // path -> label, actions this page started
  var _serverBusy = {};   // path -> label, from the last operations poll
  var BUSY_ATTRS = ['data-path', 'data-fullpath', 'data-issue-path'];

  function _busyLabel(path) {
    return _localBusy[path] || _serverBusy[path] || null;
  }

  /** The element the overlay covers: the cover when the item has one. */
  function _busyHost(el) {
    var thumb = el.querySelector('.thumbnail-container');
    if (thumb) return thumb;
    if (el.tagName === 'TR') return el.cells[0] || el;
    return el;
  }

  function _applyBusy() {
    var stale = document.querySelectorAll('.clu-file-busy-overlay');
    for (var i = 0; i < stale.length; i++) {
      if (!_busyLabel(stale[i].dataset.busyPath)) {
        var old = stale[i].parentNode;
        old.removeChild(stale[i]);
        old.classList.remove('clu-file-busy');
        old.removeAttribute('aria-busy');
      }
    }
    var paths = Object.keys(_localBusy).concat(Object.keys(_serverBusy));
    paths.forEach(function (path) {
      var label = _busyLabel(path);
      BUSY_ATTRS.forEach(function (attr) {
        var els = document.querySelectorAll('[' + attr + '="' + CSS.escape(path) + '"]');
        for (var j = 0; j < els.length; j++) {
          var host = _busyHost(els[j]);
          if (host.querySelector(':scope > .clu-file-busy-overlay')) continue;
          var overlay = document.createElement('div');
          overlay.className = 'clu-file-busy-overlay';
          overlay.dataset.busyPath = path;
          overlay.setAttribute('role', 'status');
          var spinner = document.createElement('span');
          spinner.className = 'spinner-border spinner-border-sm';
          spinner.setAttribute('aria-hidden', 'true');
          var text = document.createElement('span');
          text.className = 'clu-file-busy-text';
          text.textContent = label + '…';
          overlay.appendChild(spinner);
          overlay.appendChild(text);
          host.classList.add('clu-file-busy');
          host.setAttribute('aria-busy', 'true');
          host.appendChild(overlay);
        }
      });
    });
  }

  function _setLocalBusy(path, label) {
    if (label) _localBusy[path] = label; else delete _localBusy[path];
    _applyBusy();
  }

  document.addEventListener('clu:operations', function (event) {
    _serverBusy = {};
    (event.detail || []).forEach(function (op) {
      if (op.path && (op.status === 'running' || op.status === 'queued')) {
        _serverBusy[op.path] = String(op.label || '').split(':')[0] || 'Working';
      }
    });
    _applyBusy();
  });

  /** Ask the header indicator to redraw now rather than at its next poll. */
  function _refreshOpsIndicator() {
    if (typeof window.updateOperationsIndicator === 'function') {
      window.updateOperationsIndicator();
    }
  }

  /**
   * Execute a streaming operation on a single file.
   * Supports: crop, remove, single_file, enhance_single, add, delete
   *
   * Progress is shown in the header operations indicator, the same on every
   * page, and never in a page's own progress card: the server registers the
   * operation and keeps following it if this page goes away. This stream only
   * tells the page when to refresh and what to say at the end.
   *
   * @param {string} scriptType
   * @param {string} filePath
   */
  CLU.executeStreamingOp = function (scriptType, filePath) {
    if (!filePath) {
      CLU.showError('No file path provided');
      return;
    }

    var url = '/stream/' + scriptType + '?file_path=' + encodeURIComponent(filePath);
    var fname = _filename(filePath);
    var label = OP_LABELS[scriptType] || scriptType;
    // Taken now: the page overwrites window._cluStreaming on its next action,
    // and a second action must not steal this one's callbacks.
    var contract = _getContract();

    _setLocalBusy(filePath, label);

    var eventSource = new EventSource(url);
    var completed = false;
    // The server ends a failed run with an "An error occurred" line and then
    // closes the stream, which the browser reports as a connection error.
    // Remember that it was the operation that failed, and the last reason it
    // gave, so the toast does not blame the network.
    var failed = false;
    var failureReason = '';

    eventSource.onopen = function () {
      setTimeout(_refreshOpsIndicator, 300);
    };

    eventSource.onmessage = function (event) {
      var line = event.data.trim();
      if (!line) return;
      var reason = line.match(/Failed to .+/);
      if (reason) failureReason = reason[0];
      if (line.indexOf('An error occurred') === 0) failed = true;
    };

    eventSource.addEventListener('completed', function () {
      completed = true;
      eventSource.close();
      _setLocalBusy(filePath, null);
      CLU.showToast(label, 'Completed for ' + fname, 'success');
      _refreshOpsIndicator();
      if (typeof contract.onComplete === 'function') {
        contract.onComplete(scriptType, filePath);
      }
    });

    eventSource.onerror = function () {
      eventSource.close();

      setTimeout(function () {
        if (completed) return;
        // If the action is still running, the next operations poll re-marks
        // the file from the server's list.
        _setLocalBusy(filePath, null);
        _refreshOpsIndicator();
        var message;
        if (failed) {
          message = failureReason || (label + ' failed for ' + fname);
          if (scriptType === 'single_file') {
            message += ' — the file was left as it was. See Library Health for details.';
          }
          CLU.showError(message);
        } else {
          // The stream dropped, not the operation: the server keeps running
          // it and the header indicator still shows it.
          message = 'Connection error';
          CLU.showToast(label,
            'Lost the live connection for ' + fname +
            '. It keeps running; check the operations indicator in the header.',
            'warning');
        }
        if (typeof contract.onError === 'function') {
          contract.onError(scriptType, filePath, message);
        }
      }, 100);
    };
  };

  // ── Directory streaming op ──────────────────────────────────────────────

  /**
   * Execute a streaming operation on a directory.
   * Supports: convert, rebuild, pdf, missing, enhance_dir
   *
   * @param {string} scriptType
   * @param {string} directoryPath
   */
  CLU.executeDirectoryOp = function (scriptType, directoryPath, options) {
    if (!directoryPath) {
      CLU.showError('No directory path provided');
      return;
    }

    options = options || {};
    var url = '/stream/' + scriptType + '?directory=' + encodeURIComponent(directoryPath);
    if (options.recursive) {
      url += '&recursive=1';
    }
    console.log('CLU.executeDirectoryOp:', scriptType, directoryPath, options);

    // Reset & show progress
    CLU.resetProgress();
    CLU.updateProgress(0, 'Starting ' + scriptType + ' operation...');
    CLU.showProgressIndicator();

    // Tracking state
    var progressData = { totalFiles: 0, processedFiles: 0, initialized: false, warnings: [] };
    window.progressData = progressData;  // expose for backward compat

    var eventSource = new EventSource(url);
    var completed = false;
    var progressBar = document.getElementById('progress-bar');
    var progressText = document.getElementById('progress-text');

    eventSource.onmessage = function (event) {
      var line = event.data.trim();
      if (!line) return;
      console.log('Progress:', line);

      if (scriptType === 'convert' || scriptType === 'rebuild') {
        _parseConvertRebuild(line, progressData, progressBar, progressText, scriptType);
      } else if (scriptType === 'pdf') {
        _parsePdf(line, progressData, progressBar, progressText);
      } else if (scriptType === 'missing') {
        _parseMissing(line, progressBar, progressText);
      } else if (scriptType === 'enhance_dir') {
        _parseEnhanceDir(line, progressBar, progressText);
      } else {
        if (progressText) progressText.textContent = line;
      }
    };

    eventSource.addEventListener('completed', function (event) {
      completed = true;
      console.log('Operation completed:', event.data);

      if (progressBar) {
        progressBar.style.width = '100%';
        progressBar.textContent = '100%';
        progressBar.setAttribute('aria-valuenow', '100');
      }
      if (progressText) {
        progressText.textContent = scriptType + ' operation completed successfully!';
      }

      eventSource.close();

      // Handle missing file check results
      if (scriptType === 'missing' && window.missingFileData) {
        CLU.hideProgressIndicator();
        if (typeof showMissingFileCheckModal === 'function') {
          showMissingFileCheckModal(window.missingFileData);
        }
        window.missingFileData = null;
      } else if (progressData.warnings && progressData.warnings.length > 0) {
        var warnMsg = 'Completed with warnings:\n' + progressData.warnings.join('\n');
        CLU.showToast('Warning', warnMsg, 'warning');
        setTimeout(function () { CLU.hideProgressIndicator(); }, 5000);
      } else {
        CLU.showToast('Success', 'Directory operation completed successfully!', 'success');
        setTimeout(function () { CLU.hideProgressIndicator(); }, 5000);
      }

      var contract = _getContract();
      if (typeof contract.onComplete === 'function') {
        contract.onComplete(scriptType, directoryPath);
      }
    });

    eventSource.onerror = function (error) {
      console.error('EventSource error:', error);
      eventSource.close();

      setTimeout(function () {
        if (!completed) {
          if (progressData.totalFiles > 10) {
            CLU.showToast('Info',
              'Live progress stream ended. Operation continues in the background \u2014 check the header indicator.',
              'info');
            if (progressText) {
              progressText.textContent = 'Live stream disconnected \u2014 operation continues in background. Check the header indicator for progress.';
            }
            if (progressBar) progressBar.className = 'progress-bar bg-info';
          } else {
            CLU.showError('Connection error during operation');
            if (progressText) progressText.textContent = 'Error: Connection lost during operation';
            if (progressBar) progressBar.className = 'progress-bar bg-danger';
          }

          var contract = _getContract();
          if (typeof contract.onError === 'function') {
            contract.onError(scriptType, directoryPath, 'Connection error');
          }
        }
      }, 100);
    };
  };

  // ── Progress parsers (private) ──────────────────────────────────────────

  function _parseConvertRebuild(line, pd, bar, txt, scriptType) {
    // Total files count
    if (line.includes('Found') && (line.includes('files to convert') || line.includes('files to process')) && !pd.initialized) {
      var m = line.match(/Found (\d+) files to (?:convert|process)/);
      if (m) {
        pd.totalFiles = parseInt(m[1]);
        pd.initialized = true;
        if (txt) txt.textContent = 'Found ' + pd.totalFiles + ' files to process. Starting...';
        if (pd.totalFiles > 10) {
          var hint = document.getElementById('progress-nav-hint');
          if (!hint && txt) {
            hint = document.createElement('div');
            hint.id = 'progress-nav-hint';
            hint.className = 'alert alert-info alert-dismissible fade show mt-2 mb-0 py-2 small';
            hint.innerHTML = '<i class="bi bi-info-circle me-1"></i>' +
              'Large operation \u2014 you can safely navigate away. ' +
              'Progress is tracked in the <strong>header indicator</strong> <i class="bi bi-arrow-repeat"></i>' +
              '<button type="button" class="btn-close" data-bs-dismiss="alert" style="font-size:0.5rem;padding:0.65rem;"></button>';
            txt.parentNode.appendChild(hint);
          }
        }
      }
    }

    // File processing
    if (line.includes('Processing file:') && pd.initialized) {
      var m2 = line.match(/Processing file: (.+?) \((\d+)\/(\d+)\)/);
      if (m2) {
        var fname = m2[1], current = parseInt(m2[2]), total = parseInt(m2[3]);
        pd.processedFiles = current;
        if (total > 0) {
          var pct = Math.round((current / total) * 100);
          var remaining = total - current;
          if (bar) {
            bar.style.width = pct + '%';
            bar.textContent = pct + '% (' + current + '/' + total + ')';
            bar.setAttribute('aria-valuenow', String(pct));
          }
          if (txt) txt.textContent = 'Processing: ' + fname + ' - ' + remaining + ' file' + (remaining !== 1 ? 's' : '') + ' remaining';
        }
      }
    }

    // Large file
    if (line.includes('Processing large file') && line.includes('MB')) {
      var m3 = line.match(/Processing large file \((\d+\.\d+)MB\): (.+)/);
      if (m3 && txt) txt.textContent = 'Processing large file (' + m3[1] + 'MB): ' + m3[2] + ' - This may take several minutes...';
    }

    // Compression/extraction progress
    if (line.includes('Compression progress:')) {
      var m4 = line.match(/Compression progress: (\d+\.\d+)% \((\d+)\/(\d+) files\)/);
      if (m4 && txt) txt.textContent = 'Compressing files: ' + m4[1] + '% (' + m4[2] + '/' + m4[3] + ' files)';
    }
    if (line.includes('Extraction progress:')) {
      var m5 = line.match(/Extraction progress: (\d+\.\d+)% \((\d+)\/(\d+) files\)/);
      if (m5 && txt) txt.textContent = 'Extracting files: ' + m5[1] + '% (' + m5[2] + '/' + m5[3] + ' files)';
    }

    // Step progress
    var stepMatch = line.match(/Step (\d+)\/(\d+): (.+)/);
    if (stepMatch && txt) txt.textContent = 'Step ' + stepMatch[1] + '/' + stepMatch[2] + ': ' + stepMatch[3];

    // A file that could not be converted is kept as it was; name it in the
    // closing toast instead of reporting the whole run as a success.
    if (line.includes('Not converted:')) {
      var mw = line.match(/Not converted: (.+)/);
      if (mw) {
        pd.warnings.push(mw[1]);
      }
    }

    // Completion
    if ((line.includes('Conversion completed') || line.includes('Rebuild completed')) && pd.initialized) {
      if (bar) {
        bar.style.width = '100%';
        bar.textContent = '100% (' + pd.totalFiles + '/' + pd.totalFiles + ')';
        bar.setAttribute('aria-valuenow', '100');
      }
      if (txt) txt.textContent = 'Completed processing ' + pd.totalFiles + ' files!';
    }
  }

  function _parsePdf(line, pd, bar, txt) {
    if (line.includes('Found') && line.includes('PDF')) {
      var m = line.match(/Found (\d+) PDF/);
      if (m) {
        pd.totalFiles = parseInt(m[1]);
        pd.initialized = true;
        if (txt) txt.textContent = 'Found ' + pd.totalFiles + ' PDF files to convert...';
      }
    }
    if (line.includes('Converting:') || line.includes('Processing:')) {
      if (txt) txt.textContent = line;
    }
    if (line.includes('completed') || line.includes('Completed')) {
      if (bar) {
        bar.style.width = '100%';
        bar.textContent = '100%';
        bar.setAttribute('aria-valuenow', '100');
      }
      if (txt) txt.textContent = 'PDF conversion completed!';
    }
  }

  function _parseMissing(line, bar, txt) {
    if (line.includes('Checking') || line.includes('Scanning') || line.includes('Missing File Check')) {
      if (txt) txt.textContent = line.replace(/<[^>]*>/g, '');
      if (bar) {
        bar.style.width = '50%';
        bar.textContent = 'Scanning...';
      }
    }

    if (line.includes('missing issues')) {
      var cleanLine = line.replace(/<[^>]*>/g, '');
      if (txt) txt.textContent = cleanLine;
      if (bar) {
        bar.style.width = '75%';
        bar.textContent = '75%';
      }
      var countMatch = line.match(/<code>(\d+)<\/code>/);
      var pathMatch = line.match(/in <code>([^<]+)<\/code>/);
      if (pathMatch) {
        window.missingFileData = {
          path: pathMatch[1],
          count: countMatch ? countMatch[1] : '0',
          summary: cleanLine
        };
      }
    }

    if (line.includes('Download missing list:') && line.includes('<a href=')) {
      var linkMatch = line.match(/<a href='([^']+)'[^>]*>([^<]+)<\/a>/);
      if (linkMatch && window.missingFileData) {
        window.missingFileData.staticUrl = linkMatch[1];
      }
    }
  }

  function _parseEnhanceDir(line, bar, txt) {
    if (line.includes('Processing') || line.includes('Enhancing')) {
      if (txt) txt.textContent = line;
    }
    if (line.includes('Enhanced') && line.includes('/')) {
      var m = line.match(/(\d+)\/(\d+)/);
      if (m) {
        var current = parseInt(m[1]), total = parseInt(m[2]);
        var pct = Math.round((current / total) * 100);
        if (bar) {
          bar.style.width = pct + '%';
          bar.textContent = pct + '% (' + current + '/' + total + ')';
          bar.setAttribute('aria-valuenow', String(pct));
        }
      }
    }
    if (line.includes('complete') || line.includes('Complete')) {
      if (bar) {
        bar.style.width = '100%';
        bar.textContent = '100%';
        bar.setAttribute('aria-valuenow', '100');
      }
      if (txt) txt.textContent = 'Image enhancement completed!';
    }
  }

})();
