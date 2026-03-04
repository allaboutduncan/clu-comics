// Global variables to track current navigation paths.
// Initialized empty - will be set by loadLibraryDropdowns on page load
let currentSourcePath = '';
let currentDestinationPath = '';
// (deleteTarget/deletePanel removed – delete handled by clu-delete.js)
// Global variable to hold selected file paths.
let selectedFiles = new Set();
// Global variable to track the last clicked file element (for SHIFT selection).
let lastClickedFile = null;
// Global variables to store series data for sorting in GCD modal
let currentSeriesData = [];
let currentFilePath = '';
let currentFileName = '';
let currentIssueNumber = '';
// Global variables for CBZ info modal navigation
let cbzCurrentDirectory = '';
let cbzCurrentFileList = [];
let cbzCurrentIndex = -1;
let cbzCurrentFilePath = '';
// CBZ Page Preview Viewer State
let cbzViewerPath = null;
let cbzViewerPageCount = 0;
let cbzViewerCurrentPage = 0;
let cbzViewerPreloadedPages = new Set();

// Store raw data for each panel.
let sourceDirectoriesData = null;
let destinationDirectoriesData = null;

// Track current filter (default is 'all') per panel.
let currentFilter = { source: 'all', destination: 'all' };

// Store filter state per path for each panel (for persistence during navigation)
let filterHistory = { source: {}, destination: {} };

// Navigation history for scroll position preservation
let sourceScrollHistory = {};  // { path: scrollTop }
let destinationScrollHistory = {};

// Global variable to track GCD MySQL availability (legacy - kept for backwards compatibility)
let gcdMysqlAvailable = false;

// Global variable to track ComicVine API availability (legacy - kept for backwards compatibility)
let comicVineAvailable = false;

// Global variable to track Metron API availability (legacy - kept for backwards compatibility)
let metronAvailable = false;

// Library-specific provider tracking (new provider architecture)
let sourceLibraryId = null;
let destLibraryId = null;
let sourceLibraryProviders = [];
let destLibraryProviders = [];

// Global variable to store current folder path for XML update
// Global variable to store current file path for editing
let currentEditFilePath = null;

// Format file size helper function
function formatSize(bytes) {
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  if (bytes === 0) return '0 B';
  const i = parseInt(Math.floor(Math.log(bytes) / Math.log(1024)));
  return (bytes / Math.pow(1024, i)).toFixed(1) + ' ' + sizes[i];
}

// ==========================================
// CBZ Page Preview Viewer (delegated to clu-cbz-info.js)
// ==========================================

// encodeFilePathForReader kept here – also used by reader.js bridge
function encodeFilePathForReader(path) {
  const cleanPath = path.startsWith('/') ? path.substring(1) : path;
  return cleanPath.split('/').map(c => encodeURIComponent(c)).join('/');
}

// Function to check GCD MySQL availability
function checkGCDAvailability() {
  fetch('/gcd-mysql-status')
    .then(response => response.json())
    .then(data => {
      gcdMysqlAvailable = data.gcd_mysql_available || false;
      console.log('GCD MySQL availability checked:', gcdMysqlAvailable);
    })
    .catch(error => {
      console.warn('Error checking GCD availability:', error);
      gcdMysqlAvailable = false;
    });
}

// Function to check ComicVine API availability
function checkComicVineAvailability() {
  fetch('/config')
    .then(response => response.text())
    .then(html => {
      // Check if ComicVine API key is configured (not empty)
      const parser = new DOMParser();
      const doc = parser.parseFromString(html, 'text/html');
      const apiKeyInput = doc.getElementById('comicvineApiKey');
      comicVineAvailable = apiKeyInput && apiKeyInput.value && apiKeyInput.value.trim().length > 0;
      console.log('ComicVine API availability checked:', comicVineAvailable);
    })
    .catch(error => {
      console.warn('Error checking ComicVine availability:', error);
      comicVineAvailable = false;
    });
}

// Function to check Metron API availability
function checkMetronAvailability() {
  fetch('/config')
    .then(response => response.text())
    .then(html => {
      // Check if Metron password is configured (not empty)
      const parser = new DOMParser();
      const doc = parser.parseFromString(html, 'text/html');
      const passwordInput = doc.getElementById('metronPassword');
      metronAvailable = passwordInput && passwordInput.value && passwordInput.value.trim().length > 0;
      console.log('Metron API availability checked:', metronAvailable);
    })
    .catch(error => {
      console.warn('Error checking Metron availability:', error);
      metronAvailable = false;
    });
}

// ==========================================
// Library Provider Functions (New Provider Architecture)
// ==========================================

// Fetch providers configured for a specific library
async function fetchLibraryProviders(libraryId) {
  if (!libraryId) return [];
  try {
    const response = await fetch(`/api/libraries/${libraryId}/providers`);
    const data = await response.json();
    if (data.success && data.providers) {
      // Filter to enabled providers, sorted by priority
      const providers = data.providers
        .filter(p => p.enabled)
        .sort((a, b) => a.priority - b.priority);
      console.log(`Loaded ${providers.length} providers for library ${libraryId}:`, providers.map(p => p.provider_type));
      return providers;
    }
  } catch (e) {
    console.error('Failed to fetch library providers:', e);
  }
  return [];
}

// Get providers for a specific panel
function getProvidersForPanel(panel) {
  return panel === 'source' ? sourceLibraryProviders : destLibraryProviders;
}

// Get library ID for a specific panel
function getLibraryIdForPanel(panel) {
  return panel === 'source' ? sourceLibraryId : destLibraryId;
}

// Check if a specific provider is available for the panel
function hasProvider(panel, providerType) {
  const providers = getProvidersForPanel(panel);
  return providers.some(p => p.provider_type === providerType);
}

// Helper: refresh the correct panel based on file path
function refreshPanelForPath(filePath) {
  if (currentSourcePath && filePath.startsWith(currentSourcePath)) {
    loadDirectories(currentSourcePath, 'source');
  } else if (currentDestinationPath && filePath.startsWith(currentDestinationPath)) {
    loadDirectories(currentDestinationPath, 'destination');
  } else {
    // Fallback: refresh both panels
    loadDirectories(currentSourcePath, 'source');
    loadDirectories(currentDestinationPath, 'destination');
  }
}

// Set up metadata contract for files page and call CLU.searchMetadata
function searchMetadataForFile(filePath, fileName, panel) {
  window._cluMetadata = {
    getLibraryId: function () { return getLibraryIdForPanel(panel); },
    onMetadataFound: function (fp, data) {
      if (data.moved && data.new_file_path) {
        removeFileFromUI(fp);
      }
      if (data.rename_config && data.rename_config.enabled && data.metadata) {
        var actualPath = data.moved && data.new_file_path ? data.new_file_path : fp;
        promptRenameAfterMetadata(actualPath, fileName, data.metadata, data.rename_config);
      }
      refreshPanelForPath(fp);
    },
    onBatchComplete: function (dp) { refreshPanelForPath(dp); }
  };
  CLU.searchMetadata(filePath, fileName);
}

// Set up metadata contract for directory batch and call CLU.fetchDirectoryMetadata
function fetchDirectoryMetadataForPanel(dirPath, dirName, panel) {
  window._cluMetadata = {
    getLibraryId: function () { return getLibraryIdForPanel(panel); },
    onMetadataFound: function (fp, data) {
      refreshPanelForPath(fp);
    },
    onBatchComplete: function (dp) { refreshPanelForPath(dp); }
  };
  CLU.fetchDirectoryMetadata(dirPath, dirName);
}

// Helper function to create drop target item
function createDropTargetItem(container, currentPath, panel) {
  let dropTargetItem = document.createElement("li");
  dropTargetItem.className = "list-group-item text-center drop-target-item";
  dropTargetItem.textContent = "... Drop Files Here";

  dropTargetItem.addEventListener("dragover", function (e) {
    e.preventDefault();
    e.stopPropagation();
    dropTargetItem.classList.add("folder-hover");
  });
  dropTargetItem.addEventListener("dragleave", function (e) {
    e.stopPropagation();
    dropTargetItem.classList.remove("folder-hover");
  });
  dropTargetItem.addEventListener("drop", function (e) {
    e.preventDefault();
    e.stopPropagation();
    dropTargetItem.classList.remove("folder-hover");
    clearAllDropHoverStates();
    let dataStr = e.dataTransfer.getData("text/plain");
    let items;
    try {
      items = JSON.parse(dataStr);
      if (!Array.isArray(items)) {
        items = [items];
      }
    } catch (err) {
      items = [{ path: dataStr, type: "unknown" }];
    }
    let paths = items.map(item => item.path);
    moveMultipleItems(paths, currentPath, panel);
    selectedFiles.clear();
    if (typeof updateSelectionBadge === 'function') updateSelectionBadge();
  });

  container.appendChild(dropTargetItem);
}

// Normalize file object (handles both {name, size} or string)
function normalizeFile(file) {
  if (typeof file === 'object' && file.name) return file;
  return { name: file, size: null };
}

// Function to send a rename request.
function renameItem(oldPath, newName, panel) {
  if (typeof oldPath !== "string" || typeof newName !== "string") {
    console.error("Invalid oldPath or newName:", { oldPath, newName });
    alert("Rename failed: Internal path error (non-string input)");
    return;
  }

  const trimmedName = newName.trim();
  if (!trimmedName) {
    alert("Filename cannot be empty.");
    return;
  }

  let pathParts = oldPath.split('/');
  pathParts[pathParts.length - 1] = trimmedName;
  const newPath = pathParts.join('/');

  console.log("renameItem called:");
  console.log("  oldPath:", oldPath);
  console.log("  newName:", newName);
  console.log("  newPath:", newPath);

  fetch('/rename', {
    method: 'POST',
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ old: oldPath, new: newPath })
  })
    .then(response => response.json())
    .then(result => {
      if (result.success) {
        if (panel === 'source') {
          loadDirectories(currentSourcePath, 'source');
        } else {
          loadDirectories(currentDestinationPath, 'destination');
        }
      } else {
        alert("Error renaming item: " + result.error);
      }
    })
    .catch(error => {
      console.error("Error in rename request:", error);
      alert("Rename failed due to a network or server error.");
    });
}


// Function to create a list item with edit and delete functionality.
function createListItem(itemName, fullPath, type, panel, isDraggable) {
  let li = document.createElement("li");
  li.className = "list-group-item d-flex align-items-center justify-content-between";
  li.dataset.fullpath = fullPath;

  let fileData = typeof itemName === "object" ? itemName : { name: itemName, size: null };

  let leftContainer = document.createElement("div");
  leftContainer.className = "d-flex align-items-center";

  // Create icon container early to avoid undefined reference
  let iconContainer = document.createElement("div");
  iconContainer.className = "btn-group";
  iconContainer.setAttribute("role", "group");
  iconContainer.setAttribute("aria-label", "File actions");

  if (fileData.name.toLowerCase() !== "parent") {
    let icon = document.createElement("i");
    icon.className = (type === "directory") ? "bi bi-folder me-2" : "bi bi-file-earmark-zip me-2";
    if (type === "directory") icon.style.color = "#bf9300";
    leftContainer.appendChild(icon);

    // Track file additions for rename button visibility (only actual files, not directories)
    console.log(`createListItem: type=${type}, name=${fileData.name}, panel=${panel}`);
    if (type === "file") {
      trackFileForRename(panel);
    }
  }

  let nameSpan = document.createElement("span");
  if (type === "file" && fileData.size != null) {
    nameSpan.innerHTML = `${fileData.name} <span class="text-info-emphasis small ms-2">(${formatSize(fileData.size)})</span>`;
  } else {
    nameSpan.textContent = fileData.name;
  }
  leftContainer.appendChild(nameSpan);

  console.log('Checking CBZ condition:', {
    type: type,
    filename: fileData.name,
    lowercaseEnds: fileData.name.toLowerCase().endsWith('.cbz'),
    lowercase: fileData.name.toLowerCase()
  });

  // Add CBZ info functionality
  if (
    type === "file" &&
    ['.cbz', '.zip'].some(ext => fileData.name.toLowerCase().endsWith(ext))
  ) {
    console.log('Creating CBZ buttons for:', fileData.name);

    // Add info button for detailed CBZ information
    const infoBtn = document.createElement("button");
    infoBtn.className = "btn btn-sm btn-outline-info";
    infoBtn.innerHTML = '<i class="bi bi-eye"></i>';
    infoBtn.title = "CBZ Information";
    infoBtn.setAttribute("type", "button");
    infoBtn.onclick = function (e) {
      e.stopPropagation();
      // Get the current directory's CBZ file list
      const directoryPath = fullPath.substring(0, fullPath.lastIndexOf('/'));
      const cbzFiles = (panel === 'source' ? sourceDirectoriesData : destinationDirectoriesData)
        .files
        .filter(f => {
          const fileName = typeof f === 'object' ? f.name : f;
          return fileName.toLowerCase().endsWith('.cbz') || fileData.name.toLowerCase().endsWith('.zip') || fileName.toLowerCase().endsWith('.cbr');
        })
        .map(f => typeof f === 'object' ? f.name : f)
        .sort();

      showCBZInfo(fullPath, fileData.name, directoryPath, cbzFiles);
    };
    iconContainer.appendChild(infoBtn);
    console.log('Info button added');

    // Get providers configured for this library
    const providers = getProvidersForPanel(panel);
    const hasGCD = providers.some(p => p.provider_type === 'gcd');
    const hasAnyProvider = providers.length > 0;

    // Add cloud-download button for cascade metadata search (if any providers configured)
    if (hasAnyProvider) {
      const metadataBtn = document.createElement("button");
      metadataBtn.className = "btn btn-sm btn-outline-success";
      metadataBtn.innerHTML = '<i class="bi bi-cloud-download"></i>';
      metadataBtn.title = "Fetch Metadata (search providers by priority)";
      metadataBtn.setAttribute("type", "button");
      metadataBtn.onclick = function (e) {
        e.stopPropagation();
        searchMetadataForFile(fullPath, fileData.name, panel);
      };
      iconContainer.appendChild(metadataBtn);
    }

    // Add GCD-specific button (database-down icon) if GCD is available
    if (hasGCD) {
      const gcdBtn = document.createElement("button");
      gcdBtn.className = "btn btn-sm btn-outline-info";
      gcdBtn.innerHTML = '<i class="bi bi-database-down"></i>';
      gcdBtn.title = "Search GCD Database Only";
      gcdBtn.setAttribute("type", "button");
      gcdBtn.onclick = function (e) {
        e.stopPropagation();
        searchGCDMetadata(fullPath, fileData.name);
      };
      iconContainer.appendChild(gcdBtn);
      console.log('GCD-specific button added');
    }

    // Fallback: if no providers but legacy availability flags are set, show legacy buttons
    if (!hasAnyProvider && (gcdMysqlAvailable || comicVineAvailable)) {
      if (gcdMysqlAvailable) {
        const gcdBtn = document.createElement("button");
        gcdBtn.className = "btn btn-sm btn-outline-success";
        gcdBtn.innerHTML = '<i class="bi bi-cloud-download"></i>';
        gcdBtn.title = "Search GCD for Metadata (legacy)";
        gcdBtn.setAttribute("type", "button");
        gcdBtn.onclick = function (e) {
          e.stopPropagation();
          searchGCDMetadata(fullPath, fileData.name);
        };
        iconContainer.appendChild(gcdBtn);
      }
    }
  }

  if (type === "directory") {
    const infoWrapper = document.createElement("span");
    infoWrapper.className = "me-2";

    const infoIcon = document.createElement("button");
    infoIcon.className = "btn btn-sm btn-outline-info";
    infoIcon.innerHTML = '<i class="bi bi-info-circle"></i>';
    infoIcon.title = "Show folder information";
    infoIcon.setAttribute("type", "button");

    const sizeDisplay = document.createElement("span");
    sizeDisplay.className = "text-info-emphasis small ms-2";

    infoIcon.onclick = function (e) {
      e.stopPropagation();
      infoIcon.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';

      fetch(`/folder-size?path=${encodeURIComponent(fullPath)}`)
        .then(res => res.json())
        .then(data => {
          if (data.size != null) {
            let displayText = formatSize(data.size);
            const parts = [];

            if (data.comic_count && data.comic_count > 0) {
              parts.push(`${data.comic_count} comic${data.comic_count !== 1 ? 's' : ''}`);
            }

            if (data.magazine_count && data.magazine_count > 0) {
              parts.push(`${data.magazine_count} magazine${data.magazine_count !== 1 ? 's' : ''}`);
            }

            if (parts.length > 0) {
              displayText += " – " + parts.join(" – ");
            }

            sizeDisplay.textContent = `(${displayText})`;
          } else {
            sizeDisplay.textContent = "(error)";
          }

          // Remove the icon after success
          infoWrapper.removeChild(infoIcon);
        })
        .catch(err => {
          console.error("Error calculating folder size:", err);
          sizeDisplay.textContent = "(error)";
          infoIcon.innerHTML = '<i class="bi bi-info-circle"></i>'; // restore fallback
        });
    };

    infoWrapper.appendChild(infoIcon);
    infoWrapper.appendChild(sizeDisplay);
    iconContainer.appendChild(infoWrapper);

    // Get providers configured for this library
    const providers = getProvidersForPanel(panel);
    const hasGCD = providers.some(p => p.provider_type === 'gcd');
    const hasAnyProvider = providers.length > 0;

    // Add cloud-download button for directory cascade metadata fetch
    if (fileData.name !== "Parent" && hasAnyProvider) {
      const metadataBtn = document.createElement("button");
      metadataBtn.className = "btn btn-sm btn-outline-success";
      metadataBtn.innerHTML = '<i class="bi bi-cloud-download"></i>';
      metadataBtn.title = "Fetch Metadata for All Comics in Directory";
      metadataBtn.setAttribute("type", "button");
      metadataBtn.onclick = function (e) {
        e.stopPropagation();
        fetchDirectoryMetadataForPanel(fullPath, fileData.name, panel);
      };
      iconContainer.appendChild(metadataBtn);
    }

    // Add GCD-specific button for directory (database-down icon)
    if (fileData.name !== "Parent" && hasGCD) {
      const gcdBtn = document.createElement("button");
      gcdBtn.className = "btn btn-sm btn-outline-info";
      gcdBtn.innerHTML = '<i class="bi bi-database-down"></i>';
      gcdBtn.title = "Fetch GCD Metadata for All Comics";
      gcdBtn.setAttribute("type", "button");
      gcdBtn.onclick = function (e) {
        e.stopPropagation();
        searchGCDMetadataForDirectory(fullPath, fileData.name);
      };
      iconContainer.appendChild(gcdBtn);
    }

    // Fallback: if no providers but legacy availability flags are set, show legacy button
    if (fileData.name !== "Parent" && !hasAnyProvider && (gcdMysqlAvailable || comicVineAvailable || metronAvailable)) {
      const metadataBtn = document.createElement("button");
      metadataBtn.className = "btn btn-sm btn-outline-success";
      metadataBtn.innerHTML = '<i class="bi bi-cloud-download"></i>';
      metadataBtn.title = "Fetch Metadata for All Comics (legacy)";
      metadataBtn.setAttribute("type", "button");
      metadataBtn.onclick = function (e) {
        e.stopPropagation();
        fetchDirectoryMetadataForPanel(fullPath, fileData.name, panel);
      };
      iconContainer.appendChild(metadataBtn);
    }

    // Add rename button for directories (but not for Parent directory)
    if (fileData.name !== "Parent") {
      const renameBtn = document.createElement("button");
      renameBtn.className = "btn btn-sm btn-outline-primary";
      renameBtn.innerHTML = '<i class="bi bi-input-cursor-text"></i>';
      renameBtn.title = "Rename files in this directory";
      renameBtn.setAttribute("type", "button");
      renameBtn.addEventListener("click", function (e) {
        if (e) e.stopPropagation();
        console.log('Rename button clicked for directory:', fullPath);
        // Call the rename_files function from rename.py
        fetch('/rename-directory', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ directory: fullPath })
        })
          .then(response => {
            console.log('Rename response status:', response.status);
            return response.json();
          })
          .then(result => {
            console.log('Rename result:', result);
            if (result.success) {
              // Show success message using the enhanced showToast function
              CLU.showToast('Rename Successful', `Successfully renamed files in ${fileData.name}`, 'success');
              // Refresh the current directory listing
              if (panel === 'source') {
                loadDirectories(currentSourcePath, 'source');
              } else {
                loadDirectories(currentDestinationPath, 'destination');
              }
            } else {
              // Show error message
              if (window.bootstrap && document.getElementById("moveErrorToast")) {
                document.getElementById("moveErrorToastBody").textContent = `RENAME ERROR: ${result.error}`;
                bootstrap.Toast.getOrCreateInstance(document.getElementById("moveErrorToast")).show();
              } else {
                alert(`RENAME ERROR: ${result.error}`);
              }
            }
          })
          .catch(error => {
            console.error("Error calling rename function:", error);
            if (window.bootstrap && document.getElementById("moveErrorToast")) {
              document.getElementById("moveErrorToastBody").textContent = `RENAME ERROR: ${error.message}`;
              bootstrap.Toast.getOrCreateInstance(document.getElementById("moveErrorToast")).show();
            } else {
              alert(`RENAME ERROR: ${error.message}`);
            }
          });
      });
      iconContainer.appendChild(renameBtn);

      // Add three-dot dropdown menu for directory operations
      const dropdownContainer = document.createElement("div");
      dropdownContainer.className = "dropdown d-inline-block";

      const dropdownBtn = document.createElement("button");
      dropdownBtn.className = "btn btn-sm";
      dropdownBtn.setAttribute("type", "button");
      dropdownBtn.setAttribute("data-bs-toggle", "dropdown");
      dropdownBtn.setAttribute("aria-expanded", "false");
      dropdownBtn.innerHTML = '<i class="bi bi-three-dots-vertical"></i>';
      dropdownBtn.onclick = (e) => e.stopPropagation();

      const dropdownMenu = document.createElement("ul");
      dropdownMenu.className = "dropdown-menu";

      // Convert CBR-->CBZ option
      const convertItem = document.createElement("li");
      const convertLink = document.createElement("a");
      convertLink.className = "dropdown-item";
      convertLink.href = "#";
      convertLink.innerHTML = '<i class="bi bi-arrow-repeat me-2"></i>Convert CBR→CBZ';
      convertLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        executeScriptOnDirectory('convert', fullPath, panel);
      };
      convertItem.appendChild(convertLink);
      dropdownMenu.appendChild(convertItem);

      // Rebuild All Files option
      const rebuildItem = document.createElement("li");
      const rebuildLink = document.createElement("a");
      rebuildLink.className = "dropdown-item";
      rebuildLink.href = "#";
      rebuildLink.innerHTML = '<i class="bi bi-hammer me-2"></i>Rebuild All Files';
      rebuildLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        executeScriptOnDirectory('rebuild', fullPath, panel);
      };
      rebuildItem.appendChild(rebuildLink);
      dropdownMenu.appendChild(rebuildItem);

      // PDFs-->CBZ option
      const pdfItem = document.createElement("li");
      const pdfLink = document.createElement("a");
      pdfLink.className = "dropdown-item";
      pdfLink.href = "#";
      pdfLink.innerHTML = '<i class="bi bi-file-earmark-pdf me-2"></i>PDFs→CBZ';
      pdfLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        executeScriptOnDirectory('pdf', fullPath, panel);
      };
      pdfItem.appendChild(pdfLink);
      dropdownMenu.appendChild(pdfItem);

      // Missing File Check option
      const missingItem = document.createElement("li");
      const missingLink = document.createElement("a");
      missingLink.className = "dropdown-item";
      missingLink.href = "#";
      missingLink.innerHTML = '<i class="bi bi-search me-2"></i>Missing File Check';
      missingLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        executeScriptOnDirectory('missing', fullPath, panel);
      };
      missingItem.appendChild(missingLink);
      dropdownMenu.appendChild(missingItem);

      // Update XML option
      const updateXmlItem = document.createElement("li");
      const updateXmlLink = document.createElement("a");
      updateXmlLink.className = "dropdown-item";
      updateXmlLink.href = "#";
      updateXmlLink.innerHTML = '<i class="bi bi-code-slash me-2"></i>Update XML';
      updateXmlLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        CLU.openUpdateXmlModal(fullPath, fileData.name);
      };
      updateXmlItem.appendChild(updateXmlLink);
      dropdownMenu.appendChild(updateXmlItem);

      // Enhance Images option
      const enhanceItem = document.createElement("li");
      const enhanceLink = document.createElement("a");
      enhanceLink.className = "dropdown-item";
      enhanceLink.href = "#";
      enhanceLink.innerHTML = '<i class="bi bi-stars me-2"></i>Enhance Images';
      enhanceLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        executeScriptOnDirectory('enhance_dir', fullPath, panel);
      };
      enhanceItem.appendChild(enhanceLink);
      dropdownMenu.appendChild(enhanceItem);

      // Remove All XML option
      const removeXmlItem = document.createElement("li");
      const removeXmlLink = document.createElement("a");
      removeXmlLink.className = "dropdown-item text-danger";
      removeXmlLink.href = "#";
      removeXmlLink.innerHTML = '<i class="bi bi-eraser me-2"></i>Remove All XML';
      removeXmlLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        bulkRemoveXmlFromDirectory(fullPath, panel);
      };
      removeXmlItem.appendChild(removeXmlLink);
      dropdownMenu.appendChild(removeXmlItem);

      dropdownContainer.appendChild(dropdownBtn);
      dropdownContainer.appendChild(dropdownMenu);
      // Store for later - will be appended after trash button
      li.folderDropdown = dropdownContainer;
    }
  }

  if (fileData.name !== "Parent") {
    let pencil = document.createElement("button");
    pencil.className = "btn btn-sm btn-outline-dark";
    pencil.innerHTML = '<i class="bi bi-pencil"></i>';
    pencil.title = "Edit filename";
    pencil.setAttribute("type", "button");

    pencil.addEventListener("click", e => {
      e.stopPropagation();
      const liElem = e.currentTarget.closest("li");
      liElem.setAttribute("draggable", "false");
      liElem.classList.remove("draggable");
      const oldPath = liElem.dataset.fullpath;
      const nameSpanElem = liElem.querySelector("span");

      const input = document.createElement("input");
      input.type = "text";
      input.className = "form-control form-control-sm edit-input";
      input.value = typeof fileData === "object" ? fileData.name : fileData;
      input.addEventListener("click", ev => ev.stopPropagation());

      input.addEventListener("keypress", ev => {
        if (ev.key === "Enter") {
          const newName = input.value.trim();
          if (!newName) return alert("Filename cannot be empty.");
          liElem.setAttribute("draggable", "true");
          liElem.classList.add("draggable");
          renameItem(oldPath, newName, panel);
        }
      });

      input.addEventListener("blur", () => {
        liElem.setAttribute("draggable", "true");
        liElem.classList.add("draggable");
        liElem.replaceChild(leftContainer, input);
      });

      liElem.replaceChild(input, leftContainer);
      input.focus();
    });

    let trash = document.createElement("button");
    trash.className = "btn btn-sm btn-outline-danger";
    trash.innerHTML = '<i class="bi bi-trash"></i>';
    trash.title = "Delete file";
    trash.setAttribute("type", "button");
    trash.onclick = function (e) {
      e.stopPropagation();
      showDeletePrompt(fullPath, fileData.name, panel);
    };

    iconContainer.appendChild(pencil);
    iconContainer.appendChild(trash);

    // Add folder three-dots menu after trash button (for directories only)
    if (li.folderDropdown) {
      iconContainer.appendChild(li.folderDropdown);
      delete li.folderDropdown; // Clean up
    }

    // Add three-dots menu for CBZ/CBR files (same as collection.html)
    // Only add if this is a CBZ/CBR/ZIP file
    if (
      type === "file" &&
      ['.cbz', '.cbr', '.zip'].some(ext => fileData.name.toLowerCase().endsWith(ext))
    ) {
      const dropdownContainer = document.createElement("div");
      dropdownContainer.className = "dropdown d-inline-block";

      const dropdownBtn = document.createElement("button");
      dropdownBtn.className = "btn btn-sm";
      dropdownBtn.setAttribute("type", "button");
      dropdownBtn.setAttribute("data-bs-toggle", "dropdown");
      dropdownBtn.setAttribute("aria-expanded", "false");
      dropdownBtn.innerHTML = '<i class="bi bi-three-dots-vertical"></i>';
      dropdownBtn.title = "More options";
      dropdownBtn.onclick = (e) => e.stopPropagation();

      const dropdownMenu = document.createElement("ul");
      dropdownMenu.className = "dropdown-menu dropdown-menu-end shadow";

      // Crop Cover option
      const cropItem = document.createElement("li");
      const cropLink = document.createElement("a");
      cropLink.className = "dropdown-item";
      cropLink.href = "#";
      cropLink.textContent = "Crop Cover";
      cropLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        executeScriptOnFile('crop', fullPath, panel);
      };
      cropItem.appendChild(cropLink);
      dropdownMenu.appendChild(cropItem);

      // Remove 1st Image option
      const removeFirstItem = document.createElement("li");
      const removeFirstLink = document.createElement("a");
      removeFirstLink.className = "dropdown-item";
      removeFirstLink.href = "#";
      removeFirstLink.textContent = "Remove 1st Image";
      removeFirstLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        executeScriptOnFile('remove', fullPath, panel);
      };
      removeFirstItem.appendChild(removeFirstLink);
      dropdownMenu.appendChild(removeFirstItem);

      // Edit File option
      const editItem = document.createElement("li");
      const editLink = document.createElement("a");
      editLink.className = "dropdown-item";
      editLink.href = "#";
      editLink.textContent = "Edit File";
      editLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        openEditModal(fullPath);
      };
      editItem.appendChild(editLink);
      dropdownMenu.appendChild(editItem);

      // Rebuild option
      const rebuildItem = document.createElement("li");
      const rebuildLink = document.createElement("a");
      rebuildLink.className = "dropdown-item";
      rebuildLink.href = "#";
      rebuildLink.textContent = "Rebuild";
      rebuildLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        executeScriptOnFile('single_file', fullPath, panel);
      };
      rebuildItem.appendChild(rebuildLink);
      dropdownMenu.appendChild(rebuildItem);

      // Enhance option
      const enhanceItem = document.createElement("li");
      const enhanceLink = document.createElement("a");
      enhanceLink.className = "dropdown-item";
      enhanceLink.href = "#";
      enhanceLink.textContent = "Enhance";
      enhanceLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        executeScriptOnFile('enhance_single', fullPath, panel);
      };
      enhanceItem.appendChild(enhanceLink);
      dropdownMenu.appendChild(enhanceItem);

      // Add Blank to End option
      const addBlankItem = document.createElement("li");
      const addBlankLink = document.createElement("a");
      addBlankLink.className = "dropdown-item";
      addBlankLink.href = "#";
      addBlankLink.textContent = "Add Blank to End";
      addBlankLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        executeScriptOnFile('add', fullPath, panel);
      };
      addBlankItem.appendChild(addBlankLink);
      dropdownMenu.appendChild(addBlankItem);

      dropdownContainer.appendChild(dropdownBtn);
      dropdownContainer.appendChild(dropdownMenu);
      iconContainer.appendChild(dropdownContainer);
    }
  }

  li.appendChild(leftContainer);
  li.appendChild(iconContainer);

  if (type === "file") {
    li.setAttribute("data-fullpath", fullPath);
    li.addEventListener("click", function (e) {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        if (selectedFiles.has(fullPath)) {
          selectedFiles.delete(fullPath);
          li.classList.remove("selected");
          li.removeAttribute("data-selection-hint");
        } else {
          selectedFiles.add(fullPath);
          li.classList.add("selected");
          li.setAttribute("data-selection-hint", "Drag to move • Right-click for options");
        }
        lastClickedFile = li;
      } else if (e.shiftKey) {
        let container = li.parentNode;
        let fileItems = Array.from(container.querySelectorAll("li.list-group-item"))
          .filter(item => item.getAttribute("data-fullpath"));
        if (!lastClickedFile) lastClickedFile = li;
        let startIndex = fileItems.indexOf(lastClickedFile);
        let endIndex = fileItems.indexOf(li);
        if (startIndex === -1) startIndex = 0;
        if (endIndex === -1) endIndex = 0;
        let [minIndex, maxIndex] = startIndex < endIndex ? [startIndex, endIndex] : [endIndex, startIndex];
        for (let i = minIndex; i <= maxIndex; i++) {
          let item = fileItems[i];
          selectedFiles.add(item.getAttribute("data-fullpath"));
          item.classList.add("selected");
          item.setAttribute("data-selection-hint", "Drag to move • Right-click for options");
        }
      } else {
        // If clicking the only selected file, deselect it (toggle off)
        if (selectedFiles.size === 1 && selectedFiles.has(fullPath)) {
          selectedFiles.clear();
          li.classList.remove("selected");
          li.removeAttribute("data-selection-hint");
          lastClickedFile = null;
        } else {
          selectedFiles.clear();
          document.querySelectorAll("li.list-group-item.selected").forEach(item => {
            item.classList.remove("selected");
            item.removeAttribute("data-selection-hint");
          });
          selectedFiles.add(fullPath);
          li.classList.add("selected");
          li.setAttribute("data-selection-hint", "Drag to move • Right-click for options");
          lastClickedFile = li;
        }
      }
      updateSelectionBadge();
      e.stopPropagation();
    });

    li.addEventListener("contextmenu", e => {
      e.preventDefault();
      // Only show context menu if multiple files are selected
      if (selectedFiles.size > 1) {
        showFileContextMenu(e, panel);
      }
    });
  }

  if (type === "directory") {
    // Set data-fullpath for directories so they can be found during deletion
    li.setAttribute("data-fullpath", fullPath);

    li.onclick = function () {
      // Store current filter for current path before navigating
      const currentPath = panel === 'source' ? currentSourcePath : currentDestinationPath;
      if (currentFilter[panel] !== 'all') {
        filterHistory[panel][currentPath] = currentFilter[panel];
      } else {
        // Remove entry if filter is 'all' (default) to minimize memory usage
        delete filterHistory[panel][currentPath];
      }

      currentFilter[panel] = 'all';
      loadDirectories(fullPath, panel);
    };
    if (fileData.name.toLowerCase() !== "parent") {
      li.addEventListener("dragover", e => { e.preventDefault(); li.classList.add("folder-hover"); });
      li.addEventListener("dragleave", e => { li.classList.remove("folder-hover"); });
      li.addEventListener("drop", function (e) {
        e.preventDefault();
        e.stopPropagation();
        li.classList.remove("folder-hover");
        clearAllDropHoverStates();

        let dataStr = e.dataTransfer.getData("text/plain");
        let items;
        try {
          items = JSON.parse(dataStr);
          if (!Array.isArray(items)) items = [items];
        } catch {
          items = [{ path: dataStr, type: "unknown" }];
        }

        let targetDir = fullPath;
        let dedupedPaths = new Set();

        items.forEach(item => {
          let sourcePath = item.path;
          let sourceDir = sourcePath.substring(0, sourcePath.lastIndexOf('/'));
          if (sourceDir !== targetDir && !dedupedPaths.has(sourcePath)) {
            dedupedPaths.add(sourcePath);
          }
        });

        const paths = [...dedupedPaths];
        if (paths.length === 0) return;

        if (paths.length === 1 && items[0].type === "file") {
          moveSingleItem(paths[0], targetDir);
        } else {
          // Pass item types for better progress tracking
          moveMultipleItems(paths, targetDir, panel, items);
        }
        selectedFiles.clear();
        if (typeof updateSelectionBadge === 'function') updateSelectionBadge();
      });
    }
  } else {
    li.onclick = e => e.stopPropagation();
  }

  if (isDraggable) {
    li.classList.add("draggable");
    li.setAttribute("draggable", "true");
    li.addEventListener("dragstart", function (e) {
      if (type === "file") {
        if (selectedFiles.has(fullPath)) {
          e.dataTransfer.setData("text/plain", JSON.stringify([...selectedFiles].map(path => ({ path, type: "file" }))));
          // Set drag image for multiple files
          e.dataTransfer.effectAllowed = "move";

          // Create custom drag image showing count
          const dragCount = selectedFiles.size;
          if (dragCount > 1) {
            const dragImage = document.createElement('div');
            dragImage.className = 'drag-preview';
            dragImage.textContent = `${dragCount} files`;
            dragImage.style.cssText = 'position: absolute; top: -1000px; background: #2196f3; color: white; padding: 0.5rem; border-radius: 0.25rem; font-weight: bold;';
            document.body.appendChild(dragImage);
            e.dataTransfer.setDragImage(dragImage, 50, 25);
            setTimeout(() => document.body.removeChild(dragImage), 0);
          }
        } else {
          selectedFiles.clear();
          document.querySelectorAll("li.list-group-item.selected").forEach(item => {
            item.classList.remove("selected");
            item.removeAttribute("data-selection-hint");
          });
          selectedFiles.add(fullPath);
          li.classList.add("selected");
          li.setAttribute("data-selection-hint", "Drag to move • Right-click for options");
          if (typeof updateSelectionBadge === 'function') updateSelectionBadge();
          e.dataTransfer.setData("text/plain", JSON.stringify([{ path: fullPath, type: "file" }]));
          e.dataTransfer.effectAllowed = "move";
        }
      } else {
        e.dataTransfer.setData("text/plain", JSON.stringify([{ path: fullPath, type: "directory" }]));
        e.dataTransfer.effectAllowed = "move";
      }

      // Add dragging class for visual feedback
      li.classList.add("dragging");
      setTimeout(() => li.classList.remove("dragging"), 50);
    });

    li.addEventListener("dragend", function (e) {
      // Clean up any hover states when drag ends (whether successful or not)
      setTimeout(() => {
        clearAllDropHoverStates();
      }, 100);
    });
  }

  return li;
}

