// ====================================================================
// ContextBridge Dashboard — Frontend Logic
// ====================================================================

const CATEGORY_LABELS = {
    identity: { icon: '\u{1F464}', label: 'Identity' },
    projects: { icon: '\u{1F4C1}', label: 'Projects' },
    facts: { icon: '\u{1F4CC}', label: 'Facts' },
    decisions: { icon: '\u2696\uFE0F', label: 'Decisions' },
    open_tasks: { icon: '\u{1F4CB}', label: 'Open Tasks' },
    preferences: { icon: '\u2699\uFE0F', label: 'Preferences' },
};

const FILE_ICONS = {
    zip: '\u{1F4E6}',
    pdf: '\u{1F4D1}',
    json: '\u{1F4CA}',
    txt: '\u{1F4C4}',
    md: '\u{1F4DD}',
    html: '\u{1F310}',
    htm: '\u{1F310}',
};

let selectedFiles = [];
let generatedPrompt = '';

// -------------------------------------------------------------------
// Init
// -------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    loadPackages();
    loadLocalModels();
    setupDropZone();
    setupFileInput();
});

// -------------------------------------------------------------------
// Drag & Drop
// -------------------------------------------------------------------

function setupDropZone() {
    const zone = document.getElementById('drop-zone');

    zone.addEventListener('click', () => {
        document.getElementById('file-input').click();
    });

    zone.addEventListener('dragover', (e) => {
        e.preventDefault();
        zone.classList.add('drag-over');
    });

    zone.addEventListener('dragleave', () => {
        zone.classList.remove('drag-over');
    });

    zone.addEventListener('drop', (e) => {
        e.preventDefault();
        zone.classList.remove('drag-over');
        const files = Array.from(e.dataTransfer.files);
        addFiles(files);
    });

    // Prevent full-page drag
    document.addEventListener('dragover', (e) => e.preventDefault());
    document.addEventListener('drop', (e) => e.preventDefault());
}

function setupFileInput() {
    document.getElementById('file-input').addEventListener('change', (e) => {
        const files = Array.from(e.target.files);
        addFiles(files);
        e.target.value = ''; // reset so same file can be re-added
    });
}

// -------------------------------------------------------------------
// File Management
// -------------------------------------------------------------------

function addFiles(files) {
    for (const file of files) {
        // Avoid duplicates by name+size
        const exists = selectedFiles.some(f => f.name === file.name && f.size === file.size);
        if (!exists) {
            selectedFiles.push(file);
        }
    }
    updateFileList();
    showToast(`${files.length} file(s) added`, 'success');
}

function removeFile(index) {
    selectedFiles.splice(index, 1);
    updateFileList();
}

function clearFiles() {
    selectedFiles = [];
    updateFileList();
}

function updateFileList() {
    const listEl = document.getElementById('file-list');
    const itemsEl = document.getElementById('file-items');
    const configEl = document.getElementById('extract-config');
    const btnEl = document.getElementById('extract-btn');
    const countEl = document.getElementById('file-count');

    if (selectedFiles.length === 0) {
        listEl.style.display = 'none';
        configEl.style.display = 'none';
        btnEl.style.display = 'none';
        return;
    }

    listEl.style.display = 'block';
    configEl.style.display = 'grid';
    btnEl.style.display = 'flex';
    countEl.textContent = `${selectedFiles.length} file${selectedFiles.length > 1 ? 's' : ''} selected`;

    itemsEl.innerHTML = selectedFiles.map((file, i) => {
        const ext = file.name.split('.').pop().toLowerCase();
        const icon = FILE_ICONS[ext] || '\u{1F4C4}';
        const size = formatSize(file.size);
        return `
            <div class="file-item">
                <div class="file-item-info">
                    <span class="file-item-icon">${icon}</span>
                    <span class="file-item-name">${escapeHtml(file.name)}</span>
                    <span class="file-item-size">${size}</span>
                </div>
                <button class="file-item-remove" onclick="removeFile(${i})" title="Remove">\u2715</button>
            </div>
        `;
    }).join('');
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// -------------------------------------------------------------------
// Extract Context
// -------------------------------------------------------------------

async function extractContext() {
    const packageName = document.getElementById('package-name').value.trim();
    const model = document.getElementById('source-model').value;

    if (selectedFiles.length === 0) return showToast('Drop some files first', 'error');
    if (!packageName) return showToast('Enter a package name', 'error');

    showLoading('Extracting structured memory from your files...');
    disableBtn('extract-btn', true);

    try {
        const formData = new FormData();
        formData.append('model', model);
        formData.append('package_name', packageName);
        for (const file of selectedFiles) {
            formData.append('files[]', file);
        }

        const res = await fetch('/api/extract', {
            method: 'POST',
            body: formData,
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Extraction failed');

        displayResults(data);
        loadPackages();
        showToast(
            `Extracted ${data.total_items} memory items from ${data.files_processed.length} file(s)`,
            'success'
        );
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        hideLoading();
        disableBtn('extract-btn', false);
    }
}

function displayResults(data) {
    const panel = document.getElementById('extract-results');
    const grid = document.getElementById('memory-grid');
    const meta = document.getElementById('results-meta');
    const filesDiv = document.getElementById('files-processed');

    meta.textContent = `${data.package_name} v${data.version} \u2014 ${data.total_items} items \u2014 ${data.chars_extracted.toLocaleString()} chars processed`;

    // Show processed files
    filesDiv.innerHTML = data.files_processed
        .map(f => `<span class="file-chip">\u2713 ${escapeHtml(f)}</span>`)
        .join('');

    // Show memory categories
    grid.innerHTML = '';
    for (const [category, info] of Object.entries(CATEGORY_LABELS)) {
        const items = data.memory[category] || [];
        if (items.length === 0) continue;

        const div = document.createElement('div');
        div.className = 'memory-category';
        div.innerHTML = `
            <div class="memory-category-header">
                ${info.icon} ${info.label}
                <span class="memory-category-count">(${items.length})</span>
            </div>
            ${items.map(i => `<div class="memory-item">${escapeHtml(i.content)}</div>`).join('')}
        `;
        grid.appendChild(div);
    }

    panel.style.display = 'block';
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// -------------------------------------------------------------------
// Generate Prompt
// -------------------------------------------------------------------

async function generatePrompt() {
    const packageName = document.getElementById('prompt-package').value;
    const targetModel = document.getElementById('target-model').value;

    if (!packageName) return showToast('Select a context package first', 'error');

    showLoading('Generating prompt...');
    disableBtn('prompt-btn', true);

    try {
        const res = await fetch('/api/prompt', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ package_name: packageName, target_model: targetModel }),
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Prompt generation failed');

        generatedPrompt = data.prompt;
        document.getElementById('prompt-output').textContent = data.prompt;
        document.getElementById('prompt-target-label').textContent = `Ready for ${data.target_model}`;
        document.getElementById('prompt-results').style.display = 'block';

        // Reset copy button
        document.getElementById('copy-icon').innerHTML = '\u{1F4CB}';
        document.getElementById('copy-text').textContent = 'Copy to Clipboard';

        // Auto-copy
        try {
            await navigator.clipboard.writeText(generatedPrompt);
            document.getElementById('copy-icon').innerHTML = '\u2705';
            document.getElementById('copy-text').textContent = 'Copied!';
            showToast(`Copied! Open ${data.target_model} web \u2192 Ctrl+V \u2192 Send`, 'success');
        } catch {
            showToast('Prompt generated \u2014 click Copy to copy to clipboard', 'success');
        }

        document.getElementById('prompt-results').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        hideLoading();
        disableBtn('prompt-btn', false);
    }
}

async function copyPrompt() {
    if (!generatedPrompt) return;

    try {
        await navigator.clipboard.writeText(generatedPrompt);
        document.getElementById('copy-icon').innerHTML = '\u2705';
        document.getElementById('copy-text').textContent = 'Copied!';
        showToast('Copied to clipboard \u2014 paste into your web chat!', 'success');

        setTimeout(() => {
            document.getElementById('copy-icon').innerHTML = '\u{1F4CB}';
            document.getElementById('copy-text').textContent = 'Copy to Clipboard';
        }, 3000);
    } catch {
        const pre = document.getElementById('prompt-output');
        const range = document.createRange();
        range.selectNodeContents(pre);
        window.getSelection().removeAllRanges();
        window.getSelection().addRange(range);
        showToast('Text selected \u2014 press Ctrl+C to copy', 'warning');
    }
}

// -------------------------------------------------------------------
// Packages
// -------------------------------------------------------------------

async function loadPackages() {
    try {
        const res = await fetch('/api/packages');
        const data = await res.json();

        const list = document.getElementById('packages-list');
        const select = document.getElementById('prompt-package');

        select.innerHTML = '<option value="">Select a package</option>';

        if (!data.packages || data.packages.length === 0) {
            list.innerHTML = '<p class="empty-state">No packages yet. Drop files above to create your first one.</p>';
            return;
        }

        list.innerHTML = '';
        for (const pkg of data.packages) {
            const item = document.createElement('div');
            item.className = 'package-item';
            item.innerHTML = `
                <div class="package-info">
                    <h4>\u{1F4E6} ${escapeHtml(pkg.name)}</h4>
                    <div class="package-meta">
                        <span>v${pkg.version}</span>
                        <span>${pkg.source_model}</span>
                        <span>${pkg.total_items} items</span>
                        <span>${pkg.updated_at}</span>
                    </div>
                </div>
                <div class="package-actions">
                    <button class="btn btn-sm btn-ghost" onclick="selectPackage('${escapeAttr(pkg.name)}')">
                        \u{1F3AF} Use
                    </button>
                    <button class="btn btn-danger" onclick="deletePackage('${escapeAttr(pkg.name)}')">
                        \u{1F5D1}\uFE0F
                    </button>
                </div>
            `;
            list.appendChild(item);

            const option = document.createElement('option');
            option.value = pkg.name;
            option.textContent = `${pkg.name} (v${pkg.version} \xB7 ${pkg.total_items} items)`;
            select.appendChild(option);
        }
    } catch {
        document.getElementById('packages-list').innerHTML =
            `<p class="empty-state" style="color:var(--error)">Error loading packages</p>`;
    }
}

function selectPackage(name) {
    document.getElementById('prompt-package').value = name;
    document.querySelector('.prompt-card').scrollIntoView({ behavior: 'smooth' });
    showToast(`Selected "${name}" \u2014 choose target model and click Generate`, 'success');
}

async function deletePackage(name) {
    if (!confirm(`Delete package "${name}"? This cannot be undone.`)) return;

    try {
        const res = await fetch(`/api/packages/${encodeURIComponent(name)}`, { method: 'DELETE' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error);
        loadPackages();
        showToast(`Deleted "${name}"`, 'success');
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// -------------------------------------------------------------------
// UI Helpers
// -------------------------------------------------------------------

function showLoading(text) {
    document.getElementById('loading-text').textContent = text;
    document.getElementById('loading-overlay').style.display = 'flex';
}

function hideLoading() {
    document.getElementById('loading-overlay').style.display = 'none';
}

function disableBtn(id, disabled) {
    document.getElementById(id).disabled = disabled;
}

function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = `toast ${type}`;
    toast.style.display = 'block';
    setTimeout(() => { toast.style.display = 'none'; }, 3500);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function escapeAttr(text) {
    return text.replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

async function loadLocalModels() {
    const group = document.getElementById('ollama-models');
    try {
        const res = await fetch('/api/local/models');
        const data = await res.json();

        if (data.models && data.models.length > 0) {
            group.innerHTML = data.models.map(m =>
                `<option value="${m}">${m} (Ollama)</option>`
            ).join('');
        } else {
            group.innerHTML = '<option value="local" disabled>No models found (check Ollama)</option>';
        }
    } catch {
        group.innerHTML = '<option value="local" disabled>Ollama unreachable</option>';
    }
}
