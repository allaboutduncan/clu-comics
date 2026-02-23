console.log('reading_list.js loaded');

// ==========================================
// Tag Filter System
// ==========================================

let activeTagFilters = new Set();

function initTagFilters() {
    document.querySelectorAll('.tag-filter-btn').forEach(btn => {
        btn.addEventListener('click', () => toggleTagFilter(btn));
    });
}

function toggleTagFilter(btn) {
    const tag = btn.dataset.tag;

    if (tag === 'all') {
        // Clear all filters, show all
        activeTagFilters.clear();
        document.querySelectorAll('.tag-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
    } else {
        // Remove 'all' active state
        document.querySelector('.tag-filter-btn[data-tag="all"]')?.classList.remove('active');

        // Toggle this filter
        if (activeTagFilters.has(tag)) {
            activeTagFilters.delete(tag);
            btn.classList.remove('active');
        } else {
            activeTagFilters.add(tag);
            btn.classList.add('active');
        }

        // If no filters active, activate 'all'
        if (activeTagFilters.size === 0) {
            document.querySelector('.tag-filter-btn[data-tag="all"]')?.classList.add('active');
        }
    }

    applyTagFilters();
}

function applyTagFilters() {
    const cards = document.querySelectorAll('.reading-list-card');

    cards.forEach(card => {
        const cardTags = JSON.parse(card.dataset.tags || '[]');

        if (activeTagFilters.size === 0) {
            card.style.display = '';
        } else {
            // Show if card has ALL of the active filter tags (AND logic)
            const hasAllTags = [...activeTagFilters].every(t => cardTags.includes(t));
            card.style.display = hasAllTags ? '' : 'none';
        }
    });
}

// Initialize tag filters on page load
document.addEventListener('DOMContentLoaded', initTagFilters);

// Toast notification system
let currentProgressToast = null;

function getToastContainer() {
    let toastContainer = document.getElementById('toast-container');
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.id = 'toast-container';
        toastContainer.className = 'toast-container position-fixed end-0 p-4';
        toastContainer.style.zIndex = '1100';
        toastContainer.style.top = '60px'; // Below navbar
        document.body.appendChild(toastContainer);
    }
    return toastContainer;
}

function showToast(message, type = 'info', duration = 5000) {
    console.log(`[Toast] ${type}: ${message}`);

    const toastContainer = getToastContainer();
    const toastId = 'toast-' + Date.now();
    const bgClass = type === 'success' ? 'bg-success' : type === 'error' ? 'bg-danger' : 'bg-primary';

    const toastHtml = `
        <div id="${toastId}" class="toast align-items-center text-white ${bgClass} border-0 show" role="alert">
            <div class="d-flex">
                <div class="toast-body">${message}</div>
                <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
            </div>
        </div>
    `;

    toastContainer.insertAdjacentHTML('beforeend', toastHtml);
    const toastEl = document.getElementById(toastId);

    // Auto-hide after duration
    setTimeout(() => {
        if (toastEl && toastEl.parentNode) {
            toastEl.classList.remove('show');
            setTimeout(() => toastEl.remove(), 300);
        }
    }, duration);

    return toastEl;
}

function showProgressToast(message) {
    console.log(`[Progress] ${message}`);

    const toastContainer = getToastContainer();

    // Update existing progress toast or create new one
    if (currentProgressToast && currentProgressToast.parentNode) {
        const msgEl = currentProgressToast.querySelector('.progress-message');
        if (msgEl) {
            msgEl.textContent = message;
            console.log(`[Progress] Updated toast to: ${message}`);
        }
    } else {
        const toastHtml = `
            <div id="progress-toast" class="toast align-items-center text-white bg-primary border-0 show" role="alert">
                <div class="d-flex">
                    <div class="toast-body d-flex align-items-center">
                        <span class="spinner-border spinner-border-sm me-2 flex-shrink-0" role="status"></span>
                        <span class="progress-message">${message}</span>
                    </div>
                </div>
            </div>
        `;
        toastContainer.insertAdjacentHTML('beforeend', toastHtml);
        currentProgressToast = document.getElementById('progress-toast');
        console.log(`[Progress] Created new toast: ${message}`);
    }
}

function hideProgressToast() {
    if (currentProgressToast && currentProgressToast.parentNode) {
        currentProgressToast.remove();
        currentProgressToast = null;
    }
}

// Poll for import task completion
function pollImportStatus(taskId, filename) {
    console.log(`[Poll] Starting to poll for task: ${taskId}`);
    const pollInterval = 500; // Check every 500ms for more responsive updates

    function checkStatus() {
        fetch(`/api/reading-lists/import-status/${taskId}`)
            .then(response => response.json())
            .then(data => {
                console.log(`[Poll] Status: ${data.status}, processed: ${data.processed}/${data.total}, message: ${data.message}`);

                if (!data.success) {
                    hideProgressToast();
                    showToast('Import task not found', 'error');
                    return;
                }

                if (data.status === 'complete') {
                    hideProgressToast();
                    showToast(`Imported "${data.list_name}" (${data.processed} issues)`, 'success', 8000);
                    // Reload page to show the new list
                    setTimeout(() => window.location.reload(), 2000);
                } else if (data.status === 'error') {
                    hideProgressToast();
                    showToast(`Import failed: ${data.message}`, 'error', 10000);
                } else {
                    // Still processing, update progress toast
                    if (data.total > 0) {
                        showProgressToast(`Importing "${filename}"... ${data.processed}/${data.total} issues`);
                    } else {
                        showProgressToast(`Importing "${filename}"...`);
                    }
                    setTimeout(checkStatus, pollInterval);
                }
            })
            .catch(error => {
                console.error('Error checking import status:', error);
                setTimeout(checkStatus, pollInterval * 2); // Retry with longer delay
            });
    }

    // Show initial progress toast
    showProgressToast(`Starting import of "${filename}"...`);
    checkStatus();
}

