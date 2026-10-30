// Full JavaScript for index.html

let currentPath = "/data"; // Root directory
let currentEventSource = null;
let isScriptRunning = false;

// Global variables for modal filtering
let modalDirectoryData = null;
let currentModalFilter = 'all';

// -- Utility Functions --

function disableButtons() {
    document.querySelectorAll('button').forEach(button => button.disabled = true);
    document.getElementById('selected-directory').disabled = true;
}

function enableButtons() {
    document.querySelectorAll('button').forEach(button => button.disabled = false);
    document.getElementById('selected-directory').disabled = false;
}

function hideProgressIndicator() {
    const progressContainer = document.getElementById('progress-container');
    if (progressContainer) {
        progressContainer.style.display = 'none';
    }
}

function selectFile(filePath) {
    console.log("Selected file:", filePath);
    let selectedDirectoryInput = document.getElementById("selected-directory");
    if (selectedDirectoryInput) {
        selectedDirectoryInput.value = filePath;
    }
    let currentPathDisplay = document.getElementById("current-path-display");
    if (currentPathDisplay) {
        currentPathDisplay.textContent = filePath;
    }
    updateScriptOptions();
    // Move focus to the "Browse" button before closing modal
    let browseButton = document.getElementById("browse-btn");
    if (browseButton) {
        browseButton.focus();
    }
    // Close the modal after file selection
    let modalElement = document.getElementById("directoryModal");
    let modalInstance = bootstrap.Modal.getInstance(modalElement);
    if (modalInstance) {
        modalInstance.hide();
    }
    setTimeout(() => {
        document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
        document.body.classList.remove('modal-open');
    }, 300);
}

function selectDirectory() {
    let selectedDirectoryInput = document.getElementById("selected-directory");
    if (selectedDirectoryInput) {
        document.getElementById("selected-directory").value = selectedDirectoryInput.value;
    }
    let browseButton = document.getElementById("browse-btn");
    if (browseButton) {
        browseButton.focus();
    }
    let modalElement = document.getElementById("directoryModal");
    let modalInstance = bootstrap.Modal.getInstance(modalElement);
    if (modalInstance) {
        modalInstance.hide();
    }
    setTimeout(() => {
        document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
        document.body.classList.remove('modal-open');
    }, 300);
}

// -- Modal Directory Listing and Filtering --

// This loadDirectories function is used in the modal (via openDirectoryModal) to
// fetch directory data, then store it and update both the filter bar and the directory list.
function loadDirectories(path = "/data") {
    const directoryList = document.getElementById("directory-list");
    if (directoryList) {
        directoryList.innerHTML = `<div class="d-flex justify-content-center my-3">
            <button class="btn btn-primary" type="button" disabled>
                <span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>
                Loading...
            </button>
        </div>`;
    }

    fetch(`/list-directories?path=${encodeURIComponent(path)}`)
        .then(response => response.json())
        .then(data => {
            // Save the fetched data for later filtering in the modal
            modalDirectoryData = data;

            // A new folder starts at All, as in the other folder pickers.
            currentModalFilter = 'all';
            updateModalFilterBar(data.directories);
            // Render the directory list with the current filter applied
            renderModalDirectoryListing(data, currentModalFilter);

            // Update the selected directory input and current path display
            let selectedDirectoryInput = document.getElementById("selected-directory");
            if (selectedDirectoryInput) {
                selectedDirectoryInput.value = data.current_path;
            }
            let currentPathDisplay = document.getElementById("current-path-display");
            if (currentPathDisplay) {
                currentPathDisplay.textContent = data.current_path;
            }
            updateScriptOptions();
        })
        .catch(error => console.error("Error fetching directories:", error));
}

