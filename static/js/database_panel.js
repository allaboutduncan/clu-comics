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

    function renderStats(stats, lastBackup) {
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
            ? `${escapeHtml(lastBackup.filename)} — ${formatTimestamp(lastBackup.modified_at)}`
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

    async function refresh() {
        try {
            const [statsRes, backupsRes] = await Promise.all([
                fetch('/api/database/stats').then(r => r.json()),
                fetch('/api/database/backups').then(r => r.json()),
            ]);
            if (statsRes?.success) {
                renderStats(statsRes.stats, statsRes.last_backup);
            }
            if (backupsRes?.success) {
                renderBackups(backupsRes.backups);
            }
        } catch (e) {
            console.warn('database panel refresh failed:', e);
        }
    }

    async function onBackupNow() {
        const btn = document.getElementById('dbBackupNowBtn');
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Backing up…';
        try {
            const { ok, data } = await postJson('/api/database/backup');
            if (ok && data.success) {
                window.showToast?.(`Backup created: ${data.filename}`, 'success');
            } else {
                window.showToast?.(`Backup failed: ${data.error || 'unknown error'}`, 'error');
            }
            await refresh();
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }

    async function onRestoreClick(filename) {
        const ok = window.confirm(
            `Restore the database from "${filename}"?\n\n` +
            `This replaces your current database. A safety snapshot of the current DB is ` +
            `created automatically before restore. Restart the app afterwards to clear ` +
            `cached state in background workers.`
        );
        if (!ok) return;
        try {
            const { ok: httpOk, data } = await postJson('/api/database/restore', { filename });
            if (httpOk && data.success) {
                window.showToast?.(
                    `Restored from ${filename}. Safety snapshot: ${data.pre_restore_backup || 'n/a'}. Restart the app.`,
                    'success', 10000
                );
            } else {
                window.showToast?.(`Restore failed: ${data.error || 'unknown error'}`, 'error');
            }
            await refresh();
        } catch (e) {
            window.showToast?.(`Restore error: ${e.message}`, 'error');
        }
    }

    async function onDeleteClick(filename) {
        const ok = window.confirm(`Delete backup "${filename}"? This cannot be undone.`);
        if (!ok) return;
        try {
            const res = await fetch(`/api/database/backups/${encodeURIComponent(filename)}`, { method: 'DELETE' });
            const data = await res.json().catch(() => ({}));
            if (res.ok && data.success) {
                window.showToast?.(`Deleted ${filename}`, 'success');
            } else {
                window.showToast?.(`Delete failed: ${data.error || 'unknown error'}`, 'error');
            }
            await refresh();
        } catch (e) {
            window.showToast?.(`Delete error: ${e.message}`, 'error');
        }
    }

    function bind() {
        document.getElementById('dbBackupNowBtn')?.addEventListener('click', onBackupNow);
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
