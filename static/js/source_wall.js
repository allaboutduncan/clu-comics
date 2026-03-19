// ============================================================================
// SOURCE WALL - Metadata Table Editor
// ============================================================================

// State
let swCurrentPath = '';
let swCurrentLibrary = null;
let swDirectories = [];
let swFiles = [];
let swActiveColumns = ['name', 'ci_volume'];
let swSortColumn = 'name';
let swSortAsc = true;
let swSelectedFiles = new Set();
let swLastSelectedIndex = -1;
let swActiveFilter = null;
let swReadIssuesSet = new Set();

// Load read issues for source wall
fetch('/api/issues-read-paths')
    .then(r => r.json())
    .then(data => { swReadIssuesSet = new Set(data.paths || []); })
    .catch(err => console.warn('Failed to load read issues:', err));

// Column definitions
const SW_COLUMNS = {
    name:           { label: 'Name',         editable: false },
    ci_title:       { label: 'Title',        editable: true },
    ci_series:      { label: 'Series',       editable: true },
    ci_number:      { label: 'Number',       editable: true },
    ci_count:       { label: 'Count',        editable: true },
    ci_volume:      { label: 'Volume',       editable: true },
    ci_year:        { label: 'Year',         editable: true },
    ci_writer:      { label: 'Writer',       editable: true },
    ci_penciller:   { label: 'Penciller',    editable: true },
    ci_inker:       { label: 'Inker',        editable: true },
    ci_colorist:    { label: 'Colorist',     editable: true },
    ci_letterer:    { label: 'Letterer',     editable: true },
    ci_coverartist: { label: 'Cover Artist', editable: true },
    ci_publisher:   { label: 'Publisher',    editable: true },
    ci_genre:       { label: 'Genre',        editable: true },
    ci_characters:  { label: 'Characters',   editable: true },
};

// ── Toast helpers ──



// ── Library loading ──

function loadLibraryDropdowns() {
    fetch('/api/libraries')
        .then(r => r.json())
        .then(data => {
            const menu = document.getElementById('swLibraryMenu');
            menu.innerHTML = '';
            const libs = data.libraries || data || [];
            libs.forEach(lib => {
                if (!lib.enabled && lib.enabled !== undefined) return;
                const li = document.createElement('li');
                const a = document.createElement('a');
                a.className = 'dropdown-item';
                a.href = '#';
                a.textContent = lib.name;
                a.addEventListener('click', (e) => {
                    e.preventDefault();
                    selectLibrary(lib.path, lib.name, lib.id);
                });
                li.appendChild(a);
                menu.appendChild(li);
            });
        })
        .catch(err => console.error('Failed to load libraries:', err));
}

function selectLibrary(path, name, id) {
    swCurrentLibrary = { path, name, id };
    document.getElementById('swLibraryName').textContent = name;
    localStorage.setItem('sw_library', JSON.stringify({ path, name, id }));
    loadPath(path);
}

// ── Path loading ──

function loadPath(path) {
    swCurrentPath = path;
    swSelectedFiles.clear();
    updateBulkBar();

    document.getElementById('swTable').style.display = 'none';
    document.getElementById('swEmptyState').style.display = 'none';
    document.getElementById('swLoadingState').style.display = '';

    fetch(`/api/source-wall/files?path=${encodeURIComponent(path)}`)
        .then(r => r.json())
        .then(data => {
            document.getElementById('swLoadingState').style.display = 'none';
            if (!data.success) {
                CLU.showError(data.error || 'Failed to load');
                return;
            }
            swDirectories = data.directories || [];
            swFiles = data.files || [];

            // Update reader items for next-issue detection
            window._readerAllItems = swFiles.map(f => ({
                name: f.name,
                path: f.path,
                type: 'file',
            }));

            renderBreadcrumb();
            renderFilterBar();
            renderTable();
        })
        .catch(err => {
            document.getElementById('swLoadingState').style.display = 'none';
            CLU.showError('Error loading files');
            console.error(err);
        });
}

// ── Breadcrumb ──

