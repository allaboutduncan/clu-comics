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

// ==========================================
// Sort System
// ==========================================

function initSortButtons() {
    document.querySelectorAll('.sort-btn').forEach(btn => {
        btn.addEventListener('click', () => applySort(btn));
    });
}

function applySort(btn) {
    document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    const grid = document.querySelector('.reading-list-grid');
    if (!grid) return;

    const cards = Array.from(grid.querySelectorAll('.reading-list-card'));
    const mode = btn.dataset.sort;

    cards.sort((a, b) => {
        switch (mode) {
            case 'name-asc':
                return (a.dataset.name || '').localeCompare(b.dataset.name || '');
            case 'name-desc':
                return (b.dataset.name || '').localeCompare(a.dataset.name || '');
            case 'date-asc':
                return (a.dataset.created || '').localeCompare(b.dataset.created || '');
            case 'date-desc':
            default:
                return (b.dataset.created || '').localeCompare(a.dataset.created || '');
        }
    });

    cards.forEach(card => grid.appendChild(card));
}

document.addEventListener('DOMContentLoaded', initSortButtons);

// ==========================================
// Want to Read (dashboard bookmark)
// ==========================================
// Bookmarking a reading list puts it in the Want to Read section of Browse
// Library, and is also the opt-in that surfaces the list's next unread issue in
// On the Stack. The endpoints live under /api/favorites so Readers can use them.

// .want-to-read-toggle is the JS hook shared by both variants: the round
// overlay on the grid cards (.want-to-read-button) and the labelled button in
// the list detail header.
function _wantToReadButtons(listId) {
    return document.querySelectorAll(`.want-to-read-toggle[data-list-id="${listId}"]`);
}

function _paintWantToReadButton(button, marked) {
    const icon = button.querySelector('i');
    const label = button.querySelector('.want-to-read-label');
    const title = marked ? 'Remove from Want to Read' : 'Add to Want to Read';
    button.classList.toggle('marked', marked);
    if (icon) {
        icon.className = (marked ? 'bi bi-bookmark-fill' : 'bi bi-bookmark-plus')
            + (label ? ' me-1' : '');
    }
    if (label) label.textContent = marked ? 'In Want to Read' : 'Want to Read';
    button.title = title;
    button.setAttribute('aria-label', title);
}

// Seed the bookmark buttons from the server so they render in the right state
// on load. Silently leaves them unmarked if the fetch fails.
function loadWantToReadState() {
    const buttons = document.querySelectorAll('.want-to-read-toggle');
    if (!buttons.length) return;

    fetch('/api/favorites/to-read/reading-lists')
        .then(r => r.json())
        .then(data => {
            if (!data.success) return;
            const marked = new Set((data.lists || []).map(l => String(l.id)));
            buttons.forEach(btn => {
                if (marked.has(btn.dataset.listId)) _paintWantToReadButton(btn, true);
            });
        })
        .catch(err => console.error('Error loading Want to Read state:', err));
}

document.addEventListener('DOMContentLoaded', loadWantToReadState);

function toggleReadingListWantToRead(listId, name, button) {
    const marked = button.classList.contains('marked');
    const buttons = _wantToReadButtons(listId);

    // Optimistic update — reverted below if the request fails.
    buttons.forEach(btn => _paintWantToReadButton(btn, !marked));

    fetch('/api/favorites/to-read/reading-lists', {
        method: marked ? 'DELETE' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ list_id: listId })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showToast(
                    marked
                        ? `${name} removed from Want to Read`
                        : `${name} added to Want to Read`,
                    'success'
                );
            } else {
                buttons.forEach(btn => _paintWantToReadButton(btn, marked));
                showToast('Error: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(err => {
            console.error('Error updating Want to Read:', err);
            buttons.forEach(btn => _paintWantToReadButton(btn, marked));
            showToast('Failed to update Want to Read', 'error');
        });
}

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

// Poll for import task completion (progress is shown in the navbar ops-indicator)
function pollImportStatus(taskId, filename) {
    console.log(`[Poll] Starting to poll for task: ${taskId}`);
    const pollInterval = 2000;

    function checkStatus() {
        fetch(`/api/reading-lists/import-status/${taskId}`)
            .then(response => response.json())
            .then(data => {
                if (!data.success) {
                    showToast('Import task not found', 'error');
                    return;
                }

                if (data.status === 'complete') {
                    showToast(`Imported "${data.list_name}" (${data.processed} issues)`, 'success', 8000);
                    // Reload if still on the reading lists page
                    if (window.location.pathname === '/reading-lists') {
                        setTimeout(() => window.location.reload(), 2000);
                    }
                } else if (data.status === 'error') {
                    showToast(`Import failed: ${data.message}`, 'error', 10000);
                } else {
                    setTimeout(checkStatus, pollInterval);
                }
            })
            .catch(error => {
                console.error('Error checking import status:', error);
                setTimeout(checkStatus, pollInterval * 2);
            });
    }

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
                    // Close modal — progress shown in navbar ops-indicator
                    const modal = bootstrap.Modal.getInstance(document.getElementById('uploadCBLModal'));
                    if (modal) modal.hide();
                    showToast(`Importing "${listName}" — track progress in the navbar`, 'info', 5000);
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
                    // Close modal — progress shown in navbar ops-indicator
                    const modal = bootstrap.Modal.getInstance(document.getElementById('importGithubModal'));
                    if (modal) modal.hide();
                    showToast(`Importing "${filename}" — track progress in the navbar`, 'info', 5000);
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
    // Show confirmation toast instead of JS alert
    const toastContainer = getToastContainer();

    const toast = document.createElement('div');
    toast.className = 'toast align-items-center text-white bg-danger border-0 show';
    toast.setAttribute('role', 'alert');
    toast.innerHTML = `
        <div class="toast-body">
            <div class="mb-2">Delete this reading list?</div>
            <div class="d-flex gap-2">
                <button class="btn btn-warning btn-sm confirm-delete-btn">Delete</button>
                <button class="btn btn-light btn-sm cancel-delete-btn">Cancel</button>
            </div>
        </div>
    `;

    toast.querySelector('.cancel-delete-btn').addEventListener('click', function () {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    });

    toast.querySelector('.confirm-delete-btn').addEventListener('click', function () {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
        confirmDelete(id);
    });

    toastContainer.appendChild(toast);
}

function dismissToast(toastId) {
    const el = document.getElementById(toastId);
    if (el) {
        el.classList.remove('show');
        setTimeout(() => el.remove(), 300);
    }
}

function confirmDelete(id) {
    fetch(`/api/reading-lists/${id}`, {
        method: 'DELETE'
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Animate the card out then reload
                const card = document.querySelector(`.reading-list-card[onclick*="list_id=${id}"]`);
                if (card) {
                    card.style.transition = 'opacity 0.3s, transform 0.3s';
                    card.style.opacity = '0';
                    card.style.transform = 'scale(0.95)';
                    setTimeout(() => window.location.reload(), 400);
                } else {
                    window.location.reload();
                }
            } else {
                showToast('Error: ' + data.message, 'error');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showToast('An error occurred while deleting', 'error');
        });
}

// ==========================================
// Bulk Select & Delete
// ==========================================

