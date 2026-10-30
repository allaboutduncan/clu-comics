/**
 * clu-filters.js: the list toolbar's behaviour, shared. base.html loads it on
 * every page. The markup twin is templates/partials/filters.html.
 *
 *   CLU.filterBar(group, {onChange, multi, toggleOff})
 *       Wires a row of toggle buttons (role="group", one button per
 *       data-value). Single choice by default; data-clu-filter="multi" or
 *       multi:true makes it multi-select, where '' (All) clears the others.
 *       data-clu-toggle-off or toggleOff:true sends a second click on the
 *       selected button back to ''. The buttons carry .active and
 *       aria-pressed. onChange(value, button) gets a string, or an array for
 *       a multi group ([] = All). Calling it again on the same group only
 *       swaps the options, so a page can re-render the buttons freely.
 *
 *   CLU.renderFilterOptions(group, options, active)
 *       Draws filter_bar's buttons from a script. options:
 *       [{value, label, icon, iconClass, count, disabled}].
 *
 *   CLU.letterBar(group, {letters, active, onChange, allLabel})
 *       Draws and wires an A-Z bar: All, '#', then A-Z, as one btn-group.
 *       letters is any iterable of 'A'..'Z' / '#'. A selected letter that is
 *       not in `letters` stays on the bar so it can be switched off. Keyboard
 *       focus stays on the same letter across a redraw.
 *
 *   CLU.setFilterValue(group, value) / CLU.filterValue(group)
 *       Mark the selection without firing onChange / read it back.
 *
 *   CLU.bindSearch(input, fn, {delay})
 *       Calls fn(value) as the user types: after `delay` ms of quiet
 *       (default 250, for a search the server answers) or at once with
 *       delay 0 (a filter over rows already on the page). Enter runs it
 *       immediately. A value it already ran is skipped; a page may empty
 *       the box with input.value = '' and refresh itself.
 *
 *   CLU.formatCount(n)
 *       4132 -> "4,132", matching the server's '{:,}'.format().
 *
 *   CLU.countText({total, noun, one, shown, first, last}) / CLU.setCount(el, opts)
 *       A result count, worded as result_count() words it: "4,132 series",
 *       "Showing 12 of 245 publishers" (shown), "Showing 1–60 of 4,132
 *       series" (first/last, a paged list). one is the singular noun.
 *
 *   CLU.searchInput({id, label, value})
 *       Builds search_input's markup from a script; returns the wrapper.
 *       label is "Search <what it matches>"; the placeholder is the label
 *       plus an ellipsis, always.
 *
 *   CLU.setSearchLabel(input, label)
 *       Renames a search box when the list under it changes (Collection's
 *       views): the accessible name and the placeholder together.
 *
 *   Every input.form-control[type="search"] in the app clears on Escape
 *   (and fires `input`, so whatever listens refilters). A non-empty box eats
 *   the Escape, so clearing a search inside a modal does not also close it.
 */
