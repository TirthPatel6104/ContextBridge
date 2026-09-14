// ====================================================================
// ContextBridge Dashboard — import → review → retrieve → export
// Plain DOM, no framework, no inline handlers. All calls are same-origin.
// ====================================================================
'use strict';

const CATEGORY_LABELS = {
  identity: 'Identity',
  projects: 'Projects',
  facts: 'Facts',
  decisions: 'Decisions',
  open_tasks: 'Open tasks',
  preferences: 'Preferences',
};

const state = {
  files: [],
  packages: [],
  activePackage: null,       // detail payload from /api/packages/<name>
  lastImport: null,          // last /api/extract payload (for transcript token estimate)
  lastRetrieval: null,       // { query, options, payload }
  generatedPrompt: '',
  health: null,
};

const $ = (id) => document.getElementById(id);

// -------------------------------------------------------------------
// Small helpers
// -------------------------------------------------------------------

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function fmt(n) {
  return typeof n === 'number' ? n.toLocaleString() : '–';
}

function pct(part, whole) {
  if (!whole) return '–';
  return `${Math.round((part / whole) * 100)}%`;
}

async function api(url, options = {}) {
  const res = await fetch(url, options);
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const message = (data && data.error) || `Request failed (${res.status})`;
    throw new Error(message);
  }
  return data;
}

function setBusy(button, busy, busyLabel) {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = busyLabel || 'Working…';
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  } else {
    if (button.dataset.label) button.textContent = button.dataset.label;
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}

function showError(id, message) {
  const node = $(id);
  if (!node) return;
  if (!message) { node.hidden = true; node.textContent = ''; return; }
  node.textContent = message;
  node.hidden = false;
}

let toastTimer = null;
function toast(message, kind = 'info') {
  const node = $('toast');
  node.textContent = message;
  node.className = `toast toast-${kind}`;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 4000);
}

function confidenceBar(value) {
  const percent = Math.round((value || 0) * 100);
  return el('span', { class: 'confidence', title: `Confidence ${percent}%`, 'aria-label': `Confidence ${percent}%` }, [
    el('span', { class: 'confidence-fill', style: `width:${percent}%` }),
  ]);
}

function categoryChip(category) {
  return el('span', { class: `chip chip-${category}` }, CATEGORY_LABELS[category] || category);
}

function sourceNode(source, inline = false) {
  if (!source) return null;
  if (source.startsWith('rule:')) {
    return el('span', { class: 'muted small', title: 'Produced by the offline rule-based extractor', text: `detected by rule: ${source.slice(5).replace(/_/g, ' ')}` });
  }
  return el('details', { class: inline ? 'source inline' : 'source' }, [
    el('summary', { text: inline ? 'source excerpt' : 'Source excerpt' }),
    el('blockquote', { text: source }),
  ]);
}

function redactionBadge(redactions) {
  if (!redactions || !redactions.length) return null;
  const kinds = redactions.map((r) => `${r.kind} ×${r.count}`).join(', ');
  return el('span', { class: 'badge badge-redacted', title: `Redacted: ${kinds}` }, `🔒 redacted (${redactions.map((r) => r.kind).join(', ')})`);
}

// -------------------------------------------------------------------
// Init
// -------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', async () => {
  setupDropZone();
  setupImportForm();
  setupReview();
  setupRetrieve();
  setupExport();
  setupTools();
  setupQuality();
  await Promise.all([loadHealth(), loadPackages(), loadLocalModels()]);
});

async function loadHealth() {
  try {
    const health = await api('/api/health');
    state.health = health;
    $('status-storage').textContent = health.storage_backend;
    $('status-redaction').textContent = health.redact_by_default ? 'on by default' : 'off by default';
    $('footer-location').textContent = health.storage_location;
    $('max-upload').textContent = health.max_upload_mb;
    $('redact').checked = health.redact_by_default;
    renderCategoryFilters(health.categories);
  } catch {
    $('status-storage').textContent = 'unavailable';
  }
}

