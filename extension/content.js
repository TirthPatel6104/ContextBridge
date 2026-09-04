// ====================================================================
// ContextBridge Extension — Content Script
//
// Runs on ChatGPT and Claude pages. Adds a floating button that
// scrapes the current conversation from the page DOM and sends it
// to the local ContextBridge server for automatic memory extraction.
// Also supports attaching files (PDFs, docs) whose content gets
// embedded in the generated prompt for the target AI.
// ====================================================================

const CB_SERVER = "http://localhost:5000";

// -------------------------------------------------------------------
// Detect which site we're on
// -------------------------------------------------------------------

function detectSite() {
    const host = window.location.hostname;
    if (host.includes("chatgpt.com") || host.includes("chat.openai.com")) return "chatgpt";
    if (host.includes("claude.ai")) return "claude";
    return null;
}

// -------------------------------------------------------------------
// Chat Scrapers — extract conversation text from page DOM
// -------------------------------------------------------------------

function scrapeChatGPT() {
    const messages = [];
    const selectors = [
        '[data-message-author-role]',
        'div[class*="agent-turn"], div[class*="user-turn"]',
        'article',
        '.text-message',
    ];

    for (const selector of selectors) {
        const nodes = document.querySelectorAll(selector);
        if (nodes.length > 0) {
            nodes.forEach(node => {
                const role = node.getAttribute('data-message-author-role')
                    || (node.className.includes('user') ? 'user' : 'assistant');
                const text = node.innerText.trim();
                if (text) messages.push({ role, text });
            });
            break;
        }
    }

    if (messages.length === 0) {
        const main = document.querySelector('main') || document.querySelector('[role="main"]');
        if (main) return main.innerText;
    }

    return messages.map(m => `${m.role === 'user' ? 'User' : 'Assistant'}: ${m.text}`).join('\n\n');
}

function scrapeClaude() {
    const messages = [];
    const selectors = [
        '[data-testid="user-message"], [data-testid="ai-message"]',
        'div[class*="human-turn"], div[class*="ai-turn"]',
        '.font-user-message, .font-claude-message',
        '[class*="Message"]',
    ];

    for (const selector of selectors) {
        const nodes = document.querySelectorAll(selector);
        if (nodes.length > 0) {
            nodes.forEach(node => {
                const isUser = node.getAttribute('data-testid')?.includes('user')
                    || node.className.includes('human')
                    || node.className.includes('user');
                const text = node.innerText.trim();
                if (text) messages.push({ role: isUser ? 'user' : 'assistant', text });
            });
            break;
        }
    }

    if (messages.length === 0) {
        const main = document.querySelector('main') || document.querySelector('[role="main"]');
        if (main) return main.innerText;
    }

    return messages.map(m => `${m.role === 'user' ? 'User' : 'Assistant'}: ${m.text}`).join('\n\n');
}

function scrapeChat() {
    const site = detectSite();
    if (site === "chatgpt") return scrapeChatGPT();
    if (site === "claude") return scrapeClaude();
    return null;
}

// -------------------------------------------------------------------
// Get conversation title
// -------------------------------------------------------------------

function getConversationTitle() {
    const site = detectSite();

    if (site === "chatgpt") {
        const active = document.querySelector('nav a[class*="bg-"]') || document.querySelector('nav li.active a');
        if (active) return active.innerText.trim();
    }

    if (site === "claude") {
        const title = document.querySelector('h1') || document.querySelector('[class*="title"]');
        if (title) return title.innerText.trim();
    }

    const pageTitle = document.title.replace(/ [-|] .*$/, '').trim();
    return pageTitle || "untitled_chat";
}

// -------------------------------------------------------------------
// API Communication
// -------------------------------------------------------------------

async function sendToContextBridge(chatText, packageName) {
    const blob = new Blob([chatText], { type: 'text/plain' });
    const formData = new FormData();
    formData.append('files[]', blob, 'chat_export.txt');
    formData.append('model', 'local');
    formData.append('package_name', packageName);

    const res = await fetch(`${CB_SERVER}/api/extract`, {
        method: 'POST',
        body: formData,
    });

    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.error || `Server error ${res.status}`);
    }

    return await res.json();
}

async function attachFiles(files, packageName) {
    const formData = new FormData();
    formData.append('package_name', packageName);
    for (const file of files) {
        formData.append('files[]', file);
    }

    const res = await fetch(`${CB_SERVER}/api/files/attach`, {
        method: 'POST',
        body: formData,
    });

    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.error || `Server error ${res.status}`);
    }

    return await res.json();
}

async function generatePrompt(packageName, targetModel) {
    const res = await fetch(`${CB_SERVER}/api/prompt`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ package_name: packageName, target_model: targetModel }),
    });

    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.error || `Server error ${res.status}`);
    }

    return await res.json();
}

async function getAttachedFiles(packageName) {
    const res = await fetch(`${CB_SERVER}/api/files/${encodeURIComponent(packageName)}`);
    if (!res.ok) return { files: [] };
    return await res.json();
}

async function checkWatchFiles() {
    try {
        const res = await fetch(`${CB_SERVER}/api/watch/files`);
        if (!res.ok) return { files: [] };
        return await res.json();
    } catch (e) {
        return { files: [] }; // Server might be down
    }
}

// -------------------------------------------------------------------
// Widget UI
// -------------------------------------------------------------------

function createWidget() {
    if (document.getElementById('contextbridge-widget')) return;

    const site = detectSite();
    const sourceName = site === 'chatgpt' ? 'ChatGPT' : 'Claude';
    const targetModel = site === 'chatgpt' ? 'claude' : 'openai';
    const targetName = site === 'chatgpt' ? 'Claude' : 'ChatGPT';

    const widget = document.createElement('div');
    widget.id = 'contextbridge-widget';
    widget.innerHTML = `
        <div id="contextbridge-panel">
            <div class="cb-panel-header">
                <h3>\u{1F309} ContextBridge</h3>
                <span class="cb-source">${sourceName}</span>
            </div>
            <div class="cb-panel-body">
                <div class="cb-form-group">
                    <label>Package Name</label>
                    <input type="text" id="cb-package-name" placeholder="my_project" value="">
                </div>

                <button class="cb-btn cb-btn-primary" id="cb-extract-btn">
                    \u26A1 Extract This Chat
                </button>

                <!-- File Attachment Section -->
                <div class="cb-divider"></div>
                <div class="cb-file-section">
                    <label>Attach Files (PDFs, docs, code)</label>
                    <div class="cb-file-drop" id="cb-file-drop">
                        <input type="file" id="cb-file-input" multiple
                               accept=".pdf,.txt,.md,.json,.html,.py,.js,.ts,.css,.csv,.xml,.yaml,.yml,.log,.zip"
                               hidden>
                        <span class="cb-file-drop-text">\u{1F4CE} Click to attach files</span>
                    </div>
                    <div id="cb-file-list" class="cb-file-list"></div>
                    <div id="cb-watch-info" class="cb-watch-info" style="display:none; font-size:11px; color:#a29bfe; margin-top:4px;">
                        \u{1F441}\u{FE0F} <span id="cb-watch-count">0</span> files auto-detected from watch folder
                    </div>
                </div>

                <button class="cb-btn cb-btn-accent" id="cb-generate-btn" disabled>
                    \u{1F4CB} Copy Prompt for ${targetName} (with files)
                </button>

                <div class="cb-status" id="cb-status"></div>
                <div class="cb-stats" id="cb-stats"></div>
            </div>
        </div>
        <button id="contextbridge-fab" title="ContextBridge">\u{1F309}</button>
    `;

    document.body.appendChild(widget);

    // State
    let attachedFiles = [];

    // Toggle panel
    document.getElementById('contextbridge-fab').addEventListener('click', async () => {
        const panel = document.getElementById('contextbridge-panel');
        panel.classList.toggle('visible');

        const nameInput = document.getElementById('cb-package-name');
        if (!nameInput.value) {
            const title = getConversationTitle();
            nameInput.value = title.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '').substring(0, 30);
        }

        // Check for watched files
        if (panel.classList.contains('visible')) {
            const watchData = await checkWatchFiles();
            const watchInfo = document.getElementById('cb-watch-info');
            if (watchData.files && watchData.files.length > 0) {
                document.getElementById('cb-watch-count').textContent = watchData.files.length;
                watchInfo.style.display = 'block';
                watchInfo.title = watchData.files.map(f => f.name).join('\n');
            } else {
                watchInfo.style.display = 'none';
            }
        }
    });

    // File drop zone click
    document.getElementById('cb-file-drop').addEventListener('click', () => {
        document.getElementById('cb-file-input').click();
    });

    // File selection
    document.getElementById('cb-file-input').addEventListener('change', (e) => {
        const newFiles = Array.from(e.target.files);
        attachedFiles = [...attachedFiles, ...newFiles];
        updateFileList();
        e.target.value = '';
    });

    function updateFileList() {
        const list = document.getElementById('cb-file-list');
        if (attachedFiles.length === 0) {
            list.innerHTML = '';
            return;
        }
        list.innerHTML = attachedFiles.map((f, i) => {
            const size = f.size < 1024 ? f.size + 'B' : (f.size / 1024).toFixed(1) + 'KB';
            return `<div class="cb-file-item">
                <span>\u{1F4C4} ${f.name} <small>(${size})</small></span>
                <button class="cb-file-remove" data-idx="${i}">\u2715</button>
            </div>`;
        }).join('');

        // Remove buttons
        list.querySelectorAll('.cb-file-remove').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.dataset.idx);
                attachedFiles.splice(idx, 1);
                updateFileList();
            });
        });
    }

    // Extract button
    document.getElementById('cb-extract-btn').addEventListener('click', async () => {
        const packageName = document.getElementById('cb-package-name').value.trim();
        if (!packageName) { showStatus('Enter a package name', 'error'); return; }

        const chatText = scrapeChat();
        if (!chatText || chatText.length < 20) {
            showStatus('Could not detect chat on this page', 'error');
            return;
        }

        setWorking(true);
        showStatus('');

        try {
            // Step 1: Extract chat
            const result = await sendToContextBridge(chatText, packageName);
            let statusMsg = `\u2705 Extracted ${result.total_items} items`;

            // Step 2: Upload attached files (if any)
            if (attachedFiles.length > 0) {
                showStatus('Uploading attached files...', 'success');
                const fileResult = await attachFiles(attachedFiles, packageName);
                statusMsg += ` + ${fileResult.files_attached.length} files attached`;
            }

            showStatus(statusMsg, 'success');
            document.getElementById('cb-stats').textContent =
                `${result.chars_extracted.toLocaleString()} chars from chat` +
                (attachedFiles.length > 0 ? ` + ${attachedFiles.length} files` : '');
            document.getElementById('cb-generate-btn').disabled = false;

        } catch (err) {
            if (err.message.includes('Failed to fetch')) {
                showStatus('Server not running. Run: python -m contextbridge.web.app', 'error');
            } else {
                showStatus(err.message, 'error');
            }
        } finally {
            setWorking(false);
        }
    });

    // Generate prompt button
    document.getElementById('cb-generate-btn').addEventListener('click', async () => {
        const packageName = document.getElementById('cb-package-name').value.trim();
        setWorking(true);

        try {
            const result = await generatePrompt(packageName, targetModel);

            await navigator.clipboard.writeText(result.prompt);

            const fileInfo = result.attached_files && result.attached_files.length > 0
                ? ` (includes ${result.attached_files.length} files)`
                : '';

            showStatus(
                `\u2705 Copied! Open ${targetName} \u2192 Ctrl+V \u2192 Send${fileInfo}`,
                'success'
            );
        } catch (err) {
            if (err.name === 'NotAllowedError') {
                showStatus('Clipboard blocked \u2014 open dashboard at localhost:5000 to copy', 'error');
            } else {
                showStatus(err.message, 'error');
            }
        } finally {
            setWorking(false);
        }
    });
}

function showStatus(message, type) {
    const el = document.getElementById('cb-status');
    if (!message) { el.style.display = 'none'; return; }
    el.textContent = message;
    el.className = `cb-status ${type}`;
}

function setWorking(working) {
    const fab = document.getElementById('contextbridge-fab');
    const extractBtn = document.getElementById('cb-extract-btn');
    if (working) { fab.classList.add('working'); extractBtn.disabled = true; }
    else { fab.classList.remove('working'); extractBtn.disabled = false; }
}

// -------------------------------------------------------------------
// Init
// -------------------------------------------------------------------

function init() {
    if (!detectSite()) return;
    setTimeout(createWidget, 2000);
}

init();