// Function to dynamically build the filter bar.
function updateFilterBar(panel, directories) {
  const outerContainer = document.getElementById(`${panel}-directory-filter`);
  if (!outerContainer) return;
  const btnGroup = outerContainer.querySelector('.btn-group');
  if (!btnGroup) return;

  // Handle undefined or null directories - provide empty array as fallback
  if (!directories) {
    directories = [];
  }
  if (!Array.isArray(directories)) {
    console.warn("directories is not an array in updateFilterBar:", directories);
    directories = [];
  }

  let availableLetters = new Set();
  let hasNonAlpha = false;

  directories.forEach(dir => {
    const firstChar = dir.charAt(0).toUpperCase();
    if (firstChar >= 'A' && firstChar <= 'Z') {
      availableLetters.add(firstChar);
    } else {
      hasNonAlpha = true;
    }
  });

  let buttonsHtml = '';
  buttonsHtml += `<button type="button" class="btn btn-outline-secondary ${currentFilter[panel] === 'all' ? 'active' : ''}" onclick="filterDirectories('all', '${panel}')">All</button>`;

  if (hasNonAlpha) {
    buttonsHtml += `<button type="button" class="btn btn-outline-secondary ${currentFilter[panel] === '#' ? 'active' : ''}" onclick="filterDirectories('#', '${panel}')">#</button>`;
  }

  for (let i = 65; i <= 90; i++) {
    const letter = String.fromCharCode(i);
    if (availableLetters.has(letter)) {
      buttonsHtml += `<button type="button" class="btn btn-outline-secondary ${currentFilter[panel] === letter ? 'active' : ''}" onclick="filterDirectories('${letter}', '${panel}')">${letter}</button>`;
    }
  }
  btnGroup.innerHTML = buttonsHtml;
  // --- SEARCH BOX LOGIC (destination only, >25 dirs) ---
  if (panel === 'destination') {
    const searchRow = document.getElementById('destination-directory-search-row');
    if (directories.length > 25) {
      searchRow.innerHTML = `<input type="text" id="destination-directory-search" class="form-control mb-2" placeholder="Type to filter directories..." oninput="onDestinationDirectorySearch(this.value)">`;
    } else {
      searchRow.innerHTML = '';
    }
  }
}

// Function to restore filter from history if valid for the given path
function restoreFilterIfValid(panel, path, directories) {
  if (!filterHistory[panel][path]) {
    return; // No saved filter for this path
  }

  const savedFilter = filterHistory[panel][path];

  // Build set of available letters from current directories
  const availableLetters = new Set();
  let hasNonAlpha = false;

  if (directories && Array.isArray(directories)) {
    directories.forEach(dir => {
      const firstChar = dir.charAt(0).toUpperCase();
      if (firstChar >= 'A' && firstChar <= 'Z') {
        availableLetters.add(firstChar);
      } else {
        hasNonAlpha = true;
      }
    });
  }

  // Only restore if the saved filter is still valid
  let shouldRestore = false;
  if (savedFilter === '#' && hasNonAlpha) {
    shouldRestore = true;
  } else if (savedFilter !== '#' && savedFilter !== 'all' && availableLetters.has(savedFilter)) {
    shouldRestore = true;
  }

  if (shouldRestore) {
    currentFilter[panel] = savedFilter;
    // Re-run updateFilterBar to reflect the restored filter in button states
    updateFilterBar(panel, directories);
  }
}

// --- SEARCH STATE FOR DESTINATION PANEL ---
let destinationSearchTerm = '';
function onDestinationDirectorySearch(val) {
  destinationSearchTerm = val.trim().toLowerCase();
  if (destinationDirectoriesData) {
    renderDirectoryListing(destinationDirectoriesData, 'destination');
  }
}

// Save scroll position before navigating away
function saveScrollPosition(panel) {
  const container = document.getElementById(panel === 'source' ? 'source-list' : 'destination-list');
  const currentPath = panel === 'source' ? currentSourcePath : currentDestinationPath;
  if (container && currentPath) {
    const history = panel === 'source' ? sourceScrollHistory : destinationScrollHistory;
    history[currentPath] = container.scrollTop;
  }
}

// Restore scroll position after rendering
function restoreScrollPosition(panel, path) {
  const container = document.getElementById(panel === 'source' ? 'source-list' : 'destination-list');
  const history = panel === 'source' ? sourceScrollHistory : destinationScrollHistory;
  if (container && history[path] !== undefined) {
    // Use setTimeout to ensure DOM has rendered
    setTimeout(() => {
      container.scrollTop = history[path];
    }, 0);
  }
}

// Updated loadDirectories function.
function loadDirectories(path, panel) {
  console.log("loadDirectories called with path:", path, "panel:", panel);

  // Route virtual paths to their dedicated loaders
  if (path === 'trash') { loadTrash(panel); return; }
  if (path === 'recent-files') { loadRecentFiles(panel); return; }

  // Save scroll position before loading new content
  saveScrollPosition(panel);

  // Update button states - library dropdown replaces btnDirectories
  const btnDownloads = document.getElementById('btnDownloads');
  if (btnDownloads) btnDownloads.classList.remove('active');
  const btnRecentFiles = document.getElementById('btnRecentFiles');
  if (btnRecentFiles) btnRecentFiles.classList.remove('active');
  const btnTrashLib = document.getElementById('btnTrash');
  if (btnTrashLib) btnTrashLib.classList.remove('active');

  // Show filter bar
  const filterBar = document.getElementById(`${panel}-directory-filter`);
  if (filterBar) {
    filterBar.style.display = 'block';
  }

  window.scrollTo({ top: 0, behavior: "smooth" });
  let container = panel === 'source' ? document.getElementById("source-list")
    : document.getElementById("destination-list");
  if (!container) {
    console.error("Container not found for panel:", panel);
    return;
  }
  container.innerHTML = `<div class="d-flex justify-content-center my-3">
                                <button class="btn btn-primary" type="button" disabled>
                                  <span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>
                                  Loading...
                                </button>
                              </div>`;
  fetch(`/list-directories?path=${encodeURIComponent(path)}`)
    .then(response => response.json())
    .then(data => {
      console.log("Received data for panel", panel, ":", data);

      // Check for server errors
      if (data.error) {
        throw new Error(data.error);
      }

      if (panel === 'source') {
        currentSourcePath = data.current_path;
        // Reset file tracking AFTER updating global path variable
        resetFileTracking(panel, data.current_path);
        updateBreadcrumb('source', data.current_path);
        sourceDirectoriesData = data;
        updateFilterBar('source', data.directories);

        // Restore filter if previously set for this path
        restoreFilterIfValid('source', data.current_path, data.directories);

        renderDirectoryListing(data, 'source');
        // Restore scroll position if navigating back
        restoreScrollPosition('source', data.current_path);
      } else {
        currentDestinationPath = data.current_path;
        // Reset file tracking AFTER updating global path variable
        resetFileTracking(panel, data.current_path);
        updateBreadcrumb('destination', data.current_path);
        destinationDirectoriesData = data;
        // Reset search filter and input on navigation
        destinationSearchTerm = '';
        const searchInput = document.getElementById('destination-directory-search');
        if (searchInput) searchInput.value = '';
        updateFilterBar('destination', data.directories);

        // Restore filter if previously set for this path
        restoreFilterIfValid('destination', data.current_path, data.directories);

        renderDirectoryListing(data, 'destination');
        // Restore scroll position if navigating back
        restoreScrollPosition('destination', data.current_path);
      }
    })
    .catch(error => {
      console.error("Error loading directories:", error);
      container.innerHTML = `<div class="alert alert-danger" role="alert">
                                    Error loading directory.
                                  </div>`;
    });
}

// Function to render the directory listing.
function renderDirectoryListing(data, panel) {
  let container = panel === 'source' ? document.getElementById("source-list")
    : document.getElementById("destination-list");
  container.innerHTML = "";

  if (data.parent) {
    let parentItem = createListItem("Parent", data.parent, "directory", panel, false);
    parentItem.querySelector("span").innerHTML = `<i class="bi bi-arrow-left-square me-2"></i> Parent`;
    // Ensure Parent directory has data-fullpath for consistency
    parentItem.setAttribute("data-fullpath", data.parent);
    container.appendChild(parentItem);
  }

  // Handle undefined or null directories - provide empty array as fallback
  if (!data.directories) {
    data.directories = [];
  }
  if (!Array.isArray(data.directories)) {
    console.warn("data.directories is not an array:", data.directories);
    data.directories = [];
  }

  let filter = currentFilter[panel];
  let directoriesToShow = data.directories.filter(dir => {
    // --- SEARCH FILTER FOR DESTINATION PANEL ---
    if (panel === 'destination' && destinationSearchTerm) {
      if (!dir.toLowerCase().includes(destinationSearchTerm)) return false;
    }
    // --- END SEARCH FILTER ---
    if (filter === 'all') return true;
    if (filter === '#') return !/^[A-Za-z]/.test(dir.charAt(0));
    return dir.charAt(0).toUpperCase() === filter;
  });

  directoriesToShow.forEach(dir => {
    let fullPath = data.current_path + "/" + dir;
    let item = createListItem(dir, fullPath, "directory", panel, true);
    container.appendChild(item);
  });

  if (filter === 'all') {
    // Handle undefined or null files - provide empty array as fallback
    if (!data.files) {
      data.files = [];
    }
    if (!Array.isArray(data.files)) {
      console.warn("data.files is not an array:", data.files);
      data.files = [];
    }

    data.files.forEach(file => {
      const fileData = normalizeFile(file);
      const fullPath = data.current_path + "/" + fileData.name;
      let fileItem = createListItem(fileData, fullPath, "file", panel, true);
      container.appendChild(fileItem);
    });
  }

  // For the destination panel, only add the drop target if the directory is truly empty.
  if (panel === 'destination' &&
    (!data.directories || data.directories.length === 0) &&
    (!data.files || data.files.length === 0)) {
    createDropTargetItem(container, data.current_path, panel);
  }
}

// Function to filter directories based on the selected letter.
function filterDirectories(letter, panel) {
  if (currentFilter[panel] === letter) {
    currentFilter[panel] = 'all';
  } else {
    currentFilter[panel] = letter;
  }
  let filterContainer = document.getElementById(panel + "-directory-filter");
  if (filterContainer) {
    let btnGroup = filterContainer.querySelector('.btn-group');
    if (btnGroup) {
      let buttons = btnGroup.querySelectorAll("button");
      buttons.forEach(btn => {
        let btnText = btn.textContent.trim();
        if ((currentFilter[panel] === 'all' && btnText === 'All') || btnText === currentFilter[panel]) {
          btn.classList.add("active");
        } else {
          btn.classList.remove("active");
        }
      });
    }
  }
  if (panel === 'source' && sourceDirectoriesData) {
    renderDirectoryListing(sourceDirectoriesData, panel);
  } else if (panel === 'destination' && destinationDirectoriesData) {
    renderDirectoryListing(destinationDirectoriesData, panel);
  }
}

// New loadDownloads function to fetch downloads data.
function loadDownloads(path, panel) {
  console.log("loadDownloads called with path:", path, "panel:", panel);
  const btnDownloads = document.getElementById('btnDownloads');
  if (btnDownloads) btnDownloads.classList.add('active');
  const btnRecentFiles = document.getElementById('btnRecentFiles');
  if (btnRecentFiles) btnRecentFiles.classList.remove('active');
  const btnTrashDl = document.getElementById('btnTrash');
  if (btnTrashDl) btnTrashDl.classList.remove('active');

  // Show filter bar
  const filterBar = document.getElementById(`${panel}-directory-filter`);
  if (filterBar) {
    filterBar.style.display = 'block';
  }

  window.scrollTo({ top: 0, behavior: "smooth" });
  let container = panel === 'source' ? document.getElementById("source-list")
    : document.getElementById("destination-list");
  if (!container) {
    console.error("Container not found for panel:", panel);
    return;
  }
  container.innerHTML = `<div class="d-flex justify-content-center my-3">
                                <button class="btn btn-primary" type="button" disabled>
                                  <span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>
                                  Loading...
                                </button>
                              </div>`;
  fetch(`/list-downloads?path=${encodeURIComponent(path)}`)
    .then(response => response.json())
    .then(data => {
      console.log("Received data:", data);
      container.innerHTML = "";

      if (panel === 'source') {
        currentSourcePath = data.current_path;
        // Reset file tracking AFTER updating global path variable
        resetFileTracking(panel, data.current_path);
        updateBreadcrumb('source', data.current_path);
      } else {
        currentDestinationPath = data.current_path;
        // Reset file tracking AFTER updating global path variable
        resetFileTracking(panel, data.current_path);
        updateBreadcrumb('destination', data.current_path);
      }
      if (data.parent) {
        let parentItem = createListItem("Parent", data.parent, "directory", panel, false);
        parentItem.querySelector("span").innerHTML = `<i class="bi bi-arrow-left-square me-2"></i> Parent`;
        // Ensure Parent directory has data-fullpath for consistency
        parentItem.setAttribute("data-fullpath", data.parent);
        container.appendChild(parentItem);
      }
      if (data.directories && Array.isArray(data.directories)) {
        data.directories.forEach(dir => {
          const dirData = normalizeFile(dir);
          const fullPath = data.current_path + "/" + dirData.name;
          const item = createListItem(dirData, fullPath, "directory", panel, true);
          container.appendChild(item);
        });
      }
      if (data.files && Array.isArray(data.files)) {
        data.files.forEach(file => {
          const fileData = normalizeFile(file);
          const fullPath = data.current_path + "/" + fileData.name;
          let fileItem = createListItem(fileData, fullPath, "file", panel, true);
          container.appendChild(fileItem);
        });
      }
    })
    .catch(error => {
      console.error("Error loading downloads:", error);
      container.innerHTML = `<div class="alert alert-danger" role="alert">
                                    Error loading downloads.
                                  </div>`;
    });
}

