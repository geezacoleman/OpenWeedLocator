/* ==========================================================================
   Agent Tab — chat interface for OWL AI assistant

   Handles: API key connection, SSE streaming, tool result rendering,
   token tracking. API key stored in sessionStorage only (never persisted).
   ========================================================================== */

/* global state */
let agentConnected = false;
let agentSessionId = 'session_' + Date.now();
let agentStreaming = false;
let agentInputTokens = 0;
let agentOutputTokens = 0;
let agentPendingImages = [];  /* {base64: string, blobUrl: string} */

/**
 * Initialize the agent tab
 */
function initAgent() {
    const connectBtn = document.getElementById('agentConnectBtn');
    const sendBtn = document.getElementById('agentSendBtn');
    const input = document.getElementById('agentInput');
    const disconnectBtn = document.getElementById('agentDisconnectBtn');

    if (connectBtn) {
        connectBtn.addEventListener('click', agentConnect);
    }
    if (sendBtn) {
        sendBtn.addEventListener('click', agentSendMessage);
    }
    if (input) {
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                agentSendMessage();
            }
        });
    }
    if (disconnectBtn) {
        disconnectBtn.addEventListener('click', agentDisconnect);
    }

    var historyBtn = document.getElementById('agentHistoryBtn');
    if (historyBtn) {
        historyBtn.addEventListener('click', agentToggleHistory);
    }

    var newChatBtn = document.getElementById('agentNewChatBtn');
    if (newChatBtn) {
        newChatBtn.addEventListener('click', agentNewChat);
    }

    var attachBtn = document.getElementById('agentAttachBtn');
    var fileInput = document.getElementById('agentFileInput');
    if (attachBtn && fileInput) {
        attachBtn.addEventListener('click', function() { fileInput.click(); });
        fileInput.addEventListener('change', agentHandleFileSelect);
    }

    var grabBtn = document.getElementById('agentGrabBtn');
    if (grabBtn) {
        grabBtn.addEventListener('click', agentGrabFrame);
    }

    // Close grab picker when clicking elsewhere
    document.addEventListener('click', function(e) {
        var picker = document.getElementById('agentGrabPicker');
        if (picker && picker.classList.contains('open') &&
            !picker.contains(e.target) && e.target.id !== 'agentGrabBtn') {
            picker.classList.remove('open');
        }
    });

    var closeBtn = document.getElementById('agentSidebarClose');
    if (closeBtn) {
        closeBtn.addEventListener('click', agentCloseHistory);
    }

    // Restore connection if key exists in sessionStorage
    const savedKey = sessionStorage.getItem('owl_agent_key');
    const savedProvider = sessionStorage.getItem('owl_agent_provider');
    if (savedKey && savedProvider) {
        agentConnectWithKey(savedKey, savedProvider);
    }
}

/**
 * Connect to agent with API key from form
 */
function agentConnect() {
    const keyInput = document.getElementById('agentApiKey');
    const providerSelect = document.getElementById('agentProvider');
    const errorEl = document.getElementById('agentConnectError');

    if (!keyInput || !providerSelect) return;

    const apiKey = keyInput.value.trim();
    const provider = providerSelect.value;

    if (!apiKey) {
        showAgentError('Please enter an API key.');
        return;
    }

    agentConnectWithKey(apiKey, provider);
}

/**
 * Connect with given credentials
 */
async function agentConnectWithKey(apiKey, provider) {
    const connectBtn = document.getElementById('agentConnectBtn');
    if (connectBtn) {
        connectBtn.disabled = true;
        connectBtn.textContent = 'Connecting...';
    }
    hideAgentError();

    try {
        const resp = await fetch('/api/agent/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: apiKey, provider: provider }),
        });
        const data = await resp.json();

        if (resp.ok && data.status === 'connected') {
            // Save to sessionStorage (not persisted across browser sessions)
            sessionStorage.setItem('owl_agent_key', apiKey);
            sessionStorage.setItem('owl_agent_provider', provider);

            agentConnected = true;
            agentInputTokens = 0;
            agentOutputTokens = 0;
            agentSessionId = 'session_' + Date.now();

            // Update UI
            showAgentChat(provider, data.model);
        } else {
            showAgentError(data.error || 'Connection failed');
        }
    } catch (err) {
        showAgentError('Network error: ' + err.message);
    } finally {
        if (connectBtn) {
            connectBtn.disabled = false;
            connectBtn.textContent = 'Connect';
        }
    }
}