async function loadLocalModels() {
  const group = $('ollama-models');
  try {
    const data = await api('/api/local/models');
    group.replaceChildren();
    if (!data.models || !data.models.length) {
      group.append(el('option', { value: '', disabled: true }, 'No Ollama models detected'));
      return;
    }
    for (const model of data.models) {
      group.append(el('option', { value: model }, `${model} (Ollama, local)`));
    }
  } catch {
    group.replaceChildren(el('option', { value: '', disabled: true }, 'Ollama unreachable'));
  }
}

// -------------------------------------------------------------------
// 1. Import
// -------------------------------------------------------------------

function setupDropZone() {
  const zone = $('drop-zone');
  const input = $('file-input');
  const open = () => input.click();
  zone.addEventListener('click', open);
  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
  });
  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    addFiles(Array.from(e.dataTransfer.files));
  });
  input.addEventListener('change', (e) => { addFiles(Array.from(e.target.files)); e.target.value = ''; });
  document.addEventListener('dragover', (e) => e.preventDefault());
  document.addEventListener('drop', (e) => e.preventDefault());
  $('clear-files-btn').addEventListener('click', () => { state.files = []; renderFiles(); });
}

function addFiles(files) {
  const maxBytes = ((state.health && state.health.max_upload_mb) || 25) * 1024 * 1024;
  let added = 0;
  for (const file of files) {
    if (file.size > maxBytes) { toast(`${file.name} is larger than the upload limit`, 'error'); continue; }
    if (state.files.some((f) => f.name === file.name && f.size === file.size)) continue;
    state.files.push(file);
    added += 1;
  }
  renderFiles();
  if (added) {
    const nameInput = $('package-name');
    if (!nameInput.value && state.files[0]) nameInput.value = suggestName(state.files[0].name);
    nameInput.focus();
  }
}

