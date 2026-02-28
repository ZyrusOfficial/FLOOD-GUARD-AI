/**
 * dashboard.js — Flood Early Warning System Dashboard Frontend
 *
 * Real-time data via Socket.IO, Chart.js water level graph,
 * gauge animation, toast notifications, and audio alarm.
 */

// ---- Socket.IO Connection ----
const socket = io();

// ---- State ----
let currentAlertLevel = 0;
let chartDataPoints = [];
let chart = null;
let alarmActive = false;

// ---- Elements ----
const els = {
    systemDot: document.getElementById('systemDot'),
    systemStatus: document.getElementById('systemStatus'),
    uptime: document.getElementById('uptime'),
    cameraSource: document.getElementById('cameraSource'),
    cameraFps: document.getElementById('cameraFps'),
    videoOverlay: document.getElementById('videoOverlay'),
    videoFeed: document.getElementById('videoFeed'),
    gaugeValue: document.getElementById('gaugeValue'),
    gaugeFill: document.getElementById('gaugeFill'),
    gaugeCard: document.getElementById('gaugeCard'),
    confidenceBadge: document.getElementById('confidenceBadge'),
    alertIndicator: document.getElementById('alertIndicator'),
    alertLevelName: document.getElementById('alertLevelName'),
    alertLevelDesc: document.getElementById('alertLevelDesc'),
    alertCard: document.getElementById('alertCard'),
    alertsLog: document.getElementById('alertsLog'),
    toastContainer: document.getElementById('toastContainer'),
    btnTestAlert: document.getElementById('btnTestAlert'),
    channelDashboard: document.getElementById('channelDashboard'),
    channelSms: document.getElementById('channelSms'),
    channelBle: document.getElementById('channelBle'),
    channelNostr: document.getElementById('channelNostr'),
};

// ---- Alert Level Descriptions ----
const LEVEL_INFO = {
    0: { name: 'NORMAL', desc: 'All systems nominal', class: '' },
    1: { name: 'WARNING', desc: 'Water level elevated — monitoring closely', class: 'warning' },
    2: { name: 'DANGER', desc: 'Water level dangerous — prepare to evacuate', class: 'danger' },
    3: { name: 'CRITICAL', desc: 'EVACUATE IMMEDIATELY — flood imminent', class: 'critical' },
};

// ---- Initialize Chart ----
function initChart() {
    const ctx = document.getElementById('waterLevelChart').getContext('2d');

    chart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Water Level (cm)',
                data: [],
                borderColor: '#3b82f6',
                backgroundColor: 'rgba(59, 130, 246, 0.1)',
                borderWidth: 2,
                fill: true,
                tension: 0.4,
                pointRadius: 0,
                pointHitRadius: 10,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'index',
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: 'rgba(17, 24, 39, 0.9)',
                    titleColor: '#f1f5f9',
                    bodyColor: '#94a3b8',
                    borderColor: 'rgba(255,255,255,0.1)',
                    borderWidth: 1,
                    padding: 10,
                    cornerRadius: 8,
                    displayColors: false,
                    callbacks: {
                        label: ctx => `${ctx.parsed.y.toFixed(1)} cm`
                    }
                }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255,255,255,0.03)' },
                    ticks: {
                        color: '#64748b',
                        font: { size: 10 },
                        maxTicksLimit: 10
                    }
                },
                y: {
                    grid: { color: 'rgba(255,255,255,0.03)' },
                    ticks: {
                        color: '#64748b',
                        font: { size: 10 },
                        callback: v => v + ' cm'
                    }
                }
            },
            animation: {
                duration: 300
            }
        }
    });
}

// ---- Socket.IO Events ----
socket.on('connect', () => {
    console.log('Connected to dashboard server');
    els.systemStatus.textContent = 'Connected';
    els.systemDot.className = 'system-dot';
});

socket.on('disconnect', () => {
    console.log('Disconnected from server');
    els.systemStatus.textContent = 'Disconnected';
    els.systemDot.className = 'system-dot offline';
});

socket.on('water_level', (data) => {
    updateWaterLevel(data);
});

socket.on('alert', (data) => {
    addAlertEntry(data);
    showToast(data);
});

socket.on('status_update', (data) => {
    updateAlertStatus(data);
});