function extractListNameFromFilename(filename) {
    // Remove .cbl extension
    let name = filename.replace(/\.cbl$/i, '');
    // Extract just the list name - remove [Publisher] and (date) prefix
    // Pattern: [Publisher] (YYYY-MM) List Name
    const match = name.match(/\]\s*\([^)]+\)\s*(.+)$/);
    if (match) {
        return match[1].trim();
    }
    return name;
}

function uploadCBL() {
    console.log('uploadCBL called');
    const fileInput = document.getElementById('cblFile');
    const file = fileInput.files[0];
    if (!file) {
        alert('Please select a file');
        return;
    }

    // Show loading state
    const btn = document.getElementById('uploadBtn');
    const cancelBtn = document.getElementById('uploadCancelBtn');
    btn.disabled = true;
    cancelBtn.disabled = true;
    btn.querySelector('.btn-text').classList.add('d-none');
    btn.querySelector('.btn-loading').classList.remove('d-none');

    // Extract clean list name from filename
    const listName = extractListNameFromFilename(file.name);

    const formData = new FormData();
    formData.append('file', file);

    fetch('/api/reading-lists/upload', {
        method: 'POST',
        body: formData
    })
        .then(response => response.json())
        .then(data => {
            console.log('Upload response:', data);
            if (data.success) {
                if (data.background && data.task_id) {
                    // Close modal and start polling
                    const modal = bootstrap.Modal.getInstance(document.getElementById('uploadCBLModal'));
                    if (modal) modal.hide();
                    pollImportStatus(data.task_id, listName);
                } else {
                    window.location.reload();
                }
            } else {
                alert('Error: ' + data.message);
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('An error occurred during upload');
        })
        .finally(() => {
            // Reset loading state
            btn.disabled = false;
            cancelBtn.disabled = false;
            btn.querySelector('.btn-text').classList.remove('d-none');
            btn.querySelector('.btn-loading').classList.add('d-none');
        });
}

function extractListName(url) {
    // Extract and decode the filename from URL
    let filename = url.split('/').pop() || 'reading list';
    try {
        filename = decodeURIComponent(filename);
    } catch (e) {
        // If decoding fails, use as-is
    }
    // Remove .cbl extension
    filename = filename.replace(/\.cbl$/i, '');
    // Extract just the list name - remove [Publisher] and (date) prefix
    // Pattern: [Publisher] (YYYY-MM) List Name
    const match = filename.match(/\]\s*\([^)]+\)\s*(.+)$/);
    if (match) {
        return match[1].trim();
    }
    return filename;
}

function importGithub() {
    console.log('importGithub called');
    const urlInput = document.getElementById('githubUrl');
    const url = urlInput.value;
    if (!url) {
        alert('Please enter a URL');
        return;
    }

    // Show loading state
    const btn = document.getElementById('importBtn');
    const cancelBtn = document.getElementById('importCancelBtn');
    btn.disabled = true;
    cancelBtn.disabled = true;
    btn.querySelector('.btn-text').classList.add('d-none');
    btn.querySelector('.btn-loading').classList.remove('d-none');

    // Extract clean list name from URL for display
    const filename = extractListName(url);

    fetch('/api/reading-lists/import', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ url: url })
    })
        .then(response => response.json())
        .then(data => {
            console.log('Import response:', data);
            if (data.success) {
                if (data.background && data.task_id) {
                    // Close modal and start polling
                    const modal = bootstrap.Modal.getInstance(document.getElementById('importGithubModal'));
                    if (modal) modal.hide();
                    pollImportStatus(data.task_id, filename);
                } else {
                    window.location.reload();
                }
            } else {
                alert('Error: ' + data.message);
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('An error occurred during import');
        })
        .finally(() => {
            // Reset loading state
            btn.disabled = false;
            cancelBtn.disabled = false;
            btn.querySelector('.btn-text').classList.remove('d-none');
            btn.querySelector('.btn-loading').classList.add('d-none');
        });
}

function deleteReadingList(id) {
    if (!confirm('Are you sure you want to delete this reading list?')) {
        return;
    }

    fetch(`/api/reading-lists/${id}`, {
        method: 'DELETE'
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                window.location.reload();
            } else {
                alert('Error: ' + data.message);
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('An error occurred');
        });
}

function setAsThumbnail(filePath) {
    fetch(`/api/reading-lists/${LIST_ID}/thumbnail`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: filePath })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showToast('Thumbnail updated', 'success');
            } else {
                showToast('Failed to update thumbnail: ' + data.message, 'error');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showToast('An error occurred', 'error');
        });
}

// ==========================================
// Inline Title Editing
// ==========================================

function editTitle(listId, element) {
    const currentName = element.textContent.trim();
    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentName;
    input.className = 'form-control form-control-sm';
    input.style.maxWidth = '200px';
    input.style.display = 'inline-block';

    // Prevent clicks on input from navigating to the card link
    input.addEventListener('click', (e) => e.stopPropagation());

    // Store original element reference
    const originalElement = element.cloneNode(true);

    element.replaceWith(input);
    input.focus();
    input.select();

    let saved = false;

    function saveTitle() {
        if (saved) return;
        saved = true;

        const newName = input.value.trim();
        if (newName && newName !== currentName) {
            fetch(`/api/reading-lists/${listId}/name`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: newName })
            })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        originalElement.textContent = newName;
                        showToast('Title updated', 'success');
                    } else {
                        showToast('Failed to update title', 'error');
                    }
                })
                .catch(() => {
                    showToast('Error updating title', 'error');
                });
        }
        originalElement.textContent = newName || currentName;
        input.replaceWith(originalElement);
    }

    function cancelEdit() {
        if (saved) return;
        saved = true;
        input.replaceWith(originalElement);
    }

    input.addEventListener('blur', saveTitle);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            saveTitle();
        }
        if (e.key === 'Escape') {
            e.preventDefault();
            cancelEdit();
        }
    });
}

// ==========================================
// Tags Modal
// ==========================================

