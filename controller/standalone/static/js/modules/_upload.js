/* ==========================================================================
   OWL Dashboard - Upload Module
   S3 upload, credentials, directory browser, progress monitoring
   ========================================================================== */

let uploadProgressInterval = null;
let selectedUploadDirectory = null;
let uploadInProgress = false;

/**
 * Initialize upload tab functionality
 */
function initUploadTab() {
    const checkConnectionBtn = document.getElementById('checkConnection');
    if (checkConnectionBtn) {
        checkConnectionBtn.addEventListener('click', checkEthernetConnection);
    }

    const testCredentialsBtn = document.getElementById('testCredentials');
    if (testCredentialsBtn) {
        testCredentialsBtn.addEventListener('click', testS3Credentials);
    }

    const browseDirectoriesBtn = document.getElementById('browseDirectories');
    if (browseDirectoriesBtn) {
        browseDirectoriesBtn.addEventListener('click', showDirectoryBrowser);
    }

    const scanDirectoryBtn = document.getElementById('scanDirectory');
    if (scanDirectoryBtn) {
        scanDirectoryBtn.addEventListener('click', scanSelectedDirectory);
    }

    const startUploadBtn = document.getElementById('startUpload');
    if (startUploadBtn) {
        startUploadBtn.addEventListener('click', startUploadProcess);
    }

    const stopUploadBtn = document.getElementById('stopUpload');
    if (stopUploadBtn) {
        stopUploadBtn.addEventListener('click', stopUploadProcess);
    }

    const findKeyFilesBtn = document.getElementById('findKeyFiles');
    if (findKeyFilesBtn) {
        findKeyFilesBtn.addEventListener('click', findKeyFilesOnUSB);
    }

    // Auto-fill Hetzner endpoint when region is selected
    const regionSelect = document.getElementById('region');
    const endpointInput = document.getElementById('endpointUrl');
    if (regionSelect && endpointInput) {
        regionSelect.addEventListener('change', function() {
            if (this.value === 'fsn1') {
                endpointInput.value = 'https://fsn1.your-objectstorage.com';
            } else if (this.value === 'hel1') {
                endpointInput.value = 'https://hel1.your-objectstorage.com';
            } else if (this.value === 'nbg1') {
                endpointInput.value = 'https://nbg1.your-objectstorage.com';
            } else {
                endpointInput.value = '';
            }
        });
    }

    // Set today's date as default
    const dateInput = document.getElementById('metadataDate');
    if (dateInput) {
        dateInput.value = new Date().toISOString().split('T')[0];
    }
}

/* --------------------------------------------------------------------------
   Credentials Management
   -------------------------------------------------------------------------- */

function findKeyFilesOnUSB() {
    const button = document.getElementById('findKeyFiles');
    const listElement = document.getElementById('keyFilesList');

    button.disabled = true;
    button.textContent = 'Searching...';
    listElement.classList.add('hidden');

    apiRequest('/api/upload/find_key_files')
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                displayKeyFiles(data.key_files);
                if (data.key_files.length === 0) {
                    showNotification('Info', 'No credential files found on USB drives', 'info');
                } else {
                    showNotification('Success', `Found ${data.key_files.length} credential file(s)`, 'success');
                }
            } else {
                throw new Error(data.error);
            }
        })
        .catch(error => {
            showNotification('Error', 'Failed to search for key files: ' + error.message, 'error');
        })
        .finally(() => {
            button.disabled = false;
            button.textContent = 'Find Key Files on USB';
        });
}

function displayKeyFiles(keyFiles) {
    const listElement = document.getElementById('keyFilesList');

    if (!keyFiles || keyFiles.length === 0) {
        listElement.innerHTML = '<p>No credential files found. Make sure your USB drive has a "secret_key" folder with credential files.</p>';
        listElement.classList.remove('hidden');
        return;
    }

    let html = '<div class="key-files-container">';
    html += '<h5>Found Credential Files:</h5>';

    keyFiles.forEach(file => {
        html += `
            <div class="key-file-item">
                <div class="key-file-info">
                    <div class="key-file-name">${file.name}</div>
                    <div class="key-file-meta">
                        USB: ${file.usb_device} | Size: ${formatFileSize(file.size)} | Modified: ${file.modified}
                    </div>
                </div>
                <div class="key-file-actions">
                    <button onclick="loadCredentialsFromFile('${file.path}')" class="btn-primary">
                        Load Credentials
                    </button>
                </div>
            </div>
        `;
    });

    html += '</div>';
    listElement.innerHTML = html;
    listElement.classList.remove('hidden');
}

