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
 *       Code that asks "is this row on screen?" can keep testing display.
 *
 *       opts:
 *         items()        -> the pageable elements, in current DOM order
 *         nav            the <nav> holding a `.pagination` <ul>
 *         perPageSelect  optional <select> of page sizes
 *         scrollTarget   optional element scrolled into view on page change
 *         defaultPerPage default 50, as on /library
 *         onRender(info) optional, called with {page, totalPages, total,
 *                        perPage, start, end} after every render
 *
 *       returns { refresh({resetPage}), goTo(page) }
 *
 *       `page` and `per_page` live in the query string, as on /library, so
 *       reload and back keep your place.
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
      // Keep a per_page from the URL that is not one of the options.
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

    function writeUrl() {
      const p = new URLSearchParams(window.location.search);
      if (page !== 1) p.set('page', String(page)); else p.delete('page');
      if (perPage !== defaultPerPage) p.set('per_page', String(perPage)); else p.delete('per_page');
      const qs = p.toString();
      const url = window.location.pathname + (qs ? '?' + qs : '') + window.location.hash;
      window.history.replaceState(null, '', url);
    }

    function render() {
      const all = opts.items();
      const matching = all.filter(el => el.dataset.match !== '0');
      const total = matching.length;
      const totalPages = Math.max(1, Math.ceil(total / perPage));
      if (page > totalPages) page = totalPages;
      const start = (page - 1) * perPage;
      const end = Math.min(total, start + perPage);

      const onPage = new Set(matching.slice(start, end));
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

    return { refresh, goTo };
  };
})();
