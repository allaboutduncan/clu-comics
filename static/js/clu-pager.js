/**
 * clu-pager.js — the page bar /library uses, shared.
 *
 * Provides:
 *   CLU.renderPageLinks(ul, page, totalPages, onGo)
 *       Fills a Bootstrap `.pagination` <ul> with « 1 … 4 5 6 … 20 ».
 *       /library (metadata_browser.js) pages on the server and calls this
 *       directly, so every pager in the app looks the same.
 *
 *   CLU.createClientPager(opts)
 *       Pages a list that is already fully in the DOM and filtered/sorted in
 *       the browser (/reading-lists, /pull-list). Filtering and paging are
 *       kept apart: the page's own filter code marks each item with
 *       `data-match="0"` when it does not match, and the pager alone sets
 *       `style.display` -- shown only when it matches AND is on this page.
 *
 *       So `style.display` means "on this page", NOT "matches the filter".
 *       Selection code (select-all, bulk actions) must work on the filtered
 *       set, `pager.matching()`, and use `pager.isOnPage(el)` to tell the
 *       user about selected items they cannot currently see.
 *
 *       opts:
 *         items()        -> the pageable elements, in current DOM order
 *         nav            the <nav> holding a `.pagination` <ul>
 *         perPageSelect  optional <select> of page sizes
 *         scrollTarget   optional element scrolled into view on page change
 *         defaultPerPage default 50, as on /library
 *         urlState()     optional, -> {key: value} of the page's own sort and
 *                        filter state, written to the query string next to
 *                        `page` (an empty value or [] removes the key, an
 *                        array writes the key once per value). The page
 *                        restores it before the first refresh(), so a page
 *                        number is never restored without the sort and
 *                        filters it was taken from.
 *         onRender(info) optional, called with {page, totalPages, total,
 *                        perPage, start, end} after every render
 *
 *       returns { refresh({resetPage}), goTo(page), matching(), isOnPage(el) }
 *
 *       `page` and `per_page` live in the query string, as on /library, so
 *       reload and back keep your place. The URL is only rewritten when it
 *       changes, and at most every URL_WRITE_MS: Safari throws SecurityError
 *       after 100 replaceState calls in 10s, and a render runs per keystroke.
 */
(function () {
  'use strict';

  window.CLU = window.CLU || {};

  CLU.renderPageLinks = function (ul, page, totalPages, onGo) {
    ul.innerHTML = '';
    const addItem = (label, target, disabled, active) => {
      const li = document.createElement('li');
      li.className = 'page-item' + (disabled ? ' disabled' : '') + (active ? ' active' : '');
      const a = document.createElement('a');
      a.className = 'page-link';
      a.href = '#';
      a.textContent = label;
      a.onclick = (e) => {
        e.preventDefault();
        if (disabled || active) return;
        onGo(target);
      };
      li.appendChild(a);
      ul.appendChild(li);
    };

    addItem('«', Math.max(1, page - 1), page === 1, false);
    const span = 2;
    const pages = new Set([1, totalPages, page]);
    for (let i = 1; i <= span; i++) {
      if (page - i >= 1) pages.add(page - i);
      if (page + i <= totalPages) pages.add(page + i);
    }
    let prev = 0;
    Array.from(pages).sort((a, b) => a - b).forEach(p => {
      if (p - prev > 1) addItem('…', 0, true, false);
      addItem(String(p), p, false, p === page);
      prev = p;
    });
    addItem('»', Math.min(totalPages, page + 1), page === totalPages, false);
  };

  const URL_WRITE_MS = 250;

  function readInt(params, key, fallback) {
    const n = parseInt(params.get(key) || '', 10);
    return n > 0 ? n : fallback;
  }

  CLU.createClientPager = function (opts) {
    const defaultPerPage = opts.defaultPerPage || 50;
    const nav = opts.nav;
    const ul = nav ? nav.querySelector('.pagination') : null;
    const select = opts.perPageSelect || null;

    const params = new URLSearchParams(window.location.search);
    let page = readInt(params, 'page', 1);
    let perPage = readInt(params, 'per_page', defaultPerPage);

    if (select) {
      // A per_page from the URL that is not one of the options falls back
      // to the default.
      if (!Array.from(select.options).some(o => o.value === String(perPage))) {
        perPage = defaultPerPage;
      }
      select.value = String(perPage);
      select.addEventListener('change', () => {
        perPage = parseInt(select.value, 10) || defaultPerPage;
        page = 1;
        render();
      });
    }

    function buildUrl() {
      const p = new URLSearchParams(window.location.search);
      if (page !== 1) p.set('page', String(page)); else p.delete('page');
      if (perPage !== defaultPerPage) p.set('per_page', String(perPage)); else p.delete('per_page');
      const state = opts.urlState ? (opts.urlState() || {}) : {};
      Object.keys(state).forEach(key => {
        const value = state[key];
        p.delete(key);
        if (Array.isArray(value)) {
          value.forEach(v => p.append(key, String(v)));
        } else if (value !== undefined && value !== null && value !== '') {
          p.set(key, String(value));
        }
      });
      const qs = p.toString();
      return window.location.pathname + (qs ? '?' + qs : '') + window.location.hash;
    }

    // Skip a write that changes nothing; coalesce the rest so a burst of
    // renders (typing in a search box) makes one replaceState per window.
    let lastUrl = window.location.pathname + window.location.search + window.location.hash;
    let lastWriteAt = 0;
    let pendingWrite = null;

    function flushUrl() {
      pendingWrite = null;
      const url = buildUrl();
      if (url === lastUrl) return;
      lastUrl = url;
      lastWriteAt = Date.now();
      window.history.replaceState(null, '', url);
    }

    function writeUrl() {
      if (pendingWrite) return;
      if (buildUrl() === lastUrl) return;
      const wait = URL_WRITE_MS - (Date.now() - lastWriteAt);
      if (wait <= 0) flushUrl();
      else pendingWrite = setTimeout(flushUrl, wait);
    }

    function matching() {
      return opts.items().filter(el => el.dataset.match !== '0');
    }

    let onPage = new Set();

    function render() {
      const all = opts.items();
      const matched = all.filter(el => el.dataset.match !== '0');
      const total = matched.length;
      const totalPages = Math.max(1, Math.ceil(total / perPage));
      if (page > totalPages) page = totalPages;
      const start = (page - 1) * perPage;
      const end = Math.min(total, start + perPage);

      onPage = new Set(matched.slice(start, end));
      all.forEach(el => { el.style.display = onPage.has(el) ? '' : 'none'; });

      if (nav && ul) {
        if (totalPages <= 1) {
          nav.style.display = 'none';
          ul.innerHTML = '';
        } else {
          nav.style.display = '';
          CLU.renderPageLinks(ul, page, totalPages, goTo);
        }
      }
      writeUrl();
      if (opts.onRender) {
        opts.onRender({ page, totalPages, total, perPage, start, end });
      }
    }

    function goTo(target) {
      page = target;
      render();
      if (opts.scrollTarget) {
        opts.scrollTarget.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }

    function refresh(options) {
      if (options && options.resetPage) page = 1;
      render();
    }

    return { refresh, goTo, matching, isOnPage: el => onPage.has(el) };
  };
})();
