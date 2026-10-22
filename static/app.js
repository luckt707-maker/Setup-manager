const API_BASE = window.location.origin;
const WS_BASE = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}`;

// The server hands the session token to the page in a meta tag. Every API
// call has to carry it, and the log WebSocket has to pass it as a query
// parameter, because the browser cannot set headers on a WebSocket handshake.
const TOKEN = (document.querySelector('meta[name="sm-token"]') || {}).content || '';
const AUTH_HEADERS = TOKEN ? { 'X-Setup-Token': TOKEN } : {};

// DOM Elements
const el = {
    pathInput: document.getElementById('folder-path'),
    btnScan: document.getElementById('btn-scan'),
    imagePath: document.getElementById('image-path'),
    btnMount: document.getElementById('btn-mount'),
    btnDismount: document.getElementById('btn-dismount'),
    scanFilePath: document.getElementById('scan-file-path'),
    btnFileScan: document.getElementById('btn-file-scan'),
    fileScanStatus: document.getElementById('file-scan-status'),
    dashboard: document.getElementById('dashboard'),
    envDescription: document.getElementById('env-description'),
    envBadge: document.getElementById('env-badge'),
    projectSelect: document.getElementById('project-select'),
    candidateSelect: document.getElementById('candidate-select'),
    btnRunApp: document.getElementById('btn-run-app'),
    btnDiagnose: document.getElementById('btn-diagnose'),
    diagnosisResults: document.getElementById('diagnosis-results'),
    diagnosisSummary: document.getElementById('diagnosis-summary'),
    diagnosisChecks: document.getElementById('diagnosis-checks'),
    runtimeDownloads: document.getElementById('runtime-downloads'),
    dependencyActions: document.getElementById('dependency-actions'),
    consoleSection: document.getElementById('console-section'),
    logWindow: document.getElementById('log-window'),
    resultsSection: document.getElementById('results-section'),
    resultsBody: document.getElementById('results-body'),
    btnClearLogs: document.getElementById('btn-clear-logs'),

    modalCustom: document.getElementById('modal-custom'),
    modalCmdPreview: document.getElementById('modal-cmd-preview'),
    btnModalCancel: document.getElementById('btn-modal-cancel'),
    btnModalConfirm: document.getElementById('btn-modal-confirm'),
    btnCustom: document.getElementById('btn-custom'),
    securitySection: document.getElementById('security-section'),
    securitySummary: document.getElementById('security-summary'),
    securityBody: document.getElementById('security-body'),

    aiSection: document.getElementById('ai-section'),
    aiModel: document.getElementById('ai-model'),
    aiStatusBadge: document.getElementById('ai-status-badge'),
    aiStatusMessage: document.getElementById('ai-status-message'),
    aiQuickPrompts: document.getElementById('ai-quick-prompts'),
    aiPrompt: document.getElementById('ai-prompt'),
    aiIncludeContext: document.getElementById('ai-include-context'),
    btnAiAsk: document.getElementById('btn-ai-ask'),
    aiAnswer: document.getElementById('ai-answer'),
    aiMeta: document.getElementById('ai-meta'),
};

let activeTaskId = null;
let ws = null;
let currentProjectPath = '';
let discoveredProjects = [];
let aiHistory = [];
let aiStatus = null;

// --- Utility ---

async function apiPost(endpoint, body = null, queryParams = {}) {
    const url = new URL(`${API_BASE}${endpoint}`);
    Object.keys(queryParams).forEach(key => url.searchParams.append(key, queryParams[key]));

    const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...AUTH_HEADERS },
        body: body ? JSON.stringify(body) : null
    });
    if (!response.ok) {
        let detail = '';
        try {
            const payload = await response.json();
            if (payload && payload.detail) {
                detail = typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail);
            }
        } catch (err) {
            detail = '';
        }
        throw new Error(detail || `API Error: ${response.statusText}`);
    }
    return response.json();
}

async function apiGet(endpoint) {
    const response = await fetch(`${API_BASE}${endpoint}`, { headers: { ...AUTH_HEADERS } });
    if (!response.ok) throw new Error(`API Error: ${response.statusText}`);
    return response.json();
}

function appendLog(text, type = 'info') {
    const span = document.createElement('div');
    const timestamp = new Date().toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

    let color = 'text-green-400';
    if (type === 'error') color = 'text-red-400';
    if (type === 'warn') color = 'text-yellow-400';
    if (type === 'system') color = 'text-gray-500';

    span.className = `${color} mb-1`;
    const time = document.createElement('span');
    time.className = 'text-gray-600';
    time.textContent = `[${timestamp}]`;
    span.append(time, document.createTextNode(` ${text}`));
    el.logWindow.appendChild(span);
    el.logWindow.scrollTop = el.logWindow.scrollHeight;
}

function reportMalwareScan(report) {
    if (!report) return;
    const status = report.status || 'unknown';
    const kind = status === 'threats' || status === 'error'
        ? 'error'
        : status === 'unavailable' ? 'warn' : 'info';
    appendLog(`Malware scan ${status}: ${report.message || 'No details provided.'}`, kind);
}

// --- Core Logic ---

async function scanFolder() {
    const path = el.pathInput.value.trim();
    if (!path) return alert('Please enter a folder path');

    currentProjectPath = path;
    el.btnScan.disabled = true;
    el.btnScan.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Scanning...';

    try {
        const data = await apiPost('/analyze', { path });

        // Update UI
        el.envDescription.innerText = data.detected.length > 0
            ? `Detected environments: ${data.detected.join(', ')}`
            : 'No supported project markers found.';
        el.envBadge.innerText = data.detected[0] || 'Unknown';
        el.dashboard.classList.remove('hidden');
        el.aiSection.classList.remove('hidden');
        discoveredProjects = data.projects || [];
        if (!discoveredProjects.length && data.detected.length) {
            discoveredProjects = [{
                path: data.folder || path,
                detected: data.detected,
                candidates: data.candidates || [],
            }];
        }
        el.projectSelect.replaceChildren();
        discoveredProjects.forEach((project, index) => {
            const option = document.createElement('option');
            option.value = String(index);
            option.textContent = `${project.path} (${project.detected.join(', ')})`;
            el.projectSelect.appendChild(option);
        });
        el.dashboard.classList.toggle('hidden', discoveredProjects.length === 0);
        updateSelectedProject();

    } catch (err) {
        alert(`Scan failed: ${err.message}`);
    } finally {
        el.btnScan.disabled = false;
        el.btnScan.innerHTML = '<i class="fas fa-search"></i> Scan Folder';
    }
}

function updateSelectedProject() {
    const project = discoveredProjects[Number(el.projectSelect.value)];
    if (!project) {
        currentProjectPath = '';
        el.candidateSelect.replaceChildren();
        el.btnRunApp.disabled = true;
        return;
    }
    currentProjectPath = project.path;
    el.candidateSelect.replaceChildren();
    (project.candidates || []).forEach((candidate, index) => {
        const option = document.createElement('option');
        option.value = String(index);
        option.textContent = candidate.label;
        el.candidateSelect.appendChild(option);
    });
    el.btnRunApp.disabled = !project.candidates || project.candidates.length === 0;
    el.diagnosisResults.classList.add('hidden');
}

async function diagnoseSelectedProject() {
    if (!currentProjectPath) return alert('Scan a folder and select a project first.');
    el.btnDiagnose.disabled = true;
    el.btnDiagnose.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Checking...';
    try {
        const data = await apiPost('/diagnose', { path: currentProjectPath });
        el.diagnosisResults.classList.remove('hidden');
        el.diagnosisChecks.replaceChildren();
        el.runtimeDownloads.replaceChildren();
        el.dependencyActions.replaceChildren();
        const missing = data.checks.filter(check => check.status === 'missing').length;
        el.diagnosisSummary.textContent = missing
            ? `${missing} missing prerequisite(s) found for ${data.folder}.`
            : `No missing prerequisites detected for ${data.folder}.`;

        data.checks.forEach(check => {
            const item = document.createElement('li');
            item.className = 'flex flex-wrap items-start gap-x-2';
            const status = document.createElement('span');
            status.className = check.status === 'ready'
                ? 'text-emerald-400 font-semibold'
                : check.status === 'missing' ? 'text-amber-300 font-semibold' : 'text-gray-400';
            status.textContent = check.status.toUpperCase();
            const detail = document.createElement('span');
            detail.className = 'text-gray-300';
            detail.textContent = `${check.name}: ${check.detail}`;
            item.append(status, detail);
            el.diagnosisChecks.appendChild(item);

            if (check.status === 'missing' && check.download_url) {
                const link = document.createElement('a');
                link.href = check.download_url;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                link.className = 'text-sm text-blue-300 underline';
                link.textContent = `Download ${check.name}`;
                el.runtimeDownloads.appendChild(link);
            }
        });

        data.install_commands.forEach(install => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'bg-amber-700 hover:bg-amber-600 text-white text-sm px-3 py-2 rounded-lg';
            button.textContent = install.label;
            button.addEventListener('click', async () => {
                const approved = confirm(
                    `${install.label} in:\n${currentProjectPath}\n\n` +
                    `Command: ${install.command}\n\nThis downloads and installs project dependencies. ` +
                    `Node lifecycle scripts are disabled and Python source distributions are blocked. Continue?`
                );
                if (approved) await startTask(`/task/install-dependencies/${install.type}`);
            });
            el.dependencyActions.appendChild(button);
        });
    } catch (err) {
        alert(`Dependency check failed: ${err.message}`);
    } finally {
        el.btnDiagnose.disabled = false;
        el.btnDiagnose.innerHTML = '<i class="fas fa-stethoscope"></i> Find Missing';
    }
}

async function runSelectedApp() {
    const candidateIndex = Number(el.candidateSelect.value);
    const candidate = discoveredProjects[Number(el.projectSelect.value)]?.candidates?.[candidateIndex];
    if (!candidate || !confirm(`Run ${candidate.label} in:\n${currentProjectPath}?`)) return;
    try {
        const task = await apiPost('/task/launch', {
            path: currentProjectPath,
            candidate_index: candidateIndex,
        });
        activeTaskId = task.task_id;
        setupTaskMonitoring(task.task_id);
        reportMalwareScan(task.malware_scan);
    } catch (err) {
        alert(`Could not launch app: ${err.message}`);
    }
}

async function setDiskImageMountState(action) {
    const imagePath = el.imagePath.value.trim();
    if (!imagePath) return alert('Enter the full path to an ISO, VHD, or VHDX image.');
    if (action === 'mount' && !confirm(`Mount disk image?\n${imagePath}\n\nWindows may request administrator access.`)) return;
    if (action === 'dismount' && !confirm(`Eject disk image?\n${imagePath}`)) return;
    const button = action === 'mount' ? el.btnMount : el.btnDismount;
    button.disabled = true;
    try {
        const result = await apiPost(`/${action === 'mount' ? 'mount' : 'dismount'}`, { image_path: imagePath });
        appendLog(result.message || `${action === 'mount' ? 'Mounted' : 'Ejected'} ${result.image_path}`, 'info');
        reportMalwareScan(result.malware_scan);
        if (result.message) alert(result.message);
    } catch (err) {
        appendLog(`Disk image ${action} failed: ${err.message}`, 'error');
        alert(`Disk image ${action} failed: ${err.message}`);
    } finally {
        button.disabled = false;
    }
}

async function scanDownloadedFile() {
    const path = el.scanFilePath.value.trim();
    if (!path) return alert('Enter the full path to the downloaded file.');
    el.btnFileScan.disabled = true;
    el.fileScanStatus.className = 'mt-2 text-sm text-gray-400';
    el.fileScanStatus.textContent = 'Scanning file...';
    try {
        const report = await apiPost('/file-safety-scan', { path });
        const status = report.status || 'unknown';
        el.fileScanStatus.className = `mt-2 text-sm ${status === 'clean' ? 'text-emerald-400' : status === 'threats' || status === 'error' ? 'text-red-400' : 'text-amber-300'}`;
        el.fileScanStatus.textContent = `Malware scan ${status.toUpperCase()}: ${report.message || 'No details provided.'}`;
        reportMalwareScan(report);
    } catch (err) {
        el.fileScanStatus.className = 'mt-2 text-sm text-red-400';
        el.fileScanStatus.textContent = `File scan failed: ${err.message}`;
    } finally {
        el.btnFileScan.disabled = false;
    }
}

async function startTask(endpoint, body = null, queryParams = {}) {
    try {
        // Combine query params with path
        const params = { ...queryParams, path: currentProjectPath };
        const data = await apiPost(endpoint, body, params);
        const taskId = data.task_id;

        activeTaskId = taskId;
        setupTaskMonitoring(taskId);
        reportMalwareScan(data.malware_scan);
    } catch (err) {
        alert(`Task failed to start: ${err.message}`);
    }
}

function setupTaskMonitoring(taskId) {
    // Show console
    el.consoleSection.classList.remove('hidden');
    el.logWindow.innerHTML = '';
    appendLog(`Task ${taskId} started. Connecting to logs...`, 'system');

    // WebSocket logs
    if (ws) ws.close();
    ws = new WebSocket(`${WS_BASE}/ws/logs/${taskId}?token=${encodeURIComponent(TOKEN)}`);

    ws.onmessage = (event) => {
        appendLog(event.data);
    };

    ws.onclose = () => {
        appendLog(`Log stream closed.`, 'system');
    };

    ws.onerror = (err) => {
        appendLog(`WebSocket Error: ${err.message}`, 'error');
    };

    // Poll for completion
    const pollInterval = setInterval(async () => {
        try {
            const status = await apiGet(`/task/${taskId}`);
            if (status.status === 'completed' || status.status === 'failed') {
                clearInterval(pollInterval);
                appendLog(`Task finished with status: ${status.status}`, status.status === 'completed' ? 'info' : 'error');

                // If it was a check task, show results
                if (status.results) {
                    renderResults(status.results);
                }

                activeTaskId = null;
            }
        } catch (err) {
            console.error('Polling error:', err);
        }
    }, 2000);
}

function renderResults(results) {
    el.resultsSection.classList.remove('hidden');
    el.resultsBody.innerHTML = '';

    // Convert results object to array if necessary
    const resultsArray = Array.isArray(results)
        ? results
        : Object.entries(results).map(([name, status]) => ({ name, status }));

    resultsArray.forEach(res => {
        const row = document.createElement('tr');

        let statusClass = 'text-gray-400';
        let statusIcon = 'fa-minus';

        const status = res.status ? res.status.toLowerCase() : 'unknown';
        if (status === 'pass') {
            statusClass = 'text-green-400 font-bold';
            statusIcon = 'fa-check-circle';
        } else if (status === 'fail') {
            statusClass = 'text-red-400 font-bold';
            statusIcon = 'fa-times-circle';
        } else if (status === 'skip') {
            statusClass = 'text-gray-500';
            statusIcon = 'fa-forward';
        }

        const nameCell = document.createElement('td');
        nameCell.className = 'px-6 py-4 text-sm';
        nameCell.textContent = res.tool || res.name || '';
        const statusCell = document.createElement('td');
        statusCell.className = 'px-6 py-4 text-sm';
        const statusLabel = document.createElement('span');
        statusLabel.className = `${statusClass} flex items-center gap-2`;
        const icon = document.createElement('i');
        icon.className = `fas ${statusIcon}`;
        statusLabel.append(icon, document.createTextNode(` ${status.toUpperCase()}`));
        statusCell.appendChild(statusLabel);
        const detailCell = document.createElement('td');
        detailCell.className = 'px-6 py-4 text-sm text-gray-400';
        detailCell.textContent = res.message || '-';
        row.append(nameCell, statusCell, detailCell);
        el.resultsBody.appendChild(row);
    });
}

async function runSecurityScan() {
    try {
        const data = await apiPost('/security-scan', { path: currentProjectPath });
        el.securitySection.classList.remove('hidden');
        const malware = data.malware_scan || { status: 'unavailable', message: 'No malware scan result.' };
        el.securitySummary.textContent = `${data.files_scanned} source files checked. ${data.findings.length} finding(s). ` +
            `Malware scan: ${malware.status.toUpperCase()} (${malware.message}). ` +
            `${data.truncated ? 'File limit reached; results are partial. ' : ''}${data.scope}`;
        el.securityBody.replaceChildren();
        data.findings.forEach(finding => {
            const row = document.createElement('tr');
            const values = [
                finding.severity,
                finding.rule,
                `${finding.file}:${finding.line}`,
                finding.message,
            ];
            values.forEach(value => {
                const cell = document.createElement('td');
                cell.className = 'px-4 py-3 text-sm align-top';
                cell.textContent = value;
                row.appendChild(cell);
            });
            el.securityBody.appendChild(row);
        });
        if (data.findings.length === 0) {
            const row = document.createElement('tr');
            const cell = document.createElement('td');
            cell.colSpan = 4;
            cell.className = 'px-4 py-3 text-sm text-gray-400';
            cell.textContent = 'No matching patterns found.';
            row.appendChild(cell);
            el.securityBody.appendChild(row);
        }
    } catch (err) {
        alert(`Security scan failed: ${err.message}`);
    }
}

// --- AI Assistant (Ollama / Qwen) ---

const AI_QUICK_LABELS = {
    explain: 'Explain project',
    issues: 'Likely issues',
    setup: 'Setup commands',
    security: 'Review security',
};

function setAiBadge(text, tone) {
    el.aiStatusBadge.textContent = text;
    const tones = {
        ready: 'bg-emerald-600 text-white',
        warn: 'bg-yellow-600 text-white',
        error: 'bg-red-600 text-white',
    };
    el.aiStatusBadge.className = `px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider ${tones[tone] || 'bg-gray-700 text-gray-300'}`;
}

function renderAiQuickPrompts(prompts) {
    el.aiQuickPrompts.replaceChildren();
    prompts.forEach(item => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'text-xs px-3 py-1 rounded-full border border-gray-600 text-gray-300 hover:border-purple-500 hover:text-purple-200 transition-colors';
        button.textContent = AI_QUICK_LABELS[item.key] || item.key;
        button.dataset.promptKey = item.key;
        el.aiQuickPrompts.appendChild(button);
    });
}

async function loadAiStatus() {
    try {
        const status = await apiGet('/ai/status');
        aiStatus = status;

        const allowedModels = new Set(status.allowed_models || [status.model].filter(Boolean));
        const models = (status.models || []).filter(name => allowedModels.has(name));
        el.aiModel.replaceChildren();
        (models.length ? models : [status.model].filter(name => allowedModels.has(name))).forEach(name => {
            const option = document.createElement('option');
            option.value = name;
            option.textContent = name;
            el.aiModel.appendChild(option);
        });
        if (status.model && allowedModels.has(status.model) && !models.includes(status.model)) {
            const option = document.createElement('option');
            option.value = status.model;
            option.textContent = `${status.model} (not installed)`;
            el.aiModel.appendChild(option);
        }
        el.aiModel.value = status.model || '';

        const ready = Boolean(status.available) && models.includes(status.model);
        if (ready) {
            setAiBadge('Ready', 'ready');
        } else if (status.available) {
            setAiBadge('No model', 'warn');
        } else {
            setAiBadge('Offline', 'error');
        }

        el.aiStatusMessage.textContent = status.message
            || `Ollama ${status.version || ''} at ${status.base_url} - ${models.length} model(s) available.`;

        renderAiQuickPrompts(status.quick_prompts || []);
        return status;
    } catch (err) {
        setAiBadge('Offline', 'error');
        el.aiStatusMessage.textContent = `Cannot reach the Setup Manager API: ${err.message}`;
        return null;
    }
}

async function askAi(promptText) {
    const message = (promptText === undefined ? el.aiPrompt.value : promptText).trim();
    if (!message) return alert('Enter a question for the assistant');

    const includeContext = el.aiIncludeContext.checked;
    if (includeContext && !currentProjectPath) {
        return alert('Scan a project folder first, or untick "Include project context".');
    }

    const model = el.aiModel.value || null;
    el.btnAiAsk.disabled = true;
    el.btnAiAsk.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Thinking...';
    el.aiAnswer.classList.remove('hidden');
    el.aiAnswer.textContent = `Waiting for ${model || 'the model'}...`;
    el.aiMeta.textContent = '';

    try {
        const data = await apiPost('/ai/chat', {
            message,
            path: includeContext ? currentProjectPath : null,
            model,
            include_context: includeContext,
            history: aiHistory,
        });

        el.aiAnswer.textContent = data.reply || '(the model returned an empty response)';
        const total = data.usage && data.usage.total_tokens ? ` | ${data.usage.total_tokens} tokens` : '';
        el.aiMeta.textContent = `${data.model} | ${(data.elapsed_ms / 1000).toFixed(1)}s | ${data.context_chars} context chars${total}`;
        appendLog(`AI (${data.model}) answered in ${(data.elapsed_ms / 1000).toFixed(1)}s`, 'info');

        aiHistory.push({ role: 'user', content: message });
        aiHistory.push({ role: 'assistant', content: data.reply });
        if (aiHistory.length > 6) aiHistory = aiHistory.slice(-6);
    } catch (err) {
        el.aiAnswer.textContent = `Assistant error: ${err.message}`;
        appendLog(`AI error: ${err.message}`, 'error');
    } finally {
        el.btnAiAsk.disabled = false;
        el.btnAiAsk.innerHTML = '<i class="fas fa-paper-plane"></i> Ask Qwen';
    }
}

async function showAiAssistant() {
    el.aiSection.classList.remove('hidden');
    el.aiSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    el.aiPrompt.focus();
    if (!aiStatus) await loadAiStatus();
}

// --- Event Listeners ---

el.btnScan.addEventListener('click', scanFolder);
el.projectSelect.addEventListener('change', updateSelectedProject);
el.btnDiagnose.addEventListener('click', diagnoseSelectedProject);
el.btnRunApp.addEventListener('click', runSelectedApp);
el.btnMount.addEventListener('click', () => setDiskImageMountState('mount'));
el.btnDismount.addEventListener('click', () => setDiskImageMountState('dismount'));
el.btnFileScan.addEventListener('click', scanDownloadedFile);

el.btnClearLogs.addEventListener('click', () => {
    el.logWindow.innerHTML = '<span class="text-gray-600">Logs cleared.</span>';
});

document.querySelectorAll('.action-btn[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
        const action = btn.dataset.action;

        if (action === 'setup-python') startTask('/task/setup/python');
        if (action === 'setup-node') startTask('/task/setup/nodejs');
        if (action === 'setup-dotnet') startTask('/task/setup/dotnet');
        if (action === 'setup-git') startTask('/task/setup/git');
        if (action === 'setup-make') startTask('/task/setup/make');
        if (action === 'checks') startTask('/task/checks', null, { project_type: 'auto' });
        if (action === 'security-scan') runSecurityScan();
        if (action === 'ai-assistant') showAiAssistant();
        if (action === 'extensions') {
            startTask('/task/extensions', {
                project_type: 'auto',
                ext_types: ['linters', 'type_checkers', 'testers', 'security']
            });
        }
    });
});

// Custom Command Logic
el.btnCustom.addEventListener('click', () => {
    const cmd = prompt('Enter custom shell command:', 'npm install');
    if (!cmd) return;

    el.modalCmdPreview.innerText = cmd;
    el.modalCustom.classList.remove('hidden');

    el.btnModalConfirm.onclick = async () => {
        el.modalCustom.classList.add('hidden');
        try {
            await startTask('/task/custom', {
                command: cmd,
                cwd: currentProjectPath,
                confirm: true
            });
        } catch (err) {
            alert(`Custom command failed: ${err.message}`);
        }
    };
});

el.btnModalCancel.addEventListener('click', () => {
    el.modalCustom.classList.add('hidden');
});

// --- AI Assistant wiring ---

el.btnAiAsk.addEventListener('click', () => askAi());

el.aiQuickPrompts.addEventListener('click', (event) => {
    const button = event.target.closest('[data-prompt-key]');
    if (!button) return;
    askAi(button.dataset.promptKey);
});

el.aiModel.addEventListener('change', () => {
    aiHistory = [];
});

// Prime the assistant status once the page is ready
loadAiStatus();
