// OWL Central Controller JavaScript
// Handles UI updates, MQTT status, and command sending

// Global state
let owlsData = {};
let mqttConnected = false;
let updateInterval = null;
let configDefaults = {};

// Constants
const UPDATE_INTERVAL = 2000; // Update every 2 seconds
const OFFLINE_THRESHOLD = 10; // Seconds before marking offline

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    console.log('OWL Central Controller initializing...');

    // Setup tab switching
    setupTabs();

    // Load config defaults
    loadConfigDefaults();

    // Start polling for OWL data
    startPolling();

    // Setup target OWL selector change handler
    const targetSelect = document.getElementById('target-owl-select');
    if (targetSelect) {
        targetSelect.addEventListener('change', updateConfigSliders);
    }
});

// Tab Switching
function setupTabs() {
    const tabs = document.querySelectorAll('.nav-tab');

    tabs.forEach(tab => {
        tab.addEventListener('click', function() {
            const targetTab = this.getAttribute('data-tab');

            // Update tab active states
            tabs.forEach(t => t.classList.remove('active'));
            this.classList.add('active');

            // Update content active states
            document.querySelectorAll('.tab-content').forEach(content => {
                content.classList.remove('active');
            });
            document.getElementById(`tab-${targetTab}`).classList.add('active');
        });
    });
}

// Start polling for updates
function startPolling() {
    // Initial update
    updateDashboard();

    // Set up interval
    updateInterval = setInterval(updateDashboard, UPDATE_INTERVAL);
}

// Main dashboard update function
async function updateDashboard() {
    try {
        const response = await fetch('/api/owls');

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();

        // Update global state
        owlsData = data.owls || {};
        mqttConnected = data.mqtt_connected || false;

        // Update UI
        updateMQTTStatus();
        updateOWLCount();
        updateOWLGrid();
        updateTargetSelector();
        updateLastUpdate();

    } catch (error) {
        console.error('Error updating dashboard:', error);
        mqttConnected = false;
        updateMQTTStatus();
    }
}

// Update MQTT connection status indicator
function updateMQTTStatus() {
    const statusDot = document.getElementById('mqtt-status-dot');
    const statusText = document.getElementById('mqtt-status-text');

    if (mqttConnected) {
        statusDot.classList.add('connected');
        statusText.textContent = 'MQTT Connected';
    } else {
        statusDot.classList.remove('connected');
        statusText.textContent = 'MQTT Disconnected';
    }
}

// Update OWL count
function updateOWLCount() {
    const countElement = document.getElementById('owl-count');
    const count = Object.keys(owlsData).length;
    countElement.textContent = count;
}

// Update OWL grid with cards
function updateOWLGrid() {
    const grid = document.getElementById('owls-grid');

    // Check if we have any OWLs
    if (Object.keys(owlsData).length === 0) {
        grid.innerHTML = `
            <div class="empty-state" style="grid-column: 1 / -1;">
                <div class="empty-state-icon">🔍</div>
                <div class="empty-state-text">No OWLs Connected</div>
                <div class="empty-state-subtext">Waiting for OWLs to publish their state via MQTT...</div>
            </div>
        `;
        return;
    }

    // Build grid HTML
    let gridHTML = '';

    for (const [deviceId, owl] of Object.entries(owlsData)) {
        gridHTML += buildOWLCard(deviceId, owl);
    }

    grid.innerHTML = gridHTML;
}

// Build individual OWL card HTML
function buildOWLCard(deviceId, owl) {
    const isOnline = owl.connected || false;
    const onlineClass = isOnline ? 'online' : 'offline';

    // Extract stats
    const temp = owl.temp || owl.system?.temp || '--';
    const fanSpeed = owl.fan_speed || owl.system?.fan_speed || '--';
    const detectionEnabled = owl.detection_enable || false;
    const recordingEnabled = owl.image_sample_enable || false;
    const lastSeen = owl.last_seen_formatted || 'Never';

    // Format temperature with color
    let tempClass = 'good';
    if (typeof temp === 'number') {
        if (temp > 70) tempClass = 'danger';
        else if (temp > 60) tempClass = 'warning';
    }

    // Button states
    const detectionBtnClass = detectionEnabled ? 'btn-detection active' : 'btn-detection inactive';
    const detectionBtnText = detectionEnabled ? '✓ Detection ON' : '✗ Detection OFF';

    const recordingBtnClass = recordingEnabled ? 'btn-recording active' : 'btn-recording inactive';
    const recordingBtnText = recordingEnabled ? '⏺ Recording ON' : '○ Recording OFF';

    const disabledAttr = isOnline ? '' : 'disabled';

    return `
        <div class="owl-card ${onlineClass}">
            <div class="owl-card-header">
                <div class="owl-card-title">
                    <h3>${deviceId}</h3>
                    <span class="owl-status-badge ${onlineClass}">
                        <span class="badge-dot"></span>
                        ${isOnline ? 'Online' : 'Offline'}
                    </span>
                </div>
            </div>
            
            <div class="owl-stats">
                <div class="owl-stat-item">
                    <div class="owl-stat-label">Temperature</div>
                    <div class="owl-stat-value ${tempClass}">${temp}°C</div>
                </div>
                <div class="owl-stat-item">
                    <div class="owl-stat-label">Fan Speed</div>
                    <div class="owl-stat-value">${fanSpeed}%</div>
                </div>
                <div class="owl-stat-item" style="grid-column: 1 / -1;">
                    <div class="owl-stat-label">Last Seen</div>
                    <div class="owl-stat-value" style="font-size: 0.9rem;">${lastSeen}</div>
                </div>
            </div>
            
            <div class="owl-actions">
                <button class="owl-btn btn-video" onclick="openVideoFeed('${deviceId}')" ${disabledAttr}>
                    <span class="btn-icon">🎥</span>
                    Video
                </button>
                <button class="owl-btn ${detectionBtnClass}" onclick="toggleDetection('${deviceId}')" ${disabledAttr}>
                    ${detectionBtnText}
                </button>
                <button class="owl-btn ${recordingBtnClass}" onclick="toggleRecording('${deviceId}')" ${disabledAttr}>
                    ${recordingBtnText}
                </button>
            </div>
        </div>
    `;
}

// Update target OWL selector dropdown
function updateTargetSelector() {
    const select = document.getElementById('target-owl-select');
    if (!select) return;

    const currentValue = select.value;

    // Build options
    let optionsHTML = '<option value="all">All OWLs</option>';

    for (const deviceId of Object.keys(owlsData)) {
        optionsHTML += `<option value="${deviceId}">${deviceId}</option>`;
    }

    select.innerHTML = optionsHTML;

    // Restore selection if it still exists
    if (currentValue && Array.from(select.options).some(opt => opt.value === currentValue)) {
        select.value = currentValue;
    }
}

// Update last update timestamp
function updateLastUpdate() {
    const lastUpdateElement = document.getElementById('last-update');
    if (lastUpdateElement) {
        const now = new Date();
        lastUpdateElement.textContent = `Last update: ${now.toLocaleTimeString()}`;
    }
}

// Load default GreenOnBrown config values
async function loadConfigDefaults() {
    try {
        const response = await fetch('/api/greenonbrown/defaults');
        const data = await response.json();
        configDefaults = data;

        // Build sliders
        buildConfigSliders();

    } catch (error) {
        console.error('Error loading config defaults:', error);
    }
}

// Build configuration sliders
function buildConfigSliders() {
    const container = document.getElementById('greenonbrown-controls');
    if (!container) return;

    let html = '';

    for (const [key, config] of Object.entries(configDefaults)) {
        html += `
            <div class="slider-group">
                <div class="slider-label">
                    <span class="slider-label-text">${config.label}</span>
                    <span class="slider-value" id="slider-value-${key}">${config.value}</span>
                </div>
                <input 
                    type="range" 
                    class="slider-input" 
                    id="slider-${key}"
                    min="${config.min}" 
                    max="${config.max}" 
                    step="${config.step}" 
                    value="${config.value}"
                    data-key="${key}"
                    onchange="sendConfigValue('${key}', this.value)"
                    oninput="updateSliderValue('${key}', this.value)"
                >
            </div>
        `;
    }

    container.innerHTML = html;
}

// Update slider value display (while dragging)
function updateSliderValue(key, value) {
    const valueDisplay = document.getElementById(`slider-value-${key}`);
    if (valueDisplay) {
        valueDisplay.textContent = value;
    }
}

// Update config sliders based on selected OWL
function updateConfigSliders() {
    const targetSelect = document.getElementById('target-owl-select');
    const selectedOwl = targetSelect.value;

    if (selectedOwl === 'all') {
        // Reset to defaults
        for (const [key, config] of Object.entries(configDefaults)) {
            const slider = document.getElementById(`slider-${key}`);
            if (slider) {
                slider.value = config.value;
                updateSliderValue(key, config.value);
            }
        }
    } else {
        // Load values from selected OWL if available
        const owl = owlsData[selectedOwl];
        if (owl && owl.config) {
            for (const [key, value] of Object.entries(owl.config)) {
                const slider = document.getElementById(`slider-${key}`);
                if (slider) {
                    slider.value = value;
                    updateSliderValue(key, value);
                }
            }
        }
    }
}

// Send configuration value to OWL(s)
async function sendConfigValue(key, value) {
    const targetSelect = document.getElementById('target-owl-select');
    const target = targetSelect.value;

    const payload = {
        device_id: target,
        action: 'set_config',
        value: {
            section: 'GreenOnBrown',
            key: key,
            value: parseInt(value)
        }
    };

    try {
        const response = await fetch('/api/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });

        const result = await response.json();

        if (result.success) {
            console.log(`Config ${key} set to ${value} for ${target}`);
        } else {
            console.error('Failed to send config:', result.error);
            showNotification(`Failed to update ${key}`, 'error');
        }

    } catch (error) {
        console.error('Error sending config:', error);
        showNotification('Network error', 'error');
    }
}

// Toggle detection for an OWL
async function toggleDetection(deviceId) {
    await sendCommand(deviceId, 'toggle_detection');
}

// Toggle recording for an OWL
async function toggleRecording(deviceId) {
    await sendCommand(deviceId, 'toggle_recording');
}

// Send command to OWL via MQTT
async function sendCommand(deviceId, action, value = null) {
    const payload = {
        device_id: deviceId,
        action: action
    };

    if (value !== null) {
        payload.value = value;
    }

    try {
        const response = await fetch('/api/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });

        const result = await response.json();

        if (result.success) {
            console.log(`Command ${action} sent to ${deviceId}`);
            // Force immediate update
            setTimeout(updateDashboard, 500);
        } else {
            console.error('Command failed:', result.error);
            showNotification(`Failed to ${action}`, 'error');
        }

    } catch (error) {
        console.error('Error sending command:', error);
        showNotification('Network error', 'error');
    }
}

// Open video feed modal
function openVideoFeed(deviceId) {
    const modal = document.getElementById('video-modal');
    const title = document.getElementById('video-modal-title');
    const img = document.getElementById('video-feed-img');

    // Set title
    title.textContent = `${deviceId} - Video Feed`;

    // Set video feed URL
    // Try .local address first, fallback to IP if available
    const videoUrl = `https://${deviceId}.local/video_feed`;
    img.src = videoUrl;

    // Show modal
    modal.style.display = 'flex';

    // Handle image load error - try alternative URLs
    img.onerror = function() {
        // Could try IP address as fallback if we stored it
        console.error(`Failed to load video feed from ${videoUrl}`);
        img.alt = 'Video feed unavailable. Ensure OWL is running and accessible.';
    };
}

// Close video feed modal
function closeVideoModal() {
    const modal = document.getElementById('video-modal');
    const img = document.getElementById('video-feed-img');

    // Hide modal
    modal.style.display = 'none';

    // Clear image source to stop loading
    img.src = '';
}

// Close modal when clicking outside
document.addEventListener('click', function(event) {
    const modal = document.getElementById('video-modal');
    if (event.target === modal) {
        closeVideoModal();
    }
});

// Show notification (simple toast)
function showNotification(message, type = 'info') {
    // Simple console notification for now
    // Could be enhanced with actual toast notifications
    console.log(`[${type.toUpperCase()}] ${message}`);
}

// Cleanup on page unload
window.addEventListener('beforeunload', function() {
    if (updateInterval) {
        clearInterval(updateInterval);
    }
});

console.log('OWL Central Controller JavaScript loaded');