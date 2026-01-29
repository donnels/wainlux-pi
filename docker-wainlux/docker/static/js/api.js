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
    const timeout = setTimeout(() => controller.abort(), 30000); // 30s timeout
    
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
        
        if (!res.ok || !data.success) {
            throw new Error(data.error || 'Request failed');
        }
        
        return data;
    } catch (err) {
        if (err.name === 'AbortError') {
            throw new Error('Request timeout (30s)');
        }
        throw err;
    } finally {
        clearTimeout(timeout);
    }
}
