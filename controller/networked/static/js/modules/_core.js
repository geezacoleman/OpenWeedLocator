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
    min_detection_area: { value: 0, min: 1, max: 1000 },
    // Min weed size as % of the detection frame — log scale because useful
    // values span ~3 orders of magnitude (0.0005% ≈ 1px … 2% ≈ huge patch)
    min_detection_area_percent: { value: 0.003, min: 0.0005, max: 2, scale: 'log', decimals: 4, unit: '%' },
    lut_sensitivity: { value: 50, min: 0, max: 100 },
    crop_buffer_px: { value: 20, min: 0, max: 50 },
    confidence: { value: 50, min: 5, max: 100 }
};

// ── Log-scale aware slider maths (linear when p.scale is undefined) ──

function roundParamValue(p, val) {
    if (p.decimals) {
        const f = Math.pow(10, p.decimals);
        return Math.round(val * f) / f;
    }
    return Math.round(val);
}

function sliderPctToValue(p, pct) {
    const val = (p.scale === 'log')
        ? p.min * Math.pow(p.max / p.min, pct / 100)
        : (pct / 100) * (p.max - p.min) + p.min;
    return roundParamValue(p, Math.max(p.min, Math.min(p.max, val)));
}

function sliderValueToPct(p, val) {
    const v = Math.max(p.min, Math.min(p.max, val));
    return (p.scale === 'log')
        ? Math.log(v / p.min) / Math.log(p.max / p.min) * 100
        : ((v - p.min) / (p.max - p.min)) * 100;
}

function sliderStepValue(p, val, delta) {
    // Fine-tune: additive for linear params, multiplicative for log params
    const next = (p.scale === 'log') ? val * Math.pow(1.12, delta) : val + delta;
    return roundParamValue(p, Math.max(p.min, Math.min(p.max, next)));
}

function sliderDisplayValue(p) {
    const text = p.decimals ? Number(p.value).toFixed(p.decimals) : p.value;
    return p.unit ? text + p.unit : String(text);
}

// Global detection state
let globalDetectionEnabled = false;
let globalRecordingEnabled = false;
let globalNozzlesActive = false;
let globalTrackingEnabled = false;
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
