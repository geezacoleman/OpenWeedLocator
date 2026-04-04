/* ==========================================================================
   OWL Controllers - Shared Config Editor
   CONFIG_FIELD_DEFS, section/field rendering, and change tracking

   Usage: Include after utils.js. Both standalone and networked controllers
   use these definitions and functions for INI config editing.
   ========================================================================== */

/**
 * Field definitions for all INI config sections.
 * Defines type, constraints, and help text for each config parameter.
 */
const CONFIG_FIELD_DEFS = {
    'System': {
        'algorithm': { type: 'select', options: ['exhsv', 'exg', 'hsv', 'gog', 'gog-hybrid'], help: 'Detection algorithm' },
        'input_file_or_directory': { type: 'text', help: 'Leave empty for camera input' },
        'relay_num': { type: 'select', options: ['1', '2', '4', '8', '12', '16'], help: 'Number of relays' },
        'actuation_duration': { type: 'number', step: 0.01, min: 0.01, max: 2.0, help: 'Spray duration in seconds' },
        'delay': { type: 'number', step: 0.01, min: 0, max: 5.0, help: 'Delay before actuation' },
        'actuation_zone': { type: 'number', min: 1, max: 100, help: 'Actuation zone (% of frame from bottom)' }
    },
    'MQTT': {
        'enable': { type: 'boolean', help: 'Enable MQTT communication' },
        'broker_ip': { type: 'text', help: 'MQTT broker IP address' },
        'broker_port': { type: 'number', min: 1, max: 65535, help: 'MQTT broker port' },
        'device_id': { type: 'text', help: 'Device identifier' }
    },
    'Camera': {
        '_resolution': {
            type: 'resolution',
            options: ['1456x1088', '1280x960', '1280x720', '1024x768', '800x600', '640x480', '416x320', '320x240'],
            help: 'Camera resolution (width x height)',
            keys: { width: 'resolution_width', height: 'resolution_height' }
        },
        'exp_compensation': { type: 'select', options: ['-4', '-3', '-2', '-1', '0', '1', '2', '3', '4'], help: 'Exposure compensation' },
        'crop_factor_horizontal': { type: 'number', step: 0.01, min: 0, max: 0.5, help: 'Horizontal crop factor' },
        'crop_factor_vertical': { type: 'number', step: 0.01, min: 0, max: 0.5, help: 'Vertical crop factor' },
        'camera_type': { type: 'select', options: ['auto', 'rpi', 'usb'], help: 'Camera type (auto detects Pi camera or USB webcam)' }
    },
    'GreenOnBrown': {
        'exg_min': { type: 'number', min: 0, max: 255 },
        'exg_max': { type: 'number', min: 0, max: 255 },
        'hue_min': { type: 'number', min: 0, max: 179 },
        'hue_max': { type: 'number', min: 0, max: 179 },
        'saturation_min': { type: 'number', min: 0, max: 255 },
        'saturation_max': { type: 'number', min: 0, max: 255 },
        'brightness_min': { type: 'number', min: 0, max: 255 },
        'brightness_max': { type: 'number', min: 0, max: 255 },
        'min_detection_area': { type: 'number', min: 1, max: 10000 },
        'invert_hue': { type: 'boolean' }
    },
    'GreenOnGreen': {
        'model_path': { type: 'text', help: 'Path to YOLO model (NCNN dir or .pt file)' },
        'confidence': { type: 'number', step: 0.05, min: 0.1, max: 1.0, help: 'Detection confidence threshold' },
        'detect_classes': { type: 'text', help: 'Classes to detect (comma-separated names, empty = all)' },
        'actuation_mode': { type: 'select', options: ['centre', 'zone'], help: 'centre = box centre, zone = mask pixel coverage per lane' },
        'min_detection_pixels': { type: 'number', min: 1, max: 10000, help: 'Min weed pixels in lane to trigger relay (zone mode only)' },
        'inference_resolution': { type: 'number', min: 160, max: 1280, help: 'YOLO input resolution (lower = faster)' },
        'crop_buffer_px': { type: 'number', min: 0, max: 50, help: 'Buffer around detected crop in pixels (hybrid mode)' }
    },
    'DataCollection': {
        'image_sample_enable': { type: 'boolean', help: 'Enable image saving' },
        'sample_method': { type: 'select', options: ['whole', 'bbox', 'square'] },
        'sample_frequency': { type: 'number', min: 1, max: 1000 },
        'save_directory': { type: 'text', help: 'Save directory path' },
        'detection_enable': { type: 'boolean' },
        'log_fps': { type: 'boolean', help: 'Log FPS to console' },
        'camera_name': { type: 'text', help: 'Camera identifier for saved images' }
    },
    'Controller': {
        'controller_type': { type: 'select', options: ['none', 'ute', 'advanced', 'networked'] },
        'switch_purpose': { type: 'select', options: ['recording', 'detection'], help: 'Physical switch function' },
        'switch_pin': { type: 'number', min: 1, max: 40, help: 'GPIO BCM pin for switch' },
        'detection_mode_pin_up': { type: 'number', min: 1, max: 40, help: 'GPIO BCM pin for detection mode up' },
        'detection_mode_pin_down': { type: 'number', min: 1, max: 40, help: 'GPIO BCM pin for detection mode down' },
        'recording_pin': { type: 'number', min: 1, max: 40, help: 'GPIO BCM pin for recording switch' },
        'sensitivity_pin': { type: 'number', min: 1, max: 40, help: 'GPIO BCM pin for sensitivity switch' }
    },
    'GPS': {
        'source': { type: 'select', options: ['none', 'dashboard', 'hat'], help: 'GPS data source for owl.py' },
        'port': { type: 'text', help: 'Serial GPS device path' },
        'baudrate': { type: 'select', options: ['4800', '9600', '19200', '38400', '57600', '115200'] },
        'enable': { type: 'boolean', help: 'Enable GPS server (networked controller)' },
        'nmea_port': { type: 'number', min: 1, max: 65535, help: 'TCP port for NMEA GPS data' },
        'boom_width': { type: 'number', step: 0.5, min: 1, max: 50, help: 'Boom width in metres' },
        'track_save_directory': { type: 'text', help: 'Directory for GPS track files' }
    },
    'Actuation': {
        'actuation_duration': { type: 'number', step: 0.01, min: 0.01, max: 5.0, help: 'Spray duration in seconds' },
        'delay': { type: 'number', step: 0.01, min: 0, max: 5.0, help: 'Delay before actuation' },
        'actuation_length_cm': { type: 'number', min: 1, max: 100, help: 'Spray zone length in cm' },
        'offset_cm': { type: 'number', min: 0, max: 200, help: 'Nozzle offset from camera in cm' },
        'speed_avg_window': { type: 'number', step: 0.5, min: 1, max: 30, help: 'GPS speed averaging window in seconds' }
    },
    'Visualisation': {
        'image_loop_time': { type: 'number', step: 0.1, min: 0.1, max: 10.0, help: 'Display loop time in seconds' }
    },
    'Network': {
        'mode': { type: 'select', options: ['dhcp', 'static', 'networked'], help: 'Network mode' },
        'static_ip': { type: 'text', help: 'Static IP address (if mode=static)' },
        'controller_ip': { type: 'text', help: 'Central controller IP address' }
    },
    'WebDashboard': {
        'port': { type: 'number', min: 1, max: 65535, help: 'Dashboard web server port' }
    },
    'Tracking': {
        'tracking_enabled': { type: 'boolean', help: 'Enable weed tracking (class smoothing + crop mask persistence)' },
        'track_high_thresh': { type: 'number', min: 0.01, max: 0.5, step: 0.01, help: 'First-pass confidence threshold (lower = more detections matched)' },
        'track_low_thresh': { type: 'number', min: 0.01, max: 0.3, step: 0.01, help: 'Second-pass threshold for marginal detections' },
        'new_track_thresh': { type: 'number', min: 0.01, max: 0.5, step: 0.01, help: 'Minimum confidence to start a new track' },
        'track_buffer': { type: 'number', min: 10, max: 150, step: 5, help: 'Frames to keep lost tracks alive (higher = more persistent)' },
        'match_thresh': { type: 'number', min: 0.1, max: 0.95, step: 0.05, help: 'IoU matching threshold (lower = tolerates more camera vibration)' },
        'track_class_window': { type: 'number', min: 1, max: 20, help: 'Frames of class history for majority vote' },
        'track_crop_persist': { type: 'number', min: 1, max: 10, help: 'Frames to persist crop mask after detection drops' },
        'detection_persist_frames': { type: 'number', min: 0, max: 15, step: 1, help: 'Frames to persist lost detections via Kalman prediction (0=disabled, 5=~0.5s)' }
    },
    'Relays': { _isRelaySection: true }
};

/**
 * Sections that require a service restart when changed.
 */
const RESTART_SECTIONS = ['MQTT', 'Network', 'WebDashboard', 'Controller'];

/**
 * Preferred display order for config sections.
 */
const SECTION_ORDER = ['System', 'Camera', 'GreenOnBrown', 'GreenOnGreen', 'Tracking', 'Actuation', 'DataCollection', 'Visualisation', 'Controller', 'Network', 'WebDashboard', 'MQTT', 'GPS', 'Relays', 'Sensitivity'];

/**
 * Create a collapsible config section element.
 * @param {string} sectionName - The INI section name
 * @param {Object} sectionData - Key-value pairs for the section
 * @param {Function} onFieldChange - Callback for field changes: (event) => void
 * @returns {HTMLElement} - The section DOM element
 */
function createConfigSection(sectionName, sectionData, onFieldChange) {
    const section = document.createElement('div');
    section.className = 'config-section';

    const fieldDefs = CONFIG_FIELD_DEFS[sectionName] || {};
    const hasWarning = RESTART_SECTIONS.includes(sectionName);

    // Header
    const header = document.createElement('div');
    header.className = 'config-section-header';
    header.innerHTML = '<h3>' + sectionName +
        (hasWarning ? ' <span class="section-badge warning">Restart</span>' : '') +
        '</h3>';
    header.addEventListener('click', () => section.classList.toggle('collapsed'));

    // Body
    const body = document.createElement('div');
    body.className = 'config-section-body';

    // MQTT protection: add warning banner and lock enable field
    if (sectionName === 'MQTT') {
        const warning = document.createElement('div');
        warning.className = 'config-section-warning';
        warning.textContent = 'Changing MQTT settings may disconnect this device from the controller.';
        body.appendChild(warning);
    }

    if (fieldDefs._isRelaySection) {
        body.innerHTML = '<div class="relay-mapping"></div>';
        const rc = body.querySelector('.relay-mapping');
        Object.entries(sectionData).forEach(([key, value]) => {
            const item = document.createElement('div');
            item.className = 'relay-item';
            item.innerHTML = '<label>Relay ' + key + ':</label>' +
                '<input type="number" data-section="' + sectionName +
                '" data-key="' + key + '" value="' + value + '" min="1" max="40">';
            rc.appendChild(item);
        });
    } else {
        // Check for virtual fields (like _resolution) that combine multiple data keys
        const renderedKeys = new Set();
        Object.entries(fieldDefs).forEach(([defKey, def]) => {
            if (def && def.type === 'resolution' && def.keys) {
                const w = sectionData[def.keys.width] || '';
                const h = sectionData[def.keys.height] || '';
                body.appendChild(createConfigField(sectionName, defKey, w + 'x' + h, def));
                renderedKeys.add(def.keys.width);
                renderedKeys.add(def.keys.height);
            }
        });
        Object.entries(sectionData).forEach(([key, value]) => {
            if (renderedKeys.has(key)) return; // Already rendered as combined
            body.appendChild(createConfigField(sectionName, key, value, fieldDefs[key]));
        });
    }

    section.appendChild(header);
    section.appendChild(body);

    // Attach change listeners
    if (onFieldChange) {
        section.querySelectorAll('input, select').forEach(input => {
            // Resolution dropdown: split value and fire events for both keys
            if (input.dataset.resolutionWidth) {
                input.addEventListener('change', (e) => {
                    const parts = e.target.value.split('x');
                    if (parts.length === 2) {
                        _fireConfigChange(onFieldChange, sectionName, input.dataset.resolutionWidth, parts[0]);
                        _fireConfigChange(onFieldChange, sectionName, input.dataset.resolutionHeight, parts[1]);
                    }
                });
            } else {
                input.addEventListener('change', onFieldChange);
                input.addEventListener('input', onFieldChange);
            }
        });
    }

    return section;
}

/**
 * Create a single config field element.
 * @param {string} section - INI section name
 * @param {string} key - Config key
 * @param {*} value - Current value
 * @param {Object} [fieldDef] - Field definition from CONFIG_FIELD_DEFS
 * @returns {HTMLElement} - The field DOM element
 */
function createConfigField(section, key, value, fieldDef) {
    const field = document.createElement('div');
    field.className = 'config-field';

    // Infer type from value when no field definition exists (safety net)
    var def;
    if (fieldDef) {
        def = fieldDef;
    } else if (String(value).toLowerCase() === 'true' || String(value).toLowerCase() === 'false') {
        def = { type: 'boolean' };
    } else if (value !== '' && !isNaN(Number(value))) {
        def = { type: 'number' };
    } else {
        def = { type: 'text' };
    }
    const id = 'config-' + section + '-' + key;
    const strValue = String(value);
    let html = '';

    if (def.type === 'resolution' && def.options && def.keys) {
        // Combined resolution dropdown — sets two underlying keys
        const opts = def.options.map(o =>
            '<option value="' + o + '"' + (o === strValue ? ' selected' : '') + '>' + o + '</option>'
        ).join('');
        html = '<label for="' + id + '">Resolution</label>' +
            '<select id="' + id + '" data-section="' + section +
            '" data-key="' + key + '" data-resolution-width="' + def.keys.width +
            '" data-resolution-height="' + def.keys.height + '">' + opts + '</select>';
    } else if (def.type === 'boolean' || strValue.toLowerCase() === 'true' || strValue.toLowerCase() === 'false') {
        html = '<div class="checkbox-wrapper">' +
            '<input type="checkbox" id="' + id + '" data-section="' + section +
            '" data-key="' + key + '"' + (strValue.toLowerCase() === 'true' ? ' checked' : '') + '>' +
            '<label for="' + id + '">' + formatLabel(key) + '</label></div>';
    } else if (def.type === 'select' && def.options) {
        const opts = def.options.map(o =>
            '<option value="' + o + '"' + (String(o) === strValue ? ' selected' : '') + '>' + o + '</option>'
        ).join('');
        html = '<label for="' + id + '">' + formatLabel(key) + '</label>' +
            '<select id="' + id + '" data-section="' + section + '" data-key="' + key + '">' + opts + '</select>';
    } else if (def.type === 'number') {
        const attrs = (def.min !== undefined ? ' min="' + def.min + '"' : '') +
            (def.max !== undefined ? ' max="' + def.max + '"' : '') +
            (def.step !== undefined ? ' step="' + def.step + '"' : '');
        html = '<label for="' + id + '">' + formatLabel(key) + '</label>' +
            '<input type="number" id="' + id + '" data-section="' + section +
            '" data-key="' + key + '" value="' + value + '"' + attrs + '>';
    } else {
        html = '<label for="' + id + '">' + formatLabel(key) + '</label>' +
            '<input type="text" id="' + id + '" data-section="' + section +
            '" data-key="' + key + '" value="' + value + '">';
    }

    field.innerHTML = html;

    if (def.help) {
        const h = document.createElement('span');
        h.className = 'field-help';
        h.textContent = def.help;
        field.appendChild(h);
    }

    // Lock MQTT enable field to prevent accidental disconnection
    if (section === 'MQTT' && key === 'enable') {
        const input = field.querySelector('input');
        if (input) {
            input.disabled = true;
            field.classList.add('config-field-locked');
        }
        const note = document.createElement('span');
        note.className = 'field-help';
        note.textContent = 'Locked — disabling MQTT will disconnect this device';
        field.appendChild(note);
    }

    return field;
}

/**
 * Get ordered section names, putting known sections first.
 * @param {Object} configData - Full config object with section keys
 * @returns {string[]} - Ordered section names
 */
/**
 * Fire a synthetic config change event for a specific section/key/value.
 * Used by resolution dropdown to split into two underlying keys.
 */
function _fireConfigChange(callback, section, key, value) {
    const fakeEvent = {
        target: {
            dataset: { section: section, key: key },
            value: value,
            type: 'select-one',
            checked: false,
            tagName: 'SELECT'
        }
    };
    callback(fakeEvent);
}

function getOrderedSections(configData) {
    const order = [...SECTION_ORDER];
    // Append Sensitivity_* sections at end
    Object.keys(configData).forEach(s => {
        if (!order.includes(s)) order.push(s);
    });
    return order.filter(s => configData[s]);
}
