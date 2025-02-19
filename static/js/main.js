// Initialize status updates when the page loads
document.addEventListener('DOMContentLoaded', function() {
    // Start the system status updates
    startUpdateInterval();
});

// Global error handler
window.addEventListener('error', function(event) {
    console.error('Global error:', event.error);
    updateStatus('An error occurred');
});

// Ensure clean shutdown of intervals when page is unloaded
window.addEventListener('beforeunload', function() {
    if (updateInterval) {
        clearInterval(updateInterval);
    }
});

document.addEventListener('DOMContentLoaded', function() {
    // Existing initialization
    startUpdateInterval();

    // Initialize GPS
    if (typeof initGPS === 'function') {
        initGPS();
    }
});