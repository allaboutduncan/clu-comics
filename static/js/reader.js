// ============================================================================
// COMIC READER FUNCTIONALITY (Shared Module)
// ============================================================================
//
// Extracted from collection.js so it can be reused by source_wall and other pages.
//
// External contracts:
//   - window._readerAllItems: Array of {name, path, type, thumbnailUrl?} for next-issue detection
//   - window._readerReadIssuesSet: Set of paths already read (optional, for mark-read tracking)
//   - showError(msg) / showSuccess(msg): toast helpers (optional; errors silently ignored if missing)
//   - Swiper must be loaded before this script.
// ============================================================================

let comicReaderSwiper = null;
let currentComicPath = null;
let currentComicPageCount = 0;
let highestPageViewed = 0;
let currentComicSiblings = [];  // All comic files in current folder
let currentComicIndex = -1;     // Index of current comic in siblings
let nextIssueOverlayShown = false;  // Track if overlay is currently shown
let savedReadingPosition = null;  // Track saved reading position for current comic
let readingStartTime = null;      // Start time of current reading session
let accumulatedTime = 0;          // Total time spent reading prior to this session
let pageEdgeColors = new Map();   // Cache of extracted edge colors per page index

// Event listener references for cleanup
let zoomKeyboardHandler = null;
let mousewheelHandler = null;
let wheelTimeout = null;

// Immersive reader chrome state
let readerChromeHidden = false;
let chromeToggleTimeout = null;

// Comic file extensions
const COMIC_EXTENSIONS = ['.cbz', '.cbr', '.cb7', '.zip', '.rar', '.7z', '.pdf'];

// Zoom step levels: 3 increments from minRatio (1) to maxRatio (3)
const ZOOM_STEPS = [1, 1.67, 2.33, 3];

/**
 * Encode a file path for URL while preserving slashes
 * @param {string} path - The file path to encode
 * @returns {string} Encoded path (without leading slash for use in URLs)
 */
function encodeFilePath(path) {
    // Remove leading slash if present (will be part of the URL path)
    const cleanPath = path.startsWith('/') ? path.substring(1) : path;
    // Split by slash, encode each component, then rejoin
    return cleanPath.split('/').map(component => encodeURIComponent(component)).join('/');
}

/**
 * Handle keydown events specific to comic reader (spacebar only)
 * Arrow keys are handled by handleZoomKeyboard
 * @param {KeyboardEvent} e - The keydown event
 */
function handleComicReaderKeydown(e) {
    if (!comicReaderSwiper) return;

    // Spacebar to advance
    if (e.code === 'Space') {
        e.preventDefault(); // Prevent page scroll
        comicReaderSwiper.slideNext();
    }
}

/**
 * Check if the current viewport matches mobile/tablet size
 * @returns {boolean} True if viewport is 1024px or smaller
 */
function isMobileOrTablet() {
    return window.matchMedia('(max-width: 1024px)').matches;
}

/**
 * Toggle the reader chrome (header/footer) visibility on mobile
 */
function toggleReaderChrome() {
    const container = document.querySelector('.comic-reader-container');
    if (!container) return;
    readerChromeHidden = !readerChromeHidden;
    container.classList.toggle('reader-chrome-hidden', readerChromeHidden);
}

/** Helper: get the items array for sibling detection */
function _getReaderItems() {
    return window._readerAllItems || [];
}

/** Helper: get the readIssuesSet for mark-read tracking */
function _getReadIssuesSet() {
    return window._readerReadIssuesSet || new Set();
}

/** Helper: add path to readIssuesSet if available */
function _markPathAsRead(path) {
    const s = window._readerReadIssuesSet;
    if (s && typeof s.add === 'function') s.add(path);
}

/**
 * Open comic reader for a specific file
 * @param {string} filePath - Path to the comic file
 */
