// ============================================
// OWL Central Controller - Core State & Commands
// Global state, constants, sendCommand()
// ============================================

let owlsData = {};
let mqttConnected = false;
let updateInterval = null;
const UPDATE_INTERVAL = 2000;

// Configuration parameters - initialized empty, loaded from API
// Single source of truth: /api/greenonbrown/defaults
const configParams = {
    exg_min: { value: 0, min: 0, max: 255 },
    exg_max: { value: 0, min: 0, max: 255 },
    hue_min: { value: 0, min: 0, max: 179 },
    hue_max: { value: 0, min: 0, max: 179 },
    saturation_min: { value: 0, min: 0, max: 255 },
    saturation_max: { value: 0, min: 0, max: 255 },
    brightness_min: { value: 0, min: 0, max: 255 },
    brightness_max: { value: 0, min: 0, max: 255 },
    min_detection_area: { value: 0, min: 1, max: 1000 }
};

// Global detection state
let globalDetectionEnabled = false;
let globalRecordingEnabled = false;
let currentVideoDeviceId = null; // Track which device's video is showing

// ============================================
// COMMAND SENDING
// ============================================

async function sendCommand(deviceId, action, value = null) {
    try {
        const payload = {
            device_id: deviceId,
            action: action
        };

        if (value !== null) {
            payload.value = value;
        }

        const res = await fetch('/api/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const result = await res.json();

        if (!result.success) {
            console.error('Command failed:', result.error);
            showToast('Command failed: ' + result.error, 'error');
        } else {
            setTimeout(updateDashboard, 400);
        }

        return result;
    } catch (err) {
        console.error('Error sending command:', err);
        showToast('Error sending command', 'error');
        return { success: false, error: err.message };
    }
}
