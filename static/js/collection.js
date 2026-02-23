/**
 * browse.js
 * Frontend logic for the visual file browser.
 * Handles directory fetching, grid rendering, lazy loading, navigation, and pagination.
 */

// Global variable to store current folder path for XML update
let updateXmlCurrentPath = '';

// Per-field configuration for Update XML modal
const updateXmlFieldConfig = {
  Volume: {
    hint: 'Enter a 4-digit year (e.g., 2024)',
    placeholder: 'Enter year',
    maxlength: 4,
    validate: (v) => /^\d{4}$/.test(v) ? null : 'Volume must be a 4-digit year'
  },
  Publisher: {
    hint: 'Enter the publisher name (e.g., Marvel Comics)',
    placeholder: 'Enter publisher',
    maxlength: null,
    validate: (v) => v ? null : 'Publisher cannot be empty'
  },
  Series: {
    hint: 'Enter the series name (e.g., The Amazing Spider-Man)',
    placeholder: 'Enter series',
    maxlength: null,
    validate: (v) => v ? null : 'Series cannot be empty'
  },
  SeriesGroup: {
    hint: 'Enter the series group (e.g., Spider-Man)',
    placeholder: 'Enter series group',
    maxlength: null,
    validate: (v) => v ? null : 'Series Group cannot be empty'
  }
};

document.addEventListener('DOMContentLoaded', () => {
    // Initialize with path from URL: prefer clean URL path, fallback to query param
    const initialPath = window.INITIAL_PATH ||
        new URLSearchParams(window.location.search).get('path') ||
        '';
    loadDirectory(initialPath);

    // Load dashboard data if at root or library root level
    // Check if path is empty, '/', or a library root (e.g., '/data', '/manga')
    const isLibraryRoot = !initialPath || initialPath === '/' ||
        (initialPath.startsWith('/') && initialPath.split('/').filter(Boolean).length <= 1);
    if (isLibraryRoot) {
        loadFavoritePublishers();
        loadWantToRead();
        loadContinueReadingSwiper();
        loadRecentlyAddedSwiper();
    }

    // Fetch read issues for status icons (cached client-side for performance)
    fetch('/api/issues-read-paths')
        .then(r => r.json())
        .then(data => {
            readIssuesSet = new Set(data.paths || []);
        })
        .catch(err => console.warn('Failed to load read issues:', err));
});

// State
let currentPath = '';
let isLoading = false;
let allItems = []; // Stores all files and folders for the current directory
let readIssuesSet = new Set(); // Cached set of read issue paths for O(1) lookups
let currentPage = 1;
let itemsPerPage = 21; // Default to match the select dropdown

// All Books mode state
let isAllBooksMode = false;
let allBooksData = null;
let folderViewPath = '';
let backgroundLoadingActive = false; // Track if background loading is happening

// Recently Added mode state
let isRecentlyAddedMode = false;

// Continue Reading mode state
let isContinueReadingMode = false;

// Missing XML mode state
let isMissingXmlMode = false;

// Filter state
let currentFilter = 'all';
let gridSearchTerm = '';  // Normalized (trimmed, lowercase) for filtering
let gridSearchRaw = '';   // Original input value for display

// AbortController for in-flight metadata/thumbnail batch requests
let batchAbortController = null;

/**
 * Handle search input changes
 * @param {string} value - The search term
 */
function onGridSearch(value) {
    gridSearchRaw = value;  // Keep original for display
    gridSearchTerm = value.trim().toLowerCase();  // Normalize for filtering
    currentPage = 1; // Reset to first page when searching
    renderPage();
    loadVisiblePageData();
}

/**
 * Get filtered items based on current filter and search term.
 * @returns {Array} Filtered items
 */
function getFilteredItems() {
    let filtered = allItems;

    // Apply search filter first
    if (gridSearchTerm) {
        filtered = filtered.filter(item =>
            item.name.toLowerCase().includes(gridSearchTerm)
        );
    }

    // Then apply letter filter
    if (currentFilter !== 'all') {
        filtered = filtered.filter(item => {
            if (currentFilter === '#') {
                return !/^[A-Za-z]/.test(item.name.charAt(0));
            }
            return item.name.charAt(0).toUpperCase() === currentFilter;
        });
    }

    return filtered;
}

/**
 * Get the paths of folder items currently visible on the active page.
 * @returns {Array<string>} Paths for the current page's folder items
 */
function getCurrentPagePaths() {
    const filteredItems = getFilteredItems();
    const startIndex = (currentPage - 1) * itemsPerPage;
    const endIndex = startIndex + itemsPerPage;
    return filteredItems.slice(startIndex, endIndex)
        .filter(item => item.type === 'folder')
        .map(item => item.path);
}

/**
 * Load and display the contents of a directory.
 * @param {string} path - The directory path to load.
 * @param {boolean} preservePage - If true, keep current page (for refresh). If false, reset to page 1 (default).
 */
async function loadDirectory(path, preservePage = false, forceRefresh = false) {
    if (isLoading) return;

    // Cancel any ongoing background loading
    backgroundLoadingActive = false;
    hideLoadingMoreIndicator();

    // Cancel any in-flight batch requests from previous directory
    if (batchAbortController) {
        batchAbortController.abort();
        batchAbortController = null;
    }

    setLoading(true);
    currentPath = path;

    // Show/hide dashboard swiper sections based on path (only show at library root level)
    // Library section visibility is managed separately by renderGrid()
    const dashboardSections = document.getElementById('dashboard-sections');
    if (dashboardSections) {
        const isRoot = !path || path === '/' ||
            (path.startsWith('/') && path.split('/').filter(Boolean).length <= 1);
        dashboardSections.querySelectorAll('.dashboard-section:not(#library-section)').forEach(el => {
            el.style.display = isRoot ? '' : 'none';
        });
    }

    // Update URL without reloading - use clean URL format
    // Convert /data/Publisher/Series to /collection/Publisher/Series
    let cleanUrl = '/collection';
    if (path && path.startsWith('/data/')) {
        cleanUrl = '/collection' + path.substring(5); // Remove '/data' prefix
    } else if (path) {
        cleanUrl = '/collection/' + path;
    }
    window.history.pushState({ path }, '', cleanUrl);

    try {
        const url = `/api/browse?path=${encodeURIComponent(path)}${forceRefresh ? '&refresh=true' : ''}`;
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();

        renderBreadcrumbs(data.current_path);

        // Handle Header Image
        const headerImageContainer = document.getElementById('collection-header-image');
        if (headerImageContainer) {
            if (data.header_image_url) {
                headerImageContainer.innerHTML = `<img src="${data.header_image_url}" class="img-fluid rounded shadow-sm w-100" alt="Collection Header" style="max-height: 400px; object-fit: cover;">`;
                headerImageContainer.classList.remove('d-none');
            } else {
                headerImageContainer.classList.add('d-none');
                headerImageContainer.innerHTML = '';
            }
        }

        // Handle Overlay Background
        const mainElement = document.querySelector('main');
        if (mainElement) {
            if (data.overlay_image_url) {
                // Apply background image overlay
                mainElement.style.backgroundImage = `url('${data.overlay_image_url}')`;
                mainElement.style.backgroundSize = 'cover';
                mainElement.style.backgroundPosition = 'center top';
                mainElement.style.backgroundAttachment = 'fixed';
                mainElement.style.backgroundRepeat = 'no-repeat';
            } else {
                // Reset background if no overlay exists
                mainElement.style.backgroundImage = '';
                mainElement.style.backgroundSize = '';
                mainElement.style.backgroundPosition = '';
                mainElement.style.backgroundAttachment = '';
                mainElement.style.backgroundRepeat = '';
            }
        }

        // Process and store all items
        allItems = [];

        // Track paths that need metadata loaded asynchronously
        const pendingMetadataPaths = [];

        // Process directories
        if (data.directories) {
            data.directories.forEach(dir => {
                // Handle both string (old format) and object (new format with thumbnails)
                if (typeof dir === 'string') {
                    const itemPath = data.current_path ? `${data.current_path}/${dir}` : dir;
                    allItems.push({
                        name: dir,
                        type: 'folder',
                        path: itemPath,
                        hasThumbnail: false,
                        hasFiles: false,
                        folderCount: 0,
                        fileCount: 0,
                        metadataPending: true
                    });
                    pendingMetadataPaths.push(itemPath);
                } else {
                    const itemPath = data.current_path ? `${data.current_path}/${dir.name}` : dir.name;
                    const hasPendingMetadata = dir.folder_count === null || dir.folder_count === undefined;
                    const hasPendingThumbnail = !dir.has_thumbnail && dir.thumbnail_url === undefined;
                    allItems.push({
                        name: dir.name,
                        type: 'folder',
                        path: itemPath,
                        hasThumbnail: dir.has_thumbnail || false,
                        thumbnailUrl: dir.thumbnail_url,
                        hasFiles: dir.has_files || false,
                        folderCount: dir.folder_count || 0,
                        fileCount: dir.file_count || 0,
                        metadataPending: hasPendingMetadata,
                        thumbnailPending: hasPendingThumbnail
                    });
                    if (hasPendingMetadata) {
                        pendingMetadataPaths.push(itemPath);
                    }
                }
            });
        }

        // Process files
        if (data.files) {
            data.files.forEach(file => {
                allItems.push({
                    name: file.name,
                    type: 'file',
                    path: data.current_path ? `${data.current_path}/${file.name}` : file.name,
                    size: file.size,
                    hasThumbnail: file.has_thumbnail,
                    thumbnailUrl: file.thumbnail_url,
                    hasComicinfo: file.has_comicinfo
                });
            });
        }

        // Reset to first page on new directory load (unless preserving page)
        if (!preservePage) {
            currentPage = 1;
        }

        // Reset filter and search when loading a new directory (unless preserving page)
        if (!preservePage) {
            currentFilter = 'all';
            gridSearchTerm = '';
            gridSearchRaw = '';
        }

        // Reset All Books mode when loading a new directory
        isAllBooksMode = false;
        allBooksData = null;

        // Reset Recently Added mode when loading a new directory
        isRecentlyAddedMode = false;

        // Reset Continue Reading mode when loading a new directory
        isContinueReadingMode = false;

        // Reset Missing XML mode when loading a new directory
        isMissingXmlMode = false;

        // Update main view button states
        updateMainViewButtons();

        // Update button visibility
        updateViewButtons(path);

        renderPage();

        // Load thumbnails asynchronously for folders that don't have them yet
        const pendingThumbnailPaths = allItems
            .filter(item => item.type === 'folder' && item.thumbnailPending)
            .map(item => item.path);

        // Use prioritized loading: visible page first, then background
        if (pendingMetadataPaths.length > 0 || pendingThumbnailPaths.length > 0) {
            loadBatchDataPrioritized(pendingMetadataPaths, pendingThumbnailPaths);
        }

    } catch (error) {
        console.error('Error loading directory:', error);
        showError(error.message);
    } finally {
        setLoading(false);
    }
}

/**
 * Load metadata (folder/file counts) in parallel batches.
 * @param {Array<string>} paths - Directory paths that need metadata loaded
 * @param {AbortSignal} signal - AbortController signal for cancellation
 */
async function loadMetadataInBatches(paths, signal) {
    const BATCH_SIZE = 100; // Backend max is 100

    const batches = [];
    for (let i = 0; i < paths.length; i += BATCH_SIZE) {
        batches.push(paths.slice(i, i + BATCH_SIZE));
    }

    await Promise.all(batches.map(async (batch) => {
        try {
            const response = await fetch('/api/browse-metadata', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paths: batch }),
                signal
            });

            if (!response.ok) return;
            const data = await response.json();

            // Update allItems and DOM with received metadata
            Object.entries(data.metadata).forEach(([path, meta]) => {
                const item = allItems.find(i => i.path === path);
                if (item) {
                    item.folderCount = meta.folder_count;
                    item.fileCount = meta.file_count;
                    item.hasFiles = meta.has_files;
                    item.metadataPending = false;
                }

                const gridItem = document.querySelector(`[data-path="${CSS.escape(path)}"]`);
                if (gridItem) {
                    const metaEl = gridItem.querySelector('.item-meta');
                    if (metaEl) {
                        metaEl.classList.remove('metadata-loading');
                        const parts = [];
                        if (meta.folder_count > 0) {
                            parts.push(`${meta.folder_count} folder${meta.folder_count !== 1 ? 's' : ''}`);
                        }
                        if (meta.file_count > 0) {
                            parts.push(`${meta.file_count} file${meta.file_count !== 1 ? 's' : ''}`);
                        }
                        metaEl.textContent = parts.length > 0 ? parts.join(' | ') : 'Empty';
                    }
                }
            });
        } catch (error) {
            if (error.name === 'AbortError') return;
            console.error('Error loading metadata batch:', error);
        }
    }));
}

/**
 * Load folder thumbnails in parallel batches.
 * @param {Array<string>} paths - Directory paths that need thumbnails loaded
 * @param {AbortSignal} signal - AbortController signal for cancellation
 */
async function loadThumbnailsInBatches(paths, signal) {
    const BATCH_SIZE = 50; // Backend max is 50

    const batches = [];
    for (let i = 0; i < paths.length; i += BATCH_SIZE) {
        batches.push(paths.slice(i, i + BATCH_SIZE));
    }

    await Promise.all(batches.map(async (batch) => {
        try {
            const response = await fetch('/api/browse-thumbnails', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paths: batch }),
                signal
            });

            if (!response.ok) return;
            const data = await response.json();

            Object.entries(data.thumbnails).forEach(([path, thumbData]) => {
                const item = allItems.find(i => i.path === path);
                if (item) {
                    item.hasThumbnail = thumbData.has_thumbnail;
                    item.thumbnailUrl = thumbData.thumbnail_url;
                    item.thumbnailPending = false;
                }

                if (thumbData.has_thumbnail) {
                    const gridItem = document.querySelector(`[data-path="${CSS.escape(path)}"]`);
                    if (gridItem) {
                        const container = gridItem.querySelector('.thumbnail-container');
                        const img = gridItem.querySelector('.thumbnail');
                        const iconOverlay = gridItem.querySelector('.icon-overlay');

                        if (img && container) {
                            img.src = thumbData.thumbnail_url;
                            img.style.display = 'block';
                            container.classList.add('has-thumbnail');
                            if (iconOverlay) {
                                iconOverlay.style.display = 'none';
                            }
                        }
                    }
                }
            });
        } catch (error) {
            if (error.name === 'AbortError') return;
            console.error('Error loading thumbnails batch:', error);
        }
    }));
}

/**
 * Orchestrate metadata and thumbnail loading with visible-page priority.
 * Loads the current page's data first, then background-loads the rest.
 */
async function loadBatchDataPrioritized(metadataPaths, thumbnailPaths) {
    if (batchAbortController) {
        batchAbortController.abort();
    }
    batchAbortController = new AbortController();
    const signal = batchAbortController.signal;

    // Determine which paths are on the currently visible page
    const visiblePaths = new Set(getCurrentPagePaths());

    const visibleMetadata = metadataPaths.filter(p => visiblePaths.has(p));
    const remainingMetadata = metadataPaths.filter(p => !visiblePaths.has(p));
    const visibleThumbnails = thumbnailPaths.filter(p => visiblePaths.has(p));
    const remainingThumbnails = thumbnailPaths.filter(p => !visiblePaths.has(p));

    // Phase 1: Load visible page data (metadata + thumbnails in parallel)
    const visiblePromises = [];
    if (visibleMetadata.length > 0) visiblePromises.push(loadMetadataInBatches(visibleMetadata, signal));
    if (visibleThumbnails.length > 0) visiblePromises.push(loadThumbnailsInBatches(visibleThumbnails, signal));
    await Promise.all(visiblePromises);

    // Phase 2: Load remaining data in background
    if (signal.aborted) return;
    const remainingPromises = [];
    if (remainingMetadata.length > 0) remainingPromises.push(loadMetadataInBatches(remainingMetadata, signal));
    if (remainingThumbnails.length > 0) remainingPromises.push(loadThumbnailsInBatches(remainingThumbnails, signal));
    await Promise.all(remainingPromises);
}

/**
 * Load metadata and thumbnails for currently visible page items that are still pending.
 * Called on page change, items-per-page change, and filter/search changes.
 */
function loadVisiblePageData() {
    const visiblePaths = getCurrentPagePaths();

    const pendingMetadata = visiblePaths.filter(path => {
        const item = allItems.find(i => i.path === path);
        return item && item.metadataPending;
    });
    const pendingThumbnails = visiblePaths.filter(path => {
        const item = allItems.find(i => i.path === path);
        return item && item.thumbnailPending;
    });

    if (pendingMetadata.length === 0 && pendingThumbnails.length === 0) return;

    if (!batchAbortController || batchAbortController.signal.aborted) {
        batchAbortController = new AbortController();
    }
    const signal = batchAbortController.signal;

    const promises = [];
    if (pendingMetadata.length > 0) promises.push(loadMetadataInBatches(pendingMetadata, signal));
    if (pendingThumbnails.length > 0) promises.push(loadThumbnailsInBatches(pendingThumbnails, signal));
    Promise.all(promises);
}

/**
 * Update main view button states (Directory View vs Recently Added)
 */
function updateMainViewButtons() {
    const directoryViewBtn = document.getElementById('directoryViewBtn');
    const recentlyAddedBtn = document.getElementById('recentlyAddedBtn');

    if (!directoryViewBtn || !recentlyAddedBtn) return;

    if (isRecentlyAddedMode) {
        directoryViewBtn.classList.remove('btn-primary');
        directoryViewBtn.classList.add('btn-outline-primary');
        recentlyAddedBtn.classList.remove('btn-outline-primary');
        recentlyAddedBtn.classList.add('btn-primary');
    } else {
        directoryViewBtn.classList.remove('btn-outline-primary');
        directoryViewBtn.classList.add('btn-primary');
        recentlyAddedBtn.classList.remove('btn-primary');
        recentlyAddedBtn.classList.add('btn-outline-primary');
    }
}

/**
 * Update view toggle button visibility based on current path and mode
 * @param {string} path - Current directory path
 */
function updateViewButtons(path) {
    const allBooksBtn = document.getElementById('allBooksBtn');
    const missingXmlBtn = document.getElementById('missingXmlBtn');
    const folderViewBtn = document.getElementById('folderViewBtn');
    const viewToggleButtons = document.getElementById('viewToggleButtons');

    if (!allBooksBtn || !folderViewBtn || !viewToggleButtons) return;

    if (isRecentlyAddedMode) {
        // In Recently Added mode: show Folder View button to return to dashboard
        viewToggleButtons.style.display = 'block';
        allBooksBtn.style.display = 'none';
        if (missingXmlBtn) missingXmlBtn.style.display = 'none';
        folderViewBtn.style.display = 'inline-block';
    } else if (isMissingXmlMode) {
        // In Missing XML mode: hide All Books and Missing XML, show Folder View
        viewToggleButtons.style.display = 'block';
        allBooksBtn.style.display = 'none';
        if (missingXmlBtn) missingXmlBtn.style.display = 'none';
        folderViewBtn.style.display = 'inline-block';
    } else if (isAllBooksMode) {
        // In All Books mode: hide All Books, show Folder View
        viewToggleButtons.style.display = 'block';
        allBooksBtn.style.display = 'none';
        if (missingXmlBtn) missingXmlBtn.style.display = 'none';
        folderViewBtn.style.display = 'inline-block';
    } else {
        // In Folder mode: show All Books and Missing XML (if not root), hide Folder View
        viewToggleButtons.style.display = 'block';
        if (path === '' || path === '/') {
            allBooksBtn.style.display = 'none';
            if (missingXmlBtn) missingXmlBtn.style.display = 'none';
        } else {
            allBooksBtn.style.display = 'inline-block';
            if (missingXmlBtn) missingXmlBtn.style.display = 'inline-block';
        }
        folderViewBtn.style.display = 'none';
    }
}

/**
 * Load all books recursively from current directory
 */
