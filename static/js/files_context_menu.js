// ===========================
// Multi-file Context Menu Functions
// ===========================

let contextMenuPanel = null;

// Update the selection badge
function updateSelectionBadge() {
  const badge = document.getElementById('selectionBadge');
  const countSpan = document.getElementById('selectionCount');

  if (!badge || !countSpan) return;

  const count = selectedFiles.size;

  if (count > 0) {
    countSpan.textContent = `${count} selected`;
    badge.classList.add('show');
  } else {
    badge.classList.remove('show');
  }
}

// Show context menu for multi-file selection
function showFileContextMenu(event, panel) {
  const menu = document.getElementById('fileContextMenu');
  contextMenuPanel = panel;

  // Position the menu at the cursor
  menu.style.display = 'block';
  menu.style.left = event.pageX + 'px';
  menu.style.top = event.pageY + 'px';

  // Hide menu when clicking elsewhere
  setTimeout(() => {
    document.addEventListener('click', hideFileContextMenu);
  }, 10);
}

// Hide context menu
function hideFileContextMenu() {
  const menu = document.getElementById('fileContextMenu');
  menu.style.display = 'none';
  document.removeEventListener('click', hideFileContextMenu);
}

// Extract series name from filename
function extractSeriesName(filename) {
  // Remove file extension
  let name = filename.replace(/\.(cbz|cbr|pdf)$/i, '');

  // Remove common patterns: issue numbers, years, volume numbers
  // Pattern: Series Name 001 (2023) or Series Name #1 or Series Name v1 001
  name = name.replace(/\s+#?\d+\s*\(\d{4}\).*$/i, '');  // Remove " 001 (2023)" and everything after
  name = name.replace(/\s+#?\d+.*$/i, '');               // Remove " 001" or " #1" and everything after
  name = name.replace(/\s+v\d+.*$/i, '');                // Remove " v1" and everything after
  name = name.replace(/\s+\(\d{4}\).*$/i, '');           // Remove " (2023)" and everything after

  return name.trim();
}

// Get the most common series name from selected files
// Returns an object with { seriesName, hasMultipleSeries, uniqueSeriesCount }
function getMostCommonSeriesName(filePaths) {
  const seriesCount = {};

  filePaths.forEach(path => {
    const filename = path.split('/').pop();
    const seriesName = extractSeriesName(filename);

    if (seriesName) {
      seriesCount[seriesName] = (seriesCount[seriesName] || 0) + 1;
    }
  });

  // Find the most common series name
  let maxCount = 0;
  let mostCommon = null;
  const uniqueSeriesCount = Object.keys(seriesCount).length;

  for (const [series, count] of Object.entries(seriesCount)) {
    if (count > maxCount) {
      maxCount = count;
      mostCommon = series;
    }
  }

  return {
    seriesName: mostCommon,
    hasMultipleSeries: uniqueSeriesCount > 1,
    uniqueSeriesCount: uniqueSeriesCount,
    seriesCount: seriesCount
  };
}

// Create folder with selected files
function createFolderWithSelection() {
  hideFileContextMenu();

  if (selectedFiles.size === 0) {
    alert('No files selected');
    return;
  }

  const filePaths = Array.from(selectedFiles);
  const seriesInfo = getMostCommonSeriesName(filePaths);

  if (!seriesInfo.seriesName) {
    alert('Could not determine series name from selected files');
    return;
  }

  // If there are multiple different series, prompt the user for a folder name
  if (seriesInfo.hasMultipleSeries) {
    showFolderNamePrompt(filePaths, seriesInfo);
    return;
  }

  // Single series - proceed with automatic folder creation
  const seriesName = seriesInfo.seriesName;
  proceedWithFolderCreation(filePaths, seriesName);
}

// Show the folder name prompt modal
function showFolderNamePrompt(filePaths, seriesInfo) {
  const modal = new bootstrap.Modal(document.getElementById('folderNamePromptModal'));

  // Update the message
  const message = `Selected files contain ${seriesInfo.uniqueSeriesCount} different series. Please enter a folder name.`;
  document.getElementById('folderPromptMessage').textContent = message;

  // Populate the file list
  const fileListElement = document.getElementById('selectedFilesListItems');
  fileListElement.innerHTML = '';

  filePaths.forEach(path => {
    const filename = path.split('/').pop();
    const li = document.createElement('li');
    li.className = 'list-group-item';
    li.textContent = filename;
    fileListElement.appendChild(li);
  });

  // Clear previous input
  document.getElementById('customFolderName').value = '';

  // Show modal
  modal.show();

  // Handle Enter key in input
  const inputElement = document.getElementById('customFolderName');
  inputElement.onkeypress = function(e) {
    if (e.key === 'Enter') {
      document.getElementById('confirmCustomFolderBtn').click();
    }
  };

  // Focus the input
  document.getElementById('folderNamePromptModal').addEventListener('shown.bs.modal', function () {
    inputElement.focus();
  }, { once: true });
}

// Proceed with folder creation using the specified folder name
function proceedWithFolderCreation(filePaths, folderName) {
  // Get the directory of the first selected file
  const firstFilePath = filePaths[0];
  const parentDir = firstFilePath.substring(0, firstFilePath.lastIndexOf('/'));
  const newFolderPath = `${parentDir}/${folderName}`;

  // Create the folder
  fetch('/create-folder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: newFolderPath })
  })
  .then(response => response.json())
  .then(result => {
    if (result.success) {
      // Move all selected files to the new folder
      moveMultipleItems(filePaths, newFolderPath, contextMenuPanel);
      selectedFiles.clear();
      updateSelectionBadge();

      // Refresh the view
      if (contextMenuPanel === 'source') {
        loadDirectories(currentSourcePath, 'source');
      } else {
        loadDirectories(currentDestinationPath, 'destination');
      }
    } else {
      // If folder already exists, just move the files
      if (result.error && result.error.includes('exists')) {
        moveMultipleItems(filePaths, newFolderPath, contextMenuPanel);
        selectedFiles.clear();
        updateSelectionBadge();

        // Refresh the view
        if (contextMenuPanel === 'source') {
          loadDirectories(currentSourcePath, 'source');
        } else {
          loadDirectories(currentDestinationPath, 'destination');
        }
      } else {
        alert('Error creating folder: ' + result.error);
      }
    }
  })
  .catch(error => {
    console.error('Error creating folder:', error);
    alert('Error creating folder: ' + error.message);
  });
}

// Show the Combine Files modal
function showCombineFilesModal() {
  hideFileContextMenu();

  // Filter to only CBZ files
  const cbzFiles = Array.from(selectedFiles).filter(f => f.toLowerCase().endsWith('.cbz'));

  if (cbzFiles.length < 2) {
    showToast('Error', 'Please select at least 2 CBZ files to combine', 'error');
    return;
  }

  // Suggest name from first file's series
  const firstName = cbzFiles[0].split(/[/\\]/).pop();
  const suggestedName = extractSeriesName(firstName) || 'Combined';
  document.getElementById('combinedFileName').value = suggestedName;

  // Show file list
  const filesList = document.getElementById('combineFilesList');
  filesList.innerHTML = '<strong>Files to combine:</strong><br>' +
    cbzFiles.map(f => `• ${f.split(/[/\\]/).pop()}`).join('<br>');

  const modal = new bootstrap.Modal(document.getElementById('combineFilesModal'));
  modal.show();
}

// Combine selected CBZ files into a single file
function combineSelectedFiles() {
  const fileName = document.getElementById('combinedFileName').value.trim();

  if (!fileName) {
    showToast('Error', 'Please enter a filename', 'error');
    return;
  }

  const cbzFiles = Array.from(selectedFiles).filter(f => f.toLowerCase().endsWith('.cbz'));

  if (cbzFiles.length < 2) {
    showToast('Error', 'Need at least 2 CBZ files', 'error');
    return;
  }

  // Get directory - handle both forward and backslash
  const firstFile = cbzFiles[0];
  const lastSlash = Math.max(firstFile.lastIndexOf('/'), firstFile.lastIndexOf('\\'));
  const directory = firstFile.substring(0, lastSlash);

  console.log('Combining files:', { cbzFiles, directory, fileName });

  // Hide modal
  const modalEl = document.getElementById('combineFilesModal');
  const modal = bootstrap.Modal.getInstance(modalEl);
  if (modal) modal.hide();

  // Show progress toast
  showToast('Processing', 'Combining files...', 'info');

  fetch('/api/combine-cbz', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      files: cbzFiles,
      output_name: fileName,
      directory: directory
    })
  })
  .then(response => {
    if (!response.ok) {
      return response.text().then(text => {
        throw new Error(`Server error ${response.status}: ${text}`);
      });
    }
    return response.json();
  })
  .then(data => {
    if (data.success) {
      showToast('Success', `Created ${data.output_file}`, 'success');
      selectedFiles.clear();
      updateSelectionBadge();

      // Refresh the directory
      if (contextMenuPanel === 'source') {
        loadDirectories(currentSourcePath, 'source');
      } else {
        loadDirectories(currentDestinationPath, 'destination');
      }
    } else {
      showToast('Error', data.error || 'Failed to combine files', 'error');
    }
  })
  .catch(error => {
    console.error('Error combining files:', error);
    showToast('Error', error.message || 'An error occurred while combining files', 'error');
  });
}