// ---- Update Functions ----
function updateWaterLevel(data) {
    const cm = data.water_level_cm;
    const confidence = data.confidence || 0;
    const alertLevel = data.alert_level || 0;

    // Update gauge value
    if (cm !== null && cm !== undefined) {
        els.gaugeValue.textContent = Math.round(cm);

        // Color based on alert level
        const info = LEVEL_INFO[alertLevel];
        els.gaugeValue.className = 'gauge-value ' + info.class;

        // Update gauge fill (map cm to percentage, assuming 100-350 range)
        const minCm = 100;
        const maxCm = 350;
        const pct = Math.max(0, Math.min(100, ((cm - minCm) / (maxCm - minCm)) * 100));
        els.gaugeFill.style.width = pct + '%';
    }

    // Confidence
    els.confidenceBadge.textContent = Math.round(confidence * 100) + '% confidence';

    // Add to chart
    const time = new Date(data.timestamp * 1000);
    const timeStr = time.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

    chartDataPoints.push({ time: timeStr, value: cm, timestamp: data.timestamp });

    // Keep max 500 points
    if (chartDataPoints.length > 500) {
        chartDataPoints = chartDataPoints.slice(-500);
    }

    // Update chart
    if (chart) {
        chart.data.labels = chartDataPoints.map(p => p.time);
        chart.data.datasets[0].data = chartDataPoints.map(p => p.value);

        // Dynamic color based on alert level
        const colors = ['#22c55e', '#eab308', '#f97316', '#ef4444'];
        chart.data.datasets[0].borderColor = colors[alertLevel] || '#3b82f6';
        chart.data.datasets[0].backgroundColor = (colors[alertLevel] || '#3b82f6') + '15';

        chart.update('none');
    }

    // Update alert level
    updateAlertLevel(alertLevel);
}

function updateAlertLevel(level) {
    if (level === currentAlertLevel) return;
    currentAlertLevel = level;

    const info = LEVEL_INFO[level];

    // Alert indicator
    els.alertIndicator.className = 'alert-level-indicator ' + info.class;
    els.alertLevelName.textContent = info.name;
    els.alertLevelName.style.color = level === 0 ? '#22c55e' : level === 1 ? '#eab308' : level === 2 ? '#f97316' : '#ef4444';
    els.alertLevelDesc.textContent = info.desc;

    // System dot
    els.systemDot.className = 'system-dot ' + info.class;

    // Alert card border glow
    const borderColors = ['rgba(34,197,94,0.3)', 'rgba(234,179,8,0.4)', 'rgba(249,115,22,0.5)', 'rgba(239,68,68,0.6)'];
    els.alertCard.style.borderColor = borderColors[level];
    els.alertCard.style.boxShadow = `0 0 20px ${borderColors[level]}`;

    // Audio alarm for critical
    if (level >= 3 && !alarmActive) {
        startAlarm();
    } else if (level < 3 && alarmActive) {
        stopAlarm();
    }
}

function updateAlertStatus(data) {
    if (!data) return;

    // Update channels
    const channels = data.channels || {};
    setChannelStatus(els.channelDashboard, channels.dashboard !== false);
    setChannelStatus(els.channelSms, channels.sms === true);
    setChannelStatus(els.channelBle, channels.ble === true);
    setChannelStatus(els.channelNostr, channels.nostr === true);

    // Update alert level
    updateAlertLevel(data.level || 0);
}

function setChannelStatus(el, active) {
    if (active) {
        el.classList.add('active');
        el.classList.remove('offline');
        el.querySelector('.channel-status').style.color = '#22c55e';
    } else {
        el.classList.remove('active');
        el.classList.add('offline');
        el.querySelector('.channel-status').style.color = '#64748b';
    }
}

// ---- Alert History ----
function addAlertEntry(data) {
    // Remove empty message
    const emptyEl = els.alertsLog.querySelector('.alert-empty');
    if (emptyEl) emptyEl.remove();

    const time = new Date(data.timestamp * 1000);
    const timeStr = time.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });

    const levelClass = (data.new_level || '').toLowerCase();

    const entry = document.createElement('div');
    entry.className = 'alert-entry';
    entry.innerHTML = `
        <span class="alert-entry-time">${timeStr}</span>
        <span class="alert-entry-badge ${levelClass}">${data.new_level || 'INFO'}</span>
        <span class="alert-entry-msg">${data.message || 'Alert triggered'}</span>
    `;

    // Prepend (newest first)
    els.alertsLog.insertBefore(entry, els.alertsLog.firstChild);

    // Keep max 50 entries
    while (els.alertsLog.children.length > 50) {
        els.alertsLog.removeChild(els.alertsLog.lastChild);
    }
}

// ---- Toast Notifications ----
function showToast(data) {
    const levelClass = (data.new_level || '').toLowerCase();

    const toast = document.createElement('div');
    toast.className = `toast ${levelClass}`;
    toast.innerHTML = `
        <div class="toast-title">⚠️ Flood Alert: ${data.new_level || 'ALERT'}</div>
        <div class="toast-message">${data.message || 'Water level threshold exceeded'}</div>
    `;

    els.toastContainer.appendChild(toast);

    // Auto-remove after 5s
    setTimeout(() => {
        if (toast.parentNode) {
            toast.parentNode.removeChild(toast);
        }
    }, 5000);
}

// ---- Audio Alarm ----
function startAlarm() {
    alarmActive = true;
    // Use Web Audio API for a simple alarm tone
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const oscillator = ctx.createOscillator();
        const gain = ctx.createGain();
        oscillator.connect(gain);
        gain.connect(ctx.destination);
        oscillator.frequency.value = 800;
        oscillator.type = 'square';
        gain.gain.value = 0.1;
        oscillator.start();

        window._alarmOscillator = oscillator;
        window._alarmGain = gain;
        window._alarmCtx = ctx;
    } catch (e) {
        console.warn('Audio alarm not available:', e);
    }
}

function stopAlarm() {
    alarmActive = false;
    try {
        if (window._alarmOscillator) {
            window._alarmOscillator.stop();
            window._alarmCtx.close();
        }
    } catch (e) {
        // ignore
    }
}

// ---- Periodic Status Polling ----
function pollStatus() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => {
            // Camera info
            if (data.camera) {
                els.cameraSource.textContent = data.camera.source || '--';
                els.cameraFps.textContent = (data.camera.fps || 0).toFixed(0) + ' FPS';

                if (data.camera.connected) {
                    els.videoOverlay.classList.add('hidden');
                } else {
                    els.videoOverlay.classList.remove('hidden');
                }
            }

            // Uptime
            if (data.system) {
                const secs = Math.floor(data.system.uptime || 0);
                const h = Math.floor(secs / 3600);
                const m = Math.floor((secs % 3600) / 60);
                const s = secs % 60;
                els.uptime.textContent =
                    String(h).padStart(2, '0') + ':' +
                    String(m).padStart(2, '0') + ':' +
                    String(s).padStart(2, '0');
            }

            // Alert status
            if (data.alert) {
                updateAlertStatus(data.alert);
            }
        })
        .catch(() => { /* silent */ });
}

// ---- Test Alert Button ----
els.btnTestAlert.addEventListener('click', () => {
    fetch('/api/test_alert', { method: 'POST' })
        .then(r => r.json())
        .then(() => {
            showToast({
                new_level: 'WARNING',
                message: 'Test alert sent through all channels'
            });
        })
        .catch(e => console.error('Test alert failed:', e));
});

// ---- Chart Range Buttons ----
document.querySelectorAll('.chart-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.chart-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        const range = parseInt(btn.dataset.range) * 60; // minutes to seconds
        const now = Date.now() / 1000;
        const filtered = chartDataPoints.filter(p => (now - p.timestamp) <= range);

        if (chart) {
            chart.data.labels = filtered.map(p => p.time);
            chart.data.datasets[0].data = filtered.map(p => p.value);
            chart.update('none');
        }
    });
});

// ---- Video feed error handling ----
els.videoFeed.addEventListener('error', () => {
    els.videoOverlay.classList.remove('hidden');
});

els.videoFeed.addEventListener('load', () => {
    els.videoOverlay.classList.add('hidden');
});

// ---- Init ----
document.addEventListener('DOMContentLoaded', () => {
    initChart();
    pollStatus();
    setInterval(pollStatus, 3000);
});