async function loadAllBooks(preservePage = false) {
    if (isLoading) return;

    setLoading(true);
    folderViewPath = currentPath;  // Save current path to return to
    isAllBooksMode = true;

    try {
        // Start fetching all data
        const fetchPromise = fetch(`/api/browse-recursive?path=${encodeURIComponent(currentPath)}`);

        // Get the response and start reading
        const response = await fetchPromise;
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        allBooksData = data;

        // Map backend snake_case to frontend camelCase for thumbnails
        // In All Books mode, paths are relative to DATA_DIR, so prepend /data/
        const allFiles = data.files.map(file => ({
            ...file,
            // Ensure path starts with /data/ for consistency with folder view
            path: file.path.startsWith('/') ? file.path : `/data/${file.path}`,
            hasThumbnail: file.has_thumbnail,
            thumbnailUrl: file.thumbnail_url,
            hasComicinfo: file.has_comicinfo
        }));

        const totalFiles = allFiles.length;

        // If there are many files, show initial batch immediately
        if (totalFiles > 500) {
            // Get initial batch size (min 20, max 500, based on itemsPerPage)
            const initialBatchSize = Math.max(20, Math.min(itemsPerPage, 500));

            // Show initial batch immediately
            allItems = allFiles.slice(0, initialBatchSize);
            if (!preservePage) {
                currentPage = 1;
                currentFilter = 'all';
                gridSearchTerm = '';
                gridSearchRaw = '';
            }

            updateMainViewButtons();
            updateViewButtons(currentPath);
            renderPage();
            setLoading(false);

            // Show loading indicator for remaining items
            showLoadingMoreIndicator(initialBatchSize, totalFiles);

            // Load remaining files in batches
            await loadRemainingBooksInBackground(allFiles, initialBatchSize);
        } else {
            // For smaller collections, load everything at once
            allItems = allFiles;
            if (!preservePage) {
                currentPage = 1;
                currentFilter = 'all';
                gridSearchTerm = '';
                gridSearchRaw = '';
            }

            updateMainViewButtons();
            updateViewButtons(currentPath);
            renderPage();
            setLoading(false);
        }

    } catch (error) {
        console.error('Error loading all books:', error);
        showError('Failed to load all books: ' + error.message);
        // Reset state on error
        isAllBooksMode = false;
        allBooksData = null;
        updateViewButtons(currentPath);
        setLoading(false);
    }
}

/**
 * Load remaining books in the background
 * @param {Array} allFiles - All files to load
 * @param {number} startIndex - Index to start from
 */
async function loadRemainingBooksInBackground(allFiles, startIndex) {
    backgroundLoadingActive = true;
    const batchSize = 200; // Load 200 items at a time for better performance
    let currentIndex = startIndex;
    let lastRenderTime = Date.now();

    while (currentIndex < allFiles.length && backgroundLoadingActive) {
        // Wait a bit to not block the UI
        await new Promise(resolve => setTimeout(resolve, 200));

        // Check if loading was cancelled
        if (!backgroundLoadingActive) {
            break;
        }

        // Add next batch
        const endIndex = Math.min(currentIndex + batchSize, allFiles.length);
        const newItems = allFiles.slice(currentIndex, endIndex);

        // Add to allItems
        allItems = allItems.concat(newItems);

        // Update loading indicator
        updateLoadingMoreIndicator(allItems.length, allFiles.length);

        // Only update pagination/filter bar, not the entire grid
        // This prevents thumbnails from reloading
        const now = Date.now();
        if (now - lastRenderTime > 1000) { // Update UI at most once per second
            updatePaginationOnly();
            updateFilterBar();
            lastRenderTime = now;
        }

        currentIndex = endIndex;
    }

    // Final update when complete
    if (backgroundLoadingActive) {
        updatePaginationOnly();
        updateFilterBar();
    }

    // Hide loading indicator when done
    backgroundLoadingActive = false;
    hideLoadingMoreIndicator();
}

/**
 * Update pagination controls without re-rendering the grid
 */
function updatePaginationOnly() {
    const filteredItems = getFilteredItems();
    renderPagination(filteredItems.length);
}

/**
 * Show loading indicator for remaining items
 * @param {number} loaded - Number of items loaded
 * @param {number} total - Total number of items
 */
function showLoadingMoreIndicator(loaded, total) {
    const grid = document.getElementById('file-grid');
    let indicator = document.getElementById('loading-more-indicator');

    if (!indicator) {
        indicator = document.createElement('div');
        indicator.id = 'loading-more-indicator';
        indicator.className = 'alert alert-info mt-3';
        indicator.style.textAlign = 'center';
        grid.parentNode.insertBefore(indicator, grid.nextSibling);
    }

    indicator.innerHTML = `
        <div class="d-flex align-items-center justify-content-center">
            <div class="spinner-border spinner-border-sm me-2" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
            <span>Loading books... ${loaded} of ${total}</span>
        </div>
    `;
    indicator.style.display = 'block';
}

/**
 * Update loading indicator with current progress
 * @param {number} loaded - Number of items loaded
 * @param {number} total - Total number of items
 */
function updateLoadingMoreIndicator(loaded, total) {
    const indicator = document.getElementById('loading-more-indicator');
    if (indicator) {
        indicator.innerHTML = `
            <div class="d-flex align-items-center justify-content-center">
                <div class="spinner-border spinner-border-sm me-2" role="status">
                    <span class="visually-hidden">Loading...</span>
                </div>
                <span>Loading books... ${loaded} of ${total}</span>
            </div>
        `;
    }
}

/**
 * Hide loading indicator
 */
function hideLoadingMoreIndicator() {
    const indicator = document.getElementById('loading-more-indicator');
    if (indicator) {
        // Add fade-out animation
        indicator.classList.add('fade-out');

        // Remove it after animation completes
        setTimeout(() => {
            if (indicator && indicator.parentNode) {
                indicator.parentNode.removeChild(indicator);
            }
        }, 300);
    }
}

/**
 * Load all comics missing ComicInfo.xml from current directory
 */
async function loadMissingXml(preservePage = false) {
    if (isLoading) return;

    setLoading(true);
    folderViewPath = currentPath;
    isMissingXmlMode = true;

    try {
        const response = await fetch(`/api/missing-xml?path=${encodeURIComponent(currentPath)}`);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();

        allItems = data.files.map(file => ({
            name: file.name,
            path: file.path,
            size: file.size,
            type: 'file',
            hasThumbnail: file.has_thumbnail,
            thumbnailUrl: file.thumbnail_url,
            hasComicinfo: file.has_comicinfo
        }));

        if (!preservePage) {
            currentPage = 1;
            currentFilter = 'all';
            gridSearchTerm = '';
            gridSearchRaw = '';
        }

        updateBreadcrumb('Missing XML');

        document.getElementById('gridFilterButtons').style.display = 'none';
        document.getElementById('gridSearchRow').style.display = 'block';

        const searchInput = document.querySelector('#gridSearchRow input');
        if (searchInput) {
            searchInput.placeholder = 'Search files missing ComicInfo.xml...';
        }

        const dashboardSections = document.getElementById('dashboard-sections');
        if (dashboardSections) {
            dashboardSections.querySelectorAll('.dashboard-section:not(#library-section)').forEach(el => {
                el.style.display = 'none';
            });
        }

        updateMainViewButtons();
        updateViewButtons(currentPath);
        renderPage();
        setLoading(false);

    } catch (error) {
        console.error('Error loading missing XML files:', error);
        showError('Failed to load missing XML files: ' + error.message);
        isMissingXmlMode = false;
        updateViewButtons(currentPath);
        setLoading(false);
    }
}

/**
 * Return to normal folder view from All Books mode
 */
function returnToFolderView() {
    // Cancel any ongoing background loading
    backgroundLoadingActive = false;
    hideLoadingMoreIndicator();

    isAllBooksMode = false;
    isRecentlyAddedMode = false;
    isContinueReadingMode = false;
    isMissingXmlMode = false;
    allBooksData = null;
    loadDirectory(folderViewPath);
}

/**
 * Load directory view mode (default view)
 */
function loadDirectoryView() {
    // If we're already in directory mode and not in a special mode, do nothing
    if (!isRecentlyAddedMode && !isContinueReadingMode) {
        return;
    }

    // Exit special modes
    isRecentlyAddedMode = false;
    isContinueReadingMode = false;

    // Return to the last folder view
    if (folderViewPath) {
        loadDirectory(folderViewPath);
    } else {
        loadDirectory('');
    }
}

/**
 * Load recently added files (last 100 files)
 * @param {boolean} preservePage - If true, keep current page (for refresh). If false, reset to page 1 (default).
 */
async function loadRecentlyAdded(preservePage = false) {
    if (isLoading) return;

    setLoading(true);
    folderViewPath = currentPath; // Save current path to return to
    isRecentlyAddedMode = true;

    try {
        const response = await fetch('/list-recent-files?limit=100');
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();

        // Map the files to grid format
        const recentFiles = data.files.map(file => ({
            name: file.file_name,
            path: file.file_path,
            size: file.file_size,
            type: 'file',
            hasThumbnail: file.file_path.toLowerCase().endsWith('.cbz') || file.file_path.toLowerCase().endsWith('.cbr'),
            thumbnailUrl: file.file_path.toLowerCase().endsWith('.cbz') || file.file_path.toLowerCase().endsWith('.cbr')
                ? `/api/thumbnail?path=${encodeURIComponent(file.file_path)}`
                : null,
            addedAt: file.added_at
        }));

        allItems = recentFiles;
        if (!preservePage) {
            currentPage = 1;
            currentFilter = 'all';
            gridSearchTerm = '';
            gridSearchRaw = '';
        }

        // Update breadcrumb
        updateBreadcrumb('Recently Added');

        // Hide filter buttons and show search
        document.getElementById('gridFilterButtons').style.display = 'none';
        document.getElementById('gridSearchRow').style.display = 'block';

        // Update search placeholder
        const searchInput = document.querySelector('#gridSearchRow input');
        if (searchInput) {
            searchInput.placeholder = 'Search recently added files...';
        }

        // Hide dashboard swiper sections (not library)
        const dashboardSections = document.getElementById('dashboard-sections');
        if (dashboardSections) {
            dashboardSections.querySelectorAll('.dashboard-section:not(#library-section)').forEach(el => {
                el.style.display = 'none';
            });
        }

        // Show Folder View button to allow returning to dashboard
        const viewToggleButtons = document.getElementById('viewToggleButtons');
        const folderViewBtn = document.getElementById('folderViewBtn');
        const allBooksBtn = document.getElementById('allBooksBtn');
        if (viewToggleButtons && folderViewBtn) {
            viewToggleButtons.style.display = 'block';
            folderViewBtn.style.display = 'inline-block';
            if (allBooksBtn) allBooksBtn.style.display = 'none';
        }

        renderPage();
        setLoading(false);

    } catch (error) {
        console.error('Error loading recently added files:', error);
        showError('Failed to load recently added files: ' + error.message);
        isRecentlyAddedMode = false;
        setLoading(false);
    }
}

/**
 * Load continue reading items in full-page grid view (View All)
 */
async function loadContinueReading(preservePage = false) {
    if (isLoading) return;

    setLoading(true);
    folderViewPath = currentPath; // Save current path to return to
    isContinueReadingMode = true;

    try {
        const response = await fetch('/api/continue-reading?limit=100');
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();

        // Map the items to grid format
        const continueReadingFiles = (data.items || []).map(item => ({
            name: item.file_name,
            path: item.comic_path,
            type: 'file',
            hasThumbnail: true,
            thumbnailUrl: `/api/thumbnail?path=${encodeURIComponent(item.comic_path)}`,
            pageNumber: item.page_number,
            totalPages: item.total_pages,
            progressPercent: item.progress_percent,
            updatedAt: item.updated_at
        }));

        allItems = continueReadingFiles;
        if (!preservePage) {
            currentPage = 1;
            currentFilter = 'all';
            gridSearchTerm = '';
            gridSearchRaw = '';
        }

        // Update breadcrumb
        updateBreadcrumb('Continue Reading');

        // Hide filter buttons and show search
        document.getElementById('gridFilterButtons').style.display = 'none';
        document.getElementById('gridSearchRow').style.display = 'block';

        // Update search placeholder
        const searchInput = document.querySelector('#gridSearchRow input');
        if (searchInput) {
            searchInput.placeholder = 'Search in-progress comics...';
        }

        // Hide dashboard swiper sections (not library)
        const dashboardSections = document.getElementById('dashboard-sections');
        if (dashboardSections) {
            dashboardSections.querySelectorAll('.dashboard-section:not(#library-section)').forEach(el => {
                el.style.display = 'none';
            });
        }

        // Show Folder View button to allow returning to dashboard
        const viewToggleButtons = document.getElementById('viewToggleButtons');
        const folderViewBtn = document.getElementById('folderViewBtn');
        const allBooksBtn = document.getElementById('allBooksBtn');
        if (viewToggleButtons && folderViewBtn) {
            viewToggleButtons.style.display = 'block';
            folderViewBtn.style.display = 'inline-block';
            if (allBooksBtn) allBooksBtn.style.display = 'none';
        }

        renderPage();
        setLoading(false);

    } catch (error) {
        console.error('Error loading continue reading items:', error);
        showError('Failed to load continue reading items: ' + error.message);
        isContinueReadingMode = false;
        setLoading(false);
    }
}


/**
 * Render the current page of items.
 */
function renderPage() {
    const filteredItems = getFilteredItems();

    const startIndex = (currentPage - 1) * itemsPerPage;
    const endIndex = startIndex + itemsPerPage;
    const pageItems = filteredItems.slice(startIndex, endIndex);

    renderGrid(pageItems);
    renderPagination(filteredItems.length);
    updateFilterBar();
}

/**
 * Render the file and folder grid.
 * @param {Array} items - The list of items to render.
 */
