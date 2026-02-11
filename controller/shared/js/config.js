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
        'relay_num': { type: 'number', min: 1, max: 8, help: 'Number of relays (1-8)' },
        'actuation_duration': { type: 'number', step: 0.01, min: 0.01, max: 2.0, help: 'Spray duration in seconds' },
        'delay': { type: 'number', step: 0.01, min: 0, max: 5.0, help: 'Delay before actuation' }
    },
    'MQTT': {
        'enable': { type: 'boolean', help: 'Enable MQTT communication' },
        'broker_ip': { type: 'text', help: 'MQTT broker IP address' },
        'broker_port': { type: 'number', min: 1, max: 65535, help: 'MQTT broker port' },
        'device_id': { type: 'text', help: 'Device identifier' }
    },
    'Camera': {
        'resolution_width': { type: 'select', options: ['320', '640', '800', '1024', '1280', '1920'], help: 'Width' },
        'resolution_height': { type: 'select', options: ['240', '480', '600', '768', '720', '1080'], help: 'Height' },
        'exp_compensation': { type: 'number', min: -10, max: 10, help: 'Exposure compensation' }
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
        'detection_enable': { type: 'boolean' }
    },
    'Controller': {
        'controller_type': { type: 'select', options: ['none', 'ute', 'advanced', 'networked'] }
    },
    'GPS': {
        'source': { type: 'select', options: ['none', 'dashboard', 'hat'] },
        'port': { type: 'text' },
        'baudrate': { type: 'select', options: ['4800', '9600', '19200', '38400', '57600', '115200'] }
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
        'mode': { type: 'select', options: ['dhcp', 'static'], help: 'Network mode' },
        'static_ip': { type: 'text', help: 'Static IP address (if mode=static)' },
        'controller_ip': { type: 'text', help: 'Central controller IP address' }
    },
    'WebDashboard': {
        'port': { type: 'number', min: 1, max: 65535, help: 'Dashboard web server port' }
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
const SECTION_ORDER = ['System', 'Camera', 'GreenOnBrown', 'GreenOnGreen', 'Actuation', 'DataCollection', 'Visualisation', 'Controller', 'Network', 'WebDashboard', 'MQTT', 'GPS', 'Relays'];

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
        Object.entries(sectionData).forEach(([key, value]) => {
            body.appendChild(createConfigField(sectionName, key, value, fieldDefs[key]));
        });
    }

    section.appendChild(header);
    section.appendChild(body);

    // Attach change listeners
    if (onFieldChange) {
        section.querySelectorAll('input, select').forEach(input => {
            input.addEventListener('change', onFieldChange);
            input.addEventListener('input', onFieldChange);
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

    const def = fieldDef || { type: 'text' };
    const id = 'config-' + section + '-' + key;
    const strValue = String(value);
    let html = '';

    if (def.type === 'boolean' || strValue.toLowerCase() === 'true' || strValue.toLowerCase() === 'false') {
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
function getOrderedSections(configData) {
    const order = [...SECTION_ORDER];
    Object.keys(configData).forEach(s => {
        if (!order.includes(s)) order.push(s);
    });
    return order.filter(s => configData[s]);
}
