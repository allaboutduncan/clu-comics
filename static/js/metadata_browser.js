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

    // Page sizes are #mbPerPage's options and the default its
    // data-default-per-page (per_page_select, helpers/paging.py).
    const DEFAULT_PER_PAGE = CLU.defaultPerPage(document.getElementById('mbPerPage'));

    const state = {
        axis: 'publisher',
        sort: 'alpha',
        page: 1,
        perPage: DEFAULT_PER_PAGE,
        // Drill-down filters (set by clicking group cards, not a sidebar)
        publisher: '',
        series: '',
        year_from: null,
        year_to: null,
        search: '',
        // A–Z bar: '' (All), 'A'–'Z' or '#'. Applied by the server before
        // paging, like every other filter here.
        letter: '',
        // Result cache for pagination
        currentTotal: 0,
        currentLevel: 'publisher',
    };

    // The size last picked in the select; used when the URL does not say.
    const PER_PAGE_COOKIE = 'clu_per_page_library';

    let browseAbort = null;

    // ------- URL sync -------

    function readStateFromUrl() {
        const p = new URLSearchParams(window.location.search);
        state.axis = p.get('axis') || 'publisher';
        state.sort = p.get('sort') || 'alpha';
        state.search = p.get('search') || '';
        state.page = parseInt(p.get('page') || '1', 10) || 1;
        // A size from an older set (50, 100, 250) moves to the nearest one
        // offered instead of being dropped; a remembered one is saved back.
        const select = document.getElementById('mbPerPage');
        const fromUrl = CLU.snapPerPage(p.get('per_page'), select);
        const saved = CLU.rememberedPerPage(PER_PAGE_COOKIE);
        const fromCookie = CLU.snapPerPage(saved, select);
        if (fromCookie !== null && fromCookie !== saved) CLU.rememberPerPage(PER_PAGE_COOKIE, fromCookie);
        state.perPage = fromUrl || fromCookie || DEFAULT_PER_PAGE;
        state.publisher = p.get('publisher') || '';
        state.series = p.get('series') || '';
        const yf = p.get('year_from'); const yt = p.get('year_to');
        state.year_from = yf ? parseInt(yf, 10) : null;
        state.year_to = yt ? parseInt(yt, 10) : null;
        const letter = (p.get('letter') || '').toUpperCase();
        state.letter = (letter === '#' || /^[A-Z]$/.test(letter)) ? letter : '';
    }

    function writeStateToUrl() {
        const p = new URLSearchParams();
        p.set('axis', state.axis);
        if (state.sort !== 'alpha') p.set('sort', state.sort);
        if (state.page !== 1) p.set('page', String(state.page));
        // With a remembered size a missing per_page means "the cookie", not
        // the default, so the URL must name the size to reproduce it (#681).
        if (state.perPage !== DEFAULT_PER_PAGE || CLU.rememberedPerPage(PER_PAGE_COOKIE) !== null) {
            p.set('per_page', String(state.perPage));
        }
        if (state.search) p.set('search', state.search);
        if (state.publisher) p.set('publisher', state.publisher);
        if (state.series) p.set('series', state.series);
        if (state.year_from != null) p.set('year_from', String(state.year_from));
        if (state.year_to != null) p.set('year_to', String(state.year_to));
        if (state.letter) p.set('letter', state.letter);
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
        if (state.letter) p.set('letter', state.letter);
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
        push('Library', hasFilter ? () => {
            state.axis = rootAxis();
            clearDrilldown();
            syncControlsFromState();
            reload();
        } : null);
        if (state.publisher) {
            push(`Publisher: ${state.publisher}`, state.series
                ? () => { state.series = ''; state.page = 1; state.letter = ''; reload(); }
                : null);
        }
        if (state.series) {
            push(`Series: ${state.series}`, null);
        }
        // Page header title = the current level (the icon is left alone).
        const title = document.getElementById('mbTitle');
        const titleText = state.series || state.publisher || 'Library';
        if (title) {
            const span = title.querySelector('.clu-page-title-text');
            if (span) span.textContent = titleText;
            title.title = titleText;
        }
    }

    function clearDrilldown() {
        state.publisher = '';
        state.series = '';
        state.year_from = null;
        state.year_to = null;
        state.page = 1;
        state.letter = '';
    }

    // The axis the user was on before drilling into the current filter.
    // Derived so bookmarked deep-links (e.g. /library?publisher=X) also rewind
    // to the correct root view when the user clicks "Library".
    function rootAxis() {
        if (state.publisher) return 'publisher';
        if (state.year_from != null) return 'year';
        if (state.series) return 'series';
        return state.axis;
    }

    function renderGrid(data) {
        const grid = document.getElementById('mbGrid');
        const empty = document.getElementById('mbEmpty');
        const template = document.getElementById('grid-item-template');
        if (!grid || !template) return;

        grid.innerHTML = '';
        const items = data.items || [];
        if (!items.length) {
            grid.style.display = 'none';
            empty.replaceChildren(emptyContent(data.level));
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

    // The noun a level's cards are: the count and the empty state name it.
    const LEVEL_NOUNS = {
        publisher: ['publishers', 'publisher'], series: ['series', 'series'],
        decade: ['decades', 'decade'], year: ['years', 'year'], issue: ['issues', 'issue'],
    };

    function levelNoun(level) {
        return LEVEL_NOUNS[level] || ['items', 'item'];
    }

    function hasNarrowing() {
        return !!(state.search || state.letter || state.publisher || state.series ||
                  state.year_from != null || state.year_to != null);
    }

    // Nothing at all (an empty library) is not the same as a search or letter
    // that hid everything.
    function emptyContent(level) {
        if (!hasNarrowing()) {
            return CLU.emptyState({
                icon: 'bi-inbox', title: 'Nothing to show yet',
                message: 'Comics appear here once the library has files.',
            });
        }
        return CLU.noMatches({
            noun: levelNoun(level)[0],
            query: state.search,
            filtered: !!(state.letter || state.publisher || state.series ||
                         state.year_from != null || state.year_to != null),
            onClear: clearNarrowing,
        });
    }

    // "Clear search" empties the box; "Clear filters" also drops the letter
    // and the drill-down, back to the level the page opened on.
    function clearNarrowing() {
        const hadFilters = !!(state.letter || state.publisher || state.series ||
                              state.year_from != null || state.year_to != null);
        state.search = '';
        const box = document.getElementById('mbSearch');
        if (box) box.value = '';
        if (hadFilters) {
            state.axis = rootAxis();
            clearDrilldown();
            syncControlsFromState();
        }
        state.page = 1;
        reload();
    }

    // The server sends the total before the search box and the letter
    // narrowed it (all_total), so a search that finds 3 reads "Showing 3 of
    // 76 publishers", not "3 publishers". Asked for on every load rather than
    // remembered here, so files indexed since the page opened are counted.
    function renderCount(data) {
        const noun = levelNoun(data.level);
        const total = data.total || 0;
        const narrowed = !!(state.search || state.letter);
        const all = typeof data.all_total === 'number' ? data.all_total : null;
        const el = document.getElementById('mbCount');
        const onePage = total <= state.perPage;
        if (narrowed && all != null && onePage) {
            CLU.setCount(el, { total: all, noun: noun[0], one: noun[1], shown: total });
            return;
        }
        const first = total ? (state.page - 1) * state.perPage + 1 : 0;
        const last = total ? Math.min(total, first + (data.items || []).length - 1) : 0;
        CLU.setCount(el, { total, noun: noun[0], one: noun[1], first, last });
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
            state.letter = '';
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
                document.getElementById('editCBZModalText').textContent = `Editing ${filename}`;
                CLU.setupEditModalDropZone();
                fetch(`/edit?file_path=${encodeURIComponent(item.path)}`)
                    .then(r => r.ok ? r.json() : Promise.reject(new Error('Failed to load edit content.')))
                    .then(data => {
                        document.getElementById('editInlineContainer').innerHTML = data.modal_body;
                        document.getElementById('editInlineFolderName').value = data.folder_name;
                        document.getElementById('editInlineZipFilePath').value = data.zip_file_path;
                        document.getElementById('editInlineOriginalFilePath').value = data.original_file_path;
                        CLU.sortInlineEditCards();
                        CLU.initEditModalReorder();
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

    // The letters come from the server (`letters`: every first letter across
    // all pages), so the bar is the same on every page and a letter finds
    // every match, not just the ones on the page that happened to be loaded.
    function renderLetterFilter(serverLetters, level) {
        const container = document.getElementById('mbLetterFilter');
        const grp = container.querySelector('.btn-group');

        // The year levels have no letters, so a letter from an old URL there
        // means nothing: drop it rather than show it as active.
        if (level === 'year' || level === 'decade') {
            state.letter = '';
            container.style.display = 'none';
            return;
        }
        // CLU.letterBar keeps an active letter on the bar even when a search
        // or drill-down has emptied it, so it can still be switched off.
        if (!(serverLetters || []).length && !state.letter) {
            container.style.display = 'none';
            return;
        }
        container.style.display = '';
        CLU.letterBar(grp, {
            letters: serverLetters || [],
            active: state.letter,
            onChange: (letter) => {
                state.letter = letter;
                state.page = 1;
                reload();
            },
        });
    }

    function renderPagination() {
        const nav = document.getElementById('mbPagination');
        const ul = nav.querySelector('ul');
        const totalPages = Math.max(1, Math.ceil((state.currentTotal || 0) / state.perPage));
        // renderPageLinks empties the list itself, after noting which link
        // had focus; emptying it here first would lose that.
        if (totalPages <= 1) { nav.style.display = 'none'; ul.innerHTML = ''; return; }
        nav.style.display = '';

        // Shared with /reading-lists and /pull-list (static/js/clu-pager.js).
        CLU.renderPageLinks(ul, state.page, totalPages, (page) => {
            state.page = page; reload();
        });
    }

    // ------- Orchestration -------

    function renderBrowse(data) {
        state.currentTotal = data.total || 0;
        state.currentLevel = data.level;
        renderGrid(data);
        renderCount(data);
    }

    async function reload() {
        document.getElementById('mbLoading').style.display = 'block';
        writeStateToUrl();
        renderBreadcrumb();

        const browse = await fetchBrowse();

        document.getElementById('mbLoading').style.display = 'none';

        if (browse) {
            renderBrowse(browse);
            renderLetterFilter(browse.letters || [], browse.level);
            writeStateToUrl();
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
                state.letter = '';
                reload();
            });
        });
        const sort = document.getElementById('mbSort');
        if (sort) sort.addEventListener('change', () => {
            state.sort = sort.value; state.page = 1; reload();
        });
        const per = document.getElementById('mbPerPage');
        if (per) per.addEventListener('change', () => {
            state.perPage = parseInt(per.value, 10) || DEFAULT_PER_PAGE; state.page = 1;
            CLU.rememberPerPage(PER_PAGE_COOKIE, state.perPage);
            reload();
        });
        const refresh = document.getElementById('mbRefreshBtn');
        if (refresh) refresh.onclick = () => reload();

        const search = document.getElementById('mbSearch');
        if (search) {
            search.value = state.search;
            CLU.bindSearch(search, (value) => {
                state.search = value;
                state.page = 1;
                reload();
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