function renderGrid(items) {
    const grid = document.getElementById('file-grid');
    const emptyState = document.getElementById('empty-state');
    const template = document.getElementById('grid-item-template');
    const librarySection = document.getElementById('library-section');

    // Dispose tooltips before clearing the grid to prevent memory leaks
    disposeNameTooltips(grid);
    grid.innerHTML = '';

    // Show library section (empty-state and file-grid are inside it)
    if (librarySection) librarySection.style.display = 'block';

    if (items.length === 0 && allItems.length === 0) {
        grid.style.display = 'none';
        emptyState.style.display = 'block';
        return;
    }

    grid.style.display = 'grid';
    emptyState.style.display = 'none';

    // Create document fragment for better performance
    const fragment = document.createDocumentFragment();

    items.forEach(item => {
        const clone = template.content.cloneNode(true);
        const gridItem = clone.querySelector('.grid-item');
        const img = clone.querySelector('.thumbnail');
        const iconOverlay = clone.querySelector('.icon-overlay');
        const icon = iconOverlay.querySelector('i');
        const nameEl = clone.querySelector('.item-name');
        const metaEl = clone.querySelector('.item-meta');

        const actionsDropdown = clone.querySelector('.item-actions');

        // Set content
        nameEl.textContent = item.name;
        nameEl.title = item.name;

        // Determine if we're at root level (folders directly off /data)
        const isRootLevel = !currentPath || currentPath === '' || currentPath === '/data';

        if (item.type === 'folder') {
            gridItem.classList.add('folder');

            // Add data-path for progressive metadata updates
            gridItem.setAttribute('data-path', item.path);

            // Build folder metadata string showing counts (or loading state)
            if (item.metadataPending) {
                metaEl.textContent = 'Loading...';
                metaEl.classList.add('metadata-loading');
            } else {
                const parts = [];
                if (item.folderCount > 0) {
                    parts.push(`${item.folderCount} folder${item.folderCount !== 1 ? 's' : ''}`);
                }
                if (item.fileCount > 0) {
                    parts.push(`${item.fileCount} file${item.fileCount !== 1 ? 's' : ''}`);
                }
                metaEl.textContent = parts.length > 0 ? parts.join(' | ') : 'Empty';
            }

            // Hide info button for folders
            const infoButton = clone.querySelector('.info-button');
            if (infoButton) infoButton.style.display = 'none';

            // Add root-folder class for CSS targeting
            if (isRootLevel) {
                gridItem.classList.add('root-folder');
            }

            // Handle favorite button for root-level folders only
            const favoriteButton = clone.querySelector('.favorite-button');
            if (favoriteButton) {
                if (isRootLevel) {
                    favoriteButton.style.display = 'flex';

                    // Check if this folder is already favorited
                    if (window.favoritePaths && window.favoritePaths.has(item.path)) {
                        favoriteButton.classList.add('favorited');
                        const favIcon = favoriteButton.querySelector('i');
                        if (favIcon) favIcon.className = 'bi bi-bookmark-heart-fill';
                        favoriteButton.title = 'Remove from Favorites';
                    }

                    favoriteButton.onclick = (e) => {
                        e.stopPropagation();
                        e.preventDefault();
                        togglePublisherFavorite(item.path, item.name, favoriteButton);
                    };
                } else {
                    favoriteButton.style.display = 'none';
                }
            }

            // Handle "To Read" button for non-root items (folders and files)
            const toReadButton = clone.querySelector('.to-read-button');
            if (toReadButton) {
                if (!isRootLevel) {
                    toReadButton.style.display = 'flex';

                    // Check if this item is already in "To Read" list
                    if (window.toReadPaths && window.toReadPaths.has(item.path)) {
                        toReadButton.classList.add('marked');
                        const toReadIcon = toReadButton.querySelector('i');
                        if (toReadIcon) toReadIcon.className = 'bi bi-bookmark';
                        toReadButton.title = 'Remove from To Read';
                    }

                    toReadButton.onclick = (e) => {
                        e.stopPropagation();
                        e.preventDefault();
                        toggleToRead(item.path, item.name, item.type, toReadButton);
                    };
                } else {
                    toReadButton.style.display = 'none';
                }
            }

            // Show actions menu for all folders:
            // - Root level: Only Missing File Check
            // - Non-root level: Full menu (Generate Thumbnail, Missing File Check, Delete)
            if (actionsDropdown) {
                const btn = actionsDropdown.querySelector('button');
                if (btn) {
                    btn.onclick = (e) => {
                        e.stopPropagation();
                        // Bootstrap handles the dropdown toggle automatically
                    };
                }

                // Close dropdown on mouse leave with a small delay
                let leaveTimeout;
                actionsDropdown.onmouseleave = () => {
                    leaveTimeout = setTimeout(() => {
                        if (btn) {
                            const dropdown = bootstrap.Dropdown.getInstance(btn);
                            if (dropdown) {
                                dropdown.hide();
                            }
                        }
                    }, 300);
                };

                // Cancel the close if mouse re-enters
                actionsDropdown.onmouseenter = () => {
                    if (leaveTimeout) {
                        clearTimeout(leaveTimeout);
                    }
                };

                // Replace menu items with folder-specific options
                const dropdownMenu = actionsDropdown.querySelector('.dropdown-menu');
                if (dropdownMenu) {
                    // If at root level, show Missing File Check, Scan Files, and Generate All Missing Thumbnails
                    if (isRootLevel) {
                        dropdownMenu.innerHTML = `
                            <li><a class="dropdown-item folder-action-gen-all-thumbs" href="#"><i class="bi bi-images"></i> Generate All Missing Thumbnails</a></li>
                            <li><a class="dropdown-item folder-action-scan" href="#"><i class="bi bi-arrow-clockwise"></i> Scan Files</a></li>
                            <li><a class="dropdown-item folder-action-missing" href="#"><i class="bi bi-file-earmark-text"></i> Missing File Check</a></li>
                        `;
                    } else {
                        // For folders with files, show full menu
                        dropdownMenu.innerHTML = `
                        <li><a class="dropdown-item folder-action-thumbnail" href="#"><i class="bi bi-image"></i> Generate Thumbnail</a></li>
                        <li><a class="dropdown-item folder-action-scan" href="#"><i class="bi bi-arrow-clockwise"></i> Scan Files</a></li>
                        <li><a class="dropdown-item folder-action-missing" href="#"><i class="bi bi-file-earmark-text"></i> Missing File Check</a></li>
                        <li><a class="dropdown-item folder-action-update-xml" href="#"><i class="bi bi-filetype-xml"></i> Update XML</a></li>
                        <li><hr class="dropdown-divider"></li>
                        <li><a class="dropdown-item folder-action-delete text-danger" href="#"><i class="bi bi-trash"></i> Delete</a></li>
                        `;

                        // Bind Generate Thumbnail action
                        const thumbnailAction = dropdownMenu.querySelector('.folder-action-thumbnail');
                        if (thumbnailAction) {
                            thumbnailAction.onclick = (e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                generateFolderThumbnail(item.path, item.name);
                            };
                        }

                        // Bind Delete action
                        const deleteAction = dropdownMenu.querySelector('.folder-action-delete');
                        if (deleteAction) {
                            deleteAction.onclick = (e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                showDeleteConfirmation(item);
                            };
                        }

                        // Bind Update XML action
                        const updateXmlAction = dropdownMenu.querySelector('.folder-action-update-xml');
                        if (updateXmlAction) {
                            updateXmlAction.onclick = (e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                openUpdateXmlModal(item.path, item.name);
                            };
                        }
                    }

                    // Bind Missing File Check action (available for both root and folders with files)
                    const missingAction = dropdownMenu.querySelector('.folder-action-missing');
                    if (missingAction) {
                        missingAction.onclick = (e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            checkMissingFiles(item.path, item.name);
                        };
                    }

                    // Bind Scan Files action (only for root level directories)
                    const scanAction = dropdownMenu.querySelector('.folder-action-scan');
                    if (scanAction) {
                        scanAction.onclick = (e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            scanDirectory(item.path, item.name);
                        };
                    }

                    // Bind Generate All Missing Thumbnails action (only for root level directories)
                    const genAllThumbsAction = dropdownMenu.querySelector('.folder-action-gen-all-thumbs');
                    if (genAllThumbsAction) {
                        genAllThumbsAction.onclick = (e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            generateAllMissingThumbnails(item.path, item.name);
                        };
                    }
                }
            }

            // Check if folder has a thumbnail
            if (item.hasThumbnail && item.thumbnailUrl) {
                // Use the folder thumbnail image
                gridItem.classList.add('has-thumbnail');
                img.src = item.thumbnailUrl;
                img.style.display = 'block';
                iconOverlay.style.display = 'none';
            } else {
                // Use the default folder icon
                icon.className = 'bi bi-folder-fill';
                img.style.display = 'none';
            }

            // Handle click for folders
            gridItem.onclick = () => loadDirectory(item.path);

            // Enable drag and drop for folders
            setupFolderDropZone(gridItem, item.path);

        } else {
            gridItem.classList.add('file');
            metaEl.textContent = formatFileSize(item.size);

            // Add has-comic class for comic files
            if (item.hasThumbnail) {
                gridItem.classList.add('has-comic');

                // Show issue number badge for comics
                const issueBadge = clone.querySelector('.issue-badge');
                if (issueBadge) {
                    const issueNum = extractIssueNumber(item.name);
                    if (issueNum) {
                        const issueNumberSpan = issueBadge.querySelector('.issue-number');
                        if (issueNumberSpan) {
                            issueNumberSpan.textContent = '#' + issueNum;
                        }
                        // Check read status and update icon
                        const readIcon = issueBadge.querySelector('.read-icon');
                        if (readIcon && readIssuesSet.has(item.path)) {
                            readIcon.classList.replace('bi-book', 'bi-book-fill');
                        }
                        issueBadge.style.display = 'block';
                    }
                }
            }

            // Show missing XML badge if has_comicinfo === 0 (confirmed missing)
            if (item.hasComicinfo === 0) {
                const xmlBadge = clone.querySelector('.xml-badge');
                if (xmlBadge) {
                    xmlBadge.style.display = 'block';
                }
            }

            // Handle actions menu
            if (actionsDropdown) {
                const btn = actionsDropdown.querySelector('button');
                if (btn) {
                    btn.onclick = (e) => {
                        e.stopPropagation();
                        // Bootstrap handles the dropdown toggle automatically
                    };
                }

                // Close dropdown on mouse leave with a small delay
                let leaveTimeout;
                actionsDropdown.onmouseleave = () => {
                    leaveTimeout = setTimeout(() => {
                        if (btn) {
                            const dropdown = bootstrap.Dropdown.getInstance(btn);
                            if (dropdown) {
                                dropdown.hide();
                            }
                        }
                    }, 300); // 300ms delay to allow moving to menu
                };

                // Cancel the close if mouse re-enters
                actionsDropdown.onmouseenter = () => {
                    if (leaveTimeout) {
                        clearTimeout(leaveTimeout);
                    }
                };

                // Update "Set Read Date" text based on read status
                const setReadDateText = actionsDropdown.querySelector('.set-read-date-text');
                if (setReadDateText) {
                    const isRead = readIssuesSet.has(item.path);
                    setReadDateText.textContent = isRead ? 'Update Read Date' : 'Set Read Date';
                }

                // Bind actions
                const actions = {
                    '.action-crop': () => executeScript('crop', item.path),
                    '.action-remove-first': () => executeScript('remove', item.path),
                    '.action-edit': () => initEditMode(item.path),
                    '.action-rebuild': () => executeScript('single_file', item.path),
                    '.action-enhance': () => executeScript('enhance_single', item.path),
                    '.action-set-read-date': () => openSetReadDateModal(item.path, readIssuesSet.has(item.path)),
                    '.action-delete': () => showDeleteConfirmation(item)
                };

                Object.entries(actions).forEach(([selector, handler]) => {
                    const el = actionsDropdown.querySelector(selector);
                    if (el) {
                        el.onclick = (e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            handler();
                        };
                    }
                });
            }

            if (item.hasThumbnail) {
                // Set placeholder initially, real source in data-src for lazy loading
                img.src = '/static/images/loading.svg';
                img.dataset.src = item.thumbnailUrl;
                img.dataset.thumbnailPath = item.thumbnailUrl; // Store for polling
                img.classList.add('lazy');
                img.classList.add('polling'); // Always poll thumbnails until confirmed loaded

                // Handle error loading thumbnail
                img.onerror = function () {
                    this.src = '/static/images/error.svg';
                    this.classList.remove('lazy');
                    this.classList.remove('polling'); // Stop polling on error
                };

                // Handle successful load
                img.onload = function () {
                    // If we are polling, check status
                    if (this.classList.contains('polling')) {
                        pollThumbnail(this);
                    }
                };
            } else {
                // Generic file icon
                gridItem.classList.add('folder'); // Use folder style for icon overlay
                icon.className = 'bi bi-file-earmark-text';
                img.style.display = 'none';

                // Hide info button and actions menu for non-comic files
                const infoButton = clone.querySelector('.info-button');
                if (infoButton) infoButton.style.display = 'none';

                // Hide actions dropdown for .txt files (those actions don't apply)
                if (item.name.toLowerCase().endsWith('.txt')) {
                    const actionsDropdown = clone.querySelector('.item-actions');
                    if (actionsDropdown) actionsDropdown.style.display = 'none';
                }
            }

            // Handle "To Read" button for files (non-root items)
            const toReadButton = clone.querySelector('.to-read-button');
            if (toReadButton) {
                if (!isRootLevel) {
                    toReadButton.style.display = 'flex';
                    // Check if already marked as "to read"
                    if (window.toReadPaths && window.toReadPaths.has(item.path)) {
                        toReadButton.classList.add('marked');
                        const toReadIcon = toReadButton.querySelector('i');
                        if (toReadIcon) toReadIcon.className = 'bi bi-bookmark';
                        toReadButton.title = 'Remove from To Read';
                    }
                    toReadButton.onclick = (e) => {
                        e.stopPropagation();
                        e.preventDefault();
                        toggleToRead(item.path, item.name, item.type, toReadButton);
                    };
                } else {
                    toReadButton.style.display = 'none';
                }
            }

            // Handle click for files - open comic reader for comic files, text viewer for .txt files
            gridItem.onclick = (e) => {
                console.log('Grid item clicked:', item.name, 'hasThumbnail:', item.hasThumbnail);
                if (item.hasThumbnail) {
                    // Open comic reader for CBZ/CBR/ZIP files
                    openComicReader(item.path);
                } else if (item.name.toLowerCase().endsWith('.txt')) {
                    // Open text file viewer for .txt files
                    console.log('Opening text file viewer for:', item.path);
                    openTextFileViewer(item.path, item.name);
                } else {
                    console.log('Clicked file:', item.path);
                }
            };

            // Add info button event listener for comic files
            if (item.hasThumbnail) {
                const infoButton = gridItem.querySelector('.info-button');
                if (infoButton) {
                    infoButton.onclick = (e) => {
                        e.stopPropagation();
                        e.preventDefault();
                        showCBZInfo(item.path, item.name);
                    };
                }
            }
        }

        fragment.appendChild(clone);
    });

    grid.appendChild(fragment);

    // Initialize lazy loading
    initLazyLoading();

    // Initialize Bootstrap tooltips for truncated names
    initNameTooltips(grid);
}


/**
 * Render pagination controls.
 * @param {number} totalItems - Total number of items (after filtering)
 */
function renderPagination(totalItems) {
    const paginationNav = document.getElementById('pagination-controls');
    const paginationList = document.getElementById('pagination-list');

    // Use totalItems parameter, or default to allItems.length for backward compatibility
    const itemCount = totalItems !== undefined ? totalItems : allItems.length;

    if (itemCount <= itemsPerPage) {
        paginationNav.style.display = 'none';
        return;
    }

    paginationNav.style.display = 'block';
    paginationList.innerHTML = '';

    const totalPages = Math.ceil(itemCount / itemsPerPage);

    // Previous Button
    const prevLi = document.createElement('li');
    prevLi.className = `page-item ${currentPage === 1 ? 'disabled' : ''}`;
    prevLi.innerHTML = `<a class="page-link" href="#" onclick="changePage(${currentPage - 1}); return false;">Previous</a>`;
    paginationList.appendChild(prevLi);

    // Page Info (e.g., "Page 1 of 5")
    const infoLi = document.createElement('li');
    infoLi.className = 'page-item disabled';
    infoLi.innerHTML = `<span class="page-link text-dark">Page ${currentPage} of ${totalPages}</span>`;
    paginationList.appendChild(infoLi);

    // Next Button
    const nextLi = document.createElement('li');
    nextLi.className = `page-item ${currentPage === totalPages ? 'disabled' : ''}`;
    nextLi.innerHTML = `<a class="page-link" href="#" onclick="changePage(${currentPage + 1}); return false;">Next</a>`;
    paginationList.appendChild(nextLi);

    // Jump To dropdown (only show if there are multiple pages)
    if (totalPages > 1) {
        const jumpLi = document.createElement('li');
        jumpLi.className = 'page-item';

        // Create select dropdown with all pages
        let optionsHtml = '';
        for (let i = 1; i <= totalPages; i++) {
            optionsHtml += `<option value="${i}" ${i === currentPage ? 'selected' : ''}>Page ${i}</option>`;
        }

        jumpLi.innerHTML = `
            <select class="form-select form-select-sm" onchange="jumpToPage(this.value)" style="width: auto; border-radius: 0.375rem; margin: 0 0.25rem;">
                ${optionsHtml}
            </select>
        `;
        paginationList.appendChild(jumpLi);
    }
}

/**
 * Change the current page.
 * @param {number} page - The page number to switch to.
 */
function changePage(page) {
    const filteredItems = getFilteredItems();
    const totalPages = Math.ceil(filteredItems.length / itemsPerPage);
    if (page < 1 || page > totalPages) return;

    currentPage = page;
    renderPage();
    loadVisiblePageData();

    // Scroll to top of grid
    document.getElementById('file-grid').scrollIntoView({ behavior: 'smooth' });
}

/**
 * Jump to a specific page from the dropdown selector.
 * @param {string|number} page - The page number to jump to.
 */
function jumpToPage(page) {
    changePage(parseInt(page));
}

/**
 * Change items per page.
 * @param {number} value - The number of items per page.
 */
function changeItemsPerPage(value) {
    itemsPerPage = parseInt(value);
    currentPage = 1;
    renderPage();
    loadVisiblePageData();
}

/**
 * Update the filter bar with available letters based on current items.
 */
function updateFilterBar() {
    const filterContainer = document.getElementById('gridFilterButtons');
    if (!filterContainer) return;

    const btnGroup = filterContainer.querySelector('.btn-group');
    if (!btnGroup) return;

    // Only filter based on directories and files
    let availableLetters = new Set();
    let hasNonAlpha = false;

    allItems.forEach(item => {
        const firstChar = item.name.charAt(0).toUpperCase();
        if (firstChar >= 'A' && firstChar <= 'Z') {
            availableLetters.add(firstChar);
        } else {
            hasNonAlpha = true;
        }
    });

    // Build filter buttons
    let buttonsHtml = '';
    buttonsHtml += `<button type="button" class="btn btn-outline-secondary ${currentFilter === 'all' ? 'active' : ''}" onclick="filterItems('all')">All</button>`;

    if (hasNonAlpha) {
        buttonsHtml += `<button type="button" class="btn btn-outline-secondary ${currentFilter === '#' ? 'active' : ''}" onclick="filterItems('#')">#</button>`;
    }

    for (let i = 65; i <= 90; i++) {
        const letter = String.fromCharCode(i);
        if (availableLetters.has(letter)) {
            buttonsHtml += `<button type="button" class="btn btn-outline-secondary ${currentFilter === letter ? 'active' : ''}" onclick="filterItems('${letter}')">${letter}</button>`;
        }
    }

    btnGroup.innerHTML = buttonsHtml;

    // Show the filter bar if we have items
    if (allItems.length > 0) {
        filterContainer.style.display = 'block';
    } else {
        filterContainer.style.display = 'none';
    }

    // --- SEARCH BOX LOGIC (show if >25 items) ---
    const searchRow = document.getElementById('gridSearchRow');
    if (searchRow) {
        // Check if search input already exists
        let existingInput = document.getElementById('gridSearch');

        if (allItems.length > 25) {
            // Only create input if it doesn't exist
            if (!existingInput) {
                searchRow.innerHTML = `<input type="text" id="gridSearch" class="form-control form-control-sm" placeholder="Type to filter..." oninput="onGridSearch(this.value)">`;
                existingInput = document.getElementById('gridSearch');
            }
            // Update value if it doesn't match current search term (use raw for display)
            if (existingInput && existingInput.value !== gridSearchRaw) {
                existingInput.value = gridSearchRaw;
            }
        } else {
            // Remove input if items <= 25
            if (existingInput) {
                searchRow.innerHTML = '';
            }
        }
    }
}

/**
 * Filter items based on the selected letter.
 * @param {string} letter - The letter to filter by ('all', '#', or A-Z)
 */
function filterItems(letter) {
    // Toggle: if clicking the same filter, reset to 'all'
    if (currentFilter === letter) {
        currentFilter = 'all';
    } else {
        currentFilter = letter;
    }

    // Update button states
    const filterContainer = document.getElementById('gridFilterButtons');
    if (filterContainer) {
        const btnGroup = filterContainer.querySelector('.btn-group');
        if (btnGroup) {
            const buttons = btnGroup.querySelectorAll('button');
            buttons.forEach(btn => {
                const btnText = btn.textContent.trim();
                if ((currentFilter === 'all' && btnText === 'All') || btnText === currentFilter) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
            });
        }
    }

    // Reset to first page and re-render
    currentPage = 1;
    renderPage();
    loadVisiblePageData();
}

/**
 * Poll a thumbnail URL to check if it's ready.
 * @param {HTMLImageElement} imgElement - The image element to update
 */
function pollThumbnail(imgElement) {
    if (!imgElement.classList.contains('polling')) {
        return; // Stop if polling was cancelled
    }

    // Avoid multiple concurrent polls for the same image
    if (imgElement.dataset.isPolling === 'true') return;
    imgElement.dataset.isPolling = 'true';

    const thumbnailUrl = imgElement.dataset.thumbnailPath;
    if (!thumbnailUrl) {
        imgElement.dataset.isPolling = 'false';
        return;
    }

    // Add a cache-busting parameter to force a fresh check
    const checkUrl = thumbnailUrl + (thumbnailUrl.includes('?') ? '&' : '?') + '_check=' + Date.now();

    fetch(checkUrl, { method: 'HEAD' })
        .then(response => {
            imgElement.dataset.isPolling = 'false';

            // Check if we were redirected to the loading image or error image
            const isRedirectedToLoading = response.url.includes('loading.svg');
            const isRedirectedToError = response.url.includes('error.svg');

            // If we get a 200 AND it's not the loading/error image
            if (response.ok && response.status === 200 && !isRedirectedToLoading && !isRedirectedToError) {
                // Thumbnail is ready! 
                const newSrc = thumbnailUrl + (thumbnailUrl.includes('?') ? '&' : '?') + '_t=' + Date.now();

                // We found it's ready. Stop polling.
                imgElement.classList.remove('polling');

                // Update the image to the new version
                imgElement.src = newSrc;

            } else if (imgElement.classList.contains('polling')) {
                // Still generating, poll again in 2 seconds
                setTimeout(() => pollThumbnail(imgElement), 2000);
            }
        })
        .catch(error => {
            console.error('Error polling thumbnail:', error);
            imgElement.dataset.isPolling = 'false';
            // Retry after a longer delay on error
            if (imgElement.classList.contains('polling')) {
                setTimeout(() => pollThumbnail(imgElement), 5000);
            }
        });
}

/**
 * Update the breadcrumb navigation.
 * @param {string} path - The current directory path.
 */
/**
 * Update breadcrumb with a simple title (for special views like Recently Added)
 * @param {string} title - The title to display
 */
function updateBreadcrumb(title) {
    const breadcrumb = document.getElementById('breadcrumb');
    breadcrumb.innerHTML = '';

    const li = document.createElement('li');
    li.className = 'breadcrumb-item active';
    li.textContent = title;
    breadcrumb.appendChild(li);
}

function renderBreadcrumbs(path) {
    const breadcrumb = document.getElementById('breadcrumb');
    breadcrumb.innerHTML = '';

    // Always add Home/Root
    const homeLi = document.createElement('li');
    homeLi.className = 'breadcrumb-item';
    if (!path) {
        homeLi.classList.add('active');
        homeLi.textContent = 'Home';
    } else {
        const homeLink = document.createElement('a');
        homeLink.href = '#';
        homeLink.textContent = 'Home';
        homeLink.onclick = (e) => {
            e.preventDefault();
            loadDirectory('');
        };
        homeLi.appendChild(homeLink);
    }
    breadcrumb.appendChild(homeLi);

    if (!path) return;

    // Split path into segments
    // Handle both forward and backward slashes just in case, though API should normalize
    const segments = path.split(/[/\\]/).filter(Boolean);
    let builtPath = '';

    segments.forEach((segment, index) => {
        const isLast = index === segments.length - 1;
        const li = document.createElement('li');
        li.className = 'breadcrumb-item';

        // Reconstruct path for this segment
        // Note: We need to be careful about how we join. 
        // If the original path started with /, we might need to handle that, 
        // but usually the API returns a clean path relative to DATA_DIR or absolute.
        // For simplicity, we'll assume the API handles the path string correctly when passed back.
        if (index === 0) {
            // If the path is absolute (starts with / on linux or C:\ on windows), 
            // the split might behave differently. 
            // However, for the breadcrumb UI, we just want the folder names.
            // We'll reconstruct the path cumulatively.
            // Actually, let's just use the segments.
            builtPath = segment;
            // If the original path started with a separator that got split out, we might need to prepend it?
            // Let's assume the path passed to loadDirectory is what we want to pass back.
            // If path starts with /, split gives empty string first.
            if (path.startsWith('/')) builtPath = '/' + builtPath;
            else if (path.includes(':\\') && index === 0) {
                // Windows drive letter, keep it as is
            }
        } else {
            builtPath += '/' + segment;
        }

        if (isLast) {
            li.classList.add('active');
            li.textContent = segment;
        } else {
            const link = document.createElement('a');
            link.href = '#';
            link.textContent = segment;
            // Capture the current value of builtPath
            const clickPath = builtPath;
            link.onclick = (e) => {
                e.preventDefault();
                loadDirectory(clickPath);
            };
            li.appendChild(link);
        }
        breadcrumb.appendChild(li);
    });

    // Update library header title if function exists (multi-library support)
    if (typeof updateLibraryHeaderTitle === 'function') {
        updateLibraryHeaderTitle(path);
    }
}

