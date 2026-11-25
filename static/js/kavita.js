// Kavita reading sync: the owner's panel on the settings page and each user's
// own link on the account page. Both talk to routes/kavita.py.
//
// URLs are written root-relative; clu-url.js prefixes fetch() when CLU is
// served under a subpath.
(function () {
    'use strict';

    window.CLU = window.CLU || {};

    var DEFAULT_TIME = '05:30';
    var POLL_MS = 3000;

    function byId(id) { return document.getElementById(id); }

    // Resolves to { ok, status, data }. An expired session or a proxy error
    // page is not JSON, so neither is handed to response.json().
    async function request(url, method, payload, signal) {
        var options = { method: method || 'GET', headers: { 'Cache-Control': 'no-cache' } };
        if (signal) options.signal = signal;
        if (payload !== undefined) {
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(payload);
        }
        var response = await fetch(url, options);
        var type = response.headers.get('Content-Type') || '';
        var data = {};
        if (type.includes('application/json')) {
            data = await response.json().catch(function () { return {}; });
        }
        return { ok: response.ok, status: response.status, data: data };
    }

    function errorText(result, fallback) {
        if (result.status === 401) return 'Your session has expired, sign in again.';
        return (result.data && result.data.error) || fallback;
    }

    // Follows a sync the page started until the server says it has finished.
    // The first check waits one interval, so the background thread has taken
    // its claim by the time it is asked.
    function followSync(statusUrl, onDone) {
        var timer = CLU.startPoll(async function (signal) {
            var result = await request(statusUrl, 'GET', undefined, signal);
            if (!result.ok) return;                 // keep polling on a transient error
            if (result.data.running) return;
            clearInterval(timer);
            onDone(result.data);
        }, POLL_MS, { immediate: false });
    }

    function setBusy(button, label) {
        var original = button.innerHTML;
        button.disabled = true;
        button.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>' + label;
        return function () {
            button.disabled = false;
            button.innerHTML = original;
        };
    }

    function linkState(row) {
        if (!row.connection_valid) return { cls: 'text-bg-danger', text: 'Key rejected' };
        if (row.last_error) return { cls: 'text-bg-warning', text: 'Last sync failed' };
        return { cls: 'text-bg-success', text: 'Linked' };
    }

    // What the last run found, in words. A run that imported nothing is
    // either up to date or could not match Kavita's files, and the two must
    // not look the same.
    function describeRun(row) {
        var d = row.detail || {};
        if (!row.last_sync || d.chapters === undefined) return '';
        if (!d.chapters) {
            return d.series_skipped
                ? 'Nothing new in Kavita since the previous sync.'
                : 'Kavita reported no issues with reading progress.';
        }
        var parts = [];
        function add(count, text) { if (count) parts.push(count + ' ' + text); }
        add(d.already, 'already up to date in CLU');
        add(d.no_match, 'not found in CLU');
        add(d.ambiguous, 'matched more than one CLU file');
        add(d.no_access, 'outside your folders');
        add(d.unusable, 'not a single comic file');
        add(d.errors, 'failed to save');
        var text = 'Kavita had progress on ' + d.chapters + (d.chapters === 1 ? ' issue' : ' issues') + '.';
        if (parts.length) text += ' ' + parts.join(', ') + '.';
        if (d.no_match || d.ambiguous) {
            text += ' Files are matched by the library path mapping in Settings, or by an identical filename.';
        }
        return text;
    }

    function linkBadge(row) {
        var state = linkState(row);
        return '<span class="badge ' + state.cls + '">' + state.text + '</span>';
    }

    // =====================================================================
    // Settings page (owner)
    // =====================================================================

    var loadedUrl = '';
    var loadedForm = null;

    function renderSyncTime() {
        var input = byId('kavitaSyncTime');
        if (!input) return;
        input.value = CLU.utcToLocal(input.dataset.utc || DEFAULT_TIME, CLU.tzOffsetHours());
    }

    function renderMappings(mappings) {
        var container = byId('kavitaLibraryMappings');
        if (!mappings || !mappings.length) {
            container.innerHTML = '<p class="text-muted small fst-italic">No libraries configured. Add libraries in the Libraries section first.</p>';
            return;
        }
        var html = '<div class="table-responsive"><table class="table table-sm align-middle mb-0">'
            + '<thead><tr><th>CLU Library</th><th>CLU Path</th><th>Kavita Path Prefix</th></tr></thead><tbody>';
        mappings.forEach(function (m) {
            html += '<tr>'
                + '<td>' + CLU.escapeHtml(m.library_name) + '</td>'
                + '<td><code>' + CLU.escapeHtml(m.library_path) + '</code></td>'
                + '<td><input type="text" class="form-control form-control-sm kavita-prefix-input" '
                + 'data-library-id="' + Number(m.library_id) + '" '
                + 'aria-label="Kavita path prefix for ' + CLU.escapeHtml(m.library_name) + '" '
                + 'value="' + CLU.escapeHtml(m.kavita_path_prefix || '') + '" '
                + 'placeholder="/comics"></td>'
                + '</tr>';
        });
        container.innerHTML = html + '</tbody></table></div>';
    }

    function readForm() {
        var mappings = [];
        document.querySelectorAll('.kavita-prefix-input').forEach(function (input) {
            mappings.push({
                library_id: parseInt(input.dataset.libraryId, 10),
                kavita_path_prefix: input.value.trim()
            });
        });
        return {
            server_url: byId('kavitaServerUrl').value.trim(),
            library_mappings: mappings,
            frequency: byId('kavitaSyncFrequency').value,
            time: CLU.localToUtc(byId('kavitaSyncTime').value, CLU.tzOffsetHours()),
            weekday: parseInt(byId('kavitaSyncWeekday').value, 10) || 0
        };
    }

    function formChanged() {
        return JSON.stringify(readForm()) !== loadedForm;
    }

    function renderUsers(data) {
        var box = byId('kavitaUsers');
        var users = data.users || [];
        byId('kavitaNextRun').textContent = data.next_run || 'Not scheduled';
        if (!users.length) {
            box.innerHTML = '<p class="text-muted small fst-italic mb-0">No one has linked a Kavita account yet. '
                + 'Each user adds their own auth key under My Account.</p>';
            return;
        }
        var html = '<div class="table-responsive"><table class="table table-sm align-middle mb-0">'
            + '<thead><tr><th>CLU user</th><th>Kavita user</th><th>Status</th><th>Last sync</th>'
            + '<th class="text-end">Read</th><th class="text-end">In progress</th></tr></thead><tbody>';
        users.forEach(function (u) {
            html += '<tr>'
                + '<td>' + CLU.escapeHtml(u.username) + '</td>'
                + '<td>' + CLU.escapeHtml(u.kavita_username || '') + '</td>'
                + '<td>' + linkBadge(u)
                + (u.last_error ? ' <span class="text-muted small">' + CLU.escapeHtml(u.last_error) + '</span>' : '')
                + '</td>'
                + '<td>' + CLU.escapeHtml(u.last_sync || 'Never')
                + (describeRun(u) ? '<div class="text-muted small">' + CLU.escapeHtml(describeRun(u)) + '</div>' : '')
                + '</td>'
                + '<td class="text-end">' + Number(u.last_sync_read_count || 0) + '</td>'
                + '<td class="text-end">' + Number(u.last_sync_progress_count || 0) + '</td>'
                + '</tr>';
        });
        box.innerHTML = html + '</tbody></table></div>';
    }

    async function loadUsers() {
        try {
            var result = await request('/api/kavita/status');
            if (result.ok) renderUsers(result.data);
            return result.ok ? result.data : null;
        } catch (e) {
            return null;
        }
    }

    async function loadConfig() {
        try {
            var result = await request('/api/kavita/config');
            if (!result.ok || !result.data.config) return;
            var cfg = result.data.config;
            loadedUrl = cfg.server_url || '';
            byId('kavitaServerUrl').value = loadedUrl;
            renderMappings(cfg.library_mappings || []);
            byId('kavitaSyncFrequency').value = cfg.frequency || 'disabled';
            byId('kavitaSyncTime').dataset.utc = cfg.time || DEFAULT_TIME;
            renderSyncTime();
            byId('kavitaSyncWeekday').value = String(cfg.weekday || 0);
            byId('kavitaWeekdayGroup').style.display = cfg.frequency === 'weekly' ? 'block' : 'none';
            loadedForm = JSON.stringify(readForm());
        } catch (e) {
            console.error('Error loading Kavita config:', e);
        }
    }

    // A step of the Download and API tab's Save: resolves to null when saved
    // or when there was nothing to save, else to the error text. It posts only
    // when the form changed, so saving another setting on the tab does not
    // reschedule the sync.
    async function saveConfig() {
        if (!byId('kavitaServerUrl').value.trim() && !loadedUrl) return null;
        if (!formChanged()) return null;
        try {
            var result = await request('/api/kavita/config', 'POST', readForm());
            if (result.ok && result.data.success) {
                await loadConfig();
                loadUsers();
                return null;
            }
            return errorText(result, 'Failed to save');
        } catch (e) {
            return e.message;
        }
    }

    async function syncAll() {
        var done = setBusy(byId('kavitaSyncBtn'), 'Syncing...');
        try {
            if (formChanged()) {
                var saveError = await saveConfig();
                if (saveError) {
                    CLU.toast('Could not save Kavita: ' + saveError, 'error');
                    done();
                    return;
                }
            }
            var result = await request('/api/kavita/sync', 'POST', {});
            if (!result.ok || !result.data.success) {
                CLU.toast(errorText(result, 'Failed to start the sync'), 'error');
                done();
                return;
            }
            CLU.toast('Kavita sync started in background', 'info');
            followSync('/api/kavita/status', function (data) {
                renderUsers(data);
                CLU.toast('Kavita sync finished', 'success');
                done();
            });
        } catch (e) {
            CLU.toast('Error: ' + e.message, 'error');
            done();
        }
    }

    function initConfig() {
        if (!byId('kavitaServerUrl')) return;

        var tz = byId('timezone');
        if (tz) tz.addEventListener('change', renderSyncTime);
        // data-utc stays the source of truth, so a hand-edited time survives
        // the timezone picker moving.
        byId('kavitaSyncTime').addEventListener('change', function () {
            this.dataset.utc = CLU.localToUtc(this.value, CLU.tzOffsetHours());
        });
        byId('kavitaSyncFrequency').addEventListener('change', function () {
            byId('kavitaWeekdayGroup').style.display = this.value === 'weekly' ? 'block' : 'none';
        });
        byId('kavitaSyncBtn').addEventListener('click', syncAll);

        loadConfig();
        loadUsers();
    }

    // =====================================================================
    // Account page (every user)
    // =====================================================================

    function renderAccount(status) {
        var notConfigured = byId('kavitaNotConfigured');
        var panel = byId('kavitaAccountPanel');
        if (!status.configured) {
            notConfigured.classList.remove('d-none');
            panel.classList.add('d-none');
            return;
        }
        notConfigured.classList.add('d-none');
        panel.classList.remove('d-none');

        var linked = !!status.linked;
        byId('kavitaLinkedInfo').classList.toggle('d-none', !linked);
        byId('kavitaAccountSyncBtn').disabled = !linked || !status.connection_valid;
        byId('kavitaUnlinkBtn').classList.toggle('d-none', !linked);
        byId('kavitaAuthKey').placeholder = linked ? 'Saved. Enter a new key to replace it' : 'Kavita auth key';

        var badge = byId('kavitaAccountBadge');
        if (!linked) {
            badge.className = 'badge text-bg-secondary';
            badge.textContent = 'Not linked';
            return;
        }
        var state = linkState(status);
        badge.className = 'badge ' + state.cls;
        badge.textContent = state.text;
        byId('kavitaAccountUser').textContent = status.kavita_username || 'your Kavita account';
        byId('kavitaAccountLastSync').textContent = status.last_sync || 'Never';
        byId('kavitaAccountRead').textContent = Number(status.last_sync_read_count || 0);
        byId('kavitaAccountProgress').textContent = Number(status.last_sync_progress_count || 0);
        var summary = byId('kavitaAccountDetail');
        summary.textContent = describeRun(status);
        summary.classList.toggle('d-none', !summary.textContent);
        var error = byId('kavitaAccountError');
        error.textContent = status.last_error || '';
        error.classList.toggle('d-none', !status.last_error);
    }

    async function loadAccount() {
        try {
            var result = await request('/api/account/kavita');
            if (result.ok) renderAccount(result.data);
        } catch (e) {
            console.error('Error loading Kavita link:', e);
        }
    }

    async function saveLink() {
        var key = byId('kavitaAuthKey').value.trim();
        if (!key) {
            CLU.toast('Enter your Kavita auth key.', 'error');
            return;
        }
        var done = setBusy(byId('kavitaLinkBtn'), 'Checking...');
        try {
            var result = await request('/api/account/kavita', 'POST', { auth_key: key });
            if (result.ok && result.data.success) {
                byId('kavitaAuthKey').value = '';
                renderAccount(result.data);
                CLU.toast('Kavita account linked.', 'success');
            } else {
                CLU.toast(errorText(result, 'Could not link the account.'), 'error');
            }
        } catch (e) {
            CLU.toast('Error: ' + e.message, 'error');
        } finally {
            done();
        }
    }

    async function unlink() {
        var confirmed = await CLU.confirm({
            title: 'Unlink Kavita?',
            message: 'CLU will forget your Kavita auth key and stop importing your progress.',
            detail: 'Reading history already imported stays in CLU.',
            confirmText: 'Unlink'
        });
        if (!confirmed) return;
        try {
            var result = await request('/api/account/kavita', 'DELETE');
            if (result.ok && result.data.success) {
                renderAccount(result.data);
                CLU.toast('Kavita account unlinked.', 'success');
            } else {
                CLU.toast(errorText(result, 'Could not unlink the account.'), 'error');
            }
        } catch (e) {
            CLU.toast('Error: ' + e.message, 'error');
        }
    }

    async function syncMine() {
        var done = setBusy(byId('kavitaAccountSyncBtn'), 'Syncing...');
        try {
            var result = await request('/api/account/kavita/sync', 'POST', {});
            if (!result.ok || !result.data.success) {
                CLU.toast(errorText(result, 'Failed to start the sync'), 'error');
                done();
                return;
            }
            CLU.toast('Kavita sync started in background', 'info');
            followSync('/api/account/kavita', function (status) {
                done();
                renderAccount(status);
                if (!status.connection_valid) {
                    CLU.toast('Kavita rejected your auth key. Save a new one.', 'error');
                } else if (status.last_error) {
                    CLU.toast('Kavita sync failed: ' + status.last_error, 'error');
                } else {
                    CLU.toast('Kavita sync finished: ' + Number(status.last_sync_read_count || 0)
                        + ' read, ' + Number(status.last_sync_progress_count || 0) + ' in progress.', 'success');
                }
            });
        } catch (e) {
            CLU.toast('Error: ' + e.message, 'error');
            done();
        }
    }

    function initAccount() {
        if (!byId('kavitaAccountPanel')) return;
        byId('kavitaLinkBtn').addEventListener('click', saveLink);
        byId('kavitaUnlinkBtn').addEventListener('click', unlink);
        byId('kavitaAccountSyncBtn').addEventListener('click', syncMine);
        loadAccount();
    }

    CLU.kavita = {
        initConfig: initConfig,
        initAccount: initAccount,
        saveConfig: saveConfig
    };
})();
