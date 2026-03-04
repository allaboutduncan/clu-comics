/**
 * browse.js
 * Frontend logic for the visual file browser.
 * Handles directory fetching, grid rendering, lazy loading, navigation, and pagination.
 */

// Global variable to store current folder path for XML update
// Update XML – field config and current path now in clu-update-xml.js

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
        loadOnTheStackSwiper();
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

// Multi-file selection state
const selectedFiles = new Set();
let lastClickedFileItem = null;
let lastClickedFilePath = null;

// All Books mode state
let isAllBooksMode = false;
let allBooksData = null;
let folderViewPath = '';
let backgroundLoadingActive = false; // Track if background loading is happening

// Recently Added mode state
let isRecentlyAddedMode = false;

// Continue Reading mode state
let isContinueReadingMode = false;

// On the Stack mode state
let isOnTheStackMode = false;

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
    clearFileSelection();

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
            const hiddenFiles = new Set(['cvinfo']);
            data.files.forEach(file => {
                if (hiddenFiles.has(file.name.toLowerCase())) return;
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

        // Reset On the Stack mode when loading a new directory
        isOnTheStackMode = false;

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
        CLU.showError(error.message);
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
        const hiddenFiles = new Set(['cvinfo']);
        const allFiles = data.files
            .filter(file => !hiddenFiles.has(file.name.toLowerCase()))
            .map(file => ({
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
        CLU.showError('Failed to load all books: ' + error.message);
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
        CLU.showError('Failed to load missing XML files: ' + error.message);
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
    isOnTheStackMode = false;
    isMissingXmlMode = false;
    allBooksData = null;
    loadDirectory(folderViewPath);
}

/**
 * Load directory view mode (default view)
 */
function loadDirectoryView() {
    // If we're already in directory mode and not in a special mode, do nothing
    if (!isRecentlyAddedMode && !isContinueReadingMode && !isOnTheStackMode) {
        return;
    }

    // Exit special modes
    isRecentlyAddedMode = false;
    isContinueReadingMode = false;
    isOnTheStackMode = false;

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
        CLU.showError('Failed to load recently added files: ' + error.message);
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
        CLU.showError('Failed to load continue reading items: ' + error.message);
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

    // Filter out internal metadata files that should never appear in the UI
    const hiddenNames = new Set(['cvinfo']);
    items = items.filter(item => !hiddenNames.has(item.name.toLowerCase()));

    // Dispose tooltips before clearing the grid to prevent memory leaks
    disposeNameTooltips(grid);
    grid.innerHTML = '';

    // Show library section (empty-state and file-grid are inside it)
    if (librarySection) librarySection.style.display = 'block';

    if (items.length === 0 && allItems.length === 0) {
        grid.style.display = 'none';
        emptyState.style.display = 'block';
        const hint = document.getElementById('selectionHint');
        if (hint) hint.style.display = 'none';
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
                            <li><a class="dropdown-item folder-action-fetch-metadata" href="#"><i class="bi bi-cloud-download"></i> Fetch All Metadata</a></li>
                            <li><a class="dropdown-item folder-action-missing" href="#"><i class="bi bi-file-earmark-text"></i> Missing File Check</a></li>
                        `;
                    } else {
                        // For folders with files, show full menu
                        dropdownMenu.innerHTML = `
                        <li><a class="dropdown-item folder-action-thumbnail" href="#"><i class="bi bi-image"></i> Generate Thumbnail</a></li>
                        <li><a class="dropdown-item folder-action-scan" href="#"><i class="bi bi-arrow-clockwise"></i> Scan Files</a></li>
                        <li><a class="dropdown-item folder-action-fetch-metadata" href="#"><i class="bi bi-cloud-download"></i> Fetch All Metadata</a></li>
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
                                CLU.openUpdateXmlModal(item.path, item.name);
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

                    // Bind Fetch All Metadata action
                    const fetchMetaAction = dropdownMenu.querySelector('.folder-action-fetch-metadata');
                    if (fetchMetaAction) {
                        fetchMetaAction.onclick = (e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            fetchDirMetadataCollection(item.path, item.name);
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
            gridItem.setAttribute('data-path', item.path);
            metaEl.textContent = CLU.formatFileSize(item.size);

            // Add selection checkbox overlay
            const selCheckbox = document.createElement('div');
            selCheckbox.className = 'selection-checkbox';
            selCheckbox.innerHTML = '<i class="bi bi-check-lg"></i>';
            clone.querySelector('.thumbnail-container').appendChild(selCheckbox);

            // Restore selection state (pagination persistence)
            if (selectedFiles.has(item.path)) {
                gridItem.classList.add('bulk-selected');
            }

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
                    '.action-metadata': () => fetchMetadataCollection(item.path, item.name),
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

            // Handle click for files - multi-select aware
            gridItem.onclick = (e) => {
                // Skip if clicking interactive elements (buttons, dropdowns, links)
                if (e.target.closest('.no-propagation') || e.target.closest('.item-actions') || e.target.closest('.dropdown-menu')) {
                    return;
                }

                if (e.ctrlKey || e.metaKey) {
                    // Ctrl/Cmd+Click: toggle selection
                    e.preventDefault();
                    toggleFileSelection(gridItem, item.path);
                    lastClickedFileItem = gridItem;
                    lastClickedFilePath = item.path;
                } else if (e.shiftKey && lastClickedFileItem) {
                    // Shift+Click: range select
                    e.preventDefault();
                    selectFileRange(gridItem, item.path);
                } else {
                    // Regular click
                    if (selectedFiles.size > 0) {
                        // If selection is active, clear it
                        clearFileSelection();
                    } else {
                        // No selection active, open file normally
                        openFileDefault(item);
                    }
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

    // Show/hide selection hint based on whether files are present
    const hint = document.getElementById('selectionHint');
    if (hint) {
        const hasFiles = items.some(i => i.type !== 'folder');
        hint.style.display = hasFiles ? '' : 'none';
    }

    // Initialize lazy loading
    initLazyLoading();

    // Initialize Bootstrap tooltips for truncated names
    initNameTooltips(grid);
}

// ===========================
// Multi-File Selection Functions
// ===========================

/**
 * Open file with default behavior (comic reader or text viewer).
 */
function openFileDefault(item) {
    if (item.hasThumbnail) {
        window._readerAllItems = allItems;
        openComicReader(item.path);
    } else if (item.name.toLowerCase().endsWith('.txt')) {
        openTextFileViewer(item.path, item.name);
    }
}

/**
 * Toggle selection on a single file grid item.
 */
function toggleFileSelection(gridItem, path) {
    if (selectedFiles.has(path)) {
        selectedFiles.delete(path);
        gridItem.classList.remove('bulk-selected');
    } else {
        selectedFiles.add(path);
        gridItem.classList.add('bulk-selected');
    }
    updateCollectionBulkActionBar();
}

/**
 * Select a range of files between lastClickedFileItem and the target item.
 */
function selectFileRange(toGridItem, toPath) {
    const grid = document.getElementById('file-grid');
    const allFileItems = Array.from(grid.querySelectorAll('.grid-item.file'));

    const fromIndex = allFileItems.indexOf(lastClickedFileItem);
    const toIndex = allFileItems.indexOf(toGridItem);

    if (fromIndex === -1 || toIndex === -1) return;

    const start = Math.min(fromIndex, toIndex);
    const end = Math.max(fromIndex, toIndex);

    for (let i = start; i <= end; i++) {
        const el = allFileItems[i];
        const elPath = el.getAttribute('data-path');
        if (elPath) {
            selectedFiles.add(elPath);
            el.classList.add('bulk-selected');
        }
    }

    lastClickedFileItem = toGridItem;
    lastClickedFilePath = toPath;
    updateCollectionBulkActionBar();
}

/**
 * Clear all file selections.
 */
function clearFileSelection() {
    selectedFiles.clear();
    lastClickedFileItem = null;
    lastClickedFilePath = null;
    document.querySelectorAll('.grid-item.file.bulk-selected').forEach(el => {
        el.classList.remove('bulk-selected');
    });
    updateCollectionBulkActionBar();
}

/**
 * Update the bulk action bar visibility and count.
 */
function updateCollectionBulkActionBar() {
    const bar = document.getElementById('collectionBulkActionBar');
    const countEl = document.getElementById('collectionBulkCount');
    const grid = document.getElementById('file-grid');

    if (!bar) return;

    if (selectedFiles.size > 0) {
        bar.style.display = '';
        if (countEl) countEl.textContent = `${selectedFiles.size} file${selectedFiles.size === 1 ? '' : 's'} selected`;
        if (grid) grid.classList.add('selection-active');
    } else {
        bar.style.display = 'none';
        if (grid) grid.classList.remove('selection-active');
    }

    // Hide selection hint when files are selected (bulk bar takes over)
    const hint = document.getElementById('selectionHint');
    if (hint) {
        hint.style.display = selectedFiles.size > 0 ? 'none' : '';
    }
}

/**
 * Extract series name from filename (adapted from files_context_menu.js).
 */
function extractCollectionSeriesName(filename) {
    let name = filename.replace(/\.(cbz|cbr|pdf)$/i, '');
    name = name.replace(/\s+#?\d+\s*\(\d{4}\).*$/i, '');
    name = name.replace(/\s+#?\d+.*$/i, '');
    name = name.replace(/\s+v\d+.*$/i, '');
    name = name.replace(/\s+\(\d{4}\).*$/i, '');
    return name.trim();
}

/**
 * Get the most common series name from a list of file paths.
 */
function getCollectionMostCommonSeriesName(filePaths) {
    const seriesCount = {};
    filePaths.forEach(path => {
        const filename = path.split('/').pop();
        const seriesName = extractCollectionSeriesName(filename);
        if (seriesName) {
            seriesCount[seriesName] = (seriesCount[seriesName] || 0) + 1;
        }
    });

    let maxCount = 0;
    let mostCommon = null;
    const uniqueSeriesCount = Object.keys(seriesCount).length;

    for (const [series, count] of Object.entries(seriesCount)) {
        if (count > maxCount) {
            maxCount = count;
            mostCommon = series;
        }
    }

    return {
        seriesName: mostCommon,
        hasMultipleSeries: uniqueSeriesCount > 1,
        uniqueSeriesCount
    };
}

/**
 * Bulk action: Create a folder and move selected files into it.
 */
function bulkCreateFolder() {
    if (selectedFiles.size === 0) return;

    const filePaths = Array.from(selectedFiles);
    const seriesInfo = getCollectionMostCommonSeriesName(filePaths);

    // Populate folder name modal
    const nameInput = document.getElementById('collectionFolderName');
    const messageEl = document.getElementById('collectionFolderPromptMessage');
    const fileList = document.getElementById('collectionFolderFileList');

    nameInput.value = seriesInfo.seriesName || '';
    if (seriesInfo.hasMultipleSeries) {
        messageEl.textContent = `Selected files contain ${seriesInfo.uniqueSeriesCount} different series. Please enter a folder name:`;
    } else {
        messageEl.textContent = 'Enter a name for the new folder:';
    }

    fileList.innerHTML = '';
    filePaths.forEach(p => {
        const li = document.createElement('li');
        li.className = 'list-group-item';
        li.textContent = p.split('/').pop();
        fileList.appendChild(li);
    });

    const modal = new bootstrap.Modal(document.getElementById('collectionFolderNameModal'));
    modal.show();

    // Handle Enter key
    nameInput.onkeypress = (e) => {
        if (e.key === 'Enter') document.getElementById('collectionConfirmFolderBtn').click();
    };

    // Focus input when modal opens
    document.getElementById('collectionFolderNameModal').addEventListener('shown.bs.modal', () => {
        nameInput.focus();
    }, { once: true });
}

/**
 * Bulk action: Combine selected CBZ files.
 */
function bulkCombineFiles() {
    const cbzFiles = Array.from(selectedFiles).filter(f => f.toLowerCase().endsWith('.cbz'));

    if (cbzFiles.length < 2) {
        CLU.showError('Please select at least 2 CBZ files to combine.');
        return;
    }

    const firstName = cbzFiles[0].split('/').pop();
    const suggestedName = extractCollectionSeriesName(firstName) || 'Combined';
    document.getElementById('collectionCombineName').value = suggestedName;

    const fileListEl = document.getElementById('collectionCombineFileList');
    fileListEl.innerHTML = '<strong>Files to combine:</strong><br>' +
        cbzFiles.map(f => `&bull; ${f.split('/').pop()}`).join('<br>');

    const modal = new bootstrap.Modal(document.getElementById('collectionCombineModal'));
    modal.show();
}

/**
 * Bulk action: Delete selected files – contract setup for clu-delete.js
 */
function bulkDeleteFiles() {
    if (selectedFiles.size === 0) return;
    window._cluDelete = {
        onBulkDeleteComplete: function (paths, results) {
            const successes = results.filter(r => r.success);
            const failures = results.filter(r => !r.success);
            if (successes.length > 0) CLU.showSuccess('Deleted ' + successes.length + ' file(s)');
            if (failures.length > 0) CLU.showError(failures.length + ' file(s) failed to delete');
            clearFileSelection();
            refreshCurrentView(true, true);
        }
    };
    CLU.showBulkDeleteConfirmation(Array.from(selectedFiles));
}

/**
 * Bulk action: Remove ComicInfo.xml from selected CBZ files.
 */
function bulkRemoveXml() {
    if (selectedFiles.size === 0) return;

    const cbzFiles = Array.from(selectedFiles).filter(f => f.toLowerCase().endsWith('.cbz'));

    if (cbzFiles.length === 0) {
        CLU.showError('No CBZ files selected');
        return;
    }

    document.getElementById('collectionRemoveXmlCount').textContent = cbzFiles.length;

    const fileList = document.getElementById('collectionRemoveXmlFileList');
    fileList.innerHTML = '';
    cbzFiles.forEach(p => {
        const li = document.createElement('li');
        li.className = 'list-group-item';
        li.textContent = p.split('/').pop();
        fileList.appendChild(li);
    });

    const modal = new bootstrap.Modal(document.getElementById('collectionRemoveXmlModal'));
    modal.show();
}

/**
 * After a successful combine, prompt user to delete the original source files.
 * Re-uses the existing delete-multiple modal and endpoint.
 */
function promptDeleteOriginalFiles(filePaths, outputPath, desiredFilename) {
    // Clear the bulk selection first (the combine is done)
    clearFileSelection();

    // Populate the delete modal with the original files (uses unified IDs from modal_delete_confirm.html)
    document.getElementById('deleteMultipleCount').textContent = filePaths.length;

    const fileList = document.getElementById('deleteMultipleFileList');
    fileList.innerHTML = '';
    filePaths.forEach(p => {
        const li = document.createElement('li');
        li.className = 'list-group-item';
        li.textContent = p.split('/').pop();
        fileList.appendChild(li);
    });

    // Temporarily override the confirm button to delete these specific files
    const deleteBtn = document.getElementById('confirmDeleteMultipleBtn');
    const modalEl = document.getElementById('deleteMultipleModal');
    const modalTitle = modalEl.querySelector('.modal-title');
    const originalTitle = modalTitle.textContent;
    modalTitle.textContent = 'Delete Original Files?';

    // Store the one-time handler so we can remove it
    function onConfirmDelete() {
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();

        fetch('/api/delete-multiple', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ targets: filePaths })
        })
        .then(r => r.json())
        .then(data => {
            if (!data.results) {
                CLU.showError('Unexpected response from server');
                return;
            }
            const successes = data.results.filter(r => r.success);
            const failures = data.results.filter(r => !r.success);
            if (successes.length > 0) CLU.showSuccess(`Deleted ${successes.length} original file(s)`);
            if (failures.length > 0) CLU.showError(`${failures.length} file(s) failed to delete`);

            // If output file has a conflict suffix (e.g. "(1)"), rename to the desired name now that originals are gone
            if (outputPath && desiredFilename) {
                const actualFilename = outputPath.split('/').pop();
                if (actualFilename !== desiredFilename) {
                    const desiredPath = outputPath.substring(0, outputPath.lastIndexOf('/') + 1) + desiredFilename;
                    fetch('/move', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ source: outputPath, destination: desiredPath })
                    })
                    .then(r => r.json())
                    .then(moveResult => {
                        if (moveResult.success) {
                            CLU.showSuccess(`Renamed to ${desiredFilename}`);
                        }
                        refreshCurrentView(true, true);
                    })
                    .catch(() => {
                        refreshCurrentView(true, true);
                    });
                    return;
                }
            }
            refreshCurrentView(true, true);
        })
        .catch(err => {
            CLU.showError('Error deleting files: ' + err.message);
        });
    }

    // One-time click handler
    deleteBtn.addEventListener('click', onConfirmDelete, { once: true });

    // Restore title when modal is hidden (whether confirmed or cancelled)
    modalEl.addEventListener('hidden.bs.modal', function restore() {
        modalTitle.textContent = originalTitle;
        deleteBtn.removeEventListener('click', onConfirmDelete);
        modalEl.removeEventListener('hidden.bs.modal', restore);
    });

    const modal = new bootstrap.Modal(modalEl);
    modal.show();
}

// Wire up modal confirm buttons on DOMContentLoaded
document.addEventListener('DOMContentLoaded', () => {
    // Folder creation confirm
    const folderBtn = document.getElementById('collectionConfirmFolderBtn');
    if (folderBtn) {
        folderBtn.addEventListener('click', () => {
            const folderName = document.getElementById('collectionFolderName').value.trim();
            if (!folderName) {
                CLU.showError('Please enter a folder name.');
                return;
            }

            const modal = bootstrap.Modal.getInstance(document.getElementById('collectionFolderNameModal'));
            if (modal) modal.hide();

            const filePaths = Array.from(selectedFiles);
            const firstFilePath = filePaths[0];
            const parentDir = firstFilePath.substring(0, firstFilePath.lastIndexOf('/'));
            const newFolderPath = `${parentDir}/${folderName}`;

            fetch('/create-folder', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: newFolderPath })
            })
            .then(r => r.json())
            .then(result => {
                if (result.success || (result.error && result.error.includes('exists'))) {
                    // Move files sequentially
                    const movePromises = filePaths.map(filePath => {
                        const fileName = filePath.split('/').pop();
                        return fetch('/move', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                source: filePath,
                                destination: `${newFolderPath}/${fileName}`
                            })
                        }).then(r => r.json());
                    });

                    return Promise.all(movePromises);
                } else {
                    throw new Error(result.error || 'Failed to create folder');
                }
            })
            .then(() => {
                CLU.showSuccess(`Moved ${filePaths.length} file(s) to "${folderName}"`);
                clearFileSelection();
                refreshCurrentView(true, true);
            })
            .catch(err => {
                CLU.showError('Error creating folder: ' + err.message);
            });
        });
    }

    // Combine confirm
    const combineBtn = document.getElementById('collectionConfirmCombineBtn');
    if (combineBtn) {
        combineBtn.addEventListener('click', () => {
            const fileName = document.getElementById('collectionCombineName').value.trim();
            if (!fileName) {
                CLU.showError('Please enter a filename.');
                return;
            }

            const cbzFiles = Array.from(selectedFiles).filter(f => f.toLowerCase().endsWith('.cbz'));
            if (cbzFiles.length < 2) {
                CLU.showError('Need at least 2 CBZ files.');
                return;
            }

            const modal = bootstrap.Modal.getInstance(document.getElementById('collectionCombineModal'));
            if (modal) modal.hide();

            const firstFile = cbzFiles[0];
            const lastSlash = Math.max(firstFile.lastIndexOf('/'), firstFile.lastIndexOf('\\'));
            const directory = firstFile.substring(0, lastSlash);

            CLU.showSuccess('Combining files...');

            fetch('/api/combine-cbz', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    files: cbzFiles,
                    output_name: fileName,
                    directory: directory
                })
            })
            .then(r => {
                if (!r.ok) return r.text().then(t => { throw new Error(`Server error ${r.status}: ${t}`); });
                return r.json();
            })
            .then(data => {
                if (data.success) {
                    CLU.showSuccess(`Created ${data.output_file}`);
                    refreshCurrentView(true, true);

                    // Ask user if they want to delete the original files
                    // Pass output info so we can rename to the desired name after deleting originals
                    promptDeleteOriginalFiles(cbzFiles, data.output_path, fileName + '.cbz');
                } else {
                    CLU.showError(data.error || 'Failed to combine files');
                }
            })
            .catch(err => {
                CLU.showError(err.message || 'Error combining files');
            });
        });
    }

    // Bulk delete confirm handled by clu-delete.js

    // Remove XML confirm button
    const collectionConfirmRemoveXmlBtn = document.getElementById('collectionConfirmRemoveXmlBtn');
    if (collectionConfirmRemoveXmlBtn) {
        collectionConfirmRemoveXmlBtn.addEventListener('click', () => {
            const cbzFiles = Array.from(selectedFiles).filter(f => f.toLowerCase().endsWith('.cbz'));

            const modalEl = document.getElementById('collectionRemoveXmlModal');
            const modal = bootstrap.Modal.getInstance(modalEl);
            if (modal) modal.hide();

            fetch('/cbz-bulk-clear-comicinfo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paths: cbzFiles })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    CLU.showSuccess(`Removing XML from ${data.total} file(s)...`);
                    clearFileSelection();
                } else {
                    CLU.showError(data.error || 'Failed to remove XML');
                }
            })
            .catch(err => {
                CLU.showError('Error removing XML: ' + err.message);
            });
        });
    }

    // Escape key to clear selection
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && selectedFiles.size > 0 && !document.querySelector('.modal.show')) {
            clearFileSelection();
        }
    });
});


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
    
    const icon0 = document.createElement('i');
    icon0.className = 'bi bi-hdd-network me-1 text-primary';

    if (!path) {
        homeLi.classList.add('active', 'fw-medium');
        homeLi.appendChild(icon0);
        homeLi.appendChild(document.createTextNode('Home'));
    } else {
        const homeLink = document.createElement('a');
        homeLink.href = '#';
        homeLink.className = 'text-decoration-none fw-medium';
        homeLink.appendChild(icon0);
        homeLink.appendChild(document.createTextNode('Home'));
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

        const folderIcon = document.createElement('i');
        folderIcon.className = isLast ? 'bi bi-folder2-open me-1 text-secondary' : 'bi bi-folder2 me-1 text-secondary';

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
            li.appendChild(folderIcon);
            li.appendChild(document.createTextNode(segment));
        } else {
            const link = document.createElement('a');
            link.href = '#';
            link.className = 'text-decoration-none';
            link.appendChild(folderIcon);
            link.appendChild(document.createTextNode(segment));
            
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
 * Contract setup wrapper for CLU streaming module
 */

/**
 * Show success message using Bootstrap Toast.
 * Contract setup wrapper for CLU streaming module
 */

/**
 * Format file size bytes to human readable string.
 * Contract setup wrapper for CLU streaming module
 */

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
 * Contract setup wrapper for CLU streaming module
 */

/**
 * Hide the global progress indicator
 * Contract setup wrapper for CLU streaming module
 */

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
 * Contract setup wrapper for CLU.executeStreamingOp
 */
function executeScript(scriptType, filePath) {
    // Set up streaming contract for collection.js page-specific behavior
    window._cluStreaming = {
        onComplete: function (type, path) {
            refreshThumbnail(path);
        },
        onError: function () {}
    };
    CLU.executeStreamingOp(scriptType, filePath);
}

function fetchMetadataCollection(filePath, fileName) {
    window._cluMetadata = {
        getLibraryId: function () { return null; },
        onMetadataFound: function () {
            refreshThumbnail(filePath);
            loadDirectory(currentPath, true);
        },
        onBatchComplete: function () {
            loadDirectory(currentPath, true);
        }
    };
    CLU.searchMetadata(filePath, fileName);
}

function fetchDirMetadataCollection(dirPath, dirName) {
    window._cluMetadata = {
        getLibraryId: function () { return null; },
        onMetadataFound: function () {
            loadDirectory(currentPath, true);
        },
        onBatchComplete: function () {
            loadDirectory(currentPath, true);
        }
    };
    CLU.fetchDirectoryMetadata(dirPath, dirName);
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
            CLU.sortInlineEditCards();

            // Setup form submit handler to prevent page navigation
            setupSaveFormHandler();
        })
        .catch(error => {
            container.innerHTML = `<div class="alert alert-danger" role="alert">
                    <strong>Error:</strong> ${error.message}
                </div>`;
            CLU.showError(error.message);
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
        CLU.showProgressIndicator();
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
                        CLU.hideProgressIndicator();
                    }, 500);
                } else {
                    CLU.showError('Error saving file: ' + (result.error || 'Unknown error'));
                    CLU.hideProgressIndicator();
                }
            })
            .catch(error => {
                console.error('Error:', error);
                CLU.showError('An error occurred while saving the file.');
                CLU.hideProgressIndicator();
            });
    });
}