// Function to load recent files from the file watcher
function loadRecentFiles(panel) {
  console.log("loadRecentFiles called for panel:", panel);

  // Update button states
  const btnRecentFiles = document.getElementById('btnRecentFiles');
  if (btnRecentFiles) btnRecentFiles.classList.add('active');
  const btnDownloads = document.getElementById('btnDownloads');
  if (btnDownloads) btnDownloads.classList.remove('active');
  const btnTrashRf = document.getElementById('btnTrash');
  if (btnTrashRf) btnTrashRf.classList.remove('active');

  // Hide filter bar (not needed for recent files)
  const filterBar = document.getElementById(`${panel}-directory-filter`);
  if (filterBar) {
    filterBar.style.display = 'none';
  }

  // Update breadcrumb to show "Recent Files"
  updateBreadcrumb(panel, 'Recent Files');

  window.scrollTo({ top: 0, behavior: "smooth" });
  let container = panel === 'source' ? document.getElementById("source-list")
    : document.getElementById("destination-list");

  if (!container) {
    console.error("Container not found for panel:", panel);
    return;
  }

  // Show loading spinner
  container.innerHTML = `<div class="d-flex justify-content-center my-3">
                          <button class="btn btn-primary" type="button" disabled>
                            <span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>
                            Loading Recent Files...
                          </button>
                        </div>`;

  // Fetch recent files from the API
  fetch('/list-recent-files?limit=100')
    .then(response => response.json())
    .then(data => {
      console.log("Received recent files data:", data);
      container.innerHTML = "";

      if (panel === 'source') {
        currentSourcePath = 'recent-files';
        // Reset file tracking AFTER updating global path variable
        resetFileTracking(panel, 'recent-files');
      } else {
        currentDestinationPath = 'recent-files';
        // Reset file tracking AFTER updating global path variable
        resetFileTracking(panel, 'recent-files');
      }

      // Display date range if available
      if (data.date_range && data.files.length > 0) {
        const dateInfo = document.createElement('div');
        dateInfo.className = 'alert alert-info mb-3';
        dateInfo.innerHTML = `
          <i class="bi bi-clock-history me-2"></i>
          <strong>Recent Files (${data.total_count})</strong>
          <div class="small mt-1">
            From ${formatDateTime(data.date_range.oldest)} to ${formatDateTime(data.date_range.newest)}
          </div>
        `;
        container.appendChild(dateInfo);
      }

      // Display files
      if (data.files && Array.isArray(data.files) && data.files.length > 0) {
        data.files.forEach(file => {
          // Create custom list item for recent files with enhanced display
          const fileItem = document.createElement('li');
          fileItem.className = 'list-group-item list-group-item-action draggable d-flex align-items-start justify-content-between';
          fileItem.setAttribute('draggable', 'true');
          fileItem.setAttribute('data-fullpath', file.file_path);

          const timeAgo = getTimeAgo(file.added_at);
          const formattedDateTime = formatDateTime(file.added_at);

          // Create left container with file info
          const leftContainer = document.createElement('div');
          leftContainer.className = 'd-flex align-items-start flex-grow-1';
          leftContainer.style.minWidth = '0';
          leftContainer.innerHTML = `
            <i class="bi bi-file-earmark-zip me-2 mt-1"></i>
            <div class="flex-grow-1" style="min-width: 0;">
              <div class="fw-medium">${CLU.escapeHtml(file.file_name)}</div>
              <div class="small text-muted mt-1">
                <i class="bi bi-clock me-1"></i>${CLU.escapeHtml(timeAgo)}
                <span class="ms-2" title="${CLU.escapeHtml(formattedDateTime)}">(${CLU.escapeHtml(formattedDateTime)})</span>
              </div>
              <div class="small text-warning mt-1" style="word-break: break-all;">
                <i class="bi bi-folder me-1"></i>${CLU.escapeHtml(file.file_path)}
              </div>
            </div>
          `;

          // Create button group
          const iconContainer = document.createElement('div');
          iconContainer.className = 'btn-group';
          iconContainer.setAttribute('role', 'group');
          iconContainer.setAttribute('aria-label', 'File actions');

          // Add CBZ info button
          const infoBtn = document.createElement('button');
          infoBtn.className = 'btn btn-sm btn-outline-info';
          infoBtn.innerHTML = '<i class="bi bi-eye"></i>';
          infoBtn.title = 'CBZ Information';
          infoBtn.setAttribute('type', 'button');
          infoBtn.onclick = function (e) {
            e.stopPropagation();
            // Get the directory path
            const directoryPath = file.file_path.substring(0, file.file_path.lastIndexOf('/'));
            // For recent files, we don't have the full directory listing, so pass empty array
            showCBZInfo(file.file_path, file.file_name, directoryPath, []);
          };
          iconContainer.appendChild(infoBtn);

          // Use source panel's providers for recent files (or fall back to legacy)
          const providers = sourceLibraryProviders || [];
          const hasGCD = providers.some(p => p.provider_type === 'gcd');
          const hasAnyProvider = providers.length > 0;

          // Add cascade metadata button (if providers configured)
          if (hasAnyProvider) {
            const metadataBtn = document.createElement('button');
            metadataBtn.className = 'btn btn-sm btn-outline-success';
            metadataBtn.innerHTML = '<i class="bi bi-cloud-download"></i>';
            metadataBtn.title = 'Fetch Metadata (search providers by priority)';
            metadataBtn.setAttribute('type', 'button');
            metadataBtn.onclick = function (e) {
              e.stopPropagation();
              searchMetadataForFile(file.file_path, file.file_name, 'source');
            };
            iconContainer.appendChild(metadataBtn);
          }

          // Add GCD-specific button (if GCD is available)
          if (hasGCD) {
            const gcdBtn = document.createElement('button');
            gcdBtn.className = 'btn btn-sm btn-outline-info';
            gcdBtn.innerHTML = '<i class="bi bi-database-down"></i>';
            gcdBtn.title = 'Search GCD Database Only';
            gcdBtn.setAttribute('type', 'button');
            gcdBtn.onclick = function (e) {
              e.stopPropagation();
              searchGCDMetadata(file.file_path, file.file_name);
            };
            iconContainer.appendChild(gcdBtn);
          }

          // Fallback to legacy buttons if no providers configured
          if (!hasAnyProvider) {
            if (typeof gcdMysqlAvailable !== 'undefined' && gcdMysqlAvailable) {
              const gcdBtn = document.createElement('button');
              gcdBtn.className = 'btn btn-sm btn-outline-success';
              gcdBtn.innerHTML = '<i class="bi bi-cloud-download"></i>';
              gcdBtn.title = 'Search GCD for Metadata (legacy)';
              gcdBtn.setAttribute('type', 'button');
              gcdBtn.onclick = function (e) {
                e.stopPropagation();
                searchGCDMetadata(file.file_path, file.file_name);
              };
              iconContainer.appendChild(gcdBtn);
            }
          }

          // Add edit filename button
          const pencilBtn = document.createElement('button');
          pencilBtn.className = 'btn btn-sm btn-outline-dark';
          pencilBtn.innerHTML = '<i class="bi bi-pencil"></i>';
          pencilBtn.title = 'Edit filename';
          pencilBtn.setAttribute('type', 'button');
          pencilBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            const nameDiv = leftContainer.querySelector('.fw-medium');
            const oldPath = file.file_path;

            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'form-control form-control-sm edit-input';
            input.value = file.file_name;
            input.addEventListener('click', ev => ev.stopPropagation());

            input.addEventListener('keypress', ev => {
              if (ev.key === 'Enter') {
                const newName = input.value.trim();
                if (!newName) return alert('Filename cannot be empty.');
                renameItem(oldPath, newName, panel);
              }
            });

            input.addEventListener('blur', () => {
              nameDiv.innerHTML = CLU.escapeHtml(file.file_name);
            });

            nameDiv.innerHTML = '';
            nameDiv.appendChild(input);
            input.focus();
          });
          iconContainer.appendChild(pencilBtn);

          // Add delete button
          const trashBtn = document.createElement('button');
          trashBtn.className = 'btn btn-sm btn-outline-danger';
          trashBtn.innerHTML = '<i class="bi bi-trash"></i>';
          trashBtn.title = 'Delete file';
          trashBtn.setAttribute('type', 'button');
          trashBtn.onclick = function (e) {
            e.stopPropagation();
            showDeletePrompt(file.file_path, file.file_name, panel);
          };
          iconContainer.appendChild(trashBtn);

          // Append containers to fileItem
          fileItem.appendChild(leftContainer);
          fileItem.appendChild(iconContainer);

          // Add drag start handler
          fileItem.addEventListener("dragstart", function (e) {
            const fullPath = file.file_path;
            if (selectedFiles.has(fullPath)) {
              e.dataTransfer.setData("text/plain", JSON.stringify([...selectedFiles].map(path => ({ path, type: "file" }))));
              e.dataTransfer.effectAllowed = "move";

              // Create custom drag image showing count
              const dragCount = selectedFiles.size;
              if (dragCount > 1) {
                const dragImage = document.createElement('div');
                dragImage.className = 'drag-preview';
                dragImage.textContent = `${dragCount} files`;
                dragImage.style.cssText = 'position: absolute; top: -1000px; background: #2196f3; color: white; padding: 0.5rem; border-radius: 0.25rem; font-weight: bold;';
                document.body.appendChild(dragImage);
                e.dataTransfer.setDragImage(dragImage, 50, 25);
                setTimeout(() => document.body.removeChild(dragImage), 0);
              }
            } else {
              selectedFiles.clear();
              document.querySelectorAll("li.list-group-item.selected").forEach(item => {
                item.classList.remove("selected");
                item.removeAttribute("data-selection-hint");
              });
              selectedFiles.add(fullPath);
              fileItem.classList.add("selected");
              fileItem.setAttribute("data-selection-hint", "Drag to move • Right-click for options");
              if (typeof updateSelectionBadge === 'function') updateSelectionBadge();
              e.dataTransfer.setData("text/plain", JSON.stringify([{ path: fullPath, type: "file" }]));
              e.dataTransfer.effectAllowed = "move";
            }

            fileItem.classList.add("dragging");
            setTimeout(() => fileItem.classList.remove("dragging"), 50);
          });

          // Add drag end handler
          fileItem.addEventListener("dragend", function (e) {
            setTimeout(() => {
              if (typeof clearAllDropHoverStates === 'function') {
                clearAllDropHoverStates();
              }
            }, 100);
          });

          // Add click handler for selection
          fileItem.addEventListener('click', function (e) {
            if (e.ctrlKey || e.metaKey) {
              // Multi-select with Ctrl/Cmd
              const fullPath = file.file_path;
              if (selectedFiles.has(fullPath)) {
                selectedFiles.delete(fullPath);
                fileItem.classList.remove("selected");
                fileItem.removeAttribute("data-selection-hint");
              } else {
                selectedFiles.add(fullPath);
                fileItem.classList.add("selected");
                fileItem.setAttribute("data-selection-hint", "Drag to move • Right-click for options");
              }
              if (typeof updateSelectionBadge === 'function') updateSelectionBadge();
            }
          });

          container.appendChild(fileItem);
        });
      } else {
        // No recent files
        const emptyMsg = document.createElement('div');
        emptyMsg.className = 'alert alert-warning';
        emptyMsg.innerHTML = '<i class="bi bi-inbox me-2"></i>No recent files found. Files added to /data will appear here.';
        container.appendChild(emptyMsg);
      }
    })
    .catch(error => {
      console.error("Error loading recent files:", error);
      container.innerHTML = `<div class="alert alert-danger" role="alert">
                              <i class="bi bi-exclamation-triangle me-2"></i>
                              Error loading recent files: ${error.message}
                            </div>`;
    });
}

// =============================================
// Trash browsing
// =============================================

function updateTrashBadge() {
  fetch('/api/trash/info')
    .then(r => r.json())
    .then(data => {
      const badge = document.getElementById('trashBadge');
      if (!badge) return;
      if (data.enabled && data.item_count > 0) {
        badge.textContent = data.item_count;
        badge.style.display = '';
      } else {
        badge.style.display = 'none';
      }
    })
    .catch(() => {});
}

// Initialize trash badge on page load
document.addEventListener('DOMContentLoaded', updateTrashBadge);

function loadTrash(panel) {
  console.log("loadTrash called for panel:", panel);

  // Update button states
  const btnTrash = document.getElementById('btnTrash');
  if (btnTrash) btnTrash.classList.add('active');
  const btnDownloads = document.getElementById('btnDownloads');
  if (btnDownloads) btnDownloads.classList.remove('active');
  const btnRecentFiles = document.getElementById('btnRecentFiles');
  if (btnRecentFiles) btnRecentFiles.classList.remove('active');

  // Hide filter bar
  const filterBar = document.getElementById(`${panel}-directory-filter`);
  if (filterBar) filterBar.style.display = 'none';

  updateBreadcrumb(panel, 'Trash');

  window.scrollTo({ top: 0, behavior: "smooth" });
  let container = panel === 'source' ? document.getElementById("source-list")
    : document.getElementById("destination-list");

  if (!container) return;

  container.innerHTML = `<div class="d-flex justify-content-center my-3">
    <button class="btn btn-primary" type="button" disabled>
      <span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>
      Loading Trash...
    </button>
  </div>`;

  fetch('/api/trash/list')
    .then(response => response.json())
    .then(data => {
      container.innerHTML = "";

      if (panel === 'source') {
        currentSourcePath = 'trash';
        resetFileTracking(panel, 'trash');
      } else {
        currentDestinationPath = 'trash';
        resetFileTracking(panel, 'trash');
      }

      if (!data.enabled) {
        container.innerHTML = '<div class="alert alert-warning"><i class="bi bi-info-circle me-2"></i>Trash is disabled. Enable it in Settings &gt; File Processing.</div>';
        return;
      }

      // Header bar with info and empty button
      const totalSize = data.items.reduce((sum, i) => sum + i.size, 0);
      const header = document.createElement('div');
      header.className = 'alert alert-secondary d-flex justify-content-between align-items-center mb-3';
      header.innerHTML = `
        <div>
          <i class="bi bi-trash3 me-2"></i>
          <strong>Trash (${data.items.length} item${data.items.length !== 1 ? 's' : ''})</strong>
          <span class="ms-2 text-muted">${CLU.formatFileSize(totalSize)}</span>
        </div>
        ${data.items.length > 0 ? '<button class="btn btn-sm btn-danger" onclick="emptyTrash()"><i class="bi bi-trash me-1"></i>Empty Trash</button>' : ''}
      `;
      container.appendChild(header);

      if (data.items.length === 0) {
        const emptyMsg = document.createElement('div');
        emptyMsg.className = 'alert alert-info';
        emptyMsg.innerHTML = '<i class="bi bi-check-circle me-2"></i>Trash is empty.';
        container.appendChild(emptyMsg);
        return;
      }

      data.items.forEach(item => {
        const fileItem = document.createElement('li');
        fileItem.className = 'list-group-item list-group-item-action draggable d-flex align-items-start justify-content-between';
        fileItem.setAttribute('draggable', 'true');
        fileItem.setAttribute('data-fullpath', item.path);
        fileItem.setAttribute('data-trash-item', item.name);

        const icon = item.is_dir ? 'bi-folder' : 'bi-file-earmark-zip';
        const mtime = new Date(item.mtime * 1000);
        const timeAgo = getTimeAgo(mtime.toISOString());

        // Left container with file info
        const leftContainer = document.createElement('div');
        leftContainer.className = 'd-flex align-items-start flex-grow-1';
        leftContainer.style.minWidth = '0';
        leftContainer.innerHTML = `
          <i class="bi ${icon} me-2 mt-1"></i>
          <div class="flex-grow-1" style="min-width: 0;">
            <div class="fw-medium">${CLU.escapeHtml(item.name)}</div>
            <div class="small text-muted mt-1">
              <span>${CLU.formatFileSize(item.size)}</span>
              <span class="ms-2"><i class="bi bi-clock me-1"></i>${CLU.escapeHtml(timeAgo)}</span>
            </div>
          </div>
        `;

        // Button group
        const iconContainer = document.createElement('div');
        iconContainer.className = 'btn-group';
        iconContainer.setAttribute('role', 'group');

        // CBZ info button (files only)
        if (!item.is_dir) {
          const infoBtn = document.createElement('button');
          infoBtn.className = 'btn btn-sm btn-outline-info';
          infoBtn.innerHTML = '<i class="bi bi-eye"></i>';
          infoBtn.title = 'CBZ Information';
          infoBtn.setAttribute('type', 'button');
          infoBtn.onclick = function (e) {
            e.stopPropagation();
            const dirPath = item.path.substring(0, item.path.lastIndexOf('/'));
            showCBZInfo(item.path, item.name, dirPath, []);
          };
          iconContainer.appendChild(infoBtn);
        }

        // Permanently delete button
        const permDeleteBtn = document.createElement('button');
        permDeleteBtn.className = 'btn btn-sm btn-outline-danger';
        permDeleteBtn.innerHTML = '<i class="bi bi-x-lg"></i>';
        permDeleteBtn.title = 'Permanently delete';
        permDeleteBtn.setAttribute('type', 'button');
        permDeleteBtn.onclick = function (e) {
          e.stopPropagation();
          permanentlyDeleteTrashItem(item.name, permDeleteBtn);
        };
        iconContainer.appendChild(permDeleteBtn);

        fileItem.appendChild(leftContainer);
        fileItem.appendChild(iconContainer);

        // Drag start — allows dragging to destination panel to restore
        fileItem.addEventListener("dragstart", function (e) {
          const fullPath = item.path;
          if (selectedFiles.has(fullPath)) {
            e.dataTransfer.setData("text/plain", JSON.stringify([...selectedFiles].map(p => ({ path: p, type: item.is_dir ? "folder" : "file" }))));
            e.dataTransfer.effectAllowed = "move";

            const dragCount = selectedFiles.size;
            if (dragCount > 1) {
              const dragImage = document.createElement('div');
              dragImage.className = 'drag-preview';
              dragImage.textContent = `${dragCount} files`;
              dragImage.style.cssText = 'position: absolute; top: -1000px; background: #2196f3; color: white; padding: 0.5rem; border-radius: 0.25rem; font-weight: bold;';
              document.body.appendChild(dragImage);
              e.dataTransfer.setDragImage(dragImage, 50, 25);
              setTimeout(() => document.body.removeChild(dragImage), 0);
            }
          } else {
            selectedFiles.clear();
            document.querySelectorAll("li.list-group-item.selected").forEach(el => {
              el.classList.remove("selected");
              el.removeAttribute("data-selection-hint");
            });
            selectedFiles.add(fullPath);
            fileItem.classList.add("selected");
            fileItem.setAttribute("data-selection-hint", "Drag to destination to restore");
            if (typeof updateSelectionBadge === 'function') updateSelectionBadge();
            e.dataTransfer.setData("text/plain", JSON.stringify([{ path: fullPath, type: item.is_dir ? "folder" : "file" }]));
            e.dataTransfer.effectAllowed = "move";
          }
          fileItem.classList.add("dragging");
          setTimeout(() => fileItem.classList.remove("dragging"), 50);
        });

        fileItem.addEventListener("dragend", function () {
          setTimeout(() => {
            if (typeof clearAllDropHoverStates === 'function') clearAllDropHoverStates();
          }, 100);
        });

        // Click handler for multi-select
        fileItem.addEventListener('click', function (e) {
          if (e.ctrlKey || e.metaKey) {
            const fullPath = item.path;
            if (selectedFiles.has(fullPath)) {
              selectedFiles.delete(fullPath);
              fileItem.classList.remove("selected");
              fileItem.removeAttribute("data-selection-hint");
            } else {
              selectedFiles.add(fullPath);
              fileItem.classList.add("selected");
              fileItem.setAttribute("data-selection-hint", "Drag to destination to restore");
            }
            if (typeof updateSelectionBadge === 'function') updateSelectionBadge();
          }
        });

        container.appendChild(fileItem);
      });

      updateTrashBadge();
    })
    .catch(error => {
      console.error("Error loading trash:", error);
      container.innerHTML = `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle me-2"></i>Error loading trash: ${error.message}</div>`;
    });
}

function emptyTrash() {
  if (!confirm('Permanently delete all items in the Trash? This cannot be undone.')) return;

  fetch('/api/trash/empty', { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        CLU.showSuccess(`Emptied trash: ${data.count} item(s), ${CLU.formatFileSize(data.size_freed)} freed`);
        updateTrashBadge();
        loadTrash('source');
      } else {
        CLU.showError('Failed to empty trash');
      }
    })
    .catch(err => CLU.showError('Error emptying trash: ' + err.message));
}

function permanentlyDeleteTrashItem(name, btnEl) {
  fetch('/api/trash/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        const li = btnEl.closest('li');
        if (li) li.remove();
        updateTrashBadge();
        CLU.showSuccess('Item permanently deleted');
      } else {
        CLU.showError(data.error || 'Failed to delete item');
      }
    })
    .catch(err => CLU.showError('Error: ' + err.message));
}

// Helper function to format date/time
function formatDateTime(dateStr) {
  const date = new Date(dateStr);
  return date.toLocaleString();
}

// Helper function to calculate "time ago" string
function getTimeAgo(dateStr) {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now - date;
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return 'Just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

// Helper function to escape HTML to prevent XSS

// Test function for debugging toast
function testToast() {
  console.log('testToast() called');
  CLU.showToast('Test Toast', 'This is a test message', 'success');
}

// Function to clear all drop hover states
function clearAllDropHoverStates() {
  // Remove all hover-related classes from all elements
  document.querySelectorAll('.hover, .folder-hover, .drag-hover').forEach(element => {
    element.classList.remove('hover', 'folder-hover', 'drag-hover');
  });

  // Stop any auto-scroll that might be running
  if (typeof stopAutoScroll === 'function') {
    stopAutoScroll();
  }
}

// Make functions available globally for debugging
window.testToast = testToast;
window.clearAllDropHoverStates = clearAllDropHoverStates;

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', function () {
  console.log('DOM loaded, initializing files.js');

  // Check GCD MySQL availability
  checkGCDAvailability();

  // Check ComicVine API availability
  checkComicVineAvailability();

  // Check Metron API availability
  checkMetronAvailability();

  // Initialize rename rows as hidden (only on files page)
  const sourceRenameRow = document.getElementById('source-directory-rename-row');
  const destRenameRow = document.getElementById('destination-directory-rename-row');
  if (sourceRenameRow) sourceRenameRow.style.display = 'none';
  if (destRenameRow) destRenameRow.style.display = 'none';

  // Initial load for both panels (only on files page)
  const sourceList = document.getElementById("source-list");
  const destList = document.getElementById("destination-list");
  if (sourceList && destList) {
    loadDirectories(currentSourcePath, 'source');
    loadDirectories(currentDestinationPath, 'destination');

    // Attach drop events.
    setupDropEvents(sourceList, 'source');
    setupDropEvents(destList, 'destination');
  }

  // Add event listener for Update XML confirm button
  const updateXmlBtn = document.getElementById('updateXmlConfirmBtn');
  if (updateXmlBtn) updateXmlBtn.addEventListener('click', CLU.submitUpdateXml);

  // Add event listener for Update XML field dropdown change
  const updateXmlFieldSelect = document.getElementById('updateXmlField');
  if (updateXmlFieldSelect) updateXmlFieldSelect.addEventListener('change', CLU.updateXmlFieldChanged);

});

// Function to move an item.
function moveItem(source, destination) {
  fetch('/move', {
    method: 'POST',
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: source, destination: destination })
  })
    .then(response => response.json())
    .then(result => {
      if (result.success) {
        loadDirectories(currentSourcePath, 'source');
        loadDirectories(currentDestinationPath, 'destination');
      } else {
        alert("Error moving file: " + result.error);
      }
    })
    .catch(error => {
      console.error("Error in move request:", error);
    });
}

// Patch moveSingleItem to tolerate file objects
function moveSingleItem(sourcePath, targetFolder) {
  let actualPath = typeof sourcePath === 'object' ? sourcePath.path || sourcePath.name : sourcePath;
  showMovingModal();
  let fileName = actualPath.split('/').pop();
  setMovingStatus(`Moving ${fileName}`);
  updateMovingProgress(0);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/move", true);
  xhr.setRequestHeader("Content-Type", "application/json");
  xhr.setRequestHeader("X-Stream", "true");

  let finished = false;
  let lastResponseLength = 0;

  function completeMove() {
    if (!finished) {
      finished = true;
      xhr.onprogress = xhr.onreadystatechange = xhr.onerror = null;
      updateMovingProgress(100);
      setTimeout(() => {
        hideMovingModal();

        // Show success toast notification
        console.log('Single file move completed, showing toast for:', fileName);
        CLU.showToast('Move Successful', `Successfully moved ${fileName}`, 'success');

        setTimeout(() => {
          clearAllDropHoverStates(); // Clean up any lingering hover states
          loadDirectories(currentSourcePath, 'source');
          loadDirectories(currentDestinationPath, 'destination');
        }, 300);
      }, 200);
    }
  }

  xhr.onprogress = function (e) {
    let newData = xhr.responseText.substring(lastResponseLength);
    lastResponseLength = xhr.responseText.length;
    let events = newData.split("\n\n");
    events.forEach(event => {
      if (event.startsWith("data: ")) {
        let progressData = event.slice(6).trim();
        if (progressData === "done") {
          completeMove();
        } else if (progressData.startsWith("error:")) {
          console.error("Error:", progressData);
        } else {
          let percentComplete = parseInt(progressData);
          updateMovingProgress(percentComplete);
        }
      }
    });
  };

  xhr.onreadystatechange = function () {
    if (xhr.readyState === XMLHttpRequest.DONE && !finished) {
      completeMove();
    }
  };

  xhr.onerror = function () {
    alert("Error moving file: " + xhr.statusText);
    hideMovingModal();
  };

  const payload = {
    source: actualPath,
    destination: targetFolder + "/" + fileName
  };

  xhr.send(JSON.stringify(payload));
}