function renderBreadcrumb() {
    const ol = document.getElementById('swBreadcrumb');
    ol.innerHTML = '';

    if (!swCurrentLibrary) return;

    const libRoot = swCurrentLibrary.path;
    const relative = swCurrentPath.startsWith(libRoot)
        ? swCurrentPath.slice(libRoot.length)
        : '';
    const parts = relative.split('/').filter(Boolean);

    // Library root
    const li0 = document.createElement('li');
    li0.className = 'breadcrumb-item';
    
    const icon0 = document.createElement('i');
    icon0.className = 'bi bi-hdd-network me-1 text-primary';

    if (parts.length > 0) {
        const a = document.createElement('a');
        a.href = '#';
        a.className = 'text-decoration-none fw-medium';
        a.appendChild(icon0);
        a.appendChild(document.createTextNode(swCurrentLibrary.name));
        a.addEventListener('click', (e) => { e.preventDefault(); loadPath(libRoot); });
        li0.appendChild(a);
    } else {
        li0.classList.add('active', 'fw-medium');
        li0.appendChild(icon0);
        li0.appendChild(document.createTextNode(swCurrentLibrary.name));
    }
    ol.appendChild(li0);

    // Sub-path segments
    parts.forEach((part, i) => {
        const li = document.createElement('li');
        li.className = 'breadcrumb-item';
        
        const isLast = (i === parts.length - 1);
        
        const folderIcon = document.createElement('i');
        folderIcon.className = isLast ? 'bi bi-folder2-open me-1 text-secondary' : 'bi bi-folder2 me-1 text-secondary';

        if (!isLast) {
            const a = document.createElement('a');
            a.href = '#';
            a.className = 'text-decoration-none';
            a.appendChild(folderIcon);
            a.appendChild(document.createTextNode(part));
            const segPath = libRoot + '/' + parts.slice(0, i + 1).join('/');
            a.addEventListener('click', (e) => { e.preventDefault(); loadPath(segPath); });
            li.appendChild(a);
        } else {
            li.classList.add('active');
            li.appendChild(folderIcon);
            li.appendChild(document.createTextNode(part));
        }
        ol.appendChild(li);
    });
}

// ── Directory Filter Bar ──

function renderFilterBar() {
    const bar = document.getElementById('swFilterBar');
    const btnContainer = document.getElementById('swFilterButtons');
    const searchRow = document.getElementById('swSearchRow');

    if (swDirectories.length === 0) {
        bar.style.display = 'none';
        return;
    }

    bar.style.display = '';
    btnContainer.innerHTML = '';
    swActiveFilter = null;

    // Collect first letters
    const letters = new Set();
    swDirectories.forEach(d => {
        const first = d.name.charAt(0).toUpperCase();
        letters.add(/[A-Z]/.test(first) ? first : '#');
    });

    const sorted = [...letters].sort();

    // "All" button
    const allBtn = document.createElement('button');
    allBtn.className = 'btn btn-sm btn-primary';
    allBtn.textContent = 'All';
    allBtn.addEventListener('click', () => {
        swActiveFilter = null;
        document.querySelectorAll('#swFilterButtons .btn').forEach(b => b.classList.remove('btn-primary'));
        document.querySelectorAll('#swFilterButtons .btn').forEach(b => b.classList.add('btn-outline-secondary'));
        allBtn.classList.remove('btn-outline-secondary');
        allBtn.classList.add('btn-primary');
        applyDirectoryFilter();
    });
    btnContainer.appendChild(allBtn);

    sorted.forEach(letter => {
        const btn = document.createElement('button');
        btn.className = 'btn btn-sm btn-outline-secondary';
        btn.textContent = letter;
        btn.addEventListener('click', () => {
            swActiveFilter = letter;
            document.querySelectorAll('#swFilterButtons .btn').forEach(b => {
                b.classList.remove('btn-primary');
                b.classList.add('btn-outline-secondary');
            });
            btn.classList.remove('btn-outline-secondary');
            btn.classList.add('btn-primary');
            applyDirectoryFilter();
        });
        btnContainer.appendChild(btn);
    });

    // Show search if >25 directories
    searchRow.style.display = swDirectories.length > 25 ? '' : 'none';
    const searchInput = document.getElementById('swDirSearch');
    if (searchInput) searchInput.value = '';
}

function filterDirectoriesSW() {
    const query = (document.getElementById('swDirSearch')?.value || '').toLowerCase();
    const rows = document.querySelectorAll('.sw-directory-row');
    rows.forEach(row => {
        const name = (row.dataset.name || '').toLowerCase();
        row.style.display = name.includes(query) ? '' : 'none';
    });
}

function applyDirectoryFilter() {
    const rows = document.querySelectorAll('.sw-directory-row');
    rows.forEach(row => {
        if (!swActiveFilter) {
            row.style.display = '';
            return;
        }
        const first = (row.dataset.name || '').charAt(0).toUpperCase();
        const matchLetter = /[A-Z]/.test(first) ? first : '#';
        row.style.display = matchLetter === swActiveFilter ? '' : 'none';
    });
}

