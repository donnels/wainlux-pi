/**
 * Shared API utilities for K6 laser control
 */

/**
 * Make API call with timeout and proper headers
 * @param {string} endpoint - API endpoint path
 * @param {string} method - HTTP method (default 'POST')
 * @param {Object|FormData} body - Request body
 * @returns {Promise<Object>} Response data
 * @throws {Error} On request failure
 */
async function apiCall(endpoint, method = 'POST', body = null) {
    const controller = new AbortController();
    const endpointTimeouts = {
        '/api/status': 5000,
        '/api/connect': 8000,
        '/api/engrave': 1800000,
        '/api/job/burn': 1800000,
        '/api/calibration/burn': 1800000,
    };
    const timeoutMs = endpointTimeouts[endpoint] || 30000;
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    
    try {
        const requestSettings = getRequestSettings();
        const opts = {
            method,
            signal: controller.signal,
            headers: {
                'X-K6-Operation-Mode': requestSettings.operation_mode,
                'X-K6-Dry-Run': requestSettings.dry_run ? 'true' : 'false',
            }
        };
        
        if (body instanceof FormData) {
            // FormData: let browser set Content-Type with boundary
            opts.body = body;
        } else if (body) {
            // JSON: set Content-Type explicitly
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        
        const res = await fetch(endpoint, opts);
        const contentType = res.headers.get('content-type') || '';
        let data = {};
        if (contentType.includes('application/json')) {
            data = await res.json();
        } else {
            const text = await res.text();
            data = { error: text || `HTTP ${res.status}` };
        }
        
        // Check HTTP status first
        if (!res.ok) {
            const err = new Error(data.error || `HTTP ${res.status}: ${res.statusText}`);
            err.status = res.status;
            err.data = data;
            throw err;
        }
        
        // Check success field only if present (some endpoints like /api/status don't use it)
        if (data.hasOwnProperty('success') && !data.success) {
            const err = new Error(data.error || 'Request failed');
            err.status = res.status;
            err.data = data;
            throw err;
        }
        
        return data;
    } catch (err) {
        const logPayload = {
            endpoint,
            method,
            error: err.message,
        };
        if (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') {
            logPayload.stack = err.stack;
        }
        console.error('API call failed:', logPayload);
        if (err.name === 'AbortError') {
            const timeoutSec = timeoutMs / 1000;
            throw new Error(`Request timeout (${timeoutSec}s)`);
        }
        throw err;
    } finally {
        clearTimeout(timeout);
    }
}

const MATERIAL_CONFIG_KEY = 'materialConfig';
const DEFAULT_IMAGE_URL = '/api/images/serve/default-image.png';
const DEFAULT_IMAGE_NAME = 'default-image.png';
const REQUEST_SETTINGS_KEY = 'k6RequestSettings';
const DEFAULT_REQUEST_SETTINGS = {
    operation_mode: 'silent',
    dry_run: false,
};

function formatLogTimestamp(dateInput = new Date()) {
    const d = dateInput instanceof Date ? dateInput : new Date(dateInput);
    if (Number.isNaN(d.getTime())) {
        return '0000-00-00 00:00:00.000';
    }
    const pad2 = (n) => String(n).padStart(2, '0');
    const pad3 = (n) => String(n).padStart(3, '0');
    return (
        `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
        `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}.` +
        `${pad3(d.getMilliseconds())}`
    );
}

function getRequestSettings() {
    const raw = sessionStorage.getItem(REQUEST_SETTINGS_KEY);
    if (!raw) return { ...DEFAULT_REQUEST_SETTINGS };
    try {
        const parsed = JSON.parse(raw);
        const operation_mode = ['silent', 'verbose', 'single-step'].includes(parsed.operation_mode)
            ? parsed.operation_mode
            : DEFAULT_REQUEST_SETTINGS.operation_mode;
        const dry_run = Boolean(parsed.dry_run);
        return { operation_mode, dry_run };
    } catch (_err) {
        sessionStorage.removeItem(REQUEST_SETTINGS_KEY);
        return { ...DEFAULT_REQUEST_SETTINGS };
    }
}

function setRequestSettings(settings) {
    if (!settings || typeof settings !== 'object') {
        throw new Error('Invalid request settings');
    }
    const merged = {
        ...DEFAULT_REQUEST_SETTINGS,
        ...settings,
    };
    if (!['silent', 'verbose', 'single-step'].includes(merged.operation_mode)) {
        throw new Error(`Invalid operation_mode: ${merged.operation_mode}`);
    }
    merged.dry_run = Boolean(merged.dry_run);
    sessionStorage.setItem(REQUEST_SETTINGS_KEY, JSON.stringify(merged));
    return merged;
}

function getMaterialConfig() {
    const raw = sessionStorage.getItem(MATERIAL_CONFIG_KEY);
    if (!raw) return null;
    try {
        return JSON.parse(raw);
    } catch (err) {
        console.warn('Invalid materialConfig in sessionStorage; clearing');
        sessionStorage.removeItem(MATERIAL_CONFIG_KEY);
        return null;
    }
}

function saveMaterialConfig(nextConfig) {
    if (!nextConfig || typeof nextConfig !== 'object') {
        throw new Error('Invalid material config');
    }
    const existing = getMaterialConfig() || {};
    const expectedRevision = Number(nextConfig?._meta?.revision ?? existing?._meta?.revision ?? 0);
    const currentRevision = Number(existing?._meta?.revision || 0);

    if (expectedRevision && expectedRevision !== currentRevision) {
        throw new Error(
            `materialConfig conflict: expected revision ${expectedRevision}, current revision ${currentRevision}`
        );
    }

    const isObject = (value) => value && typeof value === 'object' && !Array.isArray(value);
    const deepMerge = (base, patch) => {
        if (!isObject(base) || !isObject(patch)) return patch;
        const out = { ...base };
        for (const [key, value] of Object.entries(patch)) {
            if (isObject(value) && isObject(base[key])) {
                out[key] = deepMerge(base[key], value);
            } else {
                out[key] = value;
            }
        }
        return out;
    };

    const merged = {
        ...deepMerge(existing, nextConfig),
        stages: deepMerge(existing.stages || {}, (nextConfig && nextConfig.stages) || {}),
        material: deepMerge(existing.material || {}, (nextConfig && nextConfig.material) || {}),
        layout: deepMerge(existing.layout || {}, (nextConfig && nextConfig.layout) || {}),
    };

    const prevRevision = currentRevision;
    merged._meta = {
        revision: prevRevision + 1,
        updated_at: new Date().toISOString(),
        updated_by: window.location.pathname,
    };

    const encoded = JSON.stringify(merged);
    if (encoded.length > 4_500_000) {
        throw new Error('materialConfig too large for sessionStorage');
    }
    try {
        sessionStorage.setItem(MATERIAL_CONFIG_KEY, encoded);
    } catch (err) {
        if (err && err.name === 'QuotaExceededError') {
            throw new Error('Browser storage full. Clear session data or reduce image size.');
        }
        throw err;
    }
    return merged;
}

function buildUploadStageData(prepared) {
    if (!prepared || !prepared.image_base64) {
        throw new Error('Invalid prepared image payload');
    }
    return {
        status: 'complete',
        data: {
            image_base64: prepared.image_base64,
            original_filename: prepared.original_filename || DEFAULT_IMAGE_NAME,
            source: prepared.source || 'upload',
            source_reference: prepared.source_reference || prepared.original_filename || '',
            width: prepared.width,
            height: prepared.height,
            original_width: prepared.original_width ?? prepared.width,
            original_height: prepared.original_height ?? prepared.height,
            resized: Boolean(prepared.resized),
            max_dimension: prepared.max_dimension,
            file_size_bytes: prepared.file_size_bytes,
            mime_type: prepared.mime_type,
        }
    };
}

async function ingestImageBlob(blob, filename = 'upload.png', options = {}) {
    const source = options.source || 'upload';
    const sourceReference = options.sourceReference || filename;
    const formData = new FormData();
    formData.append('image', blob, filename);
    formData.append('source', source);
    formData.append('source_reference', sourceReference);
    return apiCall('/api/engrave/prepare', 'POST', formData);
}

async function ingestImageUrl(url, filename = DEFAULT_IMAGE_NAME, options = {}) {
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`Failed to fetch image (${response.status})`);
    }
    const blob = await response.blob();
    return ingestImageBlob(blob, filename, {
        source: options.source || 'selection',
        sourceReference: options.sourceReference || url,
    });
}

async function ingestDefaultImage(options = {}) {
    return ingestImageUrl(DEFAULT_IMAGE_URL, DEFAULT_IMAGE_NAME, {
        source: options.source || 'default',
        sourceReference: options.sourceReference || DEFAULT_IMAGE_URL,
    });
}

function attachUploadStageToJob(job, preparedImage) {
    if (!job || typeof job !== 'object') {
        throw new Error('Invalid job');
    }
    job.stages = job.stages || {};
    job.stages.upload = buildUploadStageData(preparedImage);
    return job;
}

async function ensureJobHasUploadImage(job, options = {}) {
    if (!job || typeof job !== 'object') {
        throw new Error('Invalid job');
    }
    const existing = job?.stages?.upload?.data;
    if (existing && existing.image_base64) {
        return { job, addedDefault: false, uploadData: existing };
    }
    const prepared = await ingestDefaultImage(options);
    attachUploadStageToJob(job, prepared);
    return { job, addedDefault: true, uploadData: job.stages.upload.data };
}

/**
 * Update status bar from API data
 * @param {Object} data - Status data from /api/status
 */
function updateStatusBar(data) {
    const modeDisplay = document.getElementById('modeDisplay');
    const firmwareDisplay = document.getElementById('firmwareDisplay');
    const verifyDisplay = document.getElementById('verifyDisplay');
    
    if (!modeDisplay || !firmwareDisplay || !verifyDisplay) return;
    
    // Update mode display
    const modeLabel = data.mock_mode ? 'MOCK' : 'LIVE';
    const dryLabel = data.dry_run ? ' | DRY-RUN' : '';
    const opModeLabel = (data.operation_mode || 'silent').toUpperCase();
    modeDisplay.textContent = `Mode: ${modeLabel}${dryLabel} | ${opModeLabel}`;
    
    // Update firmware display
    if (data.version) {
        firmwareDisplay.textContent = `Firmware: ${data.version}`;
        firmwareDisplay.style.color = '#fff';
    } else {
        firmwareDisplay.textContent = 'Firmware: —';
        firmwareDisplay.style.color = '#888';
    }
    
    // Update verification status (only show when verified)
    if (data.connected && data.version) {
        verifyDisplay.textContent = '✓ Verified';
        verifyDisplay.style.display = 'inline';
        verifyDisplay.style.color = '#0f0';
    } else {
        verifyDisplay.style.display = 'none';
    }
}

/**
 * Refresh status bar from API
 */
async function refreshStatus() {
    try {
        const data = await apiCall('/api/status', 'GET');
        updateStatusBar(data);
        return data;
    } catch (err) {
        console.error('Status fetch failed:', err);
        throw err;
    }
}

async function verifyConnectionAndRefreshStatus() {
    const data = await apiCall('/api/connect');
    await refreshStatus();
    return data;
}
