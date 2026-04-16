/*
 * metadata_browser.js — controller for /library (metadata-driven browser).
 *
 * State is held in a single `state` object and serialised to the URL so links
 * are shareable. Grid items reuse the #grid-item-template from the page (same
 * markup as collection.html) so per-item actions wired by clu-cbz-*.js,
 * clu-delete.js, reading_list_picker.js continue to work.
 */
(function () {
    'use strict';

    const state = {
        axis: 'publisher',
        sort: 'alpha',
        page: 1,
        perPage: 50,
        // Drill-down filters (set by clicking group cards, not a sidebar)
        publisher: '',
        series: '',
        year_from: null,
        year_to: null,
        search: '',
        letter: '__ALL__',
        // Result cache for letter filter / pagination
        currentItems: [],
        currentTotal: 0,
        currentLevel: 'publisher',
    };

    let browseAbort = null;
    let searchTimer = null;

    // ------- URL sync -------

    function readStateFromUrl() {
        const p = new URLSearchParams(window.location.search);
        state.axis = p.get('axis') || 'publisher';
        state.sort = p.get('sort') || 'alpha';
        state.search = p.get('search') || '';
        state.page = parseInt(p.get('page') || '1', 10) || 1;
        state.perPage = parseInt(p.get('per_page') || '50', 10) || 50;
        state.publisher = p.get('publisher') || '';
        state.series = p.get('series') || '';
        const yf = p.get('year_from'); const yt = p.get('year_to');
        state.year_from = yf ? parseInt(yf, 10) : null;
        state.year_to = yt ? parseInt(yt, 10) : null;
    }

    function writeStateToUrl() {
        const p = new URLSearchParams();
        p.set('axis', state.axis);
        if (state.sort !== 'alpha') p.set('sort', state.sort);
        if (state.page !== 1) p.set('page', String(state.page));
        if (state.perPage !== 50) p.set('per_page', String(state.perPage));
        if (state.search) p.set('search', state.search);
        if (state.publisher) p.set('publisher', state.publisher);
        if (state.series) p.set('series', state.series);
        if (state.year_from != null) p.set('year_from', String(state.year_from));
        if (state.year_to != null) p.set('year_to', String(state.year_to));
        const qs = p.toString();
        const url = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
        window.history.replaceState(null, '', url);
    }

    function buildRequestParams(extra) {
        const p = new URLSearchParams();
        p.set('axis', state.axis);
        p.set('sort', state.sort);
        if (state.publisher) p.set('publisher', state.publisher);
        if (state.series) p.set('series', state.series);
        if (state.year_from != null) p.set('year_from', String(state.year_from));
        if (state.year_to != null) p.set('year_to', String(state.year_to));
        if (state.search) p.set('search', state.search);
        if (extra) Object.entries(extra).forEach(([k, v]) => p.set(k, String(v)));
        return p;
    }

    // ------- API call -------

    async function fetchBrowse() {
        if (browseAbort) browseAbort.abort();
        browseAbort = new AbortController();
        const offset = (state.page - 1) * state.perPage;
        const params = buildRequestParams({ offset, limit: state.perPage });
        try {
            const res = await fetch('/api/metadata/browse?' + params.toString(), { signal: browseAbort.signal });
            if (!res.ok) return null;
            return await res.json();
        } catch (e) {
            if (e.name !== 'AbortError') console.error('browse fetch failed', e);
            return null;
        }
    }

    // ------- Grid rendering -------

    function renderBreadcrumb() {
        const root = document.getElementById('mbBreadcrumb');
        if (!root) return;
        root.innerHTML = '';
        const push = (text, handler) => {
            const li = document.createElement('li');
            li.className = 'breadcrumb-item' + (handler ? '' : ' active');
            if (handler) {
                const a = document.createElement('a');
                a.href = '#'; a.textContent = text;
                a.onclick = (e) => { e.preventDefault(); handler(); };
                li.appendChild(a);
            } else {
                li.textContent = text;
            }
            root.appendChild(li);
        };
        const hasFilter = state.publisher || state.series;
        push('Library', hasFilter ? () => { clearDrilldown(); reload(); } : null);
        if (state.publisher) {
            push(`Publisher: ${state.publisher}`, state.series
                ? () => { state.series = ''; state.page = 1; reload(); }
                : null);
        }
        if (state.series) {
            push(`Series: ${state.series}`, null);
        }
    }

    function clearDrilldown() {
        state.publisher = '';
        state.series = '';
        state.year_from = null;
        state.year_to = null;
        state.page = 1;
        state.letter = '__ALL__';
    }

    function renderGrid(data) {
        const grid = document.getElementById('mbGrid');
        const empty = document.getElementById('mbEmpty');
        const template = document.getElementById('grid-item-template');
        if (!grid || !template) return;

        grid.innerHTML = '';
        const items = filterByLetter(data.items || []);
        if (!items.length) {
            grid.style.display = 'none';
            empty.style.display = 'block';
            return;
        }
        grid.style.display = 'grid';
        empty.style.display = 'none';

        // Expose flat item list to reader.js for next-issue detection
        if (data.level === 'issue') {
            window._readerAllItems = items.map(it => ({
                type: 'file', name: it.name, path: it.path
            }));
        } else {
            window._readerAllItems = [];
        }

        const frag = document.createDocumentFragment();
        items.forEach(item => {
            const node = data.level === 'issue'
                ? buildIssueCard(item, template)
                : buildGroupCard(item, data.level, template);
            if (node) frag.appendChild(node);
        });
        grid.appendChild(frag);
    }

    function filterByLetter(items) {
        if (state.letter === '__ALL__') return items;
        if (state.letter === '#') {
            return items.filter(i => !/^[a-z]/i.test(((i.name || i.value) || '').trim()));
        }
        return items.filter(i => ((i.name || i.value) || '').trim().toUpperCase().startsWith(state.letter));
    }

    function buildGroupCard(item, level, template) {
        const clone = template.content.cloneNode(true);
        const gridItem = clone.querySelector('.grid-item');
        gridItem.classList.add('folder');
        if (level === 'publisher' || level === 'series' || level === 'decade' || level === 'year') {
            gridItem.classList.add('root-folder');
        }
        const img = clone.querySelector('.thumbnail');
        const overlay = clone.querySelector('.icon-overlay');
        const icon = overlay.querySelector('i');
        const nameEl = clone.querySelector('.item-name');
        const metaEl = clone.querySelector('.item-meta');

        const label = item.name || String(item.value);
        nameEl.textContent = label;
        nameEl.title = label;

        const pieces = [`${item.count} issue${item.count === 1 ? '' : 's'}`];
        if (level === 'series' && item.publisher) pieces.unshift(item.publisher);
        if (level === 'series' && item.year) pieces.push(String(item.year));
        metaEl.textContent = pieces.join(' · ');

        clone.querySelector('.info-button').style.display = 'none';
        clone.querySelector('.favorite-button').style.display = 'none';
        clone.querySelector('.to-read-button').style.display = 'none';
        clone.querySelector('.item-actions').style.display = 'none';

        if (item.thumbnail_url) {
            img.src = item.thumbnail_url;
            img.style.display = 'block';
            overlay.style.display = 'none';
        } else {
            img.style.display = 'none';
            icon.className = level === 'publisher'
                ? 'bi bi-building'
                : level === 'series' ? 'bi bi-journal-text' : 'bi bi-calendar3';
        }

        gridItem.onclick = () => {
            if (level === 'publisher') {
                state.publisher = label;
                state.axis = 'series';
            } else if (level === 'series') {
                state.series = label;
            } else if (level === 'decade') {
                state.year_from = item.value;
                state.year_to = item.value + 9;
                state.axis = 'year';
            } else if (level === 'year') {
                state.year_from = item.value;
                state.year_to = item.value;
                state.axis = 'series';
            }
            state.page = 1;
            state.letter = '__ALL__';
            reload();
        };

        return clone;
    }

    function buildIssueCard(item, template) {
        const clone = template.content.cloneNode(true);
        const gridItem = clone.querySelector('.grid-item');
        gridItem.classList.add('file', 'has-comic');
        gridItem.setAttribute('data-path', item.path);

        const img = clone.querySelector('.thumbnail');
        const nameEl = clone.querySelector('.item-name');
        const metaEl = clone.querySelector('.item-meta');

        nameEl.textContent = item.name;
        nameEl.title = item.name;

        const meta = [];
        if (item.series) meta.push(item.series);
        if (item.number) meta.push('#' + item.number);
        if (item.year) meta.push(item.year);
        metaEl.textContent = meta.join(' · ') || (window.CLU && CLU.formatFileSize ? CLU.formatFileSize(item.size) : '');

        if (item.thumbnail_url) {
            img.src = item.thumbnail_url;
            img.onerror = function () { this.src = '/static/images/error.svg'; };
        }

        if (item.has_comicinfo === 0) {
            const xmlBadge = clone.querySelector('.xml-badge');
            if (xmlBadge) xmlBadge.style.display = 'block';
        }

        // Wire per-item dropdown actions to CLU.* functions from the shared
        // clu-*.js scripts loaded on this page. Each handler sets up a
        // callback contract then calls the real implementation.
        const actionsDropdown = clone.querySelector('.item-actions');
        if (actionsDropdown) {
            const wire = (sel, handler) => {
                const el = actionsDropdown.querySelector(sel);
                if (el) el.onclick = (e) => { e.preventDefault(); e.stopPropagation(); handler(); };
            };
            wire('.action-edit', () => {
                if (!CLU || !CLU.setupEditModalDropZone) return;
                window._cluCbzEdit = {
                    onSaveComplete: () => reload(),
                };
                const editModal = new bootstrap.Modal(document.getElementById('editCBZModal'));
                const container = document.getElementById('editInlineContainer');
                container.innerHTML = `<div class="d-flex justify-content-center my-3">
                    <button class="btn btn-primary" type="button" disabled>
                        <span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>
                        Unpacking CBZ File ...
                    </button></div>`;
                editModal.show();
                const filename = item.path.split('/').pop().split('\\').pop();
                document.getElementById('editCBZModalLabel').textContent = `Editing CBZ File | ${filename}`;
                CLU.setupEditModalDropZone();
                fetch(`/edit?file_path=${encodeURIComponent(item.path)}`)
                    .then(r => r.ok ? r.json() : Promise.reject(new Error('Failed to load edit content.')))
                    .then(data => {
                        document.getElementById('editInlineContainer').innerHTML = data.modal_body;
                        document.getElementById('editInlineFolderName').value = data.folder_name;
                        document.getElementById('editInlineZipFilePath').value = data.zip_file_path;
                        document.getElementById('editInlineOriginalFilePath').value = data.original_file_path;
                        CLU.sortInlineEditCards();
                    })
                    .catch(err => {
                        container.innerHTML = `<div class="alert alert-danger"><strong>Error:</strong> ${err.message}</div>`;
                    });
            });
            wire('.action-metadata', () => {
                if (!CLU || !CLU.searchMetadata) return;
                window._cluMetadata = {
                    getLibraryId: () => null,
                    onMetadataFound: () => reload(),
                    onBatchComplete: () => reload(),
                };
                CLU.searchMetadata(item.path, item.name);
            });
            wire('.action-add-to-list', () => {
                if (typeof openAddToReadingListModal === 'function') openAddToReadingListModal(item.path);
            });
            wire('.action-delete', () => {
                if (!CLU || !CLU.showDeleteConfirmation) return;
                window._cluDelete = {
                    onDeleteComplete: () => reload(),
                };
                CLU.showDeleteConfirmation(item.path, item.name, {
                    size: item.size, type: 'file', showDetails: true,
                });
            });
            // Crop/Rebuild/Enhance/Remove/Set-Read-Date are only defined on
            // collection.html; hide those menu items here to avoid dead clicks.
            ['.action-crop', '.action-remove-first', '.action-rebuild',
             '.action-enhance', '.action-set-read-date', '.action-mark-unread']
                .forEach(sel => {
                    const el = actionsDropdown.querySelector(sel);
                    if (el) el.closest('li').style.display = 'none';
                });
        }

        // Info button → CBZ info modal
        const info = clone.querySelector('.info-button');
        if (info) {
            info.onclick = (e) => {
                e.preventDefault(); e.stopPropagation();
                if (CLU && CLU.showCBZInfo) CLU.showCBZInfo(item.path, item.name);
            };
        }

        gridItem.onclick = (e) => {
            if (e.target.closest('.no-propagation') || e.target.closest('.item-actions') || e.target.closest('.dropdown-menu')) return;
            if (typeof window.openComicReader === 'function') {
                window.openComicReader(item.path);
            } else {
                window.location.href = '/reader?path=' + encodeURIComponent(item.path);
            }
        };

        return clone;
    }

    // ------- A–Z filter + pagination -------

    function renderLetterFilter(items) {
        const container = document.getElementById('mbLetterFilter');
        const grp = container.querySelector('.btn-group');
        grp.innerHTML = '';

        const firstChars = new Set();
        (items || []).forEach(i => {
            const s = ((i.name || i.value) || '').trim();
            if (!s) return;
            const c = s[0].toUpperCase();
            firstChars.add(/^[A-Z]$/.test(c) ? c : '#');
        });
        const letters = ['__ALL__', ...Array.from(firstChars).sort((a, b) => {
            if (a === '#') return 1; if (b === '#') return -1; return a.localeCompare(b);
        })];
        if (letters.length <= 1) {
            container.style.display = 'none';
            return;
        }
        container.style.display = '';
        letters.forEach(l => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-outline-secondary' + (l === state.letter ? ' active' : '');
            btn.textContent = l === '__ALL__' ? 'All' : l;
            btn.onclick = () => { state.letter = l; renderBrowse(lastBrowseData); renderPagination(); };
            grp.appendChild(btn);
        });
    }

    function renderPagination() {
        const nav = document.getElementById('mbPagination');
        const ul = nav.querySelector('ul');
        ul.innerHTML = '';
        const totalPages = Math.max(1, Math.ceil((state.currentTotal || 0) / state.perPage));
        if (totalPages <= 1) { nav.style.display = 'none'; return; }
        nav.style.display = '';

        const addItem = (label, page, disabled, active) => {
            const li = document.createElement('li');
            li.className = 'page-item' + (disabled ? ' disabled' : '') + (active ? ' active' : '');
            const a = document.createElement('a');
            a.className = 'page-link'; a.href = '#'; a.textContent = label;
            a.onclick = (e) => {
                e.preventDefault();
                if (disabled || active) return;
                state.page = page; reload();
            };
            li.appendChild(a); ul.appendChild(li);
        };

        addItem('«', Math.max(1, state.page - 1), state.page === 1, false);
        const window_ = 2;
        const pages = new Set([1, totalPages, state.page]);
        for (let i = 1; i <= window_; i++) {
            if (state.page - i >= 1) pages.add(state.page - i);
            if (state.page + i <= totalPages) pages.add(state.page + i);
        }
        const sorted = Array.from(pages).sort((a, b) => a - b);
        let prev = 0;
        sorted.forEach(p => {
            if (p - prev > 1) addItem('…', 0, true, false);
            addItem(String(p), p, false, p === state.page);
            prev = p;
        });
        addItem('»', Math.min(totalPages, state.page + 1), state.page === totalPages, false);
    }

    // ------- Orchestration -------

    let lastBrowseData = { items: [], total: 0, level: 'publisher' };

    function renderBrowse(data) {
        lastBrowseData = data;
        state.currentItems = data.items || [];
        state.currentTotal = data.total || 0;
        state.currentLevel = data.level;
        renderGrid(data);
    }

    async function reload() {
        document.getElementById('mbLoading').style.display = 'block';
        writeStateToUrl();
        renderBreadcrumb();

        const browse = await fetchBrowse();

        document.getElementById('mbLoading').style.display = 'none';

        if (browse) {
            renderBrowse(browse);
            renderLetterFilter(browse.items || []);
            renderPagination();
        }
    }

    // ------- Escaping helpers -------

    function escapeHtml(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }
    function escapeAttr(s) { return escapeHtml(s); }

    // ------- Event wiring -------

    function wireControls() {
        document.querySelectorAll('input[name="mbAxis"]').forEach(r => {
            r.addEventListener('change', () => {
                if (!r.checked) return;
                state.axis = r.value;
                state.page = 1;
                state.letter = '__ALL__';
                reload();
            });
        });
        const sort = document.getElementById('mbSort');
        if (sort) sort.addEventListener('change', () => {
            state.sort = sort.value; state.page = 1; reload();
        });
        const per = document.getElementById('mbPerPage');
        if (per) per.addEventListener('change', () => {
            state.perPage = parseInt(per.value, 10) || 50; state.page = 1; reload();
        });
        const refresh = document.getElementById('mbRefreshBtn');
        if (refresh) refresh.onclick = () => reload();

        const search = document.getElementById('mbSearch');
        if (search) {
            search.value = state.search;
            search.addEventListener('input', (e) => {
                clearTimeout(searchTimer);
                searchTimer = setTimeout(() => {
                    state.search = e.target.value.trim();
                    state.page = 1;
                    reload();
                }, 250);
            });
        }
    }

    function syncControlsFromState() {
        const axisInput = document.querySelector(`input[name="mbAxis"][value="${state.axis}"]`);
        if (axisInput) axisInput.checked = true;
        const sort = document.getElementById('mbSort');
        if (sort) sort.value = state.sort;
        const per = document.getElementById('mbPerPage');
        if (per) per.value = String(state.perPage);
    }

    // ------- Boot -------

    document.addEventListener('DOMContentLoaded', () => {
        readStateFromUrl();
        syncControlsFromState();
        wireControls();

        // Prime read-issues set for reader.js sibling/next-issue logic.
        fetch('/api/issues-read-paths')
            .then(r => r.ok ? r.json() : { paths: [] })
            .then(d => { window._readerReadIssuesSet = new Set(d.paths || []); })
            .catch(() => { window._readerReadIssuesSet = new Set(); });

        reload();
    });

    // Expose for debugging
    window.MB = { state, reload };
})();
