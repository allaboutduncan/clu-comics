/**
 * Series page JavaScript - handles directory browsing and series mapping
 */

// Global state
let modalDirectoryData = null;
let currentModalFilter = 'all';
let currentPath = (typeof defaultLibraryPath !== 'undefined') ? defaultLibraryPath : '/data';

/**
 * Open the directory mapping modal
 */
function openMappingModal() {
    let startPath = null;

    if (currentMappedPath) {
        const lastSlash = currentMappedPath.lastIndexOf('/');
        if (lastSlash > 0) {
            startPath = currentMappedPath.substring(0, lastSlash);
        }
    }

    if (!startPath) {
        // Use selected library from Subscribe dropdown, or default library
        const select = document.getElementById('subscribeLibrary');
        if (select) {
            startPath = select.value;
        } else {
            startPath = (typeof defaultLibraryPath !== 'undefined') ? defaultLibraryPath : '/data';
        }
    }

    initLibraryRoots();
    loadDirectories(startPath);
    const modal = new bootstrap.Modal(document.getElementById('directoryModal'));
    modal.show();
}

/**
 * Initialize library root quick-jump buttons in the directory modal
 */
function initLibraryRoots() {
    if (typeof libraries === 'undefined' || libraries.length <= 1) return;

    const bar = document.getElementById('library-roots-bar');
    const btnGroup = document.getElementById('library-roots-buttons');
    if (!bar || !btnGroup) return;

    bar.style.display = 'flex';
    bar.classList.add('align-items-center');
    btnGroup.innerHTML = libraries.map(lib =>
        `<button type="button" class="btn btn-outline-info" onclick="loadDirectories('${lib.path.replace(/'/g, "\\'")}')">${lib.name}</button>`
    ).join('');
}

/**
 * Load directories from server
 */
function loadDirectories(path) {
    if (!path) path = (typeof defaultLibraryPath !== 'undefined') ? defaultLibraryPath : '/data';
    currentPath = path;

    const directoryList = document.getElementById('directory-list');
    directoryList.innerHTML = '<li class="list-group-item text-center text-muted"><span class="spinner-border spinner-border-sm me-2"></span>Loading...</li>';

    fetch(`/list-directories?path=${encodeURIComponent(path)}`)
        .then(response => response.json())
        .then(data => {
            modalDirectoryData = data;
            currentModalFilter = 'all';

            updateFilterBar(data.directories);
            renderDirectoryList(data, currentModalFilter);

            // Update path display
            document.getElementById('current-path-display').textContent = data.current_path;
        })
        .catch(error => {
            console.error('Error fetching directories:', error);
            directoryList.innerHTML = '<li class="list-group-item text-danger">Error loading directories</li>';
        });
}

/**
 * Update the alphabetical filter bar
 */
function updateFilterBar(directories) {
    const filterContainer = document.getElementById('directory-filter');
    if (!filterContainer) return;

    // Analyze available first letters
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

    // Build filter buttons
    let buttonsHtml = '<button type="button" class="btn btn-sm btn-outline-secondary active" onclick="filterDirectories(\'all\')">All</button>';

    if (hasNonAlpha) {
        buttonsHtml += '<button type="button" class="btn btn-sm btn-outline-secondary" onclick="filterDirectories(\'#\')">#</button>';
    }

    const sortedLetters = Array.from(availableLetters).sort();
    sortedLetters.forEach(letter => {
        buttonsHtml += `<button type="button" class="btn btn-sm btn-outline-secondary" onclick="filterDirectories('${letter}')">${letter}</button>`;
    });

    filterContainer.querySelector('.btn-group').innerHTML = buttonsHtml;
}

/**
 * Filter directories by letter
 */
function filterDirectories(filter) {
    currentModalFilter = filter;

    // Update button states
    const buttons = document.querySelectorAll('#directory-filter button');
    buttons.forEach(btn => {
        if (btn.textContent === filter || (filter === 'all' && btn.textContent === 'All')) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });

    renderDirectoryList(modalDirectoryData, filter);
}

/**
 * Render the directory list
 */