function suggestName(filename) {
  const stem = filename.replace(/\.[^.]+$/, '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return stem && /^[a-z0-9]/.test(stem) ? stem.slice(0, 64) : 'imported_context';
}

function renderFiles() {
  const list = $('file-list');
  const form = $('import-form');
  list.replaceChildren();
  if (!state.files.length) { list.hidden = true; form.hidden = true; return; }
  state.files.forEach((file, index) => {
    list.append(el('li', { class: 'file-item' }, [
      el('span', { class: 'file-name', text: file.name }),
      el('span', { class: 'file-size muted', text: formatSize(file.size) }),
      el('button', { type: 'button', class: 'icon-btn', 'aria-label': `Remove ${file.name}`, onclick: () => { state.files.splice(index, 1); renderFiles(); } }, '✕'),
    ]));
  });
  list.hidden = false;
  form.hidden = false;
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function setupImportForm() {
  $('import-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    showError('import-error');
    const name = $('package-name').value.trim();
    if (!state.files.length) return showError('import-error', 'Add at least one file first.');
    if (!$('package-name').checkValidity() || !name) {
      return showError('import-error', 'Package name may only contain letters, digits, _ - . and must start with a letter or digit.');
    }
    const body = new FormData();
    body.append('package_name', name);
    body.append('model', $('engine').value || 'local');
    body.append('redact', $('redact').checked ? 'true' : 'false');
    body.append('mode', document.querySelector('input[name="mode"]:checked').value);
    for (const file of state.files) body.append('files[]', file);

    const btn = $('extract-btn');
    setBusy(btn, true, 'Extracting…');
    try {
      const data = await api('/api/extract', { method: 'POST', body });
      state.lastImport = data;
      renderImportResult(data);
      state.files = [];
      renderFiles();
      await loadPackages();
      await selectPackage(data.package_name);
      toast(`${data.extracted_items} items extracted into ${data.package_name} v${data.version}`, 'success');
    } catch (err) {
      showError('import-error', err.message);
    } finally {
      setBusy(btn, false);
    }
  });
}

function renderImportResult(data) {
  const panel = $('import-result');
  const verb = data.created ? 'Created' : 'Updated';
  $('import-summary').textContent =
    `${verb} ${data.package_name} v${data.version} · ${data.extracted_items} items extracted, ` +
    `${data.added_items} new · engine: ${data.engine} · transcript ≈ ${fmt(data.tokens_transcript)} tokens ` +
    `from ${data.files_processed.join(', ')}`;

  const warnings = $('import-warnings');
  warnings.replaceChildren();
  warnings.hidden = !(data.warnings && data.warnings.length);
  (data.warnings || []).forEach((w) => warnings.append(el('li', { text: w })));

  const red = $('import-redaction');
  red.replaceChildren();
  if (data.redaction && data.redaction.total > 0) {
    const parts = Object.entries(data.redaction.counts).map(([kind, n]) => `${n} × ${data.redaction.labels[kind] || kind}`);
    red.append(
      el('p', {}, [el('strong', { text: `🔒 ${data.redaction.total} value(s) redacted in ${data.redaction.items_redacted} item(s): ` }), parts.join(', ')]),
      el('p', { class: 'help', text: 'Redacted items are marked in step 2. If something was a false positive, re-import with redaction off — the original file was not changed.' }),
    );
    red.hidden = false;
  } else {
    red.append(el('p', { class: 'help', text: data.redaction ? 'No secrets or personal data detected.' : 'Redaction was disabled for this import.' }));
    red.hidden = false;
  }
  panel.hidden = false;
}

// -------------------------------------------------------------------
// Packages (shared by steps 2–4)
// -------------------------------------------------------------------

async function loadPackages() {
  try {
    const data = await api('/api/packages');
    state.packages = data.packages || [];
  } catch (err) {
    state.packages = [];
    toast(`Could not load packages: ${err.message}`, 'error');
  }
  const select = $('active-package');
  const current = select.value;
  select.replaceChildren(el('option', { value: '' }, state.packages.length ? '— choose a package —' : 'No packages yet'));
  const datalist = $('package-names');
  datalist.replaceChildren();
  const mergeSelect = $('merge-sources');
  const mergeSelected = new Set(Array.from(mergeSelect.selectedOptions).map((o) => o.value));
  mergeSelect.replaceChildren();
  for (const pkg of state.packages) {
    select.append(el('option', { value: pkg.name }, `${pkg.name} (v${pkg.version} · ${pkg.total_items} items · ${pkg.source_model})`));
    datalist.append(el('option', { value: pkg.name }));
    mergeSelect.append(el('option', { value: pkg.name, selected: mergeSelected.has(pkg.name) }, `${pkg.name} (${pkg.total_items} items)`));
  }
  if (current && state.packages.some((p) => p.name === current)) select.value = current;
  else if (state.activePackage && !state.packages.some((p) => p.name === state.activePackage.name)) clearActivePackage();
}

function clearActivePackage() {
  state.activePackage = null;
  $('active-package').value = '';
  $('review-body').hidden = true;
  $('review-empty').hidden = false;
  $('remove-selected-btn').disabled = true;
  $('delete-package-btn').disabled = true;
  $('export-link').hidden = true;
  $('retrieve-result').hidden = true;
  $('retrieve-empty').hidden = false;
  $('prompt-result').hidden = true;
  state.lastRetrieval = null;
}

async function selectPackage(name) {
  if (!name) { clearActivePackage(); return; }
  showError('review-error');
  try {
    const detail = await api(`/api/packages/${encodeURIComponent(name)}`);
    state.activePackage = detail;
    $('active-package').value = name;
    renderReview(detail);
    $('delete-package-btn').disabled = false;
    const link = $('export-link');
    link.href = `/api/packages/${encodeURIComponent(name)}/export`;
    link.setAttribute('download', `${name}.contextbridge.json`);
    link.hidden = false;
    $('retrieve-empty').textContent = `Run a query against "${name}" to preview which items would be included.`;
  } catch (err) {
    clearActivePackage();
    showError('review-error', err.message);
  }
}

// -------------------------------------------------------------------
// 2. Review
// -------------------------------------------------------------------

function setupReview() {
  $('active-package').addEventListener('change', (e) => selectPackage(e.target.value));
  $('refresh-packages-btn').addEventListener('click', async () => {
    await loadPackages();
    if (state.activePackage) await selectPackage(state.activePackage.name);
    toast('Package list refreshed');
  });
  $('memory-groups').addEventListener('change', (e) => {
    if (e.target.matches('input[type="checkbox"][data-item-id]')) updateRemoveButton();
  });
  $('remove-selected-btn').addEventListener('click', removeSelectedItems);
  $('history-table').addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-version]');
    if (!btn || !state.activePackage) return;
    const version = Number(btn.dataset.version);
    setBusy(btn, true, 'Rolling back…');
    try {
      const data = await api(`/api/packages/${encodeURIComponent(state.activePackage.name)}/rollback`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version }),
      });
      toast(`Rolled back to v${data.version}`, 'success');
      await loadPackages();
      await selectPackage(state.activePackage.name);
    } catch (err) {
      showError('review-error', err.message);
      setBusy(btn, false);
    }
  });
}