// ============================================================================
// FREE-FORM CROP FUNCTIONALITY (delegated to clu-cbz-crop.js)
// ============================================================================

// (Dead code removed – original cropImageFreeForm/setupCropHandlers/confirmFreeFormCrop moved to clu-cbz-crop.js)
// Removed: _cropImageFreeForm_DEAD, setupCropHandlers, _confirmFreeFormCrop_DEAD

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
    CLU.setupEditModalDropZone();

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
            CLU.sortInlineEditCards();
        })
        .catch(error => {
            container.innerHTML = `<div class="alert alert-danger" role="alert">
                    <strong>Error:</strong> ${error.message}
                </div>`;
            CLU.showError(error.message);
        });
}

function saveEditedCBZ() {
    window._cluCbzEdit = {
        onSaveComplete: function (filePath) {
            document.getElementById('editInlineContainer').innerHTML = '';
            if (currentEditFilePath) {
                refreshThumbnail(currentEditFilePath);
            }
        }
    };
    CLU.saveEditedCBZ();
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
                const cardHTML = CLU.generateCardHTML(fileName, data.imageData);
                container.insertAdjacentHTML('beforeend', cardHTML);

                // Re-sort cards
                CLU.sortInlineEditCards();
            } else {
                CLU.showError('Failed to load uploaded image: ' + (data.error || 'Unknown'));
            }
        })
        .catch(error => {
            console.error('Error loading uploaded image:', error);
            CLU.showError('Failed to load uploaded image');
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
// COMIC READER BRIDGE
// ============================================================================
// Reader functionality is in reader.js (shared module).
// We bridge collection-specific data via window globals.

// readIssuesSet is a Set (never reassigned), so this reference stays valid.
// allItems is reassigned on each directory load, so _readerAllItems is
// updated just before opening the reader in openFileDefault().
window._readerReadIssuesSet = readIssuesSet;




// The remaining reader functions are now in reader.js.
// Dummy anchor so the next edit target is findable.
void 0;

// Setup Update XML event listeners
document.addEventListener('DOMContentLoaded', () => {
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

// (fileToDelete removed – delete handled by clu-delete.js)

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
        CLU.showError('Please select a date');
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
                CLU.showSuccess('Read date saved successfully');
            } else {
                CLU.showError(data.error || 'Failed to save read date');
            }
        })
        .catch(err => {
            CLU.showError('Error saving read date: ' + err.message);
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

// ── Delete – contract setup for clu-delete.js ──────────────────────────────
function showDeleteConfirmation(item) {
    window._cluDelete = {
        onDeleteComplete: function (path) {
            const index = allItems.findIndex(i => i.path === path);
            if (index !== -1) allItems.splice(index, 1);
            renderPage();
            CLU.showSuccess('File deleted successfully');
        }
    };
    CLU.showDeleteConfirmation(item.path, item.name, {
        size: item.size,
        type: item.type,
        showDetails: true
    });
}
function confirmDeleteFile() { CLU.confirmDelete(); }

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
 * Load On the Stack swiper (next unread issues for subscribed series)
 */
async function loadOnTheStackSwiper() {
    const swiper = document.querySelector('#onTheStackSwiper .swiper-wrapper');
    if (!swiper) return;

    try {
        const response = await fetch('/api/on-the-stack?limit=10');
        const data = await response.json();

        if (!data.success || !data.items || !data.items.length) {
            swiper.innerHTML = `
                <div class="swiper-slide">
                    <div class="dashboard-card text-center p-4">
                        <i class="bi bi-layers text-muted" style="font-size: 3rem;"></i>
                        <p class="text-muted mt-2">No new issues to read</p>
                    </div>
                </div>
            `;
            return;
        }

        const formatReadTimeAgo = (dateStr) => {
            if (!dateStr) return '';
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

        swiper.innerHTML = data.items.map(item => {
            const thumbnailUrl = `/api/thumbnail?path=${encodeURIComponent(item.file_path)}`;
            const timeAgo = formatReadTimeAgo(item.last_read_at);
            return `
            <div class="swiper-slide">
                <div class="dashboard-card has-thumbnail" data-path="${item.file_path}"
                     onclick="openReaderForFile('${item.file_path.replace(/'/g, "\\'")}')">
                    <div class="dashboard-card-img-container">
                        <img src="${thumbnailUrl}" alt="${item.file_name}" class="thumbnail">
                    </div>
                    <div class="dashboard-card-body">
                        <div class="text-truncate text-dark item-name" title="${item.file_name}">${item.file_name}</div>
                        <small class="text-muted">${item.series_name} #${item.issue_number}<br/>${timeAgo}</small>
                    </div>
                </div>
            </div>`;
        }).join('');

        initNameTooltips(swiper);

    } catch (error) {
        console.error('Error loading on the stack items:', error);
    }
}

/**
 * Load On the Stack full-page view
 * @param {boolean} preservePage - If true, keep current page
 */
async function loadOnTheStack(preservePage = false) {
    if (isLoading) return;
    setLoading(true);
    folderViewPath = currentPath;
    isOnTheStackMode = true;

    try {
        const response = await fetch('/api/on-the-stack?limit=100');
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();

        allItems = (data.items || []).map(item => ({
            name: item.file_name,
            path: item.file_path,
            type: 'file',
            series_name: item.series_name,
            issue_number: item.issue_number,
        }));

        if (!preservePage) {
            currentPage = 1;
            currentFilter = 'all';
            gridSearchTerm = '';
            gridSearchRaw = '';
        }

        updateBreadcrumb('On the Stack');

        document.getElementById('gridFilterButtons').style.display = 'none';
        document.getElementById('gridSearchRow').style.display = 'block';

        const searchInput = document.querySelector('#gridSearchRow input');
        if (searchInput) {
            searchInput.placeholder = 'Search next issues...';
        }

        const dashboardSections = document.getElementById('dashboard-sections');
        if (dashboardSections) {
            dashboardSections.querySelectorAll('.dashboard-section:not(#library-section)').forEach(el => {
                el.style.display = 'none';
            });
        }

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
        console.error('Error loading on the stack items:', error);
        CLU.showError('Failed to load on the stack items: ' + error.message);
        isOnTheStackMode = false;
        setLoading(false);
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

                CLU.showSuccess(`${name} removed from To Read`);
            } else {
                CLU.showError('Failed to remove from To Read: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error removing from To Read:', error);
            CLU.showError('Failed to remove from To Read');
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
                    CLU.showSuccess(`${name} removed from To Read`);
                } else {
                    window.toReadPaths?.add(path);
                    CLU.showSuccess(`${name} added to To Read`);
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
                CLU.showError('Failed to update To Read: ' + (data.error || 'Unknown error'));
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
            CLU.showError('Failed to update To Read');
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
                    CLU.showSuccess(`${name} removed from favorites`);
                } else {
                    button.classList.add('favorited');
                    icon.className = 'bi bi-bookmark-heart-fill';
                    button.title = 'Remove from Favorites';
                    // Sync global favoritePaths
                    window.favoritePaths?.add(path);
                    CLU.showSuccess(`${name} added as a favorite`);
                }
            } else {
                CLU.showError('Failed to update favorite: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error toggling favorite:', error);
            CLU.showError('Failed to update favorite');
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

                CLU.showSuccess(`${name} removed from favorites`);
            } else {
                CLU.showError('Failed to remove favorite: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error removing favorite:', error);
            CLU.showError('Failed to remove favorite');
        });
}

/**
 * Generate a fanned stack thumbnail for a folder
 * @param {string} folderPath - Path to the folder
 * @param {string} folderName - Name of the folder
 */
function generateFolderThumbnail(folderPath, folderName) {
    // Show progress indicator
    CLU.showProgressIndicator();
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
            CLU.hideProgressIndicator();

            if (data.success) {
                CLU.showSuccess('Folder thumbnail generated successfully');

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
                CLU.showError('Error generating thumbnail: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            CLU.hideProgressIndicator();
            CLU.showError('An error occurred while generating the thumbnail.');
        });
}

/**
 * Check for missing files in a folder
 * @param {string} folderPath - Path to the folder
 * @param {string} folderName - Name of the folder
 */
function checkMissingFiles(folderPath, folderName) {
    // Show progress indicator
    CLU.showProgressIndicator();
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
            CLU.hideProgressIndicator();

            if (data.success) {
                // Show modal with results
                showMissingFileCheckModal(data);
                // Refresh the view (preserve page)
                refreshCurrentView(true);
            } else {
                CLU.showError('Error checking missing files: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            CLU.hideProgressIndicator();
            CLU.showError('An error occurred while checking for missing files.');
        });
}

/**
 * Scan a directory recursively and update the file index
 * @param {string} folderPath - Path to the folder to scan
 * @param {string} folderName - Name of the folder
 */
function scanDirectory(folderPath, folderName) {
    // Show progress indicator
    CLU.showProgressIndicator();
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
            CLU.hideProgressIndicator();

            if (data.success) {
                CLU.showSuccess(data.message || `Scanned ${folderName} successfully`);
                // Refresh the view to show updated results
                refreshCurrentView(true);
            } else {
                CLU.showError('Error scanning directory: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            CLU.hideProgressIndicator();
            CLU.showError('An error occurred while scanning the directory.');
        });
}

/**
 * Generate missing thumbnails for all subfolders in a root folder
 * @param {string} folderPath - Path to the root folder
 * @param {string} folderName - Name of the folder
 */
function generateAllMissingThumbnails(folderPath, folderName) {
    CLU.showProgressIndicator();
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
            CLU.hideProgressIndicator();
            if (data.success) {
                CLU.showSuccess(data.message || `Generated ${data.generated} thumbnails`);
                refreshCurrentView(true);
            } else {
                CLU.showError('Error: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            CLU.hideProgressIndicator();
            CLU.showError('An error occurred while generating thumbnails.');
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

// (duplicate formatFileSize removed — now provided by CLU.formatFileSize via wrapper above)

// ============================================================================
// CBZ INFO MODAL FUNCTIONALITY
// ============================================================================

// CBZ info – contract setup, adapts arguments for CLU.showCBZInfo
function showCBZInfo(filePath, fileName) { CLU.showCBZInfo(filePath, fileName); }

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
            content.innerHTML = `<pre>${CLU.escapeHtml(textContent)}</pre>`;
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
 * Contract setup wrapper for CLU streaming module
 */

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
        CLU.showError(`No valid files to upload. Allowed types: ${allowedExtensions.join(', ')}`);
        if (invalidFiles.length > 0) {
            CLU.showError(`Skipped files: ${invalidFiles.join(', ')}`);
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

                CLU.showSuccess(message);

                // Show details if there are skipped files or errors
                if (data.skipped.length > 0) {
                    console.log('Skipped files:', data.skipped);
                }

                if (data.errors.length > 0) {
                    console.error('Upload errors:', data.errors);
                    CLU.showError(`Errors: ${data.errors.map(e => e.name).join(', ')}`);
                }

                // Refresh the current view if we're in the same directory (preserve current page)
                if (currentPath === targetPath || currentPath === '') {
                    refreshCurrentView(true);
                }
            } else {
                CLU.showError('Upload failed: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            hideUploadProgress();
            console.error('Upload error:', error);
            CLU.showError('Upload failed: ' + error.message);
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

// (duplicate showSuccess removed — now provided by CLU.showSuccess via wrapper above)

/**
 * Refresh the current view
 * @param {boolean} preservePage - If true, keep current page. If false, reset to page 1 (default).
 */
function refreshCurrentView(preservePage = false, forceRefresh = false) {
    if (isRecentlyAddedMode) {
        loadRecentlyAdded(preservePage);
    } else if (isOnTheStackMode) {
        loadOnTheStack(preservePage);
    } else if (isMissingXmlMode) {
        loadMissingXml(preservePage);
    } else if (isAllBooksMode) {
        loadAllBooks(preservePage);
    } else {
        loadDirectory(currentPath, preservePage, forceRefresh);
    }
}

/**
 * Show a toast notification with title, message, and type
 * Contract setup wrapper for CLU streaming module
 */