(function () {
    'use strict';

    window.CLU = window.CLU || {};

    function buttons(group) {
        return Array.prototype.filter.call(group.querySelectorAll('[data-value]'),
            function (b) { return b.closest('[role="group"]') === group; });
    }

    function isMulti(group) {
        return group.getAttribute('data-clu-filter') === 'multi';
    }

    function mark(group, values) {
        var set = values.length ? values : [''];
        buttons(group).forEach(function (b) {
            var on = set.indexOf(b.getAttribute('data-value')) !== -1;
            b.classList.toggle('active', on);
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
    }

    // Counts read 4,132 everywhere: the same grouping the server's
    // '{:,}'.format() gives the page header, whatever the browser's locale.
    CLU.formatCount = function (n) {
        var v = Number(n);
        return isFinite(v) ? v.toLocaleString('en-US') : String(n);
    };

    CLU.countText = function (o) {
        var opts = o || {};
        var total = Number(opts.total) || 0;
        var noun = opts.noun || '';
        var name = total === 1 && opts.one ? opts.one : noun;
        var f = CLU.formatCount;
        var paged = opts.first != null && opts.last != null && total > 0 &&
            !(Number(opts.first) === 1 && Number(opts.last) === total);
        if (paged) {
            return 'Showing ' + f(opts.first) + '\u2013' + f(opts.last) + ' of ' + f(total) + ' ' + name;
        }
        if (opts.shown != null && Number(opts.shown) !== total) {
            return 'Showing ' + f(opts.shown) + ' of ' + f(total) + ' ' + name;
        }
        return f(total) + ' ' + name;
    };

    CLU.setCount = function (el, opts) {
        if (el) el.textContent = CLU.countText(opts);
    };

    CLU.filterValue = function (group) {
        var on = buttons(group)
            .filter(function (b) { return b.classList.contains('active'); })
            .map(function (b) { return b.getAttribute('data-value'); });
        if (isMulti(group)) return on.filter(function (v) { return v !== ''; });
        return on.length ? on[0] : '';
    };

    CLU.setFilterValue = function (group, value) {
        if (!group) return;
        var values = Array.isArray(value) ? value.map(String) : [String(value == null ? '' : value)];
        values = values.filter(function (v) { return v !== ''; });
        mark(group, values);
    };

    CLU.filterBar = function (group, opts) {
        if (!group) return;
        var o = opts || {};
        if (o.multi) group.setAttribute('data-clu-filter', 'multi');
        else if (!group.hasAttribute('data-clu-filter')) group.setAttribute('data-clu-filter', 'single');
        if (o.toggleOff) group.setAttribute('data-clu-toggle-off', '');
        group._cluFilterOpts = o;
        if (CLU.watchFilterScroll) CLU.watchFilterScroll(group);
        if (group._cluFilterWired) return;
        group._cluFilterWired = true;

        group.addEventListener('click', function (e) {
            var btn = e.target.closest('[data-value]');
            if (!btn || btn.disabled || btn.closest('[role="group"]') !== group) return;
            var value = btn.getAttribute('data-value');
            var cur = CLU.filterValue(group);
            var next;
            if (isMulti(group)) {
                if (value === '') {
                    next = [];
                } else if (cur.indexOf(value) !== -1) {
                    next = cur.filter(function (v) { return v !== value; });
                } else {
                    next = cur.concat([value]);
                }
                mark(group, next);
            } else {
                if (value === cur) {
                    if (!group.hasAttribute('data-clu-toggle-off') || value === '') return;
                    value = '';
                }
                next = value;
                mark(group, value === '' ? [] : [value]);
            }
            var cb = group._cluFilterOpts && group._cluFilterOpts.onChange;
            if (cb) cb(next, btn);
        });
    };

    function makeButton(opt, on, small) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn ' + (small ? 'btn-sm ' : '') + 'btn-outline-secondary' + (on ? ' active' : '');
        b.setAttribute('data-value', String(opt.value == null ? '' : opt.value));
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        if (opt.icon) {
            var i = document.createElement('i');
            i.className = 'bi ' + opt.icon + (opt.iconClass ? ' ' + opt.iconClass : '') + ' me-1';
            i.setAttribute('aria-hidden', 'true');
            b.appendChild(i);
        }
        b.appendChild(document.createTextNode(String(opt.label)));
        if (opt.count !== undefined && opt.count !== null && opt.count !== '') {
            var c = document.createElement('span');
            c.className = 'clu-filter-count';
            c.textContent = typeof opt.count === 'number' ? CLU.formatCount(opt.count) : String(opt.count);
            b.appendChild(c);
        }
        if (opt.disabled && !on) b.disabled = true;
        if (opt.title) b.title = opt.title;
        return b;
    }

    // Keyboard focus survives a redraw: remember the focused value, put focus
    // back on the same one (or the selected one) afterwards.
    function redraw(group, draw) {
        var focused = document.activeElement;
        var key = (focused && group.contains(focused) && focused.hasAttribute('data-value'))
            ? focused.getAttribute('data-value') : null;
        group.innerHTML = '';
        draw();
        if (key === null) return;
        var target = buttons(group).filter(function (b) {
            return b.getAttribute('data-value') === key && !b.disabled;
        })[0] || group.querySelector('.active');
        if (target) target.focus({ preventScroll: true });
    }

    CLU.renderFilterOptions = function (group, options, active) {
        if (!group) return;
        var values = Array.isArray(active) ? active.map(String) : [String(active == null ? '' : active)];
        values = values.filter(function (v) { return v !== ''; });
        if (!values.length) values = [''];
        var needBtnSm = !group.classList.contains('btn-group-sm');
        redraw(group, function () {
            (options || []).forEach(function (opt) {
                var v = String(opt.value == null ? '' : opt.value);
                group.appendChild(makeButton(opt, values.indexOf(v) !== -1, needBtnSm));
            });
        });
    };

    // '#' first, then A-Z: the order every letter bar uses.
    CLU.sortLetters = function (letters) {
        var seen = {};
        var out = [];
        Array.from(letters || []).forEach(function (l) {
            var k = String(l).toUpperCase();
            if (k !== '#' && !/^[A-Z]$/.test(k)) k = '#';
            if (!seen[k]) { seen[k] = true; out.push(k); }
        });
        return out.sort(function (a, b) {
            if (a === '#') return -1;
            if (b === '#') return 1;
            return a < b ? -1 : a > b ? 1 : 0;
        });
    };

    CLU.letterBar = function (group, opts) {
        if (!group) return;
        var o = opts || {};
        var active = o.active ? String(o.active).toUpperCase() : '';
        var letters = CLU.sortLetters(o.letters);
        if (active && letters.indexOf(active) === -1) letters = CLU.sortLetters(letters.concat([active]));

        group.classList.add('btn-group', 'btn-group-sm', 'clu-letter-bar', 'clu-filter-scroll');
        group.setAttribute('role', 'group');
        if (!group.hasAttribute('aria-label')) group.setAttribute('aria-label', 'Filter by first letter');

        redraw(group, function () {
            group.appendChild(makeButton({ value: '', label: o.allLabel || 'All' }, !active, false));
            letters.forEach(function (l) {
                group.appendChild(makeButton({ value: l, label: l }, l === active, false));
            });
        });
        CLU.filterBar(group, { onChange: o.onChange, toggleOff: true });
    };

    // ---- Search boxes -------------------------------------------------------

    CLU.bindSearch = function (input, fn, opts) {
        if (!input || !fn) return;
        var delay = opts && typeof opts.delay === 'number' ? opts.delay : 250;
        var timer = null;
        var last = null;
        function run() {
            clearTimeout(timer);
            timer = null;
            var v = input.value.trim();
            if (v === last) return;
            last = v;
            fn(v);
        }
        last = input.value.trim();
        // A page's Clear button sets input.value directly, which fires no
        // event, so `last` would keep the old query and typing it again would
        // be skipped. With nothing pending, the box should hold what last ran:
        // anything else was set by code, and what the page now shows is
        // unknown, so the next query runs whatever it is.
        input.addEventListener('beforeinput', function () {
            if (timer === null && input.value.trim() !== last) last = null;
        });
        input.addEventListener('input', function (e) {
            // An input event dispatched by code is a request to search now,
            // even for the query that last ran: the page may have reset its
            // own state (a modal reopened on its browse list) under it.
            if (e && e.isTrusted === false) { last = null; run(); return; }
            if (delay <= 0) { run(); return; }
            clearTimeout(timer);
            timer = setTimeout(run, delay);
        });
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); run(); }
        });
    };

    CLU.searchInput = function (o) {
        var opts = o || {};
        var wrap = document.createElement('div');
        wrap.className = 'input-group input-group-sm clu-search' + (opts.classes ? ' ' + opts.classes : '');
        var icon = document.createElement('span');
        icon.className = 'input-group-text bg-body';
        icon.setAttribute('aria-hidden', 'true');
        icon.innerHTML = '<i class="bi bi-search"></i>';
        var input = document.createElement('input');
        input.type = 'search';
        input.className = 'form-control';
        if (opts.id) input.id = opts.id;
        CLU.setSearchLabel(input, opts.label || 'Search');
        input.value = opts.value || '';
        input.autocomplete = 'off';
        input.spellcheck = false;
        input.setAttribute('enterkeyhint', 'search');
        wrap.appendChild(icon);
        wrap.appendChild(input);
        return wrap;
    };

    CLU.setSearchLabel = function (input, label) {
        if (!input) return;
        input.setAttribute('aria-label', label);
        input.placeholder = label + '\u2026';
    };

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape' || e.defaultPrevented) return;
        var t = e.target;
        if (!t || t.tagName !== 'INPUT' || t.type !== 'search' || !t.value) return;
        e.preventDefault();
        e.stopPropagation();
        t.value = '';
        t.dispatchEvent(new Event('input', { bubbles: true }));
    }, true);
})();