// Build the modal's A-Z bar from the directory names. CLU.letterBar keeps the
// current letter selected, and a second click on it goes back to All.
function updateModalFilterBar(directories) {
    const bar = document.querySelector("#source-directory-filter .clu-letter-bar");
    if (!bar) return;

    // CLU.sortLetters buckets each first character ('#' for a non-letter).
    const letters = CLU.sortLetters(directories.map(dir => dir.charAt(0)));
    // Keep the selection only while this folder still has that letter.
    if (currentModalFilter !== 'all' && letters.indexOf(currentModalFilter) === -1) {
        currentModalFilter = 'all';
    }

    CLU.letterBar(bar, {
        letters,
        active: currentModalFilter === 'all' ? '' : currentModalFilter,
        onChange: value => {
            currentModalFilter = value || 'all';
            renderModalDirectoryListing(modalDirectoryData, currentModalFilter);
        }
    });
}

// Render the modal's directory (and file) list based on the current filter.
function renderModalDirectoryListing(data, filter) {
    const directoryList = document.getElementById("directory-list");
    if (!directoryList) return;
    directoryList.innerHTML = "";

    // If there's a parent directory, add a "Go Back" option.
    if (data.parent) {
        let backItem = document.createElement("li");
        backItem.className = "list-group-item list-group-item-action d-flex align-items-center";
        backItem.innerHTML = `<i class="bi bi-arrow-left-square me-2"></i> Parent`;
        backItem.onclick = () => loadDirectories(data.parent);
        directoryList.appendChild(backItem);
    }

    // Filter directories based on the active filter.
    let filteredDirs = data.directories.filter(dir => {
        if (filter === 'all') return true;
        if (filter === '#') return !/^[A-Za-z]/.test(dir.charAt(0));
        return dir.charAt(0).toUpperCase() === filter;
    });
    filteredDirs.forEach(dir => {
        let item = document.createElement("li");
        item.className = "list-group-item list-group-item-action d-flex align-items-center justify-content-between";
        // Left: clicking navigates deeper into the directory
        let leftDiv = document.createElement("div");
        leftDiv.className = "d-flex align-items-center";
        leftDiv.style.cursor = "pointer";
        leftDiv.innerHTML = `<i class="bi bi-folder me-2" style="color: #bf9300"></i> ${dir}`;
        leftDiv.onclick = () => loadDirectories(data.current_path + "/" + dir);
        // Right: clicking selects the directory and closes the modal
        let selectIcon = document.createElement("i");
        selectIcon.className = "bi bi-folder-check";
        selectIcon.style.color = "blue";
        selectIcon.style.cursor = "pointer";
        selectIcon.onclick = function(e) {
            e.stopPropagation();
            let selectedDirectoryInput = document.getElementById("selected-directory");
            if (selectedDirectoryInput) {
                selectedDirectoryInput.value = data.current_path + "/" + dir;
            }
            updateScriptOptions();
            let modalElement = document.getElementById("directoryModal");
            let modalInstance = bootstrap.Modal.getInstance(modalElement);
            if (modalInstance) {
                modalInstance.hide();
            }
            setTimeout(() => {
                document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
                document.body.classList.remove('modal-open');
            }, 300);
        };
        item.appendChild(leftDiv);
        item.appendChild(selectIcon);
        directoryList.appendChild(item);
    });

    // When the filter is "all", also list files.
    if (filter === 'all' && data.files && data.files.length > 0) {
        data.files.forEach(file => {
            const fileName = typeof file === "string" ? file : file.name;
            let fileItem = document.createElement("li");
            fileItem.className = "list-group-item list-group-item-action";
            let iconClass = "bi bi-file-earmark-zip";
            let iconStyle = "";
            if (fileName.toLowerCase().endsWith(".pdf")) {
                iconClass = "bi bi-file-earmark-pdf";
                iconStyle = ' style="color: red;"';
            } else if (fileName.toLowerCase().endsWith(".cbr")) {
                iconClass = "bi bi-file-earmark-zip-fill";
                iconStyle = ' style="color: purple;"';
            }
            fileItem.innerHTML = `<i class="${iconClass} me-2"${iconStyle}></i> ${fileName}`;
            fileItem.onclick = function() {
                selectFile(data.current_path + "/" + fileName);
            };
            directoryList.appendChild(fileItem);
        });
    }

    let currentPathDisplay = document.getElementById("current-path-display");
    if (currentPathDisplay) {
        currentPathDisplay.textContent = data.current_path;
    }
}

// -- Modal Opening --

function openDirectoryModal() {
    let selectedDirInput = document.getElementById("selected-directory");
    let path = selectedDirInput ? selectedDirInput.value.trim() : "/data";
    if (!path) {
        path = "/data";
    }
    const lastSlashIndex = path.lastIndexOf("/");
    if (lastSlashIndex > 0) {
        path = path.substring(0, lastSlashIndex);
    }
    if (!path.trim() || path === "/") {
        path = "/data";
    }
    console.log("Opening directory modal in:", path);
    loadDirectories(path);
    let modal = new bootstrap.Modal(document.getElementById("directoryModal"));
    modal.show();
}

// -- Script Execution Functions --

function runScript(scriptType) {
    const directoryInput = document.getElementById('selected-directory').value.trim();
    const logsContainer = document.getElementById('logs');
    logsContainer.innerHTML = '';

    if (!directoryInput || directoryInput === "/" || directoryInput === "/data") {
        logsContainer.innerHTML = `<div class="alert alert-danger" role="alert">
                <strong>Error:</strong> Please select a valid directory or file.
            </div>`;
        return;
    }

    if (scriptType === 'edit') {
        document.getElementById('edit').classList.remove('collapse');
        document.getElementById('single').classList.add('collapse');
        const container = document.getElementById('editInlineContainer');
        container.innerHTML = `<div class="d-flex justify-content-center my-3">
                                    <button class="btn btn-primary" type="button" disabled>
                                        <span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>
                                        Unpacking CBZ File ...
                                    </button>
                                </div>`;
        fetch(`/edit?file_path=${encodeURIComponent(directoryInput)}`)
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
                sortInlineEditCards()
            })
            .catch(error => {
                logsContainer.innerHTML = `<div class="alert alert-danger" role="alert">
                        <strong>Error:</strong> ${error.message}
                    </div>`;
            });
        return;
    }

    if (scriptType === 'delete') {
        document.getElementById('filePathToDelete').textContent = directoryInput;
        const deleteModal = new bootstrap.Modal(document.getElementById('confirmDeleteModal'));
        deleteModal.show();
        window.confirmedScriptType = scriptType;
        return;
    }

    executeScript(scriptType);
}

function executeScript(scriptType) {
    const directoryInput = document.getElementById('selected-directory').value.trim();
    if (!directoryInput) {
        CLU.toast("Please select a directory or file first.", "warning");
        return;
    }
    if (currentEventSource) {
        currentEventSource.close();
        currentEventSource = null;
    }
    let url;
    const isFile = directoryInput.match(/\.\w+$/);
    if (isFile) {
        url = `/stream/${scriptType}?file_path=${encodeURIComponent(directoryInput)}`;
    } else {
        url = `/stream/${scriptType}?directory=${encodeURIComponent(directoryInput)}`;
    }
    console.log(`Connecting to: ${url}`);
    const eventSource = new EventSource(url);
    currentEventSource = eventSource;
    isScriptRunning = true;
    disableButtons();
    const logsContainer = document.getElementById('logs');
    logsContainer.innerHTML = "";
    
    // Show progress container for directory operations
    const progressContainer = document.getElementById('progress-container');
    const progressBar = document.getElementById('progress-bar');
    const progressText = document.getElementById('progress-text');
    
            if (scriptType === 'convert' || scriptType === 'rebuild') {
            progressContainer.style.display = 'block';
            progressBar.style.width = '0%';
            progressBar.textContent = '0%';
            progressBar.setAttribute('aria-valuenow', '0');
            progressText.textContent = 'Initializing...';
            
            // Initialize progress tracking variables
            window.progressData = {
                totalFiles: 0,
                processedFiles: 0,
                currentFile: '',
                initialized: false
            };
        }
    


    // Parse log messages for progress updates
    eventSource.onmessage = (event) => {
        const line = event.data.trim();
        const logLine = document.createElement('div');
        
        // Skip empty keepalive messages
        if (!line) {
            return;
        }
        
        // Check for progress-related log messages
        if (scriptType === 'convert' || scriptType === 'rebuild') {
            // Look for total files count - be more specific
            if (line.includes('Found') && (line.includes('files to convert') || line.includes('files to process')) && !window.progressData.initialized) {
                const match = line.match(/Found (\d+) files to (?:convert|process)/);
                if (match) {
                    window.progressData.totalFiles = parseInt(match[1]);
                    window.progressData.initialized = true;
                    console.log(`Total files detected: ${window.progressData.totalFiles}`);
                    progressText.textContent = `Found ${window.progressData.totalFiles} files to process. Starting...`;
                }
            }
            
            // Look for file processing messages - only if initialized
            if (line.includes('Processing file:') && window.progressData.initialized) {
                const match = line.match(/Processing file: (.+?) \((\d+)\/(\d+)\)/);
                if (match) {
                    const filename = match[1];
                    const current = parseInt(match[2]);
                    const total = parseInt(match[3]);
                    
                    window.progressData.processedFiles = current;
                    console.log(`Progress update: ${current}/${total}`);
                    
                    if (total > 0) {
                        const progressPercent = Math.round((current / total) * 100);
                        const remaining = total - current;
                        console.log(`Progress percent: ${progressPercent}%`);

                        // Update progress bar immediately
                        if (progressBar) {
                            progressBar.style.width = progressPercent + '%';
                            progressBar.textContent = `${progressPercent}% (${current}/${total})`;
                            progressBar.setAttribute('aria-valuenow', progressPercent);
                            console.log('Updated progressBar:', progressBar.textContent);
                        } else {
                            console.error('progressBar element not found');
                        }

                        if (progressText) {
                            progressText.textContent = `Processing: ${filename} - ${remaining} file${remaining !== 1 ? 's' : ''} remaining`;
                            console.log('Updated progressText:', progressText.textContent);
                        } else {
                            console.error('progressText element not found');
                        }
                    }
                }
            }
            
            // Look for large file processing
            if (line.includes('Processing large file') && line.includes('MB')) {
                const match = line.match(/Processing large file \((\d+\.\d+)MB\): (.+)/);
                if (match) {
                    const size = match[1];
                    const filename = match[2];
                    progressText.textContent = `Processing large file (${size}MB): ${filename} - This may take several minutes...`;
                }
            }
            
            // Look for compression progress
            if (line.includes('Compression progress:')) {
                const match = line.match(/Compression progress: (\d+\.\d+)% \((\d+)\/(\d+) files\)/);
                if (match) {
                    const percent = match[1];
                    const current = match[2];
                    const total = match[3];
                    progressText.textContent = `Compressing files: ${percent}% (${current}/${total} files)`;
                }
            }
            
            // Look for extraction progress (rebuild operation)
            if (line.includes('Extraction progress:')) {
                const match = line.match(/Extraction progress: (\d+\.\d+)% \((\d+)\/(\d+) files\)/);
                if (match) {
                    const percent = match[1];
                    const current = match[2];
                    const total = match[3];
                    progressText.textContent = `Extracting files: ${percent}% (${current}/${total} files)`;
                }
            }
            
            // Look for step progress (convert: 3 steps, rebuild: 4 steps)
            if (line.includes('Step 1/3:') || line.includes('Step 2/3:') || line.includes('Step 3/3:') || 
                line.includes('Step 1/4:') || line.includes('Step 2/4:') || line.includes('Step 3/4:') || line.includes('Step 4/4:')) {
                const stepMatch = line.match(/Step (\d+)\/(\d+): (.+)/);
                if (stepMatch) {
                    const step = stepMatch[1];
                    const totalSteps = stepMatch[2];
                    const action = stepMatch[3];
                    progressText.textContent = `Step ${step}/${totalSteps}: ${action}`;
                }
            }
            
            // Look for completion - be more specific
            if ((line.includes('Conversion completed') || line.includes('Rebuild completed')) && window.progressData.initialized) {
                progressBar.style.width = '100%';
                progressBar.textContent = `100% (${window.progressData.totalFiles}/${window.progressData.totalFiles})`;
                progressBar.setAttribute('aria-valuenow', '100');
                progressText.textContent = `Completed processing ${window.progressData.totalFiles} files!`;

                // Auto-hide progress container after 5 seconds
                setTimeout(() => {
                    const progressContainer = document.getElementById('progress-container');
                    if (progressContainer) {
                        progressContainer.style.display = 'none';
                    }
                }, 5000);
            }
        }
        
        // Handle regular log display
        if (line.startsWith("ERROR:")) {
            logLine.textContent = line;
            logLine.className = "alert alert-danger";
            logLine.setAttribute("role", "alert");
        } else if (line.startsWith("SUCCESS:")) {
            logLine.textContent = line;
            logLine.className = "alert alert-success";
            logLine.setAttribute("role", "alert");
        } else if (line.startsWith("IMAGE:")) {
            const imageUrl = line.substring(6).trim();
            const img = document.createElement('img');
            img.src = imageUrl;
            img.className = "mt-2 img-fluid";
            logsContainer.appendChild(img);
            logsContainer.scrollTop = logsContainer.scrollHeight;
            return;
        } else {
            logLine.innerHTML = line;
        }
        logsContainer.appendChild(logLine);
        logsContainer.scrollTop = logsContainer.scrollHeight;
    };

    eventSource.addEventListener("completed", () => {
        const successLine = document.createElement('div');
        successLine.textContent = "Process completed successfully!";
        successLine.className = "alert alert-success";
        logsContainer.appendChild(successLine);
        logsContainer.scrollTop = logsContainer.scrollHeight;
        eventSource.close();
        currentEventSource = null;
        isScriptRunning = false;
        enableButtons();
    });

    eventSource.onerror = () => {
        const errorLine = document.createElement('div');
        errorLine.textContent = "Network or connection error occurred.";
        errorLine.className = "alert alert-warning";
        logsContainer.appendChild(errorLine);
        logsContainer.scrollTop = logsContainer.scrollHeight;
        eventSource.close();
        currentEventSource = null;
        isScriptRunning = false;
        enableButtons();
    };
}

function confirmDeletion() {
    const directoryInput = document.getElementById('selected-directory').value.trim();
    if (!directoryInput) return;
    bootstrap.Modal.getInstance(document.getElementById('confirmDeleteModal')).hide();
    executeScript('delete');
}

function updateScriptOptions() {
    const directoryInput = document.getElementById('selected-directory').value.trim();
    const forbiddenExtensions = ['zip', 'rar', 'cbr', 'cbz'];
    const requiredExtensions = ['zip', 'rar', 'cbr', 'cbz'];
    const extensionMatch = directoryInput.match(/\.([^.\\/:*?"<>|\r\n]+)$/);
    const extension = extensionMatch ? extensionMatch[1].toLowerCase() : '';
    const isForbidden = forbiddenExtensions.includes(extension);
    const isRequired = requiredExtensions.includes(extension);
    document.getElementById('multiple').classList.toggle('collapse', isForbidden);
    document.getElementById('single').classList.toggle('collapse', !isForbidden);
    const scriptAvailability = {
        'rename': !isForbidden, 'convert': !isForbidden,
        'rebuild': !isForbidden, 'pdf': !isForbidden,
        'missing': !isForbidden, 'enhance_dir': !isForbidden, 'comicinfo': !isForbidden,
        'single_file': isRequired, 'crop': isRequired, 'edit': isRequired,
        'remove': isRequired, 'add': isRequired, 'enhance_single': isRequired, 'delete': isRequired
    };
    Object.keys(scriptAvailability).forEach(scriptType => {
        const button = document.getElementById(`btn-${scriptType}`);
        if (button) {
            button.disabled = !scriptAvailability[scriptType];
        }
    });
}

// -- DOMContentLoaded Event Handlers --

document.addEventListener('DOMContentLoaded', () => {
    let directoryInput = document.getElementById('selected-directory');
    let directoryModal = document.getElementById("directoryModal");

    if (directoryInput) {
        directoryInput.addEventListener("input", () => {
            console.log("User entered:", directoryInput.value);
            updateScriptOptions();
        });
        directoryInput.addEventListener("paste", () => {
            setTimeout(() => {
                console.log("Pasted path:", directoryInput.value);
                updateScriptOptions();
            }, 100);
        });
    } else {
        console.error("Error: 'selected-directory' not found in DOM.");
    }

    if (directoryModal) {
        directoryModal.addEventListener("hidden.bs.modal", () => {
            console.log("Modal closed, cleaning up...");
            let browseButton = document.getElementById("browse-btn");
            if (browseButton) {
                browseButton.focus();
            }
            setTimeout(() => {
                document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
                document.body.classList.remove('modal-open');
            }, 300);
        });
    } else {
        console.error("Error: #directoryModal not found.");
    }

    const saveForm = document.getElementById('editCbzSaveForm');
    if (saveForm) {
        saveForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const formData = new FormData(saveForm);
            fetch('/save', {
                method: 'POST',
                body: formData
            })
            .then(response => {
                if (!response.ok) {
                    throw new Error("Error in saving");
                }
                return response.json();
            })
            .then(data => {
                if (data.success) {
                    var modalEl = document.getElementById('editCbzModal');
                    var modalInstance = bootstrap.Modal.getInstance(modalEl);
                    if (modalInstance) {
                        modalInstance.hide();
                    }
                    window.location.href = '/';
                } else {
                    CLU.toast("Error: " + data.error, "error");
                }
            })
            .catch(error => {
                console.error(error);
                CLU.toast("An error occurred during processing.", "error");
            });
        });
    }

    const inlineSaveForm = document.getElementById('editInlineSaveForm');
    if (inlineSaveForm) {
        inlineSaveForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const container = document.getElementById('editInlineContainer');
            container.innerHTML = `<div class="d-flex justify-content-center my-3">
                                        <button class="btn btn-primary" type="button" disabled>
                                            <span class="spinner-grow spinner-grow-sm" role="status" aria-hidden="true"></span>
                                            Repacking CBZ File ...
                                        </button>
                                    </div>`;
            const formData = new FormData(inlineSaveForm);
            fetch('/save', {
                method: 'POST',
                body: formData
            })
            .then(response => {
                if (!response.ok) {
                    throw new Error("Error in saving");
                }
                return response.json();
            })
            .then(data => {
                if (data.success) {
                    document.getElementById('edit').classList.add('collapse');
                    document.getElementById('single').classList.remove('collapse');
                } else {
                    CLU.toast("Error: " + data.error, "error");
                }
            })
            .catch(error => {
                console.error(error);
                CLU.toast("An error occurred during processing.", "error");
            });
        });
    }
});

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

    input.addEventListener('keydown', function(event) {
        if (event.key === 'Enter') {
            event.preventDefault();
            processRename(event);
            input.blur();
        }
    });

    input.addEventListener('blur', function(event) {
        processRename(event);
    }, { once: true });
}

// New function to sort the inline edit cards by the filename value
// Mimics file system sorting: alpha-numeric order with files starting with special characters coming first
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

// Updated performRename function: after a successful rename, sort the cards.
function performRename(input) {
    const newFilename = input.value.trim();
    const folderName = document.getElementById('editInlineFolderName').value;
    const oldFilename = input.dataset.oldFilename || input.previousElementSibling.textContent.trim();
    
    // Cancel if the filename hasn't changed
    if (newFilename === oldFilename) {
        input.classList.add('d-none');
        input.previousElementSibling.classList.remove('d-none');
        return;
    }

    const oldPath = `${folderName}/${oldFilename}`;
    const newPath = `${folderName}/${newFilename}`;

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
            span.classList.remove('d-none');
            input.classList.add('d-none');
            // After updating the filename, re-sort the inline edit cards.
            sortInlineEditCards();
        } else {
            CLU.toast('Error renaming file: ' + data.error, "error");
        }
    })
    .catch(error => {
        console.error('Error:', error);
        CLU.toast('An error occurred while renaming the file.', "error");
    });
}

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
    const oldFilename = span.dataset.oldFilename || span.textContent.trim();
    const fullPath = `${folderName}/${oldFilename}`;

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
            CLU.toast("Error deleting image: " + data.error, "error");
        }
    })
    .catch(error => {
        console.error("Error:", error);
        CLU.toast("An error occurred while deleting the image.", "error");
    });
}

// handle crop functions
function cropImageLeft(buttonElement) {
    processCropImage(buttonElement, 'left');
}

function cropImageCenter(buttonElement) {
    processCropImage(buttonElement, 'center');
}

function cropImageRight(buttonElement) {
    processCropImage(buttonElement, 'right');
}

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

    const oldFilename = span.dataset.oldFilename || span.textContent.trim();
    const fullPath = `${folderName}/${oldFilename}`;

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
            CLU.toast("Error cropping image: " + data.error, "error");
        }
    })
    .catch(error => {
        console.error("Error:", error);
        CLU.toast("An error occurred while cropping the image.", "error");
    });
}

// Function to generate a new card's HTML using the new image's file path and base64 data
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
                                <button aria-label="Delete image" title="Delete image" type="button" class="btn btn-outline-danger btn-sm" onclick="deleteCardImage(this)">
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

document.addEventListener('DOMContentLoaded', () => {
    const directoryModal = document.getElementById("directoryModal");
    if (directoryModal) {
        directoryModal.addEventListener("hidden.bs.modal", () => {
            directoryModal.removeAttribute('aria-hidden');
            document.body.classList.remove('modal-open');
            document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
        });
    }
});

// Free Form Crop functionality
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

    const oldFilename = span.dataset.oldFilename || span.textContent.trim();
    const fullPath = `${folderName}/${oldFilename}`;

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

    // Load image from the server (we'll need to create an endpoint to serve the full image)
    // For now, we'll fetch the image data
    fetch('/get-image-data', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: fullPath })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            cropImage.src = data.imageData;
            cropImage.onload = function() {
                setupCropHandlers();
                cropModal.show();
            };
        } else {
            CLU.toast("Error loading image: " + data.error, "error");
        }
    })
    .catch(error => {
        console.error("Error:", error);
        CLU.toast("An error occurred while loading the image.", "error");
    });
}

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
    cropSelection.addEventListener('mousedown', function(e) {
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
    modal.addEventListener('hidden.bs.modal', function() {
        document.removeEventListener('keydown', handleKeyDown);
        document.removeEventListener('keyup', handleKeyUp);
        document.removeEventListener('mousemove', updateCrop);
        document.removeEventListener('mouseup', endCrop);
        cropContainer.removeEventListener('mousedown', startCrop);
    }, { once: true });
}

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

            CLU.toast("Free form crop completed successfully!", "success");
        } else {
            CLU.toast("Error cropping image: " + data.error, "error");
        }
    })
    .catch(error => {
        console.error("Error:", error);
        CLU.toast("An error occurred while cropping the image.", "error");
    });
}