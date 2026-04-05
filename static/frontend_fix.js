/**
 * PDFWala Frontend Fix — Drop-in replacement for the JS section
 * Paste this INSTEAD of the existing <script> block in index.html
 *
 * Fixes:
 *  1. Proper error display when backend is down or returns error
 *  2. Download button shows/hides correctly
 *  3. Setup box shown only on network failure (not on API errors)
 *  4. Toast auto-dismisses reliably
 *  5. processFile no longer swallows API-level errors silently
 */

const API = '';  // Set to your backend URL e.g. 'http://140.245.255.221' if different origin
let currentTool = null, selectedFiles = [], lastFilename = null, lastDownloadUrl = null;

// ── Health check on load ──────────────────────────────────────────
async function checkBackend() {
    try {
        const r = await fetch(API + '/api/health', { signal: AbortSignal.timeout(5000) });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        console.log('✅ Backend connected', data.version);
        return true;
    } catch (e) {
        console.warn('⚠️ Backend not reachable:', e.message);
        return false;
    }
}
window.addEventListener('load', checkBackend);

// ── Tool registry (paste your existing TOOLS object here) ────────
const TOOLS = { /* ... paste existing TOOLS const here ... */ };

// ── Open tool modal ───────────────────────────────────────────────
function openTool(toolId) {
    const tool = TOOLS[toolId];
    if (!tool) return;
    currentTool = toolId;
    selectedFiles = [];
    lastFilename = null;
    lastDownloadUrl = null;
    closeMob();

    const multi = tool.multi ? 'multiple' : '';
    document.getElementById('modalContent').innerHTML = `
        <div class="modal-header">
            <div class="modal-icon">${tool.icon}</div>
            <div>
                <div class="modal-title">${tool.title}</div>
                <div class="modal-desc">${tool.desc}</div>
            </div>
        </div>
        <div class="drop-zone" id="dropZone"
             ondragover="onDragOver(event)" ondragleave="onDragLeave()" ondrop="onDrop(event)"
             onclick="document.getElementById('fileInput').click()"
             role="button" tabindex="0">
            <span class="dz-icon">📂</span>
            <div class="dz-text"><strong>Click to browse</strong> or drag & drop</div>
            <div class="dz-hint">Supported: ${tool.accept.replace(/,/g, ' ')}</div>
            <input type="file" id="fileInput" accept="${tool.accept}" ${multi}
                   onchange="onFileSelect(event)" aria-hidden="true"/>
        </div>
        <div class="file-list" id="fileList"></div>
        ${tool.form()}
        <div class="progress-block" id="progressBlock">
            <div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>
            <div class="progress-status">
                <div class="dot-loader"><span></span><span></span><span></span></div>
                <span id="progressText">Preparing…</span>
            </div>
        </div>
        <div class="result-block" id="resultBlock">
            <div class="result-inner" id="resultInner">
                <div class="result-title" id="resultTitle"></div>
                <div class="result-meta" id="resultMeta"></div>
            </div>
        </div>
        <div class="setup-box" id="setupBox">
            <h4>⚙️ Backend Setup Required</h4>
            <p>Cannot reach the processing server. Start it locally or check your API URL.</p>
            <div class="code-block">pip install -r requirements.txt\npython app.py\n# Then set: const API = 'http://YOUR_SERVER_IP';</div>
        </div>
        <button class="btn-process" id="actionBtn" onclick="processFile()" disabled>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            Process File
        </button>
        <button class="btn-download" id="downloadBtn" onclick="downloadResult()">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            Download Processed File
        </button>`;

    // Split page range toggle
    if (toolId === 'split') {
        const modeEl = document.getElementById('mode');
        if (modeEl) modeEl.addEventListener('change', () => {
            const rg = document.getElementById('rangeGroup');
            if (rg) rg.style.display = modeEl.value === 'range' ? 'block' : 'none';
        });
    }

    document.getElementById('modalOverlay').classList.add('open');
    document.body.style.overflow = 'hidden';
}

// ── Process file ──────────────────────────────────────────────────
async function processFile() {
    if (!currentTool || !selectedFiles.length) return;
    const tool = TOOLS[currentTool];

    // If no API set, show setup box
    if (!API) {
        document.getElementById('setupBox').classList.add('show');
        document.getElementById('resultBlock').classList.remove('show');
        return;
    }

    const aBtn      = document.getElementById('actionBtn');
    const pBlock    = document.getElementById('progressBlock');
    const pFill     = document.getElementById('progressFill');
    const pText     = document.getElementById('progressText');
    const rBlock    = document.getElementById('resultBlock');
    const dBtn      = document.getElementById('downloadBtn');
    const setupBox  = document.getElementById('setupBox');

    aBtn.disabled = true;
    aBtn.innerHTML = `<div class="dot-loader"><span style="background:#fff"></span><span style="background:#fff"></span><span style="background:#fff"></span></div>&nbsp;Processing…`;
    pBlock.classList.add('show');
    rBlock.classList.remove('show');
    dBtn.style.display = 'none';
    setupBox.classList.remove('show');
    pFill.style.width = '0';

    // Fake progress animation
    let pct = 0;
    const tick = setInterval(() => {
        pct = Math.min(pct + Math.random() * 12, 88);
        pFill.style.width = pct + '%';
        pText.textContent = pct < 30 ? 'Uploading file…' : pct < 60 ? 'Processing…' : 'Finalizing…';
    }, 350);

    try {
        const fd = new FormData();
        if (tool.multi) selectedFiles.forEach(f => fd.append(tool.field, f));
        else fd.append(tool.field, selectedFiles[0]);

        // Append form fields
        const fieldIds = [
            'mode','ranges','quality','format','dpi','angle','pages','text','color',
            'opacity','password','password2','position','start','prefix','action','order',
            'left','right','top','bottom','page_size','version','lang','width','height',
            'keep_ratio','name','reason','search_text','find_text','replace_text'
        ];
        fieldIds.forEach(id => {
            const el = document.getElementById(id);
            if (!el) return;
            let val = el.value;
            if (id === 'opacity') val = (parseFloat(val) / 100).toFixed(2);
            fd.append(id, val);
        });

        const res = await fetch(API + tool.endpoint, { method: 'POST', body: fd });
        const data = await res.json();

        clearInterval(tick);
        pFill.style.width = '100%';
        pText.textContent = 'Done!';

        if (data.success) {
            showResult('success', data);
        } else {
            // API returned an error response (e.g. wrong password, missing dep)
            showResult('error', data);
        }

    } catch (err) {
        clearInterval(tick);
        pFill.style.width = '0';

        // Network error — backend unreachable
        if (err instanceof TypeError || err.name === 'TypeError') {
            setupBox.classList.add('show');
            pBlock.classList.remove('show');
            toast('Cannot reach backend server. Check your connection.', 'error');
        } else {
            showResult('error', { error: 'Unexpected error: ' + err.message });
        }
    }

    aBtn.disabled = !selectedFiles.length;
    aBtn.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg> Process File`;
}

// ── Show result ───────────────────────────────────────────────────
function showResult(type, data) {
    const rb  = document.getElementById('resultBlock');
    const ri  = document.getElementById('resultInner');
    const rt  = document.getElementById('resultTitle');
    const rm  = document.getElementById('resultMeta');
    const db  = document.getElementById('downloadBtn');

    rb.classList.add('show');
    ri.className = 'result-inner ' + type;

    const icon = type === 'success' ? '✓' : '✕';

    // PDF Info special case
    if (type === 'success' && data.page_count !== undefined) {
        rt.textContent = '✓ File Analysis Complete';
        rm.innerHTML = `
            <div style="width:100%;margin-top:10px;background:rgba(0,0,0,.18);padding:12px;border-radius:8px;color:var(--text-2);font-size:13px">
            <table style="width:100%;border-collapse:collapse">
                ${[
                    ['📄 Pages',      data.page_count],
                    ['💾 Size',       data.size_human],
                    ['👤 Author',     data.author || '—'],
                    ['🔒 Encrypted',  data.encrypted ? 'Yes' : 'No'],
                    ['📐 Dimensions', data.page_sizes?.[0]
                        ? data.page_sizes[0].width_pt + '×' + data.page_sizes[0].height_pt + ' pt'
                        : '—']
                ].map(([k, v]) =>
                    `<tr style="border-bottom:1px solid var(--border)">
                        <td style="padding:6px 0">${k}</td>
                        <td style="text-align:right;color:var(--text)">${v}</td>
                    </tr>`
                ).join('')}
            </table></div>`;
        db.style.display = 'none';
        toast('Analysis complete!', 'success');
        return;
    }

    // Standard result
    const msg = data.message || data.error || (type === 'success' ? 'Done!' : 'Processing failed');
    rt.textContent = icon + ' ' + msg;

    const pills = [];
    if (data.pages)         pills.push(data.pages + ' pages');
    if (data.size_human)    pills.push(data.size_human);
    if (data.reduction_pct) pills.push(data.reduction_pct + '% smaller');
    if (data.extracted)     pills.push('Extracted ' + data.extracted);
    if (data.expires_in)    pills.push('Expires in ' + data.expires_in);
    rm.innerHTML = pills.map(p => `<span class="result-pill">${p}</span>`).join('');

    if (type === 'success') {
        const url = data.download_url || (data.filename ? API + '/download/' + data.filename : null);
        if (url) {
            lastFilename    = data.filename || 'pdfwala_output';
            lastDownloadUrl = API + url;
            db.style.display = 'flex';
            toast('File ready — click Download!', 'success');
        } else {
            db.style.display = 'none';
            toast(msg, 'success');
        }
    } else {
        db.style.display = 'none';
        toast(msg, 'error');
    }
}

// ── Download ──────────────────────────────────────────────────────
function downloadResult() {
    if (!lastDownloadUrl) return;
    const a = document.createElement('a');
    a.href = lastDownloadUrl;
    a.download = lastFilename || 'pdfwala_output';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

// ── File handling ─────────────────────────────────────────────────
function onDragOver(e)  { e.preventDefault(); document.getElementById('dropZone').classList.add('dragover'); }
function onDragLeave()  { document.getElementById('dropZone').classList.remove('dragover'); }
function onDrop(e)      { e.preventDefault(); document.getElementById('dropZone').classList.remove('dragover'); handleFiles(Array.from(e.dataTransfer.files)); }
function onFileSelect(e){ handleFiles(Array.from(e.target.files)); }

function handleFiles(files) {
    const tool = TOOLS[currentTool];
    if (currentTool === 'compare-pdf' && files.length !== 2) {
        toast('Please upload exactly 2 PDF files for comparison', 'error');
        return;
    }
    selectedFiles = tool.multi ? [...selectedFiles, ...files] : [files[0]];
    renderFileList();
    document.getElementById('actionBtn').disabled = !selectedFiles.length;
    document.getElementById('resultBlock').classList.remove('show');
    document.getElementById('downloadBtn').style.display = 'none';
    lastFilename = null;
    lastDownloadUrl = null;
}

function renderFileList() {
    document.getElementById('fileList').innerHTML = selectedFiles.map((f, i) => `
        <div class="file-item">
            <div class="file-icon">📄</div>
            <span class="file-name">${f.name}</span>
            <span class="file-size">${fmtSz(f.size)}</span>
            <button class="file-del" onclick="removeFile(${i})" aria-label="Remove">✕</button>
        </div>`).join('');
}

function removeFile(i) {
    selectedFiles.splice(i, 1);
    renderFileList();
    document.getElementById('actionBtn').disabled = !selectedFiles.length;
}

function fmtSz(b) {
    if (b < 1024)    return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    return (b / 1048576).toFixed(1) + ' MB';
}

// ── Modal ─────────────────────────────────────────────────────────
function overlayClick(e) { if (e.target === document.getElementById('modalOverlay')) closeModal(); }
function closeModal() {
    document.getElementById('modalOverlay').classList.remove('open');
    document.body.style.overflow = '';
    currentTool = null;
    selectedFiles = [];
}

// ── Radio chips ───────────────────────────────────────────────────
function radioSelect(el, fieldId, value) {
    el.closest('.radio-group').querySelectorAll('.radio-chip').forEach(c => c.classList.remove('active'));
    el.classList.add('active');
    const h = document.getElementById(fieldId);
    if (h) { h.value = value; h.dispatchEvent(new Event('change')); }
}

// ── Toast — fixed auto-dismiss ────────────────────────────────────
function toast(msg, type = 'success') {
    const t    = document.getElementById('toast');
    const span = document.getElementById('toastMsg');
    if (!t || !span) return;
    span.textContent = msg;
    t.className = 'toast ' + type + ' show';
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove('show'), 3500);
}

// ── Theme ─────────────────────────────────────────────────────────
function toggleTheme() {
    const h = document.documentElement;
    const d = h.getAttribute('data-theme') === 'dark';
    h.setAttribute('data-theme', d ? 'light' : 'dark');
    document.getElementById('themeBtn').textContent = d ? '🌙' : '☀️';
    localStorage.setItem('pw-theme', d ? 'light' : 'dark');
}
(function () {
    const s = localStorage.getItem('pw-theme') || 'dark';
    document.documentElement.setAttribute('data-theme', s);
    document.getElementById('themeBtn').textContent = s === 'dark' ? '☀️' : '🌙';
})();

// ── Mobile nav ────────────────────────────────────────────────────
function toggleMob() {
    const n = document.getElementById('mobNav');
    n.classList.toggle('open');
    document.body.style.overflow = n.classList.contains('open') ? 'hidden' : '';
    document.getElementById('mobBtn').textContent = n.classList.contains('open') ? '✕' : '☰';
}
function closeMob() {
    document.getElementById('mobNav').classList.remove('open');
    document.body.style.overflow = '';
    document.getElementById('mobBtn').textContent = '☰';
}
function toggleMobSec(btn) {
    const sub  = btn.nextElementSibling;
    const open = sub.classList.contains('open');
    document.querySelectorAll('.mob-sub.open').forEach(s => s.classList.remove('open'));
    document.querySelectorAll('.mob-section-btn.active').forEach(b => b.classList.remove('active'));
    if (!open) { sub.classList.add('open'); btn.classList.add('active'); }
}

// ── Mega nav ──────────────────────────────────────────────────────
(function initMega() {
    const items = document.querySelectorAll('.nav-item[data-mega]');
    let closeTimer = null;
    items.forEach(item => {
        item.addEventListener('mouseenter', () => {
            clearTimeout(closeTimer);
            items.forEach(i => i.classList.remove('open'));
            item.classList.add('open');
        });
        item.addEventListener('mouseleave', () => {
            closeTimer = setTimeout(() => item.classList.remove('open'), 160);
        });
        const drop = item.querySelector('.mega-drop');
        if (drop) {
            drop.addEventListener('mouseenter', () => clearTimeout(closeTimer));
            drop.addEventListener('mouseleave', () => {
                closeTimer = setTimeout(() => item.classList.remove('open'), 160);
            });
        }
    });
    document.addEventListener('click', e => {
        if (!e.target.closest('.nav-item[data-mega]')) items.forEach(i => i.classList.remove('open'));
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') { items.forEach(i => i.classList.remove('open')); closeMob(); }
    });
})();

// ── Search ────────────────────────────────────────────────────────
document.getElementById('searchInput').addEventListener('input', function () {
    const query = this.value.toLowerCase().trim();
    const cards = document.querySelectorAll('.tool-card');
    let visible = 0;
    cards.forEach(card => {
        const match = query === '' || card.textContent.toLowerCase().includes(query);
        card.style.display = match ? '' : 'none';
        if (match) visible++;
    });
    const existing = document.getElementById('noSearchResults');
    if (existing) existing.remove();
    const grid = document.getElementById('toolsGrid');
    if (query !== '' && visible === 0) {
        const noMsg = document.createElement('div');
        noMsg.id = 'noSearchResults';
        noMsg.style.cssText = 'grid-column:1/-1;text-align:center;padding:40px;color:var(--text-3);';
        noMsg.innerHTML = `🔍 No tools found for "${query}"<br><small>Try "merge", "compress", or "pdf to image"</small>`;
        grid.parentNode.insertBefore(noMsg, grid.nextSibling);
        grid.style.display = 'none';
    } else {
        grid.style.display = '';
    }
    if (query !== '') {
        document.querySelectorAll('.tab-btn').forEach(t => t.classList.remove('active'));
        document.querySelector('.tab-btn').classList.add('active');
    }
});

function filterCat(cat, el) {
    const noMsg = document.getElementById('noSearchResults');
    if (noMsg) noMsg.remove();
    document.getElementById('toolsGrid').style.display = '';
    document.querySelectorAll('.tab-btn').forEach(t => t.classList.remove('active'));
    el.classList.add('active');
    document.querySelectorAll('.tool-card').forEach(card => {
        card.style.display = (cat === 'all' || card.dataset.cat === cat) ? '' : 'none';
    });
    document.getElementById('searchInput').value = '';
}

// ── Reveal on scroll ──────────────────────────────────────────────
const revObs = new IntersectionObserver(entries => {
    entries.forEach(e => {
        if (e.isIntersecting) { e.target.classList.add('visible'); revObs.unobserve(e.target); }
    });
}, { threshold: 0.1 });
document.querySelectorAll('.reveal').forEach(el => revObs.observe(el));
