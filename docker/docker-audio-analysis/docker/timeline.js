(() => {
    'use strict';
    const player = document.querySelector('audio');
    const hasAudio = timelineData.has_audio !== false;
    const picker = document.getElementById('audio-file');
    if (picker) picker.addEventListener('change', () => {
        if (picker.files.length) {
            player.src = URL.createObjectURL(picker.files[0]);
            player.load();
        }
    });
    const canvas = document.getElementById('waveform');
    const overview = document.getElementById('overview');
    const timeZoom = document.getElementById('time-zoom');
    const amplitudeZoom = document.getElementById('amplitude-zoom');
    const amplitudeReference = document.getElementById('amplitude-reference');
    const detail = document.getElementById('event-detail');
    const tooltip = document.getElementById('timeline-tooltip');
    const seekSelection = document.getElementById('seek-selection');
    if (!hasAudio) {
        amplitudeZoom.disabled = true;
        amplitudeReference.disabled = true;
        seekSelection.textContent = 'Go to selection';
    }
    const bytes = Uint8Array.from(atob(timelineData.waveform.minmax_base64), character => character.charCodeAt(0));
    const view = new DataView(bytes.buffer);
    const envelope = new Int16Array(bytes.length / 2);
    for (let index = 0; index < envelope.length; index++) envelope[index] = view.getInt16(index * 2, true);
    const duration = Math.max(0.001, timelineData.duration);
    const binSeconds = timelineData.waveform.bin_ms / 1000;
    const events = timelineData.events;
    const colors = {rx: '#137b63', tx: '#a76b16', job: '#176d83', run: '#765d98'};
    let start = 0;
    let cursor = 0;
    let selected = null;
    let hits = [];
    let overviewDrag = null;
    let scheduled = false;
    let audioCache = null;
    const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
    const visible = () => duration / Number(timeZoom.value);
    const formatTime = (seconds, decimals = 3) => {
        const milliseconds = Math.max(0, Math.round(seconds * 1000));
        const minutes = Math.floor(milliseconds / 60000);
        const remainder = (milliseconds % 60000 / 1000).toFixed(decimals);
        return `${String(minutes).padStart(2, '0')}:${remainder.padStart(decimals ? decimals + 3 : 2, '0')}`;
    };
    function surface(target, height) {
        const width = Math.max(320, Math.round(target.getBoundingClientRect().width));
        const ratio = devicePixelRatio || 1;
        if (target.width !== Math.round(width * ratio) || target.height !== Math.round(height * ratio)) {
            target.width = Math.round(width * ratio);
            target.height = Math.round(height * ratio);
        }
        const context = target.getContext('2d');
        context.setTransform(ratio, 0, 0, ratio, 0, 0);
        context.fillStyle = '#fff';
        context.fillRect(0, 0, width, height);
        context.font = '12px sans-serif';
        return {context, width, left: width < 600 ? 87 : 120, right: width - 18};
    }
    function waveform(context, left, right, center, height, from, span, gain) {
        const upper = [], lower = [];
        for (let pixel = 0; pixel <= Math.ceil(right - left); pixel++) {
            const first = Math.max(0, Math.floor((from + pixel / (right - left) * span) / binSeconds));
            const last = Math.min(envelope.length / 2, Math.max(first + 1, Math.ceil((from + (pixel + 1) / (right - left) * span) / binSeconds)));
            let low = 0, high = 0;
            if (first < last) {
                low = 32767;
                high = -32768;
                for (let index = first; index < last; index++) {
                    low = Math.min(low, envelope[index * 2]);
                    high = Math.max(high, envelope[index * 2 + 1]);
                }
            }
            upper.push(center - clamp(high * gain, -1, 1) * height);
            lower.push(center - clamp(low * gain, -1, 1) * height);
        }
        context.beginPath();
        upper.forEach((value, index) => index ? context.lineTo(left + index, value) : context.moveTo(left, value));
        for (let index = lower.length - 1; index >= 0; index--) context.lineTo(left + index, lower[index]);
        context.closePath();
        context.fillStyle = '#355c6c';
        context.fill();
        context.strokeStyle = '#233f4c';
        context.lineWidth = 0.6;
        context.stroke();
    }
    const laneFor = event => {
        if (event.kind.startsWith('sync_')) return 'tx';
        if (event.kind === 'rx_byte' || event.kind.endsWith('_frame')) return 'rx';
        if (event.kind.startsWith('tx_') || event.kind.startsWith('transfer_')) return 'tx';
        if (event.kind === 'job_start' || event.kind === 'job_span') return 'job';
        return 'run';
    };
    const spans = [];
    for (const event of events) {
        if (event.kind !== 'job_start' && event.kind !== 'transfer_start') continue;
        const terminal = event.kind === 'job_start'
            ? ['silence_assumed', 'reported_complete', 'receive_error', 'stalled'] : ['transfer_end', 'transfer_error'];
        const end = events.find(candidate => candidate.job_index === event.job_index && candidate.audio_time_s >= event.audio_time_s && terminal.includes(candidate.kind));
        if (end) spans.push({...event, kind: event.kind === 'job_start' ? 'job_span' : 'tx_span', end_s: end.audio_time_s, completion: end.kind});
    }
    const description = event => {
        const packet = event.frame_bytes?.map(value => value.toString(16).padStart(2, '0').toUpperCase()).join(' ');
        const parts = [formatTime(event.audio_time_s), event.kind.replaceAll('_', ' ')];
        if (event.end_s !== undefined) parts.push(`to ${formatTime(event.end_s)} (${(event.end_s - event.audio_time_s).toFixed(3)} s)`);
        if (event.job_index) parts.push(`job ${event.job_index}`);
        if (event.phase) parts.push(event.phase);
        if (event.percent !== undefined && event.percent !== null) parts.push(`${event.percent}% of band`);
        if (packet) parts.push(`bytes: ${packet}`);
        if (event.byte !== undefined && event.byte !== null) parts.push(`RX ${event.byte.toString(16).padStart(2, '0').toUpperCase()}`);
        if (event.frame) parts.push(event.frame);
        if (event.quiet_ms !== undefined && event.quiet_ms !== null) parts.push(`quiet: ${event.quiet_ms} ms`);
        if (event.metadata) parts.push(event.metadata);
        if (event.completion) parts.push(`end: ${event.completion.replaceAll('_', ' ')}`);
        parts.push(`cursor delta: ${(cursor - event.audio_time_s).toFixed(3)} s`);
        return parts.join(' | ');
    };
    function selectEvent(event) {
        selected = event;
        seekSelection.disabled = !event;
        detail.textContent = event ? description(event) : 'No event selected';
        document.querySelectorAll('.log-event').forEach(row => row.setAttribute('aria-selected', String(events[Number(row.dataset.eventIndex)] === event)));
        requestDraw();
    }
    function drawWaveform() {
        const {context, width, left, right} = surface(canvas, 440);
        const span = visible();
        start = clamp(start, 0, duration - span);
        const position = seconds => left + (seconds - start) / span * (right - left);
        hits = [];
        context.fillStyle = '#233139';
        for (const [label, height] of [['Audio', 94], ['Laser RX', 259], ['Host TX', 307], ['Jobs / bands', 354], ['Run / sync', 403]]) context.fillText(label, 8, height);
        const major = [0.2, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800].find(step => step * (right - left) / span >= 76) || 3600;
        const minor = span <= 30 ? 0.2 : major / 5;
        for (let index = Math.ceil(start / minor); index * minor <= start + span + 1e-8; index++) {
            const second = index * minor;
            const horizontal = position(second);
            const isMajor = Math.abs(second / major - Math.round(second / major)) < 1e-6;
            context.strokeStyle = isMajor ? '#d2dde2' : '#e8eef1';
            context.beginPath();
            context.moveTo(horizontal, 45);
            context.lineTo(horizontal, isMajor ? 425 : 51);
            context.stroke();
            context.fillStyle = '#40515c';
            context.textAlign = 'center';
            if (isMajor) context.fillText(formatTime(second, major < 1 ? 1 : 0), horizontal, 31);
            else if (minor === 0.2 && (right - left) / span * minor >= 10) {
                context.font = '9px sans-serif';
                context.fillText(`.${Math.round(second * 10) % 10}`, horizontal, 61);
                context.font = '12px sans-serif';
            }
        }
        context.textAlign = 'left';
        context.strokeStyle = '#b7c8d0';
        for (const height of [151, 226, 280, 326, 374, 425]) {
            context.beginPath(); context.moveTo(left, height); context.lineTo(right, height); context.stroke();
        }
        const gain = Number(amplitudeZoom.value) * (amplitudeReference.value === 'peak' ? 0.85 / Math.max(1, timelineData.waveform.reference) : 1 / 32768);
        const cacheKey = [width, start, span, gain, devicePixelRatio].join(':');
        if (audioCache?.key !== cacheKey) {
            const layer = document.createElement('canvas');
            layer.width = Math.ceil(width * (devicePixelRatio || 1));
            layer.height = Math.ceil(155 * (devicePixelRatio || 1));
            const drawing = layer.getContext('2d');
            drawing.scale(devicePixelRatio || 1, devicePixelRatio || 1);
            waveform(drawing, left, right, 76, 72, start, span, gain);
            audioCache = {key: cacheKey, layer};
        }
        if (hasAudio) context.drawImage(audioCache.layer, 0, 74, width, 155);
        else {
            context.fillStyle = '#576871';
            context.fillText('No audio recording', left + 12, 150);
        }
        context.save();
        context.beginPath(); context.rect(left, 70, right - left, 355); context.clip();
        const ranges = {rx: [236, 273], tx: [288, 318], job: [335, 366], run: [384, 416]};
        for (const event of [...spans, ...events]) {
            const lane = laneFor(event);
            const sync = event.kind.startsWith('sync_');
            const [top, bottom] = sync ? (event.kind === 'sync_cue' ? [284, 300] : [302, 322]) : ranges[lane];
            if (event.audio_time_s > start + span || (event.end_s ?? event.audio_time_s) < start) continue;
            const horizontal = position(event.audio_time_s);
            const end = event.end_s === undefined ? horizontal : position(event.end_s);
            context.strokeStyle = sync ? '#6d4ba1' : colors[lane];
            context.fillStyle = sync ? '#e8def3' : lane === 'job' ? '#dcecef' : '#f1dcba';
            context.lineWidth = event === selected ? 3 : event.kind === 'rx_byte' ? 1 : 2;
            if (event.end_s !== undefined) {
                context.fillRect(horizontal, top, end - horizontal, bottom - top);
                context.strokeRect(horizontal, top, end - horizontal, bottom - top);
                context.fillStyle = '#233139';
                context.save(); context.beginPath(); context.rect(Math.max(left, horizontal) + 4, top, Math.max(0, Math.min(right, end) - Math.max(left, horizontal) - 8), bottom - top); context.clip();
                context.fillText(event.label || event.metadata || `Job ${event.job_index ?? ''} / ${event.kind.replaceAll('_', ' ')}`, Math.max(left, horizontal) + 7, top + (bottom - top) / 2 + 4);
                context.restore();
            } else {
                context.beginPath(); context.moveTo(horizontal, top); context.lineTo(horizontal, bottom); context.stroke();
            }
            hits.push({event, left: Math.max(left, horizontal - 5), right: Math.min(right, Math.max(horizontal + 5, end)), top, bottom});
        }
        context.restore();
        if (cursor >= start && cursor <= start + span) {
            const horizontal = position(cursor);
            context.strokeStyle = '#cf3548'; context.lineWidth = 2;
            context.beginPath(); context.moveTo(horizontal, 65); context.lineTo(horizontal, 425); context.stroke();
            context.fillStyle = '#b32138';
            context.fillText(formatTime(cursor), clamp(horizontal + 6, left, right - 94), 74);
        }
        document.getElementById('window-time').value = `${formatTime(start)} - ${formatTime(start + span)} / ${formatTime(duration)}`;
        document.getElementById('cursor-time').value = formatTime(cursor);
        document.getElementById('time-zoom-value').value = `${Number(timeZoom.value).toFixed(2).replace(/\.00$/, '')}x`;
        if (selected) detail.textContent = description(selected);
        drawOverview();
    }
    let overviewCache = null;
    function drawOverview() {
        const {context, width, left, right} = surface(overview, 76);
        if (overviewCache?.width !== overview.width) {
            const layer = document.createElement('canvas');
            layer.width = overview.width; layer.height = overview.height;
            const drawing = layer.getContext('2d'); drawing.scale(devicePixelRatio || 1, devicePixelRatio || 1);
            if (hasAudio) waveform(drawing, left, right, 28, 22, 0, duration, 0.85 / Math.max(1, timelineData.waveform.reference));
            overviewCache = layer;
        }
        context.drawImage(overviewCache, 0, 0, width, 76);
        const horizontal = left + start / duration * (right - left);
        const end = left + (start + visible()) / duration * (right - left);
        context.fillStyle = '#176d8333'; context.strokeStyle = '#176d83'; context.lineWidth = 2;
        context.fillRect(horizontal, 3, end - horizontal, 49); context.strokeRect(horizontal, 3, end - horizontal, 49);
        context.lineWidth = 4;
        context.beginPath(); context.moveTo(horizontal, 12); context.lineTo(horizontal, 43); context.moveTo(end, 12); context.lineTo(end, 43); context.stroke();
        context.fillStyle = '#40515c'; context.fillText('00:00', left, 70); context.textAlign = 'right'; context.fillText(formatTime(duration, 0), right, 70); context.textAlign = 'left';
        for (const event of events.filter(event => laneFor(event) === 'run' || event.kind === 'sync_cue')) {
            const position = left + event.audio_time_s / duration * (right - left);
            context.strokeStyle = event.kind === 'sync_cue' ? '#6d4ba1' : colors.run; context.lineWidth = 1;
            context.beginPath(); context.moveTo(position, 45); context.lineTo(position, 54); context.stroke();
        }
    }
    function requestDraw() {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(() => { scheduled = false; drawWaveform(); });
    }
    function local(event, target) {
        const rect = target.getBoundingClientRect();
        return {horizontal: event.clientX - rect.left, vertical: event.clientY - rect.top};
    }
    function eventAt(point) {
        return hits.filter(hit => point.horizontal >= hit.left && point.horizontal <= hit.right && point.vertical >= hit.top && point.vertical <= hit.bottom)
            .sort((first, second) => (first.event.kind === 'rx_byte') - (second.event.kind === 'rx_byte') || Math.abs((first.left + first.right) / 2 - point.horizontal) - Math.abs((second.left + second.right) / 2 - point.horizontal))[0]?.event;
    }
    canvas.addEventListener('pointermove', event => {
        const point = local(event, canvas);
        const candidate = eventAt(point);
        tooltip.hidden = false;
        tooltip.textContent = candidate ? description(candidate) : formatTime(start + clamp((point.horizontal - (canvas.clientWidth < 600 ? 87 : 120)) / (canvas.clientWidth - (canvas.clientWidth < 600 ? 87 : 120) - 18), 0, 1) * visible());
    });
    canvas.addEventListener('pointerleave', () => { tooltip.hidden = true; });
    canvas.addEventListener('click', event => {
        const point = local(event, canvas);
        const candidate = eventAt(point);
        if (candidate) { selectEvent(candidate); return; }
        const left = canvas.clientWidth < 600 ? 87 : 120;
        if (point.horizontal < left) return;
        cursor = clamp(start + (point.horizontal - left) / (canvas.clientWidth - left - 18) * visible(), 0, duration);
        if (point.vertical < 226) seekTo(cursor);
        requestDraw();
    });
    function seekTo(seconds) {
        cursor = clamp(seconds, 0, duration);
        try { if (player) player.currentTime = cursor; } catch (_) { }
        if (cursor < start || cursor > start + visible()) start = clamp(cursor - visible() / 2, 0, duration - visible());
        requestDraw();
    }
    seekSelection.addEventListener('click', () => { if (selected) seekTo(selected.audio_time_s); });
    document.querySelectorAll('.log-event').forEach(row => {
        const select = () => selectEvent(events[Number(row.dataset.eventIndex)]);
        row.addEventListener('click', select);
        row.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } });
    });
    timeZoom.addEventListener('input', () => {
        const oldSpan = Number(timeZoom.dataset.span || duration);
        const selectedTime = selected?.audio_time_s;
        const anchor = selectedTime !== undefined && selectedTime >= start && selectedTime <= start + oldSpan
            ? selectedTime : cursor >= start && cursor <= start + oldSpan ? cursor : start + oldSpan / 2;
        start = clamp(anchor - visible() / 2, 0, duration - visible());
        timeZoom.dataset.span = visible(); requestDraw();
    });
    amplitudeZoom.addEventListener('input', () => {
        if (amplitudeZoom.validity.valid) requestDraw();
    });
    amplitudeZoom.addEventListener('change', () => {
        amplitudeZoom.value = clamp(Number(amplitudeZoom.value) || 1, 0.25, 32); requestDraw();
    });
    amplitudeReference.addEventListener('change', requestDraw);
    overview.addEventListener('pointerdown', event => {
        const width = overview.clientWidth, left = width < 600 ? 87 : 120;
        const horizontal = local(event, overview).horizontal;
        const seconds = clamp((horizontal - left) / (width - left - 18), 0, 1) * duration;
        const first = left + start / duration * (width - left - 18);
        const last = left + (start + visible()) / duration * (width - left - 18);
        const narrowCenter = last - first < 12 && horizontal > first && horizontal < last;
        const mode = narrowCenter ? 'pan' : Math.abs(horizontal - first) <= 3 && Math.abs(horizontal - first) < Math.abs(horizontal - last) ? 'left' : Math.abs(horizontal - last) <= 3 ? 'right' : 'pan';
        overviewDrag = {mode, offset: seconds >= start && seconds <= start + visible() ? seconds - start : visible() / 2, end: start + visible()};
        overview.setPointerCapture(event.pointerId);
        if (mode === 'pan') start = clamp(seconds - overviewDrag.offset, 0, duration - visible());
        requestDraw();
    });
    overview.addEventListener('pointermove', event => {
        if (!overviewDrag) return;
        const width = overview.clientWidth, left = width < 600 ? 87 : 120;
        const seconds = clamp((local(event, overview).horizontal - left) / (width - left - 18), 0, 1) * duration;
        if (overviewDrag.mode === 'pan') start = clamp(seconds - overviewDrag.offset, 0, duration - visible());
        else {
            const end = overviewDrag.mode === 'left' ? overviewDrag.end : clamp(seconds, start + duration / 128, duration);
            start = overviewDrag.mode === 'left' ? clamp(seconds, 0, end - duration / 128) : start;
            timeZoom.value = clamp(duration / (end - start), 1, 128);
            timeZoom.dataset.span = visible();
        }
        requestDraw();
    });
    const endDrag = () => { overviewDrag = null; };
    overview.addEventListener('pointerup', endDrag);
    overview.addEventListener('pointercancel', endDrag);
    function pan(event) {
        const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
        if (Number(timeZoom.value) <= 1 || event.ctrlKey) return;
        event.preventDefault(); start = clamp(start + delta / canvas.clientWidth * visible(), 0, duration - visible()); requestDraw();
    }
    canvas.addEventListener('wheel', pan, {passive: false});
    overview.addEventListener('wheel', pan, {passive: false});
    overview.addEventListener('keydown', event => {
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
            event.preventDefault(); start = clamp(start + (event.key === 'ArrowRight' ? 1 : -1) * visible() / 10, 0, duration - visible()); requestDraw();
        }
    });
    player?.addEventListener('timeupdate', () => {
        cursor = player.currentTime;
        if (!player.paused && (cursor < start || cursor > start + visible())) start = clamp(cursor, 0, duration - visible());
        requestDraw();
    });
    player?.addEventListener('seeked', () => { cursor = player.currentTime; requestDraw(); });
    document.getElementById('timeline-end').addEventListener('click', () => {
        timeZoom.value = '128'; timeZoom.dataset.span = visible(); start = duration - visible(); requestDraw();
    });
    new ResizeObserver(requestDraw).observe(canvas);
    requestDraw();
})();