const selectedListIds = new Set();

function isSelectModeActive() {
    return document.body.classList.contains('reading-list-select-mode');
}

function toggleSelectMode() {
    const enabling = !isSelectModeActive();
    document.body.classList.toggle('reading-list-select-mode', enabling);

    const toggleBtn = document.getElementById('toggleSelectModeBtn');
    if (toggleBtn) toggleBtn.classList.toggle('active', enabling);

    // Always clear selection state when toggling, in either direction
    selectedListIds.clear();
    document.querySelectorAll('.list-select-cb').forEach(cb => { cb.checked = false; });
    document.querySelectorAll('.reading-list-card.selected').forEach(c => c.classList.remove('selected'));
    updateBulkBar();
}

function onListCheckboxChange(cb) {
    const id = cb.dataset.listId;
    const card = cb.closest('.reading-list-card');
    if (cb.checked) {
        selectedListIds.add(id);
        card?.classList.add('selected');
    } else {
        selectedListIds.delete(id);
        card?.classList.remove('selected');
    }
    updateBulkBar();
}

function updateBulkBar() {
    const bar = document.getElementById('readingListBulkActionBar');
    const count = document.getElementById('readingListBulkCount');
    if (!bar || !count) return;

    if (isSelectModeActive()) {
        bar.style.display = 'block';
        count.textContent = `${selectedListIds.size} list${selectedListIds.size === 1 ? '' : 's'} selected`;
    } else {
        bar.style.display = 'none';
    }
}

function selectAllVisibleLists() {
    document.querySelectorAll('.reading-list-card').forEach(card => {
        if (card.style.display === 'none') return;
        const cb = card.querySelector('.list-select-cb');
        if (cb && !cb.checked) {
            cb.checked = true;
            onListCheckboxChange(cb);
        }
    });
}

function clearListSelection() {
    document.querySelectorAll('.list-select-cb').forEach(cb => {
        if (cb.checked) {
            cb.checked = false;
            onListCheckboxChange(cb);
        }
    });
}

function bulkDeleteSelected() {
    if (selectedListIds.size === 0) {
        showToast('No lists selected', 'warning');
        return;
    }

    const count = selectedListIds.size;
    const toastContainer = getToastContainer();
    const toast = document.createElement('div');
    toast.className = 'toast align-items-center text-white bg-danger border-0 show';
    toast.setAttribute('role', 'alert');
    toast.innerHTML = `
        <div class="toast-body">
            <div class="mb-2">Delete ${count} selected reading list${count === 1 ? '' : 's'}?</div>
            <div class="d-flex gap-2">
                <button class="btn btn-warning btn-sm confirm-bulk-delete-btn">Delete</button>
                <button class="btn btn-light btn-sm cancel-bulk-delete-btn">Cancel</button>
            </div>
        </div>
    `;
    toast.querySelector('.cancel-bulk-delete-btn').addEventListener('click', function () {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    });
    toast.querySelector('.confirm-bulk-delete-btn').addEventListener('click', function () {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
        sendBulkDelete({ ids: Array.from(selectedListIds).map(id => parseInt(id, 10)) });
    });
    toastContainer.appendChild(toast);
}

function confirmDeleteAll() {
    const total = document.querySelectorAll('.reading-list-card').length;
    if (total === 0) {
        showToast('No reading lists to delete', 'warning');
        return;
    }

    const toastContainer = getToastContainer();
    const toast = document.createElement('div');
    toast.className = 'toast align-items-center text-white bg-danger border-0 show';
    toast.setAttribute('role', 'alert');
    toast.innerHTML = `
        <div class="toast-body">
            <div class="mb-2"><strong>Delete ALL ${total} reading list${total === 1 ? '' : 's'}?</strong></div>
            <div class="small mb-2">This cannot be undone.</div>
            <div class="d-flex gap-2">
                <button class="btn btn-warning btn-sm confirm-delete-all-btn">Delete All</button>
                <button class="btn btn-light btn-sm cancel-delete-all-btn">Cancel</button>
            </div>
        </div>
    `;
    toast.querySelector('.cancel-delete-all-btn').addEventListener('click', function () {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    });
    toast.querySelector('.confirm-delete-all-btn').addEventListener('click', function () {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
        sendBulkDelete({ all: true });
    });
    toastContainer.appendChild(toast);
}

function sendBulkDelete(payload) {
    fetch('/api/reading-lists/bulk-delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
        .then(response => response.json())
        .then(data => {
            const deletedCount = (data.deleted || []).length;
            const failedCount = (data.failed || []).length;
            if (data.success) {
                showToast(`Deleted ${deletedCount} reading list${deletedCount === 1 ? '' : 's'}`, 'success');
            } else if (deletedCount > 0) {
                showToast(`Deleted ${deletedCount}, failed ${failedCount}`, 'warning');
            } else {
                showToast(data.message || 'Bulk delete failed', 'error');
            }
            setTimeout(() => window.location.reload(), 600);
        })
        .catch(error => {
            console.error('Error:', error);
            showToast('An error occurred during bulk delete', 'error');
        });
}

// Intercept card clicks while in select mode so the inline onclick="window.location.href=..."
// is suppressed and the click toggles the checkbox instead.
document.addEventListener('DOMContentLoaded', function () {
    const grid = document.querySelector('.reading-list-grid');
    if (!grid) return;
    grid.addEventListener('click', function (e) {
        if (!isSelectModeActive()) return;
        // Ignore clicks on action buttons within the card; they have their own stopPropagation.
        if (e.target.closest('.card-actions')) return;
        // Clicks directly on the checkbox/input wrapper are handled by the input itself.
        if (e.target.closest('.card-select-checkbox')) return;

        const card = e.target.closest('.reading-list-card');
        if (!card) return;
        e.preventDefault();
        e.stopPropagation();
        const cb = card.querySelector('.list-select-cb');
        if (cb) {
            cb.checked = !cb.checked;
            onListCheckboxChange(cb);
        }
    }, true); // capture phase to beat the inline onclick
});

function cropCover(filePath) {
    fetch('/crop-cover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: filePath })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                window.location.reload();
            } else {
                showToast('Crop failed: ' + (data.error || data.message || 'unknown error'), 'error');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showToast('An error occurred while cropping', 'error');
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
        .replace('{volume_year}', year || '')
        .replace('{year}', year || '')
        .replace('{start_year}', volume || year || '');

    // Clean up any remaining empty placeholders and extra spaces
    searchTerm = searchTerm.replace(/\{[^}]+\}/g, '').replace(/\s+/g, ' ').trim();

    // Remove empty parentheses that might result from missing values
    searchTerm = searchTerm.replace(/\(\s*\)/g, '').trim();

    return searchTerm;
}

function openMapModal(entryId, series, number, volume, year) {
    if (reorderMode) return;
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
                <div class="d-flex flex-column w-100">
                    <div class="d-flex w-100 justify-content-between">
                        <h6 class="mb-1 text-truncate">${file.name}</h6>
                        <small class="text-muted">${file.path.split('/').slice(-2, -1)[0]}</small>
                    </div>
                    <small class="text-info-emphasis text-break font-monospace">${file.path}</small>
                </div>
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
// Create New List
// ==========================================
function createNewList() {
    const nameInput = document.getElementById('newListName');
    const name = nameInput ? nameInput.value.trim() : '';
    if (!name) {
        alert('Please enter a name');
        return;
    }

    fetch('/api/reading-lists/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            window.location.href = '/reading-lists/' + data.list_id;
        } else {
            alert('Error: ' + data.message);
        }
    })
    .catch(err => {
        console.error('Error creating list:', err);
        alert('An error occurred');
    });
}

// ==========================================
// Remove Entry from List
// ==========================================
function removeEntry(entryId) {
    if (!confirm('Remove this issue from the reading list?')) return;

    fetch(`/api/reading-lists/${LIST_ID}/entry/${entryId}`, {
        method: 'DELETE'
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            // Remove the card from DOM
            const card = document.querySelector(`.book-card[data-entry-id="${entryId}"]`);
            if (card) card.remove();
            showToast('Entry removed', 'success');
        } else {
            showToast('Failed to remove entry: ' + data.message, 'error');
        }
    })
    .catch(err => {
        console.error('Error removing entry:', err);
        showToast('An error occurred', 'error');
    });
}

// ==========================================
// Add Issue to List (from detail view)
// ==========================================
let addIssueModal = null;
let selectedAddFilePath = null;

function openAddIssueModal() {
    selectedAddFilePath = null;
    document.getElementById('addIssueSearchInput').value = '';
    document.getElementById('addIssueResults').innerHTML = '';
    document.getElementById('confirmAddIssueBtn').disabled = true;

    if (!addIssueModal) {
        addIssueModal = new bootstrap.Modal(document.getElementById('addIssueModal'));
    }
    addIssueModal.show();

    // Focus the search input
    setTimeout(() => document.getElementById('addIssueSearchInput').focus(), 300);
}

function searchFilesForAdd() {
    const query = document.getElementById('addIssueSearchInput').value;
    if (!query) return;

    const resultsDiv = document.getElementById('addIssueResults');
    resultsDiv.innerHTML = '<div class="text-center p-3"><div class="spinner-border text-primary" role="status"></div></div>';

    fetch(`/api/reading-lists/search-file?q=${encodeURIComponent(query)}`)
        .then(r => r.json())
        .then(results => {
            resultsDiv.innerHTML = '';
            if (results.length === 0) {
                resultsDiv.innerHTML = '<div class="p-3 text-center text-muted">No files found</div>';
                return;
            }
            results.forEach(file => {
                const item = document.createElement('div');
                item.className = 'list-group-item list-group-item-action search-result-item';
                item.innerHTML = `
                    <div class="d-flex flex-column w-100">
                        <div class="d-flex w-100 justify-content-between">
                            <h6 class="mb-1 text-truncate">${file.name}</h6>
                            <small class="text-muted">${file.path.split('/').slice(-2, -1)[0]}</small>
                        </div>
                        <small class="text-info-emphasis text-break font-monospace">${file.path}</small>
                    </div>
                `;
                item.onclick = () => {
                    selectedAddFilePath = file.path;
                    resultsDiv.querySelectorAll('.search-result-item').forEach(el => el.classList.remove('active'));
                    item.classList.add('active');
                    document.getElementById('confirmAddIssueBtn').disabled = false;
                };
                resultsDiv.appendChild(item);
            });
        })
        .catch(err => {
            console.error('Error searching:', err);
            resultsDiv.innerHTML = '<div class="text-danger p-3">Error searching files</div>';
        });
}

// Enter key in add issue search
document.addEventListener('DOMContentLoaded', () => {
    const addInput = document.getElementById('addIssueSearchInput');
    if (addInput) {
        addInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') searchFilesForAdd();
        });
    }
});

function confirmAddIssue() {
    if (!selectedAddFilePath) return;

    fetch(`/api/reading-lists/${LIST_ID}/add-entry`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: selectedAddFilePath })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            location.reload();
        } else {
            alert('Error: ' + data.message);
        }
    })
    .catch(err => {
        console.error('Error adding issue:', err);
        alert('An error occurred');
    });
}

// ==========================================
// Drag & Drop Reordering (SortableJS)
// ==========================================
let sortableInstance = null;
let reorderMode = false;

function toggleReorderMode() {
    reorderMode = !reorderMode;
    const grid = document.querySelector('.reading-list-grid');
    const btn = document.getElementById('reorderToggleBtn');

    if (reorderMode) {
        grid.classList.add('reorder-mode');
        btn.classList.remove('btn-outline-secondary');
        btn.classList.add('btn-warning');
        btn.innerHTML = '<i class="bi bi-check-lg me-1"></i>Done';
        if (sortableInstance) sortableInstance.option('disabled', false);
    } else {
        grid.classList.remove('reorder-mode');
        btn.classList.remove('btn-warning');
        btn.classList.add('btn-outline-secondary');
        btn.innerHTML = '<i class="bi bi-arrows-move me-1"></i>Reorder';
        if (sortableInstance) sortableInstance.option('disabled', true);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const grid = document.querySelector('.reading-list-grid');
    if (grid && typeof Sortable !== 'undefined') {
        sortableInstance = Sortable.create(grid, {
            disabled: true,
            animation: 150,
            ghostClass: 'sortable-ghost',
            chosenClass: 'sortable-chosen',
            filter: '.dropdown-menu, .dropdown-toggle, .btn',
            preventOnFilter: false,
            onEnd: function() {
                const cards = grid.querySelectorAll('.book-card');
                const entryIds = Array.from(cards).map(c => parseInt(c.dataset.entryId));
                fetch(`/api/reading-lists/${LIST_ID}/reorder`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ entry_ids: entryIds })
                })
                .then(r => r.json())
                .then(data => {
                    if (!data.success) {
                        showToast('Failed to save order', 'error');
                    }
                })
                .catch(err => {
                    console.error('Error reordering:', err);
                    showToast('Failed to save order', 'error');
                });
            }
        });
    }
});

// ==========================================
// Comic Reader Bridge
// ==========================================
// The reader itself lives in static/js/reader.js (loaded before this file).
// This page used to carry a near-verbatim fork of it, which meant every reader
// fix had to be made twice and the two drifted apart. Instead we publish the
// contract reader.js documents at the top of that file:
//   window._readerAllItems      -- ordered sibling list for "next issue"
//   window._readerReadIssuesSet -- paths already read
//   window._readerOnMarkedRead  -- optional callback when one is marked read

// Reading list navigation
let readingListEntries = [];      // All matched entries [{path, name, thumbnail, series, issue}, ...]

// Read status tracking
let readIssuesSet = new Set();

/**
 * Open a comic from the reading list.
 * Keeps the reorder-mode guard, then delegates to the shared reader.
 * @param {string} filePath
 */