function updateRemoveButton() {
  const count = document.querySelectorAll('#memory-groups input[type="checkbox"][data-item-id]:checked').length;
  const btn = $('remove-selected-btn');
  btn.disabled = count === 0;
  btn.textContent = count ? `Remove ${count} selected` : 'Remove selected';
}

function renderReview(detail) {
  $('review-empty').hidden = true;
  $('review-body').hidden = false;
  const redacted = Object.values(detail.memory).flat().filter((i) => i.redactions && i.redactions.length).length;
  $('review-meta').textContent =
    `${detail.name} v${detail.version} · ${detail.total_items} items · source: ${detail.source_model || 'unknown'} · ` +
    `≈ ${fmt(detail.tokens_full_memory)} tokens if sent in full · ${redacted} redacted item(s) · updated ${detail.updated_at.slice(0, 16).replace('T', ' ')}`;

  const groups = $('memory-groups');
  groups.replaceChildren();
  let any = false;
  for (const [category, label] of Object.entries(CATEGORY_LABELS)) {
    const items = detail.memory[category] || [];
    if (!items.length) continue;
    any = true;
    const list = el('ul', { class: 'memory-list' });
    for (const item of items) list.append(renderMemoryItem(item));
    groups.append(el('section', { class: 'memory-group', 'aria-labelledby': `group-${category}` }, [
      el('h3', { id: `group-${category}` }, [categoryChip(category), el('span', { class: 'muted', text: ` ${items.length}` })]),
      list,
    ]));
  }
  if (!any) groups.append(el('p', { class: 'empty-state', text: 'This package has no memory items. Import a richer transcript or try an LLM engine.' }));
  updateRemoveButton();

  const tbody = $('history-table').querySelector('tbody');
  tbody.replaceChildren();
  for (const entry of detail.history) {
    const isCurrent = entry.version === detail.version;
    tbody.append(el('tr', {}, [
      el('td', {}, [`v${entry.version}`, isCurrent ? el('span', { class: 'badge', text: 'current' }) : null]),
      el('td', { text: entry.timestamp.slice(0, 16).replace('T', ' ') }),
      el('td', { text: entry.source_model || '—' }),
      el('td', { text: entry.note || '—' }),
      el('td', { text: String(entry.added) }),
      el('td', { text: String(entry.removed) }),
      el('td', {}, isCurrent || entry.version >= detail.version ? null : el('button', { type: 'button', class: 'btn btn-ghost btn-sm', dataset: { version: String(entry.version) } }, `Roll back to v${entry.version}`)),
    ]));
  }
}

