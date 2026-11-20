/**
 * CLU.wrappedStory: the full-screen Wrapped viewer, and the slide editor.
 *
 *   CLU.wrappedStory.open({
 *       manifestUrl,   // /api/wrapped/<period>/manifest
 *       downloadUrl,   // the server ZIP for the same period
 *       zipName,       // folder and file name for a ZIP built here (no ".zip")
 *       title          // shown above the slides
 *   }) -> Promise<boolean>   false when the period has nothing to show
 *
 * Markup: templates/partials/wrapped_story.html. Used by Year Wrapped on
 * Insights and Monthly Wrapped on the Timeline.
 *
 * Rules worth knowing before changing this:
 *
 * - Everything is keyed by slide id from the manifest, never by position. The
 *   monthly page used to hard-code two slides and the yearly page three.
 * - The timer for a slide starts when its image has decoded, not when it was
 *   requested: the server may be drawing it, and a slide must get its full
 *   time on screen.
 * - Only the next slide is preloaded. Each uncached slide is a Pillow render
 *   on a single-worker server; asking for all of them at once queues them
 *   behind each other and behind the rest of the app.
 * - With "reduce motion" on, nothing advances by itself.
 * - "Download All" is the server's ZIP unless a slide was edited here, in
 *   which case the ZIP is built in the browser from the same manifest.
 */
(function () {
    'use strict';

    window.CLU = window.CLU || {};

    const SLIDE_MS = 6000;
    // A press shorter than this is a tap (navigate); longer is a hold (pause).
    const TAP_MS = 250;
    const SWIPE_PX = 45;
    // Taller than this (height / width) scrolls rather than fits: a story
    // frame is 16:9, i.e. 1.78.
    const TALL_RATIO = 2.2;

    const EMOJIS = [
        '\u{1F4A5}', '\u{1F525}', '\u{2B50}', '\u{1F31F}', '\u{1F4AF}', '\u{1F389}', '\u{1F38A}', '\u{1F3C6}',
        '\u{1F4DA}', '\u{1F4D6}', '\u{1F4D8}', '\u{1F4D5}', '\u{1F4D7}', '\u{1F4D9}', '\u{1F4CC}', '\u{270F}',
        '\u{2764}', '\u{1F499}', '\u{1F49A}', '\u{1F49C}', '\u{1F9E1}', '\u{1F49B}', '\u{1F44D}', '\u{1F44F}',
        '\u{1F60D}', '\u{1F60E}', '\u{1F929}', '\u{1F973}', '\u{1F913}', '\u{1F47E}', '\u{1F916}', '\u{1F9B8}',
        '\u{1F4AA}', '\u{26A1}', '\u{1F308}', '\u{2728}', '\u{1F680}', '\u{1F30D}', '\u{1F3AF}', '\u{1F4A1}'
    ];

    let dom = null;         // looked up once, on first open
    let modal = null;
    let state = null;       // the open story, or null

    function byId(id) { return document.getElementById(id); }

    function init() {
        if (dom) return true;
        const root = byId('wrappedStoryModal');
        if (!root) return false;
        dom = {
            root,
            viewer: byId('wrappedStoryViewer'),
            editor: byId('wrappedStoryEditor'),
            segments: byId('wrappedStorySegments'),
            title: byId('wrappedStoryTitle'),
            count: byId('wrappedStoryCount'),
            pause: byId('wrappedStoryPause'),
            stage: byId('wrappedStoryStage'),
            loading: byId('wrappedStoryLoading'),
            image: byId('wrappedStoryImage'),
            prev: byId('wrappedStoryPrev'),
            next: byId('wrappedStoryNext'),
            customize: byId('wrappedStoryCustomize'),
            download: byId('wrappedStoryDownload'),
            canvas: byId('editorCanvas')
        };
        modal = new bootstrap.Modal(root);
        wireViewer();
        wireEditor();
        return true;
    }

    function reducedMotion() {
        return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    }

    // =====================================================================
    // Viewer
    // =====================================================================

    function slideSource(slide) {
        return state.edited[slide.id] || slide.url;
    }

    function setPaused(paused, byUser) {
        if (!state) return;
        state.paused = paused;
        if (byUser !== undefined) state.pausedByUser = byUser;
        dom.pause.setAttribute('aria-pressed', String(paused));
        dom.pause.setAttribute('aria-label', paused ? 'Play' : 'Pause');
        dom.pause.firstElementChild.className = paused ? 'bi bi-play-fill' : 'bi bi-pause-fill';
        state.lastTick = null;
    }

    function paintSegments() {
        const fills = dom.segments.querySelectorAll('.wrapped-story-segment > span');
        fills.forEach((fill, i) => {
            if (i < state.index) fill.style.width = '100%';
            else if (i > state.index) fill.style.width = '0';
            else fill.style.width = `${Math.min(state.elapsed / SLIDE_MS, 1) * 100}%`;
        });
    }

    function tick(now) {
        if (!state) return;
        state.frame = requestAnimationFrame(tick);
        if (state.paused || !state.ready || state.tall) { state.lastTick = null; return; }
        if (state.lastTick !== null) state.elapsed += now - state.lastTick;
        state.lastTick = now;
        if (state.elapsed >= SLIDE_MS) {
            if (state.index < state.slides.length - 1) {
                show(state.index + 1);
                return;
            }
            // The last slide stays up; the story does not loop or close.
            state.elapsed = SLIDE_MS;
            setPaused(true, true);
        }
        paintSegments();
    }

    function preload(index) {
        const slide = state.slides[index];
        if (!slide || state.edited[slide.id]) return;
        const img = new Image();
        img.src = slide.url;
    }

    function show(index) {
        const slide = state.slides[index];
        if (!slide) return;
        state.index = index;
        state.elapsed = 0;
        state.lastTick = null;
        state.ready = false;
        state.tall = false;
        const token = ++state.loadToken;

        dom.count.textContent = `${index + 1} of ${state.slides.length}`;
        dom.prev.disabled = index === 0;
        dom.next.disabled = index === state.slides.length - 1;
        dom.stage.classList.remove('tall');
        dom.stage.scrollTop = 0;
        dom.loading.style.display = 'flex';
        dom.image.style.visibility = 'hidden';
        dom.image.alt = slide.title;
        paintSegments();

        const img = new Image();
        img.onload = function () {
            // A slower earlier slide must not replace the one now showing.
            if (!state || token !== state.loadToken) return;
            dom.image.src = img.src;
            dom.image.style.visibility = 'visible';
            dom.loading.style.display = 'none';
            state.tall = img.naturalHeight / img.naturalWidth > TALL_RATIO;
            dom.stage.classList.toggle('tall', state.tall);
            state.ready = true;
            preload(index + 1);
        };
        img.onerror = function () {
            if (!state || token !== state.loadToken) return;
            dom.loading.style.display = 'none';
            CLU.showError(`Could not load "${slide.title}"`);
            setPaused(true, true);
        };
        img.src = slideSource(slide);
    }

    function go(delta) {
        if (!state) return;
        const target = state.index + delta;
        if (target < 0 || target >= state.slides.length) return;
        // Moving on by hand restarts a story that had stopped at its end.
        if (state.pausedByUser && state.elapsed >= SLIDE_MS) setPaused(false, false);
        show(target);
    }

    function wireViewer() {
        dom.prev.addEventListener('click', () => go(-1));
        dom.next.addEventListener('click', () => go(1));
        dom.pause.addEventListener('click', () => {
            if (!state) return;
            if (state.paused && state.elapsed >= SLIDE_MS) state.elapsed = 0;
            setPaused(!state.paused, !state.paused);
        });

        // Press and hold pauses; a quick tap navigates by which side it was on.
        let press = null;
        dom.stage.addEventListener('pointerdown', function (e) {
            if (!state || e.target.closest('button')) return;
            press = { at: performance.now(), x: e.clientX, y: e.clientY, wasPaused: state.paused };
            if (!state.paused) setPaused(true);
        });
        function release(e) {
            if (!press || !state) { press = null; return; }
            const held = performance.now() - press.at;
            const dx = e.clientX - press.x;
            const dy = e.clientY - press.y;
            const wasPaused = press.wasPaused;
            press = null;
            if (!wasPaused && !state.pausedByUser) setPaused(false);
            if (Math.abs(dx) > SWIPE_PX && Math.abs(dx) > Math.abs(dy)) {
                go(dx < 0 ? 1 : -1);
            } else if (held < TAP_MS && Math.abs(dx) < 10 && Math.abs(dy) < 10 && !state.tall) {
                const rect = dom.stage.getBoundingClientRect();
                go(e.clientX - rect.left < rect.width / 3 ? -1 : 1);
            }
        }
        dom.stage.addEventListener('pointerup', release);
        dom.stage.addEventListener('pointercancel', () => {
            if (press && state && !press.wasPaused && !state.pausedByUser) setPaused(false);
            press = null;
        });

        dom.root.addEventListener('keydown', function (e) {
            if (!state || state.editing) return;
            if (e.target.closest('input, textarea')) return;
            if (e.key === 'ArrowRight') { e.preventDefault(); go(1); }
            else if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1); }
            else if (e.key === ' ' && !e.target.closest('button, a')) { e.preventDefault(); dom.pause.click(); }
        });

        document.addEventListener('visibilitychange', function () {
            if (state && document.hidden && !state.paused) setPaused(true);
            else if (state && !document.hidden && !state.pausedByUser) setPaused(false);
        });

        dom.root.addEventListener('hidden.bs.modal', function () {
            if (state) cancelAnimationFrame(state.frame);
            state = null;
            dom.image.removeAttribute('src');
        });

        dom.customize.addEventListener('click', () => { if (state) openEditor(); });
        dom.download.addEventListener('click', downloadAll);
    }

    async function downloadAll(e) {
        if (!state) return;
        const edited = Object.keys(state.edited);
        if (edited.length === 0) return;        // the link is the server's ZIP

        e.preventDefault();
        if (typeof JSZip === 'undefined') {
            CLU.showError('Could not build the download with your edits. Reload the page and try again.');
            return;
        }
        try {
            const zip = new JSZip();
            const folder = zip.folder(state.options.zipName);
            for (const slide of state.slides) {
                const blob = await fetch(slideSource(slide)).then(r => {
                    if (!r.ok) throw new Error(slide.title);
                    return r.blob();
                });
                folder.file(slide.filename, blob);
            }
            const content = await zip.generateAsync({ type: 'blob' });
            const link = document.createElement('a');
            link.href = URL.createObjectURL(content);
            link.download = `${state.options.zipName}.zip`;
            link.click();
            URL.revokeObjectURL(link.href);
        } catch (error) {
            CLU.showError('Could not build the download: ' + error.message);
        }
    }

    // =====================================================================
    // Editor: text and emoji over the current slide
    // =====================================================================

    const editor = { overlays: null, selected: -1, dragging: -1, dx: 0, dy: 0, base: null, slide: null };

    function showEditor(visible) {
        state.editing = visible;
        dom.viewer.style.display = visible ? 'none' : '';
        dom.editor.style.display = visible ? '' : 'none';
    }

    function openEditor() {
        const slide = state.slides[state.index];
        setPaused(true, true);
        editor.slide = slide;
        // Edits are kept per slide, so reopening one continues where it was.
        editor.overlays = (state.overlays[slide.id] || []).map(item => Object.assign({}, item));
        editor.selected = -1;
        byId('textInputPanel').style.display = 'none';
        byId('emojiPickerPanel').style.display = 'none';

        // Always draw over the slide as the server drew it: the overlays are
        // re-applied, not baked into a copy of a copy.
        const img = new Image();
        img.onload = function () {
            if (!state) return;
            dom.canvas.width = img.naturalWidth;
            dom.canvas.height = img.naturalHeight;
            editor.base = img;
            drawEditor();
            showEditor(true);
        };
        img.onerror = () => CLU.showError('Could not open this slide for editing');
        img.src = slide.url;
    }

    function overlayFont(item) {
        return `${item.bold ? 'bold ' : ''}${item.fontSize}px sans-serif`;
    }

    function drawEditor(withSelection) {
        const ctx = dom.canvas.getContext('2d');
        if (!editor.base) return;
        ctx.clearRect(0, 0, dom.canvas.width, dom.canvas.height);
        ctx.drawImage(editor.base, 0, 0, dom.canvas.width, dom.canvas.height);
        editor.overlays.forEach((item, index) => {
            ctx.font = overlayFont(item);
            ctx.fillStyle = item.color;
            ctx.textBaseline = 'top';
            ctx.fillText(item.content, item.x, item.y);
            if (withSelection !== false && index === editor.selected) {
                const width = ctx.measureText(item.content).width;
                ctx.strokeStyle = '#ffffff';
                ctx.lineWidth = 3;
                ctx.setLineDash([8, 4]);
                ctx.strokeRect(item.x - 4, item.y - 4, width + 8, item.fontSize + 8);
                ctx.setLineDash([]);
            }
        });
    }

    function canvasPoint(e) {
        const rect = dom.canvas.getBoundingClientRect();
        return {
            x: (e.clientX - rect.left) * (dom.canvas.width / rect.width),
            y: (e.clientY - rect.top) * (dom.canvas.height / rect.height)
        };
    }

    function hitTest(point) {
        const ctx = dom.canvas.getContext('2d');
        // Topmost first.
        for (let i = editor.overlays.length - 1; i >= 0; i--) {
            const item = editor.overlays[i];
            ctx.font = overlayFont(item);
            const width = ctx.measureText(item.content).width;
            if (point.x >= item.x && point.x <= item.x + width &&
                point.y >= item.y && point.y <= item.y + item.fontSize) {
                return i;
            }
        }
        return -1;
    }

    function wireEditor() {
        const canvas = dom.canvas;

        // Pointer events cover mouse, touch and pen with one set of handlers.
        canvas.addEventListener('pointerdown', function (e) {
            const point = canvasPoint(e);
            const hit = hitTest(point);
            editor.selected = hit;
            editor.dragging = hit;
            if (hit >= 0) {
                editor.dx = point.x - editor.overlays[hit].x;
                editor.dy = point.y - editor.overlays[hit].y;
                canvas.setPointerCapture(e.pointerId);
                canvas.style.cursor = 'grabbing';
            }
            drawEditor();
        });
        canvas.addEventListener('pointermove', function (e) {
            if (editor.dragging < 0) return;
            const point = canvasPoint(e);
            const item = editor.overlays[editor.dragging];
            item.x = point.x - editor.dx;
            item.y = point.y - editor.dy;
            drawEditor();
        });
        function endDrag() {
            editor.dragging = -1;
            canvas.style.cursor = 'crosshair';
        }
        canvas.addEventListener('pointerup', endDrag);
        canvas.addEventListener('pointercancel', endDrag);

        byId('editorAddText').addEventListener('click', function () {
            const panel = byId('textInputPanel');
            panel.style.display = panel.style.display === 'none' ? 'flex' : 'none';
            byId('emojiPickerPanel').style.display = 'none';
            byId('editorTextInput').focus();
        });

        byId('editorFontSize').addEventListener('input', function () {
            byId('editorFontSizeLabel').textContent = this.value + 'px';
        });

        function placeText() {
            const input = byId('editorTextInput');
            const text = input.value.trim();
            if (!text) return;
            const fontSize = parseInt(byId('editorFontSize').value, 10) || 48;
            const ctx = canvas.getContext('2d');
            ctx.font = `${fontSize}px sans-serif`;
            editor.overlays.push({
                type: 'text',
                content: text,
                x: Math.max(0, (canvas.width - ctx.measureText(text).width) / 2),
                y: canvas.height / 2,
                fontSize,
                color: byId('editorTextColor').value || '#ffffff',
                bold: false
            });
            input.value = '';
            byId('textInputPanel').style.display = 'none';
            drawEditor();
        }
        byId('editorPlaceText').addEventListener('click', placeText);
        byId('editorTextInput').addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); placeText(); }
        });

        byId('editorAddEmoji').addEventListener('click', function () {
            const panel = byId('emojiPickerPanel');
            panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
            byId('textInputPanel').style.display = 'none';

            const picker = byId('emojiPicker');
            if (picker.children.length > 0) return;
            EMOJIS.forEach(emoji => {
                const button = document.createElement('button');
                button.type = 'button';
                button.textContent = emoji;
                button.setAttribute('aria-label', `Add ${emoji}`);
                button.addEventListener('click', function () {
                    editor.overlays.push({
                        type: 'emoji', content: emoji,
                        x: canvas.width / 2 - 40, y: canvas.height / 2,
                        fontSize: 80, color: '#000000', bold: false
                    });
                    panel.style.display = 'none';
                    drawEditor();
                });
                picker.appendChild(button);
            });
        });

        byId('editorUndo').addEventListener('click', function () {
            if (editor.overlays.length === 0) return;
            editor.overlays.pop();
            editor.selected = -1;
            drawEditor();
        });

        byId('editorDone').addEventListener('click', function () {
            if (!state) return;
            const id = editor.slide.id;
            if (editor.overlays.length === 0) {
                // Everything was undone: this slide is the server's again.
                delete state.edited[id];
                delete state.overlays[id];
            } else {
                drawEditor(false);      // no selection box in the saved image
                state.edited[id] = canvas.toDataURL('image/png');
                state.overlays[id] = editor.overlays;
            }
            showEditor(false);
            show(state.index);
        });

        byId('editorCancel').addEventListener('click', function () {
            if (state) showEditor(false);
        });
    }

    // =====================================================================
    // Open
    // =====================================================================

    async function open(options) {
        if (!init()) {
            console.error('wrappedStory: partials/wrapped_story.html is not on this page');
            return false;
        }
        const response = await fetch(options.manifestUrl);
        const manifest = await response.json();
        if (!response.ok) throw new Error(manifest.error || 'Could not load your Wrapped');
        if (!manifest.slides || manifest.slides.length === 0) return false;

        state = {
            options,
            slides: manifest.slides,
            index: 0,
            elapsed: 0,
            lastTick: null,
            ready: false,
            tall: false,
            loadToken: 0,
            paused: false,
            pausedByUser: false,
            editing: false,
            edited: {},         // slide id -> data URL
            overlays: {},       // slide id -> overlay list
            frame: null
        };

        dom.title.textContent = options.title || '';
        dom.download.href = options.downloadUrl;
        dom.segments.replaceChildren(...manifest.slides.map(() => {
            const segment = document.createElement('div');
            segment.className = 'wrapped-story-segment';
            segment.appendChild(document.createElement('span'));
            return segment;
        }));
        showEditor(false);
        setPaused(reducedMotion(), reducedMotion());

        modal.show();
        show(0);
        state.frame = requestAnimationFrame(tick);
        return true;
    }

    CLU.wrappedStory = { open, SLIDE_MS };
})();