// Set up drop events for a given panel element.
function setupDropEvents(element, panel) {
  let autoScrollInterval = null;
  function startAutoScroll(direction) {
    if (autoScrollInterval !== null) return;
    autoScrollInterval = setInterval(() => {
      if (direction === "up") {
        element.scrollTop -= 5;
      } else if (direction === "down") {
        element.scrollTop += 5;
      }
    }, 50);
  }
  function stopAutoScroll() {
    if (autoScrollInterval !== null) {
      clearInterval(autoScrollInterval);
      autoScrollInterval = null;
    }
  }
  element.addEventListener("dragover", function (e) {
    e.preventDefault();
    // Detect external file drag (from desktop) for upload styling
    let isExternal = e.dataTransfer.types.includes('Files') &&
                     !e.dataTransfer.types.includes('text/plain');
    if (isExternal) {
      element.classList.add("upload-hover");
    } else {
      element.classList.add("hover");
    }
    let rect = element.getBoundingClientRect();
    let threshold = 50;
    let scrollDirection = null;
    if (e.clientY - rect.top < threshold) {
      scrollDirection = "up";
    } else if (rect.bottom - e.clientY < threshold) {
      scrollDirection = "down";
    }
    if (scrollDirection) {
      startAutoScroll(scrollDirection);
    } else {
      stopAutoScroll();
    }
  });
  element.addEventListener("dragleave", function (e) {
    element.classList.remove("hover");
    element.classList.remove("upload-hover");
    stopAutoScroll();
  });
  element.addEventListener("drop", function (e) {
    e.preventDefault();
    element.classList.remove("hover");
    element.classList.remove("upload-hover");
    stopAutoScroll();
    clearAllDropHoverStates();

    // Detect external file drop (from desktop) vs internal drag (between panels)
    let dataStr = e.dataTransfer.getData("text/plain");
    let isExternalFileDrop = e.dataTransfer.files.length > 0 && !dataStr;

    if (isExternalFileDrop) {
      let targetPath = panel === 'source' ? currentSourcePath : currentDestinationPath;
      handleExternalFileDrop(e.dataTransfer.files, targetPath, panel);
      return;
    }

    let items;
    try {
      items = JSON.parse(dataStr);
      if (!Array.isArray(items)) {
        items = [items];
      }
    } catch (err) {
      items = [{ path: dataStr, type: "unknown" }];
    }
    let targetPath = panel === 'source' ? currentSourcePath : currentDestinationPath;

    // Filter out items whose source folder is the same as the target folder.
    let validItems = items.filter(item => {
      let sourcePath = item.path;
      let sourceDir = sourcePath.substring(0, sourcePath.lastIndexOf('/'));
      return sourceDir !== targetPath;
    });
    if (validItems.length === 0) {
      console.log("All items dropped are in the same directory. Move cancelled.");
      return;
    }

    // If only one valid file item is being moved, call moveSingleItem for progress.
    const paths = validItems.map(item => item.path);

    // If *only one item is selected*, and no other selections exist, use moveSingleItem
    if (paths.length === 1 && selectedFiles.size <= 1 && validItems[0].type === "file") {
      moveSingleItem(paths[0], targetPath);
    } else {
      // Pass item types for better progress tracking
      const itemsWithTypes = validItems.map(item => ({
        path: item.path,
        type: item.type
      }));
      moveMultipleItems(paths, targetPath, panel, itemsWithTypes);
    }
    selectedFiles.clear();
    if (typeof updateSelectionBadge === 'function') updateSelectionBadge();
  });
}


/**
 * Handle files dropped from the desktop onto a panel (upload).
 * Posts to /upload-to-folder and refreshes the directory listing.
 */
function handleExternalFileDrop(fileList, targetPath, panel) {
  if (!targetPath || targetPath === 'recent-files' || targetPath === 'trash') {
    CLU.showToast('Upload Error', 'Navigate to a directory first before uploading files.', 'error');
    return;
  }

  const files = Array.from(fileList);
  if (files.length === 0) return;

  CLU.showToast('Uploading', `Uploading ${files.length} file(s) to ${targetPath.split('/').pop() || targetPath}...`, 'info');

  const formData = new FormData();
  formData.append('target_dir', targetPath);
  files.forEach(file => {
    formData.append('files', file);
  });

  fetch('/upload-to-folder', {
    method: 'POST',
    body: formData
  })
    .then(response => response.json())
    .then(data => {
      if (data.success) {
        let msg = `Uploaded ${data.total_uploaded} file(s)`;
        if (data.total_skipped > 0) {
          msg += `, skipped ${data.total_skipped}`;
        }
        if (data.total_errors > 0) {
          msg += `, ${data.total_errors} error(s)`;
        }
        CLU.showToast('Upload Complete', msg, data.total_errors > 0 ? 'warning' : 'success');

        // Refresh the panel to show newly uploaded files
        loadDirectories(targetPath, panel);
      } else {
        CLU.showToast('Upload Failed', data.error || 'Unknown error', 'error');
      }
    })
    .catch(error => {
      console.error('Upload error:', error);
      CLU.showToast('Upload Failed', error.message, 'error');
    });
}


// Update the breadcrumb display for source or destination panel.
function updateBreadcrumb(panel, fullPath) {
  let breadcrumbEl;
  if (panel === 'source') {
    breadcrumbEl = document.getElementById("source-path-display");
  } else if (panel === 'destination') {
    breadcrumbEl = document.getElementById("destination-path-display");
  } else {
    console.error("Invalid panel:", panel);
    return;
  }

  // Handle undefined or null fullPath
  if (!fullPath) {
    breadcrumbEl.innerHTML = "";
    return;
  }

  breadcrumbEl.innerHTML = "";
  let parts = fullPath.split('/').filter(Boolean);
  let pathSoFar = "";
  parts.forEach((part, index) => {
    pathSoFar += "/" + part;
    let currentPartPath = pathSoFar;
    const li = document.createElement("li");
    li.className = "breadcrumb-item";
    if (index === parts.length - 1) {
      li.classList.add("active");
      li.setAttribute("aria-current", "page");
      li.textContent = part;
    } else {
      const a = document.createElement("a");
      a.href = "#";
      a.textContent = part;
      a.onclick = function (e) {
        e.preventDefault();
        console.log("Breadcrumb clicked:", currentPartPath, "Panel:", panel);
        loadDirectories(currentPartPath, panel);
      };
      li.appendChild(a);
    }
    breadcrumbEl.appendChild(li);
  });
}


// Create Folder Modal functionality.
let createFolderModalEl = document.getElementById('createFolderModal');
let createFolderNameInput = document.getElementById('createFolderName');
let confirmCreateFolderBtn = document.getElementById('confirmCreateFolderBtn');

// Focus input when modal opens (only if modal exists)
if (createFolderModalEl) {
  createFolderModalEl.addEventListener('shown.bs.modal', function () {
    createFolderNameInput.focus();
  });
}

// Open modal function
function openCreateFolderModal() {
  document.getElementById('createFolderName').value = '';
  let createFolderModal = new bootstrap.Modal(createFolderModalEl);
  createFolderModal.show();
}

// Function to create folder
function createFolder() {
  let folderName = createFolderNameInput.value.trim();
  if (!folderName) {
    alert('Folder name cannot be empty.');
    createFolderNameInput.focus();
    return;
  }

  let fullPath = currentDestinationPath + '/' + folderName;

  fetch('/create-folder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: fullPath })
  })
    .then(response => response.json())
    .then(data => {
      if (data.success) {
        let createFolderModal = bootstrap.Modal.getInstance(createFolderModalEl);
        createFolderModal.hide();
        currentFilter['destination'] = 'all';
        loadDirectories(currentDestinationPath, 'destination');
      } else {
        alert(data.error || 'Error creating folder.');
      }
    })
    .catch(err => {
      console.error('Error creating folder:', err);
      alert('An unexpected error occurred.');
    });
}

// Click event for "Create" button (only if button exists)
if (confirmCreateFolderBtn) {
  confirmCreateFolderBtn.addEventListener('click', createFolder);
}

// Listen for "Enter" keypress inside input field (only if input exists)
if (createFolderNameInput) createFolderNameInput.addEventListener('keypress', function (event) {
  if (event.key === 'Enter') {
    event.preventDefault(); // Prevent form submission if inside a form
    createFolder();
  }
});

// Moving Status Modal Functions.
let movingModalEl = document.getElementById('movingModal');
let movingStatusText = document.getElementById('movingStatusText');
let movingProgressBar = document.getElementById('movingProgressBar');
let movingModal = null;
if (movingModalEl) {
  movingModal = new bootstrap.Modal(movingModalEl, {
    backdrop: 'static',
    keyboard: false
  });
}
function showMovingModal() {
  if (!movingModal) return;
  movingStatusText.textContent = "Preparing to move items...";
  movingProgressBar.style.width = "0%";
  movingProgressBar.setAttribute('aria-valuenow', 0);
  movingModal.show();
}
function hideMovingModal() {
  if (!movingModal) return;
  try {
    movingModal.hide();

    // Failsafe: ensure modal is actually hidden after a short delay
    setTimeout(() => {
      const modalElement = document.getElementById('movingModal');
      if (modalElement && modalElement.classList.contains('show')) {
        console.warn('Modal still showing after hide(), forcing removal');
        // Remove Bootstrap classes manually
        modalElement.classList.remove('show');
        modalElement.style.display = 'none';
        modalElement.setAttribute('aria-hidden', 'true');
        modalElement.removeAttribute('aria-modal');
        modalElement.removeAttribute('role');

        // Remove backdrop if it exists
        const backdrop = document.querySelector('.modal-backdrop');
        if (backdrop) {
          backdrop.remove();
        }

        // Restore body scroll
        document.body.classList.remove('modal-open');
        document.body.style.removeProperty('overflow');
        document.body.style.removeProperty('padding-right');
      }
    }, 500);
  } catch (err) {
    console.error('Error hiding modal:', err);
    // Force hide using DOM manipulation
    const modalElement = document.getElementById('movingModal');
    if (modalElement) {
      modalElement.classList.remove('show');
      modalElement.style.display = 'none';
      modalElement.setAttribute('aria-hidden', 'true');

      // Remove backdrop
      const backdrop = document.querySelector('.modal-backdrop');
      if (backdrop) {
        backdrop.remove();
      }

      // Restore body scroll
      document.body.classList.remove('modal-open');
      document.body.style.removeProperty('overflow');
      document.body.style.removeProperty('padding-right');
    }
  }
}
function setMovingStatus(message) {
  movingStatusText.textContent = message;
}
function updateMovingProgress(percentage) {
  movingProgressBar.style.width = percentage + "%";
  movingProgressBar.setAttribute('aria-valuenow', percentage);
}
// Handle streaming directory move with size-based progress
function handleStreamingDirectoryMove(response, fileName, sourcePath, targetFolder) {
  return new Promise((resolve, reject) => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let streamCompleted = false;

    // Timeout to ensure we don't hang forever
    const streamTimeout = setTimeout(() => {
      if (!streamCompleted) {
        console.warn(`Stream timeout for ${fileName}, assuming success`);
        streamCompleted = true;
        reader.cancel().catch(err => console.error('Error canceling reader:', err));
        resolve({ success: true });
      }
    }, 600000); // 10 minutes per directory

    function processStream() {
      reader.read().then(({ done, value }) => {
        if (done) {
          // Stream complete
          if (!streamCompleted) {
            streamCompleted = true;
            clearTimeout(streamTimeout);
            console.log(`Stream naturally completed for ${fileName}`);
            resolve({ success: true });
          }
          return;
        }

        // Decode the chunk and add to buffer
        buffer += decoder.decode(value, { stream: true });

        // Process complete lines
        const lines = buffer.split('\n');
        buffer = lines.pop(); // Keep incomplete line in buffer

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6); // Remove 'data: ' prefix

            if (data === 'done') {
              // Operation complete
              if (!streamCompleted) {
                streamCompleted = true;
                clearTimeout(streamTimeout);
                console.log(`Stream sent 'done' for ${fileName}`);
                resolve({ success: true });
              }
              return;
            } else if (data.startsWith('error:')) {
              // Operation failed
              if (!streamCompleted) {
                streamCompleted = true;
                clearTimeout(streamTimeout);
                const error = data.slice(7); // Remove 'error: ' prefix
                console.error(`Stream error for ${fileName}:`, error);
                reject(new Error(error));
              }
              return;
            } else if (data.startsWith('keepalive:')) {
              // Keepalive message, update status
              const status = data.slice(11); // Remove 'keepalive: ' prefix
              setMovingStatus(`Moving directory ${fileName}: ${status}`);
            } else if (!isNaN(data)) {
              // Progress percentage
              const progress = parseInt(data);
              updateMovingProgress(progress);

              // Update status with progress
              if (progress < 100) {
                setMovingStatus(`Moving directory ${fileName}: ${progress}% complete`);
              } else {
                setMovingStatus(`Finalizing directory move: ${fileName}`);
              }
            }
          }
        }

        // Continue reading
        processStream();
      }).catch(err => {
        if (!streamCompleted) {
          streamCompleted = true;
          clearTimeout(streamTimeout);
          console.error(`Stream read error for ${fileName}:`, err);
          reject(err);
        }
      });
    }

    processStream();
  });
}

// Enhanced moveMultipleItems with better directory progress
function moveMultipleItems(filePaths, targetFolder, panel, itemsWithTypes = null) {
  showMovingModal();
  let totalCount = filePaths.length;
  let currentIndex = 0;
  let totalFilesToMove = 0;
  let filesMoved = 0;
  let hasPendingOperations = false;

  // Activity-based timeout: resets after each file completes
  // Only fires if no file finishes within 5 minutes (indicates a hang, not slow progress)
  let operationTimeout = null;
  function resetOperationTimeout() {
    if (operationTimeout) clearTimeout(operationTimeout);
    operationTimeout = setTimeout(() => {
      console.warn("Move operation timed out (no progress for 5 minutes), closing modal");
      hideMovingModal();
      if (window.bootstrap && document.getElementById("moveErrorToast")) {
        document.getElementById("moveErrorToastBody").textContent = "Move operation timed out (no progress). Please check the server logs for details.";
        bootstrap.Toast.getOrCreateInstance(document.getElementById("moveErrorToast")).show();
      }
    }, 300000); // 5 minutes idle timeout
  }
  resetOperationTimeout();

  // First, count files in directories to get accurate progress
  function countFilesInDirectories() {
    let directoriesToCount = [];

    if (itemsWithTypes) {
      // Use the provided type information
      directoriesToCount = itemsWithTypes
        .filter(item => item.type === "directory")
        .map(item => item.path);
    } else {
      // Fallback: check all paths
      directoriesToCount = filePaths;
    }

    if (directoriesToCount.length === 0) {
      // No directories, proceed with normal counting
      totalFilesToMove = filePaths.length;
      startMoving();
      return;
    }

    let countPromises = [];
    directoriesToCount.forEach(path => {
      countPromises.push(
        fetch(`/count-files?path=${encodeURIComponent(path)}`)
          .then(res => res.json())
          .then(data => ({ path, fileCount: data.file_count || 0 }))
          .catch(err => ({ path, fileCount: 0 }))
      );
    });

    Promise.all(countPromises).then(results => {
      // Calculate total files to move
      totalFilesToMove = results.reduce((sum, result) => sum + result.fileCount, 0);
      // Add individual files (non-directories)
      const fileItems = itemsWithTypes ?
        itemsWithTypes.filter(item => item.type === "file").length :
        filePaths.length - directoriesToCount.length;
      totalFilesToMove += fileItems;

      if (totalFilesToMove === 0) {
        totalFilesToMove = filePaths.length; // Fallback to item count
      }

      startMoving();
    });
  }

  function startMoving() {
    moveNext();
  }

  function moveNext() {
    if (currentIndex >= totalCount) {
      // Check if there are any pending operations before closing
      if (hasPendingOperations) {
        console.log("Waiting for pending operations to complete...");
        setMovingStatus("Finalizing move operation...");
        setTimeout(moveNext, 100); // Wait a bit and check again
        return;
      }

      clearTimeout(operationTimeout); // Clear the timeout
      hideMovingModal();

      // Show success toast notification
      console.log('Multiple file move completed, totalCount:', totalCount);
      if (totalCount === 1) {
        CLU.showToast('Move Successful', `Successfully moved 1 item`, 'success');
      } else {
        CLU.showToast('Move Successful', `Successfully moved ${totalCount} items`, 'success');
      }

      clearAllDropHoverStates(); // Clean up any lingering hover states
      loadDirectories(currentSourcePath, 'source');
      loadDirectories(currentDestinationPath, 'destination');
      return;
    }

    let fileObj = normalizeFile(filePaths[currentIndex]);
    let sourcePath = typeof fileObj === 'string' ? fileObj : fileObj.path || fileObj.name;
    let fileName = sourcePath.split('/').pop();

    // Determine if this is a directory based on item type or by checking filesystem
    const currentItem = itemsWithTypes ? itemsWithTypes[currentIndex] : null;
    const isDirectory = currentItem ? currentItem.type === "directory" : null;

    let movePromise;

    if (isDirectory !== null) {
      // We have type information, use it
      if (isDirectory) {
        // For directories, use streaming mode to get size-based progress
        setMovingStatus(`Preparing to move directory ${fileName}...`);

        // Initialize progress at 0 for directories
        updateMovingProgress(0);

        hasPendingOperations = true;

        // Get directory size information for better status display
        const sizePromise = fetch(`/folder-size?path=${encodeURIComponent(sourcePath)}`)
          .then(res => res.json())
          .then(data => data.size || 0)
          .catch(err => 0);

        // For directories, use streaming mode to get size-based progress
        movePromise = Promise.all([
          sizePromise,
          fetch('/move', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-Stream': 'true'
            },
            body: JSON.stringify({
              source: sourcePath,
              destination: targetFolder + '/' + fileName
            })
          })
        ]).then(([dirSize, moveResponse]) => {
          // Update status with directory size information
          if (dirSize > 0) {
            setMovingStatus(`Moving directory ${fileName} (${CLU.formatFileSize(dirSize)}) - Starting...`);
          }
          return moveResponse;
        });
      } else {
        // For files, use file-based progress
        setMovingStatus(`Moving file ${fileName} (${filesMoved + 1} of ${totalFilesToMove} files)`);

        let percentage;
        if (totalFilesToMove > 0) {
          percentage = Math.floor((filesMoved / totalFilesToMove) * 100);
        } else {
          percentage = Math.floor((currentIndex / totalCount) * 100);
        }
        updateMovingProgress(percentage);

        hasPendingOperations = true;
        movePromise = fetch('/move', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source: sourcePath,
            destination: targetFolder + '/' + fileName
          })
        });
      }
    } else {
      // Fallback: check filesystem
      movePromise = fetch(`/count-files?path=${encodeURIComponent(sourcePath)}`)
        .then(res => res.json())
        .then(data => {
          const isDirectory = data.file_count !== undefined;
          const fileCount = data.file_count || 0;

          if (isDirectory && fileCount > 0) {
            // This is a directory with files - use streaming mode for size-based progress
            setMovingStatus(`Preparing to move directory ${fileName}...`);
            updateMovingProgress(0);

            hasPendingOperations = true;

            // Get directory size information for better status display
            const sizePromise = fetch(`/folder-size?path=${encodeURIComponent(sourcePath)}`)
              .then(res => res.json())
              .then(data => data.size || 0)
              .catch(err => 0);

            return Promise.all([
              sizePromise,
              fetch('/move', {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  'X-Stream': 'true'
                },
                body: JSON.stringify({
                  source: sourcePath,
                  destination: targetFolder + '/' + fileName
                })
              })
            ]).then(([dirSize, moveResponse]) => {
              // Update status with directory size information
              if (dirSize > 0) {
                setMovingStatus(`Moving directory ${fileName} (${CLU.formatFileSize(dirSize)}) - Starting...`);
              }
              return moveResponse;
            });
          } else {
            // This is a file or empty directory
            setMovingStatus(`Moving ${fileName} (${currentIndex + 1} of ${totalCount} items)`);

            let percentage = Math.floor((currentIndex / totalCount) * 100);
            updateMovingProgress(percentage);

            hasPendingOperations = true;
            return fetch('/move', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                source: sourcePath,
                destination: targetFolder + '/' + fileName
              })
            });
          }
        });
    }

    movePromise
      .then(res => {
        // Check if this is a streaming response (for directories)
        const currentItem = itemsWithTypes ? itemsWithTypes[currentIndex] : null;
        const isDirectory = currentItem ? currentItem.type === "directory" : null;

        if (isDirectory && res.headers.get('content-type')?.includes('text/event-stream')) {
          // Handle streaming response for directories
          return handleStreamingDirectoryMove(res, fileName, sourcePath, targetFolder);
        } else {
          // Handle regular JSON response for files
          return res.json();
        }
      })
      .then(data => {
        if (!data.success) {
          console.error("Move reported error:", data.error);

          // For directory moves, this is a critical error that should stop the operation
          const currentItem = itemsWithTypes ? itemsWithTypes[currentIndex] : null;
          const isDirectory = currentItem ? currentItem.type === "directory" : null;

          if (isDirectory) {
            const fileName = filePaths[currentIndex].split('/').pop();
            const detailMessage = `Failed to move directory "${fileName}" → "${targetFolder}": ${data.error}`;

            console.error("Directory move failed:", {
              directory: fileName,
              source: filePaths[currentIndex],
              destination: targetFolder + '/' + fileName,
              error: data.error
            });

            if (window.bootstrap && document.getElementById("moveErrorToast")) {
              document.getElementById("moveErrorToastBody").textContent = detailMessage;
              bootstrap.Toast.getOrCreateInstance(document.getElementById("moveErrorToast")).show();
            } else {
              alert(detailMessage);
            }

            // Hide the modal and stop the operation for directory failures
            clearTimeout(operationTimeout); // Clear the timeout
            hideMovingModal();
            return;
          } else {
            // For files, show warning but continue
            console.warn("File move reported error, but continuing:", data.error);
          }
        }

        // Update progress counters
        const currentItem = itemsWithTypes ? itemsWithTypes[currentIndex] : null;
        const isDirectory = currentItem ? currentItem.type === "directory" : null;

        if (isDirectory) {
          // For directories, we can't track individual files, so just increment item counter
          // The progress bar will update based on currentIndex/totalCount
          console.log(`Directory move completed: ${fileName}`);
        } else {
          // For files, increment file counter for file-based progress
          if (totalFilesToMove > 0) {
            filesMoved += 1;
          }
          console.log(`File move completed: ${fileName}`);
        }

        hasPendingOperations = false; // Clear pending operations flag
        resetOperationTimeout(); // Reset idle timeout — operation is still making progress
        currentIndex++;
        moveNext();
      })
      .catch(err => {
        const fileName = filePaths[currentIndex].split('/').pop();
        const detailMessage = `Failed to move "${fileName}" → "${targetFolder}": ${err.message || err}`;

        console.error("Move failed:", {
          file: fileName,
          source: filePaths[currentIndex],
          destination: targetFolder + '/' + fileName,
          error: err
        });

        // Check if this is a directory move failure
        const currentItem = itemsWithTypes ? itemsWithTypes[currentIndex] : null;
        const isDirectory = currentItem ? currentItem.type === "directory" : null;

        if (isDirectory) {
          // For directory failures, show error and stop the operation
          if (window.bootstrap && document.getElementById("moveErrorToast")) {
            document.getElementById("moveErrorToastBody").textContent = detailMessage;
            bootstrap.Toast.getOrCreateInstance(document.getElementById("moveErrorToast")).show();
          } else {
            alert(detailMessage);
          }

          // Hide the modal and stop the operation for directory failures
          clearTimeout(operationTimeout); // Clear the timeout
          hasPendingOperations = false; // Clear pending operations flag
          hideMovingModal();
          return;
        } else {
          // For file failures, show error but continue
          if (window.bootstrap && document.getElementById("moveErrorToast")) {
            document.getElementById("moveErrorToastBody").textContent = detailMessage;
            bootstrap.Toast.getOrCreateInstance(document.getElementById("moveErrorToast")).show();
          } else {
            alert(detailMessage);
          }
        }

        hasPendingOperations = false; // Clear pending operations flag
        resetOperationTimeout(); // Reset idle timeout — operation is still making progress
        currentIndex++;
        moveNext();
      });
  }

  // Start the process
  countFilesInDirectories();
}




// CBZ info – contract setup, adapts arguments for CLU.showCBZInfo
function showCBZInfo(filePath, fileName, directoryPath, fileList) {
  var opts = {};
  if (directoryPath && fileList) { opts.directoryPath = directoryPath; opts.fileList = fileList; }
  CLU.showCBZInfo(filePath, fileName, opts);
}

// CBZ info nav/clear/page viewer – handled by clu-cbz-info.js (DOMContentLoaded wiring is there too)
// Delete confirmation – contract setup for clu-delete.js
// Sets up contract with panel-specific UI removal and files.js endpoint
function showDeletePrompt(fullPath, name, panel) {
  window._cluDelete = {
    deleteEndpoint: '/delete',
    deletePayload: function (p) { return { target: p }; },
    onDeleteComplete: function (path) {
      // UI removal logic from deleteItem
      let container = panel === 'source'
        ? document.getElementById("source-list")
        : document.getElementById("destination-list");
      let itemToRemove = container
        ? container.querySelector('li[data-fullpath="' + path + '"]')
        : null;
      if (itemToRemove) {
        itemToRemove.classList.add('deleting');
        if (selectedFiles.has(path)) selectedFiles.delete(path);
        setTimeout(function () {
          if (itemToRemove && itemToRemove.parentNode) {
            itemToRemove.remove();
            if (panel === 'destination') {
              let destContainer = document.getElementById("destination-list");
              let remaining = destContainer.querySelectorAll("li:not(.drop-target-item)");
              if (remaining.length === 0) {
                createDropTargetItem(destContainer, currentDestinationPath, panel);
              }
            }
          }
        }, 200);
      } else {
        if (panel === 'source') loadDirectories(currentSourcePath, 'source');
        else loadDirectories(currentDestinationPath, 'destination');
      }
    },
    onDeleteError: function (path, error) {
      alert("Error deleting item: " + error);
    }
  };
  CLU.showDeleteConfirmation(fullPath, name);
}
// Delete confirmation and keyboard support now handled by clu-delete.js

