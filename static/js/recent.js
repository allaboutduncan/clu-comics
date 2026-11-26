/*
 * recent.js — controller for /recent (New Releases).
 *
 * Owned issues of mapped series, paged by the server and drawn here as one
 * section per store date. The toolbar's starting state is rendered by the
 * server (routes/collection.new_releases_page); every change after that goes
 * through CLU.createServerPager, which also keeps the URL shareable.
 */
(function () {
    'use strict';

    const DEFAULT_SORT = 'newest';

    let pager = null;
    let readIssues = new Set();

    const el = (id) => document.getElementById(id);

    // ------- Dates -------

    // 'YYYY-MM-DD' as a local date. new Date('2026-11-18') is UTC midnight,
    // which prints as the 17th anywhere west of Greenwich.
    function parseStoreDate(storeDate) {
        const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(storeDate || '');
        return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
    }

    function dateHeading(storeDate) {
        const date = parseStoreDate(storeDate);
        if (!date) return storeDate || 'Unknown date';
        return date.toLocaleDateString(undefined, {
            weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
        });
    }

    // ------- Issue numbers (mirrors stripIssueZeros in collection.js) -------

    function stripIssueZeros(num) {
        const m = String(num).match(/^(-?)0*(\d+)(\..*)?$/);
        return m ? m[1] + m[2] + (m[3] || '') : String(num);
    }

    function issueNumber(item) {
        const raw = (item.issue_number == null ? '' : String(item.issue_number)).trim();
        return raw ? stripIssueZeros(raw) : '';
    }

    // ------- Read state -------

    // The one flip, used by the first draw, the back-fill and the reader.
    function applyReadState(card, isRead) {
        const icon = card.querySelector('.read-icon');
        if (!icon) return;
        icon.classList.toggle('bi-book-fill', isRead);
        icon.classList.toggle('bi-book', !isRead);
    }

    // Iterates the cards and asks the Set: the Set can hold tens of thousands
    // of paths, the page at most a few hundred cards.
    function applyReadIcons() {
        document.querySelectorAll('#recentGroups .grid-item[data-path]').forEach(card => {
            applyReadState(card, readIssues.has(card.getAttribute('data-path')));
        });
    }

    function loadReadIssues() {
        fetch('/api/issues-read-paths')
            .then(r => (r.ok ? r.json() : { paths: [] }))
            .then(d => {
                readIssues = new Set(d.paths || []);
                // Published here, not at parse time: assigning a new Set
                // rebinds the variable, and reader.js must hold this one.
                window._readerReadIssuesSet = readIssues;
                applyReadIcons();
            })
            .catch(() => {
                window._readerReadIssuesSet = readIssues;
            });
    }

    // ------- Cards -------

    function buildCard(item, template) {
        const clone = template.content.cloneNode(true);
        const card = clone.querySelector('.grid-item');
        card.setAttribute('data-path', item.file_path);

        const number = issueNumber(item);
        const label = number ? `${item.series_name} #${number}` : item.series_name;
        const nameEl = clone.querySelector('.item-name');
        nameEl.textContent = label;
        nameEl.title = item.issue_name ? `${label}: ${item.issue_name}` : label;

        clone.querySelector('.item-meta').textContent =
            [item.issue_name, item.publisher_name].filter(Boolean).join(' · ');

        const img = clone.querySelector('.thumbnail');
        img.alt = label;
        img.onerror = function () { this.onerror = null; this.src = CLU.staticUrl('images/error.svg'); };
        if (item.thumbnail_url) img.src = item.thumbnail_url;

        // Always shown for a comic: the read icon lives inside the badge, so
        // hiding the badge for an unnumbered issue would hide read status too.
        const badge = clone.querySelector('.issue-badge');
        badge.style.display = '';
        if (number) {
            badge.querySelector('.issue-number').textContent = '#' + number;
        } else {
            badge.classList.add('issue-badge-unnumbered');
        }
        applyReadState(card, readIssues.has(item.file_path));

        card.tabIndex = 0;
        card.setAttribute('role', 'button');
        card.setAttribute('aria-label', `Read ${label}`);
        const open = () => {
            if (typeof window.openComicReader === 'function') {
                window.openComicReader(item.file_path);
            }
        };
        card.addEventListener('click', open);
        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });
        return clone;
    }

    // ------- Groups -------

    // The server orders by store date, so issues of one date are adjacent:
    // a new section starts whenever the date changes.
    function renderGroups(items) {
        const root = el('recentGroups');
        const template = el('recent-item-template');
        root.replaceChildren();

        let section = null;
        let grid = null;
        let count = null;
        let current = null;
        let inGroup = 0;
        const closeGroup = () => {
            if (count) count.textContent = inGroup === 1 ? '1 issue' : `${inGroup} issues`;
        };

        items.forEach((item, index) => {
            if (section === null || item.store_date !== current) {
                closeGroup();
                current = item.store_date;
                inGroup = 0;

                section = document.createElement('section');
                section.className = 'recent-date-group mb-4';
                const headingId = `recentDate${index}`;
                section.setAttribute('aria-labelledby', headingId);

                const header = document.createElement('div');
                header.className = 'd-flex align-items-baseline justify-content-between gap-2 border-bottom pb-2 mb-3';
                const heading = document.createElement('h2');
                heading.className = 'h5 m-0';
                heading.id = headingId;
                const icon = document.createElement('i');
                icon.className = 'bi bi-calendar-event me-2';
                icon.setAttribute('aria-hidden', 'true');
                heading.append(icon, dateHeading(item.store_date));
                count = document.createElement('span');
                count.className = 'small text-body-secondary';
                header.append(heading, count);

                grid = document.createElement('div');
                grid.className = 'file-grid';
                section.append(header, grid);
                root.appendChild(section);
            }
            grid.appendChild(buildCard(item, template));
            inGroup += 1;
        });
        closeGroup();
    }

    // ------- State -------

    function narrowing() {
        return {
            q: el('recentSearch').value.trim(),
            publisher: el('recentPublisher').value,
            year: el('recentYear').value,
        };
    }

    function hasNarrowing() {
        const n = narrowing();
        return !!(n.q || n.publisher || n.year);
    }

    function clearNarrowing() {
        el('recentSearch').value = '';
        el('recentPublisher').value = '';
        el('recentYear').value = '';
        pager.load({ resetPage: true });
    }

    function emptyContent() {
        if (!hasNarrowing()) {
            return CLU.emptyState({
                icon: 'bi-calendar-event', title: 'No new releases yet',
                message: 'Issues appear here once a series is mapped to a folder and its files are matched.',
            });
        }
        const n = narrowing();
        return CLU.noMatches({
            noun: 'issues', query: n.q, filtered: !!(n.publisher || n.year),
            onClear: clearNarrowing,
        });
    }

    function onData(data) {
        el('recentLoading').style.display = 'none';
        const items = data.items || [];
        const total = data.total || 0;
        const empty = el('recentEmpty');
        const groups = el('recentGroups');

        // reader.js walks this for the next-issue prompt.
        window._readerAllItems = items.map(it => ({
            type: 'file', name: it.file_name, path: it.file_path,
        }));

        if (!items.length) {
            groups.replaceChildren();
            empty.replaceChildren(emptyContent());
            empty.style.display = 'block';
        } else {
            empty.style.display = 'none';
            renderGroups(items);
        }

        const first = total ? (data.page - 1) * data.per_page + 1 : 0;
        const last = total ? first + items.length - 1 : 0;
        CLU.setCount(el('recentCount'), {
            total, noun: 'issues', one: 'issue', first, last,
        });
    }

    function onError(err) {
        el('recentLoading').style.display = 'none';
        console.error('New releases load failed:', err);
        CLU.showError('Failed to load new releases.');
    }

    // ------- Boot -------

    document.addEventListener('DOMContentLoaded', () => {
        const groups = el('recentGroups');
        const select = el('recentPerPage');
        const sort = el('recentSort');

        pager = CLU.createServerPager({
            endpoint: '/api/new-releases/page',
            nav: el('recentPagination'),
            perPageSelect: select,
            defaultPerPage: CLU.defaultPerPage(select),
            rememberAs: groups.getAttribute('data-per-page-cookie'),
            initial: {
                page: parseInt(groups.getAttribute('data-page'), 10) || 1,
                perPage: parseInt(select.value, 10),
            },
            // The default sort stays out of the URL.
            state: () => Object.assign(narrowing(), {
                sort: sort.value === DEFAULT_SORT ? '' : sort.value,
            }),
            scrollTarget: el('recentToolbar'),
            onData,
            onError,
        });

        const reload = () => pager.load({ resetPage: true });
        CLU.bindSearch(el('recentSearch'), reload);
        el('recentPublisher').addEventListener('change', reload);
        el('recentYear').addEventListener('change', reload);
        sort.addEventListener('change', reload);
        el('recentRefreshBtn').addEventListener('click', () => pager.load());

        // reader.js calls this when an issue is finished, so the icon on
        // screen flips without a reload.
        window._readerOnMarkedRead = (path) => {
            readIssues.add(path);
            const card = document.querySelector(
                `#recentGroups .grid-item[data-path="${CSS.escape(path)}"]`);
            if (card) applyReadState(card, true);
        };

        loadReadIssues();
        pager.load();
    });
})();