function openComicReader(filePath) {
    currentComicPath = filePath;
    highestPageViewed = 0;
    nextIssueOverlayShown = false;
    savedReadingPosition = null;
    readingStartTime = Date.now();
    accumulatedTime = 0;
    pageEdgeColors = new Map();

    // Track sibling comics for "next issue" feature
    currentComicSiblings = _getReaderItems().filter(item => {
        if (item.type !== 'file') return false;
        const ext = item.name.toLowerCase().substring(item.name.lastIndexOf('.'));
        return COMIC_EXTENSIONS.includes(ext);
    });
    currentComicIndex = currentComicSiblings.findIndex(item => item.path === filePath);

    const modal = document.getElementById('comicReaderModal');
    const titleEl = document.getElementById('comicReaderTitle');
    const pageInfoEl = document.getElementById('comicReaderPageInfo');

    // Hide overlays if visible from previous session
    hideNextIssueOverlay();
    hideResumeReadingOverlay();

    // Reset bookmark button state
    updateBookmarkButtonState(false);

    // Show modal
    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden'; // Prevent scrolling

    // Immersive mode: hide chrome by default on mobile/tablet
    if (isMobileOrTablet()) {
        const container = document.querySelector('.comic-reader-container');
        if (container) {
            container.classList.add('reader-chrome-hidden');
            readerChromeHidden = true;
        }
    }

    // Set title
    const fileName = filePath.split(/[/\\]/).pop();
    titleEl.textContent = fileName;

    // Show loading
    pageInfoEl.textContent = 'Loading...';

    // Encode the path properly for URL
    const encodedPath = encodeFilePath(filePath);

    // Fetch comic info and saved position in parallel
    Promise.all([
        fetch(`/api/read/${encodedPath}/info`).then(r => r.json()),
        fetch(`/api/reading-position?path=${encodeURIComponent(filePath)}`).then(r => r.json())
    ])
        .then(([comicData, positionData]) => {
            if (comicData.success) {
                currentComicPageCount = comicData.page_count;

                // Get accumulated time if available
                if (positionData && positionData.time_spent) {
                    accumulatedTime = positionData.time_spent;
                }

                // Check if there's a saved position
                if (positionData.page_number !== null && positionData.page_number > 0) {
                    savedReadingPosition = positionData.page_number;
                    // Show resume prompt
                    showResumeReadingOverlay(positionData.page_number, comicData.page_count);
                    // Initialize reader but don't navigate yet
                    initializeComicReader(comicData.page_count, 0);
                    updateBookmarkButtonState(true);
                } else {
                    initializeComicReader(comicData.page_count, 0);
                }
            } else {
                if (typeof showError === 'function') showError('Failed to load comic: ' + (comicData.error || 'Unknown error'));
                closeComicReader();
            }
        })
        .catch(error => {
            console.error('Error loading comic:', error);
            if (typeof showError === 'function') showError('An error occurred while loading the comic.');
            closeComicReader();
        });

    // Add keyboard listener
    document.addEventListener('keydown', handleComicReaderKeydown);
}

/**
 * Initialize the Swiper comic reader
 * @param {number} pageCount - Total number of pages
 * @param {number} startPage - Page to start on (0-indexed, default 0)
 */