/**
 * Initialize IntersectionObserver for lazy loading thumbnails.
 */
function initLazyLoading() {
    if ('IntersectionObserver' in window) {
        const imageObserver = new IntersectionObserver((entries, observer) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    const img = entry.target;
                    if (img.dataset.src) {
                        img.src = img.dataset.src;
                        img.classList.remove('lazy');
                        observer.unobserve(img);
                    }
                }
            });
        });

        const lazyImages = document.querySelectorAll('img.lazy');
        lazyImages.forEach(img => {
            imageObserver.observe(img);
        });
    } else {
        // Fallback for older browsers
        const lazyImages = document.querySelectorAll('img.lazy');
        lazyImages.forEach(img => {
            img.src = img.dataset.src;
            img.classList.remove('lazy');
        });
    }
}

/**
 * Initialize Bootstrap tooltips on .item-name elements that are actually truncated.
 * Only creates a tooltip when the text overflows (scrollWidth > clientWidth).
 * @param {HTMLElement} [container=document] - Scope to search within
 */
function initNameTooltips(container) {
    const root = container || document;
    root.querySelectorAll('.item-name').forEach(el => {
        // Dispose any existing tooltip first
        const existing = bootstrap.Tooltip.getInstance(el);
        if (existing) existing.dispose();

        if (el.scrollWidth > el.clientWidth) {
            // Text is truncated — restore title if we stashed it earlier
            if (!el.getAttribute('title') && el.dataset.originalTitle) {
                el.setAttribute('title', el.dataset.originalTitle);
            }
            new bootstrap.Tooltip(el, {
                placement: 'bottom',
                trigger: 'hover',
                customClass: 'item-name-tooltip'
            });
        } else {
            // Not truncated — suppress native tooltip but stash for later
            const title = el.getAttribute('title');
            if (title) {
                el.dataset.originalTitle = title;
                el.removeAttribute('title');
            }
        }
    });
}

/**
 * Dispose Bootstrap tooltips on .item-name elements to prevent memory leaks.
 * @param {HTMLElement} [container=document] - Scope to search within
 */
function disposeNameTooltips(container) {
    const root = container || document;
    root.querySelectorAll('.item-name').forEach(el => {
        const instance = bootstrap.Tooltip.getInstance(el);
        if (instance) instance.dispose();
    });
}

/**
 * Toggle loading state UI.
 * @param {boolean} loading
 */
function setLoading(loading) {
    isLoading = loading;
    const indicator = document.getElementById('loading-indicator');
    const grid = document.getElementById('file-grid');
    const empty = document.getElementById('empty-state');
    const pagination = document.getElementById('pagination-controls');

    if (loading) {
        indicator.style.display = 'block';
        grid.style.display = 'none';
        empty.style.display = 'none';
        if (pagination) pagination.style.display = 'none';
    } else {
        indicator.style.display = 'none';
        // grid display is handled in renderGrid
    }
}

/**
 * Show error message using Bootstrap Toast.
 * @param {string} message
 */
function showError(message) {
    const toastEl = document.getElementById('errorToast');
    const toastBody = document.getElementById('errorToastBody');

    if (toastEl && toastBody) {
        toastBody.textContent = message;
        const toast = new bootstrap.Toast(toastEl, {
            autohide: true,
            delay: 5000
        });
        toast.show();
    } else {
        // Fallback to alert if toast elements not found
        alert('Error: ' + message);
    }
}

/**
 * Show success message using Bootstrap Toast.
 * @param {string} message
 */
function showSuccess(message) {
    const toastEl = document.getElementById('successToast');
    const toastBody = document.getElementById('successToastBody');

    if (toastEl && toastBody) {
        toastBody.textContent = message;
        const toast = new bootstrap.Toast(toastEl, {
            autohide: true,
            delay: 3000
        });
        toast.show();
    } else {
        // Fallback to alert if toast elements not found
        alert(message);
    }
}

/**
 * Format file size bytes to human readable string.
 * @param {number} bytes 
 * @returns {string}
 */
function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

/**
 * Extract issue number from comic filename.
 * @param {string} filename - The comic filename
 * @returns {string|null} - The issue number or null if not found
 */
function extractIssueNumber(filename) {
    // Pattern priority: "Name 001 (2022)", "Name #001", "Name 001.cbz"
    const patterns = [
        /\s(\d{1,4})\s*\(\d{4}\)/,   // "Name 001 (2022)"
        /#(\d{1,4})/,                 // "Name #001"
        /\s(\d{1,4})\.[^.]+$/         // "Name 001.cbz" (number before extension)
    ];
    for (const pattern of patterns) {
        const match = filename.match(pattern);
        if (match) return match[1];
    }
    return null;
}


// Handle browser back/forward buttons
window.onpopstate = (event) => {
    if (event.state && event.state.path !== undefined) {
        loadDirectory(event.state.path);
    } else {
        // Default to root if no state
        loadDirectory('');
    }
};

// -- File Action Execution Functions --

let currentEventSource = null;

/**
 * Show the global progress indicator
 */
function showProgressIndicator() {
    const progressContainer = document.getElementById('progress-container');
    if (progressContainer) {
        progressContainer.style.display = 'block';
    }
}

/**
 * Hide the global progress indicator
 */
function hideProgressIndicator() {
    const progressContainer = document.getElementById('progress-container');
    if (progressContainer) {
        progressContainer.style.display = 'none';
    }
}

/**
 * Refresh a specific thumbnail after an action completes
 * @param {string} filePath - The file path whose thumbnail should be refreshed
 */
function refreshThumbnail(filePath) {
    // Find the image element for this file path
    const grid = document.getElementById('file-grid');
    if (!grid) return;

    // Find all grid items
    const gridItems = grid.querySelectorAll('.grid-item.file');
    gridItems.forEach(item => {
        const nameEl = item.querySelector('.item-name');
        if (nameEl && nameEl.textContent === filePath.split('/').pop()) {
            const img = item.querySelector('.thumbnail');
            if (img && img.dataset.thumbnailPath) {
                // Force reload with cache busting
                const thumbnailUrl = img.dataset.thumbnailPath;
                const newSrc = thumbnailUrl + (thumbnailUrl.includes('?') ? '&' : '?') + '_refresh=' + Date.now();
                img.src = newSrc;
                console.log('Refreshed thumbnail for:', filePath);
            }
        }
    });
}

/**
 * Execute a script action on a file
 * @param {string} scriptType - The type of script to run (crop, remove, single_file, enhance_single)
 * @param {string} filePath - The path to the file to process
 */
function executeScript(scriptType, filePath) {
    if (!filePath) {
        showError("No file path provided");
        return;
    }

    if (currentEventSource) {
        currentEventSource.close();
        currentEventSource = null;
    }

    const url = `/stream/${scriptType}?file_path=${encodeURIComponent(filePath)}`;
    console.log(`Executing ${scriptType} on: ${filePath}`);
    console.log(`Connecting to: ${url}`);

    const eventSource = new EventSource(url);
    currentEventSource = eventSource;

    // Show progress container
    const progressContainer = document.getElementById('progress-container');
    const progressBar = document.getElementById('progress-bar');
    const progressText = document.getElementById('progress-text');

    if (progressContainer) {
        progressContainer.style.display = 'block';
        if (progressBar) {
            progressBar.style.width = '0%';
            progressBar.textContent = '0%';
            progressBar.setAttribute('aria-valuenow', '0');
        }
        if (progressText) {
            progressText.textContent = 'Initializing...';
        }
    }

    // Handle progress messages
    eventSource.onmessage = (event) => {
        const line = event.data.trim();

        // Skip empty keepalive messages
        if (!line) return;

        console.log('Progress:', line);

        // Update progress text with the message
        if (progressText) {
            progressText.textContent = line;
        }

        // Look for completion messages
        if (line.includes('completed') || line.includes('SUCCESS:')) {
            if (progressBar) {
                progressBar.style.width = '100%';
                progressBar.textContent = '100%';
                progressBar.setAttribute('aria-valuenow', '100');
            }
        }
    };

    eventSource.addEventListener("completed", () => {
        console.log('Script completed successfully');
        if (progressText) {
            progressText.textContent = 'Completed successfully!';
        }
        if (progressBar) {
            progressBar.style.width = '100%';
            progressBar.textContent = '100%';
        }

        eventSource.close();
        currentEventSource = null;

        // Refresh the thumbnail for this file
        refreshThumbnail(filePath);

        // Auto-hide progress after 3 seconds
        setTimeout(() => {
            hideProgressIndicator();
        }, 3000);
    });

    eventSource.onerror = () => {
        console.error('Error executing script');
        if (progressText) {
            progressText.textContent = 'Error occurred during processing';
        }

        eventSource.close();
        currentEventSource = null;

        // Auto-hide progress after 5 seconds
        setTimeout(() => {
            hideProgressIndicator();
        }, 5000);
    };
}

// ============================================================================
// INLINE EDIT FUNCTIONALITY
// ============================================================================

/**
 * Initialize edit mode for a CBZ file
 * @param {string} filePath - Path to the CBZ file to edit
 */
function initEditMode(filePath) {
    // Hide the file grid and other collection UI elements
    const librarySection = document.getElementById('library-section');
    if (librarySection) librarySection.style.display = 'none';

    // Show the edit section
    document.getElementById('edit').classList.remove('collapse');

    const container = document.getElementById('editInlineContainer');
    container.innerHTML = `<div class="d-flex justify-content-center my-3">
                                <button class="btn btn-primary" type="button" disabled>
                                    <span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>
                                    Unpacking CBZ File ...
                                </button>
                            </div>`;

    fetch(`/edit?file_path=${encodeURIComponent(filePath)}`)
        .then(response => {
            if (!response.ok) {
                throw new Error("Failed to load edit content.");
            }
            return response.json();
        })
        .then(data => {
            document.getElementById('editInlineContainer').innerHTML = data.modal_body;
            document.getElementById('editInlineFolderName').value = data.folder_name;
            document.getElementById('editInlineZipFilePath').value = data.zip_file_path;
            document.getElementById('editInlineOriginalFilePath').value = data.original_file_path;
            sortInlineEditCards();

            // Setup form submit handler to prevent page navigation
            setupSaveFormHandler();
        })
        .catch(error => {
            container.innerHTML = `<div class="alert alert-danger" role="alert">
                    <strong>Error:</strong> ${error.message}
                </div>`;
            showError(error.message);
        });
}

/**
 * Setup form submit handler for save functionality
 */
function setupSaveFormHandler() {
    const form = document.getElementById('editInlineSaveForm');
    if (!form) return;

    // Remove any existing submit handlers
    const newForm = form.cloneNode(true);
    form.parentNode.replaceChild(newForm, form);

    newForm.addEventListener('submit', function (e) {
        e.preventDefault();

        const formData = new FormData(newForm);
        const data = {
            folder_name: formData.get('folder_name'),
            zip_file_path: formData.get('zip_file_path'),
            original_file_path: formData.get('original_file_path')
        };

        // Show progress indicator
        showProgressIndicator();
        const progressText = document.getElementById('progress-text');
        if (progressText) {
            progressText.textContent = 'Saving CBZ file...';
        }

        fetch('/save', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        })
            .then(response => response.json())
            .then(result => {
                if (result.success) {
                    // Hide edit section and show collection grid
                    document.getElementById('edit').classList.add('collapse');
                    const librarySection = document.getElementById('library-section');
                    if (librarySection) librarySection.style.display = 'block';
                    document.getElementById('file-grid').style.display = 'grid';
                    const paginationControls = document.getElementById('pagination-controls');
                    if (paginationControls && allItems.length > itemsPerPage) {
                        paginationControls.style.display = 'block';
                    }

                    // Clear edit container
                    document.getElementById('editInlineContainer').innerHTML = '';

                    // Refresh the current view to show updated thumbnail (preserve current page)
                    setTimeout(() => {
                        refreshCurrentView(true);
                        hideProgressIndicator();
                    }, 500);
                } else {
                    showError('Error saving file: ' + (result.error || 'Unknown error'));
                    hideProgressIndicator();
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showError('An error occurred while saving the file.');
                hideProgressIndicator();
            });
    });
}

/**
 * Enable inline editing of a filename
 * @param {HTMLElement} element - The filename span element
 */
function enableFilenameEdit(element) {
    console.log("enableFilenameEdit called");
    const input = element.nextElementSibling;
    if (!input) {
        console.error("No adjacent input found for", element);
        return;
    }
    element.classList.add('d-none');
    input.classList.remove('d-none');
    input.focus();
    input.select();

    let renameProcessed = false;

    function processRename(event) {
        if (renameProcessed) return;
        renameProcessed = true;
        performRename(input);
    }

    input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
            event.preventDefault();
            processRename(event);
            input.blur();
        }
    });

    input.addEventListener('blur', function (event) {
        processRename(event);
    }, { once: true });
}

/**
 * Sort inline edit cards by filename
 * Mimics file system sorting: alpha-numeric order with special characters first
 */
function sortInlineEditCards() {
    const container = document.getElementById('editInlineContainer');
    if (!container) return;

    // Get all card elements as an array
    const cards = Array.from(container.children);

    // Regex to check if the filename starts with a letter or a digit
    const alphanumRegex = /^[a-z0-9]/i;

    // Create an Intl.Collator instance for natural (alpha-numeric) sorting
    const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

    cards.sort((a, b) => {
        const inputA = a.querySelector('.filename-input');
        const inputB = b.querySelector('.filename-input');
        const filenameA = inputA ? inputA.value : "";
        const filenameB = inputB ? inputB.value : "";

        // Determine if the filename starts with a letter or digit
        const aIsAlphaNum = alphanumRegex.test(filenameA);
        const bIsAlphaNum = alphanumRegex.test(filenameB);

        // Files starting with special characters should sort before those starting with letters or digits
        if (!aIsAlphaNum && bIsAlphaNum) return -1;
        if (aIsAlphaNum && !bIsAlphaNum) return 1;

        // Otherwise, use natural (alpha-numeric) sort order
        return collator.compare(filenameA, filenameB);
    });

    // Rebuild the container with the sorted cards
    container.innerHTML = '';
    cards.forEach(card => container.appendChild(card));
}

/**
 * Perform rename operation on a file
 * @param {HTMLInputElement} input - The input element containing the new filename
 */
function performRename(input) {
    const newFilename = input.value.trim();
    const folderName = document.getElementById('editInlineFolderName').value;

    // Get the old relative path from data-rel-path attribute (set by edit.py template)
    const oldRelPath = input.dataset.relPath || input.getAttribute('data-rel-path');
    if (!oldRelPath) {
        console.error("No relative path found in input:", input);
        return;
    }

    // Extract just the filename from the relative path for comparison
    const oldFilename = oldRelPath.includes('/')
        ? oldRelPath.substring(oldRelPath.lastIndexOf('/') + 1)
        : oldRelPath;

    // Cancel if the filename hasn't changed
    if (newFilename === oldFilename) {
        input.classList.add('d-none');
        input.previousElementSibling.classList.remove('d-none');
        return;
    }

    // Construct new relative path (preserve subdirectory if any)
    const dirPath = oldRelPath.includes('/')
        ? oldRelPath.substring(0, oldRelPath.lastIndexOf('/'))
        : '';
    const newRelPath = dirPath ? `${dirPath}/${newFilename}` : newFilename;

    const oldPath = `${folderName}/${oldRelPath}`;
    const newPath = `${folderName}/${newRelPath}`;

    console.log("Renaming", oldPath, "to", newPath);

    fetch('/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old: oldPath, new: newPath })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                const span = input.previousElementSibling;
                span.textContent = newFilename;
                // Update data-rel-path with the new relative path
                span.setAttribute('data-rel-path', newRelPath);
                input.setAttribute('data-rel-path', newRelPath);
                span.classList.remove('d-none');
                input.classList.add('d-none');
                // After updating the filename, re-sort the inline edit cards.
                sortInlineEditCards();
            } else {
                showError('Error renaming file: ' + data.error);
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showError('An error occurred while renaming the file.');
        });
}

/**
 * Delete an image card from the CBZ
 * @param {HTMLElement} buttonElement - The delete button element
 */
function deleteCardImage(buttonElement) {
    const colElement = buttonElement.closest('.col');
    if (!colElement) {
        console.error("Unable to locate column container for deletion.");
        return;
    }
    const span = colElement.querySelector('.editable-filename');
    if (!span) {
        console.error("No file reference found in column:", colElement);
        return;
    }
    const folderName = document.getElementById('editInlineFolderName').value;
    if (!folderName) {
        console.error("Folder name not found in #editInlineFolderName.");
        return;
    }
    // Get the relative path from data-rel-path attribute (set by edit.py template)
    const relPath = span.dataset.relPath || span.getAttribute('data-rel-path');
    if (!relPath) {
        console.error("No relative path found in span:", span);
        return;
    }
    const fullPath = `${folderName}/${relPath}`;

    fetch('/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: fullPath })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                colElement.classList.add("fade-out");
                setTimeout(() => {
                    colElement.remove();
                }, 300);
            } else {
                showError("Error deleting image: " + data.error);
            }
        })
        .catch(error => {
            console.error("Error:", error);
            showError("An error occurred while deleting the image.");
        });
}

/**
 * Crop left portion of image
 * @param {HTMLElement} buttonElement - The crop button element
 */
function cropImageLeft(buttonElement) {
    processCropImage(buttonElement, 'left');
}

/**
 * Crop center of image (splits into two)
 * @param {HTMLElement} buttonElement - The crop button element
 */
function cropImageCenter(buttonElement) {
    processCropImage(buttonElement, 'center');
}

/**
 * Crop right portion of image
 * @param {HTMLElement} buttonElement - The crop button element
 */
function cropImageRight(buttonElement) {
    processCropImage(buttonElement, 'right');
}

/**
 * Process crop operation
 * @param {HTMLElement} buttonElement - The crop button element
 * @param {string} cropType - Type of crop: 'left', 'center', or 'right'
 */
function processCropImage(buttonElement, cropType) {
    const colElement = buttonElement.closest('.col');
    if (!colElement) {
        console.error("Unable to locate column container.");
        return;
    }

    const span = colElement.querySelector('.editable-filename');
    if (!span) {
        console.error("No file reference found in column:", colElement);
        return;
    }

    const folderElement = document.getElementById('editInlineFolderName');
    if (!folderElement) {
        console.error("Folder name input element not found.");
        return;
    }

    const folderName = folderElement.value;
    if (!folderName) {
        console.error("Folder name is empty.");
        return;
    }

    // Get the relative path from data-rel-path attribute (set by edit.py template)
    const relPath = span.dataset.relPath || span.getAttribute('data-rel-path');
    if (!relPath) {
        console.error("No relative path found in span:", span);
        return;
    }

    const fullPath = `${folderName}/${relPath}`;

    fetch('/crop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: fullPath, cropType: cropType })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                const container = document.getElementById('editInlineContainer');

                // Remove the original card from the DOM
                colElement.remove();

                if (data.html) {
                    // Center crop returns full HTML cards
                    container.insertAdjacentHTML('beforeend', data.html);
                } else {
                    // Left/right crop returns single image + base64
                    const newCardHTML = generateCardHTML(data.newImagePath, data.newImageData);
                    container.insertAdjacentHTML('beforeend', newCardHTML);
                }

                // After insertion, sort the updated cards
                sortInlineEditCards();

            } else {
                showError("Error cropping image: " + data.error);
            }
        })
        .catch(error => {
            console.error("Error:", error);
            showError("An error occurred while cropping the image.");
        });
}

/**
 * Generate HTML for an image card
 * @param {string} imagePath - Path to the image
 * @param {string} imageData - Base64 encoded image data
 * @returns {string} HTML string for the card
 */