function renderDirectoryList(data, filter) {
    const directoryList = document.getElementById('directory-list');
    directoryList.innerHTML = '';

    // Add "Go Back" option if there's a parent
    if (data.parent) {
        const backItem = document.createElement('li');
        backItem.className = 'list-group-item list-group-item-action d-flex align-items-center';
        backItem.style.cursor = 'pointer';
        backItem.innerHTML = '<i class="bi bi-arrow-left me-2 text-muted"></i><span class="text-muted">.. (Go Back)</span>';
        backItem.onclick = () => loadDirectories(data.parent);
        directoryList.appendChild(backItem);
    }

    // Filter directories
    let filteredDirs = data.directories || [];
    if (filter !== 'all') {
        if (filter === '#') {
            filteredDirs = filteredDirs.filter(dir => {
                const firstChar = dir.charAt(0).toUpperCase();
                return !(firstChar >= 'A' && firstChar <= 'Z');
            });
        } else {
            filteredDirs = filteredDirs.filter(dir => dir.charAt(0).toUpperCase() === filter);
        }
    }

    // Sort directories
    filteredDirs.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));

    // Render directories
    filteredDirs.forEach(dir => {
        const item = document.createElement('li');
        item.className = 'list-group-item list-group-item-action d-flex justify-content-between align-items-center';

        // Left side - navigate into directory
        const leftDiv = document.createElement('div');
        leftDiv.className = 'd-flex align-items-center flex-grow-1';
        leftDiv.style.cursor = 'pointer';
        leftDiv.innerHTML = `<i class="bi bi-folder-fill me-2 text-warning"></i><span>${dir}</span>`;
        leftDiv.onclick = () => loadDirectories(data.current_path + '/' + dir);

        // Right side - select this directory
        const selectBtn = document.createElement('button');
        selectBtn.className = 'btn btn-sm btn-outline-success';
        selectBtn.innerHTML = '<i class="bi bi-check-lg"></i>';
        selectBtn.title = 'Select this folder';
        selectBtn.onclick = (e) => {
            e.stopPropagation();
            selectDirectory(data.current_path + '/' + dir);
        };

        item.appendChild(leftDiv);
        item.appendChild(selectBtn);
        directoryList.appendChild(item);
    });

    // Show message if no directories
    if (filteredDirs.length === 0 && !data.parent) {
        const emptyItem = document.createElement('li');
        emptyItem.className = 'list-group-item text-muted text-center';
        emptyItem.textContent = 'No directories found';
        directoryList.appendChild(emptyItem);
    }
}

/**
 * Select the current directory (from modal footer button)
 */
function selectCurrentDirectory() {
    selectDirectory(currentPath);
}

/**
 * Select a directory and save the mapping
 */
function selectDirectory(path) {
    if (!seriesData || !seriesData.id) {
        console.error('No series data available');
        alert('Error: Series data not available');
        return;
    }

    // Show loading state
    const modal = bootstrap.Modal.getInstance(document.getElementById('directoryModal'));
    const selectBtn = document.querySelector('#directoryModal .modal-footer .btn-primary');
    const originalText = selectBtn.innerHTML;
    selectBtn.disabled = true;
    selectBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving...';

    // Save mapping via API
    fetch(`/api/series/${seriesData.id}/map`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            mapped_path: path,
            series: seriesData
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Update UI
                document.getElementById('mapped-path-display').innerHTML =
                    `<span class="text-success"><i class="bi bi-check-circle me-1"></i>${path}</span>`;

                // Update the button text and add remove/refresh buttons if not present
                const mappingCard = document.querySelector('#mapping .card-body');
                const buttonsDiv = mappingCard.querySelector('.d-flex.gap-2');

                // Check if remove button exists, if not add it
                if (!buttonsDiv.querySelector('.btn-outline-danger')) {
                    const removeBtn = document.createElement('button');
                    removeBtn.className = 'btn btn-outline-danger';
                    removeBtn.innerHTML = '<i class="bi bi-x-circle me-1"></i>Remove';
                    removeBtn.onclick = removeMappingConfirm;
                    buttonsDiv.insertBefore(removeBtn, buttonsDiv.firstChild);
                }

                // Update map button text
                const mapBtn = buttonsDiv.querySelector('.btn-outline-primary');
                mapBtn.innerHTML = '<i class="bi bi-folder-symlink me-1"></i>Change Location';

                // Check if refresh button exists, if not add it
                if (!buttonsDiv.querySelector('#refresh-btn')) {
                    const refreshBtn = document.createElement('button');
                    refreshBtn.className = 'btn btn-outline-success';
                    refreshBtn.id = 'refresh-btn';
                    refreshBtn.innerHTML = '<i class="bi bi-arrow-clockwise me-1"></i>Refresh';
                    refreshBtn.onclick = refreshCollectionStatus;
                    buttonsDiv.appendChild(refreshBtn);
                }

                // Close modal
                modal.hide();

                // Automatically check collection status
                setTimeout(() => refreshCollectionStatus(), 300);
            } else {
                alert('Failed to save mapping: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error saving mapping:', error);
            alert('Error saving mapping: ' + error.message);
        })
        .finally(() => {
            selectBtn.disabled = false;
            selectBtn.innerHTML = originalText;
        });
}