// Track file counts and current paths for rename button
let fileTracking = {
  source: { fileCount: 0, currentPath: '' },
  destination: { fileCount: 0, currentPath: '' }
};

// Function to track file additions and update rename button
function trackFileForRename(panel) {
  fileTracking[panel].fileCount++;
  console.log(`File tracked for ${panel}: count now ${fileTracking[panel].fileCount}`);
  updateRenameButtonVisibility(panel);
}

// Function to track file removals and update rename button
function trackFileRemovalForRename(panel) {
  if (fileTracking[panel].fileCount > 0) {
    fileTracking[panel].fileCount--;
    console.log(`File removed from ${panel}: count now ${fileTracking[panel].fileCount}`);
    updateRenameButtonVisibility(panel);
  }
}

// Function to reset file tracking for a panel
function resetFileTracking(panel, currentPath) {
  fileTracking[panel].fileCount = 0;
  fileTracking[panel].currentPath = currentPath;
  console.log(`Reset file tracking for ${panel}: path=${currentPath}`);
  updateRenameButtonVisibility(panel);
}

// Function to update rename button visibility and functionality
function updateRenameButtonVisibility(panel) {
  const renameRowId = panel === 'source' ? 'source-directory-rename-row' : 'destination-directory-rename-row';
  const renameRow = document.getElementById(renameRowId);

  if (!renameRow) {
    console.log('Rename row not found:', renameRowId);
    return;
  }

  const hasFiles = fileTracking[panel].fileCount > 0;
  // Use the global path variables instead of file tracking
  const currentPath = panel === 'source' ? currentSourcePath : currentDestinationPath;
  const rootPath = panel === 'source' ? '/temp' : '/processed';
  const isNotRoot = currentPath !== rootPath;

  console.log('updateRenameButtonVisibility:', panel, 'hasFiles=', hasFiles, 'currentPath=', currentPath, 'isNotRoot=', isNotRoot);

  // Show row if we're not at root level (Add CVINFO works for empty folders too)
  if (isNotRoot) {
    console.log('Showing rename row:', panel, 'files=', fileTracking[panel].fileCount, 'path=', currentPath);

    // File-related buttons (only show when there are files)
    let renameButton = renameRow.querySelector('.rename-files-btn');
    let replaceButton = renameRow.querySelector('.replace-text-btn');
    let seriesRenameButton = renameRow.querySelector('.series-rename-btn');

    if (hasFiles) {
      // Create or update the rename text button
      if (!renameButton) {
        renameButton = document.createElement('button');
        renameButton.className = 'btn btn-outline-primary btn-sm rename-files-btn me-2';
        renameButton.innerHTML = '<i class="bi bi-input-cursor-text me-2"></i>Remove Text';
        renameButton.title = 'Remove text from all filenames in this directory';
        renameRow.appendChild(renameButton);
      }
      renameButton.style.display = '';
      renameButton.dataset.currentPath = currentPath;
      renameButton.dataset.currentPanel = panel;
      renameButton.onclick = function (e) {
        e.preventDefault();
        const pathFromData = e.target.dataset.currentPath;
        const panelFromData = e.target.dataset.currentPanel;
        console.log('Remove text button clicked, path from data:', pathFromData, 'panel:', panelFromData);
        openCustomRenameModal(pathFromData, panelFromData);
      };

      // Create or update the replace text button
      if (!replaceButton) {
        replaceButton = document.createElement('button');
        replaceButton.className = 'btn btn-outline-warning btn-sm replace-text-btn me-2';
        replaceButton.innerHTML = '<i class="bi bi-arrow-left-right me-2"></i>Replace Text';
        replaceButton.title = 'Replace text in all filenames in this directory';
        renameRow.appendChild(replaceButton);
      }
      replaceButton.style.display = '';
      replaceButton.dataset.currentPath = currentPath;
      replaceButton.dataset.currentPanel = panel;
      replaceButton.onclick = function (e) {
        e.preventDefault();
        const pathFromData = e.target.dataset.currentPath;
        const panelFromData = e.target.dataset.currentPanel;
        console.log('Replace text button clicked, path from data:', pathFromData, 'panel:', panelFromData);
        openReplaceTextModal(pathFromData, panelFromData);
      };

      // Create or update the series rename button
      if (!seriesRenameButton) {
        seriesRenameButton = document.createElement('button');
        seriesRenameButton.className = 'btn btn-outline-success btn-sm series-rename-btn me-2';
        seriesRenameButton.innerHTML = '<i class="bi bi-pencil-square me-2"></i>Rename Series';
        seriesRenameButton.title = 'Replace series name while preserving issue numbers and years';
        renameRow.appendChild(seriesRenameButton);
      }
      seriesRenameButton.style.display = '';
      seriesRenameButton.dataset.currentPath = currentPath;
      seriesRenameButton.dataset.currentPanel = panel;
      seriesRenameButton.onclick = function (e) {
        e.preventDefault();
        const pathFromData = e.target.dataset.currentPath;
        const panelFromData = e.target.dataset.currentPanel;
        console.log('Series rename button clicked, path from data:', pathFromData, 'panel:', panelFromData);
        openRenameFilesModal(pathFromData, panelFromData);
      };
    } else {
      // Hide file-related buttons when no files
      if (renameButton) renameButton.style.display = 'none';
      if (replaceButton) replaceButton.style.display = 'none';
      if (seriesRenameButton) seriesRenameButton.style.display = 'none';
    }

    // Create or update the Add CVINFO button (always visible for non-root folders)
    let cvInfoButton = renameRow.querySelector('.add-cvinfo-btn');
    if (!cvInfoButton) {
      cvInfoButton = document.createElement('button');
      cvInfoButton.className = 'btn btn-outline-info btn-sm add-cvinfo-btn me-2';
      cvInfoButton.innerHTML = '<i class="bi bi-link-45deg me-2"></i>Add CVINFO';
      cvInfoButton.title = 'Save ComicVine URL to cvinfo file in this directory';
      renameRow.appendChild(cvInfoButton);
    }

    // Store the current path as a data attribute
    cvInfoButton.dataset.currentPath = currentPath;
    cvInfoButton.dataset.currentPanel = panel;

    // Update button click handler with current context
    cvInfoButton.onclick = function (e) {
      e.preventDefault();
      const pathFromData = this.dataset.currentPath;
      const panelFromData = this.dataset.currentPanel;
      console.log('Add CVINFO button clicked, path:', pathFromData, 'panel:', panelFromData);
      promptForCVInfo(pathFromData, panelFromData);
    };

    renameRow.style.display = 'block';
  } else {
    console.log('Hiding rename row:', panel, 'hasFiles=', hasFiles, 'isNotRoot=', isNotRoot, 'path=', currentPath);
    renameRow.style.display = 'none';

    // Reset file count to 0 when hiding the button (no files in current directory)
    if (fileTracking[panel].fileCount > 0) {
      console.log(`Resetting file count for ${panel} from ${fileTracking[panel].fileCount} to 0 (no files in current directory)`);
      fileTracking[panel].fileCount = 0;
    }
  }
}

// Custom Rename Modal functionality
let customRenameModal;
let currentRenameDirectory = '';
let currentRenamePanel = '';
let fileList = [];

function openCustomRenameModal(directoryPath, panel) {
  console.log('openCustomRenameModal called with:', directoryPath, panel);
  currentRenameDirectory = directoryPath;
  currentRenamePanel = panel;

  // Validate that we have a valid directory path
  if (!directoryPath || directoryPath === '') {
    console.error('Invalid directory path provided to openCustomRenameModal:', directoryPath);
    alert('Error: No directory path provided for rename operation.');
    return;
  }

  // Reset modal state
  document.getElementById('textToRemove').value = '';
  document.getElementById('renamePreview').style.display = 'none';
  document.getElementById('previewRenameBtn').style.display = 'inline-block';
  document.getElementById('executeRenameBtn').style.display = 'none';

  // Show modal
  const modalEl = document.getElementById('customRenameModal');
  customRenameModal = new bootstrap.Modal(modalEl);
  customRenameModal.show();

  // Focus on input when modal opens
  modalEl.addEventListener('shown.bs.modal', function () {
    document.getElementById('textToRemove').focus();
  }, { once: true });
}

function previewCustomRename() {
  const textToRemove = document.getElementById('textToRemove').value;

  console.log('previewCustomRename called');
  console.log('currentRenameDirectory:', currentRenameDirectory);
  console.log('currentRenamePanel:', currentRenamePanel);
  console.log('textToRemove:', textToRemove);

  if (!textToRemove.trim()) {
    alert('Please enter text to remove from filenames.');
    return;
  }

  if (!currentRenameDirectory || currentRenameDirectory === '') {
    alert('Error: No directory selected for rename operation.');
    console.error('currentRenameDirectory is empty');
    return;
  }

  // Fetch files in the directory
  const url = `/list-directories?path=${encodeURIComponent(currentRenameDirectory)}`;
  console.log('Fetching URL:', url);
  fetch(url)
    .then(response => response.json())
    .then(data => {
      if (data.error) {
        throw new Error(data.error);
      }

      fileList = [];
      const previewList = document.getElementById('renamePreviewList');
      previewList.innerHTML = '';

      // Filter only files (not directories) that contain the text to remove
      const filesToRename = (data.files || []).filter(file => {
        const fileData = normalizeFile(file);
        const nameWithoutExtension = fileData.name.substring(0, fileData.name.lastIndexOf('.')) || fileData.name;
        return nameWithoutExtension.includes(textToRemove);
      });

      if (filesToRename.length === 0) {
        previewList.innerHTML = '<div class="text-warning">No files found containing the specified text.</div>';
      } else {
        filesToRename.forEach(file => {
          const fileData = normalizeFile(file);
          const nameWithoutExtension = fileData.name.substring(0, fileData.name.lastIndexOf('.')) || fileData.name;
          const extension = fileData.name.substring(fileData.name.lastIndexOf('.')) || '';
          const newNameWithoutExtension = nameWithoutExtension.replace(new RegExp(escapeRegExp(textToRemove), 'g'), '');
          const newName = newNameWithoutExtension + extension;

          fileList.push({
            oldPath: `${currentRenameDirectory}/${fileData.name}`,
            newName: newName,
            oldName: fileData.name
          });

          const previewItem = document.createElement('div');
          previewItem.className = 'mb-2 p-2 border rounded';
          previewItem.innerHTML = `
                <div><strong>Old:</strong> <code>${fileData.name}</code></div>
                <div><strong>New:</strong> <code>${newName}</code></div>
              `;
          previewList.appendChild(previewItem);
        });
      }

      // Show preview and execute button
      document.getElementById('renamePreview').style.display = 'block';
      if (filesToRename.length > 0) {
        document.getElementById('executeRenameBtn').style.display = 'inline-block';
      }
    })
    .catch(error => {
      console.error('Error fetching directory contents:', error);
      alert('Error fetching directory contents: ' + error.message);
    });
}

function executeCustomRename() {
  if (fileList.length === 0) {
    alert('No files to rename.');
    return;
  }

  // Disable buttons during execution
  document.getElementById('previewRenameBtn').disabled = true;
  document.getElementById('executeRenameBtn').disabled = true;
  document.getElementById('executeRenameBtn').textContent = 'Renaming...';

  // Execute renames
  const renamePromises = fileList.map(file => {
    const newPath = `${currentRenameDirectory}/${file.newName}`;
    return fetch('/custom-rename', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        old: file.oldPath,
        new: newPath
      })
    });
  });

  Promise.all(renamePromises)
    .then(responses => {
      const errors = [];
      responses.forEach((response, index) => {
        if (!response.ok) {
          errors.push(`Failed to rename ${fileList[index].oldName}`);
        }
      });

      if (errors.length > 0) {
        alert('Some files could not be renamed:\n' + errors.join('\n'));
      } else {
        // Show success message in modal before closing
        const renamePreviewList = document.getElementById('renamePreviewList');
        renamePreviewList.innerHTML = `
              <div class="alert alert-success text-center">
                <i class="bi bi-check-circle-fill me-2"></i>
                <strong>Success!</strong> Renamed ${fileList.length} files.
              </div>
            `;
        document.getElementById('previewRenameBtn').style.display = 'none';
        document.getElementById('executeRenameBtn').style.display = 'none';

        // Auto-close modal after 2 seconds
        setTimeout(() => {
          customRenameModal.hide();
        }, 2000);
      }

      // Refresh directory listing - use loadDownloads since that's what shows files
      loadDownloads(currentRenameDirectory, currentRenamePanel);
    })
    .catch(error => {
      console.error('Error during rename operation:', error);
      alert('Error during rename operation: ' + error.message);
    })
    .finally(() => {
      // Re-enable buttons
      document.getElementById('previewRenameBtn').disabled = false;
      document.getElementById('executeRenameBtn').disabled = false;
      document.getElementById('executeRenameBtn').textContent = 'Execute Rename';
    });
}

// Replace Text Modal functionality
let replaceTextModal;
let currentReplaceDirectory = '';
let currentReplacePanel = '';
let replaceFileList = [];

function openReplaceTextModal(directoryPath, panel) {
  console.log('openReplaceTextModal called with:', directoryPath, panel);
  currentReplaceDirectory = directoryPath;
  currentReplacePanel = panel;

  // Validate that we have a valid directory path
  if (!directoryPath || directoryPath === '') {
    console.error('Invalid directory path provided to openReplaceTextModal:', directoryPath);
    alert('Error: No directory path provided for replace operation.');
    return;
  }

  // Reset modal state
  document.getElementById('textToReplace').value = '';
  document.getElementById('replacementText').value = '';
  document.getElementById('replacePreview').style.display = 'none';
  document.getElementById('previewReplaceBtn').style.display = 'inline-block';
  document.getElementById('executeReplaceBtn').style.display = 'none';

  // Show modal
  const modalEl = document.getElementById('replaceTextModal');
  replaceTextModal = new bootstrap.Modal(modalEl);
  replaceTextModal.show();

  // Focus on input when modal opens
  modalEl.addEventListener('shown.bs.modal', function () {
    document.getElementById('textToReplace').focus();
  }, { once: true });
}

function previewReplaceText() {
  const textToReplace = document.getElementById('textToReplace').value;
  const replacementText = document.getElementById('replacementText').value;

  console.log('previewReplaceText called');
  console.log('currentReplaceDirectory:', currentReplaceDirectory);
  console.log('currentReplacePanel:', currentReplacePanel);
  console.log('textToReplace:', textToReplace);
  console.log('replacementText:', replacementText);

  if (!textToReplace.trim()) {
    alert('Please enter text to replace in filenames.');
    return;
  }

  if (!currentReplaceDirectory || currentReplaceDirectory === '') {
    alert('Error: No directory selected for replace operation.');
    console.error('currentReplaceDirectory is empty');
    return;
  }

  // Fetch files in the directory
  const url = `/list-directories?path=${encodeURIComponent(currentReplaceDirectory)}`;
  console.log('Fetching URL:', url);
  fetch(url)
    .then(response => response.json())
    .then(data => {
      if (data.error) {
        throw new Error(data.error);
      }

      replaceFileList = [];
      const previewList = document.getElementById('replacePreviewList');
      previewList.innerHTML = '';

      // Filter only files (not directories) that contain the text to replace
      const filesToRename = (data.files || []).filter(file => {
        const fileData = normalizeFile(file);
        const nameWithoutExtension = fileData.name.substring(0, fileData.name.lastIndexOf('.')) || fileData.name;
        return nameWithoutExtension.includes(textToReplace);
      });

      if (filesToRename.length === 0) {
        previewList.innerHTML = '<div class="text-warning">No files found containing the specified text.</div>';
      } else {
        filesToRename.forEach(file => {
          const fileData = normalizeFile(file);
          const nameWithoutExtension = fileData.name.substring(0, fileData.name.lastIndexOf('.')) || fileData.name;
          const extension = fileData.name.substring(fileData.name.lastIndexOf('.')) || '';
          const newNameWithoutExtension = nameWithoutExtension.replace(new RegExp(escapeRegExp(textToReplace), 'g'), replacementText);
          const newName = newNameWithoutExtension + extension;

          replaceFileList.push({
            oldPath: `${currentReplaceDirectory}/${fileData.name}`,
            newName: newName,
            oldName: fileData.name
          });

          const previewItem = document.createElement('div');
          previewItem.className = 'mb-2 p-2 border rounded';
          previewItem.innerHTML = `
                <div><strong>Old:</strong> <code>${fileData.name}</code></div>
                <div><strong>New:</strong> <code>${newName}</code></div>
              `;
          previewList.appendChild(previewItem);
        });
      }

      // Show preview and execute button
      document.getElementById('replacePreview').style.display = 'block';
      if (filesToRename.length > 0) {
        document.getElementById('executeReplaceBtn').style.display = 'inline-block';
      }
    })
    .catch(error => {
      console.error('Error fetching directory contents:', error);
      alert('Error fetching directory contents: ' + error.message);
    });
}

function executeReplaceText() {
  if (replaceFileList.length === 0) {
    alert('No files to rename.');
    return;
  }

  // Disable buttons during execution
  document.getElementById('previewReplaceBtn').disabled = true;
  document.getElementById('executeReplaceBtn').disabled = true;
  document.getElementById('executeReplaceBtn').textContent = 'Replacing...';

  // Execute renames
  const renamePromises = replaceFileList.map(file => {
    const newPath = `${currentReplaceDirectory}/${file.newName}`;
    return fetch('/custom-rename', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        old: file.oldPath,
        new: newPath
      })
    });
  });

  Promise.all(renamePromises)
    .then(responses => {
      const errors = [];
      responses.forEach((response, index) => {
        if (!response.ok) {
          errors.push(`Failed to rename ${replaceFileList[index].oldName}`);
        }
      });

      if (errors.length > 0) {
        alert('Some files could not be renamed:\n' + errors.join('\n'));
      } else {
        // Show success message in modal before closing
        const replacePreviewList = document.getElementById('replacePreviewList');
        replacePreviewList.innerHTML = `
              <div class="alert alert-success text-center">
                <i class="bi bi-check-circle-fill me-2"></i>
                <strong>Success!</strong> Replaced text in ${replaceFileList.length} files.
              </div>
            `;
        document.getElementById('previewReplaceBtn').style.display = 'none';
        document.getElementById('executeReplaceBtn').style.display = 'none';

        // Auto-close modal after 2 seconds
        setTimeout(() => {
          replaceTextModal.hide();
        }, 2000);
      }

      // Refresh directory listing - use loadDownloads since that's what shows files
      loadDownloads(currentReplaceDirectory, currentReplacePanel);
    })
    .catch(error => {
      console.error('Error during replace operation:', error);
      alert('Error during replace operation: ' + error.message);
    })
    .finally(() => {
      // Re-enable buttons
      document.getElementById('previewReplaceBtn').disabled = false;
      document.getElementById('executeReplaceBtn').disabled = false;
      document.getElementById('executeReplaceBtn').textContent = 'Execute Replace';
    });
}

// Helper function to escape special regex characters
function escapeRegExp(string) {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Add Enter key support for the text input (only if element exists)
const textToRemoveEl = document.getElementById('textToRemove');
if (textToRemoveEl) {
  textToRemoveEl.addEventListener('keypress', function (e) {
    if (e.key === 'Enter') {
      previewCustomRename();
    }
  });
}

// ============================================================================
// Series Rename Modal functionality
// ============================================================================
let renameFilesModal;
let currentSeriesRenameDirectory = '';
let currentSeriesRenamePanel = '';
let seriesFileList = [];

function openRenameFilesModal(directoryPath, panel) {
  console.log('openRenameFilesModal called with:', directoryPath, panel);
  currentSeriesRenameDirectory = directoryPath;
  currentSeriesRenamePanel = panel;

  // Validate that we have a valid directory path
  if (!directoryPath || directoryPath === '') {
    console.error('Invalid directory path provided to openRenameFilesModal:', directoryPath);
    CLU.showToast('Path Error', 'No directory path provided for series rename operation.', 'error');
    return;
  }

  // Reset modal state
  document.getElementById('newSeriesName').value = '';
  document.getElementById('renameFilesPreview').style.display = 'none';
  document.getElementById('previewRenameFilesBtn').style.display = 'inline-block';
  document.getElementById('executeRenameFilesBtn').style.display = 'none';
  document.getElementById('renameFilesPreviewList').innerHTML = '';

  // Initialize modal
  if (!renameFilesModal) {
    renameFilesModal = new bootstrap.Modal(document.getElementById('renameFilesModal'));
  }

  // Show modal
  renameFilesModal.show();

  // Focus on input field
  setTimeout(() => {
    document.getElementById('newSeriesName').focus();
  }, 300);
}

function previewRenameFiles() {
  const newSeriesName = document.getElementById('newSeriesName').value.trim();

  if (!newSeriesName) {
    CLU.showToast('Input Required', 'Please enter a new series name.', 'warning');
    return;
  }

  console.log('previewRenameFiles called with series name:', newSeriesName);

  // Fetch file list from the directory using the correct endpoint
  fetch(`/list-directories?path=${encodeURIComponent(currentSeriesRenameDirectory)}`)
    .then(response => response.json())
    .then(data => {
      console.log('Directory listing response:', data);

      if (data.error) {
        CLU.showToast('Directory Error', data.error, 'error');
        return;
      }

      if (!data.files || data.files.length === 0) {
        CLU.showToast('No Files Found', 'No files found in the directory.', 'warning');
        return;
      }

      // Filter only comic files
      const comicFiles = data.files.filter(file => {
        const fileData = typeof file === 'object' ? file : { name: file };
        return fileData.name.toLowerCase().endsWith('.cbz') || fileData.name.toLowerCase().endsWith('.zip') || fileData.name.toLowerCase().endsWith('.cbr');
      });

      if (comicFiles.length === 0) {
        CLU.showToast('No Comic Files', 'No comic files (.cbz/.cbr) found in the directory.', 'warning');
        return;
      }

      // Generate preview of renamed files
      seriesFileList = comicFiles.map(file => {
        const originalName = file.name;
        const newName = generateSeriesRename(originalName, newSeriesName);

        return {
          oldPath: `${currentSeriesRenameDirectory}/${originalName}`,
          originalName: originalName,
          newName: newName
        };
      });

      // Display preview
      displaySeriesRenamePreview(seriesFileList);

      // Show preview and enable execute button
      document.getElementById('renameFilesPreview').style.display = 'block';
      document.getElementById('executeRenameFilesBtn').style.display = 'inline-block';
    })
    .catch(error => {
      console.error('Error fetching directory listing:', error);
      CLU.showToast('Fetch Error', 'Error fetching file list: ' + error.message, 'error');
    });
}

function generateSeriesRename(originalName, newSeriesName) {
  // Extract issue number and year patterns from the filename
  // Common patterns: "Series Name 001 (1985).cbz", "Series Name #1 (1985).cbz", etc.

  // Try to extract issue and year information
  const patterns = [
    // "Series Name 001 (1985).cbz" or "Series Name #001 (1985).cbz"
    /^.*?(\s+#?\d{1,4})\s*\((\d{4})\)(\.\w+)$/,
    // "Series Name 001.cbz" (no year)
    /^.*?(\s+#?\d{1,4})(\.\w+)$/,
    // "Series Name (1985).cbz" (no issue)
    /^.*?\s*\((\d{4})\)(\.\w+)$/,
    // Just extension (fallback)
    /^.*?(\.\w+)$/
  ];

  for (let pattern of patterns) {
    const match = originalName.match(pattern);
    if (match) {
      if (match.length === 4) {
        // Issue and year found
        const issue = match[1];
        const year = match[2];
        const ext = match[3];
        return `${newSeriesName}${issue} (${year})${ext}`;
      } else if (match.length === 3) {
        // Check if it's issue + ext or year + ext
        if (match[1].includes('#') || /^\s+\d/.test(match[1])) {
          // Issue number found, no year
          const issue = match[1];
          const ext = match[2];
          return `${newSeriesName}${issue}${ext}`;
        } else {
          // Year found, no issue
          const year = match[1];
          const ext = match[2];
          return `${newSeriesName} (${year})${ext}`;
        }
      } else if (match.length === 2) {
        // Just extension
        const ext = match[1];
        return `${newSeriesName}${ext}`;
      }
    }
  }

  // Fallback: just replace everything before the extension
  const ext = originalName.substring(originalName.lastIndexOf('.'));
  return `${newSeriesName}${ext}`;
}

function displaySeriesRenamePreview(fileList) {
  const previewContainer = document.getElementById('renameFilesPreviewList');
  previewContainer.innerHTML = '';

  if (fileList.length === 0) {
    previewContainer.innerHTML = '<p class="text-muted">No files to rename.</p>';
    return;
  }

  fileList.forEach(file => {
    const div = document.createElement('div');
    div.className = 'mb-2 p-2 border rounded';
    div.innerHTML = `
          <div><strong>Original:</strong> <code>${file.originalName}</code></div>
          <div><strong>New:</strong> <code class="text-success">${file.newName}</code></div>
        `;
    previewContainer.appendChild(div);
  });
}

function executeRenameFiles() {
  if (seriesFileList.length === 0) {
    CLU.showToast('No Files', 'No files to rename.', 'warning');
    return;
  }

  // Disable buttons during execution
  document.getElementById('previewRenameFilesBtn').disabled = true;
  document.getElementById('executeRenameFilesBtn').disabled = true;
  document.getElementById('executeRenameFilesBtn').textContent = 'Renaming...';

  // Execute renames
  const renamePromises = seriesFileList.map(file => {
    const newPath = `${currentSeriesRenameDirectory}/${file.newName}`;
    return fetch('/custom-rename', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        old: file.oldPath,
        new: newPath
      })
    })
      .then(response => response.json())
      .then(data => {
        if (!data.success) {
          throw new Error(`Failed to rename ${file.originalName}: ${data.error}`);
        }
        return data;
      });
  });

  Promise.all(renamePromises)
    .then(results => {
      console.log('All series renames completed:', results);

      // Check if all renames were successful
      const failedRenames = results.filter(result => !result.success);

      if (failedRenames.length > 0) {
        CLU.showToast('Partial Success', `Some files could not be renamed. ${failedRenames.length} failures.`, 'warning');
      } else {
        CLU.showToast('Rename Complete', `Successfully renamed ${results.length} files with new series name.`, 'success');

        // Auto-close modal after 2 seconds
        setTimeout(() => {
          renameFilesModal.hide();
        }, 2000);
      }

      // Refresh directory listing
      loadDownloads(currentSeriesRenameDirectory, currentSeriesRenamePanel);
    })
    .catch(error => {
      console.error('Error during series rename operation:', error);
      CLU.showToast('Rename Error', 'Error during series rename operation: ' + error.message, 'error');
    })
    .finally(() => {
      // Re-enable buttons
      document.getElementById('previewRenameFilesBtn').disabled = false;
      document.getElementById('executeRenameFilesBtn').disabled = false;
      document.getElementById('executeRenameFilesBtn').textContent = 'Execute Rename';
    });
}