function openReadingListComic(filePath) {
    if (reorderMode) return;
    if (typeof window.openComicReader !== 'function') {
        showToast('Reader failed to load. Try refreshing the page.', 'error');
        return;
    }
    // Publish siblings in reading-list order so "next issue" follows the list
    // rather than folder order. reader.js finds the current index by path.
    window._readerAllItems = readingListEntries.map(e => ({
        type: 'file',
        name: e.name,
        path: e.path,
        thumbnailUrl: e.thumbnail
    }));
    window.openComicReader(filePath);
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
            // Republish: this rebinds the variable, so any reference handed to
            // reader.js earlier would be stale.
            window._readerReadIssuesSet = readIssuesSet;
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

    // Let the shared reader update this page's read badges when it marks an
    // issue read (part of the reader.js host contract).
    window._readerOnMarkedRead = function (path) {
        readIssuesSet.add(path);
        updateReadIcon(path, true);
    };

    // All reader controls (close, bookmark, resume, next-issue, Escape) are
    // wired by reader.js -- do not re-bind them here or handlers fire twice.
});

// ==========================================
// GitHub Tree Browser
// ==========================================

let _githubTreeData = null;
let _githubTreeLoaded = false;
let _collapsedFolders = new Set();
let _githubTreeParsed = null; // { folders, folderMap, subFolderIndex }

function loadGithubTree() {
    if (_githubTreeLoaded) return;

    const loading = document.getElementById('githubTreeLoading');
    const content = document.getElementById('githubTreeContent');
    const error = document.getElementById('githubTreeError');

    if (!loading) return; // Not on the reading lists page

    loading.classList.remove('d-none');
    content.classList.add('d-none');
    error.classList.add('d-none');

    fetch('/api/reading-lists/github-tree')
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                _githubTreeData = data.tree;
                _githubTreeLoaded = true;
                renderTreeView(data.tree);
                loading.classList.add('d-none');
                content.classList.remove('d-none');
            } else {
                throw new Error(data.message || 'Failed to load');
            }
        })
        .catch(err => {
            loading.classList.add('d-none');
            error.classList.remove('d-none');
            error.textContent = 'Failed to load repository: ' + err.message;
        });
}

function _createFolderEl(folder) {
    const parsed = _githubTreeParsed;
    const div = document.createElement('div');
    div.className = 'tree-folder';
    div.dataset.path = folder.path;

    const header = document.createElement('div');
    header.className = 'tree-item tree-folder-header d-flex align-items-center px-3 py-2';
    header.style.cursor = 'pointer';
    const depth = folder.path.split('/').length - 1;
    header.style.paddingLeft = (depth * 20 + 12) + 'px';
    header.onclick = () => toggleFolder(folder.path);

    header.innerHTML = `
        <i class="bi bi-chevron-down me-2 folder-chevron" style="transition: transform 0.2s; transform: rotate(-90deg);"></i>
        <i class="bi bi-folder-fill text-warning me-2"></i>
        <span>${folder.path.split('/').pop()}</span>
    `;
    div.appendChild(header);

    // Child container — starts hidden and unloaded
    const children = document.createElement('div');
    children.className = 'tree-children';
    children.dataset.folderPath = folder.path;
    children.dataset.loaded = 'false';
    children.style.display = 'none';

    div.appendChild(children);

    // Track as collapsed
    _collapsedFolders.add(folder.path);

    return div;
}

function _createFileEl(file, depth) {
    const div = document.createElement('div');
    div.className = 'tree-item tree-file d-flex align-items-center px-3 py-2';
    div.dataset.path = file.path;
    div.style.paddingLeft = (depth * 20 + 12) + 'px';

    const filename = file.path.split('/').pop().replace(/\.cbl$/i, '');
    div.innerHTML = `
        <div class="form-check mb-0">
            <input class="form-check-input tree-file-check" type="checkbox" value="${file.path}" id="chk-${file.path.replace(/[^a-zA-Z0-9]/g, '_')}" onchange="updateSelectedCount()">
            <label class="form-check-label" for="chk-${file.path.replace(/[^a-zA-Z0-9]/g, '_')}">
                <i class="bi bi-file-earmark-text me-1 text-primary"></i>${filename}
            </label>
        </div>
    `;
    return div;
}

function _loadFolderChildren(path) {
    const children = document.querySelector(`[data-folder-path="${path}"]`);
    if (!children || children.dataset.loaded !== 'false') return;

    const parsed = _githubTreeParsed;

    // Append sub-folders
    const subFolders = parsed.subFolderIndex[path] || [];
    subFolders.forEach(sf => children.appendChild(_createFolderEl(sf)));

    // Append files
    const depth = path.split('/').length;
    (parsed.folderMap[path] || []).forEach(f => {
        children.appendChild(_createFileEl(f, depth));
    });

    children.dataset.loaded = 'true';
}

function renderTreeView(tree) {
    const container = document.getElementById('githubTreeContent');
    if (!container) return;

    container.innerHTML = '';
    _collapsedFolders.clear();

    // Build data structures
    const folders = tree.filter(i => i.type === 'tree');
    const files = tree.filter(i => i.type === 'blob');

    const folderMap = {};
    folders.forEach(f => { folderMap[f.path] = []; });
    folderMap[''] = [];

    files.forEach(f => {
        const parts = f.path.split('/');
        const parent = parts.length > 1 ? parts.slice(0, -1).join('/') : '';
        if (folderMap[parent] === undefined) folderMap[parent] = [];
        folderMap[parent].push(f);
    });

    // Index sub-folders by parent for fast lookup
    const subFolderIndex = {};
    folders.forEach(f => {
        const parts = f.path.split('/');
        const parent = parts.length > 1 ? parts.slice(0, -1).join('/') : '';
        if (!subFolderIndex[parent]) subFolderIndex[parent] = [];
        subFolderIndex[parent].push(f);
    });

    // Store parsed data for lazy loading
    _githubTreeParsed = { folders, folderMap, subFolderIndex };

    // Render only root level
    const rootFolders = subFolderIndex[''] || [];
    const rootFiles = folderMap[''] || [];

    rootFolders.forEach(f => container.appendChild(_createFolderEl(f)));
    rootFiles.forEach(f => container.appendChild(_createFileEl(f, 0)));
}

function toggleFolder(path) {
    const children = document.querySelector(`[data-folder-path="${path}"]`);
    const header = children?.previousElementSibling;
    const chevron = header?.querySelector('.folder-chevron');

    if (!children) return;

    if (_collapsedFolders.has(path)) {
        // Expanding — lazy-load children on first open
        _loadFolderChildren(path);
        _collapsedFolders.delete(path);
        children.style.display = '';
        if (chevron) chevron.style.transform = '';
    } else {
        _collapsedFolders.add(path);
        children.style.display = 'none';
        if (chevron) chevron.style.transform = 'rotate(-90deg)';
    }
}

function _ensureFolderLoaded(path) {
    // Ensure this folder and all its ancestors have their children loaded
    const parts = path.split('/');
    for (let i = 1; i <= parts.length; i++) {
        const ancestorPath = parts.slice(0, i).join('/');
        _loadFolderChildren(ancestorPath);
    }
}