const PREDEFINED_TAGS = ['Event', 'Marvel', 'DC', 'Reading Order', 'Crossover'];
const TAG_ICONS = {
    'Marvel': 'bi-lightning-fill',
    'DC': 'bi-shield-fill',
    'Event': 'bi-calendar-event-fill',
    'Reading Order': 'bi-list-ol',
    'Crossover': 'bi-arrows-move'
};

let currentListIdForTags = null;
let selectedTagsSet = new Set();
let allExistingTags = [];
let tagsModal = null;

function openTagsModal(listId, currentTags = []) {
    currentListIdForTags = listId;
    selectedTagsSet = new Set(currentTags || []);

    // Fetch all existing tags for autocomplete
    fetch('/api/reading-lists/tags')
        .then(r => r.json())
        .then(data => {
            allExistingTags = data.tags || [];
            renderPredefinedTags();
        })
        .catch(() => {
            allExistingTags = [];
            renderPredefinedTags();
        });

    renderSelectedTags();

    // Clear input and set up handlers
    const tagInput = document.getElementById('tagInput');
    if (tagInput) tagInput.value = '';
    hideSuggestions();
    setupTagInputHandlers();

    if (!tagsModal) {
        tagsModal = new bootstrap.Modal(document.getElementById('tagsModal'));
    }
    tagsModal.show();
}

function renderSelectedTags() {
    const container = document.getElementById('selectedTags');
    if (!container) return;

    container.innerHTML = '';
    selectedTagsSet.forEach(tag => {
        const pill = document.createElement('span');
        pill.className = 'tag-pill';
        pill.innerHTML = `
            <i class="bi ${TAG_ICONS[tag] || 'bi-tag-fill'}"></i>
            ${tag}
            <span class="remove-tag" onclick="removeTag('${tag.replace(/'/g, "\\'")}')">
                <i class="bi bi-x"></i>
            </span>
        `;
        container.appendChild(pill);
    });
}

function renderPredefinedTags() {
    const container = document.getElementById('predefinedTags');
    if (!container) return;

    // Combine predefined and existing tags, remove duplicates
    const allTags = [...new Set([...PREDEFINED_TAGS, ...allExistingTags])];

    container.innerHTML = '';
    allTags.forEach(tag => {
        if (!selectedTagsSet.has(tag)) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-outline-secondary btn-sm';
            btn.innerHTML = `<i class="bi ${TAG_ICONS[tag] || 'bi-tag'}"></i> ${tag}`;
            btn.onclick = () => addTag(tag);
            container.appendChild(btn);
        }
    });
}

function addTag(tag) {
    tag = tag.trim();
    if (tag && !selectedTagsSet.has(tag)) {
        selectedTagsSet.add(tag);
        renderSelectedTags();
        renderPredefinedTags();
    }
    // Clear input
    const tagInput = document.getElementById('tagInput');
    if (tagInput) tagInput.value = '';
    hideSuggestions();
}

function removeTag(tag) {
    selectedTagsSet.delete(tag);
    renderSelectedTags();
    renderPredefinedTags();
}

function saveTags() {
    fetch(`/api/reading-lists/${currentListIdForTags}/tags`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tags: Array.from(selectedTagsSet) })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                tagsModal.hide();
                showToast('Tags updated', 'success');
                location.reload();
            } else {
                showToast('Failed to update tags: ' + data.message, 'error');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showToast('Error saving tags', 'error');
        });
}

function showSuggestions(suggestions) {
    const container = document.getElementById('tagSuggestions');
    if (!container) return;

    container.innerHTML = '';
    suggestions.forEach(tag => {
        const item = document.createElement('a');
        item.href = '#';
        item.className = 'list-group-item list-group-item-action';
        item.innerHTML = `<i class="bi ${TAG_ICONS[tag] || 'bi-tag'} me-2"></i>${tag}`;
        item.onclick = (e) => {
            e.preventDefault();
            addTag(tag);
        };
        container.appendChild(item);
    });
}

function hideSuggestions() {
    const container = document.getElementById('tagSuggestions');
    if (container) container.innerHTML = '';
}

// Set up tag input event handlers (called when modal opens)
function setupTagInputHandlers() {
    const tagInput = document.getElementById('tagInput');
    if (!tagInput || tagInput.dataset.handlersAttached) return;
    tagInput.dataset.handlersAttached = 'true';

    tagInput.addEventListener('input', function (e) {
        let value = e.target.value;

        // Check for comma - add tag when comma is typed
        if (value.includes(',')) {
            const parts = value.split(',');
            parts.forEach((part, index) => {
                const tag = part.trim();
                if (tag && index < parts.length - 1) {
                    // Add all complete tags (before the last comma)
                    addTag(tag);
                }
            });
            // Keep only the part after the last comma
            e.target.value = parts[parts.length - 1];
            value = e.target.value;
        }

        const query = value.toLowerCase().trim();
        if (!query) {
            hideSuggestions();
            return;
        }

        const allTags = [...new Set([...PREDEFINED_TAGS, ...allExistingTags])];
        const suggestions = allTags.filter(t =>
            t.toLowerCase().includes(query) && !selectedTagsSet.has(t)
        );

        if (suggestions.length > 0) {
            showSuggestions(suggestions);
        } else {
            hideSuggestions();
        }
    });

    tagInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            const value = e.target.value.trim();
            if (value) {
                addTag(value);
            }
        }
    });
}

// Hide suggestions when clicking outside
document.addEventListener('click', function (e) {
    if (!e.target.closest('#tagInput') && !e.target.closest('#tagSuggestions')) {
        hideSuggestions();
    }
});

// Mapping Logic
let currentEntryId = null;
let selectedFilePath = null;
let mapModal = null;