// Show remove XML confirmation modal
function showRemoveXmlConfirmation() {
  hideFileContextMenu();

  const cbzFiles = Array.from(selectedFiles).filter(f => f.toLowerCase().endsWith('.cbz'));

  if (cbzFiles.length === 0) {
    showToast('Error', 'No CBZ files selected', 'error');
    return;
  }

  document.getElementById('removeXmlCount').textContent = cbzFiles.length;

  const fileList = document.getElementById('removeXmlFileList');
  fileList.innerHTML = '';

  cbzFiles.forEach(path => {
    const li = document.createElement('li');
    li.className = 'list-group-item';
    li.textContent = path.split('/').pop();
    fileList.appendChild(li);
  });

  const modal = new bootstrap.Modal(document.getElementById('removeXmlModal'));
  modal.show();
}

// Remove XML from selected CBZ files
function removeXmlFromSelected() {
  const cbzFiles = Array.from(selectedFiles).filter(f => f.toLowerCase().endsWith('.cbz'));

  const modal = bootstrap.Modal.getInstance(document.getElementById('removeXmlModal'));
  if (modal) modal.hide();

  fetch('/cbz-bulk-clear-comicinfo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths: cbzFiles })
  })
  .then(r => r.json())
  .then(data => {
    if (data.success) {
      showToast('Processing', `Removing XML from ${data.total} file(s)...`, 'info');
      selectedFiles.clear();
      updateSelectionBadge();
    } else {
      showToast('Error', data.error || 'Failed to remove XML', 'error');
    }
  })
  .catch(err => {
    showToast('Error', err.message || 'An error occurred', 'error');
  });
}

