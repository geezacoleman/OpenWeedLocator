// ============================================
// OWL Central Controller - Video Modal
// Video feed display, frame download
// ============================================

function openVideoFeed(deviceId) {
    const modal = document.getElementById('video-modal');
    const img = document.getElementById('video-feed-img');
    const title = document.getElementById('video-modal-title');

    if (!modal || !img || !title) return;

    currentVideoDeviceId = deviceId; // Store for download
    title.textContent = `${deviceId} Video Feed`;
    img.src = `/api/video_feed/${deviceId}`;
    modal.style.display = 'flex';
}

function closeVideoModal() {
    const modal = document.getElementById('video-modal');
    const img = document.getElementById('video-feed-img');

    if (!modal || !img) return;

    modal.style.display = 'none';
    img.src = ''; // Stop video stream
    currentVideoDeviceId = null;
}

function downloadVideoFrame() {
    if (!currentVideoDeviceId) {
        showToast('No video feed active', 'error');
        return;
    }

    const img = document.getElementById('video-feed-img');
    if (!img || !img.src) {
        showToast('No image to download', 'error');
        return;
    }

    try {
        // Create a canvas to capture the current frame
        const canvas = document.createElement('canvas');
        canvas.width = img.naturalWidth || img.width;
        canvas.height = img.naturalHeight || img.height;

        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0);

        // Convert to blob and download
        canvas.toBlob((blob) => {
            if (!blob) {
                showToast('Failed to capture image', 'error');
                return;
            }

            const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, -5);
            const filename = `${currentVideoDeviceId}_${timestamp}.jpg`;

            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);

            showToast(`Image saved: ${filename}`, 'success');
        }, 'image/jpeg', 0.95);

    } catch (err) {
        console.error('Error downloading frame:', err);
        showToast('Failed to download image', 'error');
    }
}

// ============================================
// FRAME GRAB VIEWER
// ============================================

function grabFrame(deviceId) {
    var img = document.getElementById('frame-viewer-img');
    var title = document.getElementById('frame-viewer-title');
    var modal = document.getElementById('frame-viewer-modal');

    if (!img || !modal) return;

    // Fetch single high-quality frame via snapshot proxy
    img.src = '/api/snapshot/' + deviceId + '?t=' + Date.now();

    if (title) title.textContent = deviceId;
    modal.style.display = 'flex';
}

function closeFrameViewer() {
    var modal = document.getElementById('frame-viewer-modal');
    var img = document.getElementById('frame-viewer-img');

    if (modal) modal.style.display = 'none';
    if (img) img.src = '';
}
