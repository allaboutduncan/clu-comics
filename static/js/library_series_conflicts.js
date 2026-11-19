/**
 * Series Folder Conflicts page (templates/library_series_conflicts.html).
 *
 * Rows are looked up by index into state.rows, never by a path read back out
 * of a DOM attribute (see static/js/problem_files.js for why). Requests carry
 * the row's database id; the server re-reads the row and re-validates its
 * folder, so a stale page cannot map a folder that is no longer listed.
 */
(function () {
    'use strict';

    var state = { rows: [], groups: [], showIgnored: false, query: '', pending: null, bulk: null };

    function postJson(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {})
        }).then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (data) {
                return { ok: res.ok && data.success !== false, status: res.status, data: data };
            });
        });
    }

    function esc(text) { return CLU.escapeHtml(text == null ? '' : String(text)); }

    function filesBadge(n) {
        var count = Number(n || 0);
        return '<span class="badge bg-secondary ms-1">' + count + (count === 1 ? ' file' : ' files') + '</span>';
    }

    function matches(row, q) {
        if (!q) return true;
        var hay = [row.series_name, row.year, row.folder, row.mapped_folder, row.publisher_name]
            .join(' ').toLowerCase();
        return hay.indexOf(q.toLowerCase()) !== -1;
    }

    function renderRow(row, i) {
        var series = esc(row.series_name || ('Series ' + row.series_id)) +
            (row.year ? ' <span class="text-muted">(' + esc(row.year) + ')</span>' : '');
        if (row.publisher_name) series += '<div class="small text-muted">' + esc(row.publisher_name) + '</div>';

        var here = '<span class="text-break small">' + esc(row.folder) + '</span>' + filesBadge(row.file_count);
        if (!row.reachable) here += ' <span class="badge bg-warning text-dark">Folder not found</span>';
        if (row.ignored) here += ' <span class="badge bg-light text-dark border">Ignored</span>';
        if (row.likely_same) {
            here += ' <span class="badge bg-info text-dark" title="The two folder names differ only by case or punctuation">' +
                'Likely the same folder</span>';
        }

        var mapped = row.mapped_folder
            ? '<span class="text-break small">' + esc(row.mapped_folder) + '</span>'
            : '<span class="text-muted small">Not mapped any more</span>';

        var actions;
        if (!row.reachable) {
            actions = '<button class="btn btn-sm btn-outline-secondary" data-act="remove" data-idx="' + i + '">' +
                '<i class="bi bi-x-lg me-1"></i>Remove</button>';
        } else {
            actions =
                (row.mapped_folder
                    ? '<button class="btn btn-sm btn-primary text-nowrap" data-act="merge" data-idx="' + i + '"' +
                      ' title="See both folders side by side, then merge or drop duplicates">' +
                      '<i class="bi bi-intersect me-1"></i>Compare &amp; merge</button>'
                    : '') +
                '<button class="btn btn-sm btn-outline-primary text-nowrap" data-act="switch" data-idx="' + i + '"' +
                ' title="Make this folder the one the series points at">' +
                '<i class="bi bi-arrow-left-right me-1"></i>Use this folder instead</button>' +
                (row.ignored
                    ? '<button class="btn btn-sm btn-outline-secondary" data-act="unignore" data-idx="' + i + '">' +
                      '<i class="bi bi-eye me-1"></i>Un-ignore</button>'
                    : '<button class="btn btn-sm btn-outline-secondary" data-act="ignore" data-idx="' + i + '"' +
                      ' title="Keep both folders as they are and stop listing this one">' +
                      '<i class="bi bi-eye-slash me-1"></i>Ignore</button>');
        }
        return '<tr data-idx="' + i + '">' +
            '<td>' + series + '</td>' +
            '<td>' + here + '</td>' +
            '<td>' + mapped + '</td>' +
            '<td class="text-end"><div class="d-inline-flex flex-column gap-1">' + actions + '</div></td>' +
            '</tr>';
    }

    function render() {
        var tbody = document.getElementById('sc-tbody');
        var empty = document.getElementById('sc-empty');
        var table = tbody.closest('.table-responsive');
        empty.innerHTML = '';
        var shown = [];
        state.rows.forEach(function (row, i) { if (matches(row, state.query)) shown.push([row, i]); });

        var openCount = state.rows.filter(function (r) { return !r.ignored; }).length;
        document.getElementById('sc-summary').textContent = CLU.countText({
            total: state.showIgnored ? state.rows.length : openCount,
            shown: shown.length,
            noun: 'conflicts', one: 'conflict'
        });

        if (!state.rows.length) {
            table.classList.add('d-none');
            empty.appendChild(CLU.emptyState({
                icon: 'bi-check2-circle',
                title: 'No series folder conflicts',
                message: 'Every folder Scan Library found belongs to a series that points at it. ' +
                    'Run Scan Library from the Pull List to check again.'
            }));
            return;
        }
        if (!shown.length) {
            table.classList.add('d-none');
            empty.appendChild(CLU.noMatches({
                noun: 'conflicts', query: state.query,
                onClear: function () {
                    var input = document.getElementById('sc-search');
                    input.value = '';
                    state.query = '';
                    render();
                }
            }));
            return;
        }
        table.classList.remove('d-none');
        tbody.innerHTML = shown.map(function (p) { return renderRow(p[0], p[1]); }).join('');
    }

    // ---- Parent-folder groups ---------------------------------------------

    function renderGroups() {
        var box = document.getElementById('sc-groups');
        if (!state.groups.length) { box.innerHTML = ''; return; }
        box.innerHTML = state.groups.map(function (g, i) {
            return '<div class="card mb-2"><div class="card-body py-2 d-flex flex-wrap align-items-center gap-2">' +
                '<i class="bi bi-collection text-primary fs-5" aria-hidden="true"></i>' +
                '<div class="me-auto small">' +
                '<strong>' + g.count + ' series</strong> in <code class="text-break">' + esc(g.parent) + '</code>' +
                ' are already mapped under <code class="text-break">' + esc(g.mapped_parent) + '</code>' +
                (g.likely_same ? ' <span class="badge bg-info text-dark">Likely the same folder</span>' : '') +
                '</div>' +
                '<button class="btn btn-sm btn-primary text-nowrap" data-group="' + i + '">' +
                '<i class="bi bi-intersect me-1"></i>Merge all&hellip;</button>' +
                '</div></div>';
        }).join('');
    }

    function openBulk(group) {
        state.bulk = { group: group, items: [] };
        document.getElementById('sc-bulk-parent').textContent = group.parent;
        document.getElementById('sc-bulk-mapped-parent').textContent = group.mapped_parent;
        document.getElementById('sc-bulk-rename').checked = false;
        var go = document.getElementById('sc-bulk-go');
        go.disabled = true;
        go.textContent = 'Merge';
        var body = document.getElementById('sc-bulk-body');
        body.innerHTML = '<div class="text-center text-muted py-4">Working out what would change&hellip;</div>';
        bootstrap.Modal.getOrCreateInstance(document.getElementById('sc-bulk-modal')).show();

        postJson('/api/problem-files/series-conflicts/bulk-plan', { ids: group.ids }).then(function (res) {
            if (!res.ok) throw new Error(res.data.error || 'Could not prepare the dry run');
            var items = res.data.items || [], t = res.data.totals;
            state.bulk.items = items;
            var rows = items.map(function (it) {
                if (!it.ok) {
                    return '<tr class="text-muted"><td>' + esc(it.series_name || ('Conflict ' + it.id)) + '</td>' +
                        '<td colspan="3"><span class="badge bg-secondary">Skipped</span> ' + esc(it.error) + '</td></tr>';
                }
                var s = it.summary;
                var trash = it.trash.length
                    ? '<details><summary class="small">' + s.trash + '</summary><ul class="small text-break mb-0">' +
                      it.trash.map(function (p) { return '<li>' + esc(p) + '</li>'; }).join('') + '</ul></details>'
                    : '0';
                var held = s.held
                    ? '<span class="text-warning" title="' + esc(it.held.map(function (h) { return h.name; }).join(', ')) + '">' +
                      s.held + '</span>'
                    : '0';
                return '<tr><td>' + esc(it.series_name || '') +
                    '<div class="small text-muted text-break">' + esc(it.folder.split('/').pop()) + '</div></td>' +
                    '<td>' + s.move + '</td><td>' + trash + '</td><td>' + held +
                    (it.folder_emptied ? '<div class="small text-muted">folder removed</div>' : '') + '</td></tr>';
            }).join('');
            body.innerHTML =
                '<p class="mb-2"><strong>' + t.series + ' series</strong>: ' + t.move + ' comics move, ' +
                t.trash + ' files go to the trash' + (t.held ? ', ' + t.held + ' stay where they are (name taken)' : '') + '.</p>' +
                '<div class="table-responsive"><table class="table table-sm align-middle mb-0">' +
                '<thead><tr><th>Series</th><th>Move</th><th>Trash</th><th>Left in place</th></tr></thead>' +
                '<tbody>' + rows + '</tbody></table></div>';
            go.disabled = !t.series;
            go.textContent = 'Merge ' + t.series + ' series';
        }).catch(function (err) {
            body.innerHTML = '<p class="text-danger mb-0">' + esc(err.message) + '</p>';
        });
    }

    function runBulk() {
        var bulk = state.bulk;
        if (!bulk) return;
        var items = bulk.items.filter(function (it) { return it.ok; })
            .map(function (it) { return { id: it.id, signature: it.signature }; });
        var go = document.getElementById('sc-bulk-go');
        go.disabled = true;
        postJson('/api/problem-files/series-conflicts/bulk-merge', {
            items: items, rename: document.getElementById('sc-bulk-rename').checked
        }).then(function (res) {
            go.disabled = false;
            if (!res.ok) {
                CLU.toast(res.data.error || 'Could not start the merge', 'error');
                if (res.status === 409) openBulk(bulk.group);
                return;
            }
            bootstrap.Modal.getOrCreateInstance(document.getElementById('sc-bulk-modal')).hide();
            CLU.toast((res.data.queued ? 'Queued behind another file job: ' : 'Merging ') +
                res.data.series + ' series. Progress shows in the header.', 'success');
            followOperation(res.data.op_id, function (op) {
                var detail = op && op.detail ? op.detail : 'Finished';
                var failed = op && (op.status === 'error' || / failed/.test(detail));
                CLU.toast('Merge all: ' + detail + (failed ? '. The app log says why.' : ''),
                    failed ? 'error' : 'success');
                load();
            });
        });
    }

    function load() {
        var url = '/api/problem-files/series-conflicts' + (state.showIgnored ? '?include_ignored=1' : '');
        return fetch(url)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) throw new Error(data.error || 'Could not load the conflicts');
                state.rows = data.conflicts || [];
                state.groups = data.groups || [];
                renderGroups();
                render();
            })
            .catch(function (err) {
                document.getElementById('sc-tbody').innerHTML =
                    '<tr><td colspan="4" class="text-center text-danger py-4">' + esc(err.message) + '</td></tr>';
            });
    }

    function act(url, body, okMessage) {
        return postJson(url, body).then(function (res) {
            if (!res.ok) {
                CLU.toast(res.data.error || 'That did not work', 'error');
                return load();
            }
            if (okMessage) CLU.toast(okMessage, 'success');
            return load();
        });
    }

    function openSwitch(row) {
        state.pending = row;
        document.getElementById('sc-switch-series').textContent = row.series_name || ('Series ' + row.series_id);
        document.getElementById('sc-switch-new').textContent = row.folder;
        document.getElementById('sc-switch-old').textContent = row.mapped_folder || '(no folder is mapped now)';
        bootstrap.Modal.getOrCreateInstance(document.getElementById('sc-switch-modal')).show();
    }

    // ---- Compare & merge -------------------------------------------------
    //
    // state.merge = { row, plan, keep: {issueKey: 'here'|'mapped'}, mode }.
    // Keeper radios are read by index into plan.pairs. The confirmation step
    // asks the server for the plan with these choices applied and lists its
    // actions verbatim, so what the dialog says is what the job does.

    function $(id) { return document.getElementById(id); }

    function size(n) { return CLU.formatFileSize ? CLU.formatFileSize(n || 0) : (n || 0) + ' B'; }

    // One copy of an issue in both folders, as a button: the kept one is a
    // filled green card marked Keep, the other is dimmed and marked Trash.
    function pickCard(p, i, side) {
        var f = p[side];
        var kept = (state.merge.keep[p.key] || p.keep) === side;
        var cls = kept ? 'btn-success' : 'btn-outline-secondary opacity-75';
        var tag = kept
            ? '<span class="fw-semibold"><i class="bi bi-check-circle-fill me-1"></i>Keep</span>'
            : '<span><i class="bi bi-trash me-1"></i>Trash</span>';
        return '<button type="button" class="btn ' + cls + ' w-100 text-start sc-pick"' +
            ' data-pair="' + i + '" data-side="' + side + '" aria-pressed="' + kept + '">' +
            '<div class="d-flex justify-content-between align-items-center gap-2 small mb-1">' + tag +
            (p.default === side ? '<span class="badge rounded-pill ' + (kept ? 'bg-light text-success' : 'bg-secondary') +
                '">Suggested</span>' : '') + '</div>' +
            '<div class="text-break small">' + esc(f.name) + '</div>' +
            '<div class="small">' + esc(size(f.size)) +
            (f.damaged ? ' <span class="badge bg-danger ms-1">Damaged</span>' : '') + '</div>' +
            '</button>';
    }

    function section(title, count, inner, open) {
        return '<details class="mb-3"' + (open ? ' open' : '') + '>' +
            '<summary class="fw-semibold mb-2">' + esc(title) +
            ' <span class="badge bg-secondary">' + count + '</span></summary>' + inner + '</details>';
    }

    function fileList(items, extra) {
        if (!items.length) return '<p class="small text-muted ms-3">None.</p>';
        return '<ul class="list-unstyled small ms-3 mb-0">' + items.map(function (m) {
            return '<li class="text-break">' + esc(m.name) +
                (m.size != null ? ' <span class="text-muted">' + esc(size(m.size)) + '</span>' : '') +
                (extra ? extra(m) : '') + '</li>';
        }).join('') + '</ul>';
    }

    function renderPlan() {
        var m = state.merge, plan = m.plan;
        var pairs = plan.pairs.map(function (p, i) {
            return '<tr><td class="text-nowrap">' + esc(p.label) + '</td>' +
                '<td>' + pickCard(p, i, 'here') + '</td><td>' + pickCard(p, i, 'mapped') + '</td></tr>';
        }).join('');
        var pairsHtml = plan.pairs.length
            ? '<p class="small text-muted mb-2">Click the copy to keep. The other one goes to the trash.</p>' +
              '<div class="table-responsive"><table class="table table-sm table-borderless align-middle mb-0">' +
              '<thead><tr><th>Issue</th><th>This folder</th><th>Mapped folder</th></tr></thead>' +
              '<tbody id="sc-merge-pairs">' + pairs + '</tbody></table></div>'
            : '<p class="small text-muted ms-3">None.</p>';

        var dupes = plan.actions.filter(function (a) {
            return a.op === 'trash' && !plan.pairs.some(function (p) { return p.here.path === a.trash; });
        });

        var html =
            section('In both folders: pick the copy to keep', plan.pairs.length, pairsHtml, true) +
            section('Only in this folder: moved into the mapped folder', plan.only_here.length,
                fileList(plan.only_here), true);
        if (dupes.length) {
            html += section('Identical file already in the mapped folder: trashed', dupes.length,
                fileList(dupes.map(function (a) { return { name: a.trash.split('/').pop() }; })), false);
        }
        if (plan.held.length) {
            html += section('Left where they are', plan.held.length,
                fileList(plan.held, function (h) {
                    return '<div class="text-warning">' + esc(h.reason) + '</div>';
                }), true);
        }
        html += section('Only in the mapped folder: untouched', plan.only_mapped.length,
            fileList(plan.only_mapped), false);
        $('sc-merge-body').innerHTML = html;

        var canMerge = plan.pairs.length || plan.only_here.length || dupes.length;
        $('sc-merge-go').disabled = !canMerge;
        $('sc-merge-drop').disabled = !(plan.pairs.length || dupes.length);
    }

    function showStep(confirming) {
        $('sc-merge-review').classList.toggle('d-none', confirming);
        $('sc-merge-confirm').classList.toggle('d-none', !confirming);
        $('sc-merge-go').classList.toggle('d-none', confirming);
        $('sc-merge-drop').classList.toggle('d-none', confirming);
        $('sc-merge-confirm-go').classList.toggle('d-none', !confirming);
        $('sc-merge-back').classList.toggle('d-none', !confirming);
        // Drop moves nothing, so there is nothing to rename.
        $('sc-merge-rename-wrap').classList.toggle('invisible',
            !!(confirming && state.merge && state.merge.mode === 'drop'));
    }

    function planUrl(row) { return '/api/problem-files/series-conflicts/' + encodeURIComponent(row.id) + '/plan'; }

    function openMerge(row) {
        state.merge = { row: row, plan: null, keep: {}, mode: 'merge' };
        $('sc-merge-title').textContent = 'Compare folders: ' + (row.series_name || ('Series ' + row.series_id));
        $('sc-merge-folder').textContent = row.folder;
        $('sc-merge-mapped').textContent = row.mapped_folder || '';
        $('sc-merge-body').innerHTML = '<div class="text-center text-muted py-4">Loading&hellip;</div>';
        $('sc-merge-rename').checked = false;
        $('sc-merge-go').disabled = true;
        $('sc-merge-drop').disabled = true;
        showStep(false);
        bootstrap.Modal.getOrCreateInstance($('sc-merge-modal')).show();
        fetch(planUrl(row)).then(function (r) {
            return r.json().then(function (d) { return { ok: r.ok && d.success, data: d }; });
        }).then(function (res) {
            if (!res.ok) throw new Error(res.data.error || 'Could not compare the folders');
            state.merge.plan = res.data.plan;
            renderPlan();
        }).catch(function (err) {
            $('sc-merge-body').innerHTML = '<p class="text-danger mb-0">' + esc(err.message) + '</p>';
        });
    }

    function askConfirm(mode) {
        var m = state.merge;
        m.mode = mode;
        postJson(planUrl(m.row), { mode: mode, keep: m.keep }).then(function (res) {
            if (!res.ok) {
                CLU.toast(res.data.error || 'Could not prepare the merge', 'error');
                return;
            }
            var plan = res.data.plan, sum = res.data.summary;
            if (plan.signature !== m.plan.signature) {
                CLU.toast('The folders changed since you opened this. Showing them again.', 'error');
                m.plan = plan;
                renderPlan();
                return;
            }
            m.confirmPlan = plan;
            var trash = [], moves = [];
            plan.actions.forEach(function (a) {
                if (a.trash) trash.push(a.trash);
                if (a.src) moves.push(a.src);
            });
            var lead = mode === 'merge'
                ? sum.move + (sum.move === 1 ? ' comic moves' : ' comics move') + ' into the mapped folder and ' +
                  sum.trash + (sum.trash === 1 ? ' file goes' : ' files go') + ' to the trash.'
                : sum.trash + (sum.trash === 1 ? ' file' : ' files') +
                  ' in this folder ' + (sum.trash === 1 ? 'goes' : 'go') + ' to the trash. Nothing is moved.';
            if (plan.folder_emptied) lead += ' This folder is then empty of comics and goes to the trash as well.';
            if (sum.held) lead += ' ' + sum.held + ' stay where they are (name already taken).';
            $('sc-merge-confirm-lead').textContent = lead;
            $('sc-merge-confirm-list').innerHTML = trash.length
                ? '<p class="fw-semibold small mb-1">Going to the trash:</p>' +
                  '<ul class="small text-break mb-2">' + trash.map(function (p) { return '<li>' + esc(p) + '</li>'; }).join('') + '</ul>'
                : '';
            $('sc-merge-confirm-trash-note').textContent =
                'Trashed files can be restored from the trash while it has room for them.';
            $('sc-merge-confirm-go').textContent = mode === 'merge' ? 'Merge' : 'Drop duplicates';
            showStep(true);
        });
    }

    function followOperation(opId, done) {
        var busy = false;
        var timer = setInterval(function () {
            if (busy) return;
            busy = true;
            fetch('/api/operation/' + encodeURIComponent(opId)).then(function (r) { return r.json(); })
                .then(function (data) {
                    var op = data.operation || null;
                    if (op && (op.status === 'running' || op.status === 'queued')) return;
                    clearInterval(timer);
                    done(op);
                }).catch(function () {}).finally(function () { busy = false; });
        }, 2000);
        setTimeout(function () { clearInterval(timer); }, 1800000);
    }

    function runMerge() {
        var m = state.merge;
        var btn = $('sc-merge-confirm-go');
        btn.disabled = true;
        postJson('/api/problem-files/series-conflicts/merge', {
            id: m.row.id, mode: m.mode, keep: m.keep,
            rename: m.mode === 'merge' && $('sc-merge-rename').checked,
            signature: m.confirmPlan.signature
        }).then(function (res) {
            btn.disabled = false;
            if (!res.ok) {
                CLU.toast(res.data.error || 'Could not start the merge', 'error');
                return;
            }
            bootstrap.Modal.getOrCreateInstance($('sc-merge-modal')).hide();
            CLU.toast(res.data.queued ? 'Queued behind another file job.' : 'Started. Progress shows in the header.', 'success');
            followOperation(res.data.op_id, function (op) {
                var detail = op && op.detail ? op.detail : 'Finished';
                var failed = op && (op.status === 'error' || / failed/.test(detail));
                CLU.toast((m.mode === 'merge' ? 'Merge: ' : 'Drop: ') + detail +
                    (failed ? '. The app log says why.' : ''), failed ? 'error' : 'success');
                load();
            });
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        $('sc-merge-modal').addEventListener('click', function (e) {
            var card = e.target.closest('button.sc-pick');
            if (!card || !state.merge || !state.merge.plan) return;
            var i = parseInt(card.dataset.pair, 10);
            var pair = state.merge.plan.pairs[i];
            if (!pair) return;
            state.merge.keep[pair.key] = card.dataset.side === 'here' ? 'here' : 'mapped';
            var row = card.closest('tr');
            row.cells[1].innerHTML = pickCard(pair, i, 'here');
            row.cells[2].innerHTML = pickCard(pair, i, 'mapped');
        });
        $('sc-merge-go').addEventListener('click', function () { askConfirm('merge'); });
        $('sc-merge-drop').addEventListener('click', function () { askConfirm('drop'); });
        $('sc-merge-confirm-go').addEventListener('click', runMerge);
        $('sc-merge-back').addEventListener('click', function () { showStep(false); });
        $('sc-groups').addEventListener('click', function (e) {
            var btn = e.target.closest('button[data-group]');
            if (!btn) return;
            var group = state.groups[parseInt(btn.dataset.group, 10)];
            if (group) openBulk(group);
        });
        $('sc-bulk-go').addEventListener('click', runBulk);

        document.getElementById('sc-tbody').addEventListener('click', function (e) {
            var btn = e.target.closest('button[data-act]');
            if (!btn) return;
            var row = state.rows[parseInt(btn.dataset.idx, 10)];
            if (!row) return;
            var which = btn.dataset.act;
            if (which === 'switch') return openSwitch(row);
            if (which === 'merge') return openMerge(row);
            btn.disabled = true;
            if (which === 'ignore') {
                act('/api/problem-files/series-conflicts/ignore', { id: row.id, ignored: true },
                    'Ignored. It comes back if a comic is added to or removed from the folder.');
            } else if (which === 'unignore') {
                act('/api/problem-files/series-conflicts/ignore', { id: row.id, ignored: false });
            } else if (which === 'remove') {
                act('/api/problem-files/series-conflicts/remove', { id: row.id });
            }
        });

        document.getElementById('sc-switch-confirm').addEventListener('click', function () {
            var row = state.pending;
            if (!row) return;
            var btn = this;
            btn.disabled = true;
            postJson('/api/problem-files/series-conflicts/switch', { id: row.id }).then(function (res) {
                btn.disabled = false;
                bootstrap.Modal.getOrCreateInstance(document.getElementById('sc-switch-modal')).hide();
                state.pending = null;
                if (!res.ok) {
                    CLU.toast(res.data.error || 'Could not switch folders', 'error');
                } else {
                    CLU.toast('Series now points at this folder. Matching its issues in the background.', 'success');
                }
                return load();
            });
        });

        document.getElementById('sc-show-ignored').addEventListener('change', function () {
            state.showIgnored = this.checked;
            load();
        });
        CLU.bindSearch(document.getElementById('sc-search'), function (q) {
            state.query = q;
            render();
        });

        load();
    });
})();
