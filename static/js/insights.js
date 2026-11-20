/**
 * Insights page.
 *
 * Server-rendered data arrives in <script type="application/json"
 * id="insightsData">; everything else is fetched. This file was inline in
 * templates/insights.html, where it could not be cached or syntax-checked.
 *
 * Reading numbers come from two places on purpose:
 *   /api/reading-stats             the three "Reading Stats" tiles, per year
 *   /api/reading-insights/summary  goal dial, streaks, weeks, shelf
 * The year selector drives both, so the dial and the tiles describe one year.
 */
document.addEventListener('DOMContentLoaded', function () {
    'use strict';

    const pageData = JSON.parse(document.getElementById('insightsData').textContent);
    const computedStyle = getComputedStyle(document.documentElement);

    function getCssVar(name, fallback) {
        const value = computedStyle.getPropertyValue(name).trim();
        return value || fallback;
    }

    // A theme colour at a given opacity, for fills behind a solid hover colour.
    function colorWithOpacity(cssVar, fallback, opacity) {
        const color = getCssVar(cssVar, fallback);
        if (color.startsWith('rgb')) {
            return color.replace('rgb', 'rgba').replace(')', `, ${opacity})`);
        }
        if (color.startsWith('#')) {
            const r = parseInt(color.slice(1, 3), 16);
            const g = parseInt(color.slice(3, 5), 16);
            const b = parseInt(color.slice(5, 7), 16);
            return `rgba(${r}, ${g}, ${b}, ${opacity})`;
        }
        return color;
    }

    const primaryColor = getCssVar('--bs-primary', '#0d6efd');
    const successColor = getCssVar('--bs-success', '#198754');
    const infoColor = getCssVar('--bs-info', '#0dcaf0');
    const warningColor = getCssVar('--bs-warning', '#ffc107');
    const dangerColor = getCssVar('--bs-danger', '#dc3545');
    const secondaryColor = getCssVar('--bs-secondary', '#6c757d');
    const textColor = getCssVar('--bs-body-color', '#212529');
    const gridColor = getCssVar('--bs-border-color', 'rgba(0, 0, 0, 0.1)');

    Chart.defaults.color = textColor;
    Chart.defaults.borderColor = gridColor;
    Chart.defaults.font.family = getCssVar('--bs-body-font-family',
        "'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif");

    const chartColors = [primaryColor, successColor, warningColor, infoColor, dangerColor, secondaryColor];

    const THUMBNAIL_FALLBACK = "data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 150%22%3E%3Crect fill=%22%23dee2e6%22 width=%22100%22 height=%22150%22/%3E%3Ctext x=%2250%22 y=%2275%22 text-anchor=%22middle%22 fill=%22%236c757d%22 font-size=%2210%22%3ENo Image%3C/text%3E%3C/svg%3E";

    function plural(count, word) {
        return `${count.toLocaleString()} ${word}${count === 1 ? '' : 's'}`;
    }

    // empty_state(hidden=True) renders style="display: none", so visibility is
    // toggled through the style, not the hidden attribute.
    function setVisible(element, visible) {
        element.style.display = visible ? '' : 'none';
    }

    function folderUrl(path) {
        const cut = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
        return '/collection?path=' + encodeURIComponent(path.substring(0, cut));
    }

    // =====================================================================
    // Collection charts (server-rendered data)
    // =====================================================================

    function horizontalBarOptions(tooltipLabel, tickCallback) {
        const options = {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: {
                    beginAtZero: true,
                    ticks: { precision: 0, font: { size: 11 } },
                    grid: { color: gridColor, drawBorder: false }
                },
                y: { ticks: { font: { size: 11 } }, grid: { display: false } }
            }
        };
        if (tooltipLabel) {
            options.plugins.tooltip = { callbacks: { label: ctx => tooltipLabel(ctx.parsed.x) } };
        }
        if (tickCallback) options.scales.x.ticks.callback = tickCallback;
        return options;
    }

    function shorten(name) {
        return name.length > 45 ? name.substring(0, 45) + '...' : name;
    }

    new Chart(document.getElementById('fileTypeChart'), {
        type: 'doughnut',
        data: {
            labels: pageData.file_types.map(item => item.type.toUpperCase()),
            datasets: [{
                data: pageData.file_types.map(item => item.count),
                backgroundColor: chartColors,
                borderWidth: 0,
                hoverOffset: 8
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'right',
                    labels: { padding: 16, usePointStyle: true, pointStyle: 'circle', font: { size: 12 } }
                }
            },
            cutout: '65%'
        }
    });

    const publisherOptions = horizontalBarOptions();
    publisherOptions.scales.y.ticks.font.size = 12;
    new Chart(document.getElementById('publishersChart'), {
        type: 'bar',
        data: {
            labels: pageData.top_publishers.map(item => item.name),
            datasets: [{
                label: 'Files',
                data: pageData.top_publishers.map(item => item.count),
                backgroundColor: colorWithOpacity('--bs-success', '#198754', 0.7),
                hoverBackgroundColor: successColor,
                borderRadius: 4,
                borderSkipped: false
            }]
        },
        options: publisherOptions
    });

    if (pageData.largest_comics && pageData.largest_comics.length > 0) {
        new Chart(document.getElementById('largestComicsChart'), {
            type: 'bar',
            data: {
                labels: pageData.largest_comics.map(c => shorten(c.name)),
                datasets: [{
                    label: 'Size (MB)',
                    data: pageData.largest_comics.map(c => (c.size / 1024 / 1024).toFixed(1)),
                    backgroundColor: colorWithOpacity('--bs-info', '#0dcaf0', 0.7),
                    hoverBackgroundColor: infoColor,
                    borderRadius: 4,
                    borderSkipped: false
                }]
            },
            options: horizontalBarOptions(value => value + ' MB', value => value + ' MB')
        });
    }

    if (pageData.top_series && pageData.top_series.length > 0) {
        new Chart(document.getElementById('topSeriesChart'), {
            type: 'bar',
            data: {
                labels: pageData.top_series.map(s => shorten(s.name)),
                datasets: [{
                    label: 'Issues',
                    data: pageData.top_series.map(s => s.count),
                    backgroundColor: colorWithOpacity('--bs-warning', '#ffc107', 0.7),
                    hoverBackgroundColor: warningColor,
                    borderRadius: 4,
                    borderSkipped: false
                }]
            },
            options: horizontalBarOptions(value => value + ' issues')
        });
    }

    // =====================================================================
    // Reading activity line and month heatmap (server-rendered data)
    // =====================================================================

    // Daily data carries `date`, monthly data `month`.
    const historyData = pageData.reading_history.map(item => item.count);
    new Chart(document.getElementById('readingHistoryChart'), {
        type: 'line',
        data: {
            labels: pageData.reading_history.map(item => item.date || item.month),
            datasets: [{
                label: 'Issues Read',
                data: historyData,
                borderColor: primaryColor,
                backgroundColor: colorWithOpacity('--bs-primary', '#0d6efd', 0.15),
                borderWidth: 2,
                fill: true,
                tension: 0.3,
                pointRadius: historyData.length > 60 ? 0 : 3,
                pointHoverRadius: 5,
                pointBackgroundColor: primaryColor,
                pointBorderColor: getCssVar('--bs-body-bg', '#fff'),
                pointBorderWidth: 2
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { intersect: false, mode: 'index' },
            // A day on the line opens that day. Daily labels are MM-DD-YYYY.
            onClick: (event, elements) => {
                if (!elements.length) return;
                const match = /^(\d{2})-(\d{2})-(\d{4})$/.exec(pageData.reading_history[elements[0].index].date || '');
                if (match) openDay(`${match[3]}-${match[1]}-${match[2]}`);
            },
            onHover: (event, elements) => {
                event.native.target.style.cursor = elements.length ? 'pointer' : 'default';
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        title: context => context[0].label,
                        label: context => plural(context.parsed.y, 'issue'),
                        footer: () => 'Click to open the day'
                    }
                }
            },
            scales: {
                y: {
                    beginAtZero: true,
                    ticks: { precision: 0, font: { size: 11 } },
                    grid: { color: gridColor, drawBorder: false }
                },
                x: {
                    ticks: { font: { size: 10 }, maxRotation: 45, autoSkip: true, maxTicksLimit: 12 },
                    grid: { display: false }
                }
            }
        }
    });

    const heatmapMonths = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const heatmapContainer = document.getElementById('reading-heatmap');

    function renderHeatmap() {
        const heatmapData = pageData.reading_heatmap;
        if (Object.keys(heatmapData).length === 0) {
            heatmapContainer.replaceChildren(CLU.emptyState({ icon: 'bi-calendar-x', title: 'No reading history yet', compact: true }));
            return;
        }

        let maxCount = 0;
        Object.values(heatmapData).forEach(monthCounts => {
            monthCounts.forEach(c => { if (c > maxCount) maxCount = c; });
        });

        let html = '<div class="heatmap-grid">';
        html += '<div class="heatmap-row"><div class="heatmap-year"></div>';
        heatmapMonths.forEach(m => { html += `<div class="heatmap-month-label">${m}</div>`; });
        html += '</div>';

        // Newest year first. Years are digits from the server.
        Object.keys(heatmapData).sort((a, b) => b - a).forEach(year => {
            const safeYear = CLU.escapeHtml(String(year));
            html += `<div class="heatmap-row"><div class="heatmap-year">${safeYear}</div>`;
            heatmapData[year].forEach((count, monthIdx) => {
                const n = Number(count) || 0;
                const intensity = maxCount > 0 ? n / maxCount : 0;
                html += `<div class="heatmap-cell"
                             data-count="${n}"
                             data-year="${safeYear}"
                             data-month="${monthIdx + 1}"
                             style="--intensity: ${intensity}"
                             title="${heatmapMonths[monthIdx]} ${safeYear}: ${n} comics read"></div>`;
            });
            html += '</div>';
        });
        html += '</div>';
        heatmapContainer.innerHTML = html;
    }

    renderHeatmap();

    // A month with reads opens that month on the Timeline.
    heatmapContainer.addEventListener('click', function (e) {
        const cell = e.target.closest('.heatmap-cell');
        if (cell && parseInt(cell.dataset.count, 10) > 0) {
            window.location.href = `/timeline?year=${cell.dataset.year}&month=${cell.dataset.month}`;
        }
    });

    // =====================================================================
    // Goal dial, streaks, weeks and shelf (/api/reading-insights/summary)
    // =====================================================================

    const DIAL_CIRCUMFERENCE = 2 * Math.PI * 52;
    const dialProgress = document.getElementById('goalDialProgress');
    const dialValue = document.getElementById('goalDialValue');
    const dialCaption = document.getElementById('goalDialCaption');
    const goalHeadline = document.getElementById('goalHeadline');
    const goalDetail = document.getElementById('goalDetail');
    const goalYearLabel = document.getElementById('goalYearLabel');
    const editGoalBtn = document.getElementById('editGoalBtn');
    const goalModalEl = document.getElementById('goalModal');
    const goalModal = new bootstrap.Modal(goalModalEl);
    const goalInput = document.getElementById('goalInput');
    const goalModalYear = document.getElementById('goalModalYear');
    const goalSuggestion = document.getElementById('goalSuggestion');
    const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const WEEKDAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

    let summaryYear = null;       // the year the dial currently describes
    let lastGoal = null;
    let weekChart = null;
    let fiveWeekChart = null;

    function renderGoal(goal) {
        lastGoal = goal;
        goalYearLabel.textContent = goal.year;
        dialValue.textContent = goal.read.toLocaleString();

        const fraction = goal.goal ? Math.min(goal.read / goal.goal, 1) : 0;
        dialProgress.style.strokeDasharray = `${DIAL_CIRCUMFERENCE}`;
        dialProgress.style.strokeDashoffset = `${DIAL_CIRCUMFERENCE * (1 - fraction)}`;
        dialProgress.classList.toggle('reached', Boolean(goal.reached));

        if (!goal.goal) {
            dialCaption.textContent = goal.read === 1 ? 'issue read' : 'issues read';
            goalHeadline.textContent = 'No goal set';
            goalDetail.textContent = goal.complete || goal.read === 0
                ? 'Set a goal to track your pace through the year.'
                : `At this pace you will read about ${goal.projected.toLocaleString()} issues this year.`;
            editGoalBtn.innerHTML = '<i class="bi bi-bullseye me-1" aria-hidden="true"></i>Set a goal';
            return;
        }

        dialCaption.textContent = `of ${goal.goal.toLocaleString()}`;
        editGoalBtn.innerHTML = '<i class="bi bi-pencil me-1" aria-hidden="true"></i>Edit goal';

        if (goal.reached) {
            goalHeadline.textContent = 'Goal reached';
            const extra = goal.read - goal.goal;
            goalDetail.textContent = extra > 0
                ? `${plural(extra, 'issue')} past your goal.`
                : 'You hit your goal exactly.';
        } else if (goal.complete) {
            goalHeadline.textContent = `${goal.pct}% of your goal`;
            goalDetail.textContent = `${plural(goal.goal - goal.read, 'issue')} short.`;
        } else {
            goalHeadline.textContent = goal.delta >= 0
                ? (goal.delta === 0 ? 'Right on pace' : `${plural(goal.delta, 'issue')} ahead of pace`)
                : `${plural(-goal.delta, 'issue')} behind pace`;
            const parts = [`On course for ${goal.projected.toLocaleString()}`];
            if (goal.per_week_needed !== null) {
                parts.push(`${goal.per_week_needed} a week reaches your goal`);
            }
            goalDetail.textContent = parts.join(' · ') + '.';
        }
    }

    function renderStreak(streak) {
        document.getElementById('statCurrentStreak').textContent = streak.current.toLocaleString();
        document.getElementById('statLongestStreak').textContent = streak.longest.toLocaleString();
        const range = document.getElementById('statLongestStreakRange');
        if (streak.longest_start) {
            const fmt = iso => new Date(iso + 'T00:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
            range.textContent = streak.longest_start === streak.longest_end
                ? fmt(streak.longest_start)
                : `${fmt(streak.longest_start)} – ${fmt(streak.longest_end)}`;
        } else {
            range.textContent = '';
        }
    }

    function renderWeeks(weeks) {
        const thisWeek = weeks.this_week;
        const lastWeek = weeks.last_week;
        document.getElementById('weekThisCount').textContent = thisWeek.issues.toLocaleString();
        document.getElementById('weekLastCount').textContent = lastWeek.issues.toLocaleString();

        // The fair comparison is last week up to the same weekday.
        const diff = thisWeek.issues - weeks.last_week_to_date;
        const note = document.getElementById('weekCompareNote');
        const day = WEEKDAYS[weeks.weekday];
        if (thisWeek.issues === 0 && lastWeek.issues === 0) {
            note.textContent = 'Nothing read this week or last.';
        } else if (diff === 0) {
            note.textContent = `Level with last week by ${day}.`;
        } else {
            note.textContent = `${plural(Math.abs(diff), 'issue')} ${diff > 0 ? 'ahead of' : 'behind'} last week by ${day}.`;
        }

        if (weekChart) weekChart.destroy();
        weekChart = new Chart(document.getElementById('weekCompareChart'), {
            type: 'bar',
            data: {
                labels: WEEKDAYS,
                datasets: [
                    {
                        label: 'Last week',
                        data: lastWeek.by_day,
                        backgroundColor: colorWithOpacity('--bs-secondary', '#6c757d', 0.45),
                        borderRadius: 3
                    },
                    {
                        label: 'This week',
                        data: thisWeek.by_day,
                        backgroundColor: primaryColor,
                        borderRadius: 3
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                // A bar opens that day. Dataset 0 is last week, 1 is this week.
                onClick: (event, elements) => {
                    if (!elements.length || !elements[0].element.$context.parsed.y) return;
                    const offset = elements[0].index - (elements[0].datasetIndex === 0 ? 7 : 0);
                    openDay(isoDatePlusDays(weeks.week_start, offset));
                },
                onHover: (event, elements) => {
                    event.native.target.style.cursor = elements.length ? 'pointer' : 'default';
                },
                plugins: {
                    legend: { position: 'bottom', labels: { usePointStyle: true, pointStyle: 'circle', font: { size: 11 } } },
                    tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${plural(ctx.parsed.y, 'issue')}` } }
                },
                scales: {
                    y: { beginAtZero: true, ticks: { precision: 0, font: { size: 10 } }, grid: { color: gridColor } },
                    x: { ticks: { font: { size: 10 } }, grid: { display: false } }
                }
            }
        });
    }

    function renderFiveWeeks(data) {
        const container = document.getElementById('fiveWeekContainer');
        const empty = document.getElementById('fiveWeekEmpty');
        if (fiveWeekChart) { fiveWeekChart.destroy(); fiveWeekChart = null; }
        const hasData = data.series.length > 0;
        setVisible(container, hasData);
        setVisible(empty, !hasData);
        if (!hasData) return;

        fiveWeekChart = new Chart(document.getElementById('fiveWeekChart'), {
            type: 'bar',
            data: {
                labels: data.weeks.map(w => w.label),
                datasets: data.series.map((row, index) => ({
                    label: row.name,
                    data: row.counts,
                    // "Other" is always the neutral band, wherever it falls.
                    backgroundColor: row.name === 'Other' ? secondaryColor : chartColors[index % 5],
                    borderRadius: 2,
                    borderSkipped: false
                }))
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            usePointStyle: true, pointStyle: 'rectRounded', font: { size: 11 },
                            generateLabels: chart => Chart.defaults.plugins.legend.labels.generateLabels(chart)
                                .map(item => Object.assign(item, { text: item.text.length > 28 ? item.text.substring(0, 28) + '…' : item.text }))
                        }
                    },
                    tooltip: {
                        callbacks: {
                            title: items => `Week of ${items[0].label}`,
                            label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y}`,
                            footer: items => `Total: ${plural(data.totals[items[0].dataIndex], 'issue')}`
                        }
                    }
                },
                scales: {
                    x: { stacked: true, grid: { display: false }, ticks: { font: { size: 11 } } },
                    y: { stacked: true, beginAtZero: true, ticks: { precision: 0, font: { size: 11 } }, grid: { color: gridColor } }
                }
            }
        });
    }

    function shelfThumb(item, extra) {
        return `
            <a class="recently-read-item" href="${CLU.escapeHtml(folderUrl(item.path))}"
               title="${CLU.escapeHtml(item.name)}">
                <img class="recently-read-thumbnail" src="${CLU.escapeHtml(item.thumbnail_url)}"
                     alt="${CLU.escapeHtml(item.name)}" loading="lazy">
                ${extra || ''}
            </a>`;
    }

    function renderShelf(shelf) {
        const openGrid = document.getElementById('open-now-grid');
        const openEmpty = document.getElementById('open-now-empty');
        openGrid.innerHTML = shelf.open.map(item => shelfThumb(item, `
            <div class="progress shelf-progress" role="progressbar" aria-label="${CLU.escapeHtml(item.name)} progress"
                 aria-valuenow="${Number(item.percent) || 0}" aria-valuemin="0" aria-valuemax="100">
                <div class="progress-bar" style="width: ${Number(item.percent) || 0}%"></div>
            </div>
            <div class="recently-read-date">Page ${Number(item.page) || 0} of ${Number(item.total_pages) || 0}</div>`)).join('');
        setVisible(openGrid, shelf.open.length > 0);
        setVisible(openEmpty, shelf.open.length === 0);

        const grid = document.getElementById('recently-read-grid');
        const empty = document.getElementById('recently-read-empty');
        grid.innerHTML = shelf.finished.map(item => shelfThumb(item)).join('');
        setVisible(grid, shelf.finished.length > 0);
        setVisible(empty, shelf.finished.length === 0);

        // A missing cover gets the placeholder. Set here, not as an inline
        // onerror: the handler would be a string of markup inside markup.
        document.querySelectorAll('#open-now-grid img, #recently-read-grid img').forEach(img => {
            img.addEventListener('error', () => { img.src = THUMBNAIL_FALLBACK; }, { once: true });
        });
    }

    async function loadSummary(year) {
        const query = year && year !== 'all' ? `?year=${encodeURIComponent(year)}` : '';
        try {
            const response = await fetch('/api/reading-insights/summary' + query);
            const data = await response.json();
            if (!data.success) throw new Error(data.error || 'No summary');
            summaryYear = data.goal.year;
            renderGoal(data.goal);
            renderStreak(data.streak);
            renderWeeks(data.weeks);
            renderFiveWeeks(data.five_weeks);
            renderShelf(data.shelf);
        } catch (error) {
            console.error('Error loading reading insights:', error);
            goalHeadline.textContent = 'Could not load your reading summary';
            goalDetail.textContent = '';
        }
    }

    editGoalBtn.addEventListener('click', function () {
        if (!lastGoal) return;
        goalModalYear.textContent = lastGoal.year;
        goalInput.value = lastGoal.goal || '';
        goalSuggestion.textContent = !lastGoal.complete && lastGoal.projected > 0
            ? `At your current pace you will read about ${lastGoal.projected.toLocaleString()} issues in ${lastGoal.year}.`
            : '';
        goalModal.show();
    });

    goalModalEl.addEventListener('shown.bs.modal', () => goalInput.select());

    async function saveGoal(goal) {
        try {
            const response = await fetch('/api/account/reading-goal', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ year: summaryYear, goal })
            });
            const data = await response.json();
            if (!data.success) {
                CLU.showError(data.error || 'Could not save the goal');
                return;
            }
            goalModal.hide();
            CLU.showSuccess(goal ? `Goal for ${summaryYear} set to ${plural(goal, 'issue')}` : `Goal for ${summaryYear} removed`);
            loadSummary(summaryYear);
        } catch (error) {
            CLU.showError('Could not save the goal: ' + error.message);
        }
    }

    document.getElementById('goalForm').addEventListener('submit', function (e) {
        e.preventDefault();
        const goal = parseInt(goalInput.value, 10);
        if (!Number.isFinite(goal) || goal < 1) {
            CLU.showError('Enter the number of issues you want to read');
            return;
        }
        saveGoal(goal);
    });

    document.getElementById('clearGoalBtn').addEventListener('click', () => saveGoal(0));

    // =====================================================================
    // Reading clock (/api/reading-insights/clock) and the day view
    // =====================================================================

    let clockChart = null;
    let weekdayChart = null;

    function formatDuration(seconds) {
        const minutes = Math.round(seconds / 60);
        if (minutes < 60) return `${minutes}m`;
        return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
    }

    function hourLabel(hour) {
        const h = hour % 12 === 0 ? 12 : hour % 12;
        return `${h}${hour % 24 < 12 ? 'am' : 'pm'}`;
    }

    const CLOCK_SOURCE_NOTES = {
        sessions: 'From your reading sessions.',
        finish_times: 'Estimated from when each issue was finished. Reading in the CLU reader records exact sessions from now on.',
        mixed: 'Reading sessions where they exist; earlier days are estimated from when each issue was finished.',
        none: ''
    };

    function renderClock(clock) {
        const hasData = clock.source !== 'none';
        setVisible(document.getElementById('clockContainer'), hasData);
        setVisible(document.getElementById('weekdayContainer'), hasData);
        setVisible(document.getElementById('clockEmpty'), !hasData);
        if (clockChart) { clockChart.destroy(); clockChart = null; }
        if (weekdayChart) { weekdayChart.destroy(); weekdayChart = null; }

        const peak = document.getElementById('clockPeak');
        const note = document.getElementById('clockSourceNote');
        let noteText = CLOCK_SOURCE_NOTES[clock.source] || '';
        if (clock.untimed > 0) {
            noteText += ` ${plural(clock.untimed, 'issue')} marked as read without a reading time ${clock.untimed === 1 ? 'is' : 'are'} not shown.`;
        }
        note.textContent = noteText.trim();
        if (!hasData) { peak.textContent = ''; return; }

        peak.textContent = `Most reading around ${hourLabel(clock.peak_hour)}, and on ${WEEKDAY_NAMES[clock.peak_weekday]}s.`;

        clockChart = new Chart(document.getElementById('readingClockChart'), {
            type: 'polarArea',
            data: {
                labels: clock.hours.map((_, hour) => hourLabel(hour)),
                datasets: [{
                    data: clock.hours,
                    backgroundColor: colorWithOpacity('--bs-primary', '#0d6efd', 0.65),
                    borderColor: getCssVar('--bs-body-bg', '#fff'),
                    borderWidth: 1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            title: items => `${hourLabel(items[0].dataIndex)} – ${hourLabel(items[0].dataIndex + 1)}`,
                            label: ctx => formatDuration(ctx.parsed.r)
                        }
                    }
                },
                scales: {
                    r: {
                        beginAtZero: true,
                        ticks: { display: false },
                        grid: { color: gridColor },
                        angleLines: { display: false },
                        // Midnight at the top, like a 24-hour clock face.
                        pointLabels: {
                            display: true,
                            centerPointLabels: true,
                            font: { size: 10 },
                            callback: (label, index) => (index % 3 === 0 ? label : '')
                        }
                    }
                }
            }
        });

        weekdayChart = new Chart(document.getElementById('weekdayChart'), {
            type: 'bar',
            data: {
                labels: WEEKDAYS,
                datasets: [{
                    label: 'Time reading',
                    data: clock.weekdays,
                    backgroundColor: colorWithOpacity('--bs-info', '#0dcaf0', 0.7),
                    hoverBackgroundColor: infoColor,
                    borderRadius: 4,
                    borderSkipped: false
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { label: ctx => formatDuration(ctx.parsed.y) } }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        ticks: { font: { size: 11 }, callback: value => formatDuration(value) },
                        grid: { color: gridColor }
                    },
                    x: { ticks: { font: { size: 11 } }, grid: { display: false } }
                }
            }
        });
    }

    async function loadClock(year) {
        try {
            const response = await fetch(`/api/reading-insights/clock?year=${encodeURIComponent(year || 'all')}`);
            const data = await response.json();
            if (data.success) renderClock(data);
        } catch (error) {
            console.error('Error loading reading clock:', error);
        }
    }

    const dayModal = new bootstrap.Modal(document.getElementById('dayModal'));

    function timeOfDay(iso) {
        // The server sends the user's local wall-clock time; read it as text
        // so the browser's own timezone never moves it.
        const [hour, minute] = iso.substring(11, 16).split(':').map(Number);
        return `${hour % 12 === 0 ? 12 : hour % 12}:${String(minute).padStart(2, '0')}${hour < 12 ? 'am' : 'pm'}`;
    }

    function rulerBlock(minutes, className, title) {
        const left = (minutes[0] / 1440) * 100;
        // A sitting shorter than the ruler can draw is still shown.
        const width = Math.max(((minutes[1] - minutes[0]) / 1440) * 100, 0.6);
        return `<div class="day-ruler-block ${className}" style="left: ${Math.min(left, 99.4)}%; width: ${width}%"
                     title="${CLU.escapeHtml(title)}"></div>`;
    }

    function dayRow(item, timeText, detailText, estimated) {
        return `
            <a class="list-group-item list-group-item-action d-flex align-items-center gap-3"
               href="${CLU.escapeHtml(folderUrl(item.path))}">
                <img class="day-thumb" src="${CLU.escapeHtml(item.thumbnail_url)}" alt="" loading="lazy">
                <div class="flex-grow-1 text-truncate">
                    <div class="fw-medium text-truncate" title="${CLU.escapeHtml(item.name)}">${CLU.escapeHtml(item.name)}</div>
                    <div class="text-muted small text-truncate">${CLU.escapeHtml(detailText)}</div>
                </div>
                <div class="text-end small text-nowrap">
                    <div>${CLU.escapeHtml(timeText)}</div>
                    ${estimated ? '<div class="text-muted">estimated</div>' : ''}
                </div>
            </a>`;
    }

    function renderDay(day) {
        const date = new Date(day.date + 'T00:00:00');
        document.getElementById('dayModalTitle').textContent = date.toLocaleDateString(undefined,
            { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
        document.getElementById('dayTimelineLink').href =
            `/timeline?year=${date.getFullYear()}&month=${date.getMonth() + 1}`;

        const parts = [plural(day.issues, 'issue') + ' finished'];
        if (day.pages) parts.push(plural(day.pages, 'page'));
        if (day.active_seconds) parts.push(formatDuration(day.active_seconds) + ' reading');
        if (day.hidden) parts.push(`${plural(day.hidden, 'hidden read')} not shown`);
        document.getElementById('daySummary').textContent = parts.join(' · ');

        const estimates = day.reads.filter(read => read.minutes);
        document.getElementById('dayRulerTrack').innerHTML =
            day.sessions.map(s => rulerBlock(s.minutes, 'session',
                `${s.name}: ${timeOfDay(s.start)} – ${timeOfDay(s.end)}`)).join('') +
            estimates.map(r => rulerBlock(r.minutes, 'estimate',
                `${r.name}: finished ${timeOfDay(r.at)}`)).join('');
        setVisible(document.getElementById('dayRuler'), day.sessions.length + estimates.length > 0);

        const legend = [];
        if (day.sessions.length) legend.push('<span class="day-ruler-key session"></span>Reading session');
        if (estimates.length) legend.push('<span class="day-ruler-key estimate"></span>Estimated from finish time');
        document.getElementById('dayRulerLegend').innerHTML = legend.join(' &nbsp; ');

        const rows = day.sessions.map(s => dayRow(s,
            `${timeOfDay(s.start)} – ${timeOfDay(s.end)}`,
            [s.series, s.pages ? plural(s.pages, 'page') : '', formatDuration(s.active_seconds) + ' active',
                s.finished ? 'finished' : ''].filter(Boolean).join(' · '), false));
        day.reads.filter(read => !read.has_session).forEach(read => {
            rows.push(dayRow(read,
                read.timed ? `Finished ${timeOfDay(read.at)}` : 'Marked as read',
                [read.series, read.pages ? plural(read.pages, 'page') : '',
                    read.timed ? formatDuration(read.seconds) + ' in total' : ''].filter(Boolean).join(' · '),
                Boolean(read.minutes)));
        });

        const list = document.getElementById('dayList');
        if (rows.length === 0) {
            list.replaceChildren(CLU.emptyState({ icon: 'bi-calendar-x', title: 'Nothing to show for this day', compact: true }));
        } else {
            list.innerHTML = rows.join('');
            list.querySelectorAll('img').forEach(img => {
                img.addEventListener('error', () => { img.src = THUMBNAIL_FALLBACK; }, { once: true });
            });
        }
    }

    async function openDay(isoDate) {
        try {
            const response = await fetch(`/api/reading-insights/day/${encodeURIComponent(isoDate)}`);
            const data = await response.json();
            if (!data.success) throw new Error(data.error || 'No data');
            renderDay(data);
            dayModal.show();
        } catch (error) {
            CLU.showError('Could not load that day: ' + error.message);
        }
    }

    function isoDatePlusDays(iso, days) {
        const date = new Date(iso + 'T00:00:00Z');
        date.setUTCDate(date.getUTCDate() + days);
        return date.toISOString().substring(0, 10);
    }

    // =====================================================================
    // Reading stats tiles and the year selector
    // =====================================================================

    const readingStatsYearSelect = document.getElementById('readingStatsYear');
    const trendsYearFilter = document.getElementById('trendsYearFilter');

    function fillYearSelect(select, years, selected) {
        select.innerHTML = '<option value="all">All Time</option>';
        years.forEach(year => {
            const option = document.createElement('option');
            option.value = year;
            option.textContent = year;
            if (year === selected) option.selected = true;
            select.appendChild(option);
        });
    }

    async function loadReadingStats(year) {
        try {
            const response = await fetch(`/api/reading-stats?year=${encodeURIComponent(year)}`);
            const data = await response.json();
            if (data.success && data.stats) {
                document.getElementById('statIssuesRead').textContent = data.stats.total_read.toLocaleString();
                document.getElementById('statPagesRead').textContent = data.stats.total_pages.toLocaleString();
                document.getElementById('statTimeReading').textContent = `${data.stats.hours}h ${data.stats.minutes}m`;
            }
            return data;
        } catch (error) {
            console.error('Error loading reading stats:', error);
            return null;
        }
    }

    // One request fills both year selectors (they used to fetch the list twice).
    async function initYearFilters() {
        const currentYear = new Date().getFullYear();
        const data = await loadReadingStats(currentYear);
        if (data && data.success && data.years) {
            fillYearSelect(readingStatsYearSelect, data.years, currentYear);
            fillYearSelect(trendsYearFilter, data.years, null);
        }
    }

    readingStatsYearSelect.addEventListener('change', function () {
        loadReadingStats(this.value);
        loadSummary(this.value);
        loadClock(this.value);
    });

    // =====================================================================
    // Reading trends and the metadata backfill
    // =====================================================================

    const backfillMetadataBtn = document.getElementById('backfillMetadataBtn');

    async function loadReadingTrends(year = 'all') {
        const fields = [
            { field: 'writer', elementId: 'topWritersList' },
            { field: 'penciller', elementId: 'topPencillersList' },
            { field: 'characters', elementId: 'topCharactersList' },
            { field: 'publisher', elementId: 'topPublishersList' }
        ];

        for (const { field, elementId } of fields) {
            const container = document.getElementById(elementId);
            try {
                const url = year === 'all'
                    ? `/api/reading-trends/${field}?limit=10`
                    : `/api/reading-trends/${field}?year=${encodeURIComponent(year)}&limit=10`;
                const response = await fetch(url);
                const data = await response.json();

                if (data.length === 0) {
                    container.replaceChildren(CLU.emptyState({ icon: 'bi-inbox', title: 'No data yet', compact: true }));
                } else {
                    const maxCount = data[0].count;
                    container.innerHTML = data.map(item => `
                        <a href="/browse/${field}/${encodeURIComponent(item.name)}" class="trend-item text-decoration-none">
                            <div class="d-flex justify-content-between align-items-center mb-1">
                                <span class="trend-name text-truncate" title="${CLU.escapeHtml(item.name)}">${CLU.escapeHtml(item.name)}</span>
                                <span class="trend-count badge bg-primary">${Number(item.count) || 0}</span>
                            </div>
                            <div class="progress" style="height: 4px;">
                                <div class="progress-bar" role="progressbar" style="width: ${(item.count / maxCount) * 100}%"></div>
                            </div>
                        </a>
                    `).join('');
                }
            } catch (error) {
                console.error(`Error loading ${field} trends:`, error);
                container.innerHTML = '<div class="text-center text-danger py-3">Error loading data</div>';
            }
        }
    }

    trendsYearFilter.addEventListener('change', function () {
        loadReadingTrends(this.value);
    });

    const backfillConfirmModal = new bootstrap.Modal(document.getElementById('backfillConfirmModal'));

    backfillMetadataBtn.addEventListener('click', function () {
        backfillConfirmModal.show();
    });

    // The finished run's report: a toast with the counts, then the
    // skipped-issues modal when anything was skipped.
    function showBackfillResult(result) {
        if (!result) {
            CLU.showError('The backfill finished but its report is unavailable');
            return;
        }
        if (result.error) {
            CLU.showError('Backfill: ' + result.error);
        }
        CLU.showSuccess(`Backfill complete! Updated: ${result.updated} comics, Skipped: ${result.skipped} comics`);
        loadReadingTrends(trendsYearFilter.value);

        const skipped = result.skipped_issues || [];
        if (skipped.length > 0) {
            const skippedList = document.getElementById('skippedIssuesList');
            skippedList.innerHTML = skipped.map(item => `
                <div class="list-group-item d-flex justify-content-between align-items-start">
                    <div class="me-auto">
                        <div class="fw-medium text-truncate" style="max-width: 500px;" title="${CLU.escapeHtml(item.file)}">${CLU.escapeHtml(item.file)}</div>
                    </div>
                    <span class="badge bg-secondary">${CLU.escapeHtml(item.reason)}</span>
                </div>
            `).join('') + (result.skipped_issues_truncated
                ? `<div class="list-group-item text-muted small">Only the first ${skipped.length} of ${Number(result.skipped) || 0} skipped comics are listed.</div>`
                : '');
            new bootstrap.Modal(document.getElementById('skippedIssuesModal')).show();
        }
    }

    // The run is a background operation: start it, follow /api/operation/<id>
    // (never /api/operations, which clears the header's notification queue),
    // then read the report.
    document.getElementById('confirmBackfillBtn').addEventListener('click', async function () {
        backfillConfirmModal.hide();

        const btn = backfillMetadataBtn;
        const originalHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Processing...';
        const restore = () => { btn.disabled = false; btn.innerHTML = originalHtml; };

        let data;
        try {
            const response = await fetch('/api/backfill-reading-metadata', { method: 'POST' });
            data = await response.json();
        } catch (error) {
            console.error('Backfill error:', error);
            CLU.showError('Could not start the backfill: ' + error.message);
            restore();
            return;
        }
        if (!data.success) {
            CLU.showError(data.error || 'Could not start the backfill');
            restore();
            return;
        }

        CLU.showToast('Backfill', 'Reading metadata from your reading history…', 'info');
        const opId = data.op_id;
        const timer = CLU.startPoll(signal =>
            fetch(`/api/operation/${encodeURIComponent(opId)}`, { signal })
                .then(r => r.json())
                .then(opData => {
                    if (!opData.success) return;  // transient; ask again next tick
                    const op = opData.operation || null;
                    if (op && (op.status === 'running' || op.status === 'queued')) {
                        if (op.total) {
                            btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>${Number(op.current) || 0} / ${Number(op.total) || 0}`;
                        }
                        return;
                    }
                    clearInterval(timer);
                    return fetch('/api/backfill-reading-metadata/result', { signal })
                        .then(r => r.json())
                        .then(res => showBackfillResult(res.result))
                        .catch(() => CLU.showError('Could not load the backfill report'))
                        .finally(restore);
                }), 2000);
    });

    // =====================================================================
    // Year Wrapped
    // =====================================================================

    const wrappedModal = document.getElementById('wrappedModal');
    const yearButtonsContainer = document.getElementById('yearButtonsContainer');
    const wrappedYearSelect = document.getElementById('wrappedYearSelect');
    const wrappedLoading = document.getElementById('wrappedLoading');
    const wrappedCarousel = document.getElementById('wrappedCarousel');
    const wrappedCarouselInner = document.getElementById('wrappedCarouselInner');
    const wrappedFooter = document.getElementById('wrappedFooter');
    const wrappedDownloadBtn = document.getElementById('wrappedDownloadBtn');
    const wrappedLoadingHtml = wrappedLoading.innerHTML;

    function resetWrappedModal() {
        wrappedYearSelect.style.display = 'block';
        wrappedLoading.style.display = 'none';
        // A failed run replaces the spinner with an error; put it back.
        wrappedLoading.innerHTML = wrappedLoadingHtml;
        wrappedCarousel.style.display = 'none';
        wrappedFooter.style.display = 'none';
        wrappedCarouselInner.innerHTML = '';
    }

    function loadImage(url) {
        return new Promise((resolve, reject) => {
            const img = new Image();
            img.onload = () => resolve(url);
            img.onerror = () => reject(new Error(`Failed to load ${url}`));
            img.src = url;
        });
    }

    async function generateWrapped(year) {
        wrappedYearSelect.style.display = 'none';
        wrappedLoading.style.display = 'block';

        try {
            // Loading the images is what renders them on the server.
            const images = await Promise.all(
                [1, 2, 3].map(i => loadImage(`/api/wrapped/${year}/image/${i}`))
            );

            wrappedCarouselInner.innerHTML = images.map((imgSrc, idx) => `
                <div class="carousel-item ${idx === 0 ? 'active' : ''}">
                    <div class="d-flex justify-content-center">
                        <img src="${imgSrc}" class="img-fluid rounded shadow" alt="Wrapped Slide ${idx + 1}"
                             style="max-height: 70vh; object-fit: contain;">
                    </div>
                </div>
            `).join('');

            wrappedDownloadBtn.href = `/api/wrapped/${year}/download`;
            wrappedLoading.style.display = 'none';
            wrappedCarousel.style.display = 'block';
            wrappedFooter.style.display = 'flex';
            new bootstrap.Carousel(wrappedCarousel);
        } catch (error) {
            console.error('Error generating wrapped:', error);
            wrappedLoading.innerHTML = '<p class="text-danger">Error generating images. Please try again.</p>';
        }
    }

    wrappedModal.addEventListener('show.bs.modal', async function () {
        resetWrappedModal();
        try {
            const response = await fetch('/api/wrapped/years');
            const data = await response.json();

            if (data.years && data.years.length > 0) {
                yearButtonsContainer.innerHTML = data.years.map(year =>
                    `<button class="btn btn-outline-primary btn-lg wrapped-year-btn" data-year="${Number(year)}">${Number(year)}</button>`
                ).join('');
                yearButtonsContainer.querySelectorAll('.wrapped-year-btn').forEach(btn => {
                    btn.addEventListener('click', () => generateWrapped(parseInt(btn.dataset.year, 10)));
                });
            } else {
                yearButtonsContainer.innerHTML = '<p class="text-muted">No reading data available yet. Start reading to generate your Wrapped!</p>';
            }
        } catch (error) {
            console.error('Error loading years:', error);
            yearButtonsContainer.innerHTML = '<p class="text-danger">Error loading years. Please try again.</p>';
        }
    });

    document.getElementById('wrappedBackBtn').addEventListener('click', resetWrappedModal);

    // =====================================================================
    // Start
    // =====================================================================

    initYearFilters();
    loadSummary(null);
    loadClock(new Date().getFullYear());
    loadReadingTrends();
});
