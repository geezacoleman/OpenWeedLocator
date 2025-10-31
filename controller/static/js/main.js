document.addEventListener('DOMContentLoaded', () => {

    const owlsGrid = document.getElementById('owls-grid');
    const owlCount = document.getElementById('owl-count');
    const controlsContainer = document.getElementById('greenonbrown-controls');

    /**
     * Updates the dashboard with the latest OWL states.
     */
    async function updateOwlCards() {
        try {
            const response = await fetch('/api/owls');
            if (!response.ok) throw new Error('Failed to fetch OWL status');
            const owls = await response.json();

            const owlIds = Object.keys(owls);
            owlCount.textContent = owlIds.length;

            // Clear old cards
            owlsGrid.innerHTML = '';

            if (owlIds.length === 0) {
                owlsGrid.innerHTML = '<p>No OWLs are currently connected.</p>';
                return;
            }

            // Create a card for each OWL
            for (const id of owlIds) {
                const owl = owls[id];

                // Check if OWL is online (last seen within 10 seconds)
                const isOnline = (Date.now() / 1000 - owl.last_seen) < 10;

                const card = document.createElement('div');
                card.className = 'card detection-card'; // Use styles from your CSS

                const ip = owl.ip || 'Unknown IP';
                const detectStatus = owl.detection_enable ? 'Enabled' : 'Disabled';
                const statusColor = owl.detection_enable ? 'success' : 'danger';

                card.innerHTML = `
                    <div class="card-title-row">
                        <div class="title-left">
                            <h2>${id}</h2>
                            <span class="status-chip ${isOnline ? 'on' : 'off'}">
                                <span class="dot"></span>
                                <span>${isOnline ? 'Online' : 'Offline'}</span>
                            </span>
                        </div>
                    </div>
                    <div class="info-grid">
                        <div class="info-item">
                            <span class="info-label">IP Address:</span>
                            <span class="info-value">${ip}</span>
                        </div>
                        <div class="info-item">
                            <span class="info-label">Detection:</span>
                            <span class="info-value" style="color:var(--${statusColor})">${detectStatus}</span>
                        </div>
                    </div>
                    <div classs="browser-controls" style="margin-top: 1rem;">
                        <button class="btn-primary" data-owl-id="${id}" data-action="video">Video Feed</button>
                        <button class="btn-secondary" data-owl-id="${id}" data-action="toggle_detection">Toggle Detection</button>
                    </div>
                `;
                owlsGrid.appendChild(card);
            }

        } catch (error) {
            console.error(error);
            owlsGrid.innerHTML = '<p style="color:var(--danger);">Error loading OWL data. Is the controller running?</p>';
        }
    }

    /**
     * Sends a command to the Flask API.
     */
    async function sendCommand(deviceId, action, value = null) {
        try {
            await fetch('/api/command', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    device_id: deviceId,
                    action: action,
                    value: value
                })
            });

            // If it was a global config change, refresh all cards
            if (deviceId === 'all') {
                updateOwlCards();
            }
        } catch (error) {
            console.error('Failed to send command:', error);
        }
    }

    /**
     * Creates the [GreenOnBrown] control sliders.
     */
    function createGbGControls() {
        const controls = [
            { id: 'exg_min', label: 'ExG Min', min: 0, max: 255, value: 25 },
            { id: 'exg_max', label: 'ExG Max', min: 0, max: 255, value: 200 },
            { id: 'hue_min', label: 'Hue Min', min: 0, max: 179, value: 39 },
            { id: 'hue_max', label: 'Hue Max', min: 0, max: 179, value: 83 },
            { id: 'saturation_min', label: 'Sat Min', min: 0, max: 255, value: 50 },
            { id: 'saturation_max', label: 'Sat Max', min: 0, max: 255, value: 220 },
            { id: 'brightness_min', label: 'Val Min', min: 0, max: 255, value: 60 },
            { id: 'brightness_max', label: 'Val Max', min: 0, max: 255, value: 190 },
            { id: 'min_detection_area', label: 'Min Area', min: 0, max: 500, value: 10 }
        ];

        controlsContainer.innerHTML = ''; // Clear container

        controls.forEach(control => {
            const controlEl = document.createElement('div');
            controlEl.className = 'form-group';

            const label = document.createElement('label');
            label.htmlFor = control.id;
            label.innerHTML = `${control.label}: <span id="${control.id}-val">${control.value}</span>`;

            const slider = document.createElement('input');
            slider.type = 'range';
            slider.className = 'form-input'; // This might not be perfect for a slider, but uses your class
            slider.id = control.id;
            slider.min = control.min;
            slider.max = control.max;
            slider.value = control.value;

            // Update label on slider move
            slider.addEventListener('input', () => {
                document.getElementById(`${control.id}-val`).textContent = slider.value;
            });

            // Send command on slider release ('change' event)
            slider.addEventListener('change', () => {
                const configValue = {
                    key: control.id,
                    value: parseInt(slider.value, 10)
                };
                // Send to ALL devices
                sendCommand('all', 'set_greenonbrown_config', configValue);
            });

            controlEl.appendChild(label);
            controlEl.appendChild(slider);
            controlsContainer.appendChild(controlEl);
        });
    }

    // --- Event Listeners ---

    // Handle button clicks on the OWL cards
    owlsGrid.addEventListener('click', (e) => {
        const target = e.target;
        if (target.tagName === 'BUTTON') {
            const owlId = target.dataset.owlId;
            const action = target.dataset.action;

            if (action === 'video') {
                // In the future, this would open a new window or modal
                alert(`Opening video feed for ${owlId} (placeholder)`);
                // window.open(`/video_feed/${owlId}`, '_blank');
            } else if (action === 'toggle_detection') {
                sendCommand(owlId, 'toggle_detection');
                // Give the backend a moment to process before refreshing
                setTimeout(updateOwlCards, 250);
            }
        }
    });

    // --- Initial Setup ---
    createGbGControls();
    updateOwlCards();
    setInterval(updateOwlCards, 2000); // Refresh cards every 2 seconds
});