function filterTree(query) {
    const q = query.toLowerCase().trim();

    if (!q) {
        // Reset: show all loaded folders/files, restore collapse state
        document.querySelectorAll('#githubTreeContent .tree-file').forEach(i => i.style.display = '');
        document.querySelectorAll('#githubTreeContent .tree-folder').forEach(f => {
            f.style.display = '';
            // Restore collapsed state for children containers
            const childDiv = f.querySelector(':scope > .tree-children');
            if (childDiv && _collapsedFolders.has(f.dataset.path)) {
                childDiv.style.display = 'none';
            }
        });
        return;
    }

    // Force-load folders that contain matching files
    if (_githubTreeData) {
        _githubTreeData.forEach(item => {
            if (item.type === 'blob' && item.path.toLowerCase().includes(q)) {
                const parts = item.path.split('/');
                if (parts.length > 1) {
                    const parentPath = parts.slice(0, -1).join('/');
                    _ensureFolderLoaded(parentPath);
                }
            }
        });
    }

    const items = document.querySelectorAll('#githubTreeContent .tree-file');
    const folders = document.querySelectorAll('#githubTreeContent .tree-folder');

    // Hide all folders first, show matching files
    folders.forEach(f => f.style.display = 'none');
    items.forEach(item => {
        const path = item.dataset.path.toLowerCase();
        const match = path.includes(q);
        item.style.display = match ? '' : 'none';
        // Show parent folders if match
        if (match) {
            let current = item.parentElement;
            while (current && current.id !== 'githubTreeContent') {
                if (current.classList.contains('tree-folder')) {
                    current.style.display = '';
                    // Also show the children container
                    const childDiv = current.querySelector(':scope > .tree-children');
                    if (childDiv) childDiv.style.display = '';
                }
                current = current.parentElement;
            }
        }
    });
}

function getSelectedFiles() {
    const checks = document.querySelectorAll('.tree-file-check:checked');
    return Array.from(checks).map(c => c.value);
}

function updateSelectedCount() {
    const count = document.querySelectorAll('.tree-file-check:checked').length;
    const el = document.getElementById('selectedCount');
    if (el) el.textContent = `${count} file${count !== 1 ? 's' : ''} selected`;
}

function selectAllVisible() {
    document.querySelectorAll('.tree-file').forEach(item => {
        if (item.style.display !== 'none') {
            const cb = item.querySelector('.tree-file-check');
            if (cb) cb.checked = true;
        }
    });
    updateSelectedCount();
}

function deselectAll() {
    document.querySelectorAll('.tree-file-check').forEach(cb => cb.checked = false);
    updateSelectedCount();
}

function importSelectedFiles() {
    const files = getSelectedFiles();
    if (files.length === 0) {
        showToast('No files selected', 'error');
        return;
    }

    const btn = document.getElementById('importBatchBtn');
    btn.disabled = true;
    btn.querySelector('.btn-text').classList.add('d-none');
    btn.querySelector('.btn-loading').classList.remove('d-none');

    fetch('/api/reading-lists/import-batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ files: files })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                // Close modal
                const modal = bootstrap.Modal.getInstance(document.getElementById('importGithubModal'));
                if (modal) modal.hide();

                showToast(`Importing ${data.tasks.length} list(s) -- track progress in the navbar`, 'info', 5000);

                // Poll each task
                data.tasks.forEach(task => {
                    pollImportStatus(task.task_id, task.filename);
                });
            } else {
                showToast('Error: ' + data.message, 'error');
            }
        })
        .catch(err => {
            showToast('Import failed: ' + err.message, 'error');
        })
        .finally(() => {
            btn.disabled = false;
            btn.querySelector('.btn-text').classList.remove('d-none');
            btn.querySelector('.btn-loading').classList.add('d-none');
        });
}

// ==========================================
// Metron Reading List Browser
// ==========================================

let _metronListsData = null;
let _metronSearchTimeout = null;

function loadMetronLists() {
    if (_metronListsData) {
        renderMetronLists(_metronListsData);
        return;
    }

    const loading = document.getElementById('metronListLoading');
    const content = document.getElementById('metronListContent');
    const error = document.getElementById('metronListError');
    const empty = document.getElementById('metronListEmpty');
    const notConfigured = document.getElementById('metronNotConfigured');

    if (!loading) return;

    loading.classList.remove('d-none');
    content.classList.add('d-none');
    error.classList.add('d-none');
    empty.classList.add('d-none');
    notConfigured.classList.add('d-none');

    fetch('/api/reading-lists/metron-browse')
        .then(r => r.json())
        .then(data => {
            loading.classList.add('d-none');
            if (data.success) {
                _metronListsData = data.lists;
                if (data.lists.length === 0) {
                    empty.classList.remove('d-none');
                } else {
                    renderMetronLists(data.lists);
                    content.classList.remove('d-none');
                }
            } else {
                if (data.message && data.message.includes('not configured')) {
                    notConfigured.classList.remove('d-none');
                } else {
                    error.classList.remove('d-none');
                    error.textContent = data.message || 'Failed to load';
                }
            }
        })
        .catch(err => {
            loading.classList.add('d-none');
            error.classList.remove('d-none');
            error.textContent = 'Failed to load reading lists: ' + err.message;
        });
}

function searchMetronLists() {
    clearTimeout(_metronSearchTimeout);
    _metronSearchTimeout = setTimeout(() => {
        const query = document.getElementById('metronSearchInput').value.trim();

        const loading = document.getElementById('metronListLoading');
        const content = document.getElementById('metronListContent');
        const error = document.getElementById('metronListError');
        const empty = document.getElementById('metronListEmpty');
        const notConfigured = document.getElementById('metronNotConfigured');

        loading.classList.remove('d-none');
        content.classList.add('d-none');
        error.classList.add('d-none');
        empty.classList.add('d-none');
        notConfigured.classList.add('d-none');

        const url = query
            ? `/api/reading-lists/metron-browse?search=${encodeURIComponent(query)}`
            : '/api/reading-lists/metron-browse';

        fetch(url)
            .then(r => r.json())
            .then(data => {
                loading.classList.add('d-none');
                if (data.success) {
                    if (!query) _metronListsData = data.lists;
                    if (data.lists.length === 0) {
                        empty.classList.remove('d-none');
                    } else {
                        renderMetronLists(data.lists);
                        content.classList.remove('d-none');
                    }
                } else {
                    if (data.message && data.message.includes('not configured')) {
                        notConfigured.classList.remove('d-none');
                    } else {
                        error.classList.remove('d-none');
                        error.textContent = data.message || 'Failed to search';
                    }
                }
            })
            .catch(err => {
                loading.classList.add('d-none');
                error.classList.remove('d-none');
                error.textContent = 'Search failed: ' + err.message;
            });
    }, 300);
}

function renderMetronLists(lists) {
    const container = document.getElementById('metronListContent');
    if (!container) return;

    container.innerHTML = '';

    lists.forEach(list => {
        const div = document.createElement('div');
        div.className = 'metron-list-item d-flex align-items-center px-3 py-2';
        div.style.borderBottom = '1px solid #dee2e6';

        const name = list.name || 'Unnamed List';
        const user = (list.user && typeof list.user === 'object') ? (list.user.username || '') : (list.user || '');
        const rating = list.average_rating;
        const listId = list.id;

        let ratingHtml = '';
        if (rating !== null && rating !== undefined) {
            const stars = Math.round(rating);
            ratingHtml = `<span class="text-warning ms-2" title="Rating: ${rating}">`;
            for (let i = 0; i < 5; i++) {
                ratingHtml += i < stars ? '<i class="bi bi-star-fill"></i>' : '<i class="bi bi-star"></i>';
            }
            ratingHtml += '</span>';
        }

        div.innerHTML = `
            <div class="form-check mb-0 flex-grow-1">
                <input class="form-check-input metron-list-check" type="checkbox" value="${listId}" id="metron-chk-${listId}" onchange="updateMetronSelectedCount()">
                <label class="form-check-label w-100" for="metron-chk-${listId}">
                    <strong>${name}</strong>
                    ${user ? `<span class="text-muted ms-2">by ${user}</span>` : ''}
                    ${ratingHtml}
                </label>
            </div>
        `;
        container.appendChild(div);
    });
}