// ── Table Rendering ──

function renderTable() {
    const table = document.getElementById('swTable');
    const tbody = document.getElementById('swTableBody');

    if (swDirectories.length === 0 && swFiles.length === 0) {
        table.style.display = 'none';
        document.getElementById('swEmptyState').style.display = '';
        return;
    }

    table.style.display = '';
    document.getElementById('swEmptyState').style.display = 'none';

    renderTableHeader();

    // Sort files
    const sortedFiles = [...swFiles].sort((a, b) => {
        let va = a[swSortColumn] || '';
        let vb = b[swSortColumn] || '';
        if (typeof va === 'string') va = va.toLowerCase();
        if (typeof vb === 'string') vb = vb.toLowerCase();
        if (va < vb) return swSortAsc ? -1 : 1;
        if (va > vb) return swSortAsc ? 1 : -1;
        return 0;
    });

    tbody.innerHTML = '';

    // Directory rows
    swDirectories.forEach(dir => {
        const tr = document.createElement('tr');
        tr.className = 'sw-directory-row';
        tr.dataset.name = dir.name;
        tr.addEventListener('click', () => loadPath(dir.path));

        // Checkbox column (empty for directories)
        const tdCb = document.createElement('td');
        tr.appendChild(tdCb);

        // Actions column (directory metadata dropdown)
        const tdActions = document.createElement('td');
        tdActions.className = 'sw-actions-cell';
        const dirDropdownId = 'swDirDrop' + dir.name.replace(/[^a-zA-Z0-9]/g, '_');
        tdActions.innerHTML =
            '<div class="dropdown">' +
            '<button class="btn btn-sm btn-outline-secondary dropdown-toggle sw-action-btn" type="button"' +
            ' id="' + dirDropdownId + '" data-bs-toggle="dropdown" aria-expanded="false">' +
            '<i class="bi bi-three-dots-vertical"></i></button>' +
            '<ul class="dropdown-menu dropdown-menu-end" aria-labelledby="' + dirDropdownId + '">' +
            '<li><a class="dropdown-item" href="#" data-action="dir-metadata"><i class="bi bi-cloud-download me-2"></i>Fetch All Metadata</a></li>' +
            '</ul></div>';
        tdActions.querySelector('.dropdown-toggle').addEventListener('click', (e) => {
            e.stopPropagation();
        });
        tdActions.querySelector('[data-action="dir-metadata"]').addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            fetchDirMetadataSW(dir.path, dir.name);
        });
        tr.appendChild(tdActions);

        // Read column (empty for directories)
        const tdRead = document.createElement('td');
        tr.appendChild(tdRead);

        // Name with folder icon
        const tdName = document.createElement('td');
        tdName.colSpan = swActiveColumns.length;
        tdName.innerHTML = `<i class="bi bi-folder-fill text-warning me-2"></i>${CLU.escapeHtml(dir.name)}`;
        tr.appendChild(tdName);

        tbody.appendChild(tr);
    });

    // File rows
    sortedFiles.forEach((file, fileIdx) => {
        const tr = document.createElement('tr');
        tr.dataset.path = file.path;

        // Checkbox
        const tdCb = document.createElement('td');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'form-check-input';
        cb.checked = swSelectedFiles.has(file.path);
        cb.addEventListener('click', (e) => {
            e.stopPropagation();
            handleFileSelect(file.path, fileIdx, e);
        });
        tdCb.appendChild(cb);
        tr.appendChild(tdCb);

        // Actions dropdown
        const tdActions = document.createElement('td');
        tdActions.className = 'sw-actions-cell';
        const dropdownId = 'swActionDrop' + fileIdx;
        tdActions.innerHTML =
            '<div class="dropdown">' +
            '<button class="btn btn-sm btn-outline-secondary dropdown-toggle sw-action-btn" type="button"' +
            ' id="' + dropdownId + '" data-bs-toggle="dropdown" aria-expanded="false">' +
            '<i class="bi bi-three-dots-vertical"></i></button>' +
            '<ul class="dropdown-menu dropdown-menu-end" aria-labelledby="' + dropdownId + '">' +
            '<li><a class="dropdown-item" href="#" data-action="crop"><i class="bi bi-scissors me-2"></i>Crop Cover</a></li>' +
            '<li><a class="dropdown-item" href="#" data-action="remove"><i class="bi bi-file-minus me-2"></i>Remove 1st Image</a></li>' +
            '<li><a class="dropdown-item" href="#" data-action="edit"><i class="bi bi-pencil-square me-2"></i>Edit Pages</a></li>' +
            '<li><a class="dropdown-item" href="#" data-action="rebuild"><i class="bi bi-arrow-repeat me-2"></i>Rebuild</a></li>' +
            '<li><a class="dropdown-item" href="#" data-action="enhance"><i class="bi bi-stars me-2"></i>Enhance</a></li>' +
            '<li><a class="dropdown-item" href="#" data-action="add"><i class="bi bi-file-plus me-2"></i>Add Blank to End</a></li>' +
            '<li><hr class="dropdown-divider"></li>' +
            '<li><a class="dropdown-item" href="#" data-action="metadata"><i class="bi bi-cloud-download me-2"></i>Fetch Metadata</a></li>' +
            '<li><a class="dropdown-item' + (swReadIssuesSet.has(file.path) ? '' : ' d-none') + '" href="#" data-action="mark-unread"><i class="bi bi-book me-2"></i>Mark as Unread</a></li>' +
            '<li><a class="dropdown-item' + (swReadIssuesSet.has(file.path) ? '' : ' d-none') + '" href="#" data-action="hide-history"><i class="bi bi-eye-slash me-2"></i>Hide from History</a></li>' +
            '<li><hr class="dropdown-divider"></li>' +
            '<li><a class="dropdown-item" href="#" data-action="info"><i class="bi bi-info-circle me-2"></i>Info</a></li>' +
            '<li><a class="dropdown-item" href="#" data-action="add-to-list"><i class="bi bi-journal-plus me-2"></i>Add to Reading List</a></li>' +
            '<li><hr class="dropdown-divider"></li>' +
            '<li><a class="dropdown-item text-danger" href="#" data-action="delete"><i class="bi bi-trash me-2"></i>Delete</a></li>' +
            '</ul></div>';
        tdActions.querySelectorAll('[data-action]').forEach(a => {
            a.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                const action = a.dataset.action;
                if (action === 'info') showFileInfo(file.path, file.name);
                else if (action === 'edit') openEditFile(file.path);
                else if (action === 'delete') deleteFileSW(file.path, file.name);
                else if (action === 'metadata') fetchMetadataSW(file.path, file.name);
                else if (action === 'mark-unread') markIssueAsUnreadSW(file.path);
                else if (action === 'hide-history') hideFromHistorySW(file.path);
                else if (action === 'add-to-list') openAddToReadingListModal(file.path);
                else streamingOpSW(action, file.path);
            });
        });
        tr.appendChild(tdActions);

        // Read icon
        const tdRead = document.createElement('td');
        tdRead.className = 'sw-read-cell';
        tdRead.innerHTML = '<button class="btn btn-sm btn-outline-primary sw-read-btn" title="Read">' +
            '<i class="bi bi-book"></i></button>';
        tdRead.querySelector('button').addEventListener('click', (e) => {
            e.stopPropagation();
            openComicReader(file.path);
        });
        tr.appendChild(tdRead);

        // Data columns
        swActiveColumns.forEach(col => {
            const td = document.createElement('td');
            const colDef = SW_COLUMNS[col];
            const value = file[col] || '';

            if (col === 'name') {
                td.className = 'sw-name-cell';
                td.textContent = value;
                td.title = value;
            } else if (colDef && colDef.editable) {
                td.className = 'sw-editable-cell';
                td.textContent = value;
                td.title = value;
                td.dataset.path = file.path;
                td.dataset.field = col;
                td.addEventListener('click', () => startCellEdit(td, file.path, col));
            } else {
                td.textContent = value;
            }

            tr.appendChild(td);
        });

        if (swSelectedFiles.has(file.path)) {
            tr.classList.add('sw-selected');
        }

        tbody.appendChild(tr);
    });
}