/**
 * Disconnect agent
 */
function agentDisconnect() {
    agentConnected = false;
    sessionStorage.removeItem('owl_agent_key');
    sessionStorage.removeItem('owl_agent_provider');

    // Clear pending images
    for (var i = 0; i < agentPendingImages.length; i++) {
        if (agentPendingImages[i].blobUrl) URL.revokeObjectURL(agentPendingImages[i].blobUrl);
    }
    agentPendingImages = [];
    agentUpdateImagePreview();

    // Show setup, hide chat
    const setup = document.getElementById('agentSetup');
    const chat = document.getElementById('agentChat');
    if (setup) setup.style.display = '';
    if (chat) chat.classList.remove('active');

    // Close sidebar
    agentCloseHistory();

    // Clear messages
    const messages = document.getElementById('agentMessages');
    if (messages) messages.innerHTML = '';
}

/**
 * Switch UI to chat mode
 */
function showAgentChat(provider, model) {
    const setup = document.getElementById('agentSetup');
    const chat = document.getElementById('agentChat');

    if (setup) setup.style.display = 'none';
    if (chat) chat.classList.add('active');

    // Update status bar
    const providerEl = document.getElementById('agentStatusProvider');
    if (providerEl) {
        const displayName = provider === 'anthropic' ? 'Claude' : 'GPT';
        providerEl.textContent = displayName + (model ? ' (' + model + ')' : '');
    }

    updateAgentTokens();

    // Focus input
    const input = document.getElementById('agentInput');
    if (input) input.focus();
}

/**
 * Send a message
 */
async function agentSendMessage() {
    if (!agentConnected || agentStreaming) return;

    const input = document.getElementById('agentInput');
    if (!input) return;

    const message = input.value.trim();
    var images = agentPendingImages.slice();
    if (!message && images.length === 0) return;

    input.value = '';
    agentStreaming = true;
    updateAgentSendButton();

    // Render user bubble with images + text
    var imageB64s = images.map(function(img) { return img.base64; });
    appendAgentMessageWithImages('user', message, imageB64s);

    // Clear pending images
    agentPendingImages = [];
    agentUpdateImagePreview();

    // Create assistant message container for streaming
    const assistantEl = createAssistantMessage();

    try {
        var body = { session_id: agentSessionId, message: message };
        if (imageB64s.length > 0) {
            body.images = imageB64s;
        }

        const resp = await fetch('/api/agent/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!resp.ok) {
            const err = await resp.json();
            appendAgentMessage('error', err.error || 'Request failed');
            assistantEl.remove();
            return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let textContent = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const payload = line.substring(6).trim();
                if (!payload) continue;

                try {
                    const event = JSON.parse(payload);
                    if (event.type === 'text_delta') {
                        textContent += event.data;
                        updateAssistantText(assistantEl, textContent);
                    } else if (event.type === 'tool_result') {
                        appendToolResult(assistantEl, event.data);
                        // Auto-reload widgets when agent creates/deletes one
                        if (event.data.tool_name === 'create_widget' ||
                            event.data.tool_name === 'delete_widget') {
                            if (typeof initWidgets === 'function') initWidgets();
                        }
                    } else if (event.type === 'usage') {
                        if (event.data.input_tokens) agentInputTokens += event.data.input_tokens;
                        if (event.data.output_tokens) agentOutputTokens += event.data.output_tokens;
                        updateAgentTokens();
                    } else if (event.type === 'error') {
                        appendAgentMessage('error', event.data);
                    } else if (event.type === 'done') {
                        // Final token update from session info
                        if (event.data) {
                            agentInputTokens = event.data.input_tokens || agentInputTokens;
                            agentOutputTokens = event.data.output_tokens || agentOutputTokens;
                            updateAgentTokens();
                        }
                    }
                } catch (e) {
                    // Skip malformed JSON
                }
            }
        }

        // If no text was streamed, remove empty assistant bubble
        if (!textContent && !assistantEl.querySelector('.agent-tool-result')) {
            assistantEl.remove();
        }

    } catch (err) {
        appendAgentMessage('error', 'Stream error: ' + err.message);
        assistantEl.remove();
    } finally {
        agentStreaming = false;
        updateAgentSendButton();
    }
}

/* ---- History sidebar ---- */

/**
 * Toggle the session history sidebar
 */
function agentToggleHistory() {
    var sidebar = document.getElementById('agentSidebar');
    if (!sidebar) return;

    var isOpen = sidebar.classList.contains('open');
    if (isOpen) {
        sidebar.classList.remove('open');
    } else {
        agentLoadSessions();
        sidebar.classList.add('open');
    }
}

/**
 * Close the history sidebar
 */
function agentCloseHistory() {
    var sidebar = document.getElementById('agentSidebar');
    if (sidebar) sidebar.classList.remove('open');
}

/**
 * Fetch and display saved sessions
 */
async function agentLoadSessions() {
    var list = document.getElementById('agentSessionList');
    if (!list) return;

    list.innerHTML = '<div class="agent-session-loading">Loading...</div>';

    try {
        var resp = await fetch('/api/agent/sessions');
        if (!resp.ok) { list.innerHTML = ''; return; }
        var sessions = await resp.json();

        if (!sessions.length) {
            list.innerHTML = '<div class="agent-session-empty">No saved conversations</div>';
            return;
        }

        list.innerHTML = '';
        for (var i = 0; i < sessions.length; i++) {
            var s = sessions[i];
            var item = document.createElement('div');
            item.className = 'agent-session-item';
            if (s.id === agentSessionId) {
                item.classList.add('active');
            }

            var title = document.createElement('div');
            title.className = 'agent-session-title';
            title.textContent = s.title || 'Untitled';

            var meta = document.createElement('div');
            meta.className = 'agent-session-meta';
            var date = new Date(s.updated * 1000);
            meta.textContent = date.toLocaleDateString() + ' — ' +
                s.message_count + ' messages';

            var deleteBtn = document.createElement('button');
            deleteBtn.className = 'agent-session-delete';
            deleteBtn.textContent = '\u00d7';
            deleteBtn.dataset.sessionId = s.id;
            deleteBtn.addEventListener('click', function(e) {
                e.stopPropagation();
                agentDeleteSession(this.dataset.sessionId);
            });

            item.appendChild(title);
            item.appendChild(meta);
            item.appendChild(deleteBtn);

            item.dataset.sessionId = s.id;
            item.addEventListener('click', function() {
                agentLoadSession(this.dataset.sessionId);
            });

            list.appendChild(item);
        }
    } catch (err) {
        list.innerHTML = '';
    }
}

/**
 * Load a past session into the chat view
 */
async function agentLoadSession(sessionId) {
    try {
        var resp = await fetch('/api/agent/sessions/' + encodeURIComponent(sessionId));
        if (!resp.ok) return;
        var data = await resp.json();

        // Clear current messages
        var messages = document.getElementById('agentMessages');
        if (messages) messages.innerHTML = '';

        // Switch to this session
        agentSessionId = sessionId;
        agentInputTokens = data.input_tokens || 0;
        agentOutputTokens = data.output_tokens || 0;
        updateAgentTokens();

        // Replay messages into the chat view
        for (var i = 0; i < data.messages.length; i++) {
            var msg = data.messages[i];
            if (msg.role === 'user') {
                var content = msg.content;
                if (typeof content === 'string') {
                    appendAgentMessage('user', content);
                } else if (Array.isArray(content)) {
                    // Anthropic tool_result messages — skip visual replay
                    var hasToolResult = false;
                    for (var j = 0; j < content.length; j++) {
                        if (content[j].type === 'tool_result') { hasToolResult = true; break; }
                    }
                    if (!hasToolResult) {
                        var replayImages = [];
                        var replayText = '';
                        for (var j = 0; j < content.length; j++) {
                            if (content[j].type === 'text') {
                                replayText = content[j].text;
                            } else if (content[j].type === 'image') {
                                var src = content[j].source || {};
                                replayImages.push(src.data || '');
                            }
                        }
                        appendAgentMessageWithImages('user', replayText, replayImages);
                    }
                }
            } else if (msg.role === 'assistant') {
                var content = msg.content;
                if (typeof content === 'string') {
                    var el = createAssistantMessage();
                    updateAssistantText(el, content);
                } else if (Array.isArray(content)) {
                    var el = createAssistantMessage();
                    for (var j = 0; j < content.length; j++) {
                        if (content[j].type === 'text') {
                            updateAssistantText(el, content[j].text);
                        } else if (content[j].type === 'tool_use') {
                            appendToolResult(el, {
                                tool_name: content[j].name,
                                result: content[j].input
                            });
                        }
                    }
                }
            }
        }

        // Close sidebar
        agentCloseHistory();

    } catch (err) {
        console.error('Failed to load session:', err);
    }
}

/**
 * Start a new chat session
 */
function agentNewChat() {
    agentSessionId = 'session_' + Date.now();
    agentInputTokens = 0;
    agentOutputTokens = 0;
    updateAgentTokens();

    // Clear pending images
    for (var i = 0; i < agentPendingImages.length; i++) {
        if (agentPendingImages[i].blobUrl) URL.revokeObjectURL(agentPendingImages[i].blobUrl);
    }
    agentPendingImages = [];
    agentUpdateImagePreview();

    var messages = document.getElementById('agentMessages');
    if (messages) messages.innerHTML = '';

    agentCloseHistory();

    var input = document.getElementById('agentInput');
    if (input) input.focus();
}

/**
 * Delete a saved session
 */
async function agentDeleteSession(sessionId) {
    try {
        var resp = await fetch('/api/agent/sessions/' + encodeURIComponent(sessionId), {
            method: 'DELETE'
        });
        if (resp.ok) {
            // If we deleted the active session, start fresh
            if (sessionId === agentSessionId) {
                agentNewChat();
            }
            agentLoadSessions();
        }
    } catch (err) {
        console.error('Failed to delete session:', err);
    }
}

/* ---- DOM helpers ---- */

function appendAgentMessage(role, text) {
    const messages = document.getElementById('agentMessages');
    if (!messages) return;

    const el = document.createElement('div');
    el.className = 'agent-msg agent-msg-' + role;
    el.textContent = text;
    messages.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
    return el;
}

function createAssistantMessage() {
    const messages = document.getElementById('agentMessages');
    if (!messages) return document.createElement('div');

    const el = document.createElement('div');
    el.className = 'agent-msg agent-msg-assistant';
    messages.appendChild(el);
    return el;
}

function updateAssistantText(el, text) {
    // Find or create the text container
    let textSpan = el.querySelector('.agent-msg-text');
    if (!textSpan) {
        textSpan = document.createElement('div');
        textSpan.className = 'agent-msg-text';
        el.appendChild(textSpan);
    }
    textSpan.innerHTML = renderAgentMarkdown(text);

    const messages = document.getElementById('agentMessages');
    if (messages) messages.scrollTop = messages.scrollHeight;
}

/**
 * Lightweight markdown renderer for agent messages.
 * Handles: headings, tables, horizontal rules, code blocks, inline code,
 * bold, italic, bullet/numbered lists, paragraphs.
 * All text is escaped first to prevent XSS.
 */
function renderAgentMarkdown(text) {
    // Escape HTML
    var safe = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    // Extract code blocks first (protect from further processing)
    var codeBlocks = [];
    safe = safe.replace(/```(\w*)\n?([\s\S]*?)```/g, function(match, lang, code) {
        var idx = codeBlocks.length;
        codeBlocks.push('<pre class="agent-code-block">' + code.replace(/^\n|\n$/g, '') + '</pre>');
        return '\n\x00CODEBLOCK' + idx + '\x00\n';
    });

    // Inline code (`...`)
    safe = safe.replace(/`([^`]+)`/g, '<code class="agent-code-inline">$1</code>');

    // Bold (**...**)
    safe = safe.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    // Italic (*...*)
    safe = safe.replace(/\*([^*]+)\*/g, '<em>$1</em>');

    // Process line by line for block-level elements
    var lines = safe.split('\n');
    var html = '';
    var i = 0;

    while (i < lines.length) {
        var line = lines[i];
        var trimmed = line.trim();

        // Empty line
        if (!trimmed) { i++; continue; }

        // Code block placeholder
        var cbMatch = trimmed.match(/^\x00CODEBLOCK(\d+)\x00$/);
        if (cbMatch) {
            html += codeBlocks[parseInt(cbMatch[1])];
            i++;
            continue;
        }

        // Horizontal rule (--- or ***)
        if (/^[-*_]{3,}$/.test(trimmed)) {
            html += '<hr class="agent-hr">';
            i++;
            continue;
        }

        // Headings (## ...)
        var headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
        if (headingMatch) {
            var level = headingMatch[1].length;
            html += '<h' + level + ' class="agent-heading agent-h' + level + '">' +
                headingMatch[2] + '</h' + level + '>';
            i++;
            continue;
        }

        // Table (| ... | ... |)
        if (/^\|.+\|/.test(trimmed)) {
            var tableLines = [];
            while (i < lines.length && /^\|.+\|/.test(lines[i].trim())) {
                tableLines.push(lines[i].trim());
                i++;
            }
            html += renderAgentTable(tableLines);
            continue;
        }

        // Bullet/numbered list
        if (/^[-*]\s/.test(trimmed) || /^\d+[.)]\s/.test(trimmed)) {
            var listItems = [];
            while (i < lines.length) {
                var lt = lines[i].trim();
                if (!lt) break;
                if (/^[-*]\s/.test(lt) || /^\d+[.)]\s/.test(lt)) {
                    listItems.push(lt.replace(/^[-*]\s+/, '').replace(/^\d+[.)]\s+/, ''));
                    i++;
                } else {
                    break;
                }
            }
            html += '<ul class="agent-list">';
            for (var j = 0; j < listItems.length; j++) {
                html += '<li>' + listItems[j] + '</li>';
            }
            html += '</ul>';
            continue;
        }

        // Regular paragraph — collect consecutive non-special lines
        var paraLines = [];
        while (i < lines.length) {
            var pt = lines[i].trim();
            if (!pt) break;
            // Stop if next line is a special block
            if (/^#{1,4}\s/.test(pt) || /^\|.+\|/.test(pt) || /^[-*_]{3,}$/.test(pt) ||
                /^[-*]\s/.test(pt) || /^\d+[.)]\s/.test(pt) ||
                /^\x00CODEBLOCK\d+\x00$/.test(pt)) {
                break;
            }
            paraLines.push(pt);
            i++;
        }
        if (paraLines.length > 0) {
            html += '<p>' + paraLines.join('<br>') + '</p>';
        }
    }

    return html;
}

/**
 * Render a markdown table from lines of | col | col | format.
 */
function renderAgentTable(lines) {
    if (lines.length < 1) return '';

    function parseCells(line) {
        // Split on |, trim, drop empty leading/trailing
        var cells = line.split('|');
        var result = [];
        for (var i = 0; i < cells.length; i++) {
            var c = cells[i].trim();
            if (c !== '' || (i > 0 && i < cells.length - 1)) {
                if (i === 0 && c === '') continue;
                if (i === cells.length - 1 && c === '') continue;
                result.push(c);
            }
        }
        return result;
    }

    // Check if line 2 is a separator (|---|---|)
    var hasSeparator = lines.length >= 2 && /^[\s|:-]+$/.test(lines[1]);
    var headerCells = parseCells(lines[0]);
    var startRow = hasSeparator ? 2 : 1;

    var html = '<table class="agent-table"><thead><tr>';
    for (var h = 0; h < headerCells.length; h++) {
        html += '<th>' + headerCells[h] + '</th>';
    }
    html += '</tr></thead><tbody>';

    for (var r = startRow; r < lines.length; r++) {
        // Skip separator rows
        if (/^[\s|:-]+$/.test(lines[r])) continue;
        var cells = parseCells(lines[r]);
        html += '<tr>';
        for (var c = 0; c < cells.length; c++) {
            html += '<td>' + cells[c] + '</td>';
        }
        html += '</tr>';
    }
    html += '</tbody></table>';
    return html;
}

function appendToolResult(assistantEl, data) {
    const card = document.createElement('div');
    card.className = 'agent-tool-result';

    const header = document.createElement('div');
    header.className = 'agent-tool-header';
    header.innerHTML = '<span class="agent-tool-arrow">&#9654;</span> ' +
        escapeHtml(data.tool_name || 'Tool');
    card.appendChild(header);

    const body = document.createElement('div');
    body.className = 'agent-tool-body';
    body.textContent = JSON.stringify(data.result || {}, null, 2);
    card.appendChild(body);

    card.addEventListener('click', function() {
        card.classList.toggle('expanded');
    });

    assistantEl.appendChild(card);

    const messages = document.getElementById('agentMessages');
    if (messages) messages.scrollTop = messages.scrollHeight;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function updateAgentTokens() {
    const el = document.getElementById('agentStatusTokens');
    if (el) {
        el.textContent = 'Tokens: ' + agentInputTokens.toLocaleString() +
            ' in / ' + agentOutputTokens.toLocaleString() + ' out';
    }
}

function updateAgentSendButton() {
    const btn = document.getElementById('agentSendBtn');
    if (btn) {
        btn.disabled = agentStreaming;
        btn.textContent = agentStreaming ? '...' : 'Send';
    }
    const input = document.getElementById('agentInput');
    if (input) input.disabled = agentStreaming;
    var attachBtn = document.getElementById('agentAttachBtn');
    if (attachBtn) attachBtn.disabled = agentStreaming;
    var grabBtn = document.getElementById('agentGrabBtn');
    if (grabBtn) grabBtn.disabled = agentStreaming;
}

function showAgentError(msg) {
    const el = document.getElementById('agentConnectError');
    if (el) {
        el.textContent = msg;
        el.classList.add('visible');
    }
}

function hideAgentError() {
    const el = document.getElementById('agentConnectError');
    if (el) el.classList.remove('visible');
}

/* ==== Image upload & grab frame ==== */

/**
 * Handle file input change — resize and add to pending images
 */
function agentHandleFileSelect(e) {
    var files = e.target.files;
    if (!files || files.length === 0) return;
    for (var i = 0; i < files.length; i++) {
        if (agentPendingImages.length >= 4) break;
        var file = files[i];
        if (!file.type.startsWith('image/')) continue;
        agentResizeAndAddImage(file);
    }
    // Reset file input so same file can be re-selected
    e.target.value = '';
}

/**
 * Resize image via canvas (max 1024px longest side, JPEG 85%) and add to pending
 */
function agentResizeAndAddImage(file) {
    var reader = new FileReader();
    reader.onload = function(e) {
        var img = new Image();
        img.onload = function() {
            var maxDim = 1024;
            var w = img.width;
            var h = img.height;
            if (w > maxDim || h > maxDim) {
                if (w > h) {
                    h = Math.round(h * maxDim / w);
                    w = maxDim;
                } else {
                    w = Math.round(w * maxDim / h);
                    h = maxDim;
                }
            }
            var canvas = document.createElement('canvas');
            canvas.width = w;
            canvas.height = h;
            var ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, w, h);
            var dataUrl = canvas.toDataURL('image/jpeg', 0.85);
            var base64 = dataUrl.split(',')[1];

            // Create blob URL for preview
            var byteStr = atob(base64);
            var arr = new Uint8Array(byteStr.length);
            for (var j = 0; j < byteStr.length; j++) arr[j] = byteStr.charCodeAt(j);
            var blob = new Blob([arr], { type: 'image/jpeg' });
            var blobUrl = URL.createObjectURL(blob);

            if (agentPendingImages.length < 4) {
                agentPendingImages.push({ base64: base64, blobUrl: blobUrl });
                agentUpdateImagePreview();
            }
        };
        img.src = e.target.result;
    };
    reader.readAsDataURL(file);
}

/**
 * Update the image preview strip above the input row
 */
function agentUpdateImagePreview() {
    var preview = document.getElementById('agentImagePreview');
    if (!preview) return;

    preview.innerHTML = '';
    if (agentPendingImages.length === 0) {
        preview.classList.remove('has-images');
        return;
    }
    preview.classList.add('has-images');

    for (var i = 0; i < agentPendingImages.length; i++) {
        var thumb = document.createElement('div');
        thumb.className = 'agent-image-thumb';

        var img = document.createElement('img');
        img.src = agentPendingImages[i].blobUrl;
        thumb.appendChild(img);

        var removeBtn = document.createElement('button');
        removeBtn.className = 'agent-image-thumb-remove';
        removeBtn.textContent = '\u00d7';
        removeBtn.dataset.index = i;
        removeBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            var idx = parseInt(this.dataset.index);
            var removed = agentPendingImages.splice(idx, 1);
            if (removed.length > 0 && removed[0].blobUrl) {
                URL.revokeObjectURL(removed[0].blobUrl);
            }
            agentUpdateImagePreview();
        });
        thumb.appendChild(removeBtn);

        preview.appendChild(thumb);
    }
}

/**
 * Grab a camera frame from an OWL device
 */
async function agentGrabFrame() {
    if (agentStreaming || agentPendingImages.length >= 4) return;

    var grabBtn = document.getElementById('agentGrabBtn');

    // Standalone — no device picker, just grab directly
    if (typeof isStandalone !== 'undefined' && isStandalone) {
        if (grabBtn) grabBtn.classList.add('loading');
        try {
            await agentFetchFrame('/api/agent/grab_frame');
        } catch (err) {
            appendAgentMessage('error', 'Could not grab frame: ' + err.message);
        }
        if (grabBtn) grabBtn.classList.remove('loading');
        return;
    }

    // Networked — find connected OWLs, pick one
    try {
        var resp = await fetch('/api/owls');
        if (!resp.ok) {
            appendAgentMessage('error', 'Could not fetch OWL list');
            return;
        }
        var data = await resp.json();
        var owls = data.owls || {};
        var connected = Object.keys(owls);

        if (connected.length === 0) {
            appendAgentMessage('error', 'No OWL devices connected');
            return;
        }

        if (connected.length === 1) {
            // Single device — grab directly
            if (grabBtn) grabBtn.classList.add('loading');
            try {
                await agentFetchFrame('/api/agent/grab_frame/' + encodeURIComponent(connected[0]));
            } catch (err) {
                appendAgentMessage('error', 'Could not grab frame: ' + err.message);
            }
            if (grabBtn) grabBtn.classList.remove('loading');
            return;
        }

        // Multiple devices — show picker
        agentShowGrabPicker(connected);

    } catch (err) {
        appendAgentMessage('error', 'Could not fetch OWL list: ' + err.message);
    }
}

/**
 * Fetch a frame from the grab_frame endpoint and add to pending images
 */
async function agentFetchFrame(url) {
    var resp = await fetch(url);
    if (!resp.ok) {
        var errData = await resp.json().catch(function() { return {}; });
        throw new Error(errData.error || 'Failed to grab frame');
    }
    var data = await resp.json();
    var base64 = data.image;

    // Create blob URL for preview
    var byteStr = atob(base64);
    var arr = new Uint8Array(byteStr.length);
    for (var j = 0; j < byteStr.length; j++) arr[j] = byteStr.charCodeAt(j);
    var blob = new Blob([arr], { type: 'image/jpeg' });
    var blobUrl = URL.createObjectURL(blob);

    agentPendingImages.push({ base64: base64, blobUrl: blobUrl });
    agentUpdateImagePreview();
}

/**
 * Show the device picker popup for grabbing frames (networked, multi-OWL)
 */
function agentShowGrabPicker(owlIds) {
    var picker = document.getElementById('agentGrabPicker');
    if (!picker) return;

    picker.innerHTML = '';
    var title = document.createElement('div');
    title.className = 'agent-grab-picker-title';
    title.textContent = 'Grab frame from:';
    picker.appendChild(title);

    for (var i = 0; i < owlIds.length; i++) {
        var btn = document.createElement('button');
        btn.className = 'agent-grab-picker-item';
        btn.textContent = owlIds[i];
        btn.dataset.deviceId = owlIds[i];
        btn.addEventListener('click', async function() {
            var deviceId = this.dataset.deviceId;
            picker.classList.remove('open');
            var grabBtn = document.getElementById('agentGrabBtn');
            if (grabBtn) grabBtn.classList.add('loading');
            try {
                await agentFetchFrame('/api/agent/grab_frame/' + encodeURIComponent(deviceId));
            } catch (err) {
                appendAgentMessage('error', 'Could not grab frame from ' + deviceId + ': ' + err.message);
            }
            if (grabBtn) grabBtn.classList.remove('loading');
        });
        picker.appendChild(btn);
    }

    picker.classList.add('open');
}

/**
 * Render a message bubble with optional images and text
 */
function appendAgentMessageWithImages(role, text, images) {
    var messages = document.getElementById('agentMessages');
    if (!messages) return;

    var el = document.createElement('div');
    el.className = 'agent-msg agent-msg-' + role;

    // Render images
    if (images && images.length > 0) {
        var imgContainer = document.createElement('div');
        imgContainer.className = 'agent-msg-images';
        for (var i = 0; i < images.length; i++) {
            if (!images[i]) continue;
            var img = document.createElement('img');
            img.className = 'agent-msg-image';
            img.src = 'data:image/jpeg;base64,' + images[i];
            imgContainer.appendChild(img);
        }
        el.appendChild(imgContainer);
    }

    // Render text
    if (text) {
        var textSpan = document.createElement('span');
        textSpan.textContent = text;
        el.appendChild(textSpan);
    }

    messages.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
    return el;
}