function formatSearchTerm(series, number, volume, year) {
    // Use RENAME_PATTERN if defined, otherwise default format
    let pattern = (typeof RENAME_PATTERN !== 'undefined' && RENAME_PATTERN)
        ? RENAME_PATTERN
        : '{series_name} {issue_number}';

    // Replace ':' with ' -' in series name (e.g., "Batman: The Dark Knight" -> "Batman - The Dark Knight")
    let cleanSeries = (series || '').replace(/:/g, ' -');

    // Pad issue number to 3 digits
    const paddedNumber = number.toString().padStart(3, '0');

    // Replace placeholders
    let searchTerm = pattern
        .replace('{series_name}', cleanSeries)
        .replace('{series}', cleanSeries)
        .replace('{issue_number}', paddedNumber)
        .replace('{issue}', paddedNumber)
        .replace('{volume}', volume || '')
        .replace('{year}', year || '')
        .replace('{start_year}', volume || year || '');

    // Clean up any remaining empty placeholders and extra spaces
    searchTerm = searchTerm.replace(/\{[^}]+\}/g, '').replace(/\s+/g, ' ').trim();

    // Remove empty parentheses that might result from missing values
    searchTerm = searchTerm.replace(/\(\s*\)/g, '').trim();

    return searchTerm;
}

function openMapModal(entryId, series, number, volume, year) {
    currentEntryId = entryId;
    selectedFilePath = null;
    document.getElementById('mapTargetName').textContent = `${series} #${number}`;

    // Format search term using rename pattern
    const searchTerm = formatSearchTerm(series, number, volume, year);
    document.getElementById('fileSearchInput').value = searchTerm;

    document.getElementById('searchResults').innerHTML = '';
    document.getElementById('confirmMapBtn').disabled = true;

    if (!mapModal) {
        mapModal = new bootstrap.Modal(document.getElementById('mapFileModal'));
    }
    mapModal.show();

    // Auto search
    searchFiles();
}

function searchFiles(retryWithoutFirstWord = false) {
    let query = document.getElementById('fileSearchInput').value;
    if (!query) return;

    // If retrying, remove the first word (e.g., "The Flash 094" -> "Flash 094")
    if (retryWithoutFirstWord) {
        const words = query.split(' ');
        if (words.length > 1) {
            query = words.slice(1).join(' ');
            console.log(`[Search] Retrying without first word: "${query}"`);
        } else {
            // Only one word, can't retry
            return;
        }
    }

    const resultsDiv = document.getElementById('searchResults');
    resultsDiv.innerHTML = '<div class="text-center p-3"><div class="spinner-border text-primary" role="status"></div></div>';

    fetch(`/api/reading-lists/search-file?q=${encodeURIComponent(query)}`)
        .then(response => response.json())
        .then(results => {
            resultsDiv.innerHTML = '';
            if (results.length === 0) {
                // If no results and haven't tried without first word yet, retry
                if (!retryWithoutFirstWord) {
                    const words = document.getElementById('fileSearchInput').value.split(' ');
                    if (words.length > 1) {
                        console.log('[Search] No results, trying without first word...');
                        searchFiles(true);
                        return;
                    }
                }
                resultsDiv.innerHTML = '<div class="p-3 text-center text-muted">No files found</div>';
                return;
            }

            results.forEach(file => {
                const item = document.createElement('div');
                item.className = 'list-group-item list-group-item-action search-result-item';
                item.innerHTML = `
                <div class="d-flex w-100 justify-content-between">
                    <h6 class="mb-1 text-truncate">${file.name}</h6>
                    <small class="text-muted">${file.path.split('/').slice(-2, -1)[0]}</small>
                </div>
                <small class="text-muted text-break">${file.path}</small>
            `;
                item.onclick = () => selectFile(file.path, item);
                resultsDiv.appendChild(item);
            });
        })
        .catch(error => {
            console.error('Error:', error);
            resultsDiv.innerHTML = '<div class="text-danger p-3">Error searching files</div>';
        });

    // Add enter key listener
    const input = document.getElementById('fileSearchInput');
    input.onkeypress = function (e) {
        if (e.keyCode === 13) {
            searchFiles();
        }
    };
}

function selectFile(path, element) {
    selectedFilePath = path;

    // UI update
    document.querySelectorAll('.search-result-item').forEach(el => el.classList.remove('active'));
    element.classList.add('active');

    document.getElementById('confirmMapBtn').disabled = false;
}

function confirmMapping() {
    if (!currentEntryId || !selectedFilePath) return;

    fetch(`/api/reading-lists/${LIST_ID}/map`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            entry_id: currentEntryId,
            file_path: selectedFilePath
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                location.reload();
            } else {
                alert('Error: ' + data.message);
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('An error occurred');
        });
}

function clearMapping() {
    if (!confirm('Are you sure you want to clear the mapping for this issue?')) return;

    selectedFilePath = null; // Send null to clear

    fetch(`/api/reading-lists/${LIST_ID}/map`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            entry_id: currentEntryId,
            file_path: null
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                location.reload();
            } else {
                alert('Error: ' + data.message);
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('An error occurred');
        });
}

// ==========================================
// Comic Reader Functions
// ==========================================
let currentComicPath = null;
let currentComicPageCount = 0;
let comicReaderSwiper = null;

// Reading list navigation
let readingListEntries = [];      // All matched entries [{path, name, thumbnail, series, issue}, ...]
let currentEntryIndex = -1;       // Index in readingListEntries

// Reading progress tracking
let savedReadingPosition = null;  // Saved page for resume
let highestPageViewed = 0;        // Track progress
let nextIssueOverlayShown = false;
let readingStartTime = null;
let accumulatedTime = 0;
let pageEdgeColors = new Map();   // Cache of extracted edge colors per page index

// Read status tracking
let readIssuesSet = new Set();

// Immersive reader chrome state
let readerChromeHidden = false;
let chromeToggleTimeout = null;

function isMobileOrTablet() {
    return window.matchMedia('(max-width: 1024px)').matches;
}

function toggleReaderChrome() {
    const container = document.querySelector('.comic-reader-container');
    if (!container) return;
    readerChromeHidden = !readerChromeHidden;
    container.classList.toggle('reader-chrome-hidden', readerChromeHidden);
}

