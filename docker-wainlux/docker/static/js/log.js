/**
 * Log manager for displaying timestamped messages
 */

class LogManager {
    constructor(logBoxId = 'logBox') {
        this.logBox = document.getElementById(logBoxId);
    }
    
    /**
     * Add a timestamped log message
     * @param {string} msg - Message to log
     */
    add(msg) {
        if (!this.logBox) return;
        
        const line = document.createElement('div');
        const ts = (typeof formatLogTimestamp === 'function')
            ? formatLogTimestamp()
            : new Date().toISOString();
        line.textContent = `[${ts}] ${msg}`;
        this.logBox.appendChild(line);
        this.logBox.scrollTop = this.logBox.scrollHeight;
    }
    
    /**
     * Clear all log messages
     */
    clear() {
        if (this.logBox) {
            this.logBox.innerHTML = '';
        }
    }
    
    /**
     * Check if log box exists
     */
    exists() {
        return this.logBox !== null;
    }
}