// Add Enter key support for the series name input (only if element exists)
const newSeriesNameEl = document.getElementById('newSeriesName');
if (newSeriesNameEl) {
  newSeriesNameEl.addEventListener('keypress', function (e) {
    if (e.key === 'Enter') {
      previewRenameFiles();
    }
  });
}

// Search functionality
let searchModal;
let currentSearchController = null; // AbortController for current search

function openSearchModal() {
  searchModal = new bootstrap.Modal(document.getElementById('searchModal'));
  searchModal.show();

  // Clear previous search and focus on search input
  document.getElementById('searchQuery').value = '';
  document.getElementById('searchResults').style.display = 'none';
  document.getElementById('currentSearchTerm').textContent = '';

  // Cancel any ongoing search when modal opens
  cancelCurrentSearch();

  // Clear any pending search timeout
  if (searchTimeout) {
    clearTimeout(searchTimeout);
    searchTimeout = null;
  }

  // Focus on search input
  setTimeout(() => {
    document.getElementById('searchQuery').focus();
  }, 500);
}

function cancelCurrentSearch() {
  if (currentSearchController) {
    currentSearchController.abort();
    currentSearchController = null;
    console.log('Search cancelled');
  }
}

function performSearch() {
  const query = document.getElementById('searchQuery').value.trim();

  if (!query) {
    alert('Please enter a search term.');
    return;
  }

  if (query.length < 2) {
    alert('Search term must be at least 2 characters.');
    return;
  }

  // Cancel any ongoing search before starting a new one
  cancelCurrentSearch();

  // Create new AbortController for this search
  currentSearchController = new AbortController();

  // Show loading and update search term immediately
  document.getElementById('searchLoading').style.display = 'block';
  document.getElementById('searchResults').style.display = 'block';
  document.getElementById('currentSearchTerm').textContent = query;

  // Perform search with abort signal
  fetch(`/search-files?query=${encodeURIComponent(query)}`, {
    signal: currentSearchController.signal
  })
    .then(response => {
      // Check if the request was aborted
      if (response.ok) {
        return response.json();
      } else {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
    })
    .then(data => {
      // Only process results if this is still the current search
      if (currentSearchController) {
        document.getElementById('searchLoading').style.display = 'none';

        if (data.error) {
          throw new Error(data.error);
        }

        // Check for timeout message
        if (data.timeout) {
          alert(`Search timeout: ${data.message}`);
        }

        displaySearchResults(data.results, query);
      }
    })
    .catch(error => {
      // Only show error if it's not an abort error
      if (error.name !== 'AbortError') {
        document.getElementById('searchLoading').style.display = 'none';
        console.error('Search error:', error);
        alert('Search error: ' + error.message);
      }
    });
}

function displaySearchResults(results, query) {
  const resultsContainer = document.getElementById('searchResultsList');
  const resultsDiv = document.getElementById('searchResults');

  resultsContainer.innerHTML = '';

  // Always show the results container with the search term
  resultsDiv.style.display = 'block';

  if (results.length === 0) {
    resultsContainer.innerHTML = `
          <div class="text-center text-muted p-3">
            <i class="bi bi-search me-2"></i>
            No results found for "${query}"
          </div>
        `;
  } else {
    results.forEach(item => {
      const resultItem = document.createElement('div');
      resultItem.className = 'list-group-item list-group-item-action d-flex align-items-center justify-content-between';

      const icon = item.type === 'directory' ? 'bi-folder' : 'bi-file-earmark-zip';
      const size = item.type === 'file' ? formatSize(item.size) : '';

      resultItem.innerHTML = `
            <div class="d-flex align-items-center">
              <i class="bi ${icon} me-2"></i>
              <span>${item.name}</span>
              ${size ? `<span class="text-info-emphasis small ms-2">(${size})</span>` : ''}
            </div>
            <div class="text-muted small">
              ${item.parent}
            </div>
          `;

      // Add click handler to navigate to the item
      resultItem.addEventListener('click', () => {
        navigateToSearchResult(item);
      });

      resultsContainer.appendChild(resultItem);
    });
  }
}

function navigateToSearchResult(item) {
  // Close search modal
  searchModal.hide();

  // Navigate to the parent directory in the destination panel
  loadDirectories(item.parent, 'destination');

  // Highlight the item (optional - could add a visual indicator)
  setTimeout(() => {
    // You could add highlighting logic here if needed
    console.log('Navigated to:', item.parent, 'for item:', item.name);
  }, 500);
}

// Debounced search functionality
let searchTimeout = null;

// Add input event listener for debounced search (only if elements exist)
const searchQueryEl = document.getElementById('searchQuery');
const searchModalEl = document.getElementById('searchModal');

if (searchQueryEl) {
  searchQueryEl.addEventListener('input', function (e) {
    const query = e.target.value.trim();

    // Clear existing timeout
    if (searchTimeout) {
      clearTimeout(searchTimeout);
    }

    // Cancel current search if there is one
    cancelCurrentSearch();

    // Hide loading and results for new input
    document.getElementById('searchLoading').style.display = 'none';
    document.getElementById('searchResults').style.display = 'none';

    // Only search if query is at least 2 characters
    if (query.length >= 2) {
      searchTimeout = setTimeout(() => {
        performSearch();
      }, 500); // 500ms delay
    }
  });

  // Add Enter key support for search input
  searchQueryEl.addEventListener('keypress', function (e) {
    if (e.key === 'Enter') {
      // Clear the timeout and perform immediate search
      if (searchTimeout) {
        clearTimeout(searchTimeout);
        searchTimeout = null;
      }
      performSearch();
    }
  });
}

// Cancel search when modal is closed
if (searchModalEl) {
  searchModalEl.addEventListener('hidden.bs.modal', function () {
    cancelCurrentSearch();
    // Clear any pending search timeout
    if (searchTimeout) {
      clearTimeout(searchTimeout);
      searchTimeout = null;
    }
    // Hide loading and results when modal is closed
    const searchLoading = document.getElementById('searchLoading');
    const searchResults = document.getElementById('searchResults');
    if (searchLoading) searchLoading.style.display = 'none';
    if (searchResults) searchResults.style.display = 'none';
  });
}





// Helper function to show toast notifications

// Function to format timestamp in a user-friendly way
function formatTimestamp(date) {
  const now = new Date();
  const diffMs = now - date;
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) {
    return 'Just now';
  } else if (diffMins < 60) {
    return `${diffMins} minute${diffMins !== 1 ? 's' : ''} ago`;
  } else if (diffHours < 24) {
    return `${diffHours} hour${diffHours !== 1 ? 's' : ''} ago`;
  } else if (diffDays < 7) {
    return `${diffDays} day${diffDays !== 1 ? 's' : ''} ago`;
  } else {
    return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
}

// Function to search GCD for metadata and add to CBZ
function searchGCDMetadata(filePath, fileName) {
  console.log('GCD Search Called with:', { filePath, fileName });

  // Validate inputs
  if (!filePath || !fileName) {
    console.error('Invalid parameters:', { filePath, fileName });
    CLU.showToast('GCD Search Error', 'Missing file path or name', 'error');
    return;
  }

  if (!fileName.toLowerCase().match(/\.(cbz|cbr)$/)) {
    console.error('Invalid file type:', fileName);
    CLU.showToast('GCD Search Error', 'File must be CBZ or CBR format', 'error');
    return;
  }

  // Parse series name and issue from filename
  const nameWithoutExt = fileName.replace(/\.(cbz|cbr)$/i, '');

  // Auto-search without confirmation

  // Show a simple loading indicator
  const loadingToast = document.createElement('div');
  loadingToast.className = 'toast show position-fixed top-0 end-0 m-3';
  loadingToast.style.zIndex = '1200';
  loadingToast.innerHTML = `
        <div class="toast-header bg-primary text-white">
          <strong class="me-auto">GCD Search</strong>
          <small>Searching...</small>
        </div>
        <div class="toast-body">
          <div class="d-flex align-items-center">
            <div class="spinner-border spinner-border-sm me-2" role="status">
              <span class="visually-hidden">Loading...</span>
            </div>
            Searching GCD database for "${nameWithoutExt}"...
          </div>
        </div>
      `;
  document.body.appendChild(loadingToast);

  // Make request to backend
  const requestData = {
    file_path: filePath,
    file_name: fileName
  };
  console.log('GCD Search Request Data:', requestData);

  fetch('/search-gcd-metadata', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(requestData)
  })
    .then(response => {
      console.log('GCD Search Response Status:', response.status);
      if (!response.ok) {
        // For HTTP errors, get the error data and handle appropriately
        return response.json().then(errorData => {
          // Handle 404 as expected "not found" rather than error
          if (response.status === 404) {
            return { success: false, notFound: true, error: errorData.error || 'Issue not found in database' };
          }
          // For other HTTP errors, throw as before
          throw new Error(`HTTP ${response.status}: ${errorData.error || response.statusText}`);
        }).catch((jsonError) => {
          // If JSON parsing fails, throw the original HTTP error
          if (response.status === 404) {
            return { success: false, notFound: true, error: 'Issue not found in database' };
          }
          throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        });
      }
      return response.json();
    })
    .then(data => {
      console.log('GCD Search Response Data:', data);
      document.body.removeChild(loadingToast);

      if (data.success) {
        // Show success message
        const successToast = document.createElement('div');
        successToast.className = 'toast show position-fixed top-0 end-0 m-3';
        successToast.style.zIndex = '1200';
        successToast.innerHTML = `
            <div class="toast-header bg-success text-white">
              <strong class="me-auto">GCD Search</strong>
              <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
            </div>
            <div class="toast-body">
              Successfully added metadata to "${fileName}"<br>
              <small class="text-muted">Series: ${data.metadata?.series || 'Unknown'}<br>
              Issue: ${data.metadata?.issue || 'Unknown'}<br>
              Found ${data.matches_found || 0} potential matches</small>
            </div>
          `;
        document.body.appendChild(successToast);

        // Auto-remove after 5 seconds
        setTimeout(() => {
          if (document.body.contains(successToast)) {
            document.body.removeChild(successToast);
          }
        }, 5000);
      } else if (data.requires_selection) {
        // Show series selection modal
        showGCDSeriesSelectionModal(data, filePath, fileName);
      } else if (data.notFound) {
        // Show not found message as warning (not error)
        const warningToast = document.createElement('div');
        warningToast.className = 'toast show position-fixed top-0 end-0 m-3';
        warningToast.style.zIndex = '1200';
        warningToast.innerHTML = `
            <div class="toast-header bg-warning text-white">
              <strong class="me-auto">GCD Search</strong>
              <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
            </div>
            <div class="toast-body">
              ${data.error || 'Issue not found in GCD database'}
            </div>
          `;
        document.body.appendChild(warningToast);

        // Auto-remove after 5 seconds
        setTimeout(() => {
          if (document.body.contains(warningToast)) {
            document.body.removeChild(warningToast);
          }
        }, 5000);
      } else {
        // Show error message
        const errorToast = document.createElement('div');
        errorToast.className = 'toast show position-fixed top-0 end-0 m-3';
        errorToast.style.zIndex = '1200';
        errorToast.innerHTML = `
            <div class="toast-header bg-danger text-white">
              <strong class="me-auto">GCD Search Error</strong>
              <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
            </div>
            <div class="toast-body">
              Failed to add metadata: ${data.error || data.message || 'Server returned no error message'}
            </div>
          `;
        document.body.appendChild(errorToast);

        // Auto-remove after 8 seconds for errors
        setTimeout(() => {
          if (document.body.contains(errorToast)) {
            document.body.removeChild(errorToast);
          }
        }, 8000);
      }
    })
    .catch(error => {
      console.error('GCD Search Network Error:', error);
      document.body.removeChild(loadingToast);

      const errorToast = document.createElement('div');
      errorToast.className = 'toast show position-fixed top-0 end-0 m-3';
      errorToast.style.zIndex = '1200';
      errorToast.innerHTML = `
          <div class="toast-header bg-danger text-white">
            <strong class="me-auto">Network Error</strong>
            <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
          </div>
          <div class="toast-body">
            Network error: ${error.message}
          </div>
        `;
      document.body.appendChild(errorToast);

      setTimeout(() => {
        if (document.body.contains(errorToast)) {
          document.body.removeChild(errorToast);
        }
      }, 8000);
    });
}


// Function to sort GCD series results
function sortGCDSeries(sortBy) {
  // Update button states
  document.querySelectorAll('#sortBySeries, #sortByYear').forEach(btn => {
    btn.classList.remove('btn-secondary');
    btn.classList.add('btn-outline-secondary');
  });

  const activeButton = sortBy === 'series' ? document.getElementById('sortBySeries') : document.getElementById('sortByYear');
  activeButton.classList.remove('btn-outline-secondary');
  activeButton.classList.add('btn-secondary');

  // Sort the data
  let sortedData = [...currentSeriesData];

  if (sortBy === 'series') {
    sortedData.sort((a, b) => a.name.localeCompare(b.name));
  } else if (sortBy === 'year') {
    sortedData.sort((a, b) => {
      const yearA = a.year_began || 9999; // Put unknown years at the end
      const yearB = b.year_began || 9999;
      return yearA - yearB;
    });
  }

  // Re-render the series list - detect if this is directory mode or single file mode
  if (Array.isArray(currentIssueNumber)) {
    // Directory mode - currentIssueNumber contains the comicFiles array
    renderDirectorySeriesList(sortedData, currentFilePath, currentFileName, currentIssueNumber);
  } else {
    // Single file mode - currentIssueNumber is a number
    renderSeriesList(sortedData);
  }
}

// Function to render the series list
function renderSeriesList(seriesData) {
  const seriesList = document.getElementById('gcdSeriesList');
  seriesList.innerHTML = '';

  seriesData.forEach(series => {
    const seriesItem = document.createElement('div');
    seriesItem.className = 'list-group-item list-group-item-action d-flex justify-content-between align-items-start';
    seriesItem.style.cursor = 'pointer';

    const yearRange = series.year_began
      ? (series.year_ended ? `${series.year_began}-${series.year_ended}` : `${series.year_began}-ongoing`)
      : 'Unknown';

    seriesItem.innerHTML = `
          <div class="ms-2 me-auto">
            <div class="fw-bold">${series.name}</div>
            <small class="text-muted">Publisher: ${series.publisher_name || 'Unknown'}<br>Issue Count: ${series.issue_count || 'Unknown'}</small>
          </div>
          <span class="badge bg-primary rounded-pill">${yearRange}</span>
        `;

    seriesItem.addEventListener('click', () => {
      // Highlight selected item
      seriesList.querySelectorAll('.list-group-item').forEach(item => {
        item.classList.remove('active');
      });
      seriesItem.classList.add('active');

      // Call the backend with the selected series
      selectGCDSeries(currentFilePath, currentFileName, series.id, currentIssueNumber);
    });

    seriesList.appendChild(seriesItem);
  });
}

// Function to search GCD for all comics in a directory
function searchGCDMetadataForDirectory(directoryPath, directoryName) {
  // Auto-search without confirmation

  // Show loading indicator
  const loadingToast = document.createElement('div');
  loadingToast.className = 'toast show position-fixed top-0 end-0 m-3';
  loadingToast.style.zIndex = '1200';
  loadingToast.innerHTML = `
        <div class="toast-header bg-primary text-white">
          <strong class="me-auto">GCD Directory Search</strong>
          <small>Scanning...</small>
        </div>
        <div class="toast-body">
          <div class="d-flex align-items-center">
            <div class="spinner-border spinner-border-sm me-2" role="status">
              <span class="visually-hidden">Loading...</span>
            </div>
            Scanning directory for comic files...
          </div>
        </div>
      `;
  document.body.appendChild(loadingToast);

  // Get list of files in the directory
  fetch(`/list-directories?path=${encodeURIComponent(directoryPath)}`)
    .then(response => response.json())
    .then(data => {
      document.body.removeChild(loadingToast);

      if (data.error) {
        throw new Error(data.error);
      }

      // Filter for CBZ/CBR files
      const comicFiles = (data.files || []).filter(file => {
        const fileData = typeof file === 'object' ? file : { name: file };
        return fileData.name.toLowerCase().endsWith('.cbz') || fileData.name.toLowerCase().endsWith('.zip') || fileData.name.toLowerCase().endsWith('.cbr');
      });

      // Check for nested volume directories (e.g., v2015, v2016)
      const volumeDirectories = (data.directories || []).filter(dir => {
        const dirData = typeof dir === 'object' ? dir : { name: dir };
        return /^v\d{4}$/i.test(dirData.name); // Match v2015, v2016, etc.
      });

      // Approach 1: If current directory has no comics but has volume subdirectories, process all volumes
      if (comicFiles.length === 0 && volumeDirectories.length > 0) {
        CLU.showToast('Processing Volume Directories', `Found ${volumeDirectories.length} volume directories. Processing each separately...`, 'info');
        processNestedVolumeDirectories(directoryPath, directoryName, volumeDirectories);
        return;
      }

      // Approach 2: If current directory has comics, process normally
      if (comicFiles.length === 0) {
        CLU.showToast('No Comics Found', `No comic files (.cbz/.cbr) found in "${directoryName}"`, 'warning');
        return;
      }

      // Check if this is a volume directory (e.g., v2015) - Approach 2
      const volumeMatch = directoryName.match(/^v(\d{4})$/i);

      if (volumeMatch) {
        // This is a volume directory, get parent series name
        const pathParts = directoryPath.split('/');
        const parentDirectoryName = pathParts[pathParts.length - 2] || 'Unknown';
        const year = volumeMatch[1];

        CLU.showToast('Volume Directory Detected', `Processing volume ${directoryName} with parent series "${parentDirectoryName}"`, 'info');

        // Use parent directory name as series and pass volume info
        searchGCDForVolumeDirectory(directoryPath, directoryName, parentDirectoryName, year, comicFiles);
      } else {
        // Standard directory processing
        let seriesName = directoryName;

        // Clean up common directory naming patterns
        seriesName = seriesName.replace(/\s*\(\d{4}\).*$/, ''); // Remove (1994) and everything after
        seriesName = seriesName.replace(/\s*v\d+.*$/, ''); // Remove v1, v2 etc
        seriesName = seriesName.replace(/\s*-\s*complete.*$/i, ''); // Remove "- Complete" etc
        seriesName = seriesName.replace(/\s*\.INFO.*$/i, ''); // Remove .INFO

        // Start the GCD search for the directory
        searchGCDForDirectorySeries(directoryPath, directoryName, seriesName, comicFiles);
      }
    })
    .catch(error => {
      document.body.removeChild(loadingToast);
      CLU.showToast('Directory Scan Error', `Error scanning directory: ${error.message}`, 'error');
    });
}

// fetchAllMetadata – delegated to clu-metadata.js (CLU.fetchDirectoryMetadata)

// Function to process nested volume directories (e.g., Lady Killer/v2015, Lady Killer/v2016)
async function processNestedVolumeDirectories(parentPath, parentName, volumeDirectories) {
  // Extract series name from parent directory
  const seriesName = parentName;

  // Create progress modal for processing multiple volumes
  const progressModal = document.createElement('div');
  progressModal.className = 'modal fade';
  progressModal.setAttribute('data-bs-backdrop', 'static');
  progressModal.innerHTML = `
        <div class="modal-dialog">
          <div class="modal-content">
            <div class="modal-header">
              <h5 class="modal-title">Processing Volume Directories: ${seriesName}</h5>
            </div>
            <div class="modal-body">
              <div class="mb-3">
                <div class="d-flex justify-content-between">
                  <span>Progress:</span>
                  <span id="volumeProgressText">0 / ${volumeDirectories.length}</span>
                </div>
                <div class="progress">
                  <div id="volumeProgressBar" class="progress-bar progress-bar-striped progress-bar-animated"
                       style="width: 0%" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100"></div>
                </div>
              </div>
              <div id="volumeCurrentDir" class="text-muted small">Preparing...</div>
              <div id="volumeResults" class="mt-3 small" style="max-height: 200px; overflow-y: auto;">
                <!-- Results will be added here -->
              </div>
            </div>
            <div class="modal-footer">
              <button type="button" id="volumeCloseBtn" class="btn btn-secondary" disabled>Close</button>
              <button type="button" id="volumeCancelBtn" class="btn btn-danger">Cancel</button>
            </div>
          </div>
        </div>
      `;
  document.body.appendChild(progressModal);

  const volumeModal = new bootstrap.Modal(progressModal);
  volumeModal.show();

  let processedCount = 0;
  let successCount = 0;
  let errorCount = 0;
  let cancelled = false;

  document.getElementById('volumeCancelBtn').onclick = () => {
    cancelled = true;
    document.getElementById('volumeCancelBtn').disabled = true;
    document.getElementById('volumeCurrentDir').textContent = 'Cancelling...';
  };

  document.getElementById('volumeCloseBtn').onclick = () => {
    volumeModal.hide();
    document.body.removeChild(progressModal);
  };

  // Process each volume directory
  for (let i = 0; i < volumeDirectories.length && !cancelled; i++) {
    const volumeDir = volumeDirectories[i];
    const volumeName = volumeDir.name || volumeDir;
    const volumePath = parentPath + '/' + volumeName;

    // Extract year from volume directory name (e.g., v2015 -> 2015)
    const yearMatch = volumeName.match(/^v(\d{4})$/i);
    const year = yearMatch ? yearMatch[1] : null;

    document.getElementById('volumeCurrentDir').textContent = `Processing: ${volumeName}`;

    try {
      // Get files in this volume directory
      const response = await fetch(`/list-directories?path=${encodeURIComponent(volumePath)}`);
      const data = await response.json();

      if (data.error) {
        throw new Error(data.error);
      }

      const comicFiles = (data.files || []).filter(file => {
        const fileData = typeof file === 'object' ? file : { name: file };
        return fileData.name.toLowerCase().endsWith('.cbz') || fileData.name.toLowerCase().endsWith('.zip') || fileData.name.toLowerCase().endsWith('.cbr');
      });

      if (comicFiles.length === 0) {
        throw new Error(`No comic files found in ${volumeName}`);
      }

      // Search for this specific year's series
      const searchSeriesName = year ? `${seriesName} (${year})` : seriesName;

      // Process this volume directory
      await processVolumeDirectory(volumePath, volumeName, searchSeriesName, comicFiles, year);

      successCount++;
      const resultsDiv = document.getElementById('volumeResults');
      const resultItem = document.createElement('div');
      resultItem.className = 'text-success';
      resultItem.innerHTML = `✓ ${volumeName} - ${comicFiles.length} files processed`;
      resultsDiv.appendChild(resultItem);

    } catch (error) {
      errorCount++;
      const resultsDiv = document.getElementById('volumeResults');
      const resultItem = document.createElement('div');
      resultItem.className = 'text-danger';
      resultItem.innerHTML = `✗ ${volumeName} - ${error.message}`;
      resultsDiv.appendChild(resultItem);
    }

    processedCount++;

    // Update progress
    document.getElementById('volumeProgressText').textContent = `${processedCount} / ${volumeDirectories.length}`;
    const progressPercent = Math.floor((processedCount / volumeDirectories.length) * 100);
    document.getElementById('volumeProgressBar').style.width = progressPercent + '%';
    document.getElementById('volumeProgressBar').setAttribute('aria-valuenow', progressPercent);

    // Scroll results to bottom
    const resultsDiv = document.getElementById('volumeResults');
    resultsDiv.scrollTop = resultsDiv.scrollHeight;
  }

  // Finished processing
  document.getElementById('volumeCloseBtn').disabled = false;
  document.getElementById('volumeCancelBtn').style.display = 'none';
  document.getElementById('volumeCurrentDir').textContent = cancelled
    ? `Cancelled after ${processedCount} volumes`
    : `Complete! Processed ${processedCount} volumes (${successCount} success, ${errorCount} errors)`;

  // Auto-close modal after 2 seconds if not cancelled
  if (!cancelled) {
    setTimeout(() => {
      volumeModal.hide();
    }, 2000);
  }
}

// Function to process a single volume directory
async function processVolumeDirectory(volumePath, volumeName, seriesName, comicFiles, year) {
  return new Promise((resolve, reject) => {
    // Use the first file for the search
    const firstFile = comicFiles[0];
    const firstFileName = firstFile.name || firstFile;
    const firstFilePath = volumePath + '/' + firstFileName;

    fetch('/search-gcd-metadata', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        file_path: firstFilePath,
        file_name: firstFileName,
        is_directory_search: true,
        directory_path: volumePath,
        directory_name: volumeName,
        total_files: comicFiles.length,
        parent_series_name: seriesName,
        volume_year: year
      })
    })
      .then(response => response.json())
      .then(data => {
        if (data.success) {
          if (data.series_id) {
            // Auto-process with found series
            processBulkGCDMetadata(volumePath, volumeName, data.series_id, comicFiles);
            resolve();
          } else {
            reject(new Error('No series ID returned'));
          }
        } else {
          reject(new Error(data.error || 'Search failed'));
        }
      })
      .catch(error => {
        reject(error);
      });
  });
}