// Show delete confirmation modal for multiple files
function showDeleteMultipleConfirmation() {
  hideFileContextMenu();

  if (selectedFiles.size === 0) {
    alert('No files selected');
    return;
  }

  const filePaths = Array.from(selectedFiles);
  const fileNames = filePaths.map(path => path.split('/').pop());

  // Update modal content
  document.getElementById('deleteMultipleCount').textContent = filePaths.length;

  const fileList = document.getElementById('deleteMultipleFileList');
  fileList.innerHTML = '';

  fileNames.forEach(name => {
    const li = document.createElement('li');
    li.className = 'list-group-item';
    li.textContent = name;
    fileList.appendChild(li);
  });

  // Show modal
  const modal = new bootstrap.Modal(document.getElementById('deleteMultipleModal'));
  modal.show();
}

// Delete multiple selected files
function deleteMultipleFiles() {
  const filePaths = Array.from(selectedFiles);

  // Close the modal
  const modal = bootstrap.Modal.getInstance(document.getElementById('deleteMultipleModal'));
  if (modal) modal.hide();

  // Single bulk request instead of N individual requests
  fetch('/api/delete-multiple', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ targets: filePaths })
  })
  .then(response => response.json())
  .then(data => {
    if (!data.results) {
      console.error('Unexpected response:', data);
      return;
    }

    const container = contextMenuPanel === 'source'
      ? document.getElementById('source-list')
      : document.getElementById('destination-list');

    // Remove successfully deleted items from UI
    data.results.forEach(result => {
      if (result.success) {
        const item = container.querySelector(`li[data-fullpath="${result.path}"]`);
        if (item) {
          item.classList.add('deleting');
          setTimeout(() => item.remove(), 200);
        }
      }
    });

    const failures = data.results.filter(r => !r.success);

    selectedFiles.clear();
    updateSelectionBadge();
    document.querySelectorAll('li.list-group-item.selected').forEach(item => {
      item.classList.remove('selected');
      item.removeAttribute('data-selection-hint');
    });

    if (failures.length > 0) {
      alert(`${failures.length} file(s) failed to delete. Check console for details.`);
      console.error('Failed deletions:', failures);
    }

    // Refresh the view
    if (contextMenuPanel === 'source') {
      loadDirectories(currentSourcePath, 'source');
    } else {
      loadDirectories(currentDestinationPath, 'destination');
    }
  })
  .catch(error => {
    console.error('Error deleting files:', error);
    alert('Error deleting files: ' + error.message);
  });
}