function generateCardHTML(imagePath, imageData) {
    // Extract filename_only from the full path for sorting and display purposes
    const filenameOnly = imagePath.split('/').pop();
    return `
    <div class="col">
        <div class="card h-100 shadow-sm">
            <div class="row g-0">
                <div class="col-3">
                    <img src="${imageData}" class="img-fluid rounded-start object-fit-scale border rounded" alt="${filenameOnly}">
                </div>
                <div class="col-9">
                    <div class="card-body">
                        <p class="card-text small">
                            <span class="editable-filename" data-rel-path="${imagePath}" onclick="enableFilenameEdit(this)">
                                ${filenameOnly}
                            </span>
                            <input type="text" class="form-control d-none filename-input form-control-sm" value="${filenameOnly}" data-rel-path="${imagePath}">
                        </p>
                        <div class="d-flex justify-content-end">
                            <div class="btn-group" role="group" aria-label="Basic example">
                                <button type="button" class="btn btn-outline-primary btn-sm" onclick="cropImageFreeForm(this)" title="Free Form Crop">
                                    <i class="bi bi-crop"></i> Free
                                </button>
                                <button type="button" class="btn btn-outline-secondary btn-sm" onclick="cropImageLeft(this)" title="Crop Image Left">
                                    <i class="bi bi-arrow-bar-left"></i> Left
                                </button>
                                <button type="button" class="btn btn-outline-secondary" onclick="cropImageCenter(this)" title="Crop Image Center">Middle</button>
                                <button type="button" class="btn btn-outline-secondary btn-sm" onclick="cropImageRight(this)" title="Crop Image Right">
                                    Right <i class="bi bi-arrow-bar-right"></i>
                                </button>
                                <button type="button" class="btn btn-outline-danger btn-sm" onclick="deleteCardImage(this)">
                                    <i class="bi bi-trash"></i>
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>`;
}

// ============================================================================
// FREE-FORM CROP FUNCTIONALITY
// ============================================================================

// Crop state management
let cropData = {
    imagePath: null,
    startX: 0,
    startY: 0,
    endX: 0,
    endY: 0,
    isDragging: false,
    imageElement: null,
    colElement: null,
    isPanning: false,
    panStartX: 0,
    panStartY: 0,
    selectionLeft: 0,
    selectionTop: 0,
    spacebarPressed: false,
    wasDrawingBeforePan: false,
    savedWidth: 0,
    savedHeight: 0
};

/**
 * Open free-form crop modal for an image
 * @param {HTMLElement} buttonElement - The free crop button element
 */
function cropImageFreeForm(buttonElement) {
    const colElement = buttonElement.closest('.col');
    if (!colElement) {
        console.error("Unable to locate column container.");
        return;
    }

    const span = colElement.querySelector('.editable-filename');
    if (!span) {
        console.error("No file reference found in column:", colElement);
        return;
    }

    const folderElement = document.getElementById('editInlineFolderName');
    if (!folderElement) {
        console.error("Folder name input element not found.");
        return;
    }

    const folderName = folderElement.value;
    if (!folderName) {
        console.error("Folder name is empty.");
        return;
    }

    // Get the relative path from data-rel-path attribute (set by edit.py template)
    const relPath = span.dataset.relPath || span.getAttribute('data-rel-path');
    if (!relPath) {
        console.error("No relative path found in span:", span);
        return;
    }

    const fullPath = `${folderName}/${relPath}`;

    // Store the data for later use
    cropData.imagePath = fullPath;
    cropData.colElement = colElement;

    // Get the image source from the card
    const cardImg = colElement.querySelector('img');
    if (!cardImg) {
        console.error("No image found in card");
        return;
    }

    // Load the full-size image into the modal
    const cropImage = document.getElementById('cropImage');
    const cropModal = new bootstrap.Modal(document.getElementById('freeFormCropModal'));

    // Reset crop selection
    const cropSelection = document.getElementById('cropSelection');
    cropSelection.style.display = 'none';
    document.getElementById('confirmCropBtn').disabled = true;

    // Load image from the server
    fetch('/get-image-data', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: fullPath })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                cropImage.src = data.imageData;
                cropImage.onload = function () {
                    setupCropHandlers();
                    cropModal.show();
                };
            } else {
                showError("Error loading image: " + data.error);
            }
        })
        .catch(error => {
            console.error("Error:", error);
            showError("An error occurred while loading the image.");
        });
}

/**
 * Setup event handlers for crop modal
 */
function setupCropHandlers() {
    const cropImage = document.getElementById('cropImage');
    const cropSelection = document.getElementById('cropSelection');
    const confirmBtn = document.getElementById('confirmCropBtn');
    const cropContainer = document.getElementById('cropImageContainer');

    // Remove any existing event listeners by cloning the element
    const newCropImage = cropImage.cloneNode(true);
    cropImage.parentNode.replaceChild(newCropImage, cropImage);
    cropData.imageElement = newCropImage;

    // Add keyboard listeners for spacebar
    document.addEventListener('keydown', handleKeyDown);
    document.addEventListener('keyup', handleKeyUp);

    // Attach mouse events to the container for better coverage
    cropContainer.addEventListener('mousedown', startCrop);
    document.addEventListener('mousemove', updateCrop);
    document.addEventListener('mouseup', endCrop);

    // Add mousedown listener to selection box for panning
    cropSelection.addEventListener('mousedown', function (e) {
        if (cropData.spacebarPressed) {
            startPan(e);
        }
    });

    function handleKeyDown(e) {
        if (e.key === ' ' || e.code === 'Space') {
            e.preventDefault();

            // Don't change mode if already in spacebar mode
            if (cropData.spacebarPressed) return;

            cropData.spacebarPressed = true;
            cropContainer.style.cursor = 'move';
            console.log('Spacebar pressed - switching to pan mode');

            // If we're currently drawing, pause drawing and switch to panning
            if (cropData.isDragging) {
                console.log('Pausing draw mode, entering pan mode');
                cropData.wasDrawingBeforePan = true;
                cropData.isDragging = false;
                cropData.isPanning = false; // Will start on next mouse move

                // Save current selection dimensions
                cropData.savedWidth = Math.abs(cropData.endX - cropData.startX);
                cropData.savedHeight = Math.abs(cropData.endY - cropData.startY);
            }
        }
    }

    function handleKeyUp(e) {
        if (e.key === ' ' || e.code === 'Space') {
            e.preventDefault();
            cropData.spacebarPressed = false;
            cropContainer.style.cursor = 'crosshair';
            console.log('Spacebar released - back to draw mode');

            // If we were panning, stop panning
            if (cropData.isPanning) {
                cropData.isPanning = false;
                console.log('Stopped panning');
            }

            // If we were drawing before pan, resume drawing
            if (cropData.wasDrawingBeforePan) {
                console.log('Resuming draw mode');
                cropData.isDragging = true;
                cropData.wasDrawingBeforePan = false;
            }
        }
    }

    function startPan(e) {
        e.preventDefault();
        e.stopPropagation();

        console.log('Start pan - spacebar pressed:', cropData.spacebarPressed);

        cropData.isPanning = true;
        cropData.panStartX = e.clientX;
        cropData.panStartY = e.clientY;

        // Get current position
        cropData.selectionLeft = parseInt(cropSelection.style.left) || 0;
        cropData.selectionTop = parseInt(cropSelection.style.top) || 0;

        document.addEventListener('mousemove', updatePan);
        document.addEventListener('mouseup', endPan);
    }

    function updatePan(e) {
        if (!cropData.isPanning) return;

        e.preventDefault();
        const deltaX = e.clientX - cropData.panStartX;
        const deltaY = e.clientY - cropData.panStartY;

        const newLeft = cropData.selectionLeft + deltaX;
        const newTop = cropData.selectionTop + deltaY;

        // Get container bounds (not image bounds)
        const containerRect = cropContainer.getBoundingClientRect();
        const selectionWidth = parseInt(cropSelection.style.width) || 0;
        const selectionHeight = parseInt(cropSelection.style.height) || 0;

        // Constrain to container bounds
        const constrainedLeft = Math.max(0, Math.min(newLeft, containerRect.width - selectionWidth));
        const constrainedTop = Math.max(0, Math.min(newTop, containerRect.height - selectionHeight));

        cropSelection.style.left = constrainedLeft + 'px';
        cropSelection.style.top = constrainedTop + 'px';

        console.log('Update pan - left:', constrainedLeft, 'top:', constrainedTop);

        // Update crop data coordinates
        cropData.startX = constrainedLeft;
        cropData.startY = constrainedTop;
        cropData.endX = constrainedLeft + selectionWidth;
        cropData.endY = constrainedTop + selectionHeight;
    }

    function endPan(e) {
        cropData.isPanning = false;
        document.removeEventListener('mousemove', updatePan);
        document.removeEventListener('mouseup', endPan);
        console.log('End pan');
    }

    function startCrop(e) {
        // Check if clicking on the selection box with spacebar pressed
        if (e.target === cropSelection && cropData.spacebarPressed) {
            console.log('Starting pan from selection box click');
            startPan(e);
            return;
        }

        // If spacebar is pressed and we have a selection, start panning
        if (cropData.spacebarPressed && cropSelection.style.display !== 'none') {
            console.log('Starting pan - spacebar mode');
            startPan(e);
            return;
        }

        e.preventDefault();
        cropData.isDragging = true;

        const imageRect = newCropImage.getBoundingClientRect();
        const containerRect = newCropImage.parentElement.getBoundingClientRect();

        // Calculate image offset within container
        const imageOffsetX = imageRect.left - containerRect.left;
        const imageOffsetY = imageRect.top - containerRect.top;

        // Calculate position relative to the image container
        let startX = e.clientX - containerRect.left;
        let startY = e.clientY - containerRect.top;

        // Constrain starting position to image bounds
        startX = Math.max(imageOffsetX, Math.min(startX, imageOffsetX + imageRect.width));
        startY = Math.max(imageOffsetY, Math.min(startY, imageOffsetY + imageRect.height));

        cropData.startX = startX;
        cropData.startY = startY;

        console.log('Start crop at:', cropData.startX, cropData.startY);

        cropSelection.style.left = cropData.startX + 'px';
        cropSelection.style.top = cropData.startY + 'px';
        cropSelection.style.width = '0px';
        cropSelection.style.height = '0px';
        cropSelection.style.display = 'block';

        confirmBtn.disabled = true;
    }

    function updateCrop(e) {
        // Handle panning mode if spacebar is pressed during dragging
        if (cropData.spacebarPressed && cropSelection.style.display !== 'none') {
            if (!cropData.isPanning) {
                // Start panning
                cropData.isPanning = true;
                cropData.panStartX = e.clientX;
                cropData.panStartY = e.clientY;
                cropData.selectionLeft = parseInt(cropSelection.style.left) || 0;
                cropData.selectionTop = parseInt(cropSelection.style.top) || 0;
                console.log('Started panning during drag');
            }

            // Pan the selection
            e.preventDefault();
            const deltaX = e.clientX - cropData.panStartX;
            const deltaY = e.clientY - cropData.panStartY;

            const newLeft = cropData.selectionLeft + deltaX;
            const newTop = cropData.selectionTop + deltaY;

            const imageRect = newCropImage.getBoundingClientRect();
            const containerRect = cropContainer.getBoundingClientRect();

            // Calculate image offset within container
            const imageOffsetX = imageRect.left - containerRect.left;
            const imageOffsetY = imageRect.top - containerRect.top;

            const selectionWidth = parseInt(cropSelection.style.width) || 0;
            const selectionHeight = parseInt(cropSelection.style.height) || 0;

            // Constrain to image bounds
            const constrainedLeft = Math.max(imageOffsetX, Math.min(newLeft, imageOffsetX + imageRect.width - selectionWidth));
            const constrainedTop = Math.max(imageOffsetY, Math.min(newTop, imageOffsetY + imageRect.height - selectionHeight));

            cropSelection.style.left = constrainedLeft + 'px';
            cropSelection.style.top = constrainedTop + 'px';

            // Update crop data coordinates (relative to container)
            cropData.startX = constrainedLeft;
            cropData.startY = constrainedTop;
            cropData.endX = constrainedLeft + selectionWidth;
            cropData.endY = constrainedTop + selectionHeight;

            return;
        }

        if (!cropData.isDragging) return;

        e.preventDefault();

        // Get both container and image bounds
        const containerRect = newCropImage.parentElement.getBoundingClientRect();
        const imageRect = newCropImage.getBoundingClientRect();

        // Calculate image offset within container
        const imageOffsetX = imageRect.left - containerRect.left;
        const imageOffsetY = imageRect.top - containerRect.top;

        // Get current mouse position relative to container
        let currentX = e.clientX - containerRect.left;
        let currentY = e.clientY - containerRect.top;

        // Constrain current position to image bounds
        currentX = Math.max(imageOffsetX, Math.min(currentX, imageOffsetX + imageRect.width));
        currentY = Math.max(imageOffsetY, Math.min(currentY, imageOffsetY + imageRect.height));

        let width = currentX - cropData.startX;
        let height = currentY - cropData.startY;

        // Apply aspect ratio constraint if Shift is pressed
        // Comic book aspect ratio: 53:82 (width:height) ≈ 0.646
        if (e.shiftKey) {
            const aspectRatio = 53 / 82;

            // Determine which dimension to constrain based on which is larger
            if (Math.abs(width / height) > aspectRatio) {
                // Width is too large, constrain it
                width = height * aspectRatio;
                currentX = cropData.startX + width;
                // Re-constrain after aspect ratio adjustment
                if (width > 0) {
                    currentX = Math.min(currentX, imageOffsetX + imageRect.width);
                    width = currentX - cropData.startX;
                } else {
                    currentX = Math.max(currentX, imageOffsetX);
                    width = currentX - cropData.startX;
                }
            } else {
                // Height is too large, constrain it
                height = width / aspectRatio;
                currentY = cropData.startY + height;
                // Re-constrain after aspect ratio adjustment
                if (height > 0) {
                    currentY = Math.min(currentY, imageOffsetY + imageRect.height);
                    height = currentY - cropData.startY;
                } else {
                    currentY = Math.max(currentY, imageOffsetY);
                    height = currentY - cropData.startY;
                }
            }
        }

        // Handle negative width/height (dragging in different directions)
        // Constrain the selection box to stay within image bounds
        let finalLeft, finalTop, finalWidth, finalHeight;

        if (width < 0) {
            finalLeft = Math.max(imageOffsetX, cropData.startX + width);
            finalWidth = cropData.startX - finalLeft;
            cropData.endX = finalLeft;
        } else {
            finalLeft = cropData.startX;
            finalWidth = Math.min(width, (imageOffsetX + imageRect.width) - cropData.startX);
            cropData.endX = finalLeft + finalWidth;
        }

        if (height < 0) {
            finalTop = Math.max(imageOffsetY, cropData.startY + height);
            finalHeight = cropData.startY - finalTop;
            cropData.endY = finalTop;
        } else {
            finalTop = cropData.startY;
            finalHeight = Math.min(height, (imageOffsetY + imageRect.height) - cropData.startY);
            cropData.endY = finalTop + finalHeight;
        }

        // Apply the constrained values to the selection box
        cropSelection.style.left = finalLeft + 'px';
        cropSelection.style.top = finalTop + 'px';
        cropSelection.style.width = finalWidth + 'px';
        cropSelection.style.height = finalHeight + 'px';
    }

    function endCrop(e) {
        if (!cropData.isDragging) return;

        cropData.isDragging = false;

        const rect = newCropImage.getBoundingClientRect();
        const currentX = e.clientX - rect.left;
        const currentY = e.clientY - rect.top;

        cropData.endX = currentX;
        cropData.endY = currentY;

        // Enable confirm button if a valid selection was made
        const width = Math.abs(cropData.endX - cropData.startX);
        const height = Math.abs(cropData.endY - cropData.startY);

        if (width > 10 && height > 10) {
            confirmBtn.disabled = false;
        } else {
            cropSelection.style.display = 'none';
        }
    }

    // Clean up all event listeners when modal is closed
    const modal = document.getElementById('freeFormCropModal');
    modal.addEventListener('hidden.bs.modal', function () {
        document.removeEventListener('keydown', handleKeyDown);
        document.removeEventListener('keyup', handleKeyUp);
        document.removeEventListener('mousemove', updateCrop);
        document.removeEventListener('mouseup', endCrop);
        cropContainer.removeEventListener('mousedown', startCrop);
    }, { once: true });
}

/**
 * Confirm and execute free-form crop
 */
function confirmFreeFormCrop() {
    const cropImage = document.getElementById('cropImage');
    const cropContainer = document.getElementById('cropImageContainer');
    const imageRect = cropImage.getBoundingClientRect();
    const containerRect = cropContainer.getBoundingClientRect();

    // Calculate image offset within container
    const imageOffsetX = imageRect.left - containerRect.left;
    const imageOffsetY = imageRect.top - containerRect.top;

    // Calculate the scale factor between displayed image and actual image
    const scaleX = cropImage.naturalWidth / cropImage.width;
    const scaleY = cropImage.naturalHeight / cropImage.height;

    // Get the crop coordinates relative to the container
    const displayX = Math.min(cropData.startX, cropData.endX);
    const displayY = Math.min(cropData.startY, cropData.endY);
    const displayWidth = Math.abs(cropData.endX - cropData.startX);
    const displayHeight = Math.abs(cropData.endY - cropData.startY);

    // Convert to coordinates relative to the image (subtract image offset)
    const imageRelativeX = displayX - imageOffsetX;
    const imageRelativeY = displayY - imageOffsetY;

    // Convert to actual image coordinates
    let actualX = imageRelativeX * scaleX;
    let actualY = imageRelativeY * scaleY;
    let actualWidth = displayWidth * scaleX;
    let actualHeight = displayHeight * scaleY;

    // Clamp coordinates to ensure they don't exceed actual image dimensions
    actualX = Math.max(0, Math.min(actualX, cropImage.naturalWidth));
    actualY = Math.max(0, Math.min(actualY, cropImage.naturalHeight));
    actualWidth = Math.min(actualWidth, cropImage.naturalWidth - actualX);
    actualHeight = Math.min(actualHeight, cropImage.naturalHeight - actualY);

    console.log('Image offset:', { imageOffsetX, imageOffsetY });
    console.log('Display coords:', { displayX, displayY, displayWidth, displayHeight });
    console.log('Image relative coords:', { imageRelativeX, imageRelativeY });
    console.log('Natural image size:', { width: cropImage.naturalWidth, height: cropImage.naturalHeight });
    console.log('Actual crop coordinates:', { x: actualX, y: actualY, width: actualWidth, height: actualHeight });

    // Send the crop request
    fetch('/crop-freeform', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            target: cropData.imagePath,
            x: actualX,
            y: actualY,
            width: actualWidth,
            height: actualHeight
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Close the modal
                const modalElement = document.getElementById('freeFormCropModal');
                const modalInstance = bootstrap.Modal.getInstance(modalElement);
                modalInstance.hide();

                // Update the cropped image in the existing card
                const cardImg = cropData.colElement.querySelector('img');
                if (cardImg) {
                    cardImg.src = data.newImageData;
                }

                // Add the backup image as a new card
                if (data.backupImagePath && data.backupImageData) {
                    const container = document.getElementById('editInlineContainer');
                    const newCardHTML = generateCardHTML(data.backupImagePath, data.backupImageData);
                    container.insertAdjacentHTML('beforeend', newCardHTML);

                    // Sort the cards after adding the new one
                    sortInlineEditCards();
                }

                showError("Free form crop completed successfully!");
            } else {
                showError("Error cropping image: " + data.error);
            }
        })
        .catch(error => {
            console.error("Error:", error);
            showError("An error occurred while cropping the image.");
        });
}
// ============================================================================
// MODAL-BASED EDIT FUNCTIONALITY
// ============================================================================

/**
 * Initialize edit mode - opens modal and loads CBZ contents
 * @param {string} filePath - Path to the CBZ file to edit
 */