function renderTableHeader() {
    const thead = document.getElementById('swTableHead');
    thead.innerHTML = '';

    const tr = document.createElement('tr');

    // Checkbox header
    const thCb = document.createElement('th');
    thCb.style.width = '30px';
    const cbAll = document.createElement('input');
    cbAll.type = 'checkbox';
    cbAll.className = 'form-check-input';
    cbAll.addEventListener('change', (e) => {
        if (e.target.checked) {
            swFiles.forEach(f => swSelectedFiles.add(f.path));
        } else {
            swSelectedFiles.clear();
        }
        updateBulkBar();
        updateRowSelections();
    });
    thCb.appendChild(cbAll);
    tr.appendChild(thCb);

    // Actions header
    const thActions = document.createElement('th');
    thActions.style.width = '60px';
    thActions.textContent = '';
    tr.appendChild(thActions);

    // Read header
    const thRead = document.createElement('th');
    thRead.style.width = '40px';
    thRead.textContent = '';
    tr.appendChild(thRead);

    // Data columns
    swActiveColumns.forEach(col => {
        const th = document.createElement('th');
        th.className = 'sw-sortable';
        th.textContent = SW_COLUMNS[col]?.label || col;

        if (swSortColumn === col) {
            const span = document.createElement('span');
            span.className = 'sw-sort-indicator';
            span.textContent = swSortAsc ? '\u25B2' : '\u25BC';
            th.appendChild(span);
        }

        th.addEventListener('click', () => {
            if (swSortColumn === col) {
                swSortAsc = !swSortAsc;
            } else {
                swSortColumn = col;
                swSortAsc = true;
            }
            renderTable();
        });

        tr.appendChild(th);
    });

    thead.appendChild(tr);
}