// Initialize context menu event listeners
document.addEventListener('DOMContentLoaded', function() {
  const contextCreateFolder = document.getElementById('contextCreateFolder');
  const contextDeleteFiles = document.getElementById('contextDeleteFiles');
  const confirmDeleteMultipleBtn = document.getElementById('confirmDeleteMultipleBtn');
  const confirmCustomFolderBtn = document.getElementById('confirmCustomFolderBtn');

  if (contextCreateFolder) {
    contextCreateFolder.addEventListener('click', function(e) {
      e.preventDefault();
      createFolderWithSelection();
    });
  }

  if (contextDeleteFiles) {
    contextDeleteFiles.addEventListener('click', function(e) {
      e.preventDefault();
      showDeleteMultipleConfirmation();
    });
  }

  if (confirmDeleteMultipleBtn) {
    confirmDeleteMultipleBtn.addEventListener('click', function() {
      deleteMultipleFiles();
    });
  }

  if (confirmCustomFolderBtn) {
    confirmCustomFolderBtn.addEventListener('click', function() {
      const folderName = document.getElementById('customFolderName').value.trim();

      if (!folderName) {
        alert('Please enter a folder name');
        return;
      }

      // Close the modal
      const modal = bootstrap.Modal.getInstance(document.getElementById('folderNamePromptModal'));
      if (modal) modal.hide();

      // Get the selected files from the global variable
      const filePaths = Array.from(selectedFiles);

      // Proceed with folder creation
      proceedWithFolderCreation(filePaths, folderName);
    });
  }

  // Remove XML handlers
  const contextRemoveXml = document.getElementById('contextRemoveXml');
  const confirmRemoveXmlBtn = document.getElementById('confirmRemoveXmlBtn');

  if (contextRemoveXml) {
    contextRemoveXml.addEventListener('click', function(e) {
      e.preventDefault();
      showRemoveXmlConfirmation();
    });
  }

  if (confirmRemoveXmlBtn) {
    confirmRemoveXmlBtn.addEventListener('click', function() {
      removeXmlFromSelected();
    });
  }

  // Remove XML from Directory confirm handler
  const confirmRemoveXmlDirBtn = document.getElementById('confirmRemoveXmlDirBtn');
  if (confirmRemoveXmlDirBtn) {
    confirmRemoveXmlDirBtn.addEventListener('click', function() {
      const directoryPath = this.dataset.directory;

      const modalEl = document.getElementById('removeXmlDirModal');
      const modal = bootstrap.Modal.getInstance(modalEl);
      if (modal) modal.hide();

      fetch('/cbz-bulk-clear-comicinfo', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory: directoryPath })
      })
      .then(r => r.json())
      .then(data => {
        if (data.success) {
          showToast('Processing', `Removing XML from ${data.total} file(s)...`, 'info');
        } else {
          showToast('Error', data.error || 'Failed to remove XML', 'error');
        }
      })
      .catch(err => {
        showToast('Error', err.message || 'An error occurred', 'error');
      });
    });
  }

  // Combine Files handlers
  const contextCombineFiles = document.getElementById('contextCombineFiles');
  const confirmCombineFilesBtn = document.getElementById('confirmCombineFilesBtn');

  if (contextCombineFiles) {
    contextCombineFiles.addEventListener('click', function(e) {
      e.preventDefault();
      showCombineFilesModal();
    });
  }

  if (confirmCombineFilesBtn) {
    confirmCombineFilesBtn.addEventListener('click', function() {
      combineSelectedFiles();
    });
  }
});
