/**
 * Problem Files page.
 *
 * DOM contract (templates/problem_files.html):
 *   #pf-tbody, #pf-summary, #pf-refresh, #pf-search, #pf-show-dismissed,
 *   #pf-source-filter [data-source], and the modals #pf-details-modal /
 *   #pf-rebuild-modal / #pf-delete-modal / #pf-sources-modal.
 *
 * Depends on clu-utils.js (CLU.showToast / showError / showSuccess),
 * clu-streaming.js (CLU.executeStreamingOp) and clu-source-search.js
 * (CLU.createSourceSearch) -- all loaded before this file.
 *
 * Rows are addressed by a `data-path` + `data-source` PAIR, never a single
 * joined key. A joined key needs a delimiter, and there is no character a
 * filesystem path cannot contain; worse, the HTML parser rewrites some bytes
 * inside attribute values (a NUL becomes U+FFFD), so the string that comes back
 * out of getAttribute is not always the one that went in. When that happened
 * the row lookup silently missed and every button on the row did nothing.
 *
 * The action set is per-row, driven by the server's classification, not a fixed
 * list: offering Rebuild on a file whose page data is damaged, or Delete on a
 * file whose only problem is cache permissions, is worse than offering nothing.
 */
(function () {
    'use strict';

    var state = { source: '', query: '', includeDismissed: false, rows: [],
                  counts: {}, replacements: [] };
    var replacementPoll = null;
    var pending = null;         // row awaiting a modal confirmation
    var sourceSearch = null;    // lazily built CLU.createSourceSearch instance

    function escapeHtml(str) {
        return String(str === null || str === undefined ? '' : str)
            .replace(/[&<>"']/g, function (c) {
                return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
            });
    }

    function postJson(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {})
        }).then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (data) {
                return { ok: res.ok, status: res.status, data: data };
            });
        });
    }

    function formatWhen(value) {
        if (!value) return '';
        // SQLite CURRENT_TIMESTAMP is UTC without a zone marker.
        var d = new Date(String(value).replace(' ', 'T') + 'Z');
        if (isNaN(d.getTime())) return String(value);
        return d.toLocaleString();
    }

    function findRow(path, source) {
        for (var i = 0; i < state.rows.length; i++) {
            if (state.rows[i].path === path && state.rows[i].source === source) {
                return state.rows[i];
            }
        }
        return null;
    }

    /** Which buttons this row actually earns. */
    function actionsFor(row) {
        var c = row.classification || {};
        var out = [];

        if (!row.reachable) {
            // The library is probably unmounted. Every file action would fail,
            // so offer only the one that cannot.
            out.push({ act: 'remove', label: 'Remove', icon: 'x-lg', cls: 'btn-outline-secondary',
                       title: 'Remove from this list (leaves the file alone)' });
            return out;
        }

        if (row.can_retry) {
            out.push({ act: 'retry', label: 'Retry', icon: 'arrow-clockwise',
                       cls: c.action === 'config' ? 'btn-primary' : 'btn-outline-primary',
                       title: 'Run the failed operation again' });
        }

        // "Replace the file" is the advice for most real damage, so when the
        // classification says so, finding a replacement is the primary action.
        var replacing = row.replacement && row.replacement.status === 'pending';
        if (row.search && row.search.query && !replacing) {
            out.push({
                act: 'sources', label: 'Search', icon: 'search',
                cls: c.action === 'replace' ? 'btn-primary' : 'btn-outline-primary',
                title: 'Search your download sources for a replacement copy'
            });
        }

        var rebuildPrimary = c.action === 'rebuild';
        out.push({
            act: 'rebuild', label: 'Rebuild', icon: 'hammer',
            cls: rebuildPrimary ? 'btn-primary' : 'btn-outline-secondary',
            title: rebuildPrimary
                ? 'Unpack and repack as a valid CBZ'
                : 'Unlikely to help — the page data itself is damaged'
        });

        // A file whose only fault is that CLU could not write to its cache is
        // not a candidate for deletion.
        if (!c.healthy_file) {
            out.push({ act: 'delete', label: 'Delete', icon: 'trash', cls: 'btn-outline-danger',
                       title: 'Move the file to the trash' });
        }

        out.push(row.dismissed
            ? { act: 'undismiss', label: 'Restore', icon: 'eye', cls: 'btn-outline-secondary',
                title: 'Show this entry again' }
            : { act: 'dismiss', label: 'Dismiss', icon: 'eye-slash', cls: 'btn-outline-secondary',
                title: 'Hide this entry until the file changes' });

        return out;
    }

    function renderRow(row) {
        var c = row.classification || {};
        var addr = ' data-path="' + escapeHtml(row.path) + '"' +
                   ' data-source="' + escapeHtml(row.source) + '"';

        var buttons = actionsFor(row).map(function (b) {
            return '<button class="btn btn-sm ' + b.cls + ' ms-1" data-act="' + b.act + '"' +
                addr + ' title="' + escapeHtml(b.title) + '">' +
                '<i class="bi bi-' + b.icon + '"></i><span class="d-none d-xl-inline ms-1">' +
                escapeHtml(b.label) + '</span></button>';
        }).join('');

        var badges = '';
        if (row.dismissed) {
            badges += ' <span class="badge bg-secondary">Dismissed</span>';
        }
        if (!row.reachable) {
            badges += ' <span class="badge bg-warning text-dark">Unreachable</span>';
        }
        var rep = row.replacement;
        if (rep && rep.status === 'pending') {
            badges += ' <span class="badge bg-info text-dark">' +
                '<span class="spinner-border spinner-border-sm me-1" ' +
                'style="width:.6rem;height:.6rem;"></span>Replacing</span>';
        } else if (rep && rep.status === 'failed') {
            badges += ' <span class="badge bg-danger">Replacement failed</span>';
        }

        return '<tr' + (row.dismissed || !row.reachable ? ' class="opacity-50"' : '') + '>' +
            '<td><div class="text-truncate" style="max-width: 28ch;" title="' +
                escapeHtml(row.path) + '"><strong>' + escapeHtml(row.filename) + '</strong></div>' +
                '<div class="small text-muted text-truncate" style="max-width: 28ch;" title="' +
                escapeHtml(row.folder) + '">' + escapeHtml(row.folder) + '</div>' + badges + '</td>' +
            '<td><span class="badge bg-light text-dark border">' +
                escapeHtml(row.source_label) + '</span></td>' +
            '<td><div>' + escapeHtml(c.cause || '') + '</div>' +
                '<div class="small text-muted">' + escapeHtml(c.advice || '') + '</div>' +
                '<button class="btn btn-link btn-sm p-0 small" data-act="details"' + addr +
                '>Details</button></td>' +
            '<td class="text-nowrap small">' + (row.occurrences > 1
                    ? '<span class="badge bg-secondary">' + row.occurrences + '×</span><br>'
                    : '') +
                '<span class="text-muted">' + escapeHtml(formatWhen(row.last_seen)) + '</span></td>' +
            '<td class="text-end text-nowrap">' + buttons + '</td>' +
            '</tr>';
    }

    function render() {
        var tbody = document.getElementById('pf-tbody');
        var summary = document.getElementById('pf-summary');
        if (!tbody) return;

        if (!state.rows.length) {
            // "Nothing has failed" is a claim, so only make it when the counts
            // agree. A summary that reports open entries while the table is
            // empty means the listing could not be read, not that the library
            // is clean -- saying otherwise is how this page lied about 56 rows.
            var openCount = (state.counts && state.counts.open) || 0;
            var filtered = state.query || state.source;
            var message;
            if (!filtered && openCount > 0) {
                message = '<i class="bi bi-exclamation-triangle me-2 text-warning"></i>' +
                    openCount + ' entries are recorded but could not be listed. ' +
                    'Check the logs, and the database status under Settings.';
            } else if (filtered) {
                message = '<i class="bi bi-funnel me-2"></i>No entries match this filter.';
            } else {
                message = '<i class="bi bi-check-circle me-2"></i>' +
                    'Nothing has failed. Files appear here when an operation cannot read them.';
            }
            tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-4">' +
                message + '</td></tr>';
        } else {
            tbody.innerHTML = state.rows.map(renderRow).join('');
        }

        if (summary) {
            var c = state.counts || {};
            var bits = [];
            if (c.open) bits.push(c.open + ' open');
            if (c.dismissed) bits.push(c.dismissed + ' dismissed');
            summary.textContent = bits.join(' · ');
        }
    }

    function refresh() {
        var params = new URLSearchParams();
        if (state.source) params.set('source', state.source);
        if (state.query) params.set('q', state.query);
        if (state.includeDismissed) params.set('include_dismissed', '1');

        return fetch('/api/problem-files?' + params.toString())
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) throw new Error(data.error || 'Request failed');

                state.rows = data.problems || [];
                state.counts = data.counts || {};
                state.replacements = data.replacements || [];
                render();
                renderReplacements();
                if (hasPendingReplacement()) { startReplacementPoll(); }
                else { stopReplacementPoll(); }
            })
            .catch(function (e) {
                CLU.showError('Could not load problem files: ' + e.message);
            });
    }


    // ---------------------------------------------------------------------
    // Replacement banner
    // ---------------------------------------------------------------------
    //
    // A successful swap deletes the problem row -- the file is fixed, and this
    // table is a worklist, not a history -- so the row cannot carry the result.
    // The banner is where "this got replaced" is reported, and it stays until
    // the user dismisses it.

    function replacementCard(r) {
        var addr = ' data-path="' + escapeHtml(r.target_path) + '"';
        if (r.status === 'applied') {
            return '<div class="alert alert-success d-flex align-items-start py-2" role="alert">' +
                '<i class="bi bi-check-circle-fill me-2 mt-1"></i>' +
                '<div class="flex-grow-1 small">' +
                '<strong>Replaced ' + escapeHtml(r.filename) + '</strong>' +
                (r.new_filename ? ' with <span class="font-monospace">' +
                    escapeHtml(r.new_filename) + '</span>' : '') + '.' +
                (r.trashed_path ? ' The damaged copy is in the trash.' : '') +
                '</div>' +
                '<button class="btn btn-sm btn-outline-success ms-2" data-ack="1"' + addr +
                '>Dismiss</button></div>';
        }
        if (r.status === 'failed') {
            return '<div class="alert alert-danger d-flex align-items-start py-2" role="alert">' +
                '<i class="bi bi-exclamation-octagon-fill me-2 mt-1"></i>' +
                '<div class="flex-grow-1 small">' +
                '<strong>Could not replace ' + escapeHtml(r.filename) + '</strong> — ' +
                escapeHtml(r.detail || 'unknown reason') + '. ' +
                'The original is untouched.' +
                '</div>' +
                '<button class="btn btn-sm btn-outline-danger ms-2" data-ack="1"' + addr +
                '>Dismiss</button></div>';
        }
        return '<div class="alert alert-info d-flex align-items-start py-2" role="alert">' +
            '<span class="spinner-border spinner-border-sm me-2 mt-1"></span>' +
            '<div class="flex-grow-1 small">' +
            '<strong>Replacement on the way for ' + escapeHtml(r.filename) + '</strong>' +
            ' — it will be moved into place automatically when the download finishes.' +
            '</div>' +
            '<button class="btn btn-sm btn-outline-secondary ms-2" data-cancel="1"' + addr +
            '>Cancel</button></div>';
    }

    function renderReplacements() {
        var el = document.getElementById('pf-replacements');
        if (!el) return;
        el.innerHTML = (state.replacements || []).map(replacementCard).join('');
    }

    function hasPendingReplacement() {
        return (state.replacements || []).some(function (r) { return r.status === 'pending'; });
    }

    function startReplacementPoll() {
        if (replacementPoll) return;
        // The pass also runs server-side from the download pipeline; this is
        // what makes the page react promptly while it is open.
        replacementPoll = setInterval(function () {
            postJson('/api/problem-files/replacements/apply').then(function (res) {
                if (!res.ok || !res.data.success) return;
                state.replacements = res.data.replacements || [];
                renderReplacements();
                (res.data.changed || []).forEach(function (c) {
                    if (c.status === 'applied') {
                        CLU.showSuccess('Replaced ' + c.filename + '.');
                    } else if (c.status === 'failed') {
                        CLU.showError('Could not replace ' + c.filename + ' — ' +
                            (c.detail || 'unknown reason'));
                    }
                });
                if (res.data.changed && res.data.changed.length) refresh();
                if (!hasPendingReplacement()) stopReplacementPoll();
            });
        }, 10000);
    }

    function stopReplacementPoll() {
        if (replacementPoll) { clearInterval(replacementPoll); replacementPoll = null; }
    }

    function showModal(id) {
        var el = document.getElementById(id);
        if (el) bootstrap.Modal.getOrCreateInstance(el).show();
    }

    function hideModal(id) {
        var el = document.getElementById(id);
        var m = el && bootstrap.Modal.getInstance(el);
        if (m) m.hide();
    }

    function showDetails(row) {
        var c = row.classification || {};
        var body = document.getElementById('pf-details-body');
        if (!body) return;
        function pair(label, value, mono) {
            if (!value) return '';
            return '<dt class="col-sm-3">' + escapeHtml(label) + '</dt>' +
                '<dd class="col-sm-9' + (mono ? ' font-monospace small text-break' : '') + '">' +
                escapeHtml(value) + '</dd>';
        }
        body.innerHTML =
            pair('File', row.path, true) +
            pair('Source', row.source_label) +
            pair('Cause', c.cause) +
            pair('Suggested', c.advice) +
            pair('Error', (row.error_class || '') +
                (row.error_message ? ': ' + row.error_message : ''), true) +
            pair('First seen', formatWhen(row.first_seen)) +
            pair('Last seen', formatWhen(row.last_seen)) +
            pair('Occurrences', String(row.occurrences || 1));
        showModal('pf-details-modal');
    }

    // ---------------------------------------------------------------------
    // Source search -- find a replacement copy
    // ---------------------------------------------------------------------
    //
    // Reuses CLU.createSourceSearch, the same fan-out (GetComics + Usenet +
    // DC++, stacked in the user's source-priority order) the Wanted and Series
    // pages use. The series/issue/year context is parsed server-side by
    // cbz_ops.rename.parse_comic_filename, so this page cannot drift from the
    // parser the rest of the app renames and matches with.

    function getSourceSearch() {
        if (!sourceSearch) {
            if (!CLU.createSourceSearch) return null;
            sourceSearch = CLU.createSourceSearch({
                resultsEl: document.getElementById('pf-sources-results'),
                getContext: function () {
                    var s = (pending && pending.search) || {};
                    return { series: s.series, issue: s.issue, year: s.year };
                },
                toast: function (message, type) { CLU.showToast('Download', message, type); },
                onQueued: function (payload) {
                    if (!payload.keepOpen) {
                        setTimeout(function () { hideModal('pf-sources-modal'); }, 500);
                    }
                    // Claim the damaged file's own path as this download's
                    // destination. Without the claim the finished file just
                    // sits in TARGET: the issue is not "missing" (the corrupt
                    // file is still there), so the wanted sweep ignores it.
                    var row = pending;
                    if (!row) return;
                    var s = row.search || {};
                    postJson('/api/problem-files/replace', {
                        path: row.path,
                        series: s.series,
                        issue: s.issue,
                        query: s.query,
                        download_source: payload.source
                    }).then(function () {
                        CLU.showToast('Replacement queued',
                            'It will be moved onto ' + row.filename + ' when it finishes.',
                            'info');
                        refresh();
                        startReplacementPoll();
                    });
                }
            });
        }
        return sourceSearch;
    }

    function openSourceSearch(row) {
        pending = row;
        var search = getSourceSearch();
        var input = document.getElementById('pf-sources-query');
        var results = document.getElementById('pf-sources-results');
        if (!search || !input || !results) {
            CLU.showError('Source search is unavailable on this page');
            return;
        }

        var s = row.search || {};
        document.getElementById('pf-sources-file').textContent = row.filename;
        input.value = s.query || '';
        results.innerHTML = '<div class="text-center text-muted py-4">' +
            '<div class="spinner-border spinner-border-sm me-2"></div>Searching&hellip;</div>';
        showModal('pf-sources-modal');
        search.run(input.value);
    }

    function runSourceSearch() {
        var search = getSourceSearch();
        var input = document.getElementById('pf-sources-query');
        if (!search || !input || !input.value.trim()) return;
        document.getElementById('pf-sources-results').innerHTML =
            '<div class="text-center text-muted py-4">' +
            '<div class="spinner-border spinner-border-sm me-2"></div>Searching&hellip;</div>';
        search.run(input.value.trim());
    }

    // ---------------------------------------------------------------------
    // Row actions
    // ---------------------------------------------------------------------

    function doRetry(row) {
        return postJson('/api/problem-files/retry', { path: row.path, source: row.source })
            .then(function (res) {
                if (!res.ok || !res.data.success) {
                    CLU.showError(res.data.error || 'Retry failed');
                    return;
                }
                if (res.data.fixed) {
                    CLU.showSuccess(row.filename + ' is fixed.');
                } else {
                    CLU.showToast('Still failing', res.data.message || '', 'warning');
                }
                return refresh();
            });
    }

    function doDismiss(row, dismissed) {
        return postJson('/api/problem-files/dismiss',
                        { path: row.path, source: row.source, dismissed: dismissed })
            .then(function (res) {
                if (!res.ok || !res.data.success) {
                    CLU.showError(res.data.error || 'Could not update this entry');
                    return;
                }
                return refresh();
            });
    }

    function doRemove(row) {
        return postJson('/api/problem-files/remove', { path: row.path, source: row.source })
            .then(function (res) {
                if (!res.ok || !res.data.success) {
                    CLU.showError(res.data.error || 'Could not remove this entry');
                    return;
                }
                return refresh();
            });
    }

    function confirmRebuild(row) {
        pending = row;
        var c = row.classification || {};
        document.getElementById('pf-rebuild-name').textContent = row.filename;
        document.getElementById('pf-rebuild-warning').innerHTML = c.repairable
            ? 'This is the case rebuild is good at — a RAR archive with a ' +
              '<code>.cbz</code> name. It should come back as a working comic.'
            : 'Rebuilding is <strong>unlikely to fix this</strong>. ' +
              escapeHtml(c.advice || '') +
              ' If it fails, the file is left exactly as it is now.';
        showModal('pf-rebuild-modal');
    }

    function confirmDelete(row) {
        pending = row;
        document.getElementById('pf-delete-name').textContent = row.filename;
        showModal('pf-delete-modal');
    }

    function bind() {
        var tbody = document.getElementById('pf-tbody');
        if (tbody) {
            tbody.addEventListener('click', function (ev) {
                var btn = ev.target.closest('[data-act]');
                if (!btn) return;
                var row = findRow(btn.getAttribute('data-path'),
                                  btn.getAttribute('data-source'));
                if (!row) return;
                switch (btn.getAttribute('data-act')) {
                    case 'details':   showDetails(row); break;
                    case 'retry':     doRetry(row); break;
                    case 'sources':   openSourceSearch(row); break;
                    case 'dismiss':   doDismiss(row, true); break;
                    case 'undismiss': doDismiss(row, false); break;
                    case 'remove':    doRemove(row); break;
                    case 'rebuild':   confirmRebuild(row); break;
                    case 'delete':    confirmDelete(row); break;
                }
            });
        }

        var banner = document.getElementById('pf-replacements');
        if (banner) {
            banner.addEventListener('click', function (ev) {
                var btn = ev.target.closest('[data-ack],[data-cancel]');
                if (!btn) return;
                postJson('/api/problem-files/replacements/ack', {
                    path: btn.getAttribute('data-path'),
                    cancel: btn.hasAttribute('data-cancel')
                }).then(refresh);
            });
        }

        var filter = document.getElementById('pf-source-filter');
        if (filter) {
            filter.addEventListener('click', function (ev) {
                var btn = ev.target.closest('[data-source]');
                if (!btn) return;
                filter.querySelectorAll('[data-source]').forEach(function (b) {
                    b.classList.toggle('active', b === btn);
                });
                state.source = btn.getAttribute('data-source');
                refresh();
            });
        }

        var search = document.getElementById('pf-search');
        if (search) {
            var timer = null;
            search.addEventListener('input', function () {
                clearTimeout(timer);
                timer = setTimeout(function () {
                    state.query = search.value.trim();
                    refresh();
                }, 250);
            });
        }

        var dismissed = document.getElementById('pf-show-dismissed');
        if (dismissed) {
            dismissed.addEventListener('change', function () {
                state.includeDismissed = dismissed.checked;
                refresh();
            });
        }

        var refreshBtn = document.getElementById('pf-refresh');
        if (refreshBtn) refreshBtn.addEventListener('click', function () { refresh(); });

        var sourcesGo = document.getElementById('pf-sources-go');
        if (sourcesGo) sourcesGo.addEventListener('click', runSourceSearch);

        var sourcesQuery = document.getElementById('pf-sources-query');
        if (sourcesQuery) {
            sourcesQuery.addEventListener('keydown', function (ev) {
                if (ev.key === 'Enter') { ev.preventDefault(); runSourceSearch(); }
            });
        }

        var rebuildBtn = document.getElementById('pf-rebuild-confirm');
        if (rebuildBtn) {
            rebuildBtn.addEventListener('click', function () {
                hideModal('pf-rebuild-modal');
                if (!pending) return;
                // Same streaming op the File Manager drives, so progress shows
                // up in the header indicator like any other job.
                CLU.executeStreamingOp('single_file', pending.path);
                pending = null;
                // The subprocess records or clears its own row; give it a
                // moment, then re-read rather than guessing the outcome.
                setTimeout(refresh, 4000);
            });
        }

        var deleteBtn = document.getElementById('pf-delete-confirm');
        if (deleteBtn) {
            deleteBtn.addEventListener('click', function () {
                hideModal('pf-delete-modal');
                if (!pending) return;
                var row = pending;
                pending = null;
                postJson('/api/problem-files/delete', { path: row.path })
                    .then(function (res) {
                        if (!res.ok || !res.data.success) {
                            CLU.showError(res.data.error || 'Delete failed');
                            return;
                        }
                        CLU.showSuccess(row.filename + ' moved to trash.');
                        return refresh();
                    });
            });
        }
    }

    function init() {
        if (!document.getElementById('problem-files-app')) return;  // not this page
        bind();
        refresh();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
