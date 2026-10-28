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
 * CLU.emptyState({icon, title, message, compact}) -> element.
 * The JS twin of partials/empty_state.html, for empty states a page script
 * renders. Every value is set as text.
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
        return root;
    };
})();