// Function to search GCD for a volume directory using parent series name
function searchGCDForVolumeDirectory(directoryPath, directoryName, parentSeriesName, year, comicFiles) {
  // Use first comic file for the search, but flag it as volume directory search
  const firstFile = comicFiles[0];
  const firstFileName = firstFile.name || firstFile;
  const firstFilePath = directoryPath + '/' + firstFileName;

  fetch('/search-gcd-metadata', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      file_path: firstFilePath,
      file_name: firstFileName,
      is_directory_search: true,
      directory_path: directoryPath,
      directory_name: directoryName,
      total_files: comicFiles.length,
      parent_series_name: parentSeriesName,
      volume_year: year
    })
  })
    .then(response => response.json())
    .then(data => {
      if (data.success) {
        // For volume directory search with exact match, proceed with bulk processing
        if (data.series_id) {
          CLU.showToast('Exact Match Found', `Found exact match for "${parentSeriesName} (${year})". Processing all files in volume...`, 'success');
          // Start bulk processing immediately with the found series
          processBulkGCDMetadata(directoryPath, directoryName, data.series_id, comicFiles);
        } else {
          CLU.showToast('Direct Match Found', `Found exact match for "${parentSeriesName} (${year})". Consider using individual file search instead.`, 'info');
        }
      } else if (data.requires_selection) {
        // Show series selection modal for volume processing
        showGCDDirectorySeriesSelectionModal(data, directoryPath, directoryName, comicFiles);
      } else {
        // Show error or no results
        CLU.showToast('No Series Found', data.error || `No series found matching "${parentSeriesName} (${year})" in GCD database`, 'error');
      }
    })
    .catch(error => {
      CLU.showToast('Search Error', `Error searching GCD database: ${error.message}`, 'error');
    });
}

// Function to search GCD for directory series and show selection modal
function searchGCDForDirectorySeries(directoryPath, directoryName, seriesName, comicFiles) {
  // Use first comic file for the search, but flag it as directory search
  const firstFile = comicFiles[0];
  const firstFileName = firstFile.name || firstFile;
  const firstFilePath = directoryPath + '/' + firstFileName;

  fetch('/search-gcd-metadata', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      file_path: firstFilePath,
      file_name: firstFileName,
      is_directory_search: true,
      directory_path: directoryPath,
      directory_name: directoryName,
      total_files: comicFiles.length
    })
  })
    .then(response => response.json())
    .then(data => {
      if (data.success) {
        // For directory search with exact match, proceed with bulk processing
        if (data.series_id) {
          CLU.showToast('Exact Match Found', `Found exact match for "${seriesName}". Processing all files in directory...`, 'success');
          // Start bulk processing immediately with the found series
          processBulkGCDMetadata(directoryPath, directoryName, data.series_id, comicFiles);
        } else {
          CLU.showToast('Direct Match Found', `Found exact match for "${seriesName}". Consider using individual file search instead.`, 'info');
        }
      } else if (data.requires_selection) {
        // Show series selection modal for directory processing
        // Use the directory information from the server response if available
        const actualDirectoryPath = data.directory_path || directoryPath;
        const actualDirectoryName = data.directory_name || directoryName;
        showGCDDirectorySeriesSelectionModal(data, actualDirectoryPath, actualDirectoryName, comicFiles);
      } else {
        // Show error or no results
        CLU.showToast('No Series Found', data.error || `No series found matching "${seriesName}" in GCD database`, 'error');
      }
    })
    .catch(error => {
      CLU.showToast('Search Error', `Error searching GCD database: ${error.message}`, 'error');
    });
}

// Function to render series list for directory processing
function renderDirectorySeriesList(seriesData, directoryPath, directoryName, comicFiles) {
  const seriesList = document.getElementById('gcdSeriesList');
  seriesList.innerHTML = '';

  seriesData.forEach(series => {
    const seriesItem = document.createElement('div');
    seriesItem.className = 'list-group-item list-group-item-action d-flex justify-content-between align-items-start';
    seriesItem.style.cursor = 'pointer';

    const yearRange = series.year_began
      ? (series.year_ended ? `${series.year_began}-${series.year_ended}` : `${series.year_began}-ongoing`)
      : 'Unknown';

    seriesItem.innerHTML = `
          <div class="ms-2 me-auto">
            <div class="fw-bold">${series.name}</div>
            <small class="text-muted">Publisher: ${series.publisher_name || 'Unknown'}<br>Issue Count: ${series.issue_count || 'Unknown'}</small>
          </div>
          <span class="badge bg-primary rounded-pill">${yearRange}</span>
        `;

    seriesItem.addEventListener('click', () => {
      // Highlight selected item
      seriesList.querySelectorAll('.list-group-item').forEach(item => {
        item.classList.remove('active');
      });
      seriesItem.classList.add('active');

      // Call the bulk processing function for directory
      processBulkGCDMetadata(directoryPath, directoryName, series.id, comicFiles);
    });

    seriesList.appendChild(seriesItem);
  });
}

// Function to show GCD series selection modal for directory processing
function showGCDDirectorySeriesSelectionModal(data, directoryPath, directoryName, comicFiles) {
  // Populate the parsed filename information (using directory name)
  document.getElementById('gcdParsedSeries').textContent = data.parsed_filename.series_name;
  document.getElementById('gcdParsedIssue').textContent = `Directory (${comicFiles.length} files)`;
  document.getElementById('gcdParsedYear').textContent = data.parsed_filename.year || 'Unknown';

  // Store the data globally for sorting
  currentSeriesData = data.possible_matches;
  // Store directory-specific data in global variables for custom rendering
  currentFilePath = directoryPath;
  currentFileName = directoryName;
  currentIssueNumber = comicFiles;

  // Reset sort buttons
  document.querySelectorAll('#sortBySeries, #sortByYear').forEach(btn => {
    btn.classList.remove('btn-secondary');
    btn.classList.add('btn-outline-secondary');
  });

  // Render the series list with initial order (directory mode)
  renderDirectorySeriesList(currentSeriesData, directoryPath, directoryName, comicFiles);

  // Update modal title for directory processing
  document.getElementById('gcdSeriesModalLabel').textContent = `Select Series for Directory: ${directoryName}`;

  // Show the modal
  const modal = new bootstrap.Modal(document.getElementById('gcdSeriesModal'));
  modal.show();
}

// Function to process bulk GCD metadata for a directory
function processBulkGCDMetadata(directoryPath, directoryName, seriesId, comicFiles) {
  const modal = bootstrap.Modal.getInstance(document.getElementById('gcdSeriesModal'));
  if (modal) {
    modal.hide();
  }

  // Create progress modal
  const progressModal = document.createElement('div');
  progressModal.className = 'modal fade';
  progressModal.setAttribute('data-bs-backdrop', 'static');
  progressModal.innerHTML = `
        <div class="modal-dialog">
          <div class="modal-content">
            <div class="modal-header">
              <h5 class="modal-title">Processing Directory: ${directoryName}</h5>
            </div>
            <div class="modal-body">
              <div class="mb-3">
                <div class="d-flex justify-content-between">
                  <span>Progress:</span>
                  <span id="bulkProgressText">0 / ${comicFiles.length}</span>
                </div>
                <div class="progress">
                  <div id="bulkProgressBar" class="progress-bar progress-bar-striped progress-bar-animated"
                       style="width: 0%" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100"></div>
                </div>
              </div>
              <div id="bulkCurrentFile" class="text-muted small">Preparing...</div>
              <div id="bulkResults" class="mt-3 small" style="max-height: 200px; overflow-y: auto;">
                <!-- Results will be added here -->
              </div>
            </div>
            <div class="modal-footer">
              <button type="button" id="bulkCloseBtn" class="btn btn-secondary" disabled>Close</button>
              <button type="button" id="bulkCancelBtn" class="btn btn-danger">Cancel</button>
            </div>
          </div>
        </div>
      `;
  document.body.appendChild(progressModal);

  const bulkModal = new bootstrap.Modal(progressModal);
  bulkModal.show();

  // Start processing files
  let processedCount = 0;
  let successCount = 0;
  let errorCount = 0;
  let cancelled = false;
  let failedFiles = []; // Track files that failed to get metadata

  document.getElementById('bulkCancelBtn').onclick = () => {
    cancelled = true;
    document.getElementById('bulkCancelBtn').disabled = true;
    document.getElementById('bulkCurrentFile').textContent = 'Cancelling...';
  };

  // Add close button functionality
  document.getElementById('bulkCloseBtn').onclick = () => {
    bulkModal.hide();
  };

  async function processNextFile(index) {
    if (cancelled || index >= comicFiles.length) {
      // Initial processing complete - check for failed files
      if (!cancelled && failedFiles.length > 0) {
        document.getElementById('bulkCurrentFile').textContent = `Searching for unmatched files using filenames...`;
        // Start secondary search for failed files
        await processFailedFiles();
      } else {
        // Processing complete or cancelled
        document.getElementById('bulkCloseBtn').disabled = false;
        document.getElementById('bulkCancelBtn').style.display = 'none';
        document.getElementById('bulkCurrentFile').textContent = cancelled
          ? `Cancelled after ${processedCount} files`
          : `Complete! Processed ${processedCount} files (${successCount} success, ${errorCount} errors)`;

        // Auto-close modal after 2 seconds if not cancelled
        if (!cancelled) {
          setTimeout(() => {
            bulkModal.hide();
          }, 2000);
        }
      }
      return;
    }

    const file = comicFiles[index];
    const fileName = file.name || file;
    const filePath = directoryPath + '/' + fileName;

    // Update progress - show current processing
    document.getElementById('bulkCurrentFile').textContent = `Processing: ${fileName}`;
    document.getElementById('bulkProgressText').textContent = `${processedCount} / ${comicFiles.length}`;

    const progressPercent = Math.floor((processedCount / comicFiles.length) * 100);
    document.getElementById('bulkProgressBar').style.width = progressPercent + '%';
    document.getElementById('bulkProgressBar').setAttribute('aria-valuenow', progressPercent);

    try {
      // Parse issue number from filename - look for common patterns
      let issueNumber = null;

      // Try multiple patterns to extract issue number
      const patterns = [
        /(?:^|\s)(\d{1,4})(?:\s*\(|\s*$|\s*\.)/,     // Standard: "Series 123 (year)" or "Series 123.cbz"
        /(?:^|\s)#(\d{1,4})(?:\s|$)/,                 // Hash prefix: "Series #123"
        /(?:issue\s*)(\d{1,4})/i,                     // Issue prefix: "Series Issue 123"
        /(?:no\.?\s*)(\d{1,4})/i,                     // No. prefix: "Series No. 123"
        /(?:vol\.\s*\d+\s+)(\d{1,4})/i                // Volume and issue: "Series Vol. 1 123"
      ];

      for (const pattern of patterns) {
        const match = fileName.match(pattern);
        if (match) {
          issueNumber = parseInt(match[1]);
          break;
        }
      }

      // If no issue number found, skip this file with a clear error
      if (issueNumber === null) {
        throw new Error(`Could not parse issue number from filename: ${fileName}`);
      }

      // First validate that this issue number exists in the series
      const validationResponse = await fetch('/validate-gcd-issue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          series_id: seriesId,
          issue_number: issueNumber
        })
      });

      const validationResult = await validationResponse.json();
      if (!validationResult.success) {
        throw new Error(`Issue #${issueNumber} not found in series (parsed from filename)`);
      }

      const response = await fetch('/search-gcd-metadata-with-selection', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          file_path: filePath,
          file_name: fileName,
          series_id: seriesId,
          issue_number: issueNumber
        })
      });

      const result = await response.json();

      const resultsDiv = document.getElementById('bulkResults');
      const resultItem = document.createElement('div');

      if (result.success) {
        if (result.skipped) {
          // File was skipped because it already has metadata
          successCount++;
          resultItem.className = 'text-info';
          resultItem.innerHTML = `✓ ${fileName} - Skipped XML Present`;
        } else {
          // File was successfully processed
          successCount++;
          resultItem.className = 'text-success';
          resultItem.innerHTML = `✓ ${fileName} - Issue #${result.metadata.issue}`;
        }
      } else {
        errorCount++;
        resultItem.className = 'text-danger';
        resultItem.innerHTML = `✗ ${fileName} - ${result.error}`;
        // Track failed file for secondary search
        failedFiles.push({ fileName, filePath, error: result.error });
      }

      resultsDiv.appendChild(resultItem);
      resultsDiv.scrollTop = resultsDiv.scrollHeight;

    } catch (error) {
      errorCount++;
      const resultsDiv = document.getElementById('bulkResults');
      const resultItem = document.createElement('div');
      resultItem.className = 'text-danger';
      // Show actual error message instead of generic "Network error"
      const errorMsg = error.message || 'Network error';
      resultItem.innerHTML = `✗ ${fileName} - ${errorMsg}`;
      resultsDiv.appendChild(resultItem);
      // Track failed file for secondary search
      failedFiles.push({ fileName, filePath, error: errorMsg });
      // Log error to console for debugging
      console.error(`Error processing ${fileName}:`, error);
    }

    processedCount++;

    // Update progress after completing the file
    document.getElementById('bulkProgressText').textContent = `${processedCount} / ${comicFiles.length}`;
    const newProgressPercent = Math.floor((processedCount / comicFiles.length) * 100);
    document.getElementById('bulkProgressBar').style.width = newProgressPercent + '%';
    document.getElementById('bulkProgressBar').setAttribute('aria-valuenow', newProgressPercent);

    // Process next file after a short delay
    setTimeout(() => processNextFile(index + 1), 100);
  }

  // Function to process failed files using filename-based search
  async function processFailedFiles() {
    let secondarySuccessCount = 0;
    let secondaryProcessedCount = 0;
    const totalFailed = failedFiles.length;

    document.getElementById('bulkCurrentFile').innerHTML = `
          <div class="mb-2">Secondary search phase: Using filename-based search for unmatched files</div>
          <div class="text-muted small">Processing ${totalFailed} unmatched files...</div>
        `;

    // Add a separator in results
    const resultsDiv = document.getElementById('bulkResults');
    const separator = document.createElement('div');
    separator.className = 'border-top mt-2 pt-2 mb-2 text-muted small';
    separator.innerHTML = '<strong>Secondary Search (by filename):</strong>';
    resultsDiv.appendChild(separator);

    for (let i = 0; i < failedFiles.length && !cancelled; i++) {
      const failedFile = failedFiles[i];

      document.getElementById('bulkCurrentFile').innerHTML = `
            <div class="mb-2">Secondary search phase: Using filename-based search</div>
            <div class="text-muted small">Processing: ${failedFile.fileName} (${i + 1}/${totalFailed})</div>
          `;

      try {
        // Search using just the filename (not tied to the original series)
        const response = await fetch('/search-gcd-metadata', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            file_path: failedFile.filePath,
            file_name: failedFile.fileName
          })
        });

        const result = await response.json();
        const resultItem = document.createElement('div');

        if (result.success && result.requires_selection) {
          // Found potential matches - show as warning and let user handle manually
          resultItem.className = 'text-warning';
          resultItem.innerHTML = `⚠ ${failedFile.fileName} - Found ${result.possible_matches?.length || 0} potential matches, requires manual selection`;
        } else if (result.success && result.series_id) {
          // Direct match found
          secondarySuccessCount++;
          resultItem.className = 'text-success';
          resultItem.innerHTML = `✓ ${failedFile.fileName} - Found direct match: ${result.parsed_filename?.series_name || 'Unknown Series'}`;
        } else {
          // Still no match
          resultItem.className = 'text-danger';
          resultItem.innerHTML = `✗ ${failedFile.fileName} - No match found in secondary search`;
        }

        resultsDiv.appendChild(resultItem);
        resultsDiv.scrollTop = resultsDiv.scrollHeight;

      } catch (error) {
        const resultItem = document.createElement('div');
        resultItem.className = 'text-danger';
        resultItem.innerHTML = `✗ ${failedFile.fileName} - Secondary search network error`;
        resultsDiv.appendChild(resultItem);
      }

      secondaryProcessedCount++;

      // Small delay to prevent overwhelming the server
      await new Promise(resolve => setTimeout(resolve, 200));
    }

    // Final completion
    document.getElementById('bulkCloseBtn').disabled = false;
    document.getElementById('bulkCancelBtn').style.display = 'none';

    const totalSuccessCount = successCount + secondarySuccessCount;
    const totalErrorCount = errorCount - secondarySuccessCount; // Adjust error count for secondary successes

    document.getElementById('bulkCurrentFile').innerHTML = `
          <div><strong>Complete!</strong> Processed ${processedCount} files</div>
          <div class="text-muted small">
            Primary: ${successCount} success, ${errorCount} failed<br>
            Secondary: ${secondarySuccessCount} additional matches found<br>
            <strong>Total: ${totalSuccessCount} success, ${Math.max(0, totalErrorCount)} still unmatched</strong>
          </div>
        `;

    // Auto-close modal after 2 seconds
    setTimeout(() => {
      bulkModal.hide();
    }, 2000);
  }

  // Start processing
  processNextFile(0);

  // Clean up modal when closed
  progressModal.addEventListener('hidden.bs.modal', () => {
    document.body.removeChild(progressModal);
  });
}

// Function to show GCD series selection modal
function showGCDSeriesSelectionModal(data, filePath, fileName) {
  // Populate the parsed filename information
  document.getElementById('gcdParsedSeries').textContent = data.parsed_filename.series_name;
  document.getElementById('gcdParsedIssue').textContent = data.parsed_filename.issue_number;
  document.getElementById('gcdParsedYear').textContent = data.parsed_filename.year || 'Unknown';

  // Store the data globally for sorting
  currentSeriesData = data.possible_matches;
  currentFilePath = filePath;
  currentFileName = fileName;
  currentIssueNumber = data.parsed_filename.issue_number;

  // Reset sort buttons
  document.querySelectorAll('#sortBySeries, #sortByYear').forEach(btn => {
    btn.classList.remove('btn-secondary');
    btn.classList.add('btn-outline-secondary');
  });

  // Render the series list with initial order
  renderSeriesList(currentSeriesData);

  // Show the modal
  const modal = new bootstrap.Modal(document.getElementById('gcdSeriesModal'));
  modal.show();
}

// Function to handle series selection
function selectGCDSeries(filePath, fileName, seriesId, issueNumber) {
  // Show loading indicator
  const modal = bootstrap.Modal.getInstance(document.getElementById('gcdSeriesModal'));

  // Create loading toast
  const loadingToast = document.createElement('div');
  loadingToast.className = 'toast show position-fixed top-0 end-0 m-3';
  loadingToast.style.zIndex = '1200';
  loadingToast.innerHTML = `
        <div class="toast-header bg-primary text-white">
          <strong class="me-auto">GCD Search</strong>
          <small>Processing...</small>
        </div>
        <div class="toast-body">
          <div class="d-flex align-items-center">
            <div class="spinner-border spinner-border-sm me-2" role="status">
              <span class="visually-hidden">Loading...</span>
            </div>
            Adding metadata from selected series...
          </div>
        </div>
      `;
  document.body.appendChild(loadingToast);

  // Close the modal (if it exists)
  if (modal) {
    modal.hide();
  }

  // Call the backend endpoint
  fetch('/search-gcd-metadata-with-selection', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      file_path: filePath,
      file_name: fileName,
      series_id: seriesId,
      issue_number: issueNumber
    })
  })
    .then(response => response.json())
    .then(data => {
      document.body.removeChild(loadingToast);

      if (data.success) {
        // Show success message
        const successToast = document.createElement('div');
        successToast.className = 'toast show position-fixed top-0 end-0 m-3';
        successToast.style.zIndex = '1200';
        successToast.innerHTML = `
            <div class="toast-header bg-success text-white">
              <strong class="me-auto">GCD Search Success</strong>
              <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
            </div>
            <div class="toast-body">
              Successfully added metadata to "${fileName}"<br>
              <small class="text-muted">Series: ${data.metadata?.series || 'Unknown'}<br>
              Issue: ${data.metadata?.issue || 'Unknown'}<br>
              Title: ${data.metadata?.title || 'Unknown'}<br>
              Publisher: ${data.metadata?.publisher || 'Unknown'}</small>
            </div>
          `;
        document.body.appendChild(successToast);

        // Auto-remove after 5 seconds
        setTimeout(() => {
          if (document.body.contains(successToast)) {
            document.body.removeChild(successToast);
          }
        }, 5000);
      } else {
        // Show error message
        const errorToast = document.createElement('div');
        errorToast.className = 'toast show position-fixed top-0 end-0 m-3';
        errorToast.style.zIndex = '1200';
        errorToast.innerHTML = `
            <div class="toast-header bg-danger text-white">
              <strong class="me-auto">GCD Search Error</strong>
              <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
            </div>
            <div class="toast-body">
              Failed to add metadata: ${data.error || data.message || 'Server returned no error message'}
            </div>
          `;
        document.body.appendChild(errorToast);

        // Auto-remove after 8 seconds for errors
        setTimeout(() => {
          if (document.body.contains(errorToast)) {
            document.body.removeChild(errorToast);
          }
        }, 8000);
      }
    })
    .catch(error => {
      console.error('GCD Search Network Error:', error);
      document.body.removeChild(loadingToast);

      const errorToast = document.createElement('div');
      errorToast.className = 'toast show position-fixed top-0 end-0 m-3';
      errorToast.style.zIndex = '1200';
      errorToast.innerHTML = `
          <div class="toast-header bg-danger text-white">
            <strong class="me-auto">Network Error</strong>
            <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
          </div>
          <div class="toast-body">
            Network error: ${error.message}
          </div>
        `;
      document.body.appendChild(errorToast);

      setTimeout(() => {
        if (document.body.contains(errorToast)) {
          document.body.removeChild(errorToast);
        }
      }, 8000);
    });
}


// Function to sort GCD series results
function sortGCDSeries(sortBy) {
  // Update button states
  document.querySelectorAll('#sortBySeries, #sortByYear').forEach(btn => {
    btn.classList.remove('btn-secondary');
    btn.classList.add('btn-outline-secondary');
  });

  const activeButton = sortBy === 'series' ? document.getElementById('sortBySeries') : document.getElementById('sortByYear');
  activeButton.classList.remove('btn-outline-secondary');
  activeButton.classList.add('btn-secondary');

  // Sort the data
  let sortedData = [...currentSeriesData];

  if (sortBy === 'series') {
    sortedData.sort((a, b) => a.name.localeCompare(b.name));
  } else if (sortBy === 'year') {
    sortedData.sort((a, b) => {
      const yearA = a.year_began || 9999; // Put unknown years at the end
      const yearB = b.year_began || 9999;
      return yearA - yearB;
    });
  }

  // Re-render the series list - detect if this is directory mode or single file mode
  if (Array.isArray(currentIssueNumber)) {
    // Directory mode - currentIssueNumber contains the comicFiles array
    renderDirectorySeriesList(sortedData, currentFilePath, currentFileName, currentIssueNumber);
  } else {
    // Single file mode - currentIssueNumber is a number
    renderSeriesList(sortedData);
  }
}

// Function to render the series list
function renderSeriesList(seriesData) {
  const seriesList = document.getElementById('gcdSeriesList');
  seriesList.innerHTML = '';

  seriesData.forEach(series => {
    const seriesItem = document.createElement('div');
    seriesItem.className = 'list-group-item list-group-item-action d-flex justify-content-between align-items-start';
    seriesItem.style.cursor = 'pointer';

    const yearRange = series.year_began
      ? (series.year_ended ? `${series.year_began}-${series.year_ended}` : `${series.year_began}-ongoing`)
      : 'Unknown';

    seriesItem.innerHTML = `
          <div class="ms-2 me-auto">
            <div class="fw-bold">${series.name}</div>
            <small class="text-muted">Publisher: ${series.publisher_name || 'Unknown'}<br>Issue Count: ${series.issue_count || 'Unknown'}</small>
          </div>
          <div class="text-end">
            <span class="badge bg-primary rounded-pill">${yearRange}</span><br>
            <span class="badge bg-dark rounded-pill">${series.language}</span>
          </div>
        `;

    seriesItem.addEventListener('click', () => {
      // Highlight selected item
      seriesList.querySelectorAll('.list-group-item').forEach(item => {
        item.classList.remove('active');
      });
      seriesItem.classList.add('active');

      // Call the backend with the selected series
      selectGCDSeries(currentFilePath, currentFileName, series.id, currentIssueNumber);
    });

    seriesList.appendChild(seriesItem);
  });
}



// Helper function to remove a file from the UI after it's been moved
function removeFileFromUI(filePath) {
  console.log('Removing file from UI:', filePath);

  // Check both source and destination lists
  const sourceLi = document.querySelector(`#source-list li[data-fullpath="${filePath}"]`);
  const destLi = document.querySelector(`#destination-list li[data-fullpath="${filePath}"]`);

  const itemToRemove = sourceLi || destLi;
  const panel = sourceLi ? 'source' : 'destination';

  if (itemToRemove) {
    console.log(`Found item to remove in ${panel} panel`);

    // Add fade-out animation
    itemToRemove.classList.add('deleting');

    // Remove after animation
    setTimeout(() => {
      itemToRemove.remove();
      console.log('File removed from UI');

      // After removal, check if we need to show the drop target in destination panel
      if (panel === 'destination') {
        let container = document.getElementById("destination-list");
        let remainingItems = container.querySelectorAll("li:not(.drop-target-item)");

        // If no items left (excluding drop target), add the drop target
        if (remainingItems.length === 0) {
          createDropTargetItem(container, currentDestinationPath, panel);
        }
      }

      // Update file count tracker
      if (panel === 'source') {
        trackFileRemovalForRename('source');
      } else {
        trackFileRemovalForRename('destination');
      }
    }, 200); // Match the CSS transition duration
  } else {
    console.log('Item not found in UI, may already be removed or in a different location');
  }
}

