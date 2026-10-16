/* global bootstrap, CLU */
/**
 * Bulk metadata review controller.
 *
 * Drives the progress modal during a /api/bulk-metadata job and surfaces the
 * end-of-job review queue. Other JS files call BulkMetadataReview.startJob
 * with a small contract describing the scope.
 */
window.BulkMetadataReview = (function () {
    'use strict';

    let _pollTimer = null;
    let _currentJobId = null;

    function _show(id) {
        const el = document.getElementById(id);
        if (!el) return null;
        return bootstrap.Modal.getOrCreateInstance(el);
    }

    function _setProgress(current, total) {
        const bar = document.getElementById('bulkMetadataProgressBar');
        const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
        if (bar) {
            bar.style.width = pct + '%';
            bar.textContent = pct + '%';
        }
        const curEl = document.getElementById('bulkMetadataProgressCurrent');
        const totEl = document.getElementById('bulkMetadataProgressTotal');
        if (curEl) curEl.textContent = current;
        if (totEl) totEl.textContent = total;
    }

    function _setCounters(job) {
        const map = {
            bulkMetaCountAccepted: job.auto_accepted || 0,
            bulkMetaCountReview: job.needs_review || 0,
            bulkMetaCountSkipped: job.skipped || 0,
            bulkMetaCountErrors: job.errors || 0
        };
        Object.keys(map).forEach(k => {
            const el = document.getElementById(k);
            if (el) el.textContent = map[k];
        });
    }

    function _setDetail(text) {
        const el = document.getElementById('bulkMetadataProgressLabelDetail');
        if (el) el.textContent = text || '';
    }

    function _setJobLabel(jobId) {
        const el = document.getElementById('bulkMetadataProgressJob');
        if (el) el.textContent = 'Job: ' + jobId.slice(0, 8);
    }

    async function _pollOnce(jobId) {
        try {
            const r = await fetch(`/api/bulk-metadata/progress/${jobId}`);
            if (!r.ok) return null;
            return await r.json();
        } catch (e) {
            return null;
        }
    }

    function _startPolling(jobId, onDone) {
        _stopPolling();
        let lastDetail = '';
        _pollTimer = setInterval(async () => {
            const data = await _pollOnce(jobId);
            if (!data || !data.success) return;
            _setProgress(data.current || 0, data.total || 0);
            if (data.job) _setCounters(data.job);
            if (data.detail && data.detail !== lastDetail) {
                _setDetail(data.detail);
                lastDetail = data.detail;
            }
            if (data.status === 'completed' || data.status === 'error') {
                _stopPolling();
                _finish(jobId, data, onDone);
            }
        }, 1000);
    }

    function _stopPolling() {
        if (_pollTimer) {
            clearInterval(_pollTimer);
            _pollTimer = null;
        }
    }

    // Optional per-job wording for the completion toast (backfill, preview).
    let _summarize = null;

    async function _finish(jobId, lastData, onDone) {
        // Pull the final review queue so we know whether to surface the modal.
        let items = [];
        try {
            const r = await fetch(`/api/bulk-metadata/review/${jobId}`);
            if (r.ok) {
                const d = await r.json();
                items = (d && d.items) || [];
            }
        } catch (e) {
            items = [];
        }

        const progressModal = _show('bulkMetadataProgressModal');
        if (progressModal) progressModal.hide();

        const job = (lastData && lastData.job) || {};
        const summary = `Auto-matched ${job.auto_accepted || 0}, skipped ${job.skipped || 0}, errors ${job.errors || 0}.`;
        const custom = _summarize ? _summarize(job) : null;
        _summarize = null;

        if (items.length === 0) {
            CLU.showSuccess(custom || `Bulk metadata complete. ${summary}`);
            if (typeof onDone === 'function') onDone();
            return;
        }

        _renderReview(items, summary, jobId, onDone);
        const reviewModal = _show('bulkMetadataReviewModal');
        if (reviewModal) reviewModal.show();
    }

    // Wizard state — one group at a time, where a "group" is every pending
    // review row sharing the same (folder_path, parsed_series). The frontend
    // groups; the backend cascades on resolve / skip.
    let _wizard = null;

    function _groupItems(items) {
        const byKey = new Map();
        const order = [];
        items.forEach(it => {
            // The separator is a NUL: it cannot occur in a path or a series name, so
            // the two halves can never run together. Written as the escape '\0',
            // never as a literal NUL byte -- a raw one makes the whole file read as
            // binary to grep and diff, and is invisible in review.
            const key = (it.folder_path || '') + '\0' + (it.parsed_series || '');
            if (!byKey.has(key)) {
                byKey.set(key, {
                    key,
                    folder_path: it.folder_path || '',
                    parsed_series: it.parsed_series || '',
                    rows: [],
                });
                order.push(key);
            }
            byKey.get(key).rows.push(it);
        });
        return order.map(k => byKey.get(k));
    }

    function _renderReview(items, summary, jobId, onDone) {
        const groups = _groupItems(items);
        const totalFiles = items.length;
        _wizard = {
            groups,
            idx: 0,
            totalFiles,
            doneFiles: 0,
            jobId,
            onDone,
            summary,
        };
        _renderCurrentGroup();
    }

    function _updateProgressUi() {
        if (!_wizard) return;
        const idxEl = document.querySelector('[data-bulk-group-idx]');
        const totalEl = document.querySelector('[data-bulk-group-total]');
        const filesEl = document.querySelector('[data-bulk-group-files]');
        const remainEl = document.querySelector('[data-bulk-remaining]');
        const barEl = document.querySelector('[data-bulk-progress-bar]');
        const cur = _wizard.groups[_wizard.idx];
        const groupFiles = cur ? cur.rows.length : 0;
        const remaining = Math.max(0, _wizard.totalFiles - _wizard.doneFiles);
        if (idxEl) idxEl.textContent = Math.min(_wizard.idx + 1, _wizard.groups.length);
        if (totalEl) totalEl.textContent = _wizard.groups.length;
        if (filesEl) filesEl.textContent = groupFiles;
        if (remainEl) remainEl.textContent = remaining;
        if (barEl) {
            const pct = _wizard.totalFiles > 0
                ? Math.min(100, Math.round((_wizard.doneFiles / _wizard.totalFiles) * 100))
                : 100;
            barEl.style.width = pct + '%';
        }
    }

    function _renderCurrentGroup() {
        if (!_wizard) return;
        const summaryEl = document.getElementById('bulkMetadataReviewSummary');
        const list = document.getElementById('bulkMetadataReviewList');
        if (!list) return;

        // All groups handled — close modal + summary toast.
        if (_wizard.idx >= _wizard.groups.length) {
            const reviewModal = _show('bulkMetadataReviewModal');
            if (reviewModal) reviewModal.hide();
            CLU.showSuccess(`Review complete. ${_wizard.doneFiles} file(s) resolved.`);
            if (typeof _wizard.onDone === 'function') _wizard.onDone();
            _wizard = null;
            return;
        }

        const group = _wizard.groups[_wizard.idx];
        if (summaryEl) {
            summaryEl.innerHTML = `Resolve <strong>${_escape(group.parsed_series || '(unknown series)')}</strong> ` +
                `in <code>${_escape(group.folder_path)}</code>`;
        }
        list.innerHTML = '';
        const card = _renderReviewRow(group.rows[0], _wizard.jobId, group);
        list.appendChild(card);
        _updateProgressUi();
    }

    function _completeGroup(group) {
        if (!_wizard) return;
        _wizard.doneFiles += group.rows.length;
        _wizard.idx += 1;
        _renderCurrentGroup();
    }

    function _renderReviewRow(item, jobId, group) {
        const card = document.createElement('div');
        card.className = 'card mb-3';
        card.dataset.reviewId = item.id;
        card.dataset.folderPath = item.folder_path || '';

        const groupSize = group && group.rows ? group.rows.length : 1;

        const reasonBadge = {
            series_ambiguous: '<span class="badge bg-warning text-dark">Series ambiguous</span>',
            series_no_match: '<span class="badge bg-secondary">Series not found</span>',
            issue_ambiguous: '<span class="badge bg-warning text-dark">Issue ambiguous</span>',
            issue_no_match: '<span class="badge bg-secondary">Issue not found</span>',
            date_conflict: '<span class="badge bg-warning text-dark">Date conflict</span>',
            provider_error: '<span class="badge bg-danger">Provider error</span>'
        }[item.reason] || `<span class="badge bg-secondary">${item.reason}</span>`;

        const target = item.file_path
            ? `<code class="small">${_escape(item.file_path.split(/[\\/]/).pop())}</code>`
            : `<code class="small">${_escape(item.folder_path)}</code>`;

        const parsedSummary = [
            item.parsed_series ? `series "${_escape(item.parsed_series)}"` : null,
            item.parsed_issue ? `issue #${_escape(item.parsed_issue)}` : null,
            item.parsed_year ? `year ${item.parsed_year}` : null
        ].filter(Boolean).join(', ');

        const candidates = Array.isArray(item.candidates) ? item.candidates : [];

        // Cover thumbnail uses /api/thumbnail, which only handles CBZ/ZIP
        // archives. The backend pre-resolves cover_path: file_path for
        // file-level rows, first CBZ in the folder for folder-level rows.
        // Fall back to the static placeholder when no CBZ was found.
        const coverPath = item.cover_path || item.file_path || '';
        const coverSrc = coverPath
            ? `/api/thumbnail?path=${encodeURIComponent(coverPath)}`
            : '/static/images/loading.svg';

        card.innerHTML = `
            <div class="card-body">
                <div class="d-flex gap-3 align-items-start">
                    <img class="review-cover flex-shrink-0" loading="lazy"
                         src="${coverSrc}" alt=""
                         onerror="this.onerror=null; this.src='/static/images/loading.svg';"
                         style="width: 80px; height: 120px; object-fit: cover; border-radius: 4px; background: #eee;">
                    <div class="flex-grow-1 min-w-0">
                        <div class="d-flex justify-content-between align-items-start mb-2">
                            <div>
                                <div class="mb-1">${reasonBadge}</div>
                                ${target}
                                ${parsedSummary ? `<div class="small text-muted">Parsed: ${parsedSummary}</div>` : ''}
                                ${groupSize > 1 ? `<div class="small text-muted">+ ${groupSize - 1} more file(s) in this series will be resolved together.</div>` : ''}
                            </div>
                            <button class="btn btn-sm btn-outline-warning action-skip-series" type="button"
                                    title="Dismiss this series and move on">
                                <i class="bi bi-skip-forward me-1"></i>Skip series (${groupSize})
                            </button>
                        </div>
                        <div class="small text-muted mb-1">
                            Know the ID? Enter it and we'll write <code>cvinfo</code> for the folder
                            and apply metadata to every issue in it.
                        </div>
                        <div class="input-group input-group-sm mb-2 id-entry">
                            <span class="input-group-text">ComicVine ID</span>
                            <input type="text" class="form-control cv-volume-id"
                                   inputmode="numeric" placeholder="e.g. 167796">
                            <span class="input-group-text">Metron ID</span>
                            <input type="text" class="form-control metron-series-id"
                                   inputmode="numeric" placeholder="e.g. 4984">
                            <button class="btn btn-primary action-apply-ids" type="button">
                                <i class="bi bi-check2-circle me-1"></i>Apply to folder
                            </button>
                        </div>
                        <div class="apply-progress small text-muted mb-2 d-none">
                            <i class="bi bi-arrow-repeat me-1"></i>
                            <span class="apply-progress-label text-info">Working…</span>
                        </div>
                        <div class="row g-2 candidate-grid"></div>
                        <!-- A date conflict means the *series* is suspect, so the
                             rejected candidate above is not the answer — keep the
                             search box visible even though a candidate exists. -->
                        <div class="manual-search mt-2 ${candidates.length && item.reason !== 'date_conflict' ? 'd-none' : ''}">
                            <div class="input-group input-group-sm">
                                <input type="text" class="form-control manual-query"
                                    placeholder="Search series name…" value="${_escape(item.parsed_series || '')}">
                                <input type="number" class="form-control manual-year" style="max-width: 6rem;"
                                    placeholder="Year" value="${item.parsed_year || ''}">
                                <button class="btn btn-outline-primary action-manual-search" type="button">
                                    <i class="bi bi-search"></i>
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>`;

        const grid = card.querySelector('.candidate-grid');
        candidates.forEach(c => grid.appendChild(_renderCandidateCard(c, item, card, group)));

        const skipBtn = card.querySelector('.action-skip-series');
        if (skipBtn) {
            skipBtn.addEventListener('click', () => _skipSeries(skipBtn, item, group));
        }
        const manualBtn = card.querySelector('.action-manual-search');
        if (manualBtn) {
            manualBtn.addEventListener('click', () => _doManualSearch(card, item, group));
        }
        const applyIdsBtn = card.querySelector('.action-apply-ids');
        if (applyIdsBtn) {
            applyIdsBtn.addEventListener('click', () => _applyIds(card, item, group));
        }

        return card;
    }

    async function _skipSeries(btn, item, group) {
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Skipping…';
        }
        try {
            const r = await fetch(`/api/bulk-metadata/review/${item.id}/skip-series`, {
                method: 'POST'
            });
            const data = await r.json();
            if (data && data.success) {
                CLU.showSuccess(`Skipped ${data.count || group.rows.length} file(s).`);
                _completeGroup(group);
            } else {
                CLU.showError((data && data.error) || 'Skip failed');
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = `<i class="bi bi-skip-forward me-1"></i>Skip series (${group.rows.length})`;
                }
            }
        } catch (e) {
            CLU.showError(e.message || 'Skip failed');
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = `<i class="bi bi-skip-forward me-1"></i>Skip series (${group.rows.length})`;
            }
        }
    }

    async function _applyIds(card, item, group) {
        const cvInput = card.querySelector('.cv-volume-id');
        const metronInput = card.querySelector('.metron-series-id');
        const cvId = cvInput.value.trim();
        const metronId = metronInput.value.trim();
        if (!cvId && !metronId) {
            CLU.showError('Enter at least one ID.');
            return;
        }
        const btn = card.querySelector('.action-apply-ids');
        const restore = () => {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<i class="bi bi-check2-circle me-1"></i>Apply to folder';
            }
        };
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Applying…';
        }
        const opToken = _genOpToken();
        _showApplyProgress(card, 'Starting…');
        const stopPolling = _startApplyProgressPolling(card, opToken);
        try {
            const r = await _fetchWithTimeout(`/api/bulk-metadata/review/${item.id}/apply-cvinfo`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    cv_volume_id: cvId || null,
                    metron_series_id: metronId || null,
                    op_token: opToken
                })
            }, APPLY_TIMEOUT_MS);
            const data = await r.json();
            stopPolling();
            _hideApplyProgress(card);
            if (!data || !data.success) {
                CLU.showError((data && data.error) || 'Apply failed');
                restore();
                return;
            }
            CLU.showSuccess(`Wrote metadata to ${data.written || 0} file(s) in the folder.`);
            _completeGroup(group);
        } catch (e) {
            stopPolling();
            _hideApplyProgress(card);
            const msg = e.name === 'AbortError'
                ? 'Apply timed out — the server may still be processing.'
                : (e.message || 'Apply failed');
            CLU.showError(msg);
            restore();
        }
    }

    function _renderCandidateCard(candidate, reviewItem, parentCard, group) {
        const col = document.createElement('div');
        col.className = 'col-md-6 col-lg-4';
        const isIssue = candidate.issue_number !== undefined;
        const title = candidate.title || candidate.issue_number || '(untitled)';
        const sub = isIssue
            ? `Issue #${_escape(candidate.issue_number || '')}${candidate.cover_date ? ' · ' + _escape(candidate.cover_date) : ''}`
            : `${_escape(candidate.publisher || '')}${candidate.year ? ' · ' + candidate.year : ''}${candidate.issue_count ? ' · ' + candidate.issue_count + ' issues' : ''}`;
        // GCD (comics.org API) series results carry no cover — fetch it lazily.
        const isGcd = candidate.provider === 'gcd_api';
        const coverSeriesId = candidate.series_id || candidate.id;
        const coverIssue = isIssue ? (candidate.issue_number || '') : (reviewItem.parsed_issue || '');
        let cover;
        if (candidate.cover_url) {
            cover = `<img src="${_escape(candidate.cover_url)}" alt="" class="me-2" style="width: 48px; height: 72px; object-fit: cover;">`;
        } else if (isGcd && coverSeriesId) {
            cover = `<div class="me-2 bg-light flex-shrink-0" data-gcd-cover style="width: 48px; height: 72px;"></div>`;
        } else {
            cover = '';
        }
        col.innerHTML = `
            <div class="card h-100">
                <div class="card-body p-2 d-flex">
                    ${cover}
                    <div class="flex-grow-1 small">
                        <div class="fw-bold text-truncate" title="${_escape(title)}">${_escape(title)}</div>
                        <div class="text-muted small text-truncate">${sub}</div>
                        <div class="text-muted small">${_escape(candidate.provider || '')}</div>
                        <button class="btn btn-sm btn-primary mt-2 action-apply" type="button">
                            <i class="bi bi-check2 me-1"></i>Apply
                        </button>
                    </div>
                </div>
            </div>`;
        if (!candidate.cover_url && isGcd && coverSeriesId && window.CLU && CLU.lazyLoadGcdCover) {
            const ph = col.querySelector('[data-gcd-cover]');
            if (ph) {
                CLU.lazyLoadGcdCover(ph, coverSeriesId, coverIssue, (url) => {
                    candidate.cover_url = url;  // cache so re-renders skip the fetch
                    const img = document.createElement('img');
                    img.src = url;
                    img.alt = '';
                    img.className = 'me-2 flex-shrink-0';
                    img.style.cssText = 'width: 48px; height: 72px; object-fit: cover;';
                    if (ph.parentNode) ph.parentNode.replaceChild(img, ph);
                });
            }
        }
        const btn = col.querySelector('.action-apply');
        btn.addEventListener('click', async () => {
            const payload = isIssue
                ? { provider: candidate.provider, series_id: candidate.series_id, issue_id: candidate.id }
                : { provider: candidate.provider, series_id: candidate.id };
            await _applyCandidate(reviewItem.id, payload, btn, parentCard, group);
        });
        return col;
    }

    async function _applyCandidate(reviewId, payload, btn, parentCard, group) {
        const restore = () => {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<i class="bi bi-check2 me-1"></i>Apply';
            }
        };
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Applying…';
        }
        const opToken = _genOpToken();
        if (parentCard) _showApplyProgress(parentCard, 'Starting…');
        const stopPolling = parentCard
            ? _startApplyProgressPolling(parentCard, opToken)
            : () => { };
        const fullPayload = Object.assign({}, payload, { op_token: opToken });
        try {
            const r = await _fetchWithTimeout(`/api/bulk-metadata/review/${reviewId}/resolve`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(fullPayload)
            }, APPLY_TIMEOUT_MS);
            const d = await r.json();
            stopPolling();
            if (parentCard) _hideApplyProgress(parentCard);
            if (d && d.success) {
                CLU.showSuccess(d.written ? `Wrote metadata to ${d.written} file(s)` : 'Metadata applied');
                _completeGroup(group);
            } else {
                CLU.showError((d && d.error) || 'Failed to apply');
                restore();
            }
        } catch (e) {
            stopPolling();
            if (parentCard) _hideApplyProgress(parentCard);
            const msg = e.name === 'AbortError'
                ? 'Apply timed out — the server may still be processing.'
                : (e.message || 'Apply failed');
            CLU.showError(msg);
            restore();
        }
    }

    async function _doManualSearch(parentCard, item, group) {
        const q = parentCard.querySelector('.manual-query').value.trim();
        const y = parentCard.querySelector('.manual-year').value.trim();
        if (!q) return;
        const btn = parentCard.querySelector('.action-manual-search');
        const restoreBtn = () => {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<i class="bi bi-search"></i>';
            }
        };
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>';
        }
        // Reuse the existing single-file metadata search endpoint which already
        // knows how to fan out across all configured providers.
        //
        // file_name must be the REAL filename so parse_comic_filename can
        // extract the issue number and year. Sending the raw query string
        // instead defaults issue to #1 and year to None — meaning the search
        // ignores the actual issue the user is trying to resolve.
        //
        // search_term is the supported series-name override on the
        // /api/search-metadata route (the older `series_name` key is ignored).
        const realFileName = item.file_path
            ? item.file_path.split(/[\\/]/).pop()
            : (q + '.cbz');
        try {
            const r = await fetch('/api/search-metadata', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    file_path: item.file_path || item.folder_path,
                    file_name: realFileName,
                    search_term: q,
                    // search_year applies to all providers; gcd_api_start_year
                    // kept for backward compat with the legacy single-file flow.
                    search_year: y ? parseInt(y, 10) : null,
                    gcd_api_start_year: y ? parseInt(y, 10) : null
                })
            });
            const d = await r.json();

            // /api/search-metadata writes metadata directly when it finds a
            // unique match — response is {success: true, source, metadata, ...}
            // with NO possible_matches array. Detect that path first; mark the
            // triggering review row resolved, then advance the wizard so the
            // group's other files are surfaced naturally on the next group
            // (note: this path resolves only the ONE file, since the search
            // endpoint isn't group-aware).
            if (d && d.success === true && d.metadata) {
                try {
                    await fetch(`/api/bulk-metadata/review/${item.id}/mark-applied`, {
                        method: 'POST'
                    });
                } catch (e) { /* best-effort cleanup */ }
                CLU.showSuccess(`Metadata applied from ${d.source || 'provider'}.`);
                // Only the triggering file was written. Decrement the group's
                // unresolved count by one and re-render the current group so
                // the remaining files still get reviewed.
                if (group && Array.isArray(group.rows)) {
                    group.rows = group.rows.filter(r => r.id !== item.id);
                    if (_wizard) _wizard.doneFiles += 1;
                    if (group.rows.length === 0) {
                        _wizard.idx += 1;
                    }
                    _renderCurrentGroup();
                }
                return;
            }

            // /api/search-metadata returns the provider at the top level when
            // a selection is required (e.g. {provider: 'comicvine', possible_matches: [...]}).
            // Inherit it onto each candidate so the eventual /resolve call routes
            // to the right provider — without this, GCD / Metron / GCD-API
            // candidates would be POSTed as 'comicvine' and trigger a 502 on get_series.
            const responseProvider = (d && d.provider) || null;
            const results = (d && (d.possible_matches || d.matches || d.results)) || [];
            const grid = parentCard.querySelector('.candidate-grid');
            grid.innerHTML = '';
            results.forEach(c => {
                grid.appendChild(_renderCandidateCard({
                    provider: c.provider || responseProvider || 'comicvine',
                    id: c.id || c.volume_id,
                    title: c.name || c.title,
                    year: c.start_year || c.year,
                    publisher: c.publisher || c.publisher_name,
                    issue_count: c.count_of_issues || c.issue_count,
                    cover_url: c.image || c.cover_url
                }, item, parentCard, group));
            });
            if (results.length === 0) {
                CLU.showError('No matches found for that query.');
            }
        } catch (e) {
            CLU.showError(e.message || 'Search failed');
        } finally {
            restoreBtn();
        }
    }

    function _genOpToken() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return 'bm-' + window.crypto.randomUUID();
        }
        return 'bm-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
    }

    // Last-resort safety net: abort an apply request that never returns so the
    // UI can't get stuck in "Applying…". Set above the server timeout
    // (gunicorn --timeout 120) — this is a backstop, not the primary bound.
    const APPLY_TIMEOUT_MS = 150000;

    async function _fetchWithTimeout(url, opts, ms) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), ms);
        try {
            return await fetch(url, Object.assign({}, opts, { signal: controller.signal }));
        } finally {
            clearTimeout(timer);
        }
    }

    function _showApplyProgress(card, label) {
        const wrap = card.querySelector('.apply-progress');
        const lbl = card.querySelector('.apply-progress-label');
        if (wrap) wrap.classList.remove('d-none');
        if (lbl) lbl.textContent = label || 'Working…';
    }

    function _hideApplyProgress(card) {
        const wrap = card.querySelector('.apply-progress');
        if (wrap) wrap.classList.add('d-none');
    }

    function _startApplyProgressPolling(card, opToken) {
        let stopped = false;
        async function tick() {
            if (stopped) return;
            try {
                const r = await fetch(`/api/bulk-metadata/op-progress/${opToken}`);
                if (r.ok) {
                    const d = await r.json();
                    if (d && d.success) {
                        const total = d.total || 0;
                        const cur = d.current || 0;
                        const filename = d.detail || '';
                        let label;
                        if (total > 0) {
                            label = `Writing ${cur} of ${total}${filename ? ' · ' + filename : ''}`;
                        } else {
                            label = filename || 'Working…';
                        }
                        _showApplyProgress(card, label);
                        if (d.status === 'completed' || d.status === 'error') {
                            stopped = true;
                            return;
                        }
                    }
                }
            } catch (e) { /* poll noise — ignore */ }
            if (!stopped) setTimeout(tick, 500);
        }
        tick();
        return () => { stopped = true; };
    }

    function _escape(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function _resetProgressUi() {
        _setProgress(0, 0);
        _setCounters({});
        _setDetail('Starting…');
    }

    async function startJob(contract) {
        if (!contract || !contract.scope || !Array.isArray(contract.paths)) {
            CLU.showError('Bulk metadata: invalid contract');
            return;
        }
        _resetProgressUi();
        _summarize = typeof contract.summarize === 'function' ? contract.summarize : null;
        const modal = _show('bulkMetadataProgressModal');
        if (modal) modal.show();
        try {
            // `contract.url` points at another job endpoint (backfill) and
            // `contract.extra` adds fields (provider, replace, only_from, dry_run).
            const r = await fetch(contract.url || '/api/bulk-metadata/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(Object.assign({
                    scope: contract.scope,
                    paths: contract.paths,
                    library_id: contract.library_id || null,
                    overwrite_existing: !!contract.overwrite_existing
                }, contract.extra || {}))
            });
            const data = await r.json();
            if (!data || !data.success) {
                if (modal) modal.hide();
                CLU.showError((data && data.error) || 'Failed to start bulk job');
                return;
            }
            _currentJobId = data.job_id;
            _setJobLabel(data.job_id);
            _setProgress(0, data.total_files || 0);
            _startPolling(data.job_id, contract.onDone);
        } catch (e) {
            if (modal) modal.hide();
            CLU.showError(e.message || 'Start failed');
        }
    }

    return {
        startJob: startJob,
        _stopPolling: _stopPolling
    };
})();