function renderMemoryItem(item) {
  const checkboxId = `item-${item.id}`;
  return el('li', { class: 'memory-item' }, [
    el('input', { type: 'checkbox', id: checkboxId, dataset: { itemId: item.id }, 'aria-label': `Select item: ${item.content}` }),
    el('div', { class: 'memory-item-body' }, [
      el('label', { for: checkboxId, class: 'memory-content', text: item.content }),
      el('div', { class: 'memory-meta' }, [
        confidenceBar(item.confidence),
        el('span', { class: 'muted', text: `${Math.round(item.confidence * 100)}% confidence` }),
        item.origin ? el('span', { class: 'muted', title: 'Where this item came from', text: `from ${item.origin}` }) : null,
        el('span', { class: 'muted', text: `≈ ${item.tokens} tok` }),
        redactionBadge(item.redactions),
      ]),
      sourceNode(item.source),
    ]),
  ]);
}

async function removeSelectedItems() {
  if (!state.activePackage) return;
  const ids = Array.from(document.querySelectorAll('#memory-groups input[type="checkbox"][data-item-id]:checked')).map((c) => c.dataset.itemId);
  if (!ids.length) return;
  const btn = $('remove-selected-btn');
  setBusy(btn, true, 'Removing…');
  try {
    const data = await api(`/api/packages/${encodeURIComponent(state.activePackage.name)}/items/remove`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids, note: 'removed in dashboard review' }),
    });
    toast(`Removed ${ids.length} item(s) → v${data.version}. Roll back from the history table if needed.`, 'success');
    await loadPackages();
    await selectPackage(state.activePackage.name);
  } catch (err) {
    showError('review-error', err.message);
  } finally {
    setBusy(btn, false);
    updateRemoveButton();
  }
}

// -------------------------------------------------------------------
// 3. Retrieve
// -------------------------------------------------------------------

function renderCategoryFilters(categories) {
  const row = $('category-filters');
  row.replaceChildren();
  for (const category of categories || Object.keys(CATEGORY_LABELS)) {
    const id = `cat-${category}`;
    row.append(el('label', { class: 'chip-toggle', for: id }, [
      el('input', { type: 'checkbox', id, value: category, checked: true }),
      el('span', { text: CATEGORY_LABELS[category] || category }),
    ]));
  }
}

function currentRetrievalOptions() {
  const categories = Array.from(document.querySelectorAll('#category-filters input:checked')).map((c) => c.value);
  const all = document.querySelectorAll('#category-filters input').length;
  const options = {
    top_k: Number($('top-k').value) || 5,
    min_score: Number($('min-score').value) || 0,
  };
  if ($('token-budget').value) options.token_budget = Number($('token-budget').value);
  if (categories.length && categories.length < all) options.categories = categories;
  return options;
}

function setupRetrieve() {
  $('min-score').addEventListener('input', (e) => { $('min-score-out').textContent = Number(e.target.value).toFixed(2); });
  $('retrieve-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    showError('retrieve-error');
    if (!state.activePackage) return showError('retrieve-error', 'Choose a package in step 2 first.');
    const query = $('query').value.trim();
    const options = currentRetrievalOptions();
    const btn = $('retrieve-btn');
    setBusy(btn, true, 'Scoring…');
    try {
      const data = await api('/api/retrieve', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ package_name: state.activePackage.name, query, ...options }),
      });
      state.lastRetrieval = { query, options, payload: data };
      renderRetrieval(data);
      document.querySelector('input[name="scope"][value="retrieval"]').checked = true;
    } catch (err) {
      showError('retrieve-error', err.message);
    } finally {
      setBusy(btn, false);
    }
  });
}

function renderRetrieval(data) {
  const s = data.summary;
  $('retrieve-empty').hidden = true;
  $('retrieve-result').hidden = false;
  $('stat-selected').textContent = `${s.selected} of ${s.considered} considered (${s.total_items} total)`;
  $('stat-method').textContent = s.method === 'hybrid' ? 'lexical + embeddings' : 'lexical (BM25)';
  $('stat-tokens').textContent = `≈ ${fmt(s.tokens_selected)}`;
  $('stat-saved').textContent = `≈ ${fmt(s.tokens_saved_vs_full_memory)} (${pct(s.tokens_saved_vs_full_memory, s.tokens_full_memory)} of ${fmt(s.tokens_full_memory)})`;
  const transcriptTokens = state.lastImport && state.lastImport.package_name === data.package_name ? state.lastImport.tokens_transcript : null;
  $('stat-saved-transcript').textContent = transcriptTokens
    ? `≈ ${fmt(Math.max(0, transcriptTokens - s.tokens_selected))} (${pct(transcriptTokens - s.tokens_selected, transcriptTokens)} of ${fmt(transcriptTokens)})`
    : 'import a transcript this session to compare';

  const list = $('retrieve-list');
  list.replaceChildren();
  if (!data.selected.length) {
    list.append(el('li', { class: 'empty-state', text: data.query ? 'No items matched. Try broader wording, lower the minimum score, or widen the categories.' : 'No items available.' }));
  }
  for (const sel of data.selected) {
    const item = sel.item;
    list.append(el('li', { class: 'retrieve-item' }, [
      el('div', { class: 'retrieve-rank', 'aria-label': `Rank ${sel.rank}`, text: `#${sel.rank}` }),
      el('div', { class: 'retrieve-body' }, [
        el('div', { class: 'retrieve-head' }, [
          categoryChip(item.category),
          el('span', { class: 'score', title: 'Relevance score (0–1)' }, [
            el('span', { class: 'score-bar' }, [el('span', { class: 'score-fill', style: `width:${Math.round(sel.score * 100)}%` })]),
            el('span', { text: sel.score.toFixed(2) }),
          ]),
          el('span', { class: 'muted', text: `≈ ${sel.tokens} tok` }),
          redactionBadge(item.redactions),
        ]),
        el('p', { class: 'memory-content', text: item.content }),
        el('p', { class: 'why' }, [
          el('strong', { text: 'Why: ' }),
          sel.reason,
          sel.matched_terms.length ? el('span', { class: 'terms' }, sel.matched_terms.map((t) => el('code', { text: t }))) : null,
        ]),
        el('p', { class: 'muted small' }, [
          item.origin ? `from ${item.origin}` : null,
          sourceNode(item.source, true),
        ]),
      ]),
    ]));
  }
  const dropped = [];
  if (s.dropped_below_min_score) dropped.push(`${s.dropped_below_min_score} below the minimum score`);
  if (s.dropped_for_budget) dropped.push(`${s.dropped_for_budget} skipped to respect the token budget`);
  $('retrieve-dropped').textContent = dropped.length ? `Not included: ${dropped.join(' · ')}.` : '';
}

// -------------------------------------------------------------------
// 4. Export
// -------------------------------------------------------------------

function setupExport() {
  $('prompt-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    showError('prompt-error');
    if (!state.activePackage) return showError('prompt-error', 'Choose a package in step 2 first.');
    const scope = document.querySelector('input[name="scope"]:checked').value;
    const body = {
      package_name: state.activePackage.name,
      target_model: $('target-model').value,
      include_files: $('include-files').checked,
    };
    if (scope === 'retrieval') {
      if (!state.lastRetrieval || !state.lastRetrieval.query) {
        return showError('prompt-error', 'Run a retrieval preview with a query in step 3 first, or choose "Whole package".');
      }
      body.query = state.lastRetrieval.query;
      Object.assign(body, state.lastRetrieval.options);
    }
    const btn = $('prompt-btn');
    setBusy(btn, true, 'Building…');
    try {
      const data = await api('/api/prompt', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      state.generatedPrompt = data.prompt;
      $('prompt-output').value = data.prompt;
      const saved = data.tokens_full_prompt - data.tokens_prompt;
      $('prompt-meta').textContent =
        `Ready for ${data.target_name} · ${data.included_items} of ${data.total_items} items · ≈ ${fmt(data.tokens_prompt)} tokens` +
        (data.retrieval ? ` (≈ ${fmt(Math.max(0, saved))} fewer than the full package)` : '') +
        (data.attached_files.length ? ` · includes ${data.attached_files.length} attached file(s)` : '');
      $('prompt-result').hidden = false;
      $('copy-btn').disabled = false;
      toast('Prompt generated', 'success');
    } catch (err) {
      showError('prompt-error', err.message);
    } finally {
      setBusy(btn, false);
    }
  });

  $('copy-btn').addEventListener('click', async () => {
    if (!state.generatedPrompt) return;
    try {
      await navigator.clipboard.writeText(state.generatedPrompt);
      toast('Copied — paste it as your first message in the target chat', 'success');
    } catch {
      const box = $('prompt-output');
      box.focus();
      box.select();
      toast('Clipboard blocked by the browser — the text is selected, press Ctrl/Cmd+C', 'warning');
    }
  });
}

// -------------------------------------------------------------------
// Package tools: import file, merge, delete
// -------------------------------------------------------------------

function setupTools() {
  $('import-package-input').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    const status = $('tools-status');
    status.textContent = `Importing ${file.name}…`;
    const body = new FormData();
    body.append('file', file);
    try {
      const data = await api('/api/packages/import', { method: 'POST', body });
      status.textContent = `Imported ${data.package_name} v${data.version} (${data.total_items} items).`;
      await loadPackages();
      await selectPackage(data.package_name);
    } catch (err) {
      status.textContent = '';
      if (/already exists/i.test(err.message)) {
        const rename = window.prompt(`${err.message}\n\nEnter a new name for the imported package (or leave empty to cancel):`, '');
        if (rename) {
          const retry = new FormData();
          retry.append('file', file);
          retry.append('rename', rename);
          try {
            const data = await api('/api/packages/import', { method: 'POST', body: retry });
            status.textContent = `Imported as ${data.package_name}.`;
            await loadPackages();
            await selectPackage(data.package_name);
            return;
          } catch (err2) {
            toast(err2.message, 'error');
            return;
          }
        }
      }
      toast(err.message, 'error');
    }
  });

  $('merge-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    showError('merge-error');
    const dryRun = e.submitter ? e.submitter.dataset.dry === '1' : true;
    const names = Array.from($('merge-sources').selectedOptions).map((o) => o.value);
    const newName = $('merge-name').value.trim();
    if (names.length < 2) return showError('merge-error', 'Select at least two source packages.');
    if (!$('merge-name').checkValidity() || !newName) return showError('merge-error', 'Enter a valid name for the merged package.');
    const btn = e.submitter;
    setBusy(btn, true, dryRun ? 'Previewing…' : 'Merging…');
    try {
      const data = await api('/api/packages/merge', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ names, new_name: newName, dry_run: dryRun }),
      });
      renderMerge(data);
      if (!dryRun) {
        toast(`Merged into ${data.package_name}`, 'success');
        await loadPackages();
        await selectPackage(data.package_name);
      }
    } catch (err) {
      showError('merge-error', err.message);
    } finally {
      setBusy(btn, false);
    }
  });

  $('delete-package-btn').addEventListener('click', () => {
    if (!state.activePackage) return;
    $('delete-name').textContent = state.activePackage.name;
    $('delete-confirm').hidden = false;
    $('delete-yes').focus();
  });
  $('delete-no').addEventListener('click', () => { $('delete-confirm').hidden = true; });
  $('delete-yes').addEventListener('click', async () => {
    if (!state.activePackage) return;
    const name = state.activePackage.name;
    try {
      await api(`/api/packages/${encodeURIComponent(name)}`, { method: 'DELETE' });
      $('delete-confirm').hidden = true;
      toast(`Deleted ${name}`, 'success');
      clearActivePackage();
      await loadPackages();
    } catch (err) {
      toast(err.message, 'error');
    }
  });
}

function renderMerge(data) {
  const box = $('merge-result');
  const s = data.summary;
  box.replaceChildren(
    el('p', { class: 'summary-line' }, [
      el('strong', { text: data.dry_run ? 'Preview: ' : 'Saved: ' }),
      `${s.input_items} items from ${s.sources.join(' + ')} → ${s.kept_items} kept (${s.dropped_items} folded as duplicates) · ` +
      `${s.duplicate_groups} duplicate group(s) · ${s.conflicts} possible conflict(s)`,
    ]),
  );
  if (data.duplicates.length) {
    const list = el('ul', { class: 'merge-list' });
    for (const g of data.duplicates) {
      list.append(el('li', {}, [
        el('p', {}, [el('strong', { text: 'Kept: ' }), g.kept.content, el('span', { class: 'muted', text: ` (from ${g.origins.join(', ')}; similarity ≥ ${g.similarity.toFixed(2)})` })]),
        el('ul', { class: 'muted' }, g.duplicates.map((d) => el('li', { text: `folded: ${d.content}` }))),
      ]));
    }
    box.append(el('details', { open: true }, [el('summary', { text: `Duplicates folded (${data.duplicates.length})` }), list]));
  }
  if (data.conflicts.length) {
    const list = el('ul', { class: 'merge-list conflicts' });
    for (const c of data.conflicts) {
      list.append(el('li', {}, [
        el('p', {}, [categoryChip(c.category), ' ', el('strong', { text: c.reason }), el('span', { class: 'muted', text: ` (similarity ${c.similarity.toFixed(2)}; shared: ${c.shared_terms.join(', ')})` })]),
        el('p', { text: `A: ${c.a.content}` }), el('p', { class: 'muted small', text: `from ${c.a.origin}` }),
        el('p', { text: `B: ${c.b.content}` }), el('p', { class: 'muted small', text: `from ${c.b.origin}` }),
      ]));
    }
    box.append(el('details', { open: true }, [el('summary', { text: `Possible conflicts — both kept for you to resolve (${data.conflicts.length})` }), list]));
  }
  if (!data.duplicates.length && !data.conflicts.length) box.append(el('p', { class: 'help', text: 'No duplicates or conflicts detected.' }));
  box.hidden = false;
}

// -------------------------------------------------------------------
// Quality
// -------------------------------------------------------------------

function setupQuality() {
  $('eval-btn').addEventListener('click', async () => {
    showError('eval-error');
    const btn = $('eval-btn');
    setBusy(btn, true, 'Evaluating…');
    try {
      const data = await api('/api/eval');
      const labels = {
        extraction_coverage: 'Extraction coverage (offline rule-based extractor)',
        extraction_unmatched_ratio: 'Extracted items that match no gold item',
        retrieval_precision_at_k: 'Retrieval precision@3',
        retrieval_recall_at_k: 'Retrieval recall@3',
        retrieval_mrr: 'Retrieval mean reciprocal rank',
        duplicate_f1: 'Duplicate detection F1',
        redaction_recall: 'Redaction recall (planted secrets)',
        redaction_false_positives: 'Redaction false positives on clean text',
        token_savings_vs_transcript: 'Token savings vs. full transcript',
        token_savings_vs_full_memory: 'Token savings vs. full memory',
      };
      const tbody = $('eval-body');
      tbody.replaceChildren();
      for (const [key, label] of Object.entries(labels)) {
        const value = data.aggregate[key];
        const shown = key === 'redaction_false_positives' ? String(value) : `${(value * 100).toFixed(1)}%`;
        tbody.append(el('tr', {}, [el('td', { text: label }), el('td', { text: shown })]));
      }
      const notes = $('eval-notes');
      notes.replaceChildren(...data.notes.map((n) => el('li', { text: n })));
      $('eval-result').hidden = false;
    } catch (err) {
      showError('eval-error', err.message);
    } finally {
      setBusy(btn, false);
    }
  });
}