function initEditMode(filePath) {
    // Store the file path for later use when saving
    currentEditFilePath = filePath;

    // Open the edit modal
    const editModal = new bootstrap.Modal(document.getElementById('editCBZModal'));
    const container = document.getElementById('editInlineContainer');

    // Show loading spinner
    container.innerHTML = `<div class="d-flex justify-content-center my-3">
                                <button class="btn btn-primary" type="button" disabled>
                                    <span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>
                                    Unpacking CBZ File ...
                                </button>
                            </div>`;

    editModal.show();

    // Update modal title with filename
    const filename = filePath.split('/').pop().split('\\').pop();
    document.getElementById('editCBZModalLabel').textContent = `Editing CBZ File | ${filename}`;

    // Setup drag-drop upload zone
    setupEditModalDropZone();

    // Load CBZ contents
    fetch(`/edit?file_path=${encodeURIComponent(filePath)}`)
        .then(response => {
            if (!response.ok) {
                throw new Error("Failed to load edit content.");
            }
            return response.json();
        })
        .then(data => {
            document.getElementById('editInlineContainer').innerHTML = data.modal_body;
            document.getElementById('editInlineFolderName').value = data.folder_name;
            document.getElementById('editInlineZipFilePath').value = data.zip_file_path;
            document.getElementById('editInlineOriginalFilePath').value = data.original_file_path;
            sortInlineEditCards();
        })
        .catch(error => {
            container.innerHTML = `<div class="alert alert-danger" role="alert">
                    <strong>Error:</strong> ${error.message}
                </div>`;
            showError(error.message);
        });
}

/**
 * Save the edited CBZ file - sends form data and closes modal
 */
function saveEditedCBZ() {
    const form = document.getElementById('editInlineSaveForm');
    if (!form) {
        showError('Form not found');
        return;
    }

    // Show progress indicator
    showProgressIndicator();
    const progressText = document.getElementById('progress-text');
    if (progressText) {
        progressText.textContent = 'Saving CBZ file...';
    }

    // Create FormData from the form (sends as form data, not JSON)
    const formData = new FormData(form);

    fetch('/save', {
        method: 'POST',
        body: formData  // Send as form data, not JSON
    })
        .then(response => response.json())
        .then(result => {
            if (result.success) {
                // Close the modal
                const modalElement = document.getElementById('editCBZModal');
                const modalInstance = bootstrap.Modal.getInstance(modalElement);
                if (modalInstance) {
                    modalInstance.hide();
                }

                // Clear edit container
                document.getElementById('editInlineContainer').innerHTML = '';

                // Refresh only the thumbnail for this file (like Crop Cover does)
                setTimeout(() => {
                    if (currentEditFilePath) {
                        refreshThumbnail(currentEditFilePath);
                    }
                    hideProgressIndicator();
                }, 500);
            } else {
                showError('Error saving file: ' + (result.error || 'Unknown error'));
                hideProgressIndicator();
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showError('An error occurred while saving the file.');
            hideProgressIndicator();
        });
}

/**
 * Setup drag-drop upload for the CBZ edit modal
 */
function setupEditModalDropZone() {
    const modal = document.getElementById('editCBZModal');
    const modalBody = modal?.querySelector('.modal-body');
    if (!modalBody) return;

    // Skip if already setup
    if (modalBody.dataset.dropzoneSetup) return;
    modalBody.dataset.dropzoneSetup = 'true';

    // Prevent default drag behaviors
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        modalBody.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
        });
    });

    // Highlight drop zone on drag enter/over
    ['dragenter', 'dragover'].forEach(eventName => {
        modalBody.addEventListener(eventName, () => {
            modalBody.classList.add('drag-over');
        });
    });

    // Remove highlight on drag leave/drop
    ['dragleave', 'drop'].forEach(eventName => {
        modalBody.addEventListener(eventName, () => {
            modalBody.classList.remove('drag-over');
        });
    });

    // Handle dropped files
    modalBody.addEventListener('drop', (e) => {
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            handleEditModalUpload(files);
        }
    });
}

/**
 * Handle file upload in the edit modal
 * @param {FileList} files - Files to upload
 */
function handleEditModalUpload(files) {
    const folderName = document.getElementById('editInlineFolderName')?.value;
    if (!folderName) {
        showError('Cannot upload: No target folder');
        return;
    }

    // Filter to allowed image types only
    const allowedExtensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'];
    const validFiles = Array.from(files).filter(file => {
        const ext = '.' + file.name.split('.').pop().toLowerCase();
        return allowedExtensions.includes(ext);
    });

    if (validFiles.length === 0) {
        showError('No valid image files. Allowed: ' + allowedExtensions.join(', '));
        return;
    }

    // Show upload toast
    showUploadToast(validFiles.length);

    // Prepare FormData
    const formData = new FormData();
    formData.append('target_dir', folderName);
    validFiles.forEach(file => {
        formData.append('files', file);
    });

    // Upload files
    fetch('/upload-to-folder', {
        method: 'POST',
        body: formData
    })
        .then(response => response.json())
        .then(data => {
            hideUploadToast();

            if (data.success && data.uploaded.length > 0) {
                showSuccess(`Uploaded ${data.uploaded.length} file(s)`);

                // Add cards for each uploaded file
                data.uploaded.forEach(file => {
                    addUploadedFileCard(file.path, file.name);
                });
            } else if (data.total_skipped > 0) {
                showError(`Skipped ${data.total_skipped} file(s): invalid type`);
            } else {
                showError('Upload failed: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            hideUploadToast();
            console.error('Upload error:', error);
            showError('Upload failed: ' + error.message);
        });
}

/**
 * Add a card for an uploaded file to the edit container
 * @param {string} filePath - Full path to uploaded file
 * @param {string} fileName - Name of the file
 */
function addUploadedFileCard(filePath, fileName) {
    const container = document.getElementById('editInlineContainer');
    if (!container) return;

    // Fetch image data as base64 for the card
    fetch('/get-image-data', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: filePath })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Generate card HTML using existing function
                const cardHTML = generateCardHTML(fileName, data.imageData);
                container.insertAdjacentHTML('beforeend', cardHTML);

                // Re-sort cards
                sortInlineEditCards();
            } else {
                showError('Failed to load uploaded image: ' + (data.error || 'Unknown'));
            }
        })
        .catch(error => {
            console.error('Error loading uploaded image:', error);
            showError('Failed to load uploaded image');
        });
}

/**
 * Show upload progress toast
 * @param {number} fileCount - Number of files being uploaded
 */
function showUploadToast(fileCount) {
    // Remove existing toast if any
    hideUploadToast();

    const toast = document.createElement('div');
    toast.id = 'upload-progress-toast';
    toast.className = 'toast show position-fixed';
    toast.style.cssText = 'bottom: 20px; right: 20px; z-index: 9999;';
    toast.innerHTML = `
        <div class="toast-header bg-primary text-white">
            <strong class="me-auto">Uploading</strong>
        </div>
        <div class="toast-body d-flex align-items-center">
            <div class="spinner-border spinner-border-sm me-2" role="status"></div>
            <span>Uploading ${fileCount} file(s)...</span>
        </div>
    `;
    document.body.appendChild(toast);
}

/**
 * Hide upload progress toast
 */
function hideUploadToast() {
    const toast = document.getElementById('upload-progress-toast');
    if (toast) toast.remove();
}

// ============================================================================
// EDIT FILE FUNCTIONALITY
// ============================================================================

let currentEditFilePath = null; // Store the file path being edited

// ============================================================================
// COMIC READER FUNCTIONALITY
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
    currentComicSiblings = allItems.filter(item => {
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
                showError('Failed to load comic: ' + (comicData.error || 'Unknown error'));
                closeComicReader();
            }
        })
        .catch(error => {
            console.error('Error loading comic:', error);
            showError('An error occurred while loading the comic.');
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
        // Removed bullet pagination - using custom dropdown instead
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

// Zoom step levels: 3 increments from minRatio (1) to maxRatio (3)
const ZOOM_STEPS = [1, 1.67, 2.33, 3];

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
            // The zoom module will handle scrolling the zoomed image
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
                readIssuesSet.add(currentComicPath);
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
            readIssuesSet.add(currentComicPath);
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

// Setup close button handler
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
                    readIssuesSet.add(currentComicPath);
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

    // Add event listener for Update XML confirm button
    const updateXmlBtn = document.getElementById('updateXmlConfirmBtn');
    if (updateXmlBtn) updateXmlBtn.addEventListener('click', submitUpdateXml);

    // Add event listener for Update XML field dropdown change
    const updateXmlFieldSelect = document.getElementById('updateXmlField');
    if (updateXmlFieldSelect) updateXmlFieldSelect.addEventListener('change', updateXmlFieldChanged);
});

// ============================================================================
// DELETE FILE FUNCTIONALITY
// ============================================================================

let fileToDelete = null; // Store file to be deleted

/**
 * Open the Set Read Date modal
 * @param {string} comicPath - Path to the comic file
 * @param {boolean} isRead - Whether the comic is already marked as read
 */
function openSetReadDateModal(comicPath, isRead) {
    document.getElementById('readDateComicPath').value = comicPath;
    document.getElementById('setReadDateModalTitle').textContent =
        isRead ? 'Update Read Date' : 'Set Read Date';

    // Default to today
    const today = new Date().toISOString().split('T')[0];
    document.getElementById('readDateInput').value = today;

    new bootstrap.Modal(document.getElementById('setReadDateModal')).show();
}

/**
 * Submit the selected read date to the API
 */
function submitReadDate() {
    const comicPath = document.getElementById('readDateComicPath').value;
    const dateValue = document.getElementById('readDateInput').value;

    if (!dateValue) {
        showError('Please select a date');
        return;
    }

    // Combine selected date with current time
    const now = new Date();
    const timeStr = now.toTimeString().split(' ')[0];
    const readAt = `${dateValue}T${timeStr}`;

    fetch('/api/mark-comic-read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: comicPath, read_at: readAt })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                bootstrap.Modal.getInstance(document.getElementById('setReadDateModal')).hide();
                // Update UI - add to readIssuesSet, update icon
                readIssuesSet.add(comicPath);
                updateReadIcon(comicPath, true);
                showSuccess('Read date saved successfully');
            } else {
                showError(data.error || 'Failed to save read date');
            }
        })
        .catch(err => {
            showError('Error saving read date: ' + err.message);
        });
}

/**
 * Update the read icon for a specific comic path
 * @param {string} comicPath - Path to the comic
 * @param {boolean} isRead - Whether to show as read
 */
function updateReadIcon(comicPath, isRead) {
    // Find the grid item with this path and update its read icon
    const gridItems = document.querySelectorAll('.grid-item');
    gridItems.forEach(item => {
        if (item.dataset.path === comicPath) {
            const readIcon = item.querySelector('.read-icon');
            if (readIcon) {
                if (isRead) {
                    readIcon.classList.remove('bi-book');
                    readIcon.classList.add('bi-book-fill');
                } else {
                    readIcon.classList.remove('bi-book-fill');
                    readIcon.classList.add('bi-book');
                }
            }
        }
    });
}

/**
 * Show delete confirmation modal with file details
 * @param {Object} item - The item object containing file details
 */
function showDeleteConfirmation(item) {
    fileToDelete = item;

    // Populate modal with file details
    document.getElementById('deleteFileName').textContent = item.name;

    // Show size only for files, show "Folder" for folders
    if (item.type === 'folder') {
        document.getElementById('deleteFileSize').textContent = 'Folder';
    } else {
        document.getElementById('deleteFileSize').textContent = formatFileSize(item.size);
    }

    document.getElementById('deleteFilePath').textContent = item.path;

    // Show the modal
    const deleteModal = new bootstrap.Modal(document.getElementById('deleteConfirmModal'));
    deleteModal.show();
}

/**
 * Confirm and execute file deletion
 */
function confirmDeleteFile() {
    if (!fileToDelete) {
        showError('No file selected for deletion');
        return;
    }

    // Show progress indicator
    showProgressIndicator();
    const progressText = document.getElementById('progress-text');
    if (progressText) {
        progressText.textContent = 'Deleting file...';
    }

    // Call the delete API
    fetch('/api/delete-file', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: fileToDelete.path })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Close the modal
                const modalElement = document.getElementById('deleteConfirmModal');
                const modalInstance = bootstrap.Modal.getInstance(modalElement);
                if (modalInstance) {
                    modalInstance.hide();
                }

                // Remove the file from allItems array
                const index = allItems.findIndex(item => item.path === fileToDelete.path);
                if (index !== -1) {
                    allItems.splice(index, 1);
                }

                // Re-render the current page
                renderPage();

                // Show success message
                hideProgressIndicator();
                showSuccess('File deleted successfully');

                // Clear the fileToDelete reference
                fileToDelete = null;
            } else {
                hideProgressIndicator();
                showError('Error deleting file: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            hideProgressIndicator();
            showError('An error occurred while deleting the file.');
        });
}

/**
 * Load favorite publishers for the dashboard swiper
 */
async function loadFavoritePublishers() {
    const swiper = document.querySelector('#favoritesSwiper .swiper-wrapper');
    if (!swiper) return;

    try {
        // Fetch favorites and root directory data in parallel
        const [favResponse, browseResponse] = await Promise.all([
            fetch('/api/favorites/publishers'),
            fetch('/api/browse?path=/data')
        ]);

        const favData = await favResponse.json();
        const browseData = await browseResponse.json();

        // Store favorite paths globally for grid item sync
        window.favoritePaths = new Set(
            favData.success && favData.publishers
                ? favData.publishers.map(p => p.publisher_path)
                : []
        );

        if (!favData.success || !favData.publishers.length) {
            // Show empty state
            swiper.innerHTML = `
                <div class="swiper-slide">
                    <div class="dashboard-card text-center p-4">
                        <i class="bi bi-bookmark-heart text-muted" style="font-size: 3rem;"></i>
                        <p class="text-muted mt-2">No favorites yet</p>
                    </div>
                </div>
            `;
            return;
        }

        // Create a map of publisher paths to their info from browse data
        const publisherMap = {};
        if (browseData.directories) {
            browseData.directories.forEach(dir => {
                const fullPath = `/data/${dir.name}`;
                publisherMap[fullPath] = {
                    name: dir.name,
                    hasThumbnail: dir.has_thumbnail || false,
                    thumbnailUrl: dir.thumbnail_url || null,
                    folderCount: dir.folder_count || 0,
                    fileCount: dir.file_count || 0
                };
            });
        }

        // Build publisher details from favorites, enriched with browse data
        const publisherDetails = favData.publishers.map(pub => {
            const info = publisherMap[pub.publisher_path] || {};
            return {
                path: pub.publisher_path,
                name: info.name || pub.publisher_path.split('/').pop(),
                hasThumbnail: info.hasThumbnail || false,
                thumbnailUrl: info.thumbnailUrl || null,
                folderCount: info.folderCount || 0,
                fileCount: info.fileCount || 0
            };
        });

        // Render slides with same structure as grid-item folders
        swiper.innerHTML = publisherDetails.map(pub => {
            // Escape name for use in onclick handler
            const escapedName = pub.name.replace(/'/g, "\\'").replace(/"/g, '&quot;');
            return `
            <div class="swiper-slide">
                <div class="dashboard-card${pub.hasThumbnail ? ' has-thumbnail' : ''}" data-path="${pub.path}" onclick="loadDirectory('${pub.path}')">
                    <div class="dashboard-card-img-container">
                        <img src="${pub.thumbnailUrl || ''}" alt="${pub.name}" class="thumbnail" style="${pub.hasThumbnail ? '' : 'display: none;'}">
                        <div class="icon-overlay" style="${pub.hasThumbnail ? 'display: none;' : ''}">
                            <i class="bi bi-folder-fill"></i>
                        </div>
                        <button class="favorite-button favorited" onclick="event.stopPropagation(); removeFavoriteFromDashboard('${pub.path}', '${escapedName}', this)" title="Remove from Favorites">
                            <i class="bi bi-bookmark-heart-fill"></i>
                        </button>
                    </div>
                    <div class="dashboard-card-body">
                        <div class="text-truncate text-dark item-name" title="${pub.name}">${pub.name}</div>
                        <small class="text-muted item-meta${pub.folderCount === null ? ' metadata-loading' : ''}">${pub.folderCount === null ? 'Loading...' :
                    [
                        pub.folderCount > 0 ? `${pub.folderCount} folder${pub.folderCount !== 1 ? 's' : ''}` : '',
                        pub.fileCount > 0 ? `${pub.fileCount} file${pub.fileCount !== 1 ? 's' : ''}` : ''
                    ].filter(Boolean).join(' | ') || 'Empty'
                }</small>
                    </div>
                </div>
            </div>
        `}).join('');

        initNameTooltips(swiper);

        // Load thumbnails progressively for favorites that don't have them
        const pathsNeedingThumbnails = publisherDetails
            .filter(pub => !pub.hasThumbnail)
            .map(pub => pub.path);

        if (pathsNeedingThumbnails.length > 0) {
            loadDashboardThumbnails(pathsNeedingThumbnails);
        }

        // Load metadata progressively if counts are null
        const pathsNeedingMetadata = publisherDetails
            .filter(pub => pub.folderCount === null)
            .map(pub => pub.path);

        if (pathsNeedingMetadata.length > 0) {
            loadDashboardMetadata(pathsNeedingMetadata);
        }

    } catch (error) {
        console.error('Error loading favorite publishers:', error);
    }
}

/**
 * Load thumbnails for dashboard cards progressively
 * @param {Array<string>} paths - Paths to load thumbnails for
 */
async function loadDashboardThumbnails(paths) {
    try {
        const response = await fetch('/api/browse-thumbnails', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths: paths })
        });

        if (!response.ok) return;
        const data = await response.json();

        // Update dashboard cards with received thumbnails
        Object.entries(data.thumbnails).forEach(([path, thumbData]) => {
            if (thumbData.has_thumbnail) {
                const card = document.querySelector(`.dashboard-card[data-path="${CSS.escape(path)}"]`);
                if (card) {
                    const img = card.querySelector('.thumbnail');
                    const iconOverlay = card.querySelector('.icon-overlay');

                    if (img) {
                        img.src = thumbData.thumbnail_url;
                        img.style.display = '';
                    }
                    if (iconOverlay) {
                        iconOverlay.style.display = 'none';
                    }
                    card.classList.add('has-thumbnail');
                }
            }
        });
    } catch (error) {
        console.error('Error loading dashboard thumbnails:', error);
    }
}

/**
 * Load metadata for dashboard cards progressively
 * @param {Array<string>} paths - Paths to load metadata for
 */
async function loadDashboardMetadata(paths) {
    try {
        const response = await fetch('/api/browse-metadata', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths: paths })
        });

        if (!response.ok) return;
        const data = await response.json();

        // Update dashboard cards with received metadata
        Object.entries(data.metadata).forEach(([path, meta]) => {
            const card = document.querySelector(`.dashboard-card[data-path="${CSS.escape(path)}"]`);
            if (card) {
                const metaEl = card.querySelector('.item-meta');
                if (metaEl) {
                    metaEl.classList.remove('metadata-loading');
                    const parts = [];
                    if (meta.folder_count > 0) {
                        parts.push(`${meta.folder_count} folder${meta.folder_count !== 1 ? 's' : ''}`);
                    }
                    if (meta.file_count > 0) {
                        parts.push(`${meta.file_count} file${meta.file_count !== 1 ? 's' : ''}`);
                    }
                    metaEl.textContent = parts.length > 0 ? parts.join(' | ') : 'Empty';
                }
            }
        });
    } catch (error) {
        console.error('Error loading dashboard metadata:', error);
    }
}

/**
 * Load 'Want to Read' items for the dashboard swiper.
 */
