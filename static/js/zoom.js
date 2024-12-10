let currentZoom = 1;
const zoomStep = 0.2;
const maxZoom = 3;
const minZoom = 1;

function zoomIn() {
    if (currentZoom < maxZoom) {
        currentZoom += zoomStep;
        updateZoom();
    }
}

function zoomOut() {
    if (currentZoom > minZoom) {
        currentZoom -= zoomStep;
        updateZoom();
    }
}

function resetZoom() {
    currentZoom = 1;
    updateZoom();
}

function updateZoom() {
    const img = document.querySelector('.zoom-image');
    img.style.transform = `scale(${currentZoom})`;
}