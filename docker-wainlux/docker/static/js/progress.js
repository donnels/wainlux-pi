/**
 * Progress tracking for K6 laser burns
 * Manages three progress bars: setup, upload, burn
 */

class ProgressTracker {
    constructor(sectionId = 'progressSection') {
        this.section = document.getElementById(sectionId);
        this.setupFill = document.getElementById('setupFill');
        this.setupText = document.getElementById('setupText');
        this.uploadFill = document.getElementById('uploadFill');
        this.uploadText = document.getElementById('uploadText');
        this.burnFill = document.getElementById('burnFill');
        this.burnText = document.getElementById('burnText');
        
        // Threshold for text color switching (white->black when filled)
        this.colorThreshold = 10;
    }
    
    /**
     * Show progress section
     */
    show() {
        if (this.section) {
            this.section.style.display = 'block';
        }
    }
    
    /**
     * Hide progress section
     */
    hide() {
        if (this.section) {
            this.section.style.display = 'none';
        }
    }
    
    /**
     * Reset all progress bars to 0%
     */
    reset() {
        this.updateBar('setup', 0);
        this.updateBar('upload', 0);
        this.updateBar('burn', 0);
    }
    
    /**
     * Update a specific progress bar
     * @param {string} bar - 'setup', 'upload', or 'burn'
     * @param {number} progress - Percentage (0-100)
     */
    updateBar(bar, progress) {
        let fill, text;
        
        if (bar === 'setup') {
            fill = this.setupFill;
            text = this.setupText;
        } else if (bar === 'upload') {
            fill = this.uploadFill;
            text = this.uploadText;
        } else if (bar === 'burn') {
            fill = this.burnFill;
            text = this.burnText;
        } else {
            return;
        }
        
        const pct = progress + '%';
        fill.style.width = pct;
        text.textContent = pct;
        text.style.color = progress > this.colorThreshold ? '#000' : '#fff';
    }
    
    /**
     * Handle SSE progress event
     * Maps phase names to appropriate progress bar
     * @param {Object} data - SSE event data with {phase, progress, message}
     */
    handleProgressEvent(data) {
        if (data.progress === undefined) {
            return;
        }
        
        // Map phases to progress bars
        if (data.phase === 'setup' || data.phase === 'connect' || data.phase === 'prepare') {
            this.updateBar('setup', data.progress);
        } else if (data.phase === 'upload') {
            this.updateBar('upload', data.progress);
        } else if (data.phase === 'burning' || data.phase === 'wait' || data.phase === 'finalize') {
            this.updateBar('burn', data.progress);
        }
    }
    
    /**
     * Set all bars to 100% (completion state)
     */
    complete() {
        this.updateBar('setup', 100);
        this.updateBar('upload', 100);
        this.updateBar('burn', 100);
    }
}