async function loadWantToRead() {
    const swiper = document.querySelector('#wantToReadSwiper .swiper-wrapper');
    if (!swiper) return;

    try {
        const response = await fetch('/api/favorites/to-read');
        const data = await response.json();

        // Store to-read paths globally for grid item sync
        window.toReadPaths = new Set(
            data.success && data.items
                ? data.items.map(item => item.path)
                : []
        );

        if (!data.success || !data.items.length) {
            // Show empty state
            swiper.innerHTML = `
                <div class="swiper-slide">
                    <div class="dashboard-card text-center p-4">
                        <i class="bi bi-bookmark-plus text-muted" style="font-size: 3rem;"></i>
                        <p class="text-muted mt-2">No items to read yet</p>
                    </div>
                </div>
            `;
            return;
        }

        // Separate folders and files
        const folders = data.items.filter(item => item.type === 'folder');
        const files = data.items.filter(item => item.type === 'file');

        // Fetch folder thumbnails if there are folders
        let folderThumbnails = {};
        if (folders.length > 0) {
            try {
                const thumbResponse = await fetch('/api/browse-thumbnails', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paths: folders.map(f => f.path) })
                });
                const thumbData = await thumbResponse.json();
                folderThumbnails = thumbData.thumbnails || {};
            } catch (e) {
                console.error('Error fetching folder thumbnails:', e);
            }
        }

        // Render slides
        swiper.innerHTML = data.items.map(item => {
            const name = item.name || item.path.split('/').pop();
            const escapedName = name.replace(/'/g, "\\'").replace(/"/g, '&quot;');
            const escapedPath = item.path.replace(/'/g, "\\'").replace(/"/g, '&quot;');
            const isFile = item.type === 'file';

            let thumbnailUrl = '';
            let hasThumbnail = false;

            if (isFile) {
                thumbnailUrl = `/api/thumbnail?path=${encodeURIComponent(item.path)}`;
                hasThumbnail = true;
            } else {
                // Check if folder has a thumbnail
                const folderThumb = folderThumbnails[item.path];
                if (folderThumb && folderThumb.has_thumbnail) {
                    thumbnailUrl = folderThumb.thumbnail_url;
                    hasThumbnail = true;
                }
            }

            return `
            <div class="swiper-slide">
                <div class="dashboard-card${hasThumbnail ? ' has-thumbnail' : ''}" data-path="${item.path}" onclick="navigateToItem('${escapedPath}', '${item.type}')">
                    <div class="dashboard-card-img-container">
                        <img src="${thumbnailUrl}" alt="${name}" class="thumbnail" style="${hasThumbnail ? '' : 'display: none;'}">
                        <div class="icon-overlay" style="${hasThumbnail ? 'display: none;' : ''}">
                            <i class="bi bi-folder-fill"></i>
                        </div>
                        <button class="to-read-button marked" onclick="event.stopPropagation(); removeFromWantToRead('${escapedPath}', '${escapedName}', this)" title="Remove from To Read">
                            <i class="bi bi-bookmark"></i>
                        </button>
                    </div>
                    <div class="dashboard-card-body">
                        <div class="text-truncate text-dark item-name" title="${name}">${name}</div>
                        <small class="text-muted">${item.type === 'folder' ? 'Folder' : 'Comic'}</small>
                    </div>
                </div>
            </div>
        `}).join('');

        initNameTooltips(swiper);

    } catch (error) {
        console.error('Error loading want to read items:', error);
    }
}

/**
 * Load recently added files into the dashboard swiper
 */
async function loadRecentlyAddedSwiper() {
    const swiper = document.querySelector('#recentAddedSwiper .swiper-wrapper');
    if (!swiper) return;

    try {
        const response = await fetch('/list-recent-files?limit=10');
        const data = await response.json();

        if (!data.success || !data.files || !data.files.length) {
            // Show empty state
            swiper.innerHTML = `
                <div class="swiper-slide">
                    <div class="dashboard-card text-center p-4">
                        <i class="bi bi-clock-history text-muted" style="font-size: 3rem;"></i>
                        <p class="text-muted mt-2">No recently added files</p>
                    </div>
                </div>
            `;
            return;
        }

        // Helper to format relative time
        const formatTimeAgo = (dateStr) => {
            const date = new Date(dateStr);
            const now = new Date();
            const diffMs = now - date;
            const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

            if (diffDays === 0) return 'Added Today';
            if (diffDays === 1) return 'Added Yesterday';
            if (diffDays < 7) return `Added ${diffDays} days ago`;
            if (diffDays < 30) return `Added ${Math.floor(diffDays / 7)} week${Math.floor(diffDays / 7) > 1 ? 's' : ''} ago`;
            return `Added ${Math.floor(diffDays / 30)} month${Math.floor(diffDays / 30) > 1 ? 's' : ''} ago`;
        };

        // Render slides
        swiper.innerHTML = data.files.map(file => {
            const name = file.file_name;
            const path = file.file_path;
            const thumbnailUrl = `/api/thumbnail?path=${encodeURIComponent(path)}`;
            const timeAgo = formatTimeAgo(file.added_at);

            return `
            <div class="swiper-slide">
                <div class="dashboard-card has-thumbnail" data-path="${path}" onclick="openReaderForFile('${path.replace(/'/g, "\\'")}')">
                    <div class="dashboard-card-img-container">
                        <img src="${thumbnailUrl}" alt="${name}" class="thumbnail">
                    </div>
                    <div class="dashboard-card-body">
                        <div class="text-truncate text-dark item-name" title="${name}">${name}</div>
                        <small class="text-muted">${timeAgo}</small>
                    </div>
                </div>
            </div>
        `}).join('');

        initNameTooltips(swiper);

    } catch (error) {
        console.error('Error loading recently added files:', error);
    }
}

async function loadContinueReadingSwiper() {
    const swiper = document.querySelector('#continueReadingSwiper .swiper-wrapper');
    if (!swiper) return;

    try {
        const response = await fetch('/api/continue-reading?limit=10');
        const data = await response.json();

        if (!data.success || !data.items || !data.items.length) {
            // Show empty state
            swiper.innerHTML = `
                <div class="swiper-slide">
                    <div class="dashboard-card text-center p-4">
                        <i class="bi bi-book-half text-muted" style="font-size: 3rem;"></i>
                        <p class="text-muted mt-2">No comics in progress</p>
                    </div>
                </div>
            `;
            return;
        }

        // Helper to format relative time for reading
        const formatReadTimeAgo = (dateStr) => {
            const date = new Date(dateStr);
            const now = new Date();
            const diffMs = now - date;
            const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

            if (diffDays === 0) return 'Read Today';
            if (diffDays === 1) return 'Read Yesterday';
            if (diffDays < 7) return `Read ${diffDays} days ago`;
            if (diffDays < 30) return `Read ${Math.floor(diffDays / 7)} week${Math.floor(diffDays / 7) > 1 ? 's' : ''} ago`;
            return `Read ${Math.floor(diffDays / 30)} month${Math.floor(diffDays / 30) > 1 ? 's' : ''} ago`;
        };

        // Render slides
        swiper.innerHTML = data.items.map(item => {
            const name = item.file_name;
            const path = item.comic_path;
            const thumbnailUrl = `/api/thumbnail?path=${encodeURIComponent(path)}`;
            const timeAgo = formatReadTimeAgo(item.updated_at);
            const progress = item.progress_percent || 0;
            const pageInfo = item.total_pages ? `Page ${item.page_number + 1} of ${item.total_pages}` : `${progress}%`;

            return `
            <div class="swiper-slide">
                <div class="dashboard-card has-thumbnail" data-path="${path}" onclick="openReaderForFile('${path.replace(/'/g, "\\'")}')">
                    <div class="dashboard-card-img-container">
                        <img src="${thumbnailUrl}" alt="${name}" class="thumbnail">
                        <div class="progress" style="height: 4px; position: absolute; bottom: 0; left: 0; width: 100%; border-radius: 0;">
                            <div class="progress-bar bg-info" role="progressbar" style="width: ${progress}%" aria-valuenow="${progress}" aria-valuemin="0" aria-valuemax="100"></div>
                        </div>
                        <button class="mark-unread-btn" onclick="event.stopPropagation(); markAsUnread('${path.replace(/'/g, "\\'")}')" title="Mark as Unread">
                            <i class="bi bi-x-circle-fill"></i>
                        </button>
                    </div>
                    <div class="dashboard-card-body">
                        <div class="text-truncate text-dark item-name" title="${name}">${name}</div>
                        <small class="text-muted">${pageInfo}<br/>${timeAgo}</small>
                    </div>
                </div>
            </div>
        `}).join('');

        initNameTooltips(swiper);

    } catch (error) {
        console.error('Error loading continue reading items:', error);
    }
}

/**
 * Mark a comic as unread by deleting its reading position
 * @param {string} path - Full path to the comic file
 */
async function markAsUnread(path) {
    try {
        const response = await fetch(`/api/reading-position?path=${encodeURIComponent(path)}`, {
            method: 'DELETE'
        });
        const data = await response.json();

        if (data.success) {
            // Refresh the Continue Reading swiper to remove the item
            loadContinueReadingSwiper();
            showSuccessToast('Marked as unread');
        } else {
            showErrorToast('Failed to mark as unread');
        }
    } catch (error) {
        console.error('Error marking as unread:', error);
        showErrorToast('Error marking as unread');
    }
}

/**
 * Open the comic reader for a specific file path
 * @param {string} path - Full path to the comic file
 */
function openReaderForFile(path) {
    // Navigate to the parent folder first, then open the reader
    const parentPath = path.substring(0, path.lastIndexOf('/'));
    const fileName = path.substring(path.lastIndexOf('/') + 1);

    // Set up so clicking opens the reader directly
    loadDirectory(parentPath).then(() => {
        // Find and click the file's grid item to open reader
        setTimeout(() => {
            const gridItems = document.querySelectorAll('.grid-item');
            for (const item of gridItems) {
                const itemName = item.querySelector('.item-name')?.textContent;
                if (itemName === fileName) {
                    item.click();
                    break;
                }
            }
        }, 500);
    });
}

/**
 * Navigate to an item from the dashboard
 * @param {string} path - Path to the item
 * @param {string} type - 'file' or 'folder'
 */
function navigateToItem(path, type) {
    if (type === 'folder') {
        loadDirectory(path);
    } else {
        // For files, navigate to parent folder
        const parentPath = path.substring(0, path.lastIndexOf('/'));
        loadDirectory(parentPath);
    }
}

/**
 * Remove an item from 'To Read' via the dashboard swiper
 * @param {string} path - Path to the item
 * @param {string} name - Name of the item
 * @param {HTMLElement} button - The button element
 */
function removeFromWantToRead(path, name, button) {
    fetch('/api/favorites/to-read', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Remove the slide from swiper
                const slide = button.closest('.swiper-slide');
                if (slide) slide.remove();

                // Sync global toReadPaths
                window.toReadPaths?.delete(path);

                // Update grid item if visible
                const gridItem = document.querySelector(`.grid-item[data-path="${CSS.escape(path)}"]`);
                if (gridItem) {
                    const gridBtn = gridItem.querySelector('.to-read-button');
                    if (gridBtn) {
                        gridBtn.classList.remove('marked');
                        const gridIcon = gridBtn.querySelector('i');
                        if (gridIcon) gridIcon.className = 'bi bi-bookmark-plus';
                        gridBtn.title = 'Add to To Read';
                    }
                }

                showSuccess(`${name} removed from To Read`);
            } else {
                showError('Failed to remove from To Read: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error removing from To Read:', error);
            showError('Failed to remove from To Read');
        });
}

/**
 * Toggle 'To Read' status for an item
 * @param {string} path - Path to the item
 * @param {string} name - Name of the item
 * @param {string} type - 'file' or 'folder'
 * @param {HTMLElement} button - The button element
 */
function toggleToRead(path, name, type, button) {
    const isMarked = button.classList.contains('marked');
    const method = isMarked ? 'DELETE' : 'POST';
    const icon = button.querySelector('i');

    // Optimistic UI update - change immediately for responsive feel
    if (isMarked) {
        button.classList.remove('marked');
        icon.className = 'bi bi-bookmark-plus';
        button.title = 'Add to To Read';
    } else {
        button.classList.add('marked');
        icon.className = 'bi bi-bookmark';
        button.title = 'Remove from To Read';
    }

    fetch('/api/favorites/to-read', {
        method: method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path, type: type })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Update global state
                if (isMarked) {
                    window.toReadPaths?.delete(path);
                    showSuccess(`${name} removed from To Read`);
                } else {
                    window.toReadPaths?.add(path);
                    showSuccess(`${name} added to To Read`);
                }
            } else {
                // Revert on failure
                if (isMarked) {
                    button.classList.add('marked');
                    icon.className = 'bi bi-bookmark';
                    button.title = 'Remove from To Read';
                } else {
                    button.classList.remove('marked');
                    icon.className = 'bi bi-bookmark-plus';
                    button.title = 'Add to To Read';
                }
                showError('Failed to update To Read: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            // Revert on error
            if (isMarked) {
                button.classList.add('marked');
                icon.className = 'bi bi-bookmark';
                button.title = 'Remove from To Read';
            } else {
                button.classList.remove('marked');
                icon.className = 'bi bi-bookmark-plus';
                button.title = 'Add to To Read';
            }
            console.error('Error toggling To Read:', error);
            showError('Failed to update To Read');
        });
}

/**
 * Toggle favorite status for a publisher (root-level folder)
 * @param {string} path - Path to the publisher folder
 * @param {string} name - Name of the publisher
 * @param {HTMLElement} button - The favorite button element
 */
function togglePublisherFavorite(path, name, button) {
    const isFavorited = button.classList.contains('favorited');
    const method = isFavorited ? 'DELETE' : 'POST';
    const icon = button.querySelector('i');

    fetch('/api/favorites/publishers', {
        method: method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                if (isFavorited) {
                    button.classList.remove('favorited');
                    icon.className = 'bi bi-bookmark-heart';
                    button.title = 'Add to Favorites';
                    // Sync global favoritePaths
                    window.favoritePaths?.delete(path);
                    showSuccess(`${name} removed from favorites`);
                } else {
                    button.classList.add('favorited');
                    icon.className = 'bi bi-bookmark-heart-fill';
                    button.title = 'Remove from Favorites';
                    // Sync global favoritePaths
                    window.favoritePaths?.add(path);
                    showSuccess(`${name} added as a favorite`);
                }
            } else {
                showError('Failed to update favorite: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error toggling favorite:', error);
            showError('Failed to update favorite');
        });
}

/**
 * Remove a publisher from favorites via the dashboard swiper
 * @param {string} path - Path to the publisher folder
 * @param {string} name - Name of the publisher
 * @param {HTMLElement} button - The favorite button element
 */
function removeFavoriteFromDashboard(path, name, button) {
    fetch('/api/favorites/publishers', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Remove the slide from swiper
                const slide = button.closest('.swiper-slide');
                if (slide) slide.remove();

                // Sync global favoritePaths
                window.favoritePaths?.delete(path);

                // Update grid item if visible (at root level)
                const gridItem = document.querySelector(`.grid-item[data-path="${CSS.escape(path)}"]`);
                if (gridItem) {
                    const gridFavBtn = gridItem.querySelector('.favorite-button');
                    if (gridFavBtn) {
                        gridFavBtn.classList.remove('favorited');
                        const gridIcon = gridFavBtn.querySelector('i');
                        if (gridIcon) gridIcon.className = 'bi bi-bookmark-heart';
                        gridFavBtn.title = 'Add to Favorites';
                    }
                }

                showSuccess(`${name} removed from favorites`);
            } else {
                showError('Failed to remove favorite: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error removing favorite:', error);
            showError('Failed to remove favorite');
        });
}

/**
 * Generate a fanned stack thumbnail for a folder
 * @param {string} folderPath - Path to the folder
 * @param {string} folderName - Name of the folder
 */
function generateFolderThumbnail(folderPath, folderName) {
    // Show progress indicator
    showProgressIndicator();
    const progressText = document.getElementById('progress-text');
    if (progressText) {
        progressText.textContent = `Generating thumbnail for ${folderName}...`;
    }

    // Call the generate thumbnail API
    fetch('/api/generate-folder-thumbnail', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder_path: folderPath })
    })
        .then(response => response.json())
        .then(data => {
            hideProgressIndicator();

            if (data.success) {
                showSuccess('Folder thumbnail generated successfully');

                // Update just this folder's thumbnail without full page reload
                const gridItem = document.querySelector(`[data-path="${CSS.escape(folderPath)}"]`);
                if (gridItem) {
                    const container = gridItem.querySelector('.thumbnail-container');
                    const img = gridItem.querySelector('.thumbnail');
                    const iconOverlay = gridItem.querySelector('.icon-overlay');

                    if (img && container) {
                        // Add cache-buster to force reload of new image
                        const thumbnailUrl = `/api/folder-thumbnail?path=${encodeURIComponent(folderPath + '/folder.png')}&t=${Date.now()}`;
                        img.src = thumbnailUrl;
                        img.style.display = 'block';
                        gridItem.classList.add('has-thumbnail');
                        container.classList.add('has-thumbnail');
                        if (iconOverlay) {
                            iconOverlay.style.display = 'none';
                        }
                    }
                }
            } else {
                showError('Error generating thumbnail: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            hideProgressIndicator();
            showError('An error occurred while generating the thumbnail.');
        });
}

/**
 * Check for missing files in a folder
 * @param {string} folderPath - Path to the folder
 * @param {string} folderName - Name of the folder
 */
function checkMissingFiles(folderPath, folderName) {
    // Show progress indicator
    showProgressIndicator();
    const progressText = document.getElementById('progress-text');
    if (progressText) {
        progressText.textContent = `Checking for missing files in ${folderName}...`;
    }

    // Call the missing file check API
    fetch('/api/check-missing-files', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder_path: folderPath })
    })
        .then(response => response.json())
        .then(data => {
            hideProgressIndicator();

            if (data.success) {
                // Show modal with results
                showMissingFileCheckModal(data);
                // Refresh the view (preserve page)
                refreshCurrentView(true);
            } else {
                showError('Error checking missing files: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            hideProgressIndicator();
            showError('An error occurred while checking for missing files.');
        });
}

/**
 * Scan a directory recursively and update the file index
 * @param {string} folderPath - Path to the folder to scan
 * @param {string} folderName - Name of the folder
 */
function scanDirectory(folderPath, folderName) {
    // Show progress indicator
    showProgressIndicator();
    const progressText = document.getElementById('progress-text');
    if (progressText) {
        progressText.textContent = `Scanning files in ${folderName}...`;
    }

    // Call the scan directory API
    fetch('/api/scan-directory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: folderPath })
    })
        .then(response => response.json())
        .then(data => {
            hideProgressIndicator();

            if (data.success) {
                showSuccess(data.message || `Scanned ${folderName} successfully`);
                // Refresh the view to show updated results
                refreshCurrentView(true);
            } else {
                showError('Error scanning directory: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            hideProgressIndicator();
            showError('An error occurred while scanning the directory.');
        });
}

/**
 * Generate missing thumbnails for all subfolders in a root folder
 * @param {string} folderPath - Path to the root folder
 * @param {string} folderName - Name of the folder
 */
function generateAllMissingThumbnails(folderPath, folderName) {
    showProgressIndicator();
    const progressText = document.getElementById('progress-text');
    if (progressText) {
        progressText.textContent = `Generating missing thumbnails in ${folderName}...`;
    }

    fetch('/api/generate-all-missing-thumbnails', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: folderPath })
    })
        .then(response => response.json())
        .then(data => {
            hideProgressIndicator();
            if (data.success) {
                showSuccess(data.message || `Generated ${data.generated} thumbnails`);
                refreshCurrentView(true);
            } else {
                showError('Error: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            hideProgressIndicator();
            showError('An error occurred while generating thumbnails.');
        });
}

/**
 * Show the missing file check results modal
 * @param {Object} data - The response data from the API
 */
function showMissingFileCheckModal(data) {
    // Update summary
    const summaryEl = document.getElementById('missingFileCheckSummary');
    if (summaryEl) {
        summaryEl.textContent = data.summary || 'Check complete.';
    }

    // Update file path
    const pathEl = document.getElementById('missingFileCheckPath');
    if (pathEl) {
        pathEl.textContent = data.relative_missing_file || 'missing.txt';
    }

    // Update file link
    const linkEl = document.getElementById('missingFileCheckLink');
    if (linkEl) {
        // Create a link to download/view the missing.txt file
        linkEl.href = `/api/download?path=${encodeURIComponent(data.missing_file)}`;
    }

    // Show the modal
    const modal = new bootstrap.Modal(document.getElementById('missingFileCheckModal'));
    modal.show();
}

/**
 * Format file size in human-readable format
 * @param {number} bytes - File size in bytes
 * @returns {string} Formatted file size
 */
function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
}