function initializeComicReader(pageCount, startPage = 0) {
    const wrapper = document.getElementById('comicReaderWrapper');
    const pageInfoEl = document.getElementById('comicReaderPageInfo');

    // Clear existing slides
    wrapper.innerHTML = '';

    // Create slides for each page
    for (let i = 0; i < pageCount; i++) {
        const slide = document.createElement('div');
        slide.className = 'swiper-slide';
        slide.dataset.pageNum = i;

        // Add loading spinner initially
        slide.innerHTML = `
            <div class="comic-page-loading">
                <div class="spinner-border" role="status">
                    <span class="visually-hidden">Loading...</span>
                </div>
            </div>
        `;

        wrapper.appendChild(slide);
    }

    // Destroy existing swiper if it exists
    if (comicReaderSwiper) {
        comicReaderSwiper.destroy(true, true);
    }

    // Initialize Swiper with zoom support
    comicReaderSwiper = new Swiper('#comicReaderSwiper', {
        direction: 'horizontal',
        loop: false,
        initialSlide: startPage,
        keyboard: {
            enabled: false, // Disable default keyboard to handle zoom with arrow keys
            onlyInViewport: false,
        },
        mousewheel: {
            enabled: false, // Disabled - using custom handler for zoom-aware behavior
        },
        navigation: {
            nextEl: '.swiper-button-next',
            prevEl: '.swiper-button-prev',
        },
        lazy: {
            loadPrevNext: true,
            loadPrevNextAmount: 2,
        },
        // Enable zoom for pinch-to-zoom on mobile
        zoom: {
            maxRatio: 3,
            minRatio: 1,
            toggle: true, // Enable double-tap to toggle zoom
        },
        // Improve touch handling for mobile
        touchEventsTarget: 'container',
        passiveListeners: true,
        on: {
            slideChange: function () {
                const currentIndex = this.activeIndex;
                pageInfoEl.textContent = `Page ${currentIndex + 1} of ${pageCount}`;

                // Update page selector dropdown
                const pageSelector = document.getElementById('pageSelector');
                if (pageSelector) {
                    pageSelector.value = currentIndex;
                }

                // Track highest page viewed for read progress
                if (currentIndex > highestPageViewed) {
                    highestPageViewed = currentIndex;
                }
                updateReadingProgress();

                // Reset zoom when changing slides
                if (this.zoom) {
                    this.zoom.out();
                }

                // Check if reached last page - show next issue overlay if available
                if (currentIndex === pageCount - 1) {
                    checkAndShowNextIssueOverlay();
                } else {
                    // Hide overlay if navigating away from last page
                    hideNextIssueOverlay();
                }

                // Load current page
                loadComicPage(currentIndex);

                // Preload next 2 pages
                if (currentIndex + 1 < pageCount) {
                    loadComicPage(currentIndex + 1);
                }
                if (currentIndex + 2 < pageCount) {
                    loadComicPage(currentIndex + 2);
                }

                // Preload previous page for backward navigation
                if (currentIndex - 1 >= 0) {
                    loadComicPage(currentIndex - 1);
                }

                // Clean up pages that are far away to save memory
                unloadDistantPages(currentIndex, pageCount);

                // Apply cached edge color for this page
                const cachedColor = pageEdgeColors.get(currentIndex);
                if (cachedColor) {
                    applyReaderBackgroundColor(cachedColor.r, cachedColor.g, cachedColor.b);
                }
            },
            // Single tap: toggle chrome on mobile (with delay to avoid conflict with double-tap)
            tap: function (swiper, event) {
                if (!isMobileOrTablet()) return;
                // Don't toggle chrome when zoomed in (user is panning)
                if (this.zoom && this.zoom.scale > 1) return;
                // Don't toggle chrome when tapping navigation buttons
                if (event && event.target && event.target.closest('.swiper-button-next, .swiper-button-prev')) return;
                // Start a 300ms timer; if a double-tap comes, it will cancel this
                chromeToggleTimeout = setTimeout(function () {
                    chromeToggleTimeout = null;
                    toggleReaderChrome();
                }, 300);
            },
            // Double-tap to reset zoom (cancel any pending chrome toggle)
            doubleTap: function () {
                if (chromeToggleTimeout) {
                    clearTimeout(chromeToggleTimeout);
                    chromeToggleTimeout = null;
                }
                if (this.zoom.scale > 1) {
                    this.zoom.out();
                } else {
                    this.zoom.in();
                }
            },
            init: function () {
                const initialPage = this.activeIndex;
                pageInfoEl.textContent = `Page ${initialPage + 1} of ${pageCount}`;
                highestPageViewed = initialPage;
                updateReadingProgress();

                // Load initial page and adjacent pages
                loadComicPage(initialPage);
                if (initialPage + 1 < pageCount) loadComicPage(initialPage + 1);
                if (initialPage + 2 < pageCount) loadComicPage(initialPage + 2);
                if (initialPage - 1 >= 0) loadComicPage(initialPage - 1);
            }
        }
    });

    // Initialize page selector dropdown
    initializePageSelector(pageCount, startPage);

    // Initialize zoom controls
    initializeZoomControls();

    // Initialize custom mousewheel handler for zoom-aware navigation
    initializeMousewheelHandler();
}

/**
 * Initialize page selector dropdown
 * @param {number} pageCount - Total number of pages
 * @param {number} startPage - Initial page (0-indexed)
 */
function initializePageSelector(pageCount, startPage) {
    const pageSelector = document.getElementById('pageSelector');
    if (!pageSelector) return;

    // Clear existing options
    pageSelector.innerHTML = '';

    // Populate dropdown with page options
    for (let i = 0; i < pageCount; i++) {
        const option = document.createElement('option');
        option.value = i;
        option.textContent = `Page ${i + 1} of ${pageCount}`;
        if (i === startPage) {
            option.selected = true;
        }
        pageSelector.appendChild(option);
    }

    // Add change event listener
    pageSelector.addEventListener('change', function() {
        const selectedPage = parseInt(this.value, 10);
        if (comicReaderSwiper && !isNaN(selectedPage)) {
            comicReaderSwiper.slideTo(selectedPage);
        }
    });
}

/**
 * Step the zoom level up or down by one increment
 * @param {'in'|'out'} direction - Zoom direction
 */
function stepZoom(direction) {
    if (!comicReaderSwiper || !comicReaderSwiper.zoom) return;
    const current = comicReaderSwiper.zoom.scale;

    if (direction === 'in') {
        // Find the next step above the current scale
        for (let i = 0; i < ZOOM_STEPS.length; i++) {
            if (ZOOM_STEPS[i] > current + 0.01) {
                comicReaderSwiper.zoom.in(ZOOM_STEPS[i]);
                return;
            }
        }
    } else {
        // Find the next step below the current scale
        for (let i = ZOOM_STEPS.length - 1; i >= 0; i--) {
            if (ZOOM_STEPS[i] < current - 0.01) {
                if (ZOOM_STEPS[i] <= 1) {
                    comicReaderSwiper.zoom.out();
                } else {
                    comicReaderSwiper.zoom.in(ZOOM_STEPS[i]);
                }
                return;
            }
        }
        comicReaderSwiper.zoom.out();
    }
}

/**
 * Initialize zoom controls (buttons and keyboard)
 */
function initializeZoomControls() {
    const zoomInBtn = document.getElementById('zoomInBtn');
    const zoomOutBtn = document.getElementById('zoomOutBtn');

    // Zoom in button - step up one increment
    if (zoomInBtn) {
        zoomInBtn.addEventListener('click', function() {
            stepZoom('in');
        });
    }

    // Zoom out button - step down one increment
    if (zoomOutBtn) {
        zoomOutBtn.addEventListener('click', function() {
            stepZoom('out');
        });
    }

    // Remove existing keyboard listener if present
    if (zoomKeyboardHandler) {
        document.removeEventListener('keydown', zoomKeyboardHandler);
    }

    // Add keyboard event listener for arrow up/down to zoom
    zoomKeyboardHandler = handleZoomKeyboard;
    document.addEventListener('keydown', zoomKeyboardHandler);
}

/**
 * Handle keyboard events for zoom (arrow keys)
 * @param {KeyboardEvent} event
 */
function handleZoomKeyboard(event) {
    // Only handle if comic reader is open
    if (!comicReaderSwiper) return;

    // Check if user is zoomed in
    const isZoomed = comicReaderSwiper.zoom && comicReaderSwiper.zoom.scale > 1;

    switch(event.key) {
        case 'ArrowUp':
            // Zoom in with arrow up (stepped)
            event.preventDefault();
            stepZoom('in');
            break;
        case 'ArrowDown':
            // Zoom out with arrow down (stepped)
            event.preventDefault();
            stepZoom('out');
            break;
        case 'ArrowLeft':
            // Navigate to previous page if not zoomed
            if (!isZoomed) {
                event.preventDefault();
                comicReaderSwiper.slidePrev();
            }
            break;
        case 'ArrowRight':
            // Navigate to next page if not zoomed
            if (!isZoomed) {
                event.preventDefault();
                comicReaderSwiper.slideNext();
            }
            break;
    }
}

/**
 * Initialize custom mousewheel handler for zoom-aware navigation
 */
function initializeMousewheelHandler() {
    const swiperEl = document.getElementById('comicReaderSwiper');
    if (!swiperEl) return;

    // Clear any existing timeout
    if (wheelTimeout) {
        clearTimeout(wheelTimeout);
        wheelTimeout = null;
    }

    // Remove existing mousewheel listener if present
    if (mousewheelHandler) {
        swiperEl.removeEventListener('wheel', mousewheelHandler);
    }

    // Create the handler function
    mousewheelHandler = function(event) {
        if (!comicReaderSwiper) return;

        // Check if currently zoomed
        const isZoomed = comicReaderSwiper.zoom && comicReaderSwiper.zoom.scale > 1;

        if (isZoomed) {
            // When zoomed, let Swiper handle panning (don't prevent default)
            return;
        }

        // When not zoomed, use mousewheel to navigate pages
        event.preventDefault();

        // Debounce to prevent too fast navigation
        clearTimeout(wheelTimeout);
        wheelTimeout = setTimeout(() => {
            if (event.deltaY > 0) {
                // Scroll down = next page
                comicReaderSwiper.slideNext();
            } else if (event.deltaY < 0) {
                // Scroll up = previous page
                comicReaderSwiper.slidePrev();
            }
        }, 50);
    };

    // Add the event listener
    swiperEl.addEventListener('wheel', mousewheelHandler, { passive: false });
}

/**
 * Update reading progress bar display
 */
function updateReadingProgress() {
    if (currentComicPageCount === 0) return;
    const progress = ((highestPageViewed + 1) / currentComicPageCount) * 100;
    const progressBar = document.querySelector('.comic-reader-progress-fill');
    const progressText = document.querySelector('.comic-reader-progress-text');
    if (progressBar) progressBar.style.width = `${progress}%`;
    if (progressText) progressText.textContent = `${Math.round(progress)}%`;
}

/**
 * Extract the average edge color from an image by sampling pixels along all 4 edges
 * @param {HTMLImageElement} img - The loaded image element
 * @returns {{r: number, g: number, b: number}} Average RGB color of edge pixels
 */