// ── In-Place Editing ──

// Fields whose values are comma-separated lists
const SW_COMMA_FIELDS = new Set([
    'ci_writer', 'ci_penciller', 'ci_inker', 'ci_colorist',
    'ci_letterer', 'ci_coverartist', 'ci_genre', 'ci_characters',
]);

// Client-side suggest cache: { "ci_writer:ala" => ["Alan Moore", ...] }
const swSuggestCache = {};
let swSuggestTimer = null;

function startCellEdit(td, path, field) {
    if (td.querySelector('.sw-edit-wrapper')) return; // Already editing

    const originalText = td.textContent;

    // Build wrapper with input + clear icon
    const wrapper = document.createElement('div');
    wrapper.className = 'sw-edit-wrapper';

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'sw-edit-input';
    input.value = originalText;

    const clearBtn = document.createElement('i');
    clearBtn.className = 'bi bi-x-circle-fill sw-edit-clear';
    clearBtn.addEventListener('mousedown', (e) => {
        e.preventDefault(); // Prevent blur before clearing
        input.value = '';
        input.focus();
    });

    const suggestList = document.createElement('ul');
    suggestList.className = 'sw-suggest-list sw-suggest-fixed';

    wrapper.appendChild(input);
    wrapper.appendChild(clearBtn);

    // Append suggest list to body so it isn't clipped by table overflow
    document.body.appendChild(suggestList);

    td.textContent = '';
    td.classList.add('sw-editing');
    td.appendChild(wrapper);
    input.focus();
    input.select();

    function positionSuggestList() {
        const rect = input.getBoundingClientRect();
        suggestList.style.left = rect.left + 'px';
        suggestList.style.top = (rect.bottom + 2) + 'px';
        suggestList.style.width = rect.width + 'px';
    }

    suggestList._position = positionSuggestList;

    function dismissSuggestions() {
        suggestList.innerHTML = '';
        suggestList.style.display = 'none';
    }

    function cleanup() {
        dismissSuggestions();
        if (suggestList.parentNode) {
            suggestList.parentNode.removeChild(suggestList);
        }
    }

    function commit() {
        cleanup();
        td.classList.remove('sw-editing');
        const newValue = input.value.trim();
        td.textContent = newValue;
        td.title = newValue;

        if (newValue !== originalText) {
            saveFieldUpdate(path, field, newValue, td);
        }
    }

    function cancel() {
        cleanup();
        td.classList.remove('sw-editing');
        td.textContent = originalText;
        td.title = originalText;
    }

    // Auto-suggest on input
    input.addEventListener('input', () => {
        clearTimeout(swSuggestTimer);
        swSuggestTimer = setTimeout(() => fetchSuggestions(input, field, suggestList), 250);
    });

    input.addEventListener('blur', commit);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            input.removeEventListener('blur', commit);
            commit();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            input.removeEventListener('blur', commit);
            cancel();
        }
    });
}