function openComicReader(filePath) {
    currentComicPath = filePath;
    savedReadingPosition = null;
    highestPageViewed = 0;
    nextIssueOverlayShown = false;
    accumulatedTime = 0;
    readingStartTime = Date.now();
    pageEdgeColors = new Map();

    // Find index in reading list entries
    currentEntryIndex = readingListEntries.findIndex(e => e.path === filePath);

    const modal = document.getElementById('comicReaderModal');
    const titleEl = document.getElementById('comicReaderTitle');
    const pageInfoEl = document.getElementById('comicReaderPageInfo');

    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';

    // Immersive mode: hide chrome by default on mobile/tablet
    if (isMobileOrTablet()) {
        const container = document.querySelector('.comic-reader-container');
        if (container) {
            container.classList.add('reader-chrome-hidden');
            readerChromeHidden = true;
        }
    }

    const fileName = filePath.split(/[/\\]/).pop();
    titleEl.textContent = fileName;
    pageInfoEl.textContent = 'Loading...';

    // Hide any overlays from previous sessions
    hideNextIssueOverlay();
    hideResumeReadingOverlay();

    // Encode path for URL - handle both forward and back slashes
    const encodedPath = filePath.replace(/\\/g, '/').split('/').map(encodeURIComponent).join('/');

    // Fetch comic info and saved reading position in parallel
    Promise.all([
        fetch(`/api/read/${encodedPath}/info`).then(r => r.json()),
        fetch(`/api/reading-position?path=${encodeURIComponent(filePath)}`).then(r => r.json()).catch(() => ({ page_number: null }))
    ])
        .then(([comicData, positionData]) => {
            if (!comicData.success) {
                alert('Failed to load comic: ' + (comicData.error || 'Unknown error'));
                closeComicReader();
                return;
            }

            const pageCount = comicData.page_count;

            // Get accumulated time if available
            if (positionData && positionData.time_spent) {
                accumulatedTime = positionData.time_spent;
            }

            // Check if we have a saved reading position
            if (positionData && positionData.page_number !== null && positionData.page_number > 0) {
                savedReadingPosition = positionData.page_number;
                // Show resume prompt
                showResumeReadingOverlay(positionData.page_number, pageCount);
                // Initialize reader but don't navigate yet
                initializeComicReader(pageCount, 0);
                updateBookmarkButtonState(true);
            } else {
                initializeComicReader(pageCount, 0);
            }
        })
        .catch(error => {
            console.error('Error loading comic:', error);
            alert('An error occurred while loading the comic.');
            closeComicReader();
        });
}

function closeComicReader() {
    // Smart auto-save/cleanup logic for reading position
    if (currentComicPath && currentComicPageCount > 0) {
        const currentPage = comicReaderSwiper ? comicReaderSwiper.activeIndex + 1 : 1;
        const progress = ((highestPageViewed + 1) / currentComicPageCount) * 100;
        const withinLastPages = currentPage > currentComicPageCount - 3;

        if (progress >= 90 || withinLastPages) {
            // Calculate final time spent
            let sessionTime = (Date.now() - readingStartTime) / 1000;
            if (sessionTime < 10) sessionTime = 0;
            const totalTime = Math.round(accumulatedTime + sessionTime);

            // User finished or nearly finished - mark as read and delete bookmark
            fetch('/api/mark-comic-read', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    path: currentComicPath,
                    page_count: currentComicPageCount,
                    time_spent: totalTime
                })
            }).then(() => {
                readIssuesSet.add(currentComicPath);
                updateReadIcon(currentComicPath, true);
            }).catch(err => console.error('Failed to mark comic as read:', err));

            // Delete saved reading position (fire and forget)
            fetch(`/api/reading-position?path=${encodeURIComponent(currentComicPath)}`, {
                method: 'DELETE'
            }).catch(err => console.error('Failed to delete reading position:', err));
        } else if (currentPage > 1) {
            // User stopped mid-read - auto-save position silently
            let sessionTime = (Date.now() - readingStartTime) / 1000;
            if (sessionTime < 10) sessionTime = 0;
            const totalTime = Math.round(accumulatedTime + sessionTime);

            fetch('/api/reading-position', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    comic_path: currentComicPath,
                    page_number: currentPage,
                    total_pages: currentComicPageCount,
                    time_spent: totalTime
                })
            }).catch(err => console.error('Failed to auto-save reading position:', err));
        }
    }

    // Reset dynamic background colors before hiding
    resetReaderBackgroundColor();
    pageEdgeColors = new Map();

    // Reset immersive reader chrome state
    const container = document.querySelector('.comic-reader-container');
    if (container) {
        container.classList.remove('reader-chrome-hidden');
    }
    readerChromeHidden = false;
    if (chromeToggleTimeout) {
        clearTimeout(chromeToggleTimeout);
        chromeToggleTimeout = null;
    }

    const modal = document.getElementById('comicReaderModal');
    modal.style.display = 'none';
    document.body.style.overflow = '';

    if (comicReaderSwiper) {
        comicReaderSwiper.destroy(true, true);
        comicReaderSwiper = null;
    }

    // Clear state
    currentComicPath = null;
    currentComicPageCount = 0;
    highestPageViewed = 0;
    nextIssueOverlayShown = false;
    savedReadingPosition = null;

    // Hide overlays
    hideNextIssueOverlay();
    hideResumeReadingOverlay();
}