/**
 * Confirm removal of mapping
 */
function removeMappingConfirm() {
    if (!confirm('Remove the collection mapping for this series?')) {
        return;
    }

    if (!seriesData || !seriesData.id) {
        console.error('No series data available');
        return;
    }

    fetch(`/api/series/${seriesData.id}/mapping`, {
        method: 'DELETE'
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Update UI
                document.getElementById('mapped-path-display').innerHTML =
                    '<span class="text-muted">Not mapped to local collection</span>';

                // Remove the remove button
                const mappingCard = document.querySelector('#mapping .card-body');
                const buttonsDiv = mappingCard.querySelector('.d-flex.gap-2');
                const removeBtn = buttonsDiv.querySelector('.btn-outline-danger');
                if (removeBtn) {
                    removeBtn.remove();
                }

                // Update map button text
                const mapBtn = buttonsDiv.querySelector('.btn-outline-primary');
                mapBtn.innerHTML = '<i class="bi bi-folder-symlink me-1"></i>Map Location';
            } else {
                alert('Failed to remove mapping: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error removing mapping:', error);
            alert('Error removing mapping: ' + error.message);
        });
}


/**
 * Sync series data from Metron API
 */
function syncSeries() {
    if (!seriesData || !seriesData.id) {
        console.error('No series data available');
        return;
    }

    const syncBtn = document.getElementById('sync-btn');
    const originalHtml = syncBtn.innerHTML;

    // Show loading state
    syncBtn.disabled = true;
    syncBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Syncing...';

    fetch(`/api/sync/series/${seriesData.id}`, {
        method: 'POST'
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Update last synced display
                const syncedDisplay = document.getElementById('last-synced-display');
                if (syncedDisplay) {
                    const now = new Date().toLocaleString();
                    syncedDisplay.innerHTML = `<i class="bi bi-clock me-1"></i>Synced: ${now}`;
                }

                // Refresh collection status to update table
                setTimeout(() => refreshCollectionStatus(), 300);
            } else {
                alert('Failed to sync: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error syncing series:', error);
            alert('Error syncing: ' + error.message);
        })
        .finally(() => {
            syncBtn.disabled = false;
            syncBtn.innerHTML = originalHtml;
        });
}

/**
 * Refresh the collection status without reloading the page
 */
