// Browser API compatibility layer (Chrome/Firefox)
const browserAPI = typeof browser !== 'undefined' ? browser : chrome;
const storageAPI = browserAPI.storage.local; // Use local for both (sync not supported in Firefox)

document.addEventListener('DOMContentLoaded', function() {
  const apiUrlField = document.getElementById('apiUrl');
  const headersField = document.getElementById('headers');
  const saveButton = document.getElementById('save');
  const status = document.getElementById('status');
  const savedMessage = document.getElementById('saved');

  // Load previously saved options from storage
  storageAPI.get({ apiUrl: '', customHeaders: '' }, (data) => {
    apiUrlField.value = data.apiUrl;
    headersField.value = data.customHeaders;
  });

  // Save the updated options with JSON validation for customHeaders
  saveButton.addEventListener('click', function() {
    const newApiUrl = apiUrlField.value.trim();
    const newHeaders = headersField.value.trim();

    // Validate customHeaders if provided.
    if (newHeaders) {
      try {
        JSON.parse(newHeaders);
      } catch (e) {
        status.textContent = 'Error: Custom Headers must be valid JSON.';
        status.style.color = 'red';
        return;
      }
    }

    // Request host permission for the API URL origin so fetch() bypasses CORS
    // (needed when Cloudflare Access is enabled on the server).
    const saveOptions = () => {
      storageAPI.set(
        {
          apiUrl: newApiUrl,
          customHeaders: newHeaders
        },
        () => {
          status.textContent = '';
          savedMessage.textContent = 'Settings saved!';
          savedMessage.classList.remove('hidden');
          setTimeout(() => {
            savedMessage.classList.add('hidden');
          }, 2000);
        }
      );
    };

    if (newApiUrl) {
      try {
        const url = new URL(newApiUrl);
        const origin = url.origin + '/*';
        browserAPI.permissions.request({ origins: [origin] }, (granted) => {
          if (!granted) {
            status.textContent = 'Warning: Host permission denied. Downloads may fail if Cloudflare Access is enabled.';
            status.style.color = 'orange';
          }
          saveOptions();
        });
      } catch (e) {
        // Invalid URL — save anyway, user will see errors when they try to use it
        saveOptions();
      }
    } else {
      saveOptions();
    }
  });
});