function loadCredentialsFromFile(filePath) {
    showNotification('Info', 'Loading credentials...', 'info');

    apiRequest('/api/upload/load_credentials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: filePath })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                const creds = data.credentials;

                document.getElementById('accessKey').value = creds.access_key || '';
                document.getElementById('secretKey').value = creds.secret_key || '';
                document.getElementById('bucketName').value = creds.bucket_name || '';
                document.getElementById('region').value = creds.region || 'us-east-1';

                if (creds.endpoint_url) {
                    document.getElementById('endpointUrl').value = creds.endpoint_url;
                } else {
                    const regionSelect = document.getElementById('region');
                    if (regionSelect) {
                        regionSelect.dispatchEvent(new Event('change'));
                    }
                }

                showNotification('Success', 'Credentials loaded successfully', 'success');

                const testBtn = document.getElementById('testCredentials');
                if (testBtn) {
                    testBtn.disabled = false;
                }
            } else {
                throw new Error(data.error);
            }
        })
        .catch(error => {
            showNotification('Error', 'Failed to load credentials: ' + error.message, 'error');
        });
}

function checkEthernetConnection() {
    const button = document.getElementById('checkConnection');
    const statusElement = document.getElementById('connectionStatus');
    const textElement = document.getElementById('connectionText');
    const indicatorElement = document.getElementById('connectionIndicator');

    button.disabled = true;
    button.textContent = 'Checking...';
    statusElement.className = 'connection-status checking';
    indicatorElement.textContent = '⏳';
    textElement.textContent = 'Checking connection...';

    apiRequest('/api/upload/check_ethernet', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.connected) {
                statusElement.className = 'connection-status connected';
                indicatorElement.textContent = '✅';
                textElement.textContent = 'Ethernet connected';
                showNotification('Success', 'Ethernet connection verified', 'success');
            } else {
                statusElement.className = 'connection-status disconnected';
                indicatorElement.textContent = '❌';
                textElement.textContent = data.error || 'No ethernet connection';
                showNotification('Warning', data.error || 'No ethernet connection', 'warning');
            }
        })
        .catch(error => {
            statusElement.className = 'connection-status disconnected';
            indicatorElement.textContent = '❌';
            textElement.textContent = 'Connection check failed';
            showNotification('Error', 'Failed to check connection: ' + error.message, 'error');
        })
        .finally(() => {
            button.disabled = false;
            button.textContent = 'Check Ethernet Connection';
        });
}

function testS3Credentials() {
    const button = document.getElementById('testCredentials');
    const statusElement = document.getElementById('credentialsStatus');

    const accessKey = document.getElementById('accessKey').value.trim();
    const secretKey = document.getElementById('secretKey').value.trim();
    const bucketName = document.getElementById('bucketName').value.trim();
    const region = document.getElementById('region').value;
    const endpointUrl = document.getElementById('endpointUrl').value.trim() || null;

    if (!accessKey || !secretKey || !bucketName) {
        statusElement.className = 'credentials-status invalid';
        statusElement.textContent = 'Please fill in all required fields';
        return;
    }

    button.disabled = true;
    button.textContent = 'Testing...';
    statusElement.className = 'credentials-status testing';
    statusElement.textContent = 'Testing credentials...';

    apiRequest('/api/upload/test_credentials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            access_key: accessKey,
            secret_key: secretKey,
            bucket_name: bucketName,
            region: region,
            endpoint_url: endpointUrl
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.valid) {
                statusElement.className = 'credentials-status valid';
                statusElement.textContent = '✅ Credentials valid - bucket accessible';
                showNotification('Success', 'S3 credentials verified successfully', 'success');
                document.getElementById('browseDirectories').disabled = false;
            } else {
                statusElement.className = 'credentials-status invalid';
                statusElement.textContent = '❌ ' + data.error;
                showNotification('Error', 'Credential test failed: ' + data.error, 'error');
            }
        })
        .catch(error => {
            statusElement.className = 'credentials-status invalid';
            statusElement.textContent = '❌ Test failed: ' + error.message;
            showNotification('Error', 'Failed to test credentials: ' + error.message, 'error');
        })
        .finally(() => {
            button.disabled = false;
            button.textContent = 'Test Credentials';
        });
}

/* --------------------------------------------------------------------------
   Directory Browser
   -------------------------------------------------------------------------- */

function showDirectoryBrowser() {
    const browserElement = document.getElementById('directoryBrowser');
    browserElement.classList.remove('hidden');
    loadUploadDirectories('/media');
}