function fetchSuggestions(input, field, suggestList) {
    const isComma = SW_COMMA_FIELDS.has(field);
    let query = input.value;

    // For comma fields, only query the segment after the last comma
    if (isComma) {
        const parts = query.split(',');
        query = parts[parts.length - 1].trim();
    }

    if (query.length < 3) {
        suggestList.innerHTML = '';
        suggestList.style.display = 'none';
        return;
    }

    const cacheKey = `${field}:${query.toLowerCase()}`;
    if (swSuggestCache[cacheKey]) {
        renderSuggestions(suggestList, swSuggestCache[cacheKey], input, field);
        return;
    }

    if (suggestList._position) suggestList._position();
    suggestList.innerHTML = '<li class="sw-suggest-item sw-suggest-loading">Searching\u2026</li>';
    suggestList.style.display = 'block';

    fetch(`/api/source-wall/suggest?field=${encodeURIComponent(field)}&q=${encodeURIComponent(query)}&path=${encodeURIComponent(swCurrentPath)}`)
        .then(r => r.json())
        .then(data => {
            const values = data.values || [];
            console.log('[SW suggest] response:', values.length, 'values for', query);
            // Only cache non-empty results so retries work after data is added
            if (values.length > 0) swSuggestCache[cacheKey] = values;
            // Only render if the input is still in the DOM (not already committed)
            if (input.isConnected) {
                renderSuggestions(suggestList, values, input, field);
            }
        })
        .catch(err => console.warn('Suggest error:', err));
}

function renderSuggestions(suggestList, values, input, field) {
    suggestList.innerHTML = '';
    if (values.length === 0) {
        suggestList.style.display = 'none';
        return;
    }

    const isComma = SW_COMMA_FIELDS.has(field);

    values.forEach(val => {
        const li = document.createElement('li');
        li.className = 'sw-suggest-item';
        li.textContent = val;
        li.addEventListener('mousedown', (e) => {
            e.preventDefault(); // Prevent blur
            if (isComma) {
                // Replace only the current (last) segment
                const parts = input.value.split(',');
                parts[parts.length - 1] = ' ' + val;
                input.value = parts.join(',').replace(/^,?\s*/, '');
            } else {
                input.value = val;
            }
            suggestList.innerHTML = '';
            suggestList.style.display = 'none';
            input.focus();
        });
        suggestList.appendChild(li);
    });

    if (suggestList._position) suggestList._position();
    suggestList.style.display = 'block';
}

function saveFieldUpdate(path, field, value, td) {
    fetch('/api/source-wall/update-field', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, field, value }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                td.classList.remove('sw-flash-error');
                td.classList.add('sw-flash-success');
                setTimeout(() => td.classList.remove('sw-flash-success'), 1000);

                // Update in-memory data
                const file = swFiles.find(f => f.path === path);
                if (file) file[field] = value;
            } else {
                td.classList.add('sw-flash-error');
                setTimeout(() => td.classList.remove('sw-flash-error'), 1000);
                CLU.showError(data.error || 'Update failed');
            }
        })
        .catch(err => {
            td.classList.add('sw-flash-error');
            setTimeout(() => td.classList.remove('sw-flash-error'), 1000);
            CLU.showError('Network error');
            console.error(err);
        });
}

// ── File Selection ──

function handleFileSelect(path, index, event) {
    if (event.shiftKey && swLastSelectedIndex >= 0) {
        // Range select
        const start = Math.min(swLastSelectedIndex, index);
        const end = Math.max(swLastSelectedIndex, index);
        const sortedFiles = getSortedFiles();
        for (let i = start; i <= end; i++) {
            swSelectedFiles.add(sortedFiles[i].path);
        }
    } else {
        if (swSelectedFiles.has(path)) {
            swSelectedFiles.delete(path);
        } else {
            swSelectedFiles.add(path);
        }
    }
    swLastSelectedIndex = index;
    updateBulkBar();
    updateRowSelections();
}

function getSortedFiles() {
    return [...swFiles].sort((a, b) => {
        let va = a[swSortColumn] || '';
        let vb = b[swSortColumn] || '';
        if (typeof va === 'string') va = va.toLowerCase();
        if (typeof vb === 'string') vb = vb.toLowerCase();
        if (va < vb) return swSortAsc ? -1 : 1;
        if (va > vb) return swSortAsc ? 1 : -1;
        return 0;
    });
}

function updateRowSelections() {
    document.querySelectorAll('#swTableBody tr[data-path]').forEach(tr => {
        const path = tr.dataset.path;
        const cb = tr.querySelector('input[type="checkbox"]');
        if (swSelectedFiles.has(path)) {
            tr.classList.add('sw-selected');
            if (cb) cb.checked = true;
        } else {
            tr.classList.remove('sw-selected');
            if (cb) cb.checked = false;
        }
    });
}

function clearSelection() {
    swSelectedFiles.clear();
    swLastSelectedIndex = -1;
    updateBulkBar();
    updateRowSelections();
}

function updateBulkBar() {
    const bar = document.getElementById('swBulkBar');
    const count = document.getElementById('swBulkCount');
    if (swSelectedFiles.size > 0) {
        bar.classList.remove('d-none');
        count.textContent = `${swSelectedFiles.size} selected`;
    } else {
        bar.classList.add('d-none');
    }
}

// ── Bulk Update ──

function applyBulkUpdate() {
    const field = document.getElementById('swBulkField').value;
    const value = document.getElementById('swBulkValue').value;

    if (!field) {
        CLU.showError('Please select a field');
        return;
    }

    const paths = [...swSelectedFiles];
    if (paths.length === 0) return;

    fetch('/api/source-wall/bulk-update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths, field, value }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                CLU.showSuccess(`Updated ${paths.length} files`);

                // Update table cells immediately
                paths.forEach(p => {
                    const file = swFiles.find(f => f.path === p);
                    if (file) file[field] = value;

                    document.querySelectorAll(`td[data-path="${CSS.escape(p)}"][data-field="${field}"]`).forEach(td => {
                        td.textContent = value;
                        td.title = value;
                        td.classList.add('sw-flash-success');
                        setTimeout(() => td.classList.remove('sw-flash-success'), 1000);
                    });
                });

                clearSelection();
                document.getElementById('swBulkValue').value = '';
            } else {
                CLU.showError(data.error || 'Bulk update failed');
            }
        })
        .catch(err => {
            CLU.showError('Network error');
            console.error(err);
        });
}

// ── Column Preferences ──

function loadColumnPreferences() {
    fetch('/api/source-wall/columns')
        .then(r => r.json())
        .then(data => {
            if (data.success && Array.isArray(data.columns) && data.columns.length > 0) {
                swActiveColumns = data.columns;
            }
        })
        .catch(err => console.error('Failed to load column preferences:', err));
}

function openColumnSelector() {
    const container = document.getElementById('swColumnChecks');
    container.innerHTML = '';

    Object.entries(SW_COLUMNS).forEach(([key, col]) => {
        const div = document.createElement('div');
        div.className = 'form-check';

        const input = document.createElement('input');
        input.type = 'checkbox';
        input.className = 'form-check-input';
        input.id = `sw-col-${key}`;
        input.value = key;
        input.checked = swActiveColumns.includes(key);

        // Name is always required
        if (key === 'name') {
            input.checked = true;
            input.disabled = true;
        }

        const label = document.createElement('label');
        label.className = 'form-check-label';
        label.htmlFor = `sw-col-${key}`;
        label.textContent = col.label;

        div.appendChild(input);
        div.appendChild(label);
        container.appendChild(div);
    });

    new bootstrap.Modal(document.getElementById('swColumnModal')).show();
}

function saveColumnPreferences() {
    const checks = document.querySelectorAll('#swColumnChecks input:checked');
    const cols = [...checks].map(c => c.value);

    // Ensure name is always first
    if (!cols.includes('name')) cols.unshift('name');

    swActiveColumns = cols;

    fetch('/api/source-wall/columns', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ columns: cols }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                CLU.showSuccess('Columns saved');
                renderTable();
            }
        })
        .catch(err => console.error('Failed to save columns:', err));

    bootstrap.Modal.getInstance(document.getElementById('swColumnModal'))?.hide();
}

// ── CLU Module Contracts ──

function setupDeleteContract() {
    window._cluDelete = {
        onDeleteComplete: function (path) {
            swFiles = swFiles.filter(f => f.path !== path);
            swSelectedFiles.delete(path);
            renderTable();
            updateBulkBar();
            CLU.showSuccess('File deleted successfully');
        },
        onBulkDeleteComplete: function (paths, results) {
            const successes = results.filter(r => r.success);
            const failures = results.filter(r => !r.success);
            successes.forEach(r => {
                swFiles = swFiles.filter(f => f.path !== r.path);
            });
            clearSelection();
            renderTable();
            if (successes.length > 0) CLU.showSuccess('Deleted ' + successes.length + ' file(s)');
            if (failures.length > 0) CLU.showError(failures.length + ' file(s) failed to delete');
        }
    };
}

function setupInfoContract() {
    window._cluCbzInfo = {
        onClearComplete: function () {
            loadPath(swCurrentPath);
        }
    };
}

function setupEditContract() {
    window._cluCbzEdit = {
        onSaveComplete: function () {
            loadPath(swCurrentPath);
        }
    };
}

function setupUpdateXmlContract() {
    window._cluUpdateXml = {
        onUpdateComplete: function () {
            loadPath(swCurrentPath);
        }
    };
}