function refreshCollectionStatus() {
    if (!seriesData || !seriesData.id) {
        console.error('No series data available');
        return;
    }

    const refreshBtn = document.getElementById('refresh-btn');
    const originalHtml = refreshBtn ? refreshBtn.innerHTML : '';

    // Show loading state
    if (refreshBtn) {
        refreshBtn.disabled = true;
        refreshBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Checking...';
    }

    const today = new Date().toISOString().split('T')[0]; // YYYY-MM-DD format

    fetch(`/api/series/${seriesData.id}/check-collection?refresh=true`)
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Update table rows
                const tbody = document.querySelector('#issues tbody');
                if (tbody) {
                    tbody.querySelectorAll('tr').forEach(row => {
                        const issueNumCell = row.querySelector('td:first-child');
                        const storeDateCell = row.querySelector('td:nth-child(2)');
                        const actionCell = row.querySelector('td:last-child');
                        if (!issueNumCell) return;

                        // Extract issue number from cell text (e.g., "#001" -> "1")
                        const issueNumMatch = issueNumCell.textContent.match(/#(\S+)/);
                        if (!issueNumMatch) return;

                        // Normalize issue number (remove leading zeros for lookup)
                        const rawIssueNum = issueNumMatch[1];
                        const issueNum = rawIssueNum.replace(/^0+/, '') || '0';
                        const status = data.issue_status[issueNum] || data.issue_status[rawIssueNum];

                        // Get store date from the row
                        const storeDate = storeDateCell ? storeDateCell.textContent.trim() : '';
                        const isUpcoming = storeDate && storeDate !== '-' && storeDate > today;

                        // Check for manual status
                        const manualStatus = data.manual_status ? (data.manual_status[issueNum] || data.manual_status[rawIssueNum]) : null;
                        const hasManual = manualStatus && manualStatus.status;

                        if (status || hasManual) {
                            // Update row class based on status
                            if (status && status.found) {
                                row.className = 'table-success';
                            } else if (hasManual) {
                                row.className = 'table-success';
                            } else if (isUpcoming) {
                                row.className = 'table-info';
                            } else {
                                row.className = 'table-danger';
                            }

                            // Update cell content with icon and wanted badge
                            const paddedNum = /^\d+$/.test(rawIssueNum) ? rawIssueNum : rawIssueNum;
                            let iconClass, iconTitle, iconColor;

                            if (status && status.found) {
                                iconClass = 'check-circle-fill';
                                iconTitle = 'Found in collection';
                                iconColor = 'text-success';
                            } else if (hasManual) {
                                iconClass = manualStatus.status === 'owned' ? 'bookmark-check-fill' : 'skip-forward-circle-fill';
                                iconTitle = manualStatus.status === 'owned' ? 'Owned' : 'Skipped';
                                if (manualStatus.notes) iconTitle += ': ' + manualStatus.notes;
                                iconColor = 'text-success';
                            } else {
                                iconClass = 'x-circle-fill';
                                iconTitle = 'Not found';
                                iconColor = 'text-danger';
                            }

                            let cellHtml = `<i class="bi bi-${iconClass} ${iconColor} me-1" title="${iconTitle}"></i>#${paddedNum}`;

                            // Add "Wanted" badge if not found and not manually marked and not upcoming
                            if ((!status || !status.found) && !hasManual && !isUpcoming) {
                                cellHtml += '<span class="badge bg-warning text-dark ms-2">Wanted</span>';
                            }

                            issueNumCell.innerHTML = cellHtml;

                            // Update action buttons
                            if (actionCell) {
                                if (status && status.found && status.file_path) {
                                    const escapedPath = status.file_path.replace(/\\/g, '/').replace(/'/g, "\\'");
                                    actionCell.innerHTML = `
                                        <div class="btn-group" role="group">
                                            <button type="button" class="btn btn-sm btn-outline-info text-info"
                                                onclick="viewIssueFile('${escapedPath}', '${issueNum}')"
                                                title="View CBZ Info">
                                                <i class="bi bi-eye"></i>
                                            </button>
                                            <button type="button" class="btn btn-sm btn-outline-primary"
                                                onclick="editIssueFile('${escapedPath}')"
                                                title="Edit CBZ">
                                                <i class="bi bi-pencil"></i>
                                            </button>
                                            <div class="dropdown d-inline-block">
                                                <button class="btn btn-sm btn-outline-secondary" type="button"
                                                    data-bs-toggle="dropdown" aria-expanded="false" title="More options">
                                                    <i class="bi bi-three-dots-vertical"></i>
                                                </button>
                                                <ul class="dropdown-menu dropdown-menu-end shadow">
                                                    <li><a class="dropdown-item" href="#"
                                                            onclick="executeIssueScript('crop', '${escapedPath}'); return false;">
                                                            <i class="bi bi-crop me-2"></i>Crop Cover
                                                        </a></li>
                                                    <li><a class="dropdown-item" href="#"
                                                            onclick="executeIssueScript('remove', '${escapedPath}'); return false;">
                                                            <i class="bi bi-file-minus me-2"></i>Remove 1st Image
                                                        </a></li>
                                                    <li><a class="dropdown-item" href="#"
                                                            onclick="executeIssueScript('single_file', '${escapedPath}'); return false;">
                                                            <i class="bi bi-hammer me-2"></i>Rebuild
                                                        </a></li>
                                                    <li><a class="dropdown-item" href="#"
                                                            onclick="executeIssueScript('enhance_single', '${escapedPath}'); return false;">
                                                            <i class="bi bi-stars me-2"></i>Enhance
                                                        </a></li>
                                                    <li><a class="dropdown-item" href="#"
                                                            onclick="executeIssueScript('add', '${escapedPath}'); return false;">
                                                            <i class="bi bi-file-plus me-2"></i>Add Blank to End
                                                        </a></li>
                                                </ul>
                                            </div>
                                        </div>
                                    `;
                                } else if (hasManual) {
                                    // Manually marked - show clear button
                                    actionCell.innerHTML = `
                                        <button class="btn btn-sm btn-outline-secondary"
                                            onclick="clearManualStatus('${issueNum}')"
                                            title="Clear ${manualStatus.status} status">
                                            <i class="bi bi-arrow-counterclockwise"></i>
                                        </button>
                                    `;
                                } else {
                                    // Not found - show search with mark dropdown
                                    const seriesName = seriesData.name ? seriesData.name.replace(/'/g, "\\'") : '';
                                    actionCell.innerHTML = `
                                        <div class="btn-group" role="group">
                                            <button class="btn btn-sm btn-outline-primary"
                                                onclick="searchGetComics('${seriesName}', '${issueNum}')"
                                                title="Search GetComics">
                                                <i class="bi bi-search"></i>
                                            </button>
                                            <button class="btn btn-sm btn-outline-secondary dropdown-toggle dropdown-toggle-split"
                                                data-bs-toggle="dropdown" aria-expanded="false" title="Mark issue">
                                                <span class="visually-hidden">Toggle Dropdown</span>
                                            </button>
                                            <ul class="dropdown-menu dropdown-menu-end shadow">
                                                <li><a class="dropdown-item" href="#"
                                                        onclick="markIssue('${issueNum}', 'owned'); return false;">
                                                        <i class="bi bi-bookmark-check me-2"></i>Mark as Owned
                                                    </a></li>
                                                <li><a class="dropdown-item" href="#"
                                                        onclick="markIssue('${issueNum}', 'skipped'); return false;">
                                                        <i class="bi bi-skip-forward-circle me-2"></i>Mark as Skipped
                                                    </a></li>
                                            </ul>
                                        </div>
                                    `;
                                }
                            }
                        }
                    });
                }

                // Update footer counts
                const footer = document.querySelector('#issues .card-footer small:first-child');
                if (footer) {
                    const manualCount = data.manual_count || 0;
                    const wantedCount = data.missing_count || (data.total_count - data.found_count - manualCount);
                    let footerHtml = `
                        <span class="text-success"><i class="bi bi-check-circle-fill me-1"></i>${data.found_count} found</span>
                    `;
                    if (manualCount > 0) {
                        footerHtml += `
                            <span class="mx-2">|</span>
                            <span class="text-success"><i class="bi bi-bookmark-check me-1"></i>${manualCount} marked</span>
                        `;
                    }
                    footerHtml += `
                        <span class="mx-2">|</span>
                        <span class="text-warning"><i class="bi bi-star-fill me-1"></i>${wantedCount} wanted</span>
                    `;
                    footer.innerHTML = footerHtml;
                }

            } else {
                alert('Failed to refresh: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error refreshing collection status:', error);
            alert('Error refreshing: ' + error.message);
        })
        .finally(() => {
            if (refreshBtn) {
                refreshBtn.disabled = false;
                refreshBtn.innerHTML = originalHtml;
            }
        });
}

/**
 * View CBZ info for an issue file
 * @param {string} filePath - Full path to the file
 * @param {string} issueNumber - Issue number (for reference)
 */
function viewIssueFile(filePath, issueNumber) {
    const fileName = filePath.split('/').pop();
    const directoryPath = filePath.substring(0, filePath.lastIndexOf('/'));
    // Call files.js function - pass empty array for fileList since we're viewing single file
    showCBZInfo(filePath, fileName, directoryPath, []);
}

/**
 * Edit an issue file
 * @param {string} filePath - Full path to the file
 */
function editIssueFile(filePath) {
    openEditModal(filePath);
}

/**
 * Execute a script on an issue file
 * @param {string} scriptType - crop, remove, single_file, enhance_single, add
 * @param {string} filePath - Full path to the file
 */
function executeIssueScript(scriptType, filePath) {
    // Call files.js function with 'source' panel (doesn't matter for single file ops)
    executeScriptOnFile(scriptType, filePath, 'source');
}

/**
 * Hide the progress indicator
 */
function hideProgressIndicator() {
    const progressContainer = document.getElementById('progress-container');
    if (progressContainer) {
        progressContainer.style.display = 'none';
    }
}

/**
 * Open the mark issue modal
 * @param {string} issueNumber - Issue number to mark
 * @param {string} status - 'owned' or 'skipped'
 */
function markIssue(issueNumber, status) {
    document.getElementById('markIssueNumber').value = issueNumber;
    document.getElementById('markNotes').value = '';

    // Set the status radio button
    if (status === 'skipped') {
        document.getElementById('markSkipped').checked = true;
    } else {
        document.getElementById('markOwned').checked = true;
    }

    const modal = new bootstrap.Modal(document.getElementById('markIssueModal'));
    modal.show();
}

/**
 * Submit the mark issue form
 */
function submitMarkIssue() {
    if (!seriesData || !seriesData.id) {
        console.error('No series data available');
        return;
    }

    const issueNumber = document.getElementById('markIssueNumber').value;
    const status = document.querySelector('input[name="markStatus"]:checked').value;
    const notes = document.getElementById('markNotes').value.trim();

    fetch(`/api/series/${seriesData.id}/issue/${issueNumber}/manual-status`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            status: status,
            notes: notes || null
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Close modal
                bootstrap.Modal.getInstance(document.getElementById('markIssueModal')).hide();

                // Update the row in the table
                updateIssueRow(issueNumber, status, notes);

                // Show success toast or notification
                console.log(`Issue #${issueNumber} marked as ${status}`);
            } else {
                alert('Failed to mark issue: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error marking issue:', error);
            alert('Error marking issue');
        });
}