function initializeComicReader(pageCount, startPage) {
    currentComicPageCount = pageCount;
    const wrapper = document.getElementById('comicReaderWrapper');
    const pageInfoEl = document.getElementById('comicReaderPageInfo');

    wrapper.innerHTML = '';

    // Create empty slides - images loaded on demand
    for (let i = 0; i < pageCount; i++) {
        const slide = document.createElement('div');
        slide.className = 'swiper-slide';
        slide.dataset.pageNum = i;
        slide.innerHTML = '<div class="swiper-lazy-preloader"></div>';
        wrapper.appendChild(slide);
    }

    comicReaderSwiper = new Swiper('#comicReaderSwiper', {
        slidesPerView: 1,
        spaceBetween: 0,
        initialSlide: startPage,
        keyboard: { enabled: true },
        navigation: {
            nextEl: '.swiper-button-next',
            prevEl: '.swiper-button-prev',
        },
        pagination: {
            el: '.swiper-pagination',
            type: 'bullets',
            clickable: true,
        },
        on: {
            slideChange: function () {
                const currentIndex = this.activeIndex;
                const currentPage = currentIndex + 1;
                pageInfoEl.textContent = `Page ${currentPage} of ${pageCount}`;

                // Track highest page viewed
                if (currentIndex > highestPageViewed) {
                    highestPageViewed = currentIndex;
                }

                // Update progress bar
                const progressFill = document.querySelector('.comic-reader-progress-fill');
                const progressText = document.querySelector('.comic-reader-progress-text');
                if (progressFill && progressText) {
                    const percent = Math.round((currentPage / pageCount) * 100);
                    progressFill.style.width = percent + '%';
                    progressText.textContent = percent + '%';
                }

                // Preload current page
                loadComicPage(currentIndex);

                // Preload next 2 pages
                if (currentIndex + 1 < pageCount) {
                    loadComicPage(currentIndex + 1);
                }
                if (currentIndex + 2 < pageCount) {
                    loadComicPage(currentIndex + 2);
                }

                // Preload previous page for backward navigation
                if (currentIndex - 1 >= 0) {
                    loadComicPage(currentIndex - 1);
                }

                // Check if on last page - show next issue overlay
                if (currentIndex === pageCount - 1) {
                    checkAndShowNextIssueOverlay();
                } else {
                    hideNextIssueOverlay();
                }

                // Apply cached edge color for this page
                const cachedColor = pageEdgeColors.get(currentIndex);
                if (cachedColor) {
                    applyReaderBackgroundColor(cachedColor.r, cachedColor.g, cachedColor.b);
                }
            },
            // Single tap: toggle chrome on mobile/tablet (with delay to avoid conflict with navigation taps)
            tap: function (swiper, event) {
                if (!isMobileOrTablet()) return;
                // Don't toggle chrome when zoomed in (user is panning)
                if (this.zoom && this.zoom.scale > 1) return;
                // Don't toggle chrome when tapping navigation buttons
                if (event && event.target && event.target.closest('.swiper-button-next, .swiper-button-prev')) return;
                // Start a 300ms timer; if a double-tap comes, it will cancel this
                chromeToggleTimeout = setTimeout(function () {
                    chromeToggleTimeout = null;
                    toggleReaderChrome();
                }, 300);
            }
        }
    });

    const initialPage = startPage + 1;
    pageInfoEl.textContent = `Page ${initialPage} of ${pageCount}`;
    // Initialize progress
    const progressFill = document.querySelector('.comic-reader-progress-fill');
    const progressText = document.querySelector('.comic-reader-progress-text');
    if (progressFill && progressText) {
        const percent = Math.round((initialPage / pageCount) * 100);
        progressFill.style.width = percent + '%';
        progressText.textContent = percent + '%';
    }

    // Update bookmark button state
    updateBookmarkButtonState(savedReadingPosition !== null);

    // Preload initial pages
    loadComicPage(startPage);
    if (startPage + 1 < pageCount) loadComicPage(startPage + 1);
    if (startPage + 2 < pageCount) loadComicPage(startPage + 2);
    if (startPage - 1 >= 0) loadComicPage(startPage - 1);
}

/**
 * Extract the average edge color from an image by sampling pixels along all 4 edges
 */
function extractEdgeColor(img) {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');

    const scale = Math.min(100 / img.naturalWidth, 100 / img.naturalHeight, 1);
    const w = Math.max(1, Math.round(img.naturalWidth * scale));
    const h = Math.max(1, Math.round(img.naturalHeight * scale));
    canvas.width = w;
    canvas.height = h;
    ctx.drawImage(img, 0, 0, w, h);

    const imageData = ctx.getImageData(0, 0, w, h);
    const data = imageData.data;
    let rSum = 0, gSum = 0, bSum = 0, count = 0;

    function addPixel(x, y) {
        const idx = (y * w + x) * 4;
        rSum += data[idx];
        gSum += data[idx + 1];
        bSum += data[idx + 2];
        count++;
    }

    for (let x = 0; x < w; x++) {
        addPixel(x, 0);
        addPixel(x, h - 1);
    }
    for (let y = 1; y < h - 1; y++) {
        addPixel(0, y);
        addPixel(w - 1, y);
    }

    if (count === 0) return { r: 0, g: 0, b: 0 };
    return {
        r: Math.round(rSum / count),
        g: Math.round(gSum / count),
        b: Math.round(bSum / count)
    };
}

/**
 * Apply a darkened version of the given color to the reader chrome elements
 */
function applyReaderBackgroundColor(r, g, b) {
    const overlay = document.querySelector('.comic-reader-overlay');
    const header = document.querySelector('.comic-reader-header');
    const footer = document.querySelector('.comic-reader-footer');
    const slides = document.querySelectorAll('.comic-reader-swiper .swiper-slide');

    if (overlay) overlay.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
    if (header) header.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
    if (footer) footer.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
    slides.forEach(slide => {
        slide.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
    });
}

/**
 * Reset reader chrome background colors to CSS defaults
 */
function resetReaderBackgroundColor() {
    const overlay = document.querySelector('.comic-reader-overlay');
    const header = document.querySelector('.comic-reader-header');
    const footer = document.querySelector('.comic-reader-footer');
    const slides = document.querySelectorAll('.comic-reader-swiper .swiper-slide');

    if (overlay) overlay.style.backgroundColor = '';
    if (header) header.style.backgroundColor = '';
    if (footer) footer.style.backgroundColor = '';
    slides.forEach(slide => {
        slide.style.backgroundColor = '';
    });
}

function loadComicPage(pageNum) {
    const slide = document.querySelector(`.swiper-slide[data-page-num="${pageNum}"]`);
    if (!slide) return;

    // Check if already loaded or loading
    if (slide.querySelector('img') || slide.dataset.loading === 'true') return;

    // Mark as loading to prevent duplicate requests
    slide.dataset.loading = 'true';

    const encodedPath = currentComicPath.replace(/\\/g, '/').split('/').map(encodeURIComponent).join('/');
    const imageUrl = `/api/read/${encodedPath}/page/${pageNum}`;

    const img = document.createElement('img');
    img.src = imageUrl;
    img.alt = `Page ${pageNum + 1}`;
    img.decoding = 'async';

    // Set fetch priority based on distance from current page
    const currentIndex = comicReaderSwiper ? comicReaderSwiper.activeIndex : 0;
    img.fetchPriority = Math.abs(pageNum - currentIndex) <= 1 ? 'high' : 'low';

    img.onload = function () {
        // Remove preloader and add image
        slide.innerHTML = '';
        slide.appendChild(img);
        delete slide.dataset.loading;

        // Extract and cache edge color for dynamic background
        try {
            const color = extractEdgeColor(img);
            pageEdgeColors.set(pageNum, color);
            // If this is the currently active slide, apply color immediately
            if (comicReaderSwiper && comicReaderSwiper.activeIndex === pageNum) {
                applyReaderBackgroundColor(color.r, color.g, color.b);
            }
        } catch (e) {
            // Silently ignore color extraction failures (e.g., CORS)
        }
    };

    img.onerror = function () {
        slide.innerHTML = '<div class="text-center text-muted p-4">Failed to load page</div>';
        delete slide.dataset.loading;
    };
}

// ==========================================
// Overlay Functions
// ==========================================

function checkAndShowNextIssueOverlay() {
    if (currentEntryIndex >= 0 && currentEntryIndex + 1 < readingListEntries.length) {
        const nextEntry = readingListEntries[currentEntryIndex + 1];
        showNextIssueOverlay(nextEntry);
    }
}

function showNextIssueOverlay(nextEntry) {
    if (nextIssueOverlayShown) return;

    const overlay = document.getElementById('nextIssueOverlay');
    const thumbnail = document.getElementById('nextIssueThumbnail');
    const nameEl = document.getElementById('nextIssueName');

    if (!overlay || !thumbnail || !nameEl) return;

    nameEl.textContent = `${nextEntry.series} #${nextEntry.issue}`;
    thumbnail.src = nextEntry.thumbnail || '/static/img/placeholder.png';
    thumbnail.onerror = function () { this.src = '/static/img/placeholder.png'; };

    overlay.style.display = 'flex';
    nextIssueOverlayShown = true;
}

function hideNextIssueOverlay() {
    const overlay = document.getElementById('nextIssueOverlay');
    if (overlay) {
        overlay.style.display = 'none';
    }
    nextIssueOverlayShown = false;
}

function showResumeReadingOverlay(pageNumber, totalPages) {
    const overlay = document.getElementById('resumeReadingOverlay');
    const info = document.getElementById('resumeReadingInfo');

    if (!overlay || !info) return;

    info.textContent = `Continue from page ${pageNumber} of ${totalPages}?`;
    overlay.style.display = 'flex';
}

function hideResumeReadingOverlay() {
    const overlay = document.getElementById('resumeReadingOverlay');
    if (overlay) {
        overlay.style.display = 'none';
    }
}

function saveReadingPosition() {
    if (!currentComicPath || !comicReaderSwiper) return;

    const currentPage = comicReaderSwiper.activeIndex + 1; // 1-indexed

    // Calculate time spent in this session
    let sessionTime = (Date.now() - readingStartTime) / 1000;
    if (sessionTime < 10) sessionTime = 0; // Ignore quick previews
    const totalTime = Math.round(accumulatedTime + sessionTime);

    fetch('/api/reading-position', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            comic_path: currentComicPath,
            page_number: currentPage,
            total_pages: currentComicPageCount,
            time_spent: totalTime
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                savedReadingPosition = currentPage;
                updateBookmarkButtonState(true);
                // Visual feedback
                showToast(`Position saved: Page ${currentPage}`, 'success', 2000);
            }
        })
        .catch(error => {
            console.error('Error saving reading position:', error);
        });
}

function updateBookmarkButtonState(hasSavedPosition) {
    const btn = document.getElementById('comicReaderBookmark');
    if (!btn) return;

    const icon = btn.querySelector('i');
    if (icon) {
        if (hasSavedPosition) {
            icon.classList.remove('bi-bookmark');
            icon.classList.add('bi-bookmark-fill');
        } else {
            icon.classList.remove('bi-bookmark-fill');
            icon.classList.add('bi-bookmark');
        }
    }
}

function continueToNextIssue() {
    if (currentEntryIndex < 0 || currentEntryIndex + 1 >= readingListEntries.length) {
        return;
    }

    const nextEntry = readingListEntries[currentEntryIndex + 1];

    // Calculate time spent
    let sessionTime = (Date.now() - readingStartTime) / 1000;
    const totalTime = Math.round(accumulatedTime + sessionTime);

    // Mark current comic as read
    fetch('/api/mark-comic-read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            path: currentComicPath,
            page_count: currentComicPageCount,
            time_spent: totalTime
        })
    }).then(() => {
        // Update read icon in grid
        updateReadIcon(currentComicPath, true);
        // Add to local set
        readIssuesSet.add(currentComicPath);
    });

    // Delete saved reading position
    fetch(`/api/reading-position?path=${encodeURIComponent(currentComicPath)}`, {
        method: 'DELETE'
    });

    // Close current reader and open next
    const modal = document.getElementById('comicReaderModal');
    modal.style.display = 'none';

    if (comicReaderSwiper) {
        comicReaderSwiper.destroy(true, true);
        comicReaderSwiper = null;
    }

    // Open next comic
    openComicReader(nextEntry.path);
}

