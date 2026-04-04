/* ==========================================================================
   OWL Dashboard - Notifications Module
   Notification bell, panel, toasts
   ========================================================================== */

let notificationStore = [];
let notificationId = 0;

/**
 * Initialize notification system
 */
function initNotifications() {
    const bell = document.getElementById('notificationBell');
    const panel = document.getElementById('notificationPanel');
    const overlay = document.getElementById('notificationOverlay');
    const closeBtn = document.getElementById('closeNotificationPanel');
    const clearBtn = document.getElementById('clearAllNotifications');

    if (bell) {
        bell.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            toggleNotificationPanel();
        });
    }

    if (closeBtn) {
        closeBtn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            hideNotificationPanel();
        });
    }

    if (clearBtn) {
        clearBtn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            clearAllNotifications();
        });
    }

    if (overlay) {
        overlay.addEventListener('click', function(e) {
            hideNotificationPanel();
        });
    }

    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            hideNotificationPanel();
        }
    });
}

/**
 * Show notification with optional toast
 */
function showNotification(title, message, type = 'info', duration = 5000, showToast = true) {
    const notification = {
        id: ++notificationId,
        title,
        message,
        type,
        timestamp: new Date(),
        time: new Date().toLocaleTimeString()
    };

    notificationStore.unshift(notification);

    if (notificationStore.length > 50) {
        notificationStore = notificationStore.slice(0, 50);
    }

    updateNotificationCount();
    updateNotificationPanel();

    if (showToast && (type === 'success' || type === 'error')) {
        showQuickToast(message, type, 2000);
    }

    if (type === 'success' || type === 'info') {
        setTimeout(() => {
            removeNotification(notification.id);
        }, duration);
    }

    const bell = document.getElementById('notificationBell');
    if (bell) {
        bell.classList.add('has-notifications');
        setTimeout(() => bell.classList.remove('has-notifications'), 500);
    }
}

/**
 * Show quick toast for immediate feedback
 */
function showQuickToast(message, type = 'info', duration = 2000) {
    const toast = document.getElementById('quickToast');
    if (!toast) return;

    toast.textContent = message;
    toast.className = `quick-toast ${type}`;
    toast.classList.remove('hidden');

    setTimeout(() => {
        toast.classList.add('hidden');
    }, duration);
}

/**
 * Toggle notification panel
 */
function toggleNotificationPanel() {
    const panel = document.getElementById('notificationPanel');
    const overlay = document.getElementById('notificationOverlay');

    if (!panel || !overlay) return;

    if (panel.classList.contains('hidden')) {
        showNotificationPanel();
    } else {
        hideNotificationPanel();
    }
}

/**
 * Show notification panel
 */
function showNotificationPanel() {
    const panel = document.getElementById('notificationPanel');
    const overlay = document.getElementById('notificationOverlay');

    if (!panel || !overlay) return;

    overlay.classList.remove('hidden');
    overlay.classList.add('visible');

    panel.classList.remove('hidden');

    requestAnimationFrame(() => {
        panel.classList.add('visible');
    });

    updateNotificationPanel();
    document.body.style.overflow = 'hidden';

    setTimeout(() => {
        const countEl = document.getElementById('notificationCount');
        if (countEl && !countEl.classList.contains('hidden')) {
            countEl.style.animation = 'none';
        }
    }, 1000);
}

/**
 * Hide notification panel
 */
function hideNotificationPanel() {
    const panel = document.getElementById('notificationPanel');
    const overlay = document.getElementById('notificationOverlay');

    if (!panel || !overlay) return;

    panel.classList.remove('visible');
    overlay.classList.remove('visible');

    setTimeout(() => {
        panel.classList.add('hidden');
        overlay.classList.add('hidden');
        document.body.style.overflow = '';
    }, 300);
}

/**
 * Update notification count badge
 */
function updateNotificationCount() {
    const countEl = document.getElementById('notificationCount');
    if (!countEl) return;

    const count = notificationStore.length;

    if (count > 0) {
        countEl.textContent = count > 99 ? '99+' : count;
        countEl.classList.remove('hidden');
    } else {
        countEl.classList.add('hidden');
    }
}

/**
 * Update notification panel content
 */
function updateNotificationPanel() {
    const listEl = document.getElementById('notificationList');
    if (!listEl) return;

    if (notificationStore.length === 0) {
        listEl.innerHTML = '<p class="no-notifications">No notifications</p>';
        return;
    }

    let html = '';
    notificationStore.forEach(notification => {
        const iconSymbol = {
            'success': '✓',
            'error': '✕',
            'warning': '⚠',
            'info': 'i'
        }[notification.type] || 'i';

        html += `
            <div class="notification-item" data-id="${notification.id}">
                <div class="notification-icon ${notification.type}">
                    ${iconSymbol}
                </div>
                <div class="notification-content">
                    <div class="notification-title">${notification.title}</div>
                    <div class="notification-message">${notification.message}</div>
                    <div class="notification-time">${notification.time}</div>
                </div>
            </div>
        `;
    });

    listEl.innerHTML = html;
}

/**
 * Remove specific notification
 */
function removeNotification(id) {
    notificationStore = notificationStore.filter(n => n.id !== id);
    updateNotificationCount();
    updateNotificationPanel();
}

/**
 * Clear all notifications
 */
function clearAllNotifications() {
    notificationStore = [];
    updateNotificationCount();
    updateNotificationPanel();
    showQuickToast('All notifications cleared', 'info', 1500);
}
