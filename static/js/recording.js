let isRecording = false;

function updateStatus(message) {
    const status = document.getElementById('status');
    if (status) {
        status.textContent = message;
        if (!isRecording) {
            setTimeout(() => {
                status.textContent = '';
            }, 3000);
        }
    }
}

function downloadFrame() {
    fetch('/download_frame', { method: 'POST' })
        .then(response => {
            if (!response.ok) {
                throw new Error('Frame not available');
            }
            return response.blob();
        })
        .then(blob => {
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
            a.download = `owl_frame_${timestamp}.jpg`;

            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);

            updateStatus('Frame downloaded successfully');
        })
        .catch(error => {
            updateStatus(`Error: ${error.message}`);
        });
}

function toggleRecording() {
    const button = document.getElementById('recordButton');

    if (!isRecording) {
        // Start recording
        fetch('/start_recording', { method: 'POST' })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    isRecording = true;
                    button.textContent = 'Stop Recording';
                    button.classList.add('recording');
                    updateStatus('Recording started...');
                }
            })
            .catch(error => updateStatus(`Error: ${error.message}`));
    } else {
        // Stop recording and download
        fetch('/stop_recording', { method: 'POST' })
            .then(response => {
                if (!response.ok) {
                    throw new Error('Recording failed');
                }
                return response.blob();
            })
            .then(blob => {
                isRecording = false;
                button.textContent = 'Start Recording';
                button.classList.remove('recording');

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
                button.textContent = 'Start Recording';
                button.classList.remove('recording');
                updateStatus(`Error: ${error.message}`);
            });
    }
}