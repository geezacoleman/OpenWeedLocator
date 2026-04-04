/* ==========================================================================
   OWL Dashboard - Storage Module
   Storage tab, file browser, USB devices
   ========================================================================== */

let currentDirectory = '/media';

/**
 * Initialize storage tab functionality
 */
function initStorageTab() {
    const refreshButton = document.getElementById('refreshFiles');
    const downloadLogsButton = document.getElementById('downloadLogs');

    if (refreshButton) {
        refreshButton.addEventListener('click', loadStorageData);
    }

    if (downloadLogsButton) {
        downloadLogsButton.addEventListener('click', downloadLogs);
    }

    // Add breadcrumb container if it doesn't exist
    const fileList = document.getElementById('fileList');
    if (fileList && !document.getElementById('breadcrumbs')) {
        const breadcrumbDiv = document.createElement('div');
        breadcrumbDiv.id = 'breadcrumbs';
        fileList.parentNode.insertBefore(breadcrumbDiv, fileList);
    }
}

/**
 * Load storage data (USB devices and files)
 */
function loadStorageData() {
    const usbContainer = document.getElementById('usbDevices');
    if (usbContainer) {
        usbContainer.innerHTML = '<p>Scanning for USB devices...</p>';
    }

    const timestamp = new Date().getTime();
    apiRequest(`/api/usb_storage?t=${timestamp}`)
        .then(response => response.json())
        .then(data => {
            updateUSBDevices(data);
            if (data && data.length > 0) {
                currentDirectory = data[0].mount_point;
                loadDirectoryContents(currentDirectory);
            } else {
                loadDirectoryContents('/media');
            }
        })
        .catch(error => {
            if (usbContainer) {
                usbContainer.innerHTML = '<p style="color: red;">Error loading USB devices. <button onclick="loadStorageData()">Retry</button></p>';
            }
        });
}

/**
 * Load contents of a specific directory
 */
function loadDirectoryContents(directory) {
    currentDirectory = directory;

    apiRequest('/api/browse_files', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory: directory })
    })
        .then(response => response.json())
        .then(data => {
            updateFileBrowser(data.files || [], directory);
            updateBreadcrumbs(directory);
        })
        .catch(error => {
            const fileContainer = document.getElementById('fileList');
            if (fileContainer) {
                fileContainer.innerHTML = '<p style="color: red;">Error loading directory contents</p>';
            }
        });
}

/**
 * Update USB devices display
 */
function updateUSBDevices(devices) {
    const container = document.getElementById('usbDevices');
    if (!container) return;

    if (!devices || devices.length === 0) {
        container.innerHTML = '<p>No USB storage devices detected</p>';
        return;
    }

    let html = '';
    devices.forEach(device => {
        html += `
            <div class="usb-device">
                <h4>${device.device}</h4>
                <div class="device-info">
                    <span><strong>Size:</strong> ${device.size}</span>
                    <span><strong>Used:</strong> ${device.used}</span>
                    <span><strong>Available:</strong> ${device.available}</span>
                    <span><strong>Mount:</strong> ${device.mount_point}</span>
                </div>
            </div>
        `;
    });

    container.innerHTML = html;

    // Update save directory info
    const saveDirectoryElement = document.getElementById('saveDirectory');
    const availableSpaceElement = document.getElementById('availableSpace');

    if (devices.length > 0) {
        const primaryDevice = devices[0];
        if (saveDirectoryElement) {
            saveDirectoryElement.textContent = primaryDevice.mount_point;
        }
        if (availableSpaceElement) {
            availableSpaceElement.textContent = primaryDevice.available;
        }
    }
}

/**
 * Update breadcrumb navigation
 */
function updateBreadcrumbs(directory) {
    const breadcrumbContainer = document.getElementById('breadcrumbs');
    if (!breadcrumbContainer) return;

    let html = '<nav class="breadcrumbs">';

    if (directory === '/media') {
        html += '<span class="breadcrumb-item active">USB Storage</span>';
    } else {
        html += '<a href="#" onclick="navigateToDirectory(\'/media\')" class="breadcrumb-item">USB Storage</a>';

        const relativePath = directory.replace('/media/', '');
        const parts = relativePath.split('/');
        let currentPath = '/media';

        for (let i = 0; i < parts.length; i++) {
            currentPath += '/' + parts[i];
            if (i === parts.length - 1) {
                html += ` > <span class="breadcrumb-item active">${parts[i]}</span>`;
            } else {
                html += ` > <a href="#" onclick="navigateToDirectory('${currentPath}')" class="breadcrumb-item">${parts[i]}</a>`;
            }
        }
    }

    html += '</nav>';
    breadcrumbContainer.innerHTML = html;
}

/**
 * Navigate to a specific directory
 */
function navigateToDirectory(directory) {
    loadDirectoryContents(directory);
}

/**
 * Update file browser display with directory support
 */
function updateFileBrowser(files, directory) {
    const container = document.getElementById('fileList');
    if (!container) return;

    if (!files || files.length === 0) {
        container.innerHTML = '<p>No files found in this directory.</p>';
        return;
    }

    let html = '<div class="file-browser">';

    html += `<div class="directory-info">
        <strong>Current Directory:</strong> ${directory}
        <span class="item-count">(${files.length} items)</span>
    </div>`;

    files.forEach(file => {
        const isDirectory = file.is_directory;
        const icon = isDirectory ? '📁' : '📄';
        const sizeDisplay = isDirectory ? 'Directory' : file.size_formatted || formatFileSize(file.size);
        const isParent = file.is_parent || false;

        html += `
            <div class="file-item ${isDirectory ? 'directory' : 'file'}" ${isParent ? 'data-parent="true"' : ''}>
                <div class="file-info">
                    <span class="file-icon">${icon}</span>
                    <div class="file-details">
                        <strong class="file-name">${file.name}</strong><br>
                        <small class="file-meta">
                            Size: ${sizeDisplay}
                            ${file.modified ? ` | Modified: ${file.modified}` : ''}
                        </small>
                    </div>
                </div>
                <div class="file-actions">
                    ${isDirectory ? 
                        `<button onclick="navigateToDirectory('${file.path}')" class="btn-primary">Open</button>` :
                        `<button onclick="downloadFile('${file.path}')" class="btn-secondary">Download</button>`
                    }
                </div>
            </div>
        `;
    });

    html += '</div>';
    container.innerHTML = html;

    // Update total recordings count
    const fileCount = files.filter(f => !f.is_directory && !f.is_parent).length;
    const totalRecordingsElement = document.getElementById('totalRecordings');
    if (totalRecordingsElement) {
        totalRecordingsElement.textContent = fileCount;
    }
}

/**
 * Download a specific file
 */
function downloadFile(filePath) {
    if (!filePath || filePath.endsWith('/')) {
        showNotification('Error', 'Invalid file path', 'error');
        return;
    }

    showNotification('Info', 'Starting download...', 'info');

    const link = document.createElement('a');
    link.href = `/api/download_file?path=${encodeURIComponent(filePath)}`;
    link.download = filePath.split('/').pop();
    link.style.display = 'none';

    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    showNotification('Success', `Download started: ${link.download}`, 'success');
}

/**
 * Download logs
 */
function downloadLogs() {
    showNotification('Info', 'Downloading logs...', 'info');

    const link = document.createElement('a');
    link.href = '/api/download_logs';
    link.download = `owl_logs_${new Date().toISOString().split('T')[0]}.zip`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    showNotification('Success', 'Log download started', 'success');
}