// ComicVine metadata search function
function searchComicVineMetadata(filePath, fileName) {
  console.log('ComicVine Search Called with:', { filePath, fileName });

  // Validate inputs
  if (!filePath || !fileName) {
    console.error('Invalid parameters:', { filePath, fileName });
    CLU.showToast('ComicVine Search Error', 'Missing file path or name', 'error');
    return;
  }

  if (!fileName.toLowerCase().match(/\.(cbz|cbr)$/)) {
    console.error('Invalid file type:', fileName);
    CLU.showToast('ComicVine Search Error', 'File must be CBZ or CBR format', 'error');
    return;
  }

  // Parse series name and issue from filename
  const nameWithoutExt = fileName.replace(/\.(cbz|cbr)$/i, '');

  // Show a simple loading indicator
  const loadingToast = document.createElement('div');
  loadingToast.className = 'toast show position-fixed top-0 end-0 m-3';
  loadingToast.style.zIndex = '1200';
  loadingToast.innerHTML = `
    <div class="toast-header bg-success text-white">
      <strong class="me-auto">ComicVine Search</strong>
      <small>Searching...</small>
    </div>
    <div class="toast-body">
      <div class="d-flex align-items-center">
        <div class="spinner-border spinner-border-sm me-2" role="status">
          <span class="visually-hidden">Loading...</span>
        </div>
        Searching ComicVine for "${nameWithoutExt}"...
      </div>
    </div>
  `;
  document.body.appendChild(loadingToast);

  // Make request to backend
  const requestData = {
    file_path: filePath,
    file_name: fileName
  };
  console.log('ComicVine Search Request Data:', requestData);

  fetch('/search-comicvine-metadata', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(requestData)
  })
    .then(response => {
      console.log('ComicVine Search Response Status:', response.status);
      if (!response.ok) {
        return response.json().then(errorData => {
          if (response.status === 404) {
            return { success: false, notFound: true, error: errorData.error || 'Issue not found in ComicVine' };
          }
          throw new Error('HTTP error: ' + (errorData.error || response.statusText));
        }).catch((jsonError) => {
          if (response.status === 404) {
            return { success: false, notFound: true, error: 'Issue not found in ComicVine' };
          }
          throw new Error('HTTP error: ' + response.statusText);
        });
      }
      return response.json();
    })
    .then(data => {
      console.log('ComicVine Search Response Data:', data);
      document.body.removeChild(loadingToast);

      if (data.success) {
        // Show success message with cover image if available
        const successToast = document.createElement('div');
        successToast.className = 'toast show position-fixed top-0 end-0 m-3';
        successToast.style.zIndex = '1200';
        let imageHtml = data.image_url ? ('<img src="' + data.image_url + '" class="img-thumbnail mt-2" style="max-width: 100px;" alt="Cover">') : '';
        successToast.innerHTML = `
        <div class="toast-header bg-success text-white">
          <strong class="me-auto">ComicVine Search</strong>
          <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
        </div>
        <div class="toast-body">
          Successfully added metadata to "${fileName}"<br>
          <small class="text-muted">Series: ${data.metadata && data.metadata.Series || 'Unknown'}<br>
          Issue: ${data.metadata && data.metadata.Number || 'Unknown'}</small>
          ${imageHtml}
        </div>
      `;
        document.body.appendChild(successToast);

        setTimeout(() => {
          if (document.body.contains(successToast)) {
            document.body.removeChild(successToast);
          }
        }, 5000);

        // Remove file from UI if it was moved to a new location
        if (data.moved) {
          console.log('File was moved, removing from current panel');
          removeFileFromUI(filePath);
        }

        // Handle file rename if configured
        if (data.metadata && data.metadata.Series) {
          const actualFilePath = data.moved ? data.new_file_path : filePath;
          promptRenameAfterMetadata(actualFilePath, fileName, data.metadata, data.rename_config);
        }
      } else if (data.requires_selection) {
        // Show volume selection modal
        showComicVineVolumeSelectionModal(data, filePath, fileName);
      } else if (data.notFound) {
        // Show not found message as warning
        const warningToast = document.createElement('div');
        warningToast.className = 'toast show position-fixed top-0 end-0 m-3';
        warningToast.style.zIndex = '1200';
        warningToast.innerHTML = `
        <div class="toast-header bg-warning text-white">
          <strong class="me-auto">ComicVine Search</strong>
          <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
        </div>
        <div class="toast-body">
          ${data.error || 'Issue not found in ComicVine'}
        </div>
      `;
        document.body.appendChild(warningToast);

        setTimeout(() => {
          if (document.body.contains(warningToast)) {
            document.body.removeChild(warningToast);
          }
        }, 5000);
      } else {
        // Show error message
        const errorToast = document.createElement('div');
        errorToast.className = 'toast show position-fixed top-0 end-0 m-3';
        errorToast.style.zIndex = '1200';
        errorToast.innerHTML = `
        <div class="toast-header bg-danger text-white">
          <strong class="me-auto">ComicVine Search Error</strong>
          <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
        </div>
        <div class="toast-body">
          Failed to add metadata: ${data.error || 'Unknown error'}
        </div>
      `;
        document.body.appendChild(errorToast);

        setTimeout(() => {
          if (document.body.contains(errorToast)) {
            document.body.removeChild(errorToast);
          }
        }, 8000);
      }
    })
    .catch(error => {
      console.error('ComicVine Search Network Error:', error);
      if (document.body.contains(loadingToast)) {
        document.body.removeChild(loadingToast);
      }

      const errorToast = document.createElement('div');
      errorToast.className = 'toast show position-fixed top-0 end-0 m-3';
      errorToast.style.zIndex = '1200';
      errorToast.innerHTML = `
      <div class="toast-header bg-danger text-white">
        <strong class="me-auto">Network Error</strong>
        <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
      </div>
      <div class="toast-body">
        Network error: ${error.message}
      </div>
    `;
      document.body.appendChild(errorToast);

      setTimeout(() => {
        if (document.body.contains(errorToast)) {
          document.body.removeChild(errorToast);
        }
      }, 8000);
    });
}

// Stub for ComicVine volume selection modal - will be implemented similar to GCD
function showComicVineVolumeSelectionModal(data, filePath, fileName) {
  console.log('Showing ComicVine volume selection modal', data);

  // Populate parsed filename info
  document.getElementById('cvParsedSeries').textContent = data.parsed_filename.series_name;
  document.getElementById('cvParsedIssue').textContent = data.parsed_filename.issue_number;
  document.getElementById('cvParsedYear').textContent = data.parsed_filename.year || 'Unknown';

  // Update modal title
  const modalTitle = document.getElementById('comicVineVolumeModalLabel');
  if (modalTitle) {
    modalTitle.textContent = `Found ${data.possible_matches.length} Volume(s) - Select Correct One`;
  }

  // Populate volume list
  const volumeList = document.getElementById('cvVolumeList');
  volumeList.innerHTML = '';

  data.possible_matches.forEach(volume => {
    const volumeItem = document.createElement('div');
    volumeItem.className = 'list-group-item list-group-item-action d-flex align-items-start';
    volumeItem.style.cursor = 'pointer';

    const yearDisplay = volume.start_year || 'Unknown';
    const descriptionPreview = volume.description ?
      `<small class="text-muted d-block mt-1">${volume.description}</small>` : '';

    // Display thumbnail if available, otherwise show placeholder
    const thumbnailHtml = volume.image_url ?
      `<img src="${volume.image_url}" class="img-thumbnail me-3" style="width: 80px; height: 120px; object-fit: cover;" alt="${volume.name} cover">` :
      `<div class="me-3 d-flex align-items-center justify-content-center bg-secondary text-white" style="width: 80px; height: 120px; font-size: 10px;">No Cover</div>`;

    volumeItem.innerHTML = `
      ${thumbnailHtml}
      <div class="flex-grow-1 d-flex justify-content-between align-items-start">
        <div class="me-2">
          <div class="fw-bold">${volume.name}</div>
          <small class="text-muted">Publisher: ${volume.publisher_name || 'Unknown'}<br>Issues: ${volume.count_of_issues || 'Unknown'}</small>
          ${descriptionPreview}
        </div>
        <span class="badge bg-success rounded-pill">${yearDisplay}</span>
      </div>
    `;

    volumeItem.addEventListener('click', () => {
      // Highlight selected item
      volumeList.querySelectorAll('.list-group-item').forEach(item => {
        item.classList.remove('active');
      });
      volumeItem.classList.add('active');

      // Call backend with selected volume (including publisher)
      selectComicVineVolume(filePath, fileName, volume.id, volume.publisher_name, data.parsed_filename.issue_number, data.parsed_filename.year);
    });

    volumeList.appendChild(volumeItem);
  });

  // Show the modal
  const modal = new bootstrap.Modal(document.getElementById('comicVineVolumeModal'));
  modal.show();
}

// showBatchVolumeSelectionModal, fetchAllMetadataWithVolume – delegated to clu-metadata.js

function selectComicVineVolume(filePath, fileName, volumeId, publisherName, issueNumber, year) {
  console.log('ComicVine volume selected:', { filePath, fileName, volumeId, publisherName, issueNumber, year });

  // Close the modal
  const modal = bootstrap.Modal.getInstance(document.getElementById('comicVineVolumeModal'));
  modal.hide();

  // Show loading indicator
  const loadingToast = document.createElement('div');
  loadingToast.className = 'toast show position-fixed top-0 end-0 m-3';
  loadingToast.style.zIndex = '1200';
  loadingToast.innerHTML = `
    <div class="toast-header bg-success text-white">
      <strong class="me-auto">ComicVine</strong>
      <small>Processing...</small>
    </div>
    <div class="toast-body">
      <div class="d-flex align-items-center">
        <div class="spinner-border spinner-border-sm me-2" role="status">
          <span class="visually-hidden">Loading...</span>
        </div>
        Retrieving metadata...
      </div>
    </div>
  `;
  document.body.appendChild(loadingToast);

  // Make request to backend
  fetch('/search-comicvine-metadata-with-selection', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      file_path: filePath,
      file_name: fileName,
      volume_id: volumeId,
      publisher_name: publisherName,
      issue_number: issueNumber,
      year: year
    })
  })
    .then(response => response.json())
    .then(data => {
      document.body.removeChild(loadingToast);

      if (data.success) {
        // Show success with cover image
        const successToast = document.createElement('div');
        successToast.className = 'toast show position-fixed top-0 end-0 m-3';
        successToast.style.zIndex = '1200';
        let imageHtml = data.image_url ? `<img src="${data.image_url}" class="img-thumbnail mt-2" style="max-width: 100px;" alt="Cover">` : '';
        successToast.innerHTML = `
        <div class="toast-header bg-success text-white">
          <strong class="me-auto">ComicVine Success</strong>
          <button type="button" class="btn-close btn-close-white" onclick="this.closest('.toast').remove()"></button>
        </div>
        <div class="toast-body">
          Successfully added metadata to "${fileName}"<br>
          <small class="text-muted">Series: ${data.metadata?.Series || 'Unknown'}<br>
          Issue: ${data.metadata?.Number || 'Unknown'}</small>
          ${imageHtml}
        </div>
      `;
        document.body.appendChild(successToast);

        setTimeout(() => {
          if (document.body.contains(successToast)) {
            document.body.removeChild(successToast);
          }
        }, 5000);

        // Remove file from UI if it was moved to a new location
        if (data.moved) {
          console.log('File was moved, removing from current panel');
          removeFileFromUI(filePath);
        }

        // Handle file rename if configured
        if (data.metadata?.Series) {
          const actualFilePath = data.moved ? data.new_file_path : filePath;
          promptRenameAfterMetadata(actualFilePath, fileName, data.metadata, data.rename_config);
        }
      } else {
        CLU.showToast('ComicVine Error', data.error || 'Failed to retrieve metadata', 'error');
      }
    })
    .catch(error => {
      document.body.removeChild(loadingToast);
      CLU.showToast('ComicVine Error', error.message, 'error');
    });
}

function padIssueNumber(numStr, width = 3) {
  numStr = String(numStr).trim();
  if (!numStr) return '';
  if (numStr.includes('.')) {
    const parts = numStr.split('.');
    return parts[0].padStart(width, '0') + '.' + parts.slice(1).join('.');
  }
  return numStr.padStart(width, '0');
}

function promptRenameAfterMetadata(filePath, fileName, metadata, renameConfig) {
  console.log('promptRenameAfterMetadata called with:', { filePath, fileName, metadata, renameConfig });

  let suggestedName;
  const ext = fileName.match(/\.(cbz|cbr)$/i)?.[0] || '.cbz';

  // Check if custom rename pattern is enabled and defined
  if (renameConfig && renameConfig.enabled && renameConfig.pattern) {
    console.log('Using custom rename pattern:', renameConfig.pattern);

    // Apply custom pattern - similar to rename.py logic
    let pattern = renameConfig.pattern;

    // Prepare values for replacement
    let series = metadata.Series || '';
    series = series.replace(/:/g, ' -');  // Replace colon with dash for Windows
    series = series.replace(/[<>"/\\|?*]/g, '');  // Remove invalid chars

    const issueNumber = padIssueNumber(metadata.Number);
    const year = metadata.Year || '';
    const volumeNumber = '';  // ComicVine uses year as Volume, not volume number

    let issueTitle = metadata.Title || '';
    issueTitle = issueTitle.replace(/:/g, ' -');
    issueTitle = issueTitle.replace(/[<>"/\\|?*]/g, '');
    issueTitle = issueTitle.replace(/[\x00-\x1f]/g, '');
    issueTitle = issueTitle.replace(/^[.\s]+|[.\s]+$/g, '');

    console.log('Pattern replacement values:', { series, issueNumber, year, volumeNumber, issueTitle, metadata });

    // Replace pattern variables (case-insensitive for flexibility)
    let result = pattern;
    result = result.replace(/{series_name}/gi, series);
    result = result.replace(/{issue_number}/gi, issueNumber);
    result = result.replace(/{year}/gi, year);
    result = result.replace(/{YYYY}/g, year);  // Support YYYY as well
    result = result.replace(/{volume_number}/gi, volumeNumber);
    result = result.replace(/{issue_title}/gi, issueTitle);

    // Clean up extra spaces
    result = result.replace(/\s+/g, ' ').trim();

    // Remove empty parentheses
    result = result.replace(/\s*\(\s*\)/g, '').trim();

    // Remove orphaned separators (e.g., trailing " - " when issue_title is empty)
    result = result.replace(/\s*-\s*(?=\(|$)/g, ' ').replace(/\s+/g, ' ').trim();

    suggestedName = result + ext;
  } else {
    // Default rename pattern: Series Number.ext
    let series = metadata.Series;
    series = series.replace(/:/g, ' -');  // Replace colon with dash
    series = series.replace(/[<>"/\\|?*]/g, '');  // Remove other invalid filename chars
    series = series.replace(/\s+/g, ' ').trim();  // Normalize whitespace

    const number = padIssueNumber(metadata.Number);
    suggestedName = `${series} ${number}${ext}`;
  }

  // Only proceed if the name would actually change
  if (suggestedName === fileName) {
    return;
  }

  // Check if auto-rename is enabled
  if (renameConfig && renameConfig.auto_rename) {
    console.log('Auto-rename is enabled, renaming file automatically');
    // Automatically rename without prompting
    renameFileAfterMetadata(filePath, fileName, suggestedName);
  } else {
    console.log('Auto-rename is disabled, skipping rename');
    // Auto-rename is disabled, do nothing (no prompt, no rename)
    return;
  }
}

function renameFileAfterMetadata(filePath, oldName, newName) {
  console.log('renameFileAfterMetadata called with:', { filePath, oldName, newName });

  // Construct the new full path
  const directory = filePath.substring(0, filePath.lastIndexOf('/'));
  const newPath = directory + '/' + newName;

  console.log('Constructed paths:', { old: filePath, new: newPath, directory });

  // Show loading toast
  const loadingToast = document.createElement('div');
  loadingToast.className = 'toast show position-fixed top-0 end-0 m-3';
  loadingToast.style.zIndex = '1200';
  loadingToast.innerHTML = `
    <div class="toast-header bg-primary text-white">
      <strong class="me-auto">Renaming</strong>
    </div>
    <div class="toast-body">
      <div class="d-flex align-items-center">
        <div class="spinner-border spinner-border-sm me-2" role="status">
          <span class="visually-hidden">Loading...</span>
        </div>
        Renaming file...
      </div>
    </div>
  `;
  document.body.appendChild(loadingToast);

  fetch('/rename', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      old: filePath,
      new: newPath
    })
  })
    .then(response => {
      if (!response.ok) {
        return response.json().then(err => {
          throw new Error(err.error || 'Rename failed');
        });
      }
      return response.json();
    })
    .then(data => {
      document.body.removeChild(loadingToast);
      if (data.success) {
        CLU.showToast('File Renamed', `Successfully renamed to: ${newName}`, 'success');

        // Update the file in the DOM instead of reloading entire list
        updateRenamedFileInDOM(filePath, newPath, newName);
      } else {
        CLU.showToast('Rename Failed', data.error || 'Failed to rename file', 'error');
      }
    })
    .catch(error => {
      if (document.body.contains(loadingToast)) {
        document.body.removeChild(loadingToast);
      }
      console.error('Rename error:', error);
      CLU.showToast('Rename Error', error.message, 'error');
    });
}

function updateRenamedFileInDOM(oldPath, newPath, newName) {
  console.log('updateRenamedFileInDOM:', { oldPath, newPath, newName });

  // Find the list item with the old path
  const sourceList = document.getElementById('source-list');
  const destList = document.getElementById('destination-list');

  // Check both panels for the file
  [sourceList, destList].forEach(list => {
    if (!list) return;

    const listItem = list.querySelector(`li[data-fullpath="${oldPath}"]`);
    if (listItem) {
      console.log('Found list item to update:', listItem);

      // Update the data attribute
      listItem.dataset.fullpath = newPath;

      // Find and update the filename span
      const nameSpan = listItem.querySelector('span');
      if (nameSpan) {
        // Preserve the size info if it exists
        const sizeMatch = nameSpan.innerHTML.match(/<span class="text-info-emphasis small ms-2">\([^)]+\)<\/span>/);
        if (sizeMatch) {
          nameSpan.innerHTML = `${newName} ${sizeMatch[0]}`;
        } else {
          nameSpan.textContent = newName;
        }
        console.log('Updated filename in DOM');
      }
    }
  });
}

// ============================================================================
// THREE-DOTS MENU ACTIONS (FROM COLLECTION.HTML)
// ============================================================================

/**
 * Execute a script action on a file (crop, remove first image, rebuild, enhance, add)
 * Contract setup wrapper for CLU.executeStreamingOp
 */
function executeScriptOnFile(scriptType, filePath, panel) {
  // Set up streaming contract for files.js page-specific behavior
  window._cluStreaming = {
    onComplete: function (type, path) {
      const currentPath = panel === 'source' ? currentSourcePath : currentDestinationPath;
      loadDirectories(currentPath, panel);
    },
    onError: function () {}
  };
  CLU.executeStreamingOp(scriptType, filePath);
}

/**
 * Hide the progress indicator
 * Contract setup wrapper for CLU streaming module
 */

/**
 * Show the missing file check results modal
 * @param {Object} data - The missing file data containing path, count, summary
 */
function showMissingFileCheckModal(data) {
  // Update summary
  const summaryEl = document.getElementById('missingFileCheckSummary');
  if (summaryEl) {
    summaryEl.textContent = data.summary || `Found ${data.count} missing issues.`;
  }

  // Update file path display
  const pathEl = document.getElementById('missingFileCheckPath');
  if (pathEl) {
    pathEl.textContent = data.path + '/missing.txt';
  }

  // Update file link
  const linkEl = document.getElementById('missingFileCheckLink');
  if (linkEl) {
    // Use static URL if available, otherwise construct download URL
    if (data.staticUrl) {
      linkEl.href = data.staticUrl;
    } else {
      linkEl.href = `/api/download?path=${encodeURIComponent(data.path + '/missing.txt')}`;
    }
    linkEl.target = '_blank';
  }

  // Show the modal
  const modalElement = document.getElementById('missingFileCheckModal');
  if (modalElement) {
    const modal = new bootstrap.Modal(modalElement);
    modal.show();
  }
}

/**
 * Execute a script operation on a directory
 * @param {string} scriptType - The type of script to run (convert, rebuild, pdf, missing, enhance_dir)
 * @param {string} directoryPath - Full path to the directory
 * @param {string} panel - Which panel the directory is in (source or destination)
 */
function bulkRemoveXmlFromDirectory(directoryPath, panel) {
  const folderName = directoryPath.split('/').pop() || directoryPath;
  document.getElementById('removeXmlDirName').textContent = folderName;

  // Store the path on the confirm button so the handler can read it
  const confirmBtn = document.getElementById('confirmRemoveXmlDirBtn');
  confirmBtn.dataset.directory = directoryPath;

  const modal = new bootstrap.Modal(document.getElementById('removeXmlDirModal'));
  modal.show();
}

/**
 * Execute a script operation on a directory
 * Contract setup wrapper for CLU.executeDirectoryOp
 */
function executeScriptOnDirectory(scriptType, directoryPath, panel) {
  // Set up streaming contract for files.js page-specific behavior
  window._cluStreaming = {
    onComplete: function (type, path) {
      const currentPath = panel === 'source' ? currentSourcePath : currentDestinationPath;
      loadDirectories(currentPath, panel);
    },
    onError: function () {}
  };
  CLU.executeDirectoryOp(scriptType, directoryPath);
}

/**
 * Open the edit modal for a CBZ file
 * @param {string} filePath - Path to the CBZ file to edit
 */
function openEditModal(filePath) {
  // Store the file path for later use when saving
  currentEditFilePath = filePath;

  // Open the edit modal
  const editModal = new bootstrap.Modal(document.getElementById('editCBZModal'));
  const container = document.getElementById('editInlineContainer');

  // Show loading spinner
  container.innerHTML = `<div class="d-flex justify-content-center my-3">
                              <button class="btn btn-primary" type="button" disabled>
                                  <span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>
                                  Unpacking CBZ File ...
                              </button>
                          </div>`;

  editModal.show();
  CLU.setupEditModalDropZone();

  // Load CBZ contents
  fetch(`/edit?file_path=${encodeURIComponent(filePath)}`)
    .then(response => {
      if (!response.ok) {
        throw new Error("Failed to load edit content.");
      }
      return response.json();
    })
    .then(data => {
      document.getElementById('editInlineContainer').innerHTML = data.modal_body;
      document.getElementById('editInlineFolderName').value = data.folder_name;
      document.getElementById('editInlineZipFilePath').value = data.zip_file_path;
      document.getElementById('editInlineOriginalFilePath').value = data.original_file_path;
      CLU.sortInlineEditCards();
    })
    .catch(error => {
      container.innerHTML = `<div class="alert alert-danger" role="alert">
                                  <strong>Error:</strong> ${error.message}
                              </div>`;
      CLU.showToast('Error', error.message, 'error');
    });
}

function saveEditedCBZ() {
  window._cluCbzEdit = {
    onSaveComplete: function (filePath) {
      if (currentSourcePath && currentEditFilePath && currentEditFilePath.startsWith(currentSourcePath)) {
        loadDirectories(currentSourcePath, 'source');
      } else if (currentDestinationPath && currentEditFilePath && currentEditFilePath.startsWith(currentDestinationPath)) {
        loadDirectories(currentDestinationPath, 'destination');
      }
    }
  };
  CLU.saveEditedCBZ();
}

// ============================================================================
// EDIT MODAL CARD FUNCTIONS
// ============================================================================

// ============================================================================
// FREE-FORM CROP FUNCTIONALITY (delegated to clu-cbz-crop.js)
// ============================================================================

/**
 * Open the ComicVine URL modal for user to enter the URL
 * @param {string} directoryPath - The directory to save the cvinfo file in
 * @param {string} panel - The panel ('source' or 'destination') to refresh after saving
 */
function promptForCVInfo(directoryPath, panel) {
  // Store the directory path and panel in hidden fields
  document.getElementById('cvInfoDirectoryPath').value = directoryPath;
  document.getElementById('cvInfoPanel').value = panel;

  // Clear the input fields
  document.getElementById('cvInfoIdInput').value = '';
  document.getElementById('metronIdInput').value = '';

  // Show the modal
  const modal = new bootstrap.Modal(document.getElementById('cvInfoModal'));
  modal.show();

  // Focus the input field after modal is shown
  document.getElementById('cvInfoModal').addEventListener('shown.bs.modal', function () {
    document.getElementById('cvInfoIdInput').focus();
  }, { once: true });
}

/**
 * Save the ComicVine URL from the modal
 */
function saveCVInfo() {
  const cvId = document.getElementById('cvInfoIdInput').value.trim();
  const metronId = document.getElementById('metronIdInput').value.trim();
  const directoryPath = document.getElementById('cvInfoDirectoryPath').value;
  const panel = document.getElementById('cvInfoPanel').value;

  if (!cvId) {
    CLU.showToast('Error', 'Please enter a Comic Vine Volume ID', 'error');
    return;
  }

  // Validate that it's a number
  if (!/^\d+$/.test(cvId)) {
    CLU.showToast('Error', 'Comic Vine ID must be a number', 'error');
    return;
  }

  // Validate Metron ID if provided
  if (metronId && !/^\d+$/.test(metronId)) {
    CLU.showToast('Error', 'Metron ID must be a number', 'error');
    return;
  }

  // Build the file content
  let content = `https://comicvine.gamespot.com/volume/4050-${cvId}`;
  if (metronId) {
    content += `\nseries_id: ${metronId}`;
  }

  // Hide the modal
  const modalEl = document.getElementById('cvInfoModal');
  const modal = bootstrap.Modal.getInstance(modalEl);
  modal.hide();

  // Save the cvinfo file
  fetch('/api/save-cvinfo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      directory: directoryPath,
      content: content
    })
  })
    .then(response => response.json())
    .then(data => {
      if (data.success) {
        CLU.showToast('Success', 'CVINFO file saved successfully!', 'success');
        // Refresh the directory listing to show the new file
        loadDirectories(directoryPath, panel);
      } else {
        CLU.showToast('Error', data.error || 'Failed to save CVINFO file', 'error');
      }
    })
    .catch(error => {
      console.error('Error saving CVINFO:', error);
      CLU.showToast('Error', 'An error occurred while saving the CVINFO file', 'error');
    });
}
