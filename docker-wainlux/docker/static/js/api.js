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
    // Default 30s, but 30min for /api/engrave (large burns can take 15-20min)
    const timeoutMs = endpoint === '/api/engrave' ? 1800000 : 30000;
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    
    try {
        const opts = {
            method,
            signal: controller.signal
        };
        
        if (body instanceof FormData) {
            // FormData: let browser set Content-Type with boundary
            opts.body = body;
        } else if (body) {
            // JSON: set Content-Type explicitly
            opts.headers = { 'Content-Type': 'application/json' };
            opts.body = JSON.stringify(body);
        }
        
        const res = await fetch(endpoint, opts);
        const data = await res.json();
        
        // Check HTTP status first
        if (!res.ok) {
            throw new Error(data.error || `HTTP ${res.status}: ${res.statusText}`);
        }
        
        // Check success field only if present (some endpoints like /api/status don't use it)
        if (data.hasOwnProperty('success') && !data.success) {
            throw new Error(data.error || 'Request failed');
        }
        
        return data;
    } catch (err) {
        console.error('API call failed:', {
            endpoint,
            method,
            error: err.message,
            stack: err.stack
        });
        if (err.name === 'AbortError') {
            const timeoutSec = timeoutMs / 1000;
            throw new Error(`Request timeout (${timeoutSec}s)`);
        }
        throw err;
    } finally {
        clearTimeout(timeout);
    }
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
    } catch (err) {
        console.error('Status fetch failed:', err);
    }
}
