// Browser API compatibility layer (Chrome/Firefox/Edge)
const browserAPI = typeof browser !== 'undefined' ? browser : chrome;

/** * PLACEHOLDER FOR YOUR BASE64 IMAGE
 * Replace the string below with your full Base64 string.
 */
const CLU_ICON_BASE64 = "data:image/png;base64,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";

// Helper to create the icon element
function createCluIcon(linkUrl) {
    const img = document.createElement('img');

    img.src = CLU_ICON_BASE64;

    // Reset any inherited styles and force the 48px size
    img.style.all = 'initial'; // Clears any inherited padding/borders
    img.style.width = '48px';
    img.style.height = '48px';
    img.style.minWidth = '48px'; // Prevents squashing in flex containers
    img.style.minHeight = '48px';

    // Positioning and interactivity
    img.style.cursor = 'pointer';
    img.style.marginLeft = '10px';
    img.style.verticalAlign = 'middle';
    img.style.display = 'inline-block';

    img.title = 'Send to CLU';

    img.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();

        // Instant visual feedback (grayscale)
        img.style.filter = 'grayscale(100%)';

        browserAPI.runtime.sendMessage({
            action: "sendLink",
            linkUrl: linkUrl
        }, (response) => {
            if (response && response.success) {
                // Success: Green glow
                img.style.filter = 'drop-shadow(0 0 5px #4CAF50)';
                setTimeout(() => img.style.filter = 'none', 2000);
            } else {
                // Error: Red glow
                img.style.filter = 'drop-shadow(0 0 5px #F44336)';
                console.error("Failed to send link to CLU", response);
            }
        });
    });

    return img;
}

// Function to process ComicBookPlus links
function processComicBookPlus() {
    const links = document.querySelectorAll('a[href*="/dload/"]');
    links.forEach(link => {
        // Use a data attribute to prevent double injection
        if (link.dataset.cluInjected) return;

        const icon = createCluIcon(link.href);
        link.parentNode.insertBefore(icon, link.nextSibling);
        link.dataset.cluInjected = "true";
    });
}

// Function to process GetComics links
function processGetComics() {
    const links = document.querySelectorAll([
        'a[href*="/dlds/"]',
        'a[href*="pixeldrain.com"]',
        'a[href*="mega.nz"]',
        'a[href*="comicfiles.ru"]'
    ].join(', '));
    links.forEach(link => {
        const title = (link.getAttribute('title') || "").toUpperCase();
        const text = (link.innerText || "").toUpperCase();

        // Broaden the check to catch buttons or plain text links
        if (title.includes('PIXELDRAIN') || title.includes('MEGA') || text.includes('DOWNLOAD NOW')) {
            if (link.dataset.cluInjected) return;

            const icon = createCluIcon(link.href);
            link.parentNode.insertBefore(icon, link.nextSibling);
            link.dataset.cluInjected = "true";
        }
    });
}

// Execution logic based on site
const hostname = window.location.hostname;

if (hostname.includes('comicbookplus.com')) {
    processComicBookPlus();
} else if (hostname.includes('getcomics.org')) {
    // Sites often load extra content dynamically; wait slightly
    setTimeout(processGetComics, 1000);
}