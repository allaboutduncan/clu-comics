/**
 * Reading List Picker — shared JS for "Add to Reading List" flow.
 * Requires: modal_add_to_reading_list.html partial included in the page.
 */

let _rlPickerModal = null;
let _rlPickerFilePath = null;

function openAddToReadingListModal(filePath) {
    _rlPickerFilePath = filePath;

    const loading = document.getElementById('readingListPickerLoading');
    const content = document.getElementById('readingListPickerContent');
    const select = document.getElementById('readingListPickerSelect');
    const confirmBtn = document.getElementById('readingListPickerConfirmBtn');
    const newNameInput = document.getElementById('readingListPickerNewName');

    loading.style.display = '';
    content.style.display = 'none';
    confirmBtn.disabled = true;
    newNameInput.value = '';

    if (!_rlPickerModal) {
        _rlPickerModal = new bootstrap.Modal(document.getElementById('addToReadingListModal'));
    }
    _rlPickerModal.show();

    // Fetch reading lists
    fetch('/api/reading-lists/summary')
        .then(r => r.json())
        .then(lists => {
            select.innerHTML = '<option value="">-- Choose a list --</option>';
            lists.forEach(l => {
                const opt = document.createElement('option');
                opt.value = l.id;
                opt.textContent = l.name;
                select.appendChild(opt);
            });
            loading.style.display = 'none';
            content.style.display = '';
        })
        .catch(err => {
            console.error('Error fetching reading lists:', err);
            loading.innerHTML = '<div class="text-danger">Failed to load lists</div>';
        });

    // Enable confirm when a list is selected
    select.onchange = () => {
        confirmBtn.disabled = !select.value;
    };

    // Create new list inline
    document.getElementById('readingListPickerCreateBtn').onclick = () => {
        const name = newNameInput.value.trim();
        if (!name) return;

        fetch('/api/reading-lists/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                const opt = document.createElement('option');
                opt.value = data.list_id;
                opt.textContent = name;
                select.appendChild(opt);
                select.value = data.list_id;
                confirmBtn.disabled = false;
                newNameInput.value = '';
                _rlPickerShowToast('List created', 'success');
            } else {
                _rlPickerShowToast('Error: ' + data.message, 'error');
            }
        })
        .catch(err => {
            console.error('Error creating list:', err);
            _rlPickerShowToast('Failed to create list', 'error');
        });
    };

    // Confirm button — add to selected list
    confirmBtn.onclick = () => {
        const listId = select.value;
        if (!listId || !_rlPickerFilePath) return;

        confirmBtn.disabled = true;

        fetch(`/api/reading-lists/${listId}/add-entry`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_path: _rlPickerFilePath })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                _rlPickerModal.hide();
                _rlPickerShowToast('Added to reading list', 'success');
            } else {
                _rlPickerShowToast('Error: ' + data.message, 'error');
                confirmBtn.disabled = false;
            }
        })
        .catch(err => {
            console.error('Error adding to list:', err);
            _rlPickerShowToast('Failed to add to list', 'error');
            confirmBtn.disabled = false;
        });
    };
}

function _rlPickerShowToast(message, type) {
    // Use existing showToast if available (from reading_list.js or other pages)
    if (typeof showToast === 'function') {
        showToast(message, type);
        return;
    }
    // Fallback: create a simple toast
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container position-fixed end-0 p-4';
        container.style.zIndex = '1100';
        container.style.top = '60px';
        document.body.appendChild(container);
    }
    const bgClass = type === 'success' ? 'bg-success' : type === 'error' ? 'bg-danger' : 'bg-primary';
    const id = 'rl-toast-' + Date.now();
    container.insertAdjacentHTML('beforeend', `
        <div id="${id}" class="toast align-items-center text-white ${bgClass} border-0 show" role="alert">
            <div class="d-flex">
                <div class="toast-body">${message}</div>
                <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
            </div>
        </div>
    `);
    setTimeout(() => {
        const el = document.getElementById(id);
        if (el) el.remove();
    }, 5000);
}
