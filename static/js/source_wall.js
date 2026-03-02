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

function showSuccess(msg) {
    _showToast(msg, 'text-bg-success');
}

function showError(msg) {
    _showToast(msg, 'text-bg-danger');
}

function _showToast(msg, cls) {
    const el = document.getElementById('swToast');
    const body = document.getElementById('swToastBody');
    if (!el || !body) return;
    el.className = 'toast align-items-center border-0 ' + cls;
    body.textContent = msg;
    const toast = bootstrap.Toast.getOrCreateInstance(el, { delay: 3000 });
    toast.show();
}

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
                showError(data.error || 'Failed to load');
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
            showError('Error loading files');
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

        // Read column (empty for directories)
        const tdRead = document.createElement('td');
        tr.appendChild(tdRead);

        // Name with folder icon
        const tdName = document.createElement('td');
        tdName.colSpan = swActiveColumns.length;
        tdName.innerHTML = `<i class="bi bi-folder-fill text-warning me-2"></i>${escapeHtml(dir.name)}`;
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

        // Read icon
        const tdRead = document.createElement('td');
        const readIcon = document.createElement('i');
        readIcon.className = 'bi bi-book sw-read-icon';
        readIcon.title = 'Read';
        readIcon.addEventListener('click', (e) => {
            e.stopPropagation();
            openComicReader(file.path);
        });
        tdRead.appendChild(readIcon);
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
                td.addEventListener('click', () => startCellEdit(td, file.path, col, value));
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

    // Read header
    const thRead = document.createElement('th');
    thRead.style.width = '30px';
    thRead.innerHTML = '<i class="bi bi-book"></i>';
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

function startCellEdit(td, path, field, currentValue) {
    if (td.querySelector('input')) return; // Already editing

    const originalText = currentValue;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'sw-edit-input';
    input.value = currentValue;

    td.textContent = '';
    td.appendChild(input);
    input.focus();
    input.select();

    function commit() {
        const newValue = input.value.trim();
        td.textContent = newValue;
        td.title = newValue;

        if (newValue !== originalText) {
            saveFieldUpdate(path, field, newValue, td);
        }
    }

    function cancel() {
        td.textContent = originalText;
        td.title = originalText;
    }

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
                showError(data.error || 'Update failed');
            }
        })
        .catch(err => {
            td.classList.add('sw-flash-error');
            setTimeout(() => td.classList.remove('sw-flash-error'), 1000);
            showError('Network error');
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
        showError('Please select a field');
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
                showSuccess(`Updated ${paths.length} files`);

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
                showError(data.error || 'Bulk update failed');
            }
        })
        .catch(err => {
            showError('Network error');
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
                showSuccess('Columns saved');
                renderTable();
            }
        })
        .catch(err => console.error('Failed to save columns:', err));

    bootstrap.Modal.getInstance(document.getElementById('swColumnModal'))?.hide();
}

// ── Utility ──

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

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