// ============================================================================
// CBZ INFO MODAL FUNCTIONALITY
// ============================================================================

/**
 * Show CBZ information modal
 * @param {string} filePath - Path to the CBZ file
 * @param {string} fileName - Name of the file
 */
function showCBZInfo(filePath, fileName) {
    const modalElement = document.getElementById('cbzInfoModal');
    const content = document.getElementById('cbzInfoContent');

    // Reset content
    content.innerHTML = `
        <div class="text-center">
            <div class="spinner-border" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
            <p class="mt-2">Loading CBZ information...</p>
        </div>
    `;

    // Show modal
    const modal = new bootstrap.Modal(modalElement);
    modal.show();

    // Load metadata
    fetch(`/cbz-metadata?path=${encodeURIComponent(filePath)}`)
        .then(res => res.json())
        .then(data => {
            let html = `
                <div class="row">
                    <div class="col-md-7">
            `;

            // Add ComicInfo section if available
            if (data.comicinfo) {
                html += `
                    <h6 class="mb-3">Comic Information</h6>
                    <div class="card">
                        <div class="card-body">
                            <div class="row">
                `;

                const comicInfo = data.comicinfo;

                // Define field groups
                const fieldGroups = [
                    {
                        title: "Basic Information",
                        fields: [
                            { key: 'Title', label: 'Title' },
                            { key: 'Series', label: 'Series' },
                            { key: 'Number', label: 'Number' },
                            { key: 'Count', label: 'Count' },
                            { key: 'Volume', label: 'Volume' },
                            { key: 'AlternateSeries', label: 'Alternate Series' },
                            { key: 'AlternateNumber', label: 'Alternate Number' },
                            { key: 'AlternateCount', label: 'Alternate Count' }
                        ]
                    },
                    {
                        title: "Publication Details",
                        fields: [
                            { key: 'Year', label: 'Year' },
                            { key: 'Month', label: 'Month' },
                            { key: 'Day', label: 'Day' },
                            { key: 'Publisher', label: 'Publisher' },
                            { key: 'Imprint', label: 'Imprint' },
                            { key: 'Format', label: 'Format' },
                            { key: 'PageCount', label: 'Page Count' },
                            { key: 'LanguageISO', label: 'Language' },
                            { key: 'MetronId', label: 'Metron ID' }
                        ]
                    },
                    {
                        title: "Creative Team",
                        fields: [
                            { key: 'Writer', label: 'Writer' },
                            { key: 'Penciller', label: 'Penciller' },
                            { key: 'Inker', label: 'Inker' },
                            { key: 'Colorist', label: 'Colorist' },
                            { key: 'Letterer', label: 'Letterer' },
                            { key: 'CoverArtist', label: 'Cover Artist' },
                            { key: 'Editor', label: 'Editor' }
                        ]
                    },
                    {
                        title: "Content Details",
                        fields: [
                            { key: 'Genre', label: 'Genre' },
                            { key: 'Characters', label: 'Characters' },
                            { key: 'Teams', label: 'Teams' },
                            { key: 'Locations', label: 'Locations' },
                            { key: 'StoryArc', label: 'Story Arc' },
                            { key: 'SeriesGroup', label: 'Series Group' },
                            { key: 'MainCharacterOrTeam', label: 'Main Character/Team' },
                            { key: 'AgeRating', label: 'Age Rating' }
                        ]
                    },
                    {
                        title: "Additional Information",
                        fields: [
                            { key: 'Summary', label: 'Summary' },
                            { key: 'Notes', label: 'Notes' },
                            { key: 'Web', label: 'Web' },
                            { key: 'ScanInformation', label: 'Scan Information' },
                            { key: 'Review', label: 'Review' },
                            { key: 'CommunityRating', label: 'Community Rating' },
                            { key: 'BlackAndWhite', label: 'Black & White' },
                            { key: 'Manga', label: 'Manga' }
                        ],
                        fullWidth: true
                    }
                ];

                // Generate HTML for each field group
                fieldGroups.forEach(group => {
                    const hasFields = group.fields.some(field => comicInfo[field.key]);
                    if (hasFields) {
                        const colClass = group.fullWidth ? 'col-md-12' : 'col-md-12';
                        html += `
                            <div class="${colClass} mb-3">
                                <h6 class="text-muted small">${group.title}</h6>
                                <ul class="list-unstyled small">
                        `;

                        group.fields.forEach(field => {
                            if (comicInfo[field.key] && comicInfo[field.key] !== '' && comicInfo[field.key] !== -1) {
                                let value = comicInfo[field.key];

                                // Format special values
                                if (field.key === 'PageCount') {
                                    value = parseInt(value);
                                }

                                if (field.key === 'BlackAndWhite' || field.key === 'Manga') {
                                    if (value === 'YesAndRightToLeft') value = 'Yes (Right to Left)';
                                    else if (value !== 'Yes' && value !== 'No') value = 'Unknown';
                                }

                                if (field.key === 'CommunityRating' && value > 0) {
                                    value = `${value}/5`;
                                }

                                html += `<li><strong>${field.label}:</strong> ${value}</li>`;
                            }
                        });

                        html += `
                                </ul>
                            </div>
                        `;
                    }
                });

                html += `
                            </div>
                        </div>
                    </div>
                `;
            } else {
                html += `<p class="text-muted">No ComicInfo.xml found</p>`;
            }

            html += `
                    </div>
                    <div class="col-md-5">
                        <h6>Preview</h6>
                        <div id="cbzPreviewContainer" class="text-center">
                            <div class="spinner-border spinner-border-sm" role="status">
                                <span class="visually-hidden">Loading...</span>
                            </div>
                        </div>
                    </div>
                </div>
            `;

            // Add File Information below
            html += `
                <div class="row mt-4">
                    <div class="col-12">
                        <h6>File Information</h6>
                        <ul class="list-unstyled">
                            <li><strong>Name:</strong> ${fileName}</li>
                            <li><strong>Path:</strong> <code style="word-break: break-all;">${filePath}</code></li>
                            <li><strong>Size:</strong> ${formatFileSize(data.file_size)}</li>
                            <li><strong>Total Files:</strong> ${data.total_files}</li>
                            <li><strong>Image Files:</strong> ${data.image_files}</li>
                        </ul>

                        <h6 class="mt-4">First Files</h6>
                        <ul class="list-unstyled small">
            `;

            // Add file list
            if (data.file_list && data.file_list.length > 0) {
                data.file_list.forEach(file => {
                    html += `<li><code>${file}</code></li>`;
                });
            }

            html += `
                        </ul>
                    </div>
                </div>
            `;

            content.innerHTML = html;

            // Load preview
            fetch(`/cbz-preview?path=${encodeURIComponent(filePath)}&size=large`)
                .then(res => res.json())
                .then(previewData => {
                    const previewContainer = document.getElementById('cbzPreviewContainer');
                    if (previewData.success) {
                        previewContainer.innerHTML = `
                            <div class="cbz-preview-wrapper">
                                <div class="cbz-spinner text-center py-2">
                                    <div class="spinner-border spinner-border-sm text-primary" role="status"></div>
                                </div>
                                <div class="cbz-image-container" style="display: none;"></div>
                                <div class="cbz-image-info text-center mt-2 small text-muted"></div>
                            </div>`;

                        const spinnerEl = previewContainer.querySelector('.cbz-spinner');
                        const imageContainer = previewContainer.querySelector('.cbz-image-container');
                        const imageInfo = previewContainer.querySelector('.cbz-image-info');

                        const img = new Image();
                        img.src = previewData.preview;
                        img.className = 'img-fluid';
                        img.style.maxWidth = '100%';
                        img.style.maxHeight = '600px';
                        img.alt = 'CBZ Preview';
                        img.style.opacity = '0';
                        img.style.transition = 'opacity 0.2s ease-in';

                        img.onload = () => {
                            if (spinnerEl) spinnerEl.style.display = 'none';
                            if (imageContainer) {
                                imageContainer.style.display = 'block';
                                imageContainer.innerHTML = '';
                                imageContainer.appendChild(img);
                                img.offsetHeight;
                                img.style.opacity = '1';
                            }
                            if (imageInfo) {
                                const fileName = previewData.file_name || 'Preview';
                                const origW = previewData.original_size ? previewData.original_size.width : img.naturalWidth;
                                const origH = previewData.original_size ? previewData.original_size.height : img.naturalHeight;
                                const dimensions = `${origW} \u00d7 ${origH}`;
                                imageInfo.innerHTML = `
                                    <div><strong>${fileName}</strong></div>
                                    <div>${dimensions}</div>`;
                            }
                        };

                        img.onerror = () => {
                            if (spinnerEl) spinnerEl.style.display = 'none';
                            if (imageContainer) {
                                imageContainer.style.display = 'block';
                                imageContainer.innerHTML = '<p class="text-muted">Preview not available</p>';
                            }
                        };
                    } else {
                        previewContainer.innerHTML = '<p class="text-muted">Preview not available</p>';
                    }
                })
                .catch(err => {
                    document.getElementById('cbzPreviewContainer').innerHTML = '<p class="text-danger">Error loading preview</p>';
                });
        })
        .catch(err => {
            content.innerHTML = `
                <div class="alert alert-danger">
                    Error loading CBZ information: ${err.message}
                </div>
            `;
        });
}

// ============================================================================
// TEXT FILE VIEWER FUNCTIONALITY
// ============================================================================

/**
 * Open text file viewer modal
 * @param {string} filePath - Path to the text file
 * @param {string} fileName - Name of the file
 */
function openTextFileViewer(filePath, fileName) {
    const modalElement = document.getElementById('textFileViewerModal');
    const fileNameEl = document.getElementById('textFileName');
    const content = document.getElementById('textFileContent');

    // Set file name
    fileNameEl.textContent = fileName;

    // Reset content to loading state
    content.innerHTML = `
        <div class="text-center">
            <div class="spinner-border" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
            <p class="mt-2">Loading text file...</p>
        </div>
    `;

    // Show modal
    const modal = new bootstrap.Modal(modalElement);
    modal.show();

    // Fetch text file content
    fetch(`/api/read-text-file?path=${encodeURIComponent(filePath)}`)
        .then(response => {
            if (!response.ok) {
                throw new Error('Failed to load text file');
            }
            return response.text();
        })
        .then(textContent => {
            // Display the text content
            content.innerHTML = `<pre>${escapeHtml(textContent)}</pre>`;
        })
        .catch(err => {
            content.innerHTML = `
                <div class="alert alert-danger">
                    <i class="bi bi-exclamation-triangle"></i> Error loading text file: ${err.message}
                </div>
            `;
        });
}

/**
 * Escape HTML to prevent XSS
 * @param {string} text - The text to escape
 * @returns {string} Escaped text
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Setup drag and drop zone for a folder
 * @param {HTMLElement} folderElement - The folder grid item element
 * @param {string} folderPath - The path to the folder
 */
function setupFolderDropZone(folderElement, folderPath) {
    // Prevent default drag behaviors
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        folderElement.addEventListener(eventName, preventDefaults, false);
    });

    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }

    // Highlight drop zone when dragging over it
    ['dragenter', 'dragover'].forEach(eventName => {
        folderElement.addEventListener(eventName, () => {
            folderElement.classList.add('drag-over');
        }, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        folderElement.addEventListener(eventName, () => {
            folderElement.classList.remove('drag-over');
        }, false);
    });

    // Handle dropped files
    folderElement.addEventListener('drop', (e) => {
        const dt = e.dataTransfer;
        const files = dt.files;

        if (files.length > 0) {
            handleFileUpload(files, folderPath);
        }
    }, false);
}

/**
 * Handle file upload to a folder
 * @param {FileList} files - The files to upload
 * @param {string} targetPath - The target folder path
 */
function handleFileUpload(files, targetPath) {
    // Validate file types
    const allowedExtensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.cbz', '.cbr'];
    const validFiles = [];
    const invalidFiles = [];

    Array.from(files).forEach(file => {
        const fileName = file.name.toLowerCase();
        const isValid = allowedExtensions.some(ext => fileName.endsWith(ext));

        if (isValid) {
            validFiles.push(file);
        } else {
            invalidFiles.push(file.name);
        }
    });

    // Show error if no valid files
    if (validFiles.length === 0) {
        showError(`No valid files to upload. Allowed types: ${allowedExtensions.join(', ')}`);
        if (invalidFiles.length > 0) {
            showError(`Skipped files: ${invalidFiles.join(', ')}`);
        }
        return;
    }

    // Prepare form data
    const formData = new FormData();
    formData.append('target_dir', targetPath);

    validFiles.forEach(file => {
        formData.append('files', file);
    });

    // Show loading indicator
    showUploadProgress(validFiles.length);

    // Upload files
    fetch('/upload-to-folder', {
        method: 'POST',
        body: formData
    })
        .then(response => response.json())
        .then(data => {
            hideUploadProgress();

            if (data.success) {
                let message = `Successfully uploaded ${data.total_uploaded} file(s)`;

                if (data.total_skipped > 0) {
                    message += `, skipped ${data.total_skipped} file(s)`;
                }

                if (data.total_errors > 0) {
                    message += `, ${data.total_errors} error(s)`;
                }

                showSuccess(message);

                // Show details if there are skipped files or errors
                if (data.skipped.length > 0) {
                    console.log('Skipped files:', data.skipped);
                }

                if (data.errors.length > 0) {
                    console.error('Upload errors:', data.errors);
                    showError(`Errors: ${data.errors.map(e => e.name).join(', ')}`);
                }

                // Refresh the current view if we're in the same directory (preserve current page)
                if (currentPath === targetPath || currentPath === '') {
                    refreshCurrentView(true);
                }
            } else {
                showError('Upload failed: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            hideUploadProgress();
            console.error('Upload error:', error);
            showError('Upload failed: ' + error.message);
        });
}

/**
 * Show upload progress indicator
 * @param {number} fileCount - Number of files being uploaded
 */
function showUploadProgress(fileCount) {
    const container = document.createElement('div');
    container.id = 'upload-progress-indicator';
    container.className = 'alert alert-info position-fixed bottom-0 end-0 m-3';
    container.style.zIndex = '9999';
    container.innerHTML = `
        <div class="d-flex align-items-center">
            <div class="spinner-border spinner-border-sm me-2" role="status">
                <span class="visually-hidden">Uploading...</span>
            </div>
            <div>Uploading ${fileCount} file(s)...</div>
        </div>
    `;
    document.body.appendChild(container);
}

/**
 * Hide upload progress indicator
 */
function hideUploadProgress() {
    const indicator = document.getElementById('upload-progress-indicator');
    if (indicator) {
        indicator.remove();
    }
}

/**
 * Show success message using Bootstrap Toast
 * @param {string} message - Success message to display
 */
function showSuccess(message) {
    const toastEl = document.getElementById('successToast');
    const toastBody = document.getElementById('successToastBody');

    if (toastEl && toastBody) {
        toastBody.textContent = message;
        const toast = new bootstrap.Toast(toastEl, {
            autohide: true,
            delay: 5000
        });
        toast.show();
    } else {
        // Fallback: create a temporary toast if it doesn't exist
        const toastContainer = document.createElement('div');
        toastContainer.className = 'position-fixed top-0 end-0 p-3';
        toastContainer.style.zIndex = '9999';
        toastContainer.innerHTML = `
            <div class="toast align-items-center text-white bg-success border-0" role="alert">
                <div class="d-flex">
                    <div class="toast-body">${message}</div>
                    <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
                </div>
            </div>
        `;
        document.body.appendChild(toastContainer);
        const toast = new bootstrap.Toast(toastContainer.querySelector('.toast'), {
            autohide: true,
            delay: 5000
        });
        toast.show();

        // Remove container after toast is hidden
        toastContainer.querySelector('.toast').addEventListener('hidden.bs.toast', () => {
            toastContainer.remove();
        });
    }
}

/**
 * Refresh the current view
 * @param {boolean} preservePage - If true, keep current page. If false, reset to page 1 (default).
 */
function refreshCurrentView(preservePage = false, forceRefresh = false) {
    if (isRecentlyAddedMode) {
        loadRecentlyAdded(preservePage);
    } else if (isMissingXmlMode) {
        loadMissingXml(preservePage);
    } else if (isAllBooksMode) {
        loadAllBooks(preservePage);
    } else {
        loadDirectory(currentPath, preservePage, forceRefresh);
    }
}

// ============================================================================
// UPDATE XML FUNCTIONALITY
// ============================================================================

/**
 * Update the XML modal input when the field dropdown changes
 */
function updateXmlFieldChanged() {
    const field = document.getElementById('updateXmlField').value;
    const cfg = updateXmlFieldConfig[field];
    if (!cfg) return;
    const input = document.getElementById('updateXmlValue');
    const hint = document.getElementById('updateXmlHint');
    input.placeholder = cfg.placeholder;
    if (cfg.maxlength) {
        input.setAttribute('maxlength', cfg.maxlength);
    } else {
        input.removeAttribute('maxlength');
    }
    hint.textContent = cfg.hint;
}

/**
 * Open the Update XML modal
 * @param {string} folderPath - Path to the folder
 * @param {string} folderName - Display name of the folder
 */
function openUpdateXmlModal(folderPath, folderName) {
    updateXmlCurrentPath = folderPath;
    document.getElementById('updateXmlFolderName').textContent = folderName;
    document.getElementById('updateXmlValue').value = '';
    document.getElementById('updateXmlField').value = 'Volume';
    updateXmlFieldChanged();

    const modal = new bootstrap.Modal(document.getElementById('updateXmlModal'));
    modal.show();
}

/**
 * Show a toast notification with title, message, and type
 * @param {string} title - Toast header title
 * @param {string} message - Toast body message
 * @param {string} type - 'info', 'success', 'warning', or 'error'
 */
function showToast(title, message, type = 'info') {
    const toastContainer = document.querySelector('.toast-container');
    if (!toastContainer) {
        alert(`${title}: ${message}`);
        return;
    }

    const bgClass = type === 'error' ? 'danger' : type === 'success' ? 'success' : type === 'warning' ? 'warning' : 'info';
    const textClass = type === 'warning' ? '' : 'text-white';

    const toastEl = document.createElement('div');
    toastEl.className = `toast bg-${bgClass} ${textClass}`;
    toastEl.setAttribute('role', 'alert');
    toastEl.setAttribute('aria-live', 'assertive');
    toastEl.setAttribute('aria-atomic', 'true');
    toastEl.innerHTML = `
        <div class="toast-header bg-${bgClass} ${textClass}">
            <strong class="me-auto">${title}</strong>
            <button type="button" class="btn-close${textClass ? ' btn-close-white' : ''}" data-bs-dismiss="toast" aria-label="Close"></button>
        </div>
        <div class="toast-body">${message}</div>
    `;

    toastContainer.appendChild(toastEl);

    const toast = new bootstrap.Toast(toastEl);
    toast.show();

    toastEl.addEventListener('hidden.bs.toast', () => {
        if (toastEl.parentNode === toastContainer) {
            toastContainer.removeChild(toastEl);
        }
    });
}

/**
 * Submit the Update XML form
 */
function submitUpdateXml() {
    const field = document.getElementById('updateXmlField').value;
    const value = document.getElementById('updateXmlValue').value.trim();

    const cfg = updateXmlFieldConfig[field];
    const validationError = cfg ? cfg.validate(value) : (!value ? 'Please enter a value' : null);
    if (validationError) {
        showToast('Validation Error', validationError, 'warning');
        return;
    }

    // Close modal
    bootstrap.Modal.getInstance(document.getElementById('updateXmlModal')).hide();

    // Show progress toast
    showToast('Updating XML', `Updating ${field} in all CBZ files...`, 'info');

    // Call API
    fetch('/api/update-xml', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            directory: updateXmlCurrentPath,
            field: field,
            value: value
        })
    })
        .then(response => response.json())
        .then(result => {
            if (result.error) {
                showToast('Update Error', result.error, 'error');
            } else {
                showToast('Update Complete',
                    `Updated ${result.updated} file(s), skipped ${result.skipped}`,
                    result.updated > 0 ? 'success' : 'info');
            }
        })
        .catch(error => {
            showToast('Update Error', error.message, 'error');
        });
}
