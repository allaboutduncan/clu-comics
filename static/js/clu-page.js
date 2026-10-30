/*
 * Page header: on a phone, fold the secondary actions into the More menu.
 *
 * partials/page_header.html renders actions inline. Below the sm breakpoint
 * only the primary action (.btn-primary) and anything marked data-clu-keep
 * stay on the title line; every other direct child moves into the More menu
 * and comes back when the screen widens. The element itself moves, so its
 * id, data-bs-* attributes and event listeners all keep working. While it is
 * in the menu its btn classes are swapped for .dropdown-item.
 */
(function () {
    'use strict';

    var PHONE = window.matchMedia('(max-width: 575.98px)');

    function isSecondary(el) {
        if (el.classList.contains('clu-page-more')) return false;
        if (el.hasAttribute('data-clu-keep')) return false;
        if (el.classList.contains('btn-primary')) return false;
        // A dropdown or button group cannot live inside another menu.
        if (el.querySelector('.dropdown-toggle, .dropdown-menu')) return false;
        return el.matches('a, button');
    }

    function fold(bar) {
        var more = bar.querySelector('.clu-page-more');
        var menu = more && more.querySelector('[data-clu-more]');
        if (!menu) return;
        var moved = Array.prototype.filter.call(bar.children, isSecondary);
        if (!moved.length) return;
        var first = menu.firstElementChild;
        moved.forEach(function (el) {
            var btnClasses = Array.prototype.filter.call(el.classList, function (c) {
                return c === 'btn' || c.indexOf('btn-') === 0;
            });
            el.dataset.cluBtnClasses = btnClasses.join(' ');
            btnClasses.forEach(function (c) { el.classList.remove(c); });
            el.classList.add('dropdown-item');
            // Mark the spot so unfold puts it back in its original order.
            var mark = document.createComment('clu-folded');
            bar.insertBefore(mark, el);
            el._cluMark = mark;
            var li = document.createElement('li');
            li.setAttribute('data-clu-folded', '');
            li.appendChild(el);
            menu.insertBefore(li, first);
        });
        if (first) {
            var hr = document.createElement('li');
            hr.setAttribute('data-clu-folded', '');
            hr.innerHTML = '<hr class="dropdown-divider">';
            menu.insertBefore(hr, first);
        }
        more.classList.add('clu-page-more-folded');
    }

    function unfold(bar) {
        var more = bar.querySelector('.clu-page-more');
        if (!more) return;
        more.querySelectorAll('li[data-clu-folded]').forEach(function (li) {
            var el = li.firstElementChild;
            if (el && el.dataset.cluBtnClasses !== undefined) {
                el.classList.remove('dropdown-item');
                el.dataset.cluBtnClasses.split(' ').forEach(function (c) {
                    if (c) el.classList.add(c);
                });
                delete el.dataset.cluBtnClasses;
                var mark = el._cluMark;
                if (mark && mark.parentNode === bar) {
                    bar.replaceChild(el, mark);
                } else {
                    bar.insertBefore(el, more);
                }
                el._cluMark = null;
            }
            li.remove();
        });
        more.classList.remove('clu-page-more-folded');
    }

    function apply() {
        document.querySelectorAll('[data-clu-actions]').forEach(function (bar) {
            unfold(bar);
            if (PHONE.matches) fold(bar);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', apply);
    } else {
        apply();
    }
    if (PHONE.addEventListener) {
        PHONE.addEventListener('change', apply);
    } else if (PHONE.addListener) {
        PHONE.addListener(apply);
    }
})();

/*
 * CLU.emptyState({icon, title, message, compact, tone, actions}) -> element.
 * The JS twin of partials/empty_state.html, for empty states a page script
 * renders. Every value is set as text. actions: [{label, icon, onClick, href,
 * primary, id}] -- at most one primary; the rest are outline-secondary.
 *
 * CLU.noMatches({noun, query, filtered, onClear, compact}) -> element.
 * The one "a search or filter matched nothing" state, the twin of
 * no_matches() in empty_state.html. query is the search text; filtered says
 * another filter is narrowing the list too. onClear runs from its button
 * ("Clear search", or "Clear filters" when a filter is on) and should clear
 * every narrowing control the button names.
 */
(function () {
    'use strict';
    var CLU = window.CLU = window.CLU || {};
    CLU.emptyState = function (opts) {
        opts = opts || {};
        var root = document.createElement('div');
        root.className = 'clu-empty' + (opts.compact ? ' clu-empty-compact' : '') +
            (opts.tone ? ' clu-empty-' + opts.tone : '');
        if (opts.icon) {
            var i = document.createElement('i');
            i.className = 'bi ' + opts.icon + ' clu-empty-icon';
            i.setAttribute('aria-hidden', 'true');
            root.appendChild(i);
        }
        var h = document.createElement('h2');
        h.className = 'clu-empty-title';
        h.textContent = opts.title || '';
        root.appendChild(h);
        if (opts.message) {
            var p = document.createElement('p');
            p.className = 'clu-empty-text';
            p.textContent = opts.message;
            root.appendChild(p);
        }
        if (opts.actions && opts.actions.length) {
            var bar = document.createElement('div');
            bar.className = 'clu-empty-actions';
            opts.actions.forEach(function (a) {
                var b = document.createElement(a.href ? 'a' : 'button');
                if (a.href) b.href = a.href; else b.type = 'button';
                b.className = 'btn ' + (a.primary ? 'btn-primary' : 'btn-outline-secondary');
                if (a.id) b.id = a.id;
                if (a.icon) {
                    var ic = document.createElement('i');
                    ic.className = 'bi ' + a.icon + ' me-2';
                    ic.setAttribute('aria-hidden', 'true');
                    b.appendChild(ic);
                }
                b.appendChild(document.createTextNode(a.label));
                if (a.onClick) b.addEventListener('click', a.onClick);
                bar.appendChild(b);
            });
            root.appendChild(bar);
        }
        return root;
    };

    CLU.noMatchesText = function (o) {
        var opts = o || {};
        var q = (opts.query || '').trim();
        var noun = opts.noun || 'items';
        if (q && !opts.filtered) {
            return {
                title: 'No ' + noun + ' match \u201c' + q + '\u201d',
                message: 'Check the spelling, or search for part of a name.',
                clear: 'Clear search', icon: 'bi-search'
            };
        }
        return {
            title: 'No ' + noun + ' match these filters',
            message: q ? 'Nothing matches the search and the filters together.'
                       : 'Change or clear a filter to see more.',
            clear: 'Clear filters', icon: 'bi-funnel'
        };
    };

    CLU.noMatches = function (o) {
        var opts = o || {};
        var t = CLU.noMatchesText(opts);
        return CLU.emptyState({
            icon: t.icon, title: t.title, message: t.message, compact: opts.compact,
            actions: opts.onClear ? [{ label: t.clear, onClick: opts.onClear }] : []
        });
    };
})();

/*
 * Filter rows that scroll sideways on a phone (.clu-filter-scroll; the layout
 * is in clu-page.css). Marks the edges that have more to scroll to
 * (data-more-start / data-more-end, which fade that edge) and keeps the active
 * choice (.active or aria-pressed="true") in view: on load, so ?letter=T does
 * not open on a row showing A-F, and whenever a page script redraws the row,
 * which would otherwise jump it back to the start.
 */
(function () {
    'use strict';

    var ACTIVE = '.active, [aria-pressed="true"]';

    function edges(row) {
        var max = row.scrollWidth - row.clientWidth;
        var x = Math.abs(row.scrollLeft);
        row.toggleAttribute('data-more-start', max > 1 && x > 1);
        row.toggleAttribute('data-more-end', max > 1 && max - x > 1);
    }

    // scrollLeft only: scrollIntoView would also scroll the page vertically
    // to a row below the fold.
    function reveal(row) {
        if (row.scrollWidth <= row.clientWidth) return;
        var item = row.querySelector(ACTIVE);
        if (!item) return;
        var pad = parseFloat(getComputedStyle(row).scrollPaddingInlineStart) || 0;
        var r = row.getBoundingClientRect();
        var i = item.getBoundingClientRect();
        if (i.left < r.left + pad) {
            row.scrollLeft -= (r.left + pad) - i.left;
        } else if (i.right > r.right - pad) {
            row.scrollLeft += i.right - (r.right - pad);
        }
    }

    function watch(row) {
        if (row._cluFilterScroll) return;
        row._cluFilterScroll = true;
        var queued = false;
        function refresh() {
            if (queued) return;
            queued = true;
            requestAnimationFrame(function () {
                queued = false;
                reveal(row);
                edges(row);
            });
        }
        row.addEventListener('scroll', function () { edges(row); }, { passive: true });
        if (window.ResizeObserver) new ResizeObserver(refresh).observe(row);
        new MutationObserver(refresh).observe(row, {
            childList: true, subtree: true,
            attributes: true, attributeFilter: ['class', 'aria-pressed', 'hidden'],
        });
        refresh();
    }

    function init() {
        document.querySelectorAll('.clu-filter-scroll').forEach(watch);
    }

    // For rows a script builds after load (CLU.letterBar, CLU.filterBar).
    window.CLU = window.CLU || {};
    CLU.watchFilterScroll = function (row) {
        if (row && row.classList.contains('clu-filter-scroll')) watch(row);
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