function markCurrentAsReadAndClose() {
    // Calculate time spent
    let sessionTime = (Date.now() - readingStartTime) / 1000;
    const totalTime = Math.round(accumulatedTime + sessionTime);

    // Mark current comic as read
    fetch('/api/mark-comic-read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            path: currentComicPath,
            page_count: currentComicPageCount,
            time_spent: totalTime
        })
    }).then(() => {
        // Update read icon in grid
        updateReadIcon(currentComicPath, true);
        // Add to local set
        readIssuesSet.add(currentComicPath);
    });

    // Delete saved reading position
    fetch(`/api/reading-position?path=${encodeURIComponent(currentComicPath)}`, {
        method: 'DELETE'
    });

    closeComicReader();
}

function updateReadIcon(comicPath, isRead) {
    // Find the book cover with this path and update its read icon
    const covers = document.querySelectorAll('.book-cover[data-file-path]');
    covers.forEach(cover => {
        if (cover.dataset.filePath === comicPath) {
            const readIcon = cover.querySelector('.read-icon');
            if (readIcon) {
                if (isRead) {
                    readIcon.classList.remove('bi-book');
                    readIcon.classList.add('bi-book-fill');
                } else {
                    readIcon.classList.remove('bi-book-fill');
                    readIcon.classList.add('bi-book');
                }
            }
        }
    });
}

function buildReadingListEntries() {
    readingListEntries = [];
    const bookCards = document.querySelectorAll('.book-card');

    bookCards.forEach(card => {
        const cover = card.querySelector('.book-cover[data-file-path]');
        if (cover) {
            const filePath = cover.dataset.filePath;
            const series = card.dataset.series || '';
            const issue = card.dataset.issue || '';
            const thumbnailUrl = cover.style.backgroundImage.replace(/url\(['"]?([^'"]+)['"]?\)/, '$1');

            readingListEntries.push({
                path: filePath,
                series: series,
                issue: issue,
                thumbnail: thumbnailUrl,
                name: `${series} #${issue}`
            });
        }
    });

    console.log(`Built reading list entries: ${readingListEntries.length} matched issues`);
}

function loadReadIssues() {
    fetch('/api/issues-read-paths')
        .then(r => r.json())
        .then(data => {
            readIssuesSet = new Set(data.paths || []);
            console.log(`Loaded ${readIssuesSet.size} read issues`);

            // Update icons for already-read issues
            const covers = document.querySelectorAll('.book-cover[data-file-path]');
            covers.forEach(cover => {
                const filePath = cover.dataset.filePath;
                if (readIssuesSet.has(filePath)) {
                    const readIcon = cover.querySelector('.read-icon');
                    if (readIcon) {
                        readIcon.classList.remove('bi-book');
                        readIcon.classList.add('bi-book-fill');
                    }
                }
            });
        })
        .catch(err => console.warn('Failed to load read issues:', err));
}

// ==========================================
// Set up event listeners when DOM is ready
// ==========================================
document.addEventListener('DOMContentLoaded', function () {
    // Build the reading list entries from DOM
    buildReadingListEntries();

    // Load read issues for status icons
    loadReadIssues();

    // Close button
    const closeBtn = document.getElementById('comicReaderClose');
    if (closeBtn) {
        closeBtn.addEventListener('click', closeComicReader);
    }

    // Overlay click to close
    const overlay = document.querySelector('.comic-reader-overlay');
    if (overlay) {
        overlay.addEventListener('click', closeComicReader);
    }

    // Bookmark button
    const bookmarkBtn = document.getElementById('comicReaderBookmark');
    if (bookmarkBtn) {
        bookmarkBtn.addEventListener('click', saveReadingPosition);
    }

    // Resume reading overlay buttons
    const resumeYes = document.getElementById('resumeReadingYes');
    if (resumeYes) {
        resumeYes.addEventListener('click', function () {
            hideResumeReadingOverlay();
            if (comicReaderSwiper && savedReadingPosition) {
                comicReaderSwiper.slideTo(savedReadingPosition - 1); // Convert 1-indexed to 0-indexed
            }
        });
    }

    const resumeNo = document.getElementById('resumeReadingNo');
    if (resumeNo) {
        resumeNo.addEventListener('click', function () {
            hideResumeReadingOverlay();
            if (comicReaderSwiper) {
                comicReaderSwiper.slideTo(0);
            }
            savedReadingPosition = null;
            updateBookmarkButtonState(false);
        });
    }

    // Next issue overlay buttons
    const nextIssueContinue = document.getElementById('nextIssueContinue');
    if (nextIssueContinue) {
        nextIssueContinue.addEventListener('click', continueToNextIssue);
    }

    const nextIssueClose = document.getElementById('nextIssueClose');
    if (nextIssueClose) {
        nextIssueClose.addEventListener('click', markCurrentAsReadAndClose);
    }

    // Click outside next issue overlay to dismiss
    const nextIssueOverlay = document.getElementById('nextIssueOverlay');
    if (nextIssueOverlay) {
        nextIssueOverlay.addEventListener('click', function (e) {
            if (e.target === nextIssueOverlay) {
                hideNextIssueOverlay();
            }
        });
    }

    // Click outside resume overlay to dismiss
    const resumeOverlay = document.getElementById('resumeReadingOverlay');
    if (resumeOverlay) {
        resumeOverlay.addEventListener('click', function (e) {
            if (e.target === resumeOverlay) {
                hideResumeReadingOverlay();
                // Start from beginning if dismissed
                if (comicReaderSwiper) {
                    comicReaderSwiper.slideTo(0);
                }
            }
        });
    }

    // Escape key to close reader
    document.addEventListener('keydown', function (e) {
        const modal = document.getElementById('comicReaderModal');
        if (e.key === 'Escape' && modal && modal.style.display === 'flex') {
            closeComicReader();
        }
    });
});