function getSelectedMetronLists() {
    const checks = document.querySelectorAll('.metron-list-check:checked');
    return Array.from(checks).map(c => parseInt(c.value));
}

function updateMetronSelectedCount() {
    const count = document.querySelectorAll('.metron-list-check:checked').length;
    const el = document.getElementById('metronSelectedCount');
    if (el) el.textContent = `${count} list${count !== 1 ? 's' : ''} selected`;
}

function selectAllMetronVisible() {
    document.querySelectorAll('.metron-list-item').forEach(item => {
        if (item.style.display !== 'none') {
            const cb = item.querySelector('.metron-list-check');
            if (cb) cb.checked = true;
        }
    });
    updateMetronSelectedCount();
}

function deselectAllMetron() {
    document.querySelectorAll('.metron-list-check').forEach(cb => cb.checked = false);
    updateMetronSelectedCount();
}

function importSelectedMetronLists() {
    const listIds = getSelectedMetronLists();
    if (listIds.length === 0) {
        showToast('No lists selected', 'error');
        return;
    }

    const btn = document.getElementById('importMetronBtn');
    btn.disabled = true;
    btn.querySelector('.btn-text').classList.add('d-none');
    btn.querySelector('.btn-loading').classList.remove('d-none');

    fetch('/api/reading-lists/metron-import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ list_ids: listIds })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                const modal = bootstrap.Modal.getInstance(document.getElementById('importMetronModal'));
                if (modal) modal.hide();

                showToast(`Importing ${data.tasks.length} list(s) -- track progress in the navbar`, 'info', 5000);

                data.tasks.forEach(task => {
                    pollImportStatus(task.task_id, `Metron list ${task.list_id}`);
                });
            } else {
                showToast('Error: ' + data.message, 'error');
            }
        })
        .catch(err => {
            showToast('Import failed: ' + err.message, 'error');
        })
        .finally(() => {
            btn.disabled = false;
            btn.querySelector('.btn-text').classList.remove('d-none');
            btn.querySelector('.btn-loading').classList.add('d-none');
        });
}

// ==========================================
// Metron Story Arc Browser
// ==========================================

let _metronArcsPage = 0;       // current page loaded (0 = none yet)
let _metronArcsHasNext = false;
let _metronArcsSearchQuery = '';
let _metronArcsLoaded = false;  // true after first successful load
let _metronArcsTotalCount = 0;
let _metronArcsDisplayed = 0;
let _metronArcSearchTimeout = null;
let _metronArcsFetching = false;
let _metronArcsObserver = null;

function activateMetronTab(tab) {
    const rlTab = document.getElementById('metronRLTab');
    const arcTab = document.getElementById('metronArcTab');
    const importMetronBtn = document.getElementById('importMetronBtn');
    const importMetronArcBtn = document.getElementById('importMetronArcBtn');

    if (tab === 'story-arcs' && arcTab) {
        const bsTab = new bootstrap.Tab(arcTab);
        bsTab.show();
        if (importMetronBtn) importMetronBtn.classList.add('d-none');
        if (importMetronArcBtn) importMetronArcBtn.classList.remove('d-none');
    } else if (rlTab) {
        const bsTab = new bootstrap.Tab(rlTab);
        bsTab.show();
        if (importMetronBtn) importMetronBtn.classList.remove('d-none');
        if (importMetronArcBtn) importMetronArcBtn.classList.add('d-none');
    }
}

function loadMetronArcs() {
    if (_metronArcsLoaded && !_metronArcsSearchQuery) {
        return; // already showing browse results
    }
    _metronArcsSearchQuery = '';
    _metronArcsPage = 0;
    _metronArcsDisplayed = 0;
    fetchMetronArcsPage(1);
}

function searchMetronArcs() {
    clearTimeout(_metronArcSearchTimeout);
    _metronArcSearchTimeout = setTimeout(() => {
        const query = document.getElementById('metronArcSearchInput').value.trim();
        _metronArcsSearchQuery = query;
        _metronArcsPage = 0;
        _metronArcsDisplayed = 0;
        if (!query) {
            _metronArcsLoaded = false; // allow browse reload
        }
        fetchMetronArcsPage(1);
    }, 300);
}

function fetchMetronArcsPage(page) {
    if (_metronArcsFetching) return;
    _metronArcsFetching = true;

    const loading = document.getElementById('metronArcListLoading');
    const content = document.getElementById('metronArcListContent');
    const error = document.getElementById('metronArcListError');
    const empty = document.getElementById('metronArcListEmpty');
    const notConfigured = document.getElementById('metronArcNotConfigured');
    const sentinel = document.getElementById('metronArcScrollSentinel');
    const sentinelSpinner = document.getElementById('metronArcSentinelSpinner');
    const pageInfo = document.getElementById('metronArcPageInfo');

    if (!loading) { _metronArcsFetching = false; return; }

    const append = page > 1;

    if (!append) {
        loading.classList.remove('d-none');
        content.classList.add('d-none');
        error.classList.add('d-none');
        empty.classList.add('d-none');
        notConfigured.classList.add('d-none');
        if (sentinel) sentinel.classList.add('d-none');
        if (pageInfo) pageInfo.classList.add('d-none');
    } else {
        // Show the spinner inside the sentinel while fetching
        if (sentinelSpinner) sentinelSpinner.classList.remove('d-none');
    }

    let url = `/api/reading-lists/metron-browse-arcs?page=${page}`;
    if (_metronArcsSearchQuery) {
        url += `&search=${encodeURIComponent(_metronArcsSearchQuery)}`;
    }

    fetch(url)
        .then(r => r.json())
        .then(data => {
            loading.classList.add('d-none');
            _metronArcsFetching = false;
            if (data.success) {
                _metronArcsPage = data.page;
                _metronArcsHasNext = data.has_next;
                _metronArcsTotalCount = data.count;

                if (data.results.length === 0 && !append) {
                    empty.classList.remove('d-none');
                    if (sentinel) sentinel.classList.add('d-none');
                } else {
                    renderMetronArcs(data.results, append);
                    _metronArcsDisplayed += data.results.length;
                    content.classList.remove('d-none');
                    if (!_metronArcsSearchQuery) _metronArcsLoaded = true;

                    // Keep sentinel visible inside scroll container when more pages exist
                    // Hide spinner text until next fetch triggers
                    if (sentinelSpinner) sentinelSpinner.classList.add('d-none');
                    if (_metronArcsHasNext) {
                        if (sentinel) sentinel.classList.remove('d-none');
                        _setupArcScrollObserver();
                    } else {
                        if (sentinel) sentinel.classList.add('d-none');
                        if (_metronArcsObserver) {
                            _metronArcsObserver.disconnect();
                            _metronArcsObserver = null;
                        }
                    }

                    if (pageInfo) {
                        pageInfo.textContent = `Showing ${_metronArcsDisplayed} of ${_metronArcsTotalCount} arcs`;
                        pageInfo.classList.remove('d-none');
                    }
                }
            } else {
                if (data.message && data.message.includes('not configured')) {
                    notConfigured.classList.remove('d-none');
                } else {
                    error.classList.remove('d-none');
                    error.textContent = data.message || 'Failed to load';
                }
                if (sentinel) sentinel.classList.add('d-none');
            }
        })
        .catch(err => {
            loading.classList.add('d-none');
            _metronArcsFetching = false;
            error.classList.remove('d-none');
            error.textContent = 'Failed to load story arcs: ' + err.message;
            if (sentinel) sentinel.classList.add('d-none');
        });
}