function loadUploadDirectories(directory) {
    const contentsElement = document.getElementById('directoryContents');
    contentsElement.innerHTML = '<p>Loading directories...</p>';

    apiRequest('/api/upload/browse_directories', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory: directory })
    })
        .then(response => response.json())
        .then(data => {
            updateDirectoryBrowser(data.directories || [], directory);
            updateUploadBreadcrumbs(directory);
        })
        .catch(error => {
            contentsElement.innerHTML = '<p style="color: red;">Error loading directories</p>';
        });
}

function updateDirectoryBrowser(directories, currentPath) {
    const contentsElement = document.getElementById('directoryContents');

    if (!directories || directories.length === 0) {
        contentsElement.innerHTML = '<p>No directories found.</p>';
        return;
    }

    let html = '<div class="directories-list">';

    directories.forEach(dir => {
        const isParent = dir.is_parent || false;
        const iconClass = isParent ? 'parent' : '';

        html += `
            <div class="directory-item ${iconClass}" onclick="selectUploadDirectory('${dir.path}', '${dir.name}', ${isParent})">
                <span class="directory-icon">${isParent ? '⬅️' : '📁'}</span>
                <div class="directory-info">
                    <div class="directory-name">${dir.name}</div>
                    ${dir.modified ? `<div class="directory-meta">Modified: ${dir.modified}</div>` : ''}
                </div>
                <div class="directory-actions">
                    ${!isParent ? '<button class="btn-primary" onclick="event.stopPropagation(); chooseUploadDirectory(\'' + dir.path + '\')">Select</button>' : ''}
                </div>
            </div>
        `;
    });

    html += '</div>';
    contentsElement.innerHTML = html;
}

function selectUploadDirectory(path, name, isParent) {
    loadUploadDirectories(path);
}

function chooseUploadDirectory(path) {
    selectedUploadDirectory = path;
    document.getElementById('selectedDirectory').textContent = path;
    document.getElementById('scanDirectory').disabled = false;
    document.getElementById('directoryBrowser').classList.add('hidden');
    showNotification('Info', `Selected directory: ${path}`, 'info');
}

function updateUploadBreadcrumbs(directory) {
    const breadcrumbContainer = document.getElementById('directoryBreadcrumbs');
    if (!breadcrumbContainer) return;

    let html = '<nav class="breadcrumbs">';

    if (directory === '/media') {
        html += '<span class="breadcrumb-item active">USB Storage</span>';
    } else {
        html += '<a href="#" onclick="loadUploadDirectories(\'/media\')" class="breadcrumb-item">USB Storage</a>';

        const relativePath = directory.replace('/media/', '');
        const parts = relativePath.split('/');
        let currentPath = '/media';

        for (let i = 0; i < parts.length; i++) {
            currentPath += '/' + parts[i];
            if (i === parts.length - 1) {
                html += ` > <span class="breadcrumb-item active">${parts[i]}</span>`;
            } else {
                html += ` > <a href="#" onclick="loadUploadDirectories('${currentPath}')" class="breadcrumb-item">${parts[i]}</a>`;
            }
        }
    }

    html += '</nav>';
    breadcrumbContainer.innerHTML = html;
}

function scanSelectedDirectory() {
    if (!selectedUploadDirectory) {
        showNotification('Warning', 'Please select a directory first', 'warning');
        return;
    }

    const button = document.getElementById('scanDirectory');
    const resultsElement = document.getElementById('scanResults');

    button.disabled = true;
    button.textContent = 'Scanning...';
    resultsElement.classList.add('hidden');

    apiRequest('/api/upload/scan_directory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory_path: selectedUploadDirectory })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                document.getElementById('scanFileCount').textContent = data.file_count.toLocaleString();
                document.getElementById('scanTotalSize').textContent = data.total_size_formatted;
                resultsElement.classList.remove('hidden');

                if (data.file_count > 0) {
                    document.getElementById('startUpload').disabled = false;
                    showNotification('Success', `Found ${data.file_count} files (${data.total_size_formatted})`, 'success');
                } else {
                    showNotification('Info', 'No files found in selected directory', 'info');
                }
            } else {
                throw new Error(data.error);
            }
        })
        .catch(error => {
            showNotification('Error', 'Failed to scan directory: ' + error.message, 'error');
        })
        .finally(() => {
            button.disabled = false;
            button.textContent = 'Scan Selected Directory';
        });
}

/* --------------------------------------------------------------------------
   Upload Process
   -------------------------------------------------------------------------- */

function createMetadataFile(uploadDirectory) {
    const metadata = {
        name: document.getElementById('metadataName').value.trim(),
        date: document.getElementById('metadataDate').value,
        location: document.getElementById('metadataLocation').value.trim(),
        field: document.getElementById('metadataField').value.trim(),
        weather: document.getElementById('metadataWeather').value.trim(),
        crop: document.getElementById('metadataCrop').value.trim(),
        expected_weeds: document.getElementById('metadataWeeds').value.trim(),
        notes: document.getElementById('metadataNotes').value.trim()
    };

    return apiRequest('/api/upload/create_metadata', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            metadata: metadata,
            upload_directory: uploadDirectory
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                return data;
            } else {
                throw new Error(data.error);
            }
        });
}