function extractEdgeColor(img) {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');

    // Scale down to max 100px on longest side for performance
    const scale = Math.min(100 / img.naturalWidth, 100 / img.naturalHeight, 1);
    const w = Math.max(1, Math.round(img.naturalWidth * scale));
    const h = Math.max(1, Math.round(img.naturalHeight * scale));
    canvas.width = w;
    canvas.height = h;
    ctx.drawImage(img, 0, 0, w, h);

    const imageData = ctx.getImageData(0, 0, w, h);
    const data = imageData.data;
    let rSum = 0, gSum = 0, bSum = 0, count = 0;

    function addPixel(x, y) {
        const idx = (y * w + x) * 4;
        rSum += data[idx];
        gSum += data[idx + 1];
        bSum += data[idx + 2];
        count++;
    }

    // Sample all 4 edges
    for (let x = 0; x < w; x++) {
        addPixel(x, 0);         // top edge
        addPixel(x, h - 1);     // bottom edge
    }
    for (let y = 1; y < h - 1; y++) {
        addPixel(0, y);         // left edge
        addPixel(w - 1, y);     // right edge
    }

    if (count === 0) return { r: 0, g: 0, b: 0 };
    return {
        r: Math.round(rSum / count),
        g: Math.round(gSum / count),
        b: Math.round(bSum / count)
    };
}

/**
 * Apply a darkened version of the given color to the reader chrome elements
 * @param {number} r - Red component (0-255)
 * @param {number} g - Green component (0-255)
 * @param {number} b - Blue component (0-255)
 */
function applyReaderBackgroundColor(r, g, b) {
    const overlay = document.querySelector('.comic-reader-overlay');
    const header = document.querySelector('.comic-reader-header');
    const footer = document.querySelector('.comic-reader-footer');
    const slides = document.querySelectorAll('.comic-reader-swiper .swiper-slide');

    if (overlay) overlay.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
    if (header) header.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
    if (footer) footer.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
    slides.forEach(slide => {
        slide.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
    });
}

/**
 * Reset reader chrome background colors to CSS defaults
 */
function resetReaderBackgroundColor() {
    const overlay = document.querySelector('.comic-reader-overlay');
    const header = document.querySelector('.comic-reader-header');
    const footer = document.querySelector('.comic-reader-footer');
    const slides = document.querySelectorAll('.comic-reader-swiper .swiper-slide');

    if (overlay) overlay.style.backgroundColor = '';
    if (header) header.style.backgroundColor = '';
    if (footer) footer.style.backgroundColor = '';
    slides.forEach(slide => {
        slide.style.backgroundColor = '';
    });
}

/**
 * Load a specific comic page
 * @param {number} pageNum - Page number to load
 */
function loadComicPage(pageNum) {
    const slide = document.querySelector(`.swiper-slide[data-page-num="${pageNum}"]`);
    if (!slide) return;

    // Check if already loaded or loading
    if (slide.querySelector('img') || slide.dataset.loading === 'true') return;

    // Mark as loading to prevent duplicate requests
    slide.dataset.loading = 'true';

    // Encode the path properly for URL
    const encodedPath = encodeFilePath(currentComicPath);
    const imageUrl = `/api/read/${encodedPath}/page/${pageNum}`;

    // Create image element
    const img = document.createElement('img');
    img.src = imageUrl;
    img.alt = `Page ${pageNum + 1}`;

    // Add decoding hint for faster rendering
    img.decoding = 'async';

    // Add fetchpriority for current/next pages
    const currentIndex = comicReaderSwiper ? comicReaderSwiper.activeIndex : 0;
    if (Math.abs(pageNum - currentIndex) <= 1) {
        img.fetchPriority = 'high';
    } else {
        img.fetchPriority = 'low';
    }

    img.onload = function () {
        // Remove loading spinner and wrap image in zoom container for pinch-to-zoom
        slide.innerHTML = '';

        // Create zoom container (required for Swiper zoom module)
        const zoomContainer = document.createElement('div');
        zoomContainer.className = 'swiper-zoom-container';
        zoomContainer.appendChild(img);

        slide.appendChild(zoomContainer);
        slide.dataset.loading = 'false';

        // Extract and cache edge color for dynamic background
        try {
            const color = extractEdgeColor(img);
            pageEdgeColors.set(pageNum, color);
            // If this is the currently active slide, apply color immediately
            if (comicReaderSwiper && comicReaderSwiper.activeIndex === pageNum) {
                applyReaderBackgroundColor(color.r, color.g, color.b);
            }
        } catch (e) {
            // Silently ignore color extraction failures (e.g., CORS)
        }
    };

    img.onerror = function () {
        slide.innerHTML = `
            <div class="comic-page-loading">
                <p>Failed to load page ${pageNum + 1}</p>
            </div>
        `;
        slide.dataset.loading = 'false';
    };
}

/**
 * Unload pages that are far from the current page to save memory
 * @param {number} currentIndex - Current page index
 * @param {number} pageCount - Total number of pages
 */
function unloadDistantPages(currentIndex, pageCount) {
    const keepDistance = 5; // Keep pages within 5 pages of current

    for (let i = 0; i < pageCount; i++) {
        // Skip pages close to current position
        if (Math.abs(i - currentIndex) <= keepDistance) continue;

        const slide = document.querySelector(`.swiper-slide[data-page-num="${i}"]`);
        if (!slide) continue;

        const img = slide.querySelector('img');
        if (img) {
            // Replace with loading spinner to free memory
            slide.innerHTML = `
                <div class="comic-page-loading">
                    <div class="spinner-border" role="status">
                        <span class="visually-hidden">Loading...</span>
                    </div>
                </div>
            `;
            slide.dataset.loading = 'false';
        }
    }
}

/**
 * Close the comic reader
 */
function closeComicReader() {
    // Smart auto-save/cleanup logic for reading position
    if (currentComicPath && currentComicPageCount > 0) {
        const currentPage = comicReaderSwiper ? comicReaderSwiper.activeIndex + 1 : 1;
        const progress = ((highestPageViewed + 1) / currentComicPageCount) * 100;
        const withinLastPages = currentPage > currentComicPageCount - 3;

        if (progress >= 90 || withinLastPages) {
            // Calculate final time spent
            let sessionTime = (Date.now() - readingStartTime) / 1000;
            if (sessionTime < 10) sessionTime = 0;
            const totalTime = Math.round(accumulatedTime + sessionTime);

            // User finished or nearly finished - mark as read and delete bookmark
            fetch('/api/mark-comic-read', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    path: currentComicPath,
                    page_count: currentComicPageCount,
                    time_spent: totalTime
                })
            }).then(() => {
                _markPathAsRead(currentComicPath);
            }).catch(err => console.error('Failed to mark comic as read:', err));

            // Delete saved reading position (fire and forget)
            fetch(`/api/reading-position?path=${encodeURIComponent(currentComicPath)}`, {
                method: 'DELETE'
            }).catch(err => console.error('Failed to delete reading position:', err));
        } else if (currentPage > 1) {
            // User stopped mid-read - auto-save position silently
            let sessionTime = (Date.now() - readingStartTime) / 1000;
            if (sessionTime < 10) sessionTime = 0;
            const totalTime = Math.round(accumulatedTime + sessionTime);

            fetch('/api/reading-position', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    comic_path: currentComicPath,
                    page_number: currentPage,
                    total_pages: currentComicPageCount,
                    time_spent: totalTime
                })
            }).catch(err => console.error('Failed to auto-save reading position:', err));
        }
    }

    // Reset dynamic background colors before hiding
    resetReaderBackgroundColor();
    pageEdgeColors = new Map();

    const modal = document.getElementById('comicReaderModal');
    modal.style.display = 'none';
    document.body.style.overflow = ''; // Restore scrolling

    // Reset immersive reader chrome state
    const container = document.querySelector('.comic-reader-container');
    if (container) {
        container.classList.remove('reader-chrome-hidden');
    }
    readerChromeHidden = false;
    if (chromeToggleTimeout) {
        clearTimeout(chromeToggleTimeout);
        chromeToggleTimeout = null;
    }

    // Destroy swiper
    if (comicReaderSwiper) {
        comicReaderSwiper.destroy(true, true);
        comicReaderSwiper = null;
    }

    // Clear state
    currentComicPath = null;
    currentComicPageCount = 0;
    highestPageViewed = 0;
    currentComicSiblings = [];
    currentComicIndex = -1;
    nextIssueOverlayShown = false;
    savedReadingPosition = null;

    // Hide overlays
    hideNextIssueOverlay();
    hideResumeReadingOverlay();

    // Remove keyboard listeners
    document.removeEventListener('keydown', handleComicReaderKeydown);
    if (zoomKeyboardHandler) {
        document.removeEventListener('keydown', zoomKeyboardHandler);
        zoomKeyboardHandler = null;
    }

    // Remove mousewheel listener
    if (mousewheelHandler) {
        const swiperEl = document.getElementById('comicReaderSwiper');
        if (swiperEl) {
            swiperEl.removeEventListener('wheel', mousewheelHandler);
        }
        mousewheelHandler = null;
    }

    // Clear any pending wheel timeout
    if (wheelTimeout) {
        clearTimeout(wheelTimeout);
        wheelTimeout = null;
    }
}

/**
 * Check if there's a next issue and show the overlay
 */
function checkAndShowNextIssueOverlay() {
    // Check if there's a next comic in the folder
    if (currentComicIndex >= 0 && currentComicIndex + 1 < currentComicSiblings.length) {
        const nextComic = currentComicSiblings[currentComicIndex + 1];
        showNextIssueOverlay(nextComic);
    }
    // If no next issue, do nothing (close normally per user preference)
}

/**
 * Show the next issue overlay with comic info
 * @param {Object} nextComic - The next comic file object {name, path}
 */
function showNextIssueOverlay(nextComic) {
    if (nextIssueOverlayShown) return;  // Already shown

    const overlay = document.getElementById('nextIssueOverlay');
    const thumbnail = document.getElementById('nextIssueThumbnail');
    const nameEl = document.getElementById('nextIssueName');

    if (!overlay) return;

    // Set the next comic name
    nameEl.textContent = nextComic.name;
    nameEl.title = nextComic.name;

    // Set thumbnail URL - use existing thumbnailUrl from allItems if available
    if (nextComic.thumbnailUrl) {
        thumbnail.src = nextComic.thumbnailUrl;
    } else {
        // Fallback to placeholder if no thumbnail available
        thumbnail.src = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 150"%3E%3Crect fill="%23333" width="100" height="150"/%3E%3Ctext x="50" y="75" text-anchor="middle" fill="%23666" font-size="12"%3ENo Preview%3C/text%3E%3C/svg%3E';
    }
    thumbnail.onerror = function () {
        // Fallback to placeholder on error
        this.src = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 150"%3E%3Crect fill="%23333" width="100" height="150"/%3E%3Ctext x="50" y="75" text-anchor="middle" fill="%23666" font-size="12"%3ENo Preview%3C/text%3E%3C/svg%3E';
    };

    // Show overlay
    overlay.style.display = 'flex';
    nextIssueOverlayShown = true;
}

/**
 * Hide the next issue overlay
 */
function hideNextIssueOverlay() {
    const overlay = document.getElementById('nextIssueOverlay');
    if (overlay) {
        overlay.style.display = 'none';
    }
    nextIssueOverlayShown = false;
}

/**
 * Show the resume reading overlay
 * @param {number} pageNumber - The saved page number
 * @param {number} totalPages - Total pages in the comic
 */
function showResumeReadingOverlay(pageNumber, totalPages) {
    const overlay = document.getElementById('resumeReadingOverlay');
    const info = document.getElementById('resumeReadingInfo');

    if (!overlay || !info) return;

    info.textContent = `Continue from page ${pageNumber} of ${totalPages}?`;
    overlay.style.display = 'flex';
}

/**
 * Hide the resume reading overlay
 */
function hideResumeReadingOverlay() {
    const overlay = document.getElementById('resumeReadingOverlay');
    if (overlay) {
        overlay.style.display = 'none';
    }
}

/**
 * Update the bookmark button state
 * @param {boolean} hasSavedPosition - Whether there's a saved position
 */
function updateBookmarkButtonState(hasSavedPosition) {
    const bookmarkBtn = document.getElementById('comicReaderBookmark');
    if (!bookmarkBtn) return;

    const icon = bookmarkBtn.querySelector('i');
    if (icon) {
        if (hasSavedPosition) {
            icon.classList.remove('bi-bookmark');
            icon.classList.add('bi-bookmark-fill');
            bookmarkBtn.title = 'Position Saved';
        } else {
            icon.classList.remove('bi-bookmark-fill');
            icon.classList.add('bi-bookmark');
            bookmarkBtn.title = 'Save Position';
        }
    }
}

/**
 * Save current reading position
 */
function saveReadingPosition() {
    if (!currentComicPath || !comicReaderSwiper) return;

    const currentPage = comicReaderSwiper.activeIndex + 1; // 1-indexed for display

    // Calculate time spent
    let sessionTime = (Date.now() - readingStartTime) / 1000;
    if (sessionTime < 10) sessionTime = 0; // Ignore sessions shorter than 10 seconds (previewing)
    const totalTime = Math.round(accumulatedTime + sessionTime);

    fetch('/api/reading-position', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            comic_path: currentComicPath,
            page_number: currentPage,
            total_pages: currentComicPageCount,
            time_spent: totalTime
        })
    }).then(response => response.json())
        .then(data => {
            if (data.success) {
                savedReadingPosition = currentPage;
                updateBookmarkButtonState(true);
                // Brief visual feedback
                const bookmarkBtn = document.getElementById('comicReaderBookmark');
                if (bookmarkBtn) {
                    bookmarkBtn.classList.add('btn-success');
                    bookmarkBtn.classList.remove('btn-outline-light');
                    setTimeout(() => {
                        bookmarkBtn.classList.remove('btn-success');
                        bookmarkBtn.classList.add('btn-outline-light');
                    }, 1000);
                }
            }
        }).catch(err => console.error('Failed to save reading position:', err));
}

/**
 * Continue to the next issue
 */
function continueToNextIssue() {
    if (currentComicIndex < 0 || currentComicIndex + 1 >= currentComicSiblings.length) {
        return;
    }

    const nextComic = currentComicSiblings[currentComicIndex + 1];

    // Mark current comic as read and delete bookmark (since we finished it)
    if (currentComicPath) {
        // Calculate final time spent
        let sessionTime = (Date.now() - readingStartTime) / 1000;
        if (sessionTime < 10) sessionTime = 0;
        const totalTime = Math.round(accumulatedTime + sessionTime);

        fetch('/api/mark-comic-read', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                path: currentComicPath,
                page_count: currentComicPageCount,
                time_spent: totalTime
            })
        }).then(() => {
            _markPathAsRead(currentComicPath);
        }).catch(err => console.error('Failed to mark comic as read:', err));

        // Delete saved reading position
        fetch(`/api/reading-position?path=${encodeURIComponent(currentComicPath)}`, {
            method: 'DELETE'
        }).catch(err => console.error('Failed to delete reading position:', err));
    }

    // Close current comic without triggering the normal close logic
    const modal = document.getElementById('comicReaderModal');
    modal.style.display = 'none';

    if (comicReaderSwiper) {
        comicReaderSwiper.destroy(true, true);
        comicReaderSwiper = null;
    }

    // Reset state
    currentComicPath = null;
    currentComicPageCount = 0;
    highestPageViewed = 0;
    hideNextIssueOverlay();

    // Open the next comic (keeping the siblings list intact)
    openComicReader(nextComic.path);
}

// Setup reader event handlers
document.addEventListener('DOMContentLoaded', () => {
    const closeBtn = document.getElementById('comicReaderClose');
    if (closeBtn) {
        closeBtn.addEventListener('click', closeComicReader);
    }

    // Close on overlay click
    const overlay = document.querySelector('.comic-reader-overlay');
    if (overlay) {
        overlay.addEventListener('click', closeComicReader);
    }

    // Close on Escape key
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && currentComicPath) {
            closeComicReader();
        }
    });

    // Next issue overlay handlers
    const nextIssueContinue = document.getElementById('nextIssueContinue');
    if (nextIssueContinue) {
        nextIssueContinue.addEventListener('click', continueToNextIssue);
    }

    const nextIssueClose = document.getElementById('nextIssueClose');
    if (nextIssueClose) {
        nextIssueClose.addEventListener('click', () => {
            // Mark as read and delete bookmark since user finished the comic
            if (currentComicPath) {
                // Calculate final time spent
                let sessionTime = (Date.now() - readingStartTime) / 1000;
                if (sessionTime < 10) sessionTime = 0;
                const totalTime = Math.round(accumulatedTime + sessionTime);

                fetch('/api/mark-comic-read', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        path: currentComicPath,
                        page_count: currentComicPageCount,
                        time_spent: totalTime
                    })
                }).then(() => {
                    _markPathAsRead(currentComicPath);
                }).catch(err => console.error('Failed to mark comic as read:', err));

                fetch(`/api/reading-position?path=${encodeURIComponent(currentComicPath)}`, {
                    method: 'DELETE'
                }).catch(err => console.error('Failed to delete reading position:', err));
            }
            closeComicReader();
        });
    }

    // Close overlay when clicking outside the panel (just dismiss, don't mark as read)
    const nextIssueOverlay = document.getElementById('nextIssueOverlay');
    if (nextIssueOverlay) {
        nextIssueOverlay.addEventListener('click', (e) => {
            if (e.target === nextIssueOverlay) {
                hideNextIssueOverlay();
            }
        });
    }

    // Bookmark button handler
    const bookmarkBtn = document.getElementById('comicReaderBookmark');
    if (bookmarkBtn) {
        bookmarkBtn.addEventListener('click', saveReadingPosition);
    }

    // Resume reading overlay handlers
    const resumeReadingYes = document.getElementById('resumeReadingYes');
    if (resumeReadingYes) {
        resumeReadingYes.addEventListener('click', () => {
            hideResumeReadingOverlay();
            // Navigate to saved position
            if (comicReaderSwiper && savedReadingPosition) {
                comicReaderSwiper.slideTo(savedReadingPosition - 1); // Convert 1-indexed to 0-indexed
            }
        });
    }

    const resumeReadingNo = document.getElementById('resumeReadingNo');
    if (resumeReadingNo) {
        resumeReadingNo.addEventListener('click', () => {
            hideResumeReadingOverlay();
            // Start from the beginning
            if (comicReaderSwiper) {
                comicReaderSwiper.slideTo(0);
            }
            savedReadingPosition = null;
            updateBookmarkButtonState(false);
        });
    }

    // Close resume overlay when clicking outside the panel
    const resumeOverlay = document.getElementById('resumeReadingOverlay');
    if (resumeOverlay) {
        resumeOverlay.addEventListener('click', (e) => {
            if (e.target === resumeOverlay) {
                hideResumeReadingOverlay();
            }
        });
    }
});