function _setupArcScrollObserver() {
    // Clean up previous observer
    if (_metronArcsObserver) {
        _metronArcsObserver.disconnect();
        _metronArcsObserver = null;
    }
    if (!_metronArcsHasNext) return;

    const sentinel = document.getElementById('metronArcScrollSentinel');
    const scrollContainer = document.getElementById('metronArcListContainer');
    if (!sentinel || !scrollContainer) return;

    _metronArcsObserver = new IntersectionObserver((entries) => {
        if (entries[0].isIntersecting && _metronArcsHasNext && !_metronArcsFetching) {
            fetchMetronArcsPage(_metronArcsPage + 1);
        }
    }, {
        root: scrollContainer,
        rootMargin: '200px',
    });
    _metronArcsObserver.observe(sentinel);
}

function renderMetronArcs(arcs, append) {
    const container = document.getElementById('metronArcListContent');
    if (!container) return;

    if (!append) container.innerHTML = '';

    arcs.forEach(arc => {
        const div = document.createElement('div');
        div.className = 'metron-arc-item d-flex align-items-center px-3 py-2';
        div.style.borderBottom = '1px solid #dee2e6';

        const name = arc.name || 'Unnamed Arc';
        const desc = arc.desc || arc.description || '';
        const arcId = arc.id;

        let descHtml = '';
        if (desc) {
            const snippet = desc.length > 80 ? desc.substring(0, 80) + '...' : desc;
            descHtml = `<span class="text-muted ms-2 small">${snippet}</span>`;
        }

        div.innerHTML = `
            <div class="form-check mb-0 flex-grow-1">
                <input class="form-check-input metron-arc-check" type="checkbox" value="${arcId}" id="metron-arc-chk-${arcId}" onchange="updateMetronArcSelectedCount()">
                <label class="form-check-label w-100" for="metron-arc-chk-${arcId}">
                    <strong>${name}</strong>
                    ${descHtml}
                </label>
            </div>
        `;
        container.appendChild(div);
    });
}

function getSelectedMetronArcs() {
    const checks = document.querySelectorAll('.metron-arc-check:checked');
    return Array.from(checks).map(c => parseInt(c.value));
}

function updateMetronArcSelectedCount() {
    const count = document.querySelectorAll('.metron-arc-check:checked').length;
    const el = document.getElementById('metronArcSelectedCount');
    if (el) el.textContent = `${count} arc${count !== 1 ? 's' : ''} selected`;
}

function selectAllMetronArcsVisible() {
    document.querySelectorAll('.metron-arc-item').forEach(item => {
        if (item.style.display !== 'none') {
            const cb = item.querySelector('.metron-arc-check');
            if (cb) cb.checked = true;
        }
    });
    updateMetronArcSelectedCount();
}

function deselectAllMetronArcs() {
    document.querySelectorAll('.metron-arc-check').forEach(cb => cb.checked = false);
    updateMetronArcSelectedCount();
}

function importSelectedMetronArcs() {
    const arcIds = getSelectedMetronArcs();
    if (arcIds.length === 0) {
        showToast('No arcs selected', 'error');
        return;
    }

    const btn = document.getElementById('importMetronArcBtn');
    btn.disabled = true;
    btn.querySelector('.btn-text').classList.add('d-none');
    btn.querySelector('.btn-loading').classList.remove('d-none');

    fetch('/api/reading-lists/metron-import-arcs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ arc_ids: arcIds })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                const modal = bootstrap.Modal.getInstance(document.getElementById('importMetronModal'));
                if (modal) modal.hide();

                showToast(`Importing ${data.tasks.length} arc(s) -- track progress in the navbar`, 'info', 5000);

                data.tasks.forEach(task => {
                    pollImportStatus(task.task_id, `Metron arc ${task.arc_id}`);
                });
            } else {
                showToast('Error: ' + data.message, 'error');
            }
        })
        .catch(err => {
            showToast('Import failed: ' + err.message, 'error');
        })
        .finally(() => {
            btn.disabled = false;
            btn.querySelector('.btn-text').classList.remove('d-none');
            btn.querySelector('.btn-loading').classList.add('d-none');
        });
}

// ==========================================
// ComicVine Story Arc Browser
// ==========================================

let _cvArcsLoaded = false;
let _cvArcsSearchQuery = '';
let _cvArcSearchTimeout = null;

function loadCVArcs() {
    if (_cvArcsLoaded && !_cvArcsSearchQuery) {
        return;
    }
    _cvArcsSearchQuery = '';
    const searchInput = document.getElementById('cvArcSearchInput');
    if (searchInput) searchInput.value = '';
    fetchCVArcs();
}

function searchCVArcs() {
    clearTimeout(_cvArcSearchTimeout);
    _cvArcSearchTimeout = setTimeout(() => {
        const query = document.getElementById('cvArcSearchInput').value.trim();
        _cvArcsSearchQuery = query;
        if (!query) {
            _cvArcsLoaded = false;
        }
        fetchCVArcs();
    }, 300);
}

function fetchCVArcs() {
    const loading = document.getElementById('cvArcListLoading');
    const content = document.getElementById('cvArcListContent');
    const error = document.getElementById('cvArcListError');
    const empty = document.getElementById('cvArcListEmpty');
    const notConfigured = document.getElementById('cvArcNotConfigured');

    if (!loading) return;

    loading.classList.remove('d-none');
    content.classList.add('d-none');
    error.classList.add('d-none');
    empty.classList.add('d-none');
    notConfigured.classList.add('d-none');

    let url = '/api/reading-lists/cv-browse-arcs';
    if (_cvArcsSearchQuery) {
        url += `?search=${encodeURIComponent(_cvArcsSearchQuery)}`;
    }

    fetch(url)
        .then(r => r.json())
        .then(data => {
            loading.classList.add('d-none');
            if (data.success) {
                if (!data.arcs || data.arcs.length === 0) {
                    empty.classList.remove('d-none');
                } else {
                    renderCVArcs(data.arcs);
                    content.classList.remove('d-none');
                    if (!_cvArcsSearchQuery) _cvArcsLoaded = true;
                }
            } else {
                if (data.message && data.message.includes('not configured')) {
                    notConfigured.classList.remove('d-none');
                } else {
                    error.classList.remove('d-none');
                    error.textContent = data.message || 'Failed to load';
                }
            }
        })
        .catch(err => {
            loading.classList.add('d-none');
            error.classList.remove('d-none');
            error.textContent = 'Failed to load story arcs: ' + err.message;
        });
}

function renderCVArcs(arcs) {
    const container = document.getElementById('cvArcListContent');
    if (!container) return;

    container.innerHTML = '';

    arcs.forEach(arc => {
        const div = document.createElement('div');
        div.className = 'cv-arc-item d-flex align-items-center px-3 py-2';
        div.style.borderBottom = '1px solid #dee2e6';

        const name = arc.name || 'Unnamed Arc';
        const desc = arc.description || '';
        const arcId = arc.id;
        const issueCount = arc.issue_count;

        let metaHtml = '';
        if (issueCount) {
            metaHtml += `<span class="badge bg-secondary ms-2">${issueCount} issues</span>`;
        }

        let descHtml = '';
        if (desc) {
            const snippet = desc.length > 80 ? desc.substring(0, 80) + '...' : desc;
            descHtml = `<span class="text-muted ms-2 small">${snippet}</span>`;
        }

        div.innerHTML = `
            <div class="form-check mb-0 flex-grow-1">
                <input class="form-check-input cv-arc-check" type="checkbox" value="${arcId}" id="cv-arc-chk-${arcId}" onchange="updateCVArcSelectedCount()">
                <label class="form-check-label w-100" for="cv-arc-chk-${arcId}">
                    <strong>${name}</strong>
                    ${metaHtml}
                    ${descHtml}
                </label>
            </div>
        `;
        container.appendChild(div);
    });
}

function getSelectedCVArcs() {
    const checks = document.querySelectorAll('.cv-arc-check:checked');
    return Array.from(checks).map(c => parseInt(c.value));
}

function updateCVArcSelectedCount() {
    const count = document.querySelectorAll('.cv-arc-check:checked').length;
    const el = document.getElementById('cvArcSelectedCount');
    if (el) el.textContent = `${count} arc${count !== 1 ? 's' : ''} selected`;
}

function selectAllCVArcsVisible() {
    document.querySelectorAll('.cv-arc-item').forEach(item => {
        if (item.style.display !== 'none') {
            const cb = item.querySelector('.cv-arc-check');
            if (cb) cb.checked = true;
        }
    });
    updateCVArcSelectedCount();
}

function deselectAllCVArcs() {
    document.querySelectorAll('.cv-arc-check').forEach(cb => cb.checked = false);
    updateCVArcSelectedCount();
}

function importSelectedCVArcs() {
    const arcIds = getSelectedCVArcs();
    if (arcIds.length === 0) {
        showToast('No arcs selected', 'error');
        return;
    }

    const btn = document.getElementById('importCVArcBtn');
    btn.disabled = true;
    btn.querySelector('.btn-text').classList.add('d-none');
    btn.querySelector('.btn-loading').classList.remove('d-none');

    fetch('/api/reading-lists/cv-import-arcs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ arc_ids: arcIds })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                const modal = bootstrap.Modal.getInstance(document.getElementById('importCVModal'));
                if (modal) modal.hide();

                showToast(`Importing ${data.tasks.length} arc(s) -- track progress in the navbar`, 'info', 5000);

                data.tasks.forEach(task => {
                    pollImportStatus(task.task_id, `CV arc ${task.arc_id}`);
                });
            } else {
                showToast('Error: ' + data.message, 'error');
            }
        })
        .catch(err => {
            showToast('Import failed: ' + err.message, 'error');
        })
        .finally(() => {
            btn.disabled = false;
            btn.querySelector('.btn-text').classList.remove('d-none');
            btn.querySelector('.btn-loading').classList.add('d-none');
        });
}

// ==========================================
// Sync Reading List
// ==========================================

function syncReadingList(listId) {
    const btn = event.currentTarget;
    const originalHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Syncing...';

    fetch(`/api/reading-lists/${listId}/sync`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                if (data.changed) {
                    showToast(`Synced: ${data.added} added, ${data.removed} removed`, 'success');
                    setTimeout(() => window.location.reload(), 1500);
                } else {
                    showToast('No changes detected', 'info');
                }
            } else {
                showToast('Sync failed: ' + data.message, 'error');
            }
        })
        .catch(err => {
            showToast('Sync error: ' + err.message, 'error');
        })
        .finally(() => {
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        });
}

// ==========================================
// Source Search (GetComics / Usenet / DC++)
// ==========================================
//
// The fan-out to all three sources lives in clu-source-search.js, shared with
// the Series and Wanted pages. This page used to carry its own GetComics-only
// copy of the modal.

let currentSearchSeries = '';
let currentSearchIssue = '';
let currentSearchYear = '';
let getcomicsModal = null;
let sourceSearch = null;

// The modal only exists for users who can manage the list, so nothing here may
// be built on page load — CLU.createSourceSearch binds a listener to the
// results element as soon as it is created.
function ensureGetComicsModal() {
    if (!getcomicsModal) {
        const el = document.getElementById('getcomicsModal');
        if (!el) return null;
        getcomicsModal = new bootstrap.Modal(el);
    }
    return getcomicsModal;
}

function ensureSourceSearch() {
    if (!sourceSearch) {
        const resultsEl = document.getElementById('getcomicsResults');
        if (!resultsEl) return null;
        sourceSearch = CLU.createSourceSearch({
            resultsEl: resultsEl,
            getContext: () => ({
                series: currentSearchSeries,
                issue: currentSearchIssue,
                year: currentSearchYear,
            }),
            // The shared module speaks Bootstrap severities; showToast() here
            // only knows 'success'/'error' and renders anything else blue.
            toast: (message, type) => showToast(message, type === 'danger' ? 'error' : type),
            // The keep-open button leaves the results up so the user can grab
            // more than one file from a single search.
            onQueued: ({ keepOpen }) => {
                if (!keepOpen) setTimeout(() => getcomicsModal.hide(), 500);
            },
        });
    }
    return sourceSearch;
}

function openGetComicsSearch(series, issueNumber, issueYear) {
    const modal = ensureGetComicsModal();
    if (!modal) return;

    currentSearchSeries = series;
    currentSearchIssue = issueNumber || '';
    currentSearchYear = issueYear || '';

    // The issue's year narrows every source — "Iron Man 8" matches every volume
    // that ever had an #8, "Iron Man 8 2026" matches this one.
    document.getElementById('getcomicsQuery').value =
        [series, currentSearchIssue, currentSearchYear]
            .filter(part => part !== '' && part !== null && part !== undefined)
            .join(' ');

    modal.show();
    doGetComicsSearch();
}

// The query box is the source of truth for the year: seeded by
// openGetComicsSearch(), but a user who edits or clears it widens/narrows
// every source together.
function extractQueryYear(query) {
    const m = String(query).trim().match(/(?:^|\s)((?:19|20)\d{2})$/);
    return m ? m[1] : '';
}

async function doGetComicsSearch() {
    const query = document.getElementById('getcomicsQuery').value.trim();
    if (!query) return;

    const search = ensureSourceSearch();
    if (!search) return;

    // Keep the per-source year filter in step with what's in the box.
    currentSearchYear = extractQueryYear(query);
    await search.run(query);
}