/**
 * Clear manual status for an issue
 * @param {string} issueNumber - Issue number to clear
 */
function clearManualStatus(issueNumber) {
    if (!seriesData || !seriesData.id) {
        console.error('No series data available');
        return;
    }

    fetch(`/api/series/${seriesData.id}/issue/${issueNumber}/manual-status`, {
        method: 'DELETE'
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Refresh to show updated status
                refreshCollectionStatus();
            } else {
                alert('Failed to clear status: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error clearing status:', error);
            alert('Error clearing status');
        });
}

/**
 * Update a single issue row after marking
 * @param {string} issueNumber - Issue number
 * @param {string} status - 'owned' or 'skipped'
 * @param {string} notes - Optional notes
 */
function updateIssueRow(issueNumber, status, notes) {
    // Find the row by data attribute
    const row = document.querySelector(`tr[data-issue-number="${issueNumber}"]`);
    if (!row) {
        // Fallback: refresh the whole collection status
        refreshCollectionStatus();
        return;
    }

    // Update row class to success (green)
    row.className = 'table-success';

    // Update the icon in the first cell
    const firstCell = row.querySelector('td:first-child');
    if (firstCell) {
        const icon = firstCell.querySelector('i');
        if (icon) {
            const iconClass = status === 'owned' ? 'bookmark-check-fill' : 'skip-forward-circle-fill';
            const title = status === 'owned' ? 'Owned' : 'Skipped';
            icon.className = `bi bi-${iconClass} text-success me-1`;
            icon.title = notes ? `${title}: ${notes}` : title;
        }

        // Remove "Wanted" badge if present
        const wantedBadge = firstCell.querySelector('.badge');
        if (wantedBadge) {
            wantedBadge.remove();
        }
    }

    // Update action cell to show clear button
    const actionCell = row.querySelector('td:last-child');
    if (actionCell) {
        actionCell.innerHTML = `
            <button class="btn btn-sm btn-outline-secondary"
                onclick="clearManualStatus('${issueNumber}')"
                title="Clear ${status} status">
                <i class="bi bi-arrow-counterclockwise"></i>
            </button>
        `;
    }

    // Update footer counts
    updateFooterCounts();
}

/**
 * Update the footer counts after marking/clearing an issue
 */
function updateFooterCounts() {
    // Count rows by class
    const successRows = document.querySelectorAll('#issues tbody tr.table-success').length;
    const dangerRows = document.querySelectorAll('#issues tbody tr.table-danger').length;
    const totalRows = document.querySelectorAll('#issues tbody tr').length;

    // Update footer - this is a simplified version, full refresh might be better
    const footer = document.querySelector('.card-footer small');
    if (footer) {
        // Refresh the whole collection status for accurate counts
        refreshCollectionStatus();
    }
}
