(function () {
    function escapeHtml(str) {
        return String(str ?? '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    function formatBytes(n) {
        if (typeof n !== 'number' || isNaN(n)) return '–';
        if (n < 1024) return `${n} B`;
        const units = ['KB', 'MB', 'GB', 'TB'];
        let i = -1;
        let size = n;
        do { size /= 1024; i++; } while (size >= 1024 && i < units.length - 1);
        return `${size.toFixed(size >= 100 ? 0 : size >= 10 ? 1 : 2)} ${units[i]}`;
    }

    function formatTimestamp(epochSeconds) {
        if (!epochSeconds) return '–';
        try {
            return new Date(epochSeconds * 1000).toLocaleString();
        } catch (e) {
            return '–';
        }
    }

    function formatNumber(n) {
        if (typeof n !== 'number') return '–';
        return n.toLocaleString();
    }

    async function postJson(url, body) {
        const opts = { method: 'POST', headers: { 'Content-Type': 'application/json' } };
        if (body !== undefined) opts.body = JSON.stringify(body);
        const res = await fetch(url, opts);
        let data = {};
        try { data = await res.json(); } catch (e) { /* ignore */ }
        return { ok: res.ok, status: res.status, data };
    }

    function toast(message, type, duration) {
        // showToast is defined in config.html's inline script. Optional-chained
        // because this file loads first; the important results are also written
        // into the page, never only into a toast.
        window.showToast?.(message, type || 'info', duration || 5000);
    }

    // ---- Confirmation modal -------------------------------------------------
    // CLAUDE.md forbids window.confirm/alert/prompt outright. One parameterised
    // modal serves restore, delete, compact, salvage-apply and discard; the
    // salvage one needs a whole diff table in its body, which is why this is not
    // CLU.showDeleteConfirmation().
    let confirmHandler = null;

    function showConfirm({ title, bodyHtml, confirmLabel, variant }, onConfirm) {
        const modalEl = document.getElementById('dbConfirmModal');
        if (!modalEl) { onConfirm(); return; }
        document.getElementById('dbConfirmModalLabel').textContent = title;
        document.getElementById('dbConfirmModalBody').innerHTML = bodyHtml;
        const btn = document.getElementById('dbConfirmModalBtn');
        btn.textContent = confirmLabel || 'Confirm';
        btn.className = `btn btn-${variant || 'danger'}`;
        confirmHandler = onConfirm;
        bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }

    function hideConfirm() {
        const modalEl = document.getElementById('dbConfirmModal');
        if (modalEl) bootstrap.Modal.getOrCreateInstance(modalEl).hide();
    }

    // ---- Rendering ----------------------------------------------------------

    function renderIntegrity(stats) {
        const badge = document.getElementById('dbIntegrityBadge');
        const alertBox = document.getElementById('dbIntegrityAlert');
        const alertMsg = document.getElementById('dbIntegrityAlertMsg');
        if (!badge) return;

        const integrity = stats?.integrity;
        const lastKnown = stats?.last_known_integrity;
        // Red if EITHER is bad. quick_check does not read every page, so a
        // database that threw "malformed" an hour ago can still pass it -- a
        // passing check must never silently clear a latched failure.
        const liveOk = integrity ? integrity.ok !== false : true;
        const latchedOk = lastKnown ? lastKnown.ok !== false : true;
        const ok = liveOk && latchedOk;

        badge.classList.remove('d-none', 'bg-success', 'bg-danger');
        const checkedEl = document.getElementById('dbStatsCheckedAt');
        if (checkedEl) {
            checkedEl.textContent = lastKnown?.checked_at
                ? formatTimestamp(lastKnown.checked_at)
                : '–';
        }

        if (ok) {
            badge.classList.add('bg-success');
            badge.textContent = 'Healthy';
            alertBox?.classList.add('d-none');
            return;
        }

        badge.classList.add('bg-danger');
        badge.textContent = 'Corrupted — salvage or restore';
        if (alertBox && alertMsg) {
            const detail = (!liveOk && integrity?.error)
                ? integrity.error
                : (lastKnown?.error || '');
            alertMsg.textContent =
                'Database integrity check failed' +
                (detail ? `: ${detail}` : '') +
                (liveOk && !latchedOk
                    ? '. A later quick check passed, but the failure above really happened — quick checks do not read every page.'
                    : '') +
                ' Use Salvage below, or restore a backup, then restart the app.';
            alertBox.classList.remove('d-none');
        }
    }

    function renderStorage(storage) {
        const el = document.getElementById('dbStatsStorage');
        const alertBox = document.getElementById('dbStorageAlert');
        const alertMsg = document.getElementById('dbStorageAlertMsg');
        if (!el) return;

        if (!storage || !storage.fstype) {
            el.textContent = 'unknown';
            alertBox?.classList.add('d-none');
            return;
        }
        el.textContent = `${storage.fstype}${storage.mountpoint ? ` at ${storage.mountpoint}` : ''}`;

        alertBox?.classList.remove('d-none', 'alert-danger', 'alert-warning', 'alert-secondary');
        if (storage.risk === 'ok' || !alertBox || !alertMsg) {
            alertBox?.classList.add('d-none');
            return;
        }
        alertBox.classList.add(storage.risk === 'danger' ? 'alert-danger' : 'alert-warning');
        alertMsg.textContent = storage.note || '';
    }

    function renderPages(pages, dbSize) {
        const reclaimEl = document.getElementById('dbStatsReclaimable');
        const pragmaEl = document.getElementById('dbStatsPragmas');
        if (reclaimEl) {
            const bytes = pages?.reclaimable_bytes;
            if (typeof bytes === 'number') {
                const pct = dbSize ? ` (${Math.round((bytes / dbSize) * 100)}%)` : '';
                reclaimEl.textContent = `${formatBytes(bytes)}${pct}`;
            } else {
                reclaimEl.textContent = '–';
            }
        }
        if (pragmaEl) {
            const sync = { 0: 'OFF', 1: 'NORMAL', 2: 'FULL', 3: 'EXTRA' };
            pragmaEl.textContent = pages?.journal_mode
                ? `${String(pages.journal_mode).toUpperCase()} / ${sync[pages.synchronous] ?? pages.synchronous ?? '?'}`
                : '–';
        }
    }

    function renderErrors(errors) {
        const tbody = document.getElementById('dbErrorsBody');
        if (!tbody) return;
        if (!errors || errors.length === 0) {
            tbody.innerHTML = '<tr><td colspan="4" class="text-muted text-center">No database errors recorded</td></tr>';
            return;
        }
        tbody.innerHTML = errors.map(e => `
            <tr class="${e.kind === 'corruption' ? 'table-danger' : ''}">
                <td class="text-nowrap">${escapeHtml(formatTimestamp(e.ts))}</td>
                <td>${escapeHtml(e.kind)}</td>
                <td><code>${escapeHtml(e.context || '–')}</code></td>
                <td class="small">${escapeHtml(e.message)}</td>
            </tr>
        `).join('');
    }

    function renderStats(stats, lastBackup) {
        renderIntegrity(stats);
        renderStorage(stats?.storage);
        renderPages(stats?.pages, stats?.db_size);
        renderErrors(stats?.errors);

        document.getElementById('dbStatsPath').textContent = stats?.db_path || '–';
        document.getElementById('dbStatsSize').textContent = formatBytes(stats?.db_size);
        const wal = stats?.wal_size || 0;
        const shm = stats?.shm_size || 0;
        document.getElementById('dbStatsWal').textContent =
            (wal === 0 && shm === 0) ? '–' : `${formatBytes(wal)} / ${formatBytes(shm)}`;
        const tables = stats?.tables || [];
        document.getElementById('dbStatsTableCount').textContent = formatNumber(tables.length);
        document.getElementById('dbStatsTotalRows').textContent = formatNumber(stats?.total_rows || 0);

        const tbody = document.getElementById('dbStatsTablesBody');
        if (tables.length === 0) {
            tbody.innerHTML = '<tr><td colspan="2" class="text-muted text-center">No tables</td></tr>';
        } else {
            tbody.innerHTML = tables.map(t => `
                <tr>
                    <td><code>${escapeHtml(t.name)}</code></td>
                    <td class="text-end">${t.rows === null ? '<span class="text-muted">err</span>' : formatNumber(t.rows)}</td>
                </tr>
            `).join('');
        }

        document.getElementById('dbLastBackup').textContent = lastBackup
            ? `${lastBackup.filename} — ${formatTimestamp(lastBackup.modified_at)}`
            : 'no backups yet';
    }

    function renderBackups(backups) {
        const tbody = document.getElementById('dbBackupsBody');
        if (!backups || backups.length === 0) {
            tbody.innerHTML = '<tr><td colspan="4" class="text-muted text-center">No backups yet</td></tr>';
            return;
        }
        tbody.innerHTML = backups.map(b => {
            const fn = escapeHtml(b.filename);
            return `
            <tr>
                <td><code>${fn}</code></td>
                <td>${escapeHtml(formatTimestamp(b.modified_at))}</td>
                <td class="text-end">${formatBytes(b.size)}</td>
                <td class="text-end">
                    <a class="btn btn-sm btn-outline-secondary me-1"
                       href="/api/database/backups/${encodeURIComponent(b.filename)}/download"
                       title="Download">
                        <i class="bi bi-download"></i>
                    </a>
                    <button class="btn btn-sm btn-outline-warning me-1" data-restore="${fn}" title="Restore">
                        <i class="bi bi-arrow-counterclockwise"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-danger" data-delete="${fn}" title="Delete">
                        <i class="bi bi-trash"></i>
                    </button>
                </td>
            </tr>
            `;
        }).join('');
    }

    function renderQuarantine(snapshots) {
        const wrap = document.getElementById('dbQuarantineWrap');
        const tbody = document.getElementById('dbQuarantineBody');
        if (!wrap || !tbody) return;
        if (!snapshots || snapshots.length === 0) {
            wrap.classList.add('d-none');
            return;
        }
        wrap.classList.remove('d-none');
        tbody.innerHTML = snapshots.map(q => `
            <tr>
                <td><code>${escapeHtml(q.filename)}</code></td>
                <td>${escapeHtml(formatTimestamp(q.modified_at))}</td>
                <td class="text-end">${formatBytes(q.size)}</td>
                <td class="text-end">
                    <a class="btn btn-sm btn-outline-secondary"
                       href="/api/database/quarantine/${encodeURIComponent(q.filename)}/download"
                       title="Download">
                        <i class="bi bi-download"></i>
                    </a>
                </td>
            </tr>
        `).join('');
    }

    function deltaCell(row) {
        if (row.missing) {
            return '<span class="text-danger fw-semibold">lost</span>';
        }
        if (typeof row.delta !== 'number') {
            return '<span class="text-muted" title="The original count is unknown, so the change cannot be computed">unknown</span>';
        }
        if (row.delta === 0) return '<span class="text-muted">0</span>';
        const cls = row.delta < 0 ? 'text-danger fw-semibold' : 'text-success';
        const sign = row.delta > 0 ? '+' : '−';
        return `<span class="${cls}">${sign}${formatNumber(Math.abs(row.delta))}</span>`;
    }

    function renderSalvageCandidate(candidate) {
        const wrap = document.getElementById('dbSalvageResult');
        if (!wrap) return;
        if (!candidate) {
            wrap.classList.add('d-none');
            return;
        }
        wrap.classList.remove('d-none');
        wrap.dataset.token = candidate.token || '';

        document.getElementById('dbSalvageMethod').textContent = candidate.method || '–';
        const integrityEl = document.getElementById('dbSalvageIntegrity');
        integrityEl.textContent = candidate.integrity_ok
            ? 'clean' : `FAILED — ${candidate.integrity_message || 'unknown'}`;
        integrityEl.className = candidate.integrity_ok ? 'text-success' : 'text-danger fw-semibold';

        const diff = candidate.diff || {};
        // Tables that could not be counted in the damaged database have an
        // unknown "before", so the loss figure is a floor, not a total. Saying
        // "0 rows lost" when the one broken table was uncountable would defeat
        // the point of showing a diff at all.
        const unknown = (diff.unknown_before || []).length;
        document.getElementById('dbSalvageRowsLost').textContent =
            unknown > 0
                ? `at least ${formatNumber(diff.rows_lost || 0)} (${unknown} table${unknown === 1 ? '' : 's'} could not be counted before)`
                : formatNumber(diff.rows_lost || 0);
        document.getElementById('dbSalvageSize').textContent =
            `${formatBytes(candidate.size_before)} → ${formatBytes(candidate.size_after)}`;

        const tbody = document.getElementById('dbSalvageDiffBody');
        const rows = diff.tables || [];
        tbody.innerHTML = rows.length === 0
            ? '<tr><td colspan="4" class="text-muted text-center">No tables</td></tr>'
            : rows.map(r => `
                <tr>
                    <td><code>${escapeHtml(r.name)}</code></td>
                    <td class="text-end">${r.before === null ? '<span class="text-muted" title="This table could not be counted in the damaged database">unknown</span>' : formatNumber(r.before)}</td>
                    <td class="text-end">${r.after === null ? '<span class="text-muted">—</span>' : formatNumber(r.after)}</td>
                    <td class="text-end">${deltaCell(r)}</td>
                </tr>
            `).join('');

        document.getElementById('dbSalvageApplyBtn').disabled = !candidate.integrity_ok;
    }

    // ---- Data ---------------------------------------------------------------

    async function refresh() {
        try {
            const [statsRes, backupsRes, salvageRes] = await Promise.all([
                fetch('/api/database/stats').then(r => r.json()),
                fetch('/api/database/backups').then(r => r.json()),
                fetch('/api/database/salvage').then(r => r.json()).catch(() => null),
            ]);
            if (statsRes?.success) {
                renderStats(statsRes.stats, statsRes.last_backup);
                renderQuarantine(statsRes.quarantine);
            }
            if (backupsRes?.success) {
                renderBackups(backupsRes.backups);
            }
            if (salvageRes?.success) {
                renderSalvageCandidate(salvageRes.candidate);
            }
        } catch (e) {
            console.warn('database panel refresh failed:', e);
        }
    }

    function showResult(html, variant) {
        const box = document.getElementById('dbMaintenanceResult');
        if (!box) return;
        box.className = `alert alert-${variant || 'info'} small mb-3`;
        box.innerHTML = html;
    }

    async function withBusy(btn, label, fn) {
        if (!btn) return fn();
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>${label}`;
        try {
            return await fn();
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }

    /**
     * Follow a background operation to completion.
     *
     * Polls /api/operation/<id> for progress, never /api/operations -- that one
     * clears the pending notification queue as a side effect, so it can only
     * have the single poller in base.html.
     */
    function followOperation(opId, onProgress) {
        return new Promise((resolve) => {
            const tick = async () => {
                try {
                    const res = await fetch(`/api/operation/${encodeURIComponent(opId)}`);
                    const data = await res.json().catch(() => ({}));
                    const op = data?.operation;
                    if (op && onProgress) onProgress(op);

                    // Registry statuses are 'running' | 'completed' | 'error',
                    // and a finished op is pruned after a TTL -- so a missing
                    // operation means finished, not failed. Either way the
                    // authoritative answer is the stashed result.
                    if (!op || op.status === 'completed' || op.status === 'error') {
                        const resultRes = await fetch(
                            `/api/database/operation/${encodeURIComponent(opId)}`
                        );
                        const resultData = await resultRes.json().catch(() => ({}));
                        if (resultData?.pending) {
                            setTimeout(tick, 1000);
                            return;
                        }
                        resolve(resultData?.result || {});
                        return;
                    }
                    setTimeout(tick, 1000);
                } catch (e) {
                    setTimeout(tick, 2000);
                }
            };
            tick();
        });
    }

    // ---- Actions ------------------------------------------------------------

    async function onBackupNow() {
        await withBusy(document.getElementById('dbBackupNowBtn'), 'Backing up…', async () => {
            const { ok, data } = await postJson('/api/database/backup');
            if (ok && data.success) {
                toast(`Backup created: ${data.filename}`, 'success');
            } else {
                toast(`Backup failed: ${data.error || 'unknown error'}`, 'error');
            }
            await refresh();
        });
    }

    function onRestoreClick(filename) {
        showConfirm({
            title: 'Restore this database?',
            bodyHtml: `
                <p>Restore the database from <code>${escapeHtml(filename)}</code>?</p>
                <p class="mb-0">This replaces your current database. A safety snapshot of
                the current one is taken automatically first. Restart the app afterwards so
                background workers reload their state.</p>`,
            confirmLabel: 'Restore',
            variant: 'warning',
        }, async () => {
            hideConfirm();
            const { ok, data } = await postJson('/api/database/restore', { filename });
            if (ok && data.success) {
                toast(
                    `Restored from ${filename}. Safety snapshot: ${data.pre_restore_backup || 'n/a'}. Restart the app.`,
                    'success', 10000
                );
            } else {
                toast(`Restore failed: ${data.error || 'unknown error'}`, 'error', 10000);
            }
            await refresh();
        });
    }

    function onDeleteClick(filename) {
        showConfirm({
            title: 'Delete this backup?',
            bodyHtml: `<p class="mb-0">Delete <code>${escapeHtml(filename)}</code>? This cannot be undone.</p>`,
            confirmLabel: 'Delete',
        }, async () => {
            hideConfirm();
            const res = await fetch(`/api/database/backups/${encodeURIComponent(filename)}`, { method: 'DELETE' });
            const data = await res.json().catch(() => ({}));
            if (res.ok && data.success) {
                toast(`Deleted ${filename}`, 'success');
            } else {
                toast(`Delete failed: ${data.error || 'unknown error'}`, 'error');
            }
            await refresh();
        });
    }

    async function onIntegrityQuick() {
        await withBusy(document.getElementById('dbIntegrityQuickBtn'), 'Checking…', async () => {
            const { data } = await postJson('/api/database/integrity', { full: false });
            if (data.ok) {
                showResult('<i class="bi bi-check-circle me-1"></i>Quick integrity check passed.', 'success');
            } else {
                showResult(
                    `<i class="bi bi-exclamation-octagon me-1"></i>Integrity check FAILED: ${escapeHtml(data.message || data.error || 'unknown')}`,
                    'danger'
                );
            }
            await refresh();
        });
    }

    async function onIntegrityFull() {
        await withBusy(document.getElementById('dbIntegrityFullBtn'), 'Checking…', async () => {
            const { data } = await postJson('/api/database/integrity', { full: true });
            if (!data.op_id) {
                showResult(`Could not start: ${escapeHtml(data.error || 'unknown error')}`, 'danger');
                return;
            }
            showResult('<span class="spinner-border spinner-border-sm me-1"></span>Running a full integrity check…', 'info');
            const result = await followOperation(data.op_id);
            if (result.ok) {
                showResult('<i class="bi bi-check-circle me-1"></i>Full integrity check passed — every page read cleanly.', 'success');
            } else {
                const lines = (result.lines || []).map(l => `<li><code>${escapeHtml(l)}</code></li>`).join('');
                showResult(
                    `<i class="bi bi-exclamation-octagon me-1"></i><strong>Full integrity check FAILED.</strong>` +
                    (lines ? `<ul class="mb-0 mt-2">${lines}</ul>` : ` ${escapeHtml(result.message || result.error || '')}`),
                    'danger'
                );
            }
            await refresh();
        });
    }

    async function onCheckpoint() {
        await withBusy(document.getElementById('dbCheckpointBtn'), 'Checkpointing…', async () => {
            const { data } = await postJson('/api/database/checkpoint');
            if (!data.success) {
                showResult(`Checkpoint failed: ${escapeHtml(data.error || 'unknown error')}`, 'danger');
            } else if (data.busy) {
                showResult(
                    `<i class="bi bi-info-circle me-1"></i>Checkpoint ran but could not truncate the log: ` +
                    `something is holding a read open (${formatNumber(data.log)} pages, ` +
                    `${formatNumber(data.checkpointed)} moved). This is usually temporary.`,
                    'warning'
                );
            } else {
                showResult(
                    `<i class="bi bi-check-circle me-1"></i>WAL checkpointed: ` +
                    `${formatBytes(data.wal_size_before)} → ${formatBytes(data.wal_size_after)}.`,
                    'success'
                );
            }
            await refresh();
        });
    }

    async function onOptimize() {
        await withBusy(document.getElementById('dbOptimizeBtn'), 'Optimizing…', async () => {
            const { data } = await postJson('/api/database/optimize');
            if (data.success) {
                showResult('<i class="bi bi-check-circle me-1"></i>Statistics refreshed.', 'success');
            } else {
                showResult(`Optimize failed: ${escapeHtml(data.error || 'unknown error')}`, 'danger');
            }
        });
    }

    function onCompact() {
        showConfirm({
            title: 'Compact the database?',
            bodyHtml: `
                <p>This rebuilds the database compactly and installs the result,
                reclaiming space held by deleted rows.</p>
                <p class="mb-0">A safety snapshot is taken first, and the rebuild runs on a
                copy — your current database is not touched until the new one has been
                verified. It can take a few minutes on a large library.</p>`,
            confirmLabel: 'Compact',
            variant: 'warning',
        }, async () => {
            hideConfirm();
            await withBusy(document.getElementById('dbCompactBtn'), 'Compacting…', async () => {
                const { data } = await postJson('/api/database/compact');
                if (!data.op_id) {
                    showResult(`Could not start: ${escapeHtml(data.error || 'unknown error')}`, 'danger');
                    return;
                }
                showResult('<span class="spinner-border spinner-border-sm me-1"></span>Compacting…', 'info');
                const result = await followOperation(data.op_id, op => {
                    if (op.detail) {
                        showResult(
                            `<span class="spinner-border spinner-border-sm me-1"></span>${escapeHtml(op.detail)}…`,
                            'info'
                        );
                    }
                });
                if (result.success) {
                    showResult(
                        `<i class="bi bi-check-circle me-1"></i>Compacted: ` +
                        `${formatBytes(result.size_before)} → ${formatBytes(result.size_after)} ` +
                        `(reclaimed ${formatBytes(result.reclaimed)}). ` +
                        `Snapshot: <code>${escapeHtml(result.pre_swap_backup || 'n/a')}</code>.`,
                        'success'
                    );
                } else {
                    showResult(`Compact failed: ${escapeHtml(result.error || 'unknown error')}`, 'danger');
                }
                await refresh();
            });
        });
    }

    async function onSalvage() {
        await withBusy(document.getElementById('dbSalvageBtn'), 'Salvaging…', async () => {
            const { data } = await postJson('/api/database/salvage');
            if (!data.op_id) {
                showResult(`Could not start salvage: ${escapeHtml(data.error || 'unknown error')}`, 'danger');
                return;
            }
            showResult('<span class="spinner-border spinner-border-sm me-1"></span>Salvaging…', 'info');
            const result = await followOperation(data.op_id, op => {
                if (op.detail) {
                    showResult(
                        `<span class="spinner-border spinner-border-sm me-1"></span>Salvaging: ${escapeHtml(op.detail)}`,
                        'info'
                    );
                }
            });
            if (result.success && result.candidate) {
                showResult(
                    '<i class="bi bi-check-circle me-1"></i>Salvage complete. Review the row ' +
                    'counts below before installing the recovered database.',
                    'success'
                );
                renderSalvageCandidate(result.candidate);
            } else {
                showResult(`Salvage failed: ${escapeHtml(result.error || 'unknown error')}`, 'danger');
            }
            await refresh();
        });
    }

    function onSalvageApply() {
        const wrap = document.getElementById('dbSalvageResult');
        const token = wrap?.dataset.token;
        if (!token) {
            toast('No salvaged database is available.', 'error');
            return;
        }
        const rowsLost = document.getElementById('dbSalvageRowsLost').textContent;
        showConfirm({
            title: 'Install the salvaged database?',
            bodyHtml: `
                <p>This replaces your current database with the recovered copy.</p>
                <p><strong>${escapeHtml(rowsLost)}</strong> rows were not recovered and will be
                lost. Review the per-table breakdown before continuing.</p>
                <p class="mb-0">A snapshot of the current database is taken first, so this can
                be undone from the Backups list. Restart the app afterwards.</p>`,
            confirmLabel: 'Install it',
        }, async () => {
            hideConfirm();
            const { data } = await postJson('/api/database/salvage/apply', { token });
            if (data.success) {
                showResult(
                    '<i class="bi bi-check-circle me-1"></i>Salvaged database installed. ' +
                    `Snapshot of the old one: <code>${escapeHtml(data.pre_swap_backup || 'n/a')}</code>. ` +
                    '<strong>Restart the app</strong> so background workers reload their state.',
                    'success'
                );
                renderSalvageCandidate(null);
            } else {
                showResult(`Install failed: ${escapeHtml(data.error || 'unknown error')}`, 'danger');
            }
            await refresh();
        });
    }

    function onSalvageDiscard() {
        showConfirm({
            title: 'Discard the salvaged copy?',
            bodyHtml: '<p class="mb-0">The recovered database will be deleted. Your current database is unaffected.</p>',
            confirmLabel: 'Discard',
        }, async () => {
            hideConfirm();
            await fetch('/api/database/salvage', { method: 'DELETE' });
            renderSalvageCandidate(null);
            showResult('Salvaged copy discarded.', 'secondary');
            await refresh();
        });
    }

    function bind() {
        document.getElementById('dbConfirmModalBtn')?.addEventListener('click', () => {
            const handler = confirmHandler;
            confirmHandler = null;
            if (handler) handler();
        });

        document.getElementById('dbBackupNowBtn')?.addEventListener('click', onBackupNow);
        document.getElementById('dbIntegrityQuickBtn')?.addEventListener('click', onIntegrityQuick);
        document.getElementById('dbIntegrityFullBtn')?.addEventListener('click', onIntegrityFull);
        document.getElementById('dbCheckpointBtn')?.addEventListener('click', onCheckpoint);
        document.getElementById('dbOptimizeBtn')?.addEventListener('click', onOptimize);
        document.getElementById('dbCompactBtn')?.addEventListener('click', onCompact);
        document.getElementById('dbSalvageBtn')?.addEventListener('click', onSalvage);
        document.getElementById('dbSalvageApplyBtn')?.addEventListener('click', onSalvageApply);
        document.getElementById('dbSalvageDiscardBtn')?.addEventListener('click', onSalvageDiscard);

        document.getElementById('dbBackupsBody')?.addEventListener('click', e => {
            const restoreBtn = e.target.closest('button[data-restore]');
            if (restoreBtn) {
                onRestoreClick(restoreBtn.dataset.restore);
                return;
            }
            const deleteBtn = e.target.closest('button[data-delete]');
            if (deleteBtn) {
                onDeleteClick(deleteBtn.dataset.delete);
            }
        });
    }

    function init() {
        const tabBtn = document.getElementById('database-tab');
        if (!tabBtn) return; // not on the config page
        bind();
        tabBtn.addEventListener('shown.bs.tab', refresh);
        if (tabBtn.classList.contains('active')) refresh();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
