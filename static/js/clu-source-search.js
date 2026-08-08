/**
 * Multi-source issue search for the Wanted and Series pages.
 *
 * CLU can acquire an issue from GetComics, Usenet (indexers -> SABnzbd/NZBGet)
 * or DC++ (AirDC++ hubs). The search modal queries every configured source at
 * once and stacks the result sections in the user's Source Priority order.
 *
 * This module exists because that fan-out used to be copy-pasted into both
 * wanted.html and series.html with a hardcoded two-source Promise.all and a
 * pairwise "usenet first?" ternary, which does not extend to a third source.
 * Pages now supply only their own context and callbacks.
 *
 * Result cards use event delegation rather than inline onclick handlers, so
 * the module does not depend on page-level global function names (the two
 * pages historically used different ones: showToast vs showToastGC).
 *
 * Usage:
 *     const search = CLU.createSourceSearch({
 *         resultsEl: document.getElementById('getcomicsResults'),
 *         getContext: () => ({ series, issue, year }),
 *         toast: (message, type) => showToast(message, type),
 *         onQueued: ({ source, downloadId, issue }) => { ... },
 *     });
 *     search.run(queryString);
 */
(function (window, document) {
    'use strict';

    const CLU = window.CLU = window.CLU || {};

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = (text === null || text === undefined) ? '' : text;
        return div.innerHTML;
    }

    function fmtSize(bytes) {
        return bytes ? (bytes / (1024 * 1024)).toFixed(0) + ' MB' : '';
    }

    function sectionHeader(label, icon) {
        return `<div class="text-muted small fw-bold text-uppercase mb-2 mt-1">
                    <i class="bi bi-${icon} me-1"></i>${escapeHtml(label)}</div>`;
    }

    function alertBox(cssClass, icon, message) {
        return `<div class="alert ${cssClass} py-2 small mb-2">
                    <i class="bi bi-${icon} me-1"></i>${message}</div>`;
    }

    // Turn a wanted-issue filename into something the wanted-matcher will pick
    // up: "<series> <issue>.cbz", stripped of characters that break filesystems.
    function targetFilename(ctx) {
        const base = `${ctx.series || ''} ${ctx.issue || ''}`.trim();
        return base.replace(/[^a-zA-Z0-9\s\-#]/g, '').trim() + '.cbz';
    }

    /**
     * Sort GetComics results by how well the title matches the wanted issue.
     * GetComics returns raw page hits with no server-side scoring, unlike the
     * Usenet and DC++ endpoints which score before responding.
     */
    function scoreGetComicsResults(results, ctx) {
        return results.map(r => {
            let score = 0;
            const title = String(r.title || '').toLowerCase();
            const series = String(ctx.series || '').toLowerCase();
            const issue = ctx.issue;

            if (series && title.includes(series)) score += 10;
            if (title.includes(`#${issue}`) || title.includes(` ${issue} `) ||
                title.endsWith(` ${issue}`)) score += 5;
            // A matching year separates this volume's issue from every reprint
            // and reboot that shares the number.
            if (ctx.year && title.includes(String(ctx.year))) score += 8;

            return Object.assign({}, r, { score: score });
        }).sort((a, b) => b.score - a.score);
    }

    /**
     * Render a scored result list with the low-scoring tail behind a toggle.
     *
     * Indexers and hubs can return 50+ hits for one issue; an unfiltered list
     * pushes the other sources off the screen. The collapse element is keyed
     * by source so two sections on the same page cannot collide.
     */
    function scoredResultList(sourceKey, results, cardFor) {
        const isMatch = r => r.decision === 'ACCEPT' || r.decision === 'FALLBACK';
        const matched = results.filter(isMatch);
        const others = results.filter(r => !isMatch(r));
        const othersId = `cluSourceOthers-${sourceKey}`;

        let html = matched.length
            ? matched.map(cardFor).join('')
            : '<div class="text-muted small mb-2">No confident match.</div>';

        if (others.length) {
            html += `
            <div class="text-center my-2">
                <button class="btn btn-sm btn-outline-secondary clu-src-toggle" type="button"
                    data-target="${othersId}">
                    <i class="bi bi-chevron-down me-1"></i>${othersLabel(others.length, false, matched.length > 0)}
                </button>
            </div>
            <div id="${othersId}" class="d-none" data-count="${others.length}"
                data-has-match="${matched.length > 0 ? '1' : ''}">
                ${others.map(cardFor).join('')}
            </div>`;
        }
        return html;
    }

    function othersLabel(count, expanded, hasMatch) {
        const noun = count === 1 ? 'result' : 'results';
        // "lower-scoring" only reads right when something better is shown above.
        const qualifier = hasMatch ? 'lower-scoring ' : '';
        return `${expanded ? 'Hide' : 'Show'} ${count} ${qualifier}${noun}`;
    }

    function decisionBadge(decision) {
        if (decision === 'ACCEPT') return '<span class="badge bg-success ms-1">Match</span>';
        if (decision === 'FALLBACK') return '<span class="badge bg-warning text-dark ms-1">Pack</span>';
        return '';
    }

    /**
     * A card for a scored (Usenet/DC++) result. `action` carries the data-*
     * attributes the click delegate needs to queue it.
     */
    function scoredCard(r, subtitle, action, enabled) {
        return `
        <div class="card mb-2">
            <div class="card-body d-flex align-items-center gap-3 py-2">
                <div class="flex-grow-1 overflow-hidden">
                    <div class="fw-bold text-truncate" title="${escapeHtml(r.title)}">${escapeHtml(r.title)}${decisionBadge(r.decision)}</div>
                    <small class="text-muted text-truncate d-block">${subtitle}</small>
                </div>
                <button class="btn btn-sm btn-primary flex-shrink-0 clu-src-grab" ${action}
                    title="Send to download client" ${enabled ? '' : 'disabled'}>
                    <i class="bi bi-cloud-download"></i>
                </button>
            </div>
        </div>`;
    }

    // -----------------------------------------------------------------------
    // Source definitions
    //
    // Each source builds its own section HTML and knows how to queue one of
    // its results. `build` returns '' to stay silent when the source is not
    // configured, so an unused source never adds noise to the modal.
    // -----------------------------------------------------------------------

    const SOURCES = {
        getcomics: {
            label: 'GetComics',
            icon: 'globe',
            async build(ctx) {
                const resp = await fetch(`/api/getcomics/search?q=${encodeURIComponent(ctx.query)}`);
                const data = await resp.json();
                if (!data.success) {
                    return alertBox('alert-danger', 'exclamation-octagon', escapeHtml(data.error));
                }
                if (!data.results || !data.results.length) return '';

                const cards = scoreGetComicsResults(data.results, ctx).map((r, idx) => `
                <div class="card mb-2 ${idx === 0 && r.score > 5 ? 'border-success' : ''}">
                    <div class="card-body d-flex align-items-center gap-3 py-2">
                        ${r.image ? `<img src="${escapeHtml(r.image)}" style="width:50px;height:75px;object-fit:cover;" class="rounded" onerror="this.style.display='none'">` : '<div style="width:50px"></div>'}
                        <div class="flex-grow-1 overflow-hidden">
                            <div class="fw-bold text-truncate" title="${escapeHtml(r.title)}">${escapeHtml(r.title)}</div>
                            <small class="text-muted text-truncate d-block">${escapeHtml(r.link)}</small>
                        </div>
                        <button class="btn btn-sm btn-primary flex-shrink-0 clu-src-grab"
                            data-source="getcomics" data-link="${escapeHtml(r.link)}"
                            data-title="${escapeHtml(r.title)}" title="Download">
                            <i class="bi bi-download"></i>
                        </button>
                    </div>
                </div>`).join('');
                return sectionHeader('GetComics', 'globe') + cards;
            },
            idleIcon: 'bi-download',
            async queue(btn, ctx) {
                // GetComics names the file after the post title, not the wanted
                // issue — the post is the authority on what is inside it.
                const title = btn.dataset.title || '';
                const filename = title.replace(/[^a-zA-Z0-9\s\-#]/g, '').trim() + '.cbz';
                const resp = await fetch('/api/getcomics/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: btn.dataset.link, filename: filename }),
                });
                const data = await resp.json();
                return {
                    ok: !!data.success,
                    error: data.error,
                    downloadId: data.download_id,
                    message: 'Download queued successfully!',
                };
            },
        },

        usenet: {
            label: 'Usenet (Indexers)',
            icon: 'hdd-network',
            async build(ctx) {
                const resp = await fetch('/api/usenet/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        series: ctx.series,
                        issue: ctx.issue,
                        // Without a year every volume's #8 scores the same; with
                        // it, wrong-year releases fall below the accept line.
                        issue_year: ctx.year ? Number(ctx.year) : null,
                    }),
                });
                const data = await resp.json();

                // No indexers configured -> Usenet isn't set up; stay quiet.
                if (!data.success || !data.has_indexers) return '';

                const header = sectionHeader('Usenet (Indexers)', 'hdd-network');
                const clientNote = data.has_client ? '' : alertBox(
                    'alert-warning', 'exclamation-triangle',
                    'No active download client — set one active on the Download Clients tab to send NZBs.'
                );
                const errNote = (data.errors && data.errors.length)
                    ? alertBox('alert-danger', 'exclamation-octagon',
                        data.errors.map(escapeHtml).join('<br>')) : '';

                if (!data.results || !data.results.length) {
                    return header + clientNote + errNote +
                        '<div class="text-muted small mb-2">No Usenet results found.</div>';
                }

                const card = (r) => scoredCard(
                    r,
                    `${escapeHtml(r.indexer_name || '')} ${fmtSize(r.size)} · score ${r.score}`,
                    `data-source="usenet" data-nzb-url="${escapeHtml(r.nzb_url)}"`,
                    data.has_client
                );
                return header + clientNote + errNote +
                    scoredResultList('usenet', data.results, card);
            },
            idleIcon: 'bi-cloud-download',
            async queue(btn, ctx) {
                const resp = await fetch('/api/usenet/grab', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        nzb_url: btn.dataset.nzbUrl,
                        filename: targetFilename(ctx),
                        series: ctx.series,
                        issue: ctx.issue,
                    }),
                });
                const data = await resp.json();
                return {
                    ok: !!data.success,
                    error: data.error || 'grab failed',
                    downloadId: data.download_id,
                    message: 'Sent to download client!',
                };
            },
        },

        dcpp: {
            label: 'DC++ (Hubs)',
            icon: 'diagram-3',
            async build(ctx) {
                const resp = await fetch('/api/dcpp/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        series: ctx.series,
                        issue: ctx.issue,
                        issue_year: ctx.year ? Number(ctx.year) : null,
                    }),
                });
                const data = await resp.json();

                // No active DC++ client -> DC++ isn't set up; stay quiet.
                if (!data.success || !data.has_client) return '';

                const header = sectionHeader('DC++ (Hubs)', 'diagram-3');
                const errNote = (data.errors && data.errors.length)
                    ? alertBox('alert-danger', 'exclamation-octagon',
                        data.errors.map(escapeHtml).join('<br>')) : '';

                if (!data.results || !data.results.length) {
                    return header + errNote +
                        '<div class="text-muted small mb-2">No DC++ results found.</div>';
                }

                const card = (r) => {
                    const users = r.users ? `${r.users} user${r.users === 1 ? '' : 's'} · ` : '';
                    return scoredCard(
                        r,
                        `${users}${fmtSize(r.size)} · score ${r.score}`,
                        `data-source="dcpp" data-token="${escapeHtml(r.result_token)}"`,
                        true
                    );
                };
                return header + errNote + scoredResultList('dcpp', data.results, card);
            },
            idleIcon: 'bi-cloud-download',
            async queue(btn, ctx) {
                const resp = await fetch('/api/dcpp/grab', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        result_token: btn.dataset.token,
                        filename: targetFilename(ctx),
                        series: ctx.series,
                        issue: ctx.issue,
                    }),
                });
                const data = await resp.json();
                return {
                    ok: !!data.success,
                    error: data.error || 'grab failed',
                    downloadId: data.download_id,
                    message: 'Queued in AirDC++!',
                };
            },
        },
    };

    /**
     * Fetch the user's source priority.
     *
     * Falls back to GetComics alone if the endpoint is unreachable, matching
     * the server-side default, so the modal still works.
     */
    async function fetchSourceOrder() {
        try {
            const resp = await fetch('/api/download-clients/sources');
            const data = await resp.json();
            if (data && data.success && Array.isArray(data.order) && data.order.length) {
                return data.order.filter(s => SOURCES[s]);
            }
        } catch (e) { /* fall through */ }
        return ['getcomics'];
    }

    CLU.createSourceSearch = function createSourceSearch(options) {
        const resultsEl = options.resultsEl;
        const getContext = options.getContext;
        const toast = options.toast || function () {};
        const onQueued = options.onQueued || function () {};

        // One delegated listener for the whole results area, installed once —
        // re-rendering the HTML cannot detach it.
        resultsEl.addEventListener('click', function (event) {
            const toggle = event.target.closest('.clu-src-toggle');
            if (toggle) {
                handleToggle(toggle);
                return;
            }
            const grab = event.target.closest('.clu-src-grab');
            if (grab) {
                handleGrab(grab);
            }
        });

        function handleToggle(btn) {
            const el = document.getElementById(btn.dataset.target);
            if (!el) return;
            const hidden = el.classList.toggle('d-none');
            const count = Number(el.dataset.count) || 0;
            btn.innerHTML = `<i class="bi bi-chevron-${hidden ? 'down' : 'up'} me-1"></i>` +
                othersLabel(count, !hidden, !!el.dataset.hasMatch);
        }

        async function handleGrab(btn) {
            const source = SOURCES[btn.dataset.source];
            if (!source || btn.disabled) return;

            const ctx = getContext();
            const idle = `<i class="bi ${source.idleIcon}"></i>`;
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
            try {
                const outcome = await source.queue(btn, ctx);
                if (outcome.ok) {
                    btn.innerHTML = '<i class="bi bi-check"></i>';
                    btn.classList.remove('btn-primary');
                    btn.classList.add('btn-success');
                    toast(outcome.message, 'success');
                    onQueued({
                        source: btn.dataset.source,
                        downloadId: outcome.downloadId,
                        issue: ctx.issue,
                    });
                } else {
                    btn.disabled = false;
                    btn.innerHTML = idle;
                    toast('Error: ' + outcome.error, 'danger');
                }
            } catch (e) {
                btn.disabled = false;
                btn.innerHTML = idle;
                toast('Error: ' + e.message, 'danger');
            }
        }

        /** Query every configured source and render the sections in priority order. */
        async function run(query) {
            if (!query) return;

            resultsEl.innerHTML = `
            <div class="text-center py-4">
                <div class="spinner-border text-primary"></div>
                <p class="mt-2">Searching...</p>
            </div>`;

            const order = await fetchSourceOrder();
            const ctx = Object.assign({ query: query }, getContext());

            // All sources run concurrently; the priority order decides only how
            // the finished sections are stacked.
            const sections = await Promise.all(order.map(async (key) => {
                try {
                    return await SOURCES[key].build(ctx);
                } catch (e) {
                    return alertBox('alert-danger', 'exclamation-octagon',
                        `${escapeHtml(SOURCES[key].label)} error: ${escapeHtml(e.message)}`);
                }
            }));

            const combined = sections.join('');
            resultsEl.innerHTML = combined.trim() ? combined : `
            <div class="text-center text-muted py-4">
                <i class="bi bi-emoji-frown display-4 opacity-25"></i>
                <p class="mt-2">No results found</p>
            </div>`;
        }

        return { run: run };
    };
})(window, document);