function setupStreamingContract() {
    window._cluStreaming = {
        onComplete: function () {
            loadPath(swCurrentPath);
        }
    };
}

function setupMetadataContract() {
    window._cluMetadata = {
        getLibraryId: function () { return swCurrentLibrary ? swCurrentLibrary.id : null; },
        onMetadataFound: function () { loadPath(swCurrentPath); },
        onBatchComplete: function () { loadPath(swCurrentPath); }
    };
}

// ── Per-Row Actions ──

function showFileInfo(path, name) {
    setupInfoContract();
    CLU.showCBZInfo(path, name);
}

function openEditFile(path) {
    setupEditContract();

    var editModal = new bootstrap.Modal(document.getElementById('editCBZModal'));
    var container = document.getElementById('editInlineContainer');

    container.innerHTML = '<div class="d-flex justify-content-center my-3">' +
        '<button class="btn btn-primary" type="button" disabled>' +
        '<span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>' +
        ' Unpacking CBZ File ...</button></div>';

    editModal.show();
    CLU.setupEditModalDropZone();

    fetch('/edit?file_path=' + encodeURIComponent(path))
        .then(function (r) {
            if (!r.ok) throw new Error('Failed to load edit content.');
            return r.json();
        })
        .then(function (data) {
            document.getElementById('editInlineContainer').innerHTML = data.modal_body;
            document.getElementById('editInlineFolderName').value = data.folder_name;
            document.getElementById('editInlineZipFilePath').value = data.zip_file_path;
            document.getElementById('editInlineOriginalFilePath').value = data.original_file_path;
            CLU.sortInlineEditCards();
        })
        .catch(function (err) {
            container.innerHTML = '<div class="alert alert-danger" role="alert">' +
                '<strong>Error:</strong> ' + CLU.escapeHtml(err.message) + '</div>';
            CLU.showError(err.message);
        });
}

function deleteFileSW(path, name) {
    setupDeleteContract();
    CLU.showDeleteConfirmation(path, name);
}

function streamingOpSW(scriptType, path) {
    setupStreamingContract();
    CLU.executeStreamingOp(scriptType, path);
}

function fetchMetadataSW(path, name) {
    setupMetadataContract();
    CLU.searchMetadata(path, name);
}

function fetchDirMetadataSW(path, name) {
    setupMetadataContract();
    CLU.fetchDirectoryMetadata(path, name);
}

// ── Bulk Actions ──

function bulkDeleteSW() {
    if (swSelectedFiles.size === 0) return;
    setupDeleteContract();
    CLU.showBulkDeleteConfirmation(Array.from(swSelectedFiles));
}

function updateXmlSW() {
    if (!swCurrentPath) return;
    setupUpdateXmlContract();
    const folderName = swCurrentPath.split('/').pop() || 'Current Folder';
    CLU.openUpdateXmlModal(swCurrentPath, folderName);
}

// ── Utility ──


// ── Initialization ──

document.addEventListener('DOMContentLoaded', () => {
    loadLibraryDropdowns();
    loadColumnPreferences();

    // Restore saved library
    const saved = localStorage.getItem('sw_library');
    if (saved) {
        try {
            const lib = JSON.parse(saved);
            if (lib.path && lib.name) {
                selectLibrary(lib.path, lib.name, lib.id);
            }
        } catch (e) { /* ignore */ }
    }
});

/**
 * Mark a read issue as unread from source wall
 * @param {string} path - Full path to the comic file
 */
async function markIssueAsUnreadSW(path) {
    try {
        const response = await fetch('/api/favorites/issues', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: path })
        });
        const data = await response.json();
        if (data.success) {
            swReadIssuesSet.delete(path);
            CLU.showSuccess('Marked as unread');
        } else {
            CLU.showError('Failed to mark as unread');
        }
    } catch (error) {
        console.error('Error marking as unread:', error);
        CLU.showError('Error marking as unread');
    }
}

/**
 * Hide a read issue from timeline and wrapped views (source wall)
 * @param {string} path - Full path to the comic file
 */
async function hideFromHistorySW(path) {
    try {
        const response = await fetch('/api/favorites/issues/hide', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: path })
        });
        const data = await response.json();
        if (data.success) {
            CLU.showSuccess('Hidden from history');
        } else {
            CLU.showError('Failed to hide from history');
        }
    } catch (error) {
        console.error('Error hiding from history:', error);
        CLU.showError('Error hiding from history');
    }
}
