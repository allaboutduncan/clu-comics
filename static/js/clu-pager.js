/**
 * clu-pager.js — the page bar /library uses, shared.
 *
 * Provides:
 *   CLU.renderPageLinks(ul, page, totalPages, onGo)
 *       Fills a Bootstrap `.pagination` <ul> with « 1 … 4 5 6 … 20 ».
 *       /library (metadata_browser.js) calls this directly, so every pager in
 *       the app looks the same.
 *
 *   CLU.createServerPager(opts)
 *       Pages a list the server filters, sorts and pages (/pull-list,
 *       /reading-lists). The page is rendered with its first page already in
 *       place; every later filter, sort or page change fetches one page from
 *       `endpoint`, which answers {success, html, total, page, per_page,
 *       total_pages}. Only that page ever reaches the browser, so selection
 *       across pages is the page's own job (a Set of ids, plus an ids
 *       endpoint for "select all matching").
 *
 *       opts:
 *         endpoint       URL of the JSON page endpoint (from url_for)
 *         nav            the <nav> holding a `.pagination` <ul>
 *         perPageSelect  optional <select> of page sizes
 *         scrollTarget   optional element scrolled into view on page change
 *         defaultPerPage default 50, as on /library
 *         initial        {page, perPage, totalPages} of the server-rendered page
 *         state()        -> {key: value} of the page's own sort and filter
 *                        state, sent to the endpoint and written to the query
 *                        string next to `page` (an empty value or [] leaves
 *                        the key out, an array sends the key once per value)
 *         onData(data)   called with the endpoint's answer after every load
 *         onError(err)   optional, called when a load fails. The pager
 *                        has already put its page, size and bar back to the
 *                        rows still on screen; a filter the page changed
 *                        before calling load() is the page's to flag.
 *
 *       returns { load({resetPage}), goTo(page), page(), query() }
 *
 *       A newer load aborts an older one still in flight, so a fast typist
 *       never sees an old answer land last. `page` and `per_page` live in the
 *       query string, as on /library, so reload and back keep your place.
 *       The URL is only rewritten when it changes, and at most every
 *       URL_WRITE_MS: Safari throws SecurityError after 100 replaceState
 *       calls in 10s.
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

  CLU.createServerPager = function (opts) {
    const defaultPerPage = opts.defaultPerPage || 50;
    const nav = opts.nav || null;
    const ul = nav ? nav.querySelector('.pagination') : null;
    const select = opts.perPageSelect || null;
    const initial = opts.initial || {};

    let page = initial.page || 1;
    let perPage = initial.perPage || defaultPerPage;
    let totalPages = initial.totalPages || 1;

    if (select) {
      select.value = String(perPage);
      select.addEventListener('change', () => {
        perPage = parseInt(select.value, 10) || defaultPerPage;
        load({ resetPage: true });
      });
    }

    // The page's own filters, as query parameters (no page/per_page).
    function stateParams() {
      const p = new URLSearchParams();
      const state = opts.state ? (opts.state() || {}) : {};
      Object.keys(state).forEach(key => {
        const value = state[key];
        if (Array.isArray(value)) {
          value.forEach(v => p.append(key, String(v)));
        } else if (value !== undefined && value !== null && value !== '') {
          p.set(key, String(value));
        }
      });
      return p;
    }

    function pageParams() {
      const p = stateParams();
      if (page !== 1) p.set('page', String(page));
      if (perPage !== defaultPerPage) p.set('per_page', String(perPage));
      return p;
    }

    // The address bar keeps any parameter the pager does not own; only the
    // page's state keys and page/per_page are rewritten.
    function buildUrl() {
      const p = new URLSearchParams(window.location.search);
      const own = Object.keys(opts.state ? (opts.state() || {}) : {}).concat(['page', 'per_page']);
      own.forEach(key => p.delete(key));
      pageParams().forEach((value, key) => p.append(key, value));
      const qs = p.toString();
      return window.location.pathname + (qs ? '?' + qs : '') + window.location.hash;
    }

    // Skip a write that changes nothing; coalesce the rest so a burst of
    // loads makes one replaceState per window. Compared order-blind, so
    // ?page=3&q=x and ?q=x&page=3 are the same URL.
    function canonical(url) {
      const q = url.indexOf('?');
      if (q === -1) return url;
      const h = url.indexOf('#', q);
      const search = h === -1 ? url.slice(q + 1) : url.slice(q + 1, h);
      const p = new URLSearchParams(search);
      p.sort();
      return url.slice(0, q) + '?' + p.toString() + (h === -1 ? '' : url.slice(h));
    }

    let lastUrl = canonical(window.location.pathname + window.location.search + window.location.hash);
    let lastWriteAt = 0;
    let pendingWrite = null;

    function flushUrl() {
      pendingWrite = null;
      const url = buildUrl();
      if (canonical(url) === lastUrl) return;
      lastUrl = canonical(url);
      lastWriteAt = Date.now();
      window.history.replaceState(null, '', url);
    }

    function writeUrl() {
      if (pendingWrite) return;
      if (canonical(buildUrl()) === lastUrl) return;
      const wait = URL_WRITE_MS - (Date.now() - lastWriteAt);
      if (wait <= 0) flushUrl();
      else pendingWrite = setTimeout(flushUrl, wait);
    }

    function drawBar() {
      if (!nav || !ul) return;
      if (totalPages <= 1) {
        nav.style.display = 'none';
        ul.innerHTML = '';
      } else {
        nav.style.display = '';
        CLU.renderPageLinks(ul, page, totalPages, goTo);
      }
    }

    let inFlight = null;
    // Which load is current. Not inFlight: that is null both after a load
    // lands and where AbortController is missing.
    let loadSeq = 0;

    // What the rows on screen are: page, size and bar of the last load that
    // landed. A failed load puts these back, so the bar and the URL never
    // describe rows that were not drawn (the page's own filter controls are
    // the page's to reconcile; its onError is told).
    let shown = { page, perPage, totalPages };

    function rollback() {
      page = shown.page;
      perPage = shown.perPage;
      totalPages = shown.totalPages;
      if (select) select.value = String(perPage);
      drawBar();
    }

    function load(options) {
      if (options && options.resetPage) page = 1;
      const seq = ++loadSeq;
      if (inFlight) inFlight.abort();
      const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      inFlight = ctrl;
      const qs = pageParams().toString();
      return fetch(opts.endpoint + (qs ? '?' + qs : ''), {
        headers: { Accept: 'application/json' },
        signal: ctrl ? ctrl.signal : undefined,
      })
        .then(r => {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(data => {
          if (seq !== loadSeq) return null;
          if (!data || !data.success) throw new Error((data && data.error) || 'Failed to load');
          inFlight = null;
          // The server clamps a page past the end; follow it.
          page = data.page || 1;
          perPage = data.per_page || perPage;
          totalPages = data.total_pages || 1;
          shown = { page, perPage, totalPages };
          drawBar();
          writeUrl();
          opts.onData(data);
          return data;
        })
        .catch(err => {
          if (err && err.name === 'AbortError') return null;
          // A newer load replaced this one: it owns the state now.
          if (seq !== loadSeq) return null;
          inFlight = null;
          rollback();
          if (opts.onError) opts.onError(err);
          else console.error('Pager load failed:', err);
          return null;
        });
    }

    function goTo(target) {
      page = target;
      return load().then(data => {
        if (data && opts.scrollTarget) {
          opts.scrollTarget.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    }

    drawBar();
    // The server clamps ?page=99 to the last page; say so in the URL too.
    writeUrl();

    return {
      load,
      goTo,
      page: () => page,
      query: () => stateParams().toString(),
    };
  };
})();
