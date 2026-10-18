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

    // ComicInfo.xml lives inside a zip, so only a .cbz/.zip has anything to show.
    function hasComicInfoViewer(row) {
        return row.reachable && row.source !== 'series-json' &&
            row.source !== 'empty-folder' &&
            /\.(cbz|zip)$/i.test(row.filename || '');
    }

    /** Which buttons this row actually earns. */
    function actionsFor(row) {
        var out = baseActionsFor(row);
        if (hasComicInfoViewer(row)) {
            out.unshift({ act: 'info', label: 'Info', icon: 'info-circle',
                          cls: 'btn-outline-secondary',
                          title: 'View or edit the ComicInfo.xml and pages of this file' });
        }
        return out;
    }

    function baseActionsFor(row) {
        var c = row.classification || {};
        var out = [];

        if (!row.reachable) {
            // The library is probably unmounted. Every file action would fail,
            // so offer only the one that cannot.
            out.push({ act: 'remove', label: 'Remove', icon: 'x-lg', cls: 'btn-outline-secondary',
                       title: 'Remove from this list (leaves the file alone)' });
            return out;
        }

        if (row.source === 'series-json') {
            // A sidecar, not a comic: none of Rebuild / Search / Delete apply.
            out.push({ act: 'refresh-sj', label: 'Refresh', icon: 'cloud-download',
                       cls: 'btn-primary',
                       title: 'Rewrite series.json from Metron or ComicVine' });
            out.push({ act: 'retry', label: 'Re-check', icon: 'arrow-clockwise',
                       cls: 'btn-outline-primary',
                       title: 'Read the file again (after editing it by hand)' });
            out.push(row.dismissed
                ? { act: 'undismiss', label: 'Restore', icon: 'eye', cls: 'btn-outline-secondary',
                    title: 'Show this entry again' }
                : { act: 'dismiss', label: 'Dismiss', icon: 'eye-slash', cls: 'btn-outline-secondary',
                    title: 'Hide this entry until the file changes' });
            out.push({ act: 'remove', label: 'Remove', icon: 'x-lg', cls: 'btn-outline-secondary',
                       title: 'Remove from this list (leaves the file alone)' });
            return out;
        }

        if (row.source === 'empty-folder') {
            // A folder with no comics in it. Delete is the point of the row;
            // the server checks again that it is still empty before trashing.
            out.push({ act: 'retry', label: 'Re-check', icon: 'arrow-clockwise',
                       cls: 'btn-outline-primary',
                       title: 'Look in this folder again' });
            out.push({ act: 'delete', label: 'Delete', icon: 'trash', cls: 'btn-outline-danger',
                       title: 'Move the folder and its leftover files to the trash' });
            out.push(row.dismissed
                ? { act: 'undismiss', label: 'Restore', icon: 'eye', cls: 'btn-outline-secondary',
                    title: 'Show this entry again' }
                : { act: 'dismiss', label: 'Dismiss', icon: 'eye-slash', cls: 'btn-outline-secondary',
                    title: 'Hide this entry until the folder changes' });
            out.push({ act: 'remove', label: 'Remove', icon: 'x-lg', cls: 'btn-outline-secondary',
                       title: 'Remove from this list (leaves the folder alone)' });
            return out;
        }

        if (row.source === 'orphan') {
            // Not damage and not a failed operation: Rebuild and Search do not
            // apply. Delete is offered only where the classification says the
            // file itself is the surplus (a .cbr beside its .cbz).
            out.push({ act: 'retry', label: 'Re-check', icon: 'arrow-clockwise',
                       cls: 'btn-outline-primary',
                       title: 'Look at this entry again' });
            if (!c.healthy_file) {
                out.push({ act: 'delete', label: 'Delete', icon: 'trash', cls: 'btn-outline-danger',
                           title: 'Move the file to the trash' });
            }
            out.push(row.dismissed
                ? { act: 'undismiss', label: 'Restore', icon: 'eye', cls: 'btn-outline-secondary',
                    title: 'Show this entry again' }
                : { act: 'dismiss', label: 'Dismiss', icon: 'eye-slash', cls: 'btn-outline-secondary',
                    title: 'Hide this entry until the file changes' });
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

        // Every series.json is called series.json, so the folder is the name
        // that tells rows apart.
        var title = row.source === 'series-json'
            ? (row.folder || '').split(/[\\/]/).filter(Boolean).pop() || row.filename
            : row.filename;

        return '<tr' + (row.dismissed || !row.reachable ? ' class="opacity-50"' : '') + '>' +
            '<td><div class="text-truncate" style="max-width: 28ch;" title="' +
                escapeHtml(row.path) + '"><strong>' + escapeHtml(title) + '</strong></div>' +
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
                updateSjBulkButton();
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
            pair(row.source === 'empty-folder' ? 'Folder' : 'File', row.path, true) +
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

    /** The <tr> for a row. Compared as strings, never through a selector: see
     *  the addressing note at the top of this file. */
    function findTr(path, source) {
        var btns = document.querySelectorAll('#pf-tbody [data-act="details"]');
        for (var i = 0; i < btns.length; i++) {
            if (btns[i].getAttribute('data-path') === path &&
                btns[i].getAttribute('data-source') === source) {
                return btns[i].closest('tr');
            }
        }
        return null;
    }

    /** Flash the corrected row green, then fade it out. Resolves when done so
     *  the caller reloads the list only after the animation has played. */
    function celebrateFixed(row) {
        var tr = findTr(row.path, row.source);
        var reduced = window.matchMedia &&
            window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (!tr || reduced) return Promise.resolve();
        tr.classList.add('pf-fixed');
        return new Promise(function (resolve) {
            setTimeout(function () {
                tr.classList.add('pf-fading');
                setTimeout(resolve, 650);
            }, 900);
        });
    }

    function doRetry(row) {
        return postJson('/api/problem-files/retry', { path: row.path, source: row.source })
            .then(function (res) {
                if (!res.ok || !res.data.success) {
                    CLU.showError(res.data.error || 'Retry failed');
                    return;
                }
                if (res.data.fixed) {
                    CLU.showSuccess(row.filename + ' is fixed.');
                    return celebrateFixed(row).then(refresh);
                }
                CLU.showToast('Still failing', res.data.message || '', 'warning');
                // Reload first (the error text may have changed), then flash
                // the re-rendered row: it stays in the list.
                return refresh().then(function () { warnStillFailing(row); });
            });
    }

    /** Highlight a row yellow that is still broken after a fix attempt. Unlike
     *  celebrateFixed this never removes it; the highlight fades back out. */
    function warnStillFailing(row) {
        var tr = findTr(row.path, row.source);
        if (!tr) return;
        tr.classList.add('pf-warn');
        setTimeout(function () { tr.classList.remove('pf-warn'); }, 2500);
    }

    // Refresh rewrites series.json from a provider, then re-checks that one
    // file so the row clears (or stays, with the new reason) without a rescan.
    function doRefreshSeriesJson(row) {
        CLU.showToast('series.json', 'Refreshing ' + row.folder + '…', 'info');
        return postJson('/api/series-json/refresh', { path: row.folder })
            .then(function (res) {
                if (!res.ok || !res.data.success) {
                    CLU.showError((res.data && res.data.error) || 'Could not refresh series.json');
                    return;
                }
                return doRetry(row);
            });
    }

    /** Highlight one source filter button and show that source. */
    function showSource(source) {
        state.source = source;
        var filter = document.getElementById('pf-source-filter');
        if (filter) {
            filter.querySelectorAll('[data-source]').forEach(function (b) {
                b.classList.toggle('active', b.getAttribute('data-source') === source);
            });
        }
    }

    /**
     * Start a background scan, follow its operation, then show its rows.
     * Shared by "Scan series.json" and "Scan empty folders".
     */
    function runScan(opts) {
        var btn = document.getElementById(opts.buttonId);
        if (btn) btn.disabled = true;
        postJson(opts.url).then(function (res) {
            if (!res.ok || !res.data.success) {
                if (btn) btn.disabled = false;
                CLU.showError((res.data && res.data.error) || 'Could not start the scan');
                return;
            }
            CLU.showToast(opts.label, 'Scanning your libraries…', 'info');
            var opId = res.data.op_id;
            // /api/operation/<id>, never /api/operations (see base.html).
            var timer = CLU.startPoll(function (signal) {
                return fetch('/api/operation/' + encodeURIComponent(opId), { signal: signal })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        var op = data.operation || null;
                        // A finished op is pruned after a TTL: missing means done.
                        if (op && (op.status === 'running' || op.status === 'queued')) return;
                        clearInterval(timer);
                        if (btn) btn.disabled = false;
                        if (op && op.status === 'error') {
                            CLU.showError(opts.label + ' scan failed');
                        } else {
                            CLU.showSuccess(opts.label + ' scan complete');
                        }
                        showSource(opts.source);
                        return refresh();
                    });
            }, 2000);
        });
    }

    function scanSeriesJson() {
        runScan({ url: '/api/problem-files/scan-series-json', buttonId: 'pf-scan-seriesjson',
                  label: 'series.json', source: 'series-json' });
    }

    function scanEmptyFolders() {
        runScan({ url: '/api/problem-files/scan-empty-folders', buttonId: 'pf-scan-empty',
                  label: 'Empty folder', source: 'empty-folder' });
    }

    // ---------------------------------------------------------------------
    // Refresh all series.json
    // ---------------------------------------------------------------------
    var sjResultHidden = false;

    function openSjCount() {
        return (state.counts && state.counts.by_source && state.counts.by_source['series-json']) || 0;
    }

    /** The bulk button earns its place only while series.json rows are open. */
    function updateSjBulkButton() {
        var btn = document.getElementById('pf-refresh-sj-all');
        if (!btn) return;
        var n = openSjCount();
        btn.classList.toggle('d-none', n === 0);
        var badge = document.getElementById('pf-refresh-sj-count');
        if (badge) badge.textContent = n;
    }

    function renderSjResult(result) {
        var host = document.getElementById('pf-sj-result');
        if (!host) return;
        if (!result || sjResultHidden) { host.innerHTML = ''; return; }
        var failed = result.failed || [];
        var when = result.finished_at ? new Date(result.finished_at * 1000).toLocaleString() : '';
        var tone = failed.length ? 'warning' : 'success';
        var list = failed.length
            ? '<ul class="small mb-0 mt-2">' + failed.map(function (f) {
                return '<li class="text-break"><strong>' + escapeHtml(f.folder) + '</strong> &mdash; ' +
                    escapeHtml(f.reason) + '</li>';
            }).join('') + '</ul>'
            : '';
        host.innerHTML =
            '<div class="alert alert-' + tone + ' alert-dismissible mb-3" role="alert">' +
            '<div><i class="bi bi-cloud-download me-2"></i><strong>series.json refresh</strong>' +
            (when ? ' <span class="text-muted small">' + escapeHtml(when) + '</span>' : '') + '</div>' +
            '<div>' + result.fixed + ' of ' + result.total + ' corrected' +
            (failed.length ? ', <strong>' + failed.length + ' still need attention</strong>' : '.') +
            (result.error ? ' (' + escapeHtml(result.error) + ')' : '') + '</div>' +
            list +
            '<button type="button" class="btn-close" id="pf-sj-result-close" aria-label="Dismiss"></button>' +
            '</div>';
        var close = document.getElementById('pf-sj-result-close');
        if (close) close.addEventListener('click', function () {
            sjResultHidden = true;
            host.innerHTML = '';
        });
    }

    function loadSjResult() {
        return fetch('/api/problem-files/refresh-series-json-all/result')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) { sjResultHidden = false; renderSjResult(data.result); }
            })
            .catch(function () { /* advisory */ });
    }

    function confirmRefreshAllSj() {
        var n = openSjCount();
        if (!n) { CLU.showError('There are no series.json entries to refresh.'); return; }
        document.getElementById('pf-sj-all-count').textContent =
            n + ' series.json file' + (n === 1 ? '' : 's');
        showModal('pf-sj-all-modal');
    }

    function startRefreshAllSj() {
        hideModal('pf-sj-all-modal');
        var btn = document.getElementById('pf-refresh-sj-all');
        if (btn) btn.disabled = true;
        postJson('/api/problem-files/refresh-series-json-all').then(function (res) {
            if (!res.ok || !res.data.success) {
                if (btn) btn.disabled = false;
                CLU.showError((res.data && res.data.error) || 'Could not start the refresh');
                return;
            }
            CLU.showToast('series.json', 'Refreshing series.json files\u2026', 'info');
            var opId = res.data.op_id;
            // /api/operation/<id>, never /api/operations (see base.html).
            var timer = CLU.startPoll(function (signal) {
                return fetch('/api/operation/' + encodeURIComponent(opId), { signal: signal })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        var op = data.operation || null;
                        if (op && (op.status === 'running' || op.status === 'queued')) return;
                        clearInterval(timer);
                        if (btn) btn.disabled = false;
                        if (op && op.status === 'error') {
                            CLU.showError('series.json refresh failed');
                        }
                        return loadSjResult().then(function () {
                            showSource('series-json');
                            return refresh();
                        });
                    });
            }, 2000);
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

    // The viewer can edit or clear ComicInfo.xml, which rewrites the archive. Any
    // such change is a reason to re-read this list (the file may now pass), but
    // not while the modal is open: that would redraw rows under the user's hands.
    var infoChanged = false;

    function openInfo(row) {
        if (!window.CLU || typeof CLU.showCBZInfo !== 'function') {
            CLU.showError('The file information viewer is unavailable.');
            return;
        }
        infoChanged = false;
        CLU.showCBZInfo(row.path, row.filename);
    }

    function bindInfoModal() {
        window._cluCbzInfo = window._cluCbzInfo || {};
        window._cluCbzInfo.onClearComplete = function () { infoChanged = true; };
        window._cluCbzInfo.onMetadataSaved = function () { infoChanged = true; };
        var modal = document.getElementById('cbzInfoModal');
        if (modal) {
            modal.addEventListener('hidden.bs.modal', function () {
                if (infoChanged) { infoChanged = false; refresh(); }
            });
        }
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
        var folder = row.source === 'empty-folder';
        document.getElementById('pf-delete-title').textContent =
            folder ? 'Delete this folder?' : 'Delete this file?';
        document.getElementById('pf-delete-name').textContent = folder ? row.path : row.filename;
        var note = document.getElementById('pf-delete-folder-note');
        if (note) note.classList.toggle('d-none', !folder);
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
                    case 'info':      openInfo(row); break;
                    case 'details':   showDetails(row); break;
                    case 'retry':     doRetry(row); break;
                    case 'refresh-sj': doRefreshSeriesJson(row); break;
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

        var scanBtn = document.getElementById('pf-scan-seriesjson');
        if (scanBtn) scanBtn.addEventListener('click', scanSeriesJson);
        var emptyBtn = document.getElementById('pf-scan-empty');
        if (emptyBtn) emptyBtn.addEventListener('click', scanEmptyFolders);

        var sjAllBtn = document.getElementById('pf-refresh-sj-all');
        if (sjAllBtn) sjAllBtn.addEventListener('click', confirmRefreshAllSj);
        var sjAllGo = document.getElementById('pf-sj-all-confirm');
        if (sjAllGo) sjAllGo.addEventListener('click', startRefreshAllSj);

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
                            // A folder that filled up since the scan is
                            // dropped from the list server-side.
                            if (res.status === 409) refresh();
                            return;
                        }
                        // An empty or cvinfo-only folder is removed outright
                        // (helpers/trash.move_to_trash), not trashed.
                        CLU.showSuccess(row.filename +
                            (res.data.trashed ? ' moved to trash.' : ' deleted.'));
                        return refresh();
                    });
            });
        }
    }

    // ---------------------------------------------------------------------
    // Library health card
    // ---------------------------------------------------------------------
    var healthPoll = null;

    function fmtNumber(n) {
        return Number(n || 0).toLocaleString();
    }

    function renderHealth(h) {
        var line = document.getElementById('pf-health-line');
        if (!line) return;
        var cov = h.coverage || {};
        var dups = h.duplicates || {};
        var parts = [];
        if (!h.last_run_at) {
            parts.push('No scan has run yet. An empty list below means nothing has failed, ' +
                       'not that the library is clean.');
        } else {
            parts.push('Last scan ' + new Date(h.last_run_at * 1000).toLocaleString());
            parts.push('Checked ' + fmtNumber(cov.checked) + ' of ' + fmtNumber(cov.total) +
                       ' files (' + (cov.pct || 0) + '%)');
            parts.push(fmtNumber(h.integrity_open) + ' damaged');
            parts.push(fmtNumber(dups.exact) + ' exact / ' + fmtNumber(dups.likely) +
                       ' likely duplicate groups');
            parts.push(fmtNumber(h.orphans_open) + ' stray files');
            parts.push(fmtNumber(h.empty_folders_open) + ' empty folders');
            if (h.comicinfo && h.comicinfo.total) {
                parts.push(fmtNumber(h.comicinfo.missing) + ' of ' + fmtNumber(h.comicinfo.total) +
                           ' without ComicInfo.xml');
            }
        }
        line.textContent = parts.join(' · ');

        var score = document.getElementById('pf-health-score');
        if (score) {
            if (h.last_run_at) {
                var colour = { green: 'success', amber: 'warning', red: 'danger' }[h.level] || 'secondary';
                score.className = 'badge ms-2 bg-' + colour + (colour === 'warning' ? ' text-dark' : '');
                score.textContent = h.score + '/100';
            } else {
                score.className = 'badge ms-2 d-none';
            }
        }
        var bar = document.getElementById('pf-health-bar');
        if (bar) bar.style.width = (cov.pct || 0) + '%';

        var dupBadge = document.getElementById('pf-health-dups');
        if (dupBadge) {
            var total = (dups.exact || 0) + (dups.likely || 0);
            dupBadge.textContent = total;
            dupBadge.classList.toggle('d-none', !total);
        }

        var enabled = document.getElementById('pf-health-enabled');
        if (enabled) enabled.checked = !!h.enabled;
        var interval = document.getElementById('pf-health-interval');
        if (interval && h.interval_hours) interval.value = String(h.interval_hours);
        var scanBtn = document.getElementById('pf-health-scan');
        if (scanBtn) scanBtn.disabled = !!h.running;
    }

    function loadHealth() {
        return fetch('/api/problem-files/health/summary')
            .then(function (r) { return r.json(); })
            .then(function (h) { if (h.success) renderHealth(h); })
            .catch(function () { /* the card is advisory; the list still works */ });
    }

    function loadHealthSettings() {
        return fetch('/api/problem-files/health/settings')
            .then(function (r) { return r.json(); })
            .then(function (s) {
                if (!s.success) return;
                var batch = document.getElementById('pf-health-batch');
                if (batch) {
                    if (!batch.querySelector('option[value="' + s.batch + '"]')) {
                        var opt = document.createElement('option');
                        opt.value = String(s.batch);
                        opt.textContent = String(s.batch);
                        batch.appendChild(opt);
                    }
                    batch.value = String(s.batch);
                }
                var note = document.getElementById('pf-health-note');
                if (note) {
                    note.textContent = 'Reads every comic once, then again every ' +
                        s.recheck_days + ' days. Off by default.';
                }
            })
            .catch(function () {});
    }

    function saveHealthSettings(patch) {
        return postJson('/api/problem-files/health/settings', patch).then(function (res) {
            if (!res.ok || !res.data.success) {
                CLU.showError((res.data && res.data.error) || 'Could not save the setting');
            }
            return loadHealth();
        });
    }

    function runHealthScan() {
        var btn = document.getElementById('pf-health-scan');
        if (btn) btn.disabled = true;
        postJson('/api/problem-files/health/scan').then(function (res) {
            if (!res.ok || !res.data.success) {
                if (btn) btn.disabled = false;
                CLU.showError((res.data && res.data.error) || 'Could not start the scan');
                return;
            }
            CLU.showToast('Library health', 'Scanning your library…', 'info');
            var opId = res.data.op_id;
            if (healthPoll) clearInterval(healthPoll);
            // /api/operation/<id>, never /api/operations (see base.html).
            healthPoll = CLU.startPoll(function (signal) {
                return fetch('/api/operation/' + encodeURIComponent(opId), { signal: signal })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        var op = data.operation || null;
                        if (op && (op.status === 'running' || op.status === 'queued')) return;
                        clearInterval(healthPoll);
                        healthPoll = null;
                        if (op && op.status === 'error') {
                            CLU.showError('Library health scan failed');
                        } else {
                            CLU.showSuccess('Library health scan complete');
                        }
                        return Promise.all([loadHealth(), refresh()]);
                    });
            }, 2000);
        });
    }

    function bindHealth() {
        var scanBtn = document.getElementById('pf-health-scan');
        if (scanBtn) scanBtn.addEventListener('click', runHealthScan);
        var enabled = document.getElementById('pf-health-enabled');
        if (enabled) {
            enabled.addEventListener('change', function () {
                saveHealthSettings({ enabled: enabled.checked });
            });
        }
        var interval = document.getElementById('pf-health-interval');
        if (interval) {
            interval.addEventListener('change', function () {
                saveHealthSettings({ interval_hours: parseInt(interval.value, 10) });
            });
        }
        var batch = document.getElementById('pf-health-batch');
        if (batch) {
            batch.addEventListener('change', function () {
                saveHealthSettings({ batch: parseInt(batch.value, 10) });
            });
        }
    }

    function init() {
        if (!document.getElementById('problem-files-app')) return;  // not this page
        bind();
        bindInfoModal();
        bindHealth();
        refresh();
        loadSjResult();
        loadHealth();
        loadHealthSettings();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