function startUploadProcess() {
    const accessKey = document.getElementById('accessKey').value.trim();
    const secretKey = document.getElementById('secretKey').value.trim();
    const bucketName = document.getElementById('bucketName').value.trim();
    const region = document.getElementById('region').value;
    const s3Prefix = document.getElementById('s3Prefix').value.trim();
    const endpointUrl = document.getElementById('endpointUrl').value.trim() || null;

    if (!accessKey || !secretKey || !bucketName || !selectedUploadDirectory) {
        showNotification('Error', 'Please complete all required fields and select a directory', 'error');
        return;
    }

    createMetadataFile(selectedUploadDirectory)
        .then(() => {
            showNotification('Info', 'Metadata file created, starting upload...', 'info');

            return apiRequest('/api/upload/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    directory_path: selectedUploadDirectory,
                    access_key: accessKey,
                    secret_key: secretKey,
                    bucket_name: bucketName,
                    s3_prefix: s3Prefix,
                    region: region,
                    endpoint_url: endpointUrl,
                    max_workers: 4
                })
            });
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                uploadInProgress = true;
                showUploadProgress();
                startUploadProgressMonitoring();

                document.getElementById('startUpload').disabled = true;
                document.getElementById('stopUpload').disabled = false;

                showNotification('Success', 'Upload started successfully with metadata', 'success');
            } else {
                throw new Error(data.error);
            }
        })
        .catch(error => {
            showNotification('Error', 'Failed to start upload: ' + error.message, 'error');
        });
}

function stopUploadProcess() {
    apiRequest('/api/upload/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Info', 'Upload stopped', 'info');
            }
        })
        .catch(error => {
            showNotification('Error', 'Failed to stop upload: ' + error.message, 'error');
        });
}

function showUploadProgress() {
    document.getElementById('uploadProgress').classList.remove('hidden');
    document.getElementById('uploadResults').classList.add('hidden');
}

function startUploadProgressMonitoring() {
    if (uploadProgressInterval) {
        clearInterval(uploadProgressInterval);
    }

    uploadProgressInterval = setInterval(() => {
        if (!uploadInProgress) return;

        apiRequest('/api/upload/progress')
            .then(response => response.json())
            .then(data => {
                updateUploadProgress(data);

                if (data.status === 'completed' || data.status === 'failed' || data.status === 'cancelled') {
                    uploadInProgress = false;
                    clearInterval(uploadProgressInterval);
                    showUploadComplete(data);
                }
            })
            .catch(error => {
                // Silent fail for progress polling
            });
    }, 1000);
}

function updateUploadProgress(data) {
    document.getElementById('uploadStatus').textContent = data.status.charAt(0).toUpperCase() + data.status.slice(1);

    const progressPercent = Math.round(data.progress_percent || 0);
    document.getElementById('overallProgressBar').style.width = progressPercent + '%';
    document.getElementById('overallProgressText').textContent = progressPercent + '%';

    document.getElementById('fileProgressText').textContent = `${data.completed_files} / ${data.total_files}`;

    document.getElementById('uploadSpeed').textContent = `${data.speed_mbps.toFixed(1)} MB/s`;
    document.getElementById('uploadETA').textContent = formatTime(data.eta_seconds);
    document.getElementById('currentFile').textContent = data.current_file || '--';
}

function showUploadComplete(data) {
    document.getElementById('uploadProgress').classList.add('hidden');
    document.getElementById('uploadResults').classList.remove('hidden');

    const successCount = data.completed_files - data.failed_files;
    document.getElementById('successCount').textContent = successCount;
    document.getElementById('failedCount').textContent = data.failed_files;
    document.getElementById('totalTime').textContent = formatTime(data.elapsed_seconds);

    document.getElementById('startUpload').disabled = false;
    document.getElementById('stopUpload').disabled = true;

    if (data.status === 'completed') {
        showNotification('Success', `Upload completed! ${successCount} files uploaded successfully`, 'success', 10000);
    } else if (data.status === 'failed') {
        showNotification('Error', `Upload failed: ${data.error_message}`, 'error', 10000);
    } else if (data.status === 'cancelled') {
        showNotification('Info', 'Upload was cancelled', 'info');
    } else {
        showNotification('Warning', `Upload completed with ${data.failed_files} failures`, 'warning', 8000);
    }
}
