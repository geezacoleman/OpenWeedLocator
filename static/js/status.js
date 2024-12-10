// Function to get color based on percentage
function getColorForPercentage(percent, max) {
    const normalized = percent / max;  // For temperature, max is 85
    const hue = ((1 - normalized) * 120).toFixed(0);  // 120 is green, 0 is red
    return `hsl(${hue}, 70%, 50%)`;
}

function updateSystemStats() {
    fetch('/system_stats')
        .then(response => response.json())
        .then(data => {
            // Update CPU Usage
            const cpuElement = document.getElementById('cpuValue');
            cpuElement.textContent = `${data.cpu_percent}%`;
            cpuElement.style.color = getColorForPercentage(data.cpu_percent, 100);

            // Update CPU Temperature
            const tempElement = document.getElementById('tempValue');
            tempElement.textContent = `${data.cpu_temp}°C`;
            tempElement.style.color = getColorForPercentage(data.cpu_temp, 85);

            // Update Connection Status
            const statusElement = document.getElementById('connectionStatus');
            statusElement.textContent = data.status;
            if (data.status === "Retrying") {
                statusElement.textContent = `${data.status} (${data.retry_count}/${data.max_retries})`;
            }
            statusElement.className = `connection-status status-${data.status.toLowerCase()}`;

            // Update timestamp
            document.getElementById('statusTimestamp').textContent =
                `Last updated: ${data.timestamp}`;
        })
        .catch(error => {
            console.error('Error fetching system stats:', error);
            const statusElement = document.getElementById('connectionStatus');
            const currentStatus = statusElement.textContent;

            // Only change to disconnected if we're not already retrying
            if (currentStatus !== "Retrying") {
                statusElement.textContent = 'Disconnected';
                statusElement.className = 'connection-status status-disconnected';
            }
        });
}

// Single interval handler that checks status and updates accordingly
let updateInterval;

function startUpdateInterval() {
    // Clear any existing interval
    if (updateInterval) {
        clearInterval(updateInterval);
    }

    // Initial update
    updateSystemStats();

    // Set interval based on status
    updateInterval = setInterval(() => {
        const status = document.getElementById('connectionStatus');
        // Update every second if retrying, otherwise every 2 seconds
        const interval = status && status.textContent.includes('Retrying') ? 1000 : 2000;
        updateSystemStats();
        // Adjust interval if status changed
        if (interval !== updateInterval._interval) {
            startUpdateInterval();
        }
    }, 2000);
    updateInterval._interval = 2000;
}