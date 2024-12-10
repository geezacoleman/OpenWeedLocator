let isRecording = false;
let recordingStartTime = null;
const MAX_RECORDING_TIME = 30; // seconds
let estimatedSize = 0;
const ESTIMATED_BITRATE = 500000; // 500 kbps - adjust based on your video quality

function updateRecordingStatus() {
    const statusElement = document.getElementById('recordingStatus');
    if (!statusElement) return;

    if (isRecording) {
        const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
        const remaining = MAX_RECORDING_TIME - elapsed;
        estimatedSize = Math.floor((elapsed * ESTIMATED_BITRATE) / (8 * 1024 * 1024)); // Convert to MB

        statusElement.innerHTML = `
            Recording: ${remaining}s remaining
            <br>
            Estimated Size: ~${estimatedSize}MB
        `;

        // Auto-stop if max time reached
        if (elapsed >= MAX_RECORDING_TIME) {
            toggleRecording();
        }
    }
}

function showProcessingSpinner() {
    const button = document.getElementById('recordButton');
    button.disabled = true;
    button.innerHTML = `
        <div class="spinner"></div>
        Processing...
    `;
}

function hideProcessingSpinner() {
    const button = document.getElementById('recordButton');
    button.disabled = false;
    button.textContent = 'Start Recording';
}

function toggleRecording() {
    const button = document.getElementById('recordButton');
    const statusElement = document.getElementById('recordingStatus');

    if (!isRecording) {
        // Start recording
        fetch('/start_recording', { method: 'POST' })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    isRecording = true;
                    recordingStartTime = Date.now();
                    button.textContent = 'Stop Recording';
                    button.classList.add('recording');
                    statusElement.style.display = 'block';
                    // Start status updates
                    setInterval(updateRecordingStatus, 1000);
                }
            })
            .catch(error => updateStatus(`Error: ${error.message}`));
    } else {
        // Stop recording and download
        showProcessingSpinner();

        fetch('/stop_recording', { method: 'POST' })
            .then(response => {
                if (!response.ok) {
                    throw new Error('Recording failed');
                }
                return response.blob();
            })
            .then(blob => {
                isRecording = false;
                hideProcessingSpinner();
                statusElement.style.display = 'none';

                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.style.display = 'none';
                a.href = url;
                const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
                a.download = `owl_recording_${timestamp}.mp4`;

                document.body.appendChild(a);
                a.click();
                window.URL.revokeObjectURL(url);
                document.body.removeChild(a);

                updateStatus('Recording saved and downloaded');
            })
            .catch(error => {
                isRecording = false;
                hideProcessingSpinner();
                statusElement.style.display = 'none';
                button.classList.remove('recording');
                updateStatus(`Error: ${error.message}`);
            });
    }
}