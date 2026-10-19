/**
 * Duplicate Files review page.
 *
 * DOM contract (templates/library_duplicates.html):
 *   #dup-groups, #dup-summary, #dup-show-dismissed, and the modal
 *   #dup-confirm-modal with #dup-confirm-keep / #dup-confirm-trash /
 *   #dup-confirm-go. The row and "Compare" buttons open #cbzDupeModal
 *   (partials/modal_cbz_dupe.html, static/js/clu-cbz-dupe.js).
 *
 * Groups and their members are looked up by index into state.groups, never by
 * a path stored in an attribute: the HTML parser rewrites some bytes inside
 * attribute values, so a path read back out of the DOM is not always the one
 * that went in (see static/js/problem_files.js). The server re-derives and
 * validates the group on every resolve, so a stale page cannot trash a file
 * that is no longer a duplicate.
 */
(function () {
    'use strict';

    var state = { groups: [], includeDismissed: false, pending: null };

    function escapeHtml(str) {
        return String(str === null || str === undefined ? '' : str)
            .replace(/[&<>"']/g, function (c) {
                return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
            });
    }

    function formatSize(bytes) {
        var n = Number(bytes || 0);
        var units = ['B', 'KB', 'MB', 'GB', 'TB'];
        var i = 0;
        while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
        return (i === 0 ? n : n.toFixed(1)) + ' ' + units[i];
    }

    function formatDate(epoch) {
        if (!epoch) return '';
        return new Date(epoch * 1000).toLocaleDateString();
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

    function showModal(id) {
        var el = document.getElementById(id);
        if (el) bootstrap.Modal.getOrCreateInstance(el).show();
    }

    function hideModal(id) {
        var el = document.getElementById(id);
        if (el) bootstrap.Modal.getOrCreateInstance(el).hide();
    }

    function badges(m) {
        var out = '';
        if (m.status === 'damaged') out += ' <span class="badge bg-danger">Damaged</span>';
        out += m.has_comicinfo
            ? ' <span class="badge bg-success">Tagged</span>'
            : ' <span class="badge bg-secondary">No ComicInfo</span>';
        if (m.provider) out += ' <span class="badge bg-light text-dark border">' + escapeHtml(m.provider) + '</span>';
        return out;
    }

    // A pair opens side by side in #cbzDupeModal; a larger group shows the one
    // file. Every format gets the button: pages and file facts work for a .cbr
    // too, only its ComicInfo cannot be read.
    function infoButton(gi, mi, m, pair) {
        return '<button class="btn btn-sm btn-outline-secondary" data-act="info"' +
            ' data-group="' + gi + '" data-member="' + mi + '"' +
            ' title="' + (pair ? 'Compare these copies' : 'View file details') + '">' +
            '<i class="bi ' + (pair ? 'bi-layout-split' : 'bi-info-circle') + '"></i></button>';
    }

    function renderGroup(g, gi) {
        var pair = g.members.length === 2;
        var rows = g.members.map(function (m, mi) {
            var checked = m.path === g.keep_suggestion ? ' checked' : '';
            return '<tr>' +
                '<td class="text-center" style="width:1%;">' +
                    '<input class="form-check-input" type="radio" name="keep-' + gi + '"' +
                    ' data-group="' + gi + '" data-member="' + mi + '"' + checked +
                    ' title="Keep this copy"></td>' +
                '<td><div class="text-break"><strong>' + escapeHtml(m.name) + '</strong>' +
                    badges(m) + '</div>' +
                    '<div class="small text-muted text-break">' + escapeHtml(m.folder) + '</div></td>' +
                '<td class="text-nowrap text-end">' + formatSize(m.size) + '</td>' +
                '<td class="text-nowrap">' + formatDate(m.modified_at) + '</td>' +
                '<td class="text-nowrap">' + infoButton(gi, mi, m, pair) + '</td>' +
                '</tr>';
        }).join('');

        var dismissedBadge = '';
        if (g.dismissed) {
            dismissedBadge = g.dismiss_mode === 'scan'
                ? '<span class="badge bg-secondary ms-2" title="Comes back if the next library health scan still finds it">' +
                    'Dismissed until next scan</span>'
                : '<span class="badge bg-secondary ms-2">Marked not duplicates</span>';
        }

        return '<div class="card mb-3" id="dup-card-' + gi + '">' +
            '<div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">' +
                '<span><span class="badge ' + (g.kind === 'exact' ? 'bg-danger' : 'bg-warning text-dark') + ' me-2">' +
                    (g.kind === 'exact' ? 'Exact' : 'Likely') + '</span>' +
                    escapeHtml(g.reason) + dismissedBadge + '</span>' +
                '<span class="small text-muted">' + formatSize(g.wasted_bytes) + ' reclaimable</span>' +
            '</div>' +
            '<div class="table-responsive"><table class="table table-sm align-middle mb-0">' +
                '<thead><tr><th>Keep</th><th>File</th><th class="text-end">Size</th><th>Modified</th><th></th></tr></thead>' +
                '<tbody>' + rows + '</tbody></table></div>' +
            '<div class="card-footer d-flex flex-wrap gap-2 justify-content-end">' +
                (pair
                    ? '<button class="btn btn-sm btn-outline-primary me-auto" data-act="compare" data-group="' + gi + '">' +
                        '<i class="bi bi-layout-split me-1"></i>Compare</button>'
                    : '') +
                (g.dismissed
                    ? '<button class="btn btn-sm btn-outline-secondary" data-act="restore" data-group="' + gi + '">' +
                        '<i class="bi bi-eye me-1"></i>Restore</button>'
                    : '<button class="btn btn-sm btn-outline-secondary" data-act="dismiss-scan" data-group="' + gi + '"' +
                        ' title="Already sorted out elsewhere (e.g. the File Manager). Hidden until the next' +
                        ' library health scan, and shown again only if that scan still finds it.">' +
                        '<i class="bi bi-clock-history me-1"></i>Dismiss</button>' +
                      '<button class="btn btn-sm btn-outline-secondary" data-act="dismiss" data-group="' + gi + '"' +
                        ' title="These are different comics. Hidden for good, unless another copy turns up.">' +
                        '<i class="bi bi-eye-slash me-1"></i>Not duplicates</button>') +
                '<button class="btn btn-sm btn-danger" data-act="resolve" data-group="' + gi + '">' +
                    '<i class="bi bi-trash me-1"></i>Keep selected, trash the rest</button>' +
            '</div></div>';
    }

    var EMPTY_HTML = '<div class="text-center text-muted py-4"><i class="bi bi-check-circle me-2"></i>Nothing to review.</div>';
    var totals = { total: 0, truncated: false };

    // Computed from what is still on screen, so a card removed optimistically
    // is reflected without asking the server to re-derive every group.
    function updateSummary() {
        var summary = document.getElementById('dup-summary');
        if (!summary) return;
        var live = state.groups.filter(function (g) { return !g.removed; });
        var wasted = live.reduce(function (n, g) {
            return n + (g.kind === 'exact' ? Number(g.wasted_bytes || 0) : 0);
        }, 0);
        summary.textContent = live.length
            ? (live.length + ' groups' + (totals.truncated ? ' (showing the first ' + state.groups.length + ' of ' + totals.total + ')' : '') +
               ' · ' + formatSize(wasted) + ' in exact duplicates')
            : 'No duplicates found among the files the health scan has checked so far.';
    }

    function render(data) {
        var host = document.getElementById('dup-groups');
        if (!host) return;
        state.groups = (data.exact || []).concat(data.likely || []);
        totals = { total: data.total_groups, truncated: !!data.truncated };
        updateSummary();
        host.innerHTML = state.groups.length ? state.groups.map(renderGroup).join('') : EMPTY_HTML;
    }

    function cardEl(gi) { return document.getElementById('dup-card-' + gi); }

    function removeCard(gi) {
        state.groups[gi].removed = true;
        var el = cardEl(gi);
        if (el) el.remove();
        updateSummary();
        if (!state.groups.some(function (g) { return !g.removed; })) {
            document.getElementById('dup-groups').innerHTML = EMPTY_HTML;
        }
    }

    // While a request is in flight the card is dimmed and inert, so a second
    // click cannot send the same group twice.
    function setBusy(gi, busy) {
        var el = cardEl(gi);
        if (!el) return;
        el.style.opacity = busy ? '0.5' : '';
        el.style.pointerEvents = busy ? 'none' : '';
    }

    function load() {
        var qs = state.includeDismissed ? '?include_dismissed=1' : '';
        return fetch('/api/problem-files/duplicates' + qs)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) throw new Error(data.error || 'Request failed');
                render(data);
            })
            .catch(function (e) {
                CLU.showError('Could not load duplicates: ' + e.message);
            });
    }

    function selectedKeep(gi) {
        var checked = document.querySelector('input[type=radio][data-group="' + gi + '"]:checked');
        return checked ? parseInt(checked.getAttribute('data-member'), 10) : -1;
    }

    function confirmResolve(gi) {
        var g = state.groups[gi];
        var keepIdx = selectedKeep(gi);
        if (!g || keepIdx < 0) {
            CLU.showError('Choose the copy to keep first.');
            return;
        }
        var keep = g.members[keepIdx].path;
        var trash = g.members.filter(function (m, i) { return i !== keepIdx; })
                             .map(function (m) { return m.path; });
        state.pending = { gi: gi, body: { id: g.id, keep: keep, trash: trash } };
        document.getElementById('dup-confirm-keep').textContent = keep;
        var list = document.getElementById('dup-confirm-trash');
        list.innerHTML = trash.map(function (p) { return '<li>' + escapeHtml(p) + '</li>'; }).join('');
        showModal('dup-confirm-modal');
    }

    function doResolve() {
        var p = state.pending;
        state.pending = null;
        hideModal('dup-confirm-modal');
        if (!p) return;
        var gi = p.gi;
        setBusy(gi, true);
        postJson('/api/problem-files/duplicates/resolve', p.body).then(function (res) {
            var d = res.data || {};
            if (d.success) {
                removeCard(gi);
                CLU.showSuccess('Moved ' + p.body.trash.length + ' file(s) to the trash.');
                return;
            }
            // Anything short of full success: the group may have changed on the
            // server, so show what is really left.
            if (d.error) {
                CLU.showError(d.error);
            } else {
                var failed = (d.results || []).filter(function (r) { return !r.success; });
                CLU.showError(failed.length + ' file(s) could not be trashed: ' +
                              failed.map(function (r) { return r.error; }).join('; '));
            }
            return load();
        }).catch(function () {
            setBusy(gi, false);
            CLU.showError('Could not reach the server.');
        });
    }

    // untilScan: "Dismiss" (resolved elsewhere; back if the next scan still
    // finds it) rather than "Not duplicates" (hidden for good).
    function doDismiss(gi, dismissed, untilScan) {
        var g = state.groups[gi];
        if (!g) return;
        // Update the page now; the server write happens in the background.
        var el = cardEl(gi);
        if (state.includeDismissed) {
            // The group stays listed, so flip it in place (Restore <-> Dismiss).
            g.dismissed = dismissed;
            g.dismiss_mode = dismissed ? (untilScan ? 'scan' : 'not_duplicate') : null;
            if (el) el.outerHTML = renderGroup(g, gi);
        } else {
            removeCard(gi);
        }
        // The paths the user was shown, from state rather than the DOM, so the
        // server can refuse to hide a copy that turned up after the page loaded.
        var seen = g.members.map(function (m) { return m.path; });
        postJson('/api/problem-files/duplicates/dismiss',
                 { id: g.id, dismissed: dismissed, until_scan: !!untilScan, members: seen })
            .then(function (res) {
                var d = res.data || {};
                if (!res.ok || !d.success) {
                    if (d.changed) {
                        CLU.showToast('Group changed',
                            'Another copy has turned up since this page loaded, so it was not dismissed. ' +
                            'The list has been refreshed.', 'warning');
                    } else {
                        CLU.showError(d.error || 'Could not update this group');
                    }
                    return load();
                }
                if (d.gone) {
                    // Fixed elsewhere (e.g. the File Manager) before the click:
                    // nothing left to hide, so the card simply goes.
                    removeCard(gi);
                    CLU.showSuccess('Already resolved: these files are no longer duplicates.');
                } else if (dismissed && untilScan) {
                    CLU.showSuccess('Dismissed. It comes back only if the next library health scan still finds it.');
                }
            })
            .catch(function () {
                CLU.showError('Could not reach the server.');
                return load();
            });
    }

    // The compare view is read-only, so nothing it does can change a group and
    // there is nothing to reload when it closes. A pair is shown side by side
    // (columns in member order); a larger group shows only the clicked file.
    function openInfo(gi, mi) {
        var g = state.groups[gi];
        var m = g && g.members[mi];
        if (!m) return;
        if (!window.CLU || typeof CLU.showCBZDupe !== 'function') {
            CLU.showError('The file comparison viewer is unavailable.');
            return;
        }
        var memberIdx = g.members.length === 2 ? [0, 1] : [mi];
        var keep = selectedKeep(gi);
        CLU.showCBZDupe(memberIdx.map(function (i) { return g.members[i]; }), {
            keepIndex: memberIdx.indexOf(keep),
            onKeep: function (col) { keepFromModal(gi, memberIdx[col]); }
        });
    }

    // Select the copy, close the viewer, then ask for the usual confirmation --
    // only once the viewer has gone, so two modals never stack.
    function keepFromModal(gi, mi) {
        var radio = document.querySelector('input[type=radio][data-group="' + gi + '"][data-member="' + mi + '"]');
        if (radio) radio.checked = true;
        var el = document.getElementById('cbzDupeModal');
        if (!el || !el.classList.contains('show')) { confirmResolve(gi); return; }
        el.addEventListener('hidden.bs.modal', function () { confirmResolve(gi); }, { once: true });
        hideModal('cbzDupeModal');
    }

    function init() {
        if (!document.getElementById('duplicates-app')) return;
        var host = document.getElementById('dup-groups');
        host.addEventListener('click', function (ev) {
            var btn = ev.target.closest('[data-act]');
            if (!btn) return;
            var gi = parseInt(btn.getAttribute('data-group'), 10);
            if (btn.getAttribute('data-act') === 'info') openInfo(gi, parseInt(btn.getAttribute('data-member'), 10));
            else if (btn.getAttribute('data-act') === 'compare') openInfo(gi, 0);
            else if (btn.getAttribute('data-act') === 'resolve') confirmResolve(gi);
            else if (btn.getAttribute('data-act') === 'dismiss-scan') doDismiss(gi, true, true);
            else if (btn.getAttribute('data-act') === 'dismiss') doDismiss(gi, true, false);
            else if (btn.getAttribute('data-act') === 'restore') doDismiss(gi, false, false);
        });
        var toggle = document.getElementById('dup-show-dismissed');
        if (toggle) {
            toggle.addEventListener('change', function () {
                state.includeDismissed = toggle.checked;
                load();
            });
        }
        var go = document.getElementById('dup-confirm-go');
        if (go) go.addEventListener('click', doResolve);
        load();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
