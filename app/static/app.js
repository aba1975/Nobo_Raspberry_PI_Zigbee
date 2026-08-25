// ===== Global State =====
let ws = null;
let reconnectInterval = null;
let pingInterval = null;
let zones = [];
let hubInfo = {};
let temperatureTimers = {};
let currentPage = 'main';
let globalMode = 'home';  // 'home', 'away', 'eco', or 'comfort'
let scheduleData = {};
let currentScheduleZone = null;
let currentZoneDetail = null;
let logFilter = 'all';
let logAutoRefreshTimer = null;
let logEntries = [];
let activeFormState = null;   // { type: 'add'|'edit', day: string, blockIndex: number|null }
let copyDayPopoverDay = null; // which day's copy popover is currently open

const SCHEDULE_DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
const SCHEDULE_DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

// Maps serial prefix → { name, image } where image is the slug for /static/images/{slug}.png
const DEVICE_MODELS = {
    '000': { name: 'NTB-2R',                  image: 'NTB-2R' },
    '120': { name: 'RS 700',                   image: 'RS-700' },
    '121': { name: 'RSX 700',                  image: 'RSX-700' },
    '130': { name: 'RCE 700',                  image: 'RCE-700' },
    '160': { name: 'R80 RDC 700',              image: 'R80-RDC-700' },
    '165': { name: 'R80 RDC 700 LST (GB)',     image: 'R80-RDC-700' },
    '168': { name: 'NCU-2R',                   image: 'NCU-2R' },
    '169': { name: 'DCU-2R',                   image: 'DCU-2R' },
    '170': { name: 'Serie 18, ewt touch',      image: 'SERIE-18' },
    '180': { name: '2NC9 700',                 image: '2NC9-700' },
    '182': { name: 'R80 RSC 700',              image: 'R80-RSC-700' },
    '183': { name: 'R80 RSC 700',              image: 'R80-RSC-700' },
    '184': { name: 'NCU-1R',                   image: 'NCU-1R' },
    '186': { name: 'DCU-1R',                   image: 'DCU-1R' },
    '190': { name: 'Safir',                    image: 'SAFIR' },
    '192': { name: 'R80 TXF 700',              image: 'R80-TXF-700' },
    '194': { name: 'R80 RXC 700',              image: 'R80-RXC-700' },
    '198': { name: 'NCU-ER',                   image: 'NCU-ER' },
    '199': { name: 'DCU-ER',                   image: 'DCU-ER' },
    '200': { name: 'TRB 36 700',               image: 'TRB-36-700' },
    '210': { name: 'NTB-2R',                   image: 'NTB-2R' },
    '220': { name: 'TR36',                     image: 'TR36' },
    '230': { name: 'TCU 700',                  image: 'TCU-700' },
    '231': { name: 'THB 700',                  image: 'THB-700' },
    '232': { name: 'TXB 700',                  image: 'TXB-700' },
    '234': { name: 'SW4',                      image: 'SW4' },
};

function deviceImageTag(serial, altText, cssClass) {
    const prefix = String(serial).replace(/\s/g, '').slice(0, 3);
    const model = DEVICE_MODELS[prefix];
    const slug = model ? model.image : 'placeholder';
    const slugLower = slug.toLowerCase();
    const cls = cssClass ? ` class="${cssClass}"` : '';
    const alt = altText || (model ? model.name : 'Device');
    return `<img src="/static/images/${slug}.png" alt="${escapeHtml(alt)}"${cls} onerror="this.onerror=null;this.src='/static/images/${slugLower}.svg';this.onerror=function(){this.src='/static/images/placeholder.svg';};">`;
}

/**
 * Attach auto-formatting listeners to the away-schedule date/time inputs.
 */
function initAwayScheduleInputFormatters() {
    const dateIds = ['scheduleStartDate', 'scheduleEndDate'];
    const timeIds = ['scheduleStartTime', 'scheduleEndTime'];

    dateIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.setAttribute('inputmode', 'numeric');
            el.addEventListener('input', function() {
                const pos = this.selectionStart;
                const oldLen = this.value.length;
                this.value = normalizeDateDigitsToDDMMYYYY(this.value);
                const newLen = this.value.length;
                const newPos = pos + (newLen - oldLen);
                this.setSelectionRange(newPos, newPos);
            });
        }
    });

    timeIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.setAttribute('inputmode', 'numeric');
            el.addEventListener('input', function() {
                const pos = this.selectionStart;
                const oldLen = this.value.length;
                this.value = normalizeTimeDigitsToHHmm(this.value);
                const newLen = this.value.length;
                const newPos = pos + (newLen - oldLen);
                this.setSelectionRange(newPos, newPos);
            });
        }
    });
}

// ===== Initialization =====
document.addEventListener('DOMContentLoaded', () => {
    console.log('Nobø Control - Initializing...');
    initThemeToggle();
    initRouter();
    initWebSocket();
    initCapabilities();
    fetchHubInfo();
    fetchZones();
    fetchAwaySchedule();
    initAwayScheduleInputFormatters();

    // Hub reachability can change without any user action (hub rebooted, network
    // came back), and the reconnect loop runs server-side, so poll for it.
    setInterval(fetchHubInfo, 15000);
});

// ===== Feature Capabilities =====
// A few features are not available in every mode. The server publishes what it
// can actually do at /api/capabilities, and every control that maps to an
// unsupported feature is disabled with the reason shown as its tooltip. Without
// this the buttons look live and fail with a 501 (QA defect D-04).
//
// The list used to cover most of the editing features, because they were only
// implemented for demo mode. They now work against a real hub too, so all that
// is left is device discovery, which needs the hub's radio and is therefore the
// one thing demo mode cannot do.

let capabilities = {};

// Which capability each onclick handler needs. Anything not listed here works
// in both demo and real-hub mode and is never gated.
const CAPABILITY_BY_ACTION = {
    startDeviceSearch: 'discover_devices',
    stopDeviceSearch: 'discover_devices',
    useDiscoveredDevice: 'discover_devices',
};

async function initCapabilities() {
    await refreshCapabilities();

    // Most controls are rendered from template strings scattered across this
    // file, and new ones appear whenever a page or modal is re-rendered.
    // Watching the DOM means a control cannot escape gating by being added from
    // a render path that forgot to call the gating function.
    let queued = false;
    const observer = new MutationObserver(() => {
        if (queued) return;
        queued = true;
        requestAnimationFrame(() => {
            queued = false;
            applyCapabilityGating();
        });
    });
    observer.observe(document.body, { childList: true, subtree: true });
}

async function refreshCapabilities() {
    try {
        const r = await fetch('/api/capabilities');
        if (!r.ok) return;
        const data = await r.json();
        capabilities = data.features || {};
        applyCapabilityGating();
        renderCapabilityNotices(data.demo_mode);
    } catch (e) {
        console.error('Could not read capabilities:', e);
    }
}

/** The capability a control needs, or null if it is always available. */
function capabilityForElement(el) {
    const handler = el.getAttribute('data-capability');
    if (handler) return handler;

    const onclick = el.getAttribute('onclick');
    if (!onclick) return null;

    const match = onclick.match(/([A-Za-z_$][\w$]*)\s*\(/);
    return match ? (CAPABILITY_BY_ACTION[match[1]] || null) : null;
}

function applyCapabilityGating() {
    if (!Object.keys(capabilities).length) return;

    document.querySelectorAll('[onclick], [data-capability]').forEach(el => {
        const name = capabilityForElement(el);
        if (!name) return;

        const cap = capabilities[name];
        const blocked = cap && cap.supported === false;

        if (blocked) {
            el.classList.add('capability-disabled');
            el.setAttribute('aria-disabled', 'true');
            el.title = cap.reason;
            if ('disabled' in el) el.disabled = true;
        } else if (el.classList.contains('capability-disabled')) {
            el.classList.remove('capability-disabled');
            el.removeAttribute('aria-disabled');
            el.removeAttribute('title');
            if ('disabled' in el) el.disabled = false;
        }
    });
}

/** Explain once per section why the greyed-out controls are greyed out. */
function renderCapabilityNotices(demoMode) {
    document.querySelectorAll('.capability-notice').forEach(el => el.remove());
    if (demoMode !== false) return;

    const notice = document.createElement('p');
    notice.className = 'capability-notice';
    notice.textContent =
        'Connected to a real Nobø Eco Hub. Adding, removing and renaming zones, ' +
        'devices and week schedules is done in the official Nobø Energy Control ' +
        'app — those controls are greyed out here. Temperatures, comfort/eco/away ' +
        'modes and the away schedule all work normally.';

    const zoneFooter = document.querySelector('.zone-list-footer');
    if (zoneFooter) zoneFooter.insertAdjacentElement('beforebegin', notice);

    const devices = document.querySelector('.devices-section h2');
    if (devices) devices.insertAdjacentElement('afterend', notice.cloneNode(true));
}

// ===== Theme Toggle =====
function initThemeToggle() {
    const toggle = document.getElementById('themeToggle');
    if (!toggle) return;

    // Determine initial theme
    const saved = localStorage.getItem('nobo-theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const theme = saved || (prefersDark ? 'dark' : 'light');
    applyTheme(theme);

    toggle.addEventListener('click', () => {
        const current = document.documentElement.getAttribute('data-theme') || 'light';
        const next = current === 'dark' ? 'light' : 'dark';
        applyTheme(next);
        localStorage.setItem('nobo-theme', next);
    });

    // Listen for system preference changes (when no saved preference)
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
        if (!localStorage.getItem('nobo-theme')) {
            applyTheme(e.matches ? 'dark' : 'light');
        }
    });
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    // Update meta theme-color
    const metaThemeColor = document.getElementById('metaThemeColor');
    if (metaThemeColor) {
        metaThemeColor.setAttribute('content', theme === 'dark' ? '#1a1d23' : '#2c3e50');
    }
}

// ===== Router =====
function initRouter() {
    // Handle hash changes
    window.addEventListener('hashchange', handleRouteChange);
    
    // Handle initial route
    handleRouteChange();
}

function handleRouteChange() {
    let hash = window.location.hash.slice(1) || 'main';
    
    // Check if it's a zone detail route
    if (hash.startsWith('zone/')) {
        const zoneId = hash.split('/')[1];
        navigateToZoneDetail(zoneId);
    } else {
        navigateToPage(hash);
    }
}

function navigateToPage(pageName) {
    // Stop log auto-refresh when leaving log page
    if (pageName !== 'log' && logAutoRefreshTimer) {
        clearInterval(logAutoRefreshTimer);
        logAutoRefreshTimer = null;
        const cb = document.getElementById('logAutoRefresh');
        if (cb) cb.checked = false;
    }

    // Hide all pages
    document.querySelectorAll('.page').forEach(page => {
        page.classList.remove('active');
    });
    
    // Show selected page
    const pageElement = document.getElementById(`${pageName}Page`);
    if (pageElement) {
        pageElement.classList.add('active');
        currentPage = pageName;
        
        // Update nav links
        document.querySelectorAll('.nav-link').forEach(link => {
            link.classList.remove('active');
            if (link.dataset.page === pageName) {
                link.classList.add('active');
            }
        });
        
        // Load page-specific data
        if (pageName === 'devices') {
            loadDevicesPage();
        } else if (pageName === 'log') {
            loadLogPage();
        }
    }
}

function navigateToZoneDetail(zoneId) {
    currentZoneDetail = zoneId;
    window.location.hash = '#zone/' + zoneId;
    navigateToPage('zoneDetail');
    renderZoneDetail(zoneId);
}

function navigateBack() {
    currentZoneDetail = null;
    window.location.hash = '#main';
    navigateToPage('main');
    renderZoneList();
}

// ===== WebSocket Connection =====
function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;
    
    console.log('Connecting to WebSocket:', wsUrl);
    updateConnectionStatus('connecting');
    
    try {
        ws = new WebSocket(wsUrl);
        
        ws.onopen = () => {
            console.log('WebSocket connected');
            updateConnectionStatus('connected');
            
            if (reconnectInterval) {
                clearInterval(reconnectInterval);
                reconnectInterval = null;
            }
            
            if (pingInterval) {
                clearInterval(pingInterval);
            }
            
            pingInterval = setInterval(() => {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send('ping');
                }
            }, 30000);
        };
        
        ws.onmessage = (event) => {
            try {
                // Ignore pong responses
                if (event.data === 'pong') {
                    return;
                }
                
                const data = JSON.parse(event.data);
                console.log('WebSocket message received:', data.type);
                
                if (data.type === 'zones_update') {
                    handleZoneUpdate(data.data);
                }
            } catch (error) {
                console.error('Error parsing WebSocket message:', error);
            }
        };
        
        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
            updateConnectionStatus('error');
        };
        
        ws.onclose = () => {
            console.log('WebSocket closed');
            updateConnectionStatus('disconnected');
            
            if (!reconnectInterval) {
                reconnectInterval = setInterval(() => {
                    console.log('Attempting to reconnect...');
                    initWebSocket();
                }, 5000);
            }
        };
    } catch (error) {
        console.error('Failed to create WebSocket:', error);
        updateConnectionStatus('error');
    }
}

function handleZoneUpdate(data) {
    // Handle both {zones: []} and [] formats
    zones = data.zones || data;
    updateLastUpdated();
    
    if (currentPage === 'main') {
        renderZoneList();
    } else if (currentPage === 'zoneDetail' && currentZoneDetail) {
        renderZoneDetail(currentZoneDetail);
    } else if (currentPage === 'devices') {
        renderDevicesList();
    }
}

// ===== API Calls =====
async function fetchHubInfo() {
    try {
        const response = await fetch('/api/hub');

        if (response.ok) {
            hubInfo = await response.json();
            updateHubInfo();
            return;
        }

        // /api/hub returns 503 while the hub is unreachable. Fall back to the
        // health endpoint, which always answers, so the header can show the real
        // state instead of silently keeping stale "connected" information.
        const health = await fetch('/api/health');
        if (health.ok) {
            const data = await health.json();
            hubInfo = {
                ...hubInfo,
                connected: data.connected === true,
                demo_mode: data.demo_mode === true
            };
            updateHubInfo();
        }
    } catch (error) {
        console.error('Error fetching hub info:', error);
    }
}

async function fetchZones() {
    // Show skeleton loaders on first load
    if (zones.length === 0 && currentPage === 'main') {
        showSkeletonLoaders();
    }
    
    try {
        const response = await fetch('/api/zones');
        if (!response.ok) {
            // 503 means the server is up but the hub is not reachable. Say that,
            // instead of a generic failure that looks like an app bug.
            if (response.status === 503) {
                const err = new Error(
                    'Cannot reach the Nobø hub, so no zones could be loaded. ' +
                    'Check the hub serial number and IP address under Devices.'
                );
                err.hubDown = true;
                throw err;
            }
            throw new Error('Failed to fetch zones');
        }
        
        const data = await response.json();
        zones = data.zones || data; // Handle both {zones: []} and [] formats
        
        if (currentPage === 'main') {
            renderZoneList();
        } else if (currentPage === 'zoneDetail' && currentZoneDetail) {
            renderZoneDetail(currentZoneDetail);
        }
        
        updateLastUpdated();
    } catch (error) {
        console.error('Error fetching zones:', error);
        showToast(error.hubDown ? error.message : 'Failed to fetch zones', error.hubDown ? 'warning' : 'error');
        if (error.hubDown) {
            // Keep the header honest even if the hub poll has not run yet.
            hubInfo = { ...hubInfo, connected: false };
            renderConnectionStatus();
        }
    }
}

async function setGlobalMode(mode) {
    globalMode = mode;
    updateGlobalModeButtons();
    
    try {
        const response = await fetch(`/api/global/override/${mode}`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to set global mode');
        }
        
        await fetchZones();
        showToast(`Global mode set to ${getModeLabel(mode)}`, 'success');
    } catch (error) {
        console.error('Error setting global mode:', error);
        showToast('Failed to set global mode', 'error');
    }
}

async function setZoneOverride(zoneId, mode) {
    try {
        const response = await fetch(`/api/zones/${zoneId}/override/${mode}`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to set zone override');
        }
        
        await fetchZones();
        if (mode === 'normal') {
            showToast('Override cancelled — returning to schedule', 'info');
        } else {
            showToast(`Zone set to ${getModeLabel(mode)}`, 'success');
        }
    } catch (error) {
        console.error('Error setting zone override:', error);
        showToast('Failed to set zone override', 'error');
    }
}

async function setZoneTemperature(zoneId, comfort, eco) {
    try {
        const body = {};
        if (comfort !== null) body.comfort = comfort;
        if (eco !== null) body.eco = eco;
        
        const response = await fetch(`/api/zones/${zoneId}/temperature`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update temperature');
        }
    } catch (error) {
        console.error('Error setting temperature:', error);
        showToast(error.message, 'error');
    }
}

// ===== UI Update Functions =====
// Tracks the browser <-> server WebSocket only. Whether the *hub* is reachable is
// a separate question, answered by hubInfo, and the two must not be conflated:
// the WebSocket can be perfectly healthy while the hub is unreachable.
let wsStatus = 'connecting';

function updateConnectionStatus(status) {
    if (status) {
        wsStatus = status;
    }
    renderConnectionStatus();
}

function renderConnectionStatus() {
    const statusDot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');
    if (!statusDot || !statusText) return;

    statusDot.className = 'status-dot';

    // No link to the server at all — nothing else can be trusted.
    if (wsStatus === 'connecting') {
        statusText.textContent = 'Connecting...';
        return;
    }
    if (wsStatus === 'disconnected') {
        statusDot.classList.add('disconnected');
        statusText.textContent = 'Disconnected';
        return;
    }
    if (wsStatus === 'error') {
        statusDot.classList.add('disconnected');
        statusText.textContent = 'Connection Error';
        return;
    }

    // Server is reachable — now report the hub.
    if (hubInfo.demo_mode) {
        statusDot.classList.add('demo');
        statusText.textContent = '🟡 Demo Mode';
    } else if (hubInfo.connected === false) {
        statusDot.classList.add('disconnected');
        statusText.textContent = '⚠️ Hub Unreachable';
    } else {
        statusDot.classList.add('connected');
        statusText.textContent = 'Connected';
    }
}

function updateHubInfo() {
    const hubNameEl = document.getElementById('hubName');
    const hubVersionEl = document.getElementById('hubVersion');
    
    if (hubInfo.name) {
        hubNameEl.textContent = hubInfo.name;
    }
    if (hubInfo.software_version) {
        hubVersionEl.textContent = `Version ${hubInfo.software_version}`;
    }

    renderConnectionStatus();
}

function updateLastUpdated() {
    const lastUpdatedEl = document.getElementById('lastUpdated');
    const now = new Date();
    lastUpdatedEl.textContent = now.toLocaleTimeString();
}

function updateGlobalModeButtons() {
    const btnHome = document.getElementById('btnHome');
    const btnAway = document.getElementById('btnAway');
    const btnEco = document.getElementById('btnEco');
    const btnComfort = document.getElementById('btnComfort');
    
    btnHome.classList.toggle('active', globalMode === 'home');
    btnAway.classList.toggle('active', globalMode === 'away');
    btnEco.classList.toggle('active', globalMode === 'eco');
    btnComfort.classList.toggle('active', globalMode === 'comfort');
}

// ===== Zone List Rendering =====
function renderZoneList() {
    const zoneList = document.getElementById('zoneList');
    
    if (zones.length === 0) {
        zoneList.innerHTML = '<div class="loading-message">No zones found</div>';
        return;
    }
    
    zoneList.innerHTML = zones.map(zone => createZoneListItem(zone)).join('');
}

function showSkeletonLoaders() {
    const zoneList = document.getElementById('zoneList');
    if (!zoneList) return;
    const skeletonCount = 3;
    zoneList.innerHTML = Array.from({ length: skeletonCount }, () => `
        <div class="skeleton-zone-item" aria-hidden="true">
            <div class="skeleton-top">
                <div class="skeleton-dot"></div>
                <div class="skeleton-text skeleton-name"></div>
            </div>
            <div class="skeleton-bottom">
                <div class="skeleton-temp"></div>
                <div class="skeleton-mode"></div>
            </div>
        </div>
    `).join('');
}

function createZoneListItem(zone) {
    const mode = zone.current_mode || 'normal';
    const icon = zone.icon || '';
    
    // Color-coded dot and label based on mode
    let dotColor = '#95a5a6'; // grey for off/normal
    let modeLabel = 'Schedule';
    let modeCssClass = '';
    const hasOverride = mode !== 'normal';
    
    if (mode === 'comfort') {
        dotColor = '#E74C3C';
        modeLabel = 'Comfort';
        modeCssClass = 'mode-comfort';
    } else if (mode === 'eco') {
        dotColor = '#27AE60';
        modeLabel = 'Eco';
        modeCssClass = 'mode-eco';
    } else if (mode === 'away') {
        dotColor = '#3498DB';
        modeLabel = 'Away';
        modeCssClass = 'mode-away';
    } else {
        // 'normal' — following schedule; reflect active schedule block in label and dot color
        const schedMode = zone.schedule_mode || 'comfort';
        if (schedMode === 'eco') {
            dotColor = '#27AE60';
            modeLabel = 'Schedule · Eco';
            modeCssClass = 'mode-eco';
        } else if (schedMode === 'away') {
            dotColor = '#3498DB';
            modeLabel = 'Schedule · Away';
            modeCssClass = 'mode-away';
        } else {
            dotColor = '#E74C3C';
            modeLabel = 'Schedule · Comfort';
            modeCssClass = 'mode-comfort';
        }
    }
    
    // Subtitle for grouped zones
    const subtitle = zone.rooms && zone.rooms.length > 1 
        ? `<div class="zone-list-subtitle">${zone.rooms.join(' · ')}</div>` 
        : '';
    
    const supportsTemp = zone.supports_temp_adjust || false;
    let setTemp = '—';
    let setTempAriaLabel = 'no temperature';
    if (supportsTemp) {
        let tempVal = null;
        if (mode === 'comfort') {
            tempVal = zone.comfort_temperature;
        } else if (mode === 'eco') {
            tempVal = zone.eco_temperature;
        } else if (mode === 'away') {
            tempVal = zone.away_temperature;
        } else {
            // 'normal' — use the active schedule block's target temperature
            const schedMode = zone.schedule_mode || 'comfort';
            if (schedMode === 'eco') {
                tempVal = zone.eco_temperature;
            } else if (schedMode === 'away') {
                tempVal = zone.away_temperature;
            } else {
                tempVal = zone.comfort_temperature;
            }
        }
        if (tempVal != null) {
            setTemp = '🎯 ' + tempVal.toFixed(1) + '°C';
            setTempAriaLabel = 'target temperature ' + tempVal.toFixed(1) + ' degrees Celsius';
        }
    }
    
    return `
        <div class="zone-list-item ripple-container" 
             onclick="navigateToZoneDetail('${zone.zone_id}')"
             role="button"
             tabindex="0"
             aria-label="${zone.name}, ${setTempAriaLabel}, ${modeLabel}"
             onkeydown="if(event.key==='Enter'||event.key===' ')navigateToZoneDetail('${zone.zone_id}')">
            <div class="zone-list-item-top">
                <div style="display:flex;align-items:center;gap:10px;flex:1;min-width:0;">
                    <div class="zone-list-dot${hasOverride ? ' active-override' : ''}" 
                         style="background-color: ${dotColor};" 
                         aria-hidden="true"></div>
                    <div class="zone-list-info">
                        <div class="zone-list-name">${icon} ${zone.name}</div>
                        ${subtitle}
                    </div>
                </div>
                <div class="zone-list-chevron" aria-hidden="true">›</div>
            </div>
            <div class="zone-list-bottom">
                <div class="zone-list-temp">${setTemp}</div>
                <div class="zone-list-mode${modeCssClass ? ' ' + modeCssClass : ''}">${modeLabel}</div>
            </div>
        </div>
    `;
}

// ===== Zone Detail Rendering =====
function renderZoneDetail(zoneId) {
    const zone = zones.find(z => z.zone_id === zoneId);
    if (!zone) {
        document.getElementById('zoneDetailContent').innerHTML = '<p>Zone not found</p>';
        return;
    }
    
    const mode = zone.current_mode || 'normal';
    const hasOverride = mode !== 'normal';
    const deviceType = zone.device_type || 'Unknown';
    const supportsTemp = zone.supports_temp_adjust || false;
    const hasManualDevices = zone.has_manual_devices || false;
    
    // Status
    const statusIcon = hasOverride ? '⚡' : '📅';
    const statusText = hasOverride 
        ? `Override: ${getModeLabel(mode)}`
        : 'Following Schedule';
    
    // Current temperature display
    const currentTempDisplay = supportsTemp && zone.current_temperature != null
        ? zone.current_temperature.toFixed(1) + '°C'
        : '—';
    const noSensorNotice = !supportsTemp ? `
        <div class="no-sensor-notice">
            <span class="icon">📵</span>
            <span>No built-in temperature sensor — temperature is read locally on the device</span>
        </div>
    ` : '';

    // Temperature controls section
    const mixedZoneNotice = hasManualDevices ? `
        <div class="manual-temp-notice">
            <span class="icon">🔧</span>
            <span>Note: Some devices in this zone (R80 RDC 700) require comfort &amp; eco temperatures to be adjusted locally on the device.</span>
        </div>
    ` : '';

    const tempSection = supportsTemp ? `
        <div class="detail-section">
            <h3>Temperatures</h3>
            <div class="temp-control">
                <span class="temp-label">Comfort temp:</span>
                <div class="temp-adjuster">
                    <button class="temp-btn" onclick="adjustTemperature('${zone.zone_id}', 'comfort', -0.5)">−</button>
                    <span class="temp-value">${zone.comfort_temperature != null ? zone.comfort_temperature.toFixed(1) : '--'}°C</span>
                    <button class="temp-btn" onclick="adjustTemperature('${zone.zone_id}', 'comfort', 0.5)">+</button>
                </div>
            </div>
            <div class="temp-control">
                <span class="temp-label">Eco temp:</span>
                <div class="temp-adjuster">
                    <button class="temp-btn" onclick="adjustTemperature('${zone.zone_id}', 'eco', -0.5)">−</button>
                    <span class="temp-value">${zone.eco_temperature != null ? zone.eco_temperature.toFixed(1) : '--'}°C</span>
                    <button class="temp-btn" onclick="adjustTemperature('${zone.zone_id}', 'eco', 0.5)">+</button>
                </div>
            </div>
            <div class="temp-control temp-locked">
                <span class="temp-label">Away temp:</span>
                <div class="temp-value-locked">
                    ${zone.away_temperature != null ? zone.away_temperature.toFixed(1) : '7.0'}°C 🔒 <span class="temp-lock-text">(set by Nobø)</span>
                </div>
            </div>
            ${mixedZoneNotice}
        </div>
    ` : `
        <div class="detail-section">
            <h3>Temperatures</h3>
            <div class="manual-temp-notice">
                <span class="icon">🔧</span>
                <span>Comfort & Eco temperatures are adjusted manually on the device</span>
            </div>
            <div class="temp-control temp-locked">
                <span class="temp-label">Away temp:</span>
                <div class="temp-value-locked">
                    ${zone.away_temperature != null ? zone.away_temperature.toFixed(1) : '7.0'}°C 🔒 <span class="temp-lock-text">(set by Nobø)</span>
                </div>
            </div>
        </div>
    `;
    
    // Override section
    const overrideSection = `
        <div class="detail-section">
            <h3>Override</h3>
            <div class="override-buttons">
                <button class="override-btn ${mode === 'comfort' ? 'active' : ''}" onclick="setZoneOverride('${zone.zone_id}', 'comfort')">🔥 Comfort</button>
                <button class="override-btn ${mode === 'eco' ? 'active' : ''}" onclick="setZoneOverride('${zone.zone_id}', 'eco')">🌿 Eco</button>
                <button class="override-btn ${mode === 'away' ? 'active' : ''}" onclick="setZoneOverride('${zone.zone_id}', 'away')">🏖️ Away</button>
            </div>
            ${hasOverride ? `<button class="cancel-override-btn" onclick="setZoneOverride('${zone.zone_id}', 'normal')">✖ Cancel Override — Return to Schedule</button>` : ''}
        </div>
    `;
    
    // Schedule section
    const scheduleSection = `
        <div class="detail-section">
            <h3>Schedule</h3>
            <button class="btn btn-primary" onclick="openScheduleModal('${zone.zone_id}')">
                <span class="icon">📅</span> Set Schedule
            </button>
        </div>
    `;
    
    // Rooms/Thermostats section
    const componentsHtml = (zone.components || []).map((serial, idx) => {
        const displaySerial = zone.components_display ? zone.components_display[idx] : serial;
        const roomName = zone.rooms && zone.rooms[idx] ? zone.rooms[idx] : `Device ${idx + 1}`;
        const componentName = zone.components_names && zone.components_names[idx] ? zone.components_names[idx] : roomName;
        const componentType = zone.components_types && zone.components_types[idx] ? zone.components_types[idx] : (zone.device_type || 'Unknown');
        const typeBadgeClass = componentType === 'NTB-2R' ? 'device-badge-blue' : 'device-badge-grey';
        const imgTag = deviceImageTag(serial, componentType, 'component-img');
        return `
            <div class="component-item">
                ${imgTag}
                <div class="component-info">
                    <div class="component-name-row">
                        <span id="deviceNameDisplay-${serial}" class="component-name">${escapeHtml(componentName)}</span>
                        <button class="btn btn-xs btn-icon" title="Edit name" onclick="startEditDeviceName('${serial}', '${zone.zone_id}', ${JSON.stringify(componentName).replace(/"/g, '&quot;')})">✏️</button>
                    </div>
                    <div id="deviceNameEditRow-${serial}" class="device-name-edit-row" style="display:none;">
                        <input type="text" id="deviceNameInput-${serial}" class="device-name-input" value="${escapeHtml(componentName)}" maxlength="64">
                        <button class="btn btn-xs btn-primary" onclick="saveDeviceName('${serial}', '${zone.zone_id}')">Save</button>
                        <button class="btn btn-xs btn-secondary" onclick="cancelEditDeviceName('${serial}', ${JSON.stringify(componentName).replace(/"/g, '&quot;')})">Cancel</button>
                    </div>
                    <span class="component-type-badge ${typeBadgeClass}">${componentType}</span>
                    <span class="component-serial">${displaySerial}</span>
                    <div class="device-actions">
                        <button class="btn btn-sm" onclick="replaceDevice('${serial}', '${zone.zone_id}')">Replace</button>
                        <button class="btn btn-sm" onclick="moveDevice('${serial}', '${zone.zone_id}')">Move</button>
                        <button class="btn btn-sm btn-danger" onclick="removeDevice('${serial}', '${zone.zone_id}')">Remove</button>
                    </div>
                </div>
            </div>
        `;
    }).join('');
    
    const roomsSection = `
        <div class="detail-section">
            <h3>Rooms / Thermostats</h3>
            <div class="components-list">
                ${componentsHtml || '<p>No devices assigned</p>'}
            </div>
            <div id="inlineAddDeviceForm-${zone.zone_id}" class="inline-add-device-form" style="display:none;">
                <div class="form-group">
                    <label for="inlineDeviceSerial-${zone.zone_id}">Serial Number:</label>
                    <input type="text" id="inlineDeviceSerial-${zone.zone_id}"
                           placeholder="210 000 016 247" maxlength="15"
                           oninput="formatSerialInput(this); detectInlineDeviceModel('${zone.zone_id}')">
                    <span id="inlineDetectedModel-${zone.zone_id}" class="detected-model"></span>
                </div>
                <div class="form-group">
                    <label for="inlineDeviceName-${zone.zone_id}">Device Name:</label>
                    <input type="text" id="inlineDeviceName-${zone.zone_id}"
                           placeholder="e.g. Living Room Heater" maxlength="64">
                </div>
                <div class="form-actions">
                    <button class="btn btn-primary" onclick="addDeviceToZone('${zone.zone_id}')">
                        <span class="icon">➕</span> Add
                    </button>
                    <button class="btn btn-secondary" onclick="closeInlineAddDevice('${zone.zone_id}')">Cancel</button>
                </div>
            </div>
            <button class="btn btn-secondary" id="openInlineAddDeviceBtn-${zone.zone_id}"
                    onclick="openInlineAddDevice('${zone.zone_id}')" style="margin-top:12px;">
                <span class="icon">➕</span> Add Device
            </button>
        </div>
    `;
    
    const html = `
        <div class="zone-detail">
            <div class="zone-detail-header">
                <button class="back-btn" onclick="navigateBack()">← Back to Main</button>
            </div>
            
            <div class="zone-detail-title">
                <div id="zoneNameDisplay-${zone.zone_id}" class="zone-name-display">
                    <h2>${escapeHtml(zone.icon)} ${escapeHtml(zone.name)}</h2>
                    <button class="btn btn-sm btn-secondary zone-edit-btn" onclick="startEditZone('${zone.zone_id}')">✏️ Edit</button>
                </div>
                <div id="zoneEditForm-${zone.zone_id}" class="zone-edit-form" style="display:none;">
                    <div class="zone-edit-fields">
                        <div class="zone-edit-icon-picker">
                            <label>Icon:</label>
                            <input type="text" id="zoneEditIcon-${zone.zone_id}" class="zone-edit-icon-input" value="${escapeHtml(zone.icon)}" maxlength="8" placeholder="🏠">
                            <div class="icon-picker-options">
                                ${['🛁','🚪','🛏️','🍳','🛋️','💻','🏠','🔥','❄️','🏖️','🌿','📡'].map(e => `<button class="icon-option" onclick="selectZoneIcon('${zone.zone_id}','${e}')">${e}</button>`).join('')}
                            </div>
                        </div>
                        <div class="zone-edit-name">
                            <label>Name:</label>
                            <input type="text" id="zoneEditName-${zone.zone_id}" class="zone-edit-name-input" value="${escapeHtml(zone.name)}" maxlength="64" placeholder="Zone name">
                        </div>
                    </div>
                    <div class="form-actions">
                        <button class="btn btn-primary" onclick="saveZoneEdit('${zone.zone_id}')">💾 Save</button>
                        <button class="btn btn-secondary" onclick="cancelEditZone('${zone.zone_id}')">Cancel</button>
                    </div>
                </div>
            </div>
            
            <div class="zone-detail-current">
                <span class="detail-currently">${currentTempDisplay}</span>
                <span class="detail-status">${statusIcon} ${statusText}</span>
            </div>
            
            ${noSensorNotice}
            ${tempSection}
            ${overrideSection}
            ${scheduleSection}
            ${roomsSection}

            <div class="detail-section zone-danger-section">
                <button class="btn btn-danger" onclick="deleteZone('${zone.zone_id}')">🗑️ Delete Zone</button>
            </div>
        </div>
    `;
    
    document.getElementById('zoneDetailContent').innerHTML = html;
}

function getModeLabel(mode) {
    const labels = {
        'comfort': 'Comfort',
        'eco': 'Eco',
        'away': 'Away',
        'normal': 'Schedule'
    };
    return labels[mode] || 'Unknown';
}

// ===== Temperature Adjustment =====
function adjustTemperature(zoneId, tempType, delta) {
    const zone = zones.find(z => z.zone_id === zoneId);
    if (!zone) return;
    
    if (!zone.supports_temp_adjust) {
        showToast(`Temperature cannot be adjusted remotely for ${zone.device_type} devices`, 'warning');
        return;
    }
    
    let currentTemp = tempType === 'comfort' ? zone.comfort_temperature : zone.eco_temperature;
    let newTemp = currentTemp + delta;
    
    newTemp = Math.max(7, Math.min(30, newTemp));
    
    if (tempType === 'comfort') {
        zone.comfort_temperature = newTemp;
    } else {
        zone.eco_temperature = newTemp;
    }
    
    // Re-render zone detail
    if (currentPage === 'zoneDetail') {
        renderZoneDetail(zoneId);
    }
    
    const timerKey = `${zoneId}_${tempType}`;
    if (temperatureTimers[timerKey]) {
        clearTimeout(temperatureTimers[timerKey]);
    }
    
    temperatureTimers[timerKey] = setTimeout(() => {
        console.log(`Sending temperature update for zone ${zoneId}, ${tempType}: ${newTemp}`);
        
        if (tempType === 'comfort') {
            setZoneTemperature(zoneId, newTemp, null);
        } else {
            setZoneTemperature(zoneId, null, newTemp);
        }
        
        delete temperatureTimers[timerKey];
    }, 500);
}

// ===== Schedule Modal =====
function openScheduleModal(zoneId) {
    currentScheduleZone = zoneId;
    const zone = zones.find(z => z.zone_id === zoneId);
    
    if (zone) {
        document.getElementById('scheduleModalZoneName').textContent = zone.name;
    }
    
    document.getElementById('scheduleModal').classList.add('show');
    loadScheduleFromAPI();
}

function closeScheduleModal() {
    document.getElementById('scheduleModal').classList.remove('show');
    currentScheduleZone = null;
}

async function loadScheduleFromAPI() {
    if (!currentScheduleZone) return;
    
    try {
        const response = await fetch(`/api/zones/${currentScheduleZone}/schedule`);
        if (!response.ok) {
            throw new Error('Failed to load schedule');
        }
        
        const data = await response.json();
        scheduleData = data.schedule || {};
        renderSharedScheduleNotice(data);
        renderSchedule();
    } catch (error) {
        console.error('Error loading schedule:', error);
        showToast('Failed to load schedule', 'error');
        // Render with default sample data
        renderSchedule();
    }
}

// A hub week profile is a shared object: several zones can point at the same
// one, and every zone starts on the factory default. The app gives a zone its
// own copy the first time its schedule is edited, so no other room is changed
// by accident — but the user should know that is about to happen.
function renderSharedScheduleNotice(data) {
    const container = document.getElementById('scheduleDays');
    if (!container || !container.parentElement) return;

    let notice = document.getElementById('sharedScheduleNotice');
    const shared = (data && data.shared_with_zones) || [];

    if (!shared.length) {
        if (notice) notice.remove();
        return;
    }

    if (!notice) {
        notice = document.createElement('div');
        notice.id = 'sharedScheduleNotice';
        notice.className = 'shared-schedule-notice';
        container.parentElement.insertBefore(notice, container);
    }

    const others = shared.map(escapeHtml).join(', ');
    notice.innerHTML = `<span class="icon">ℹ️</span> This schedule
        (<strong>${escapeHtml(data.week_profile_name || 'shared')}</strong>) is also used by
        ${others}. Saving changes here gives this zone its own copy, so the other
        zones keep the schedule they have now.`;
}

// ===== Schedule Helpers =====

function timeToMinutes(timeStr) {
    const [hours, minutes] = timeStr.split(':').map(Number);
    return hours * 60 + minutes;
}

function minutesToTime(minutes) {
    if (minutes >= 24 * 60) return '24:00';
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

function generateTimeOptions() {
    const options = [];
    for (let h = 0; h < 24; h++) {
        options.push(`${String(h).padStart(2, '0')}:00`);
        options.push(`${String(h).padStart(2, '0')}:30`);
    }
    options.push('24:00');
    return options;
}

function fillGaps(day) {
    let blocks = (scheduleData[day] || []).slice();
    blocks.sort((a, b) => timeToMinutes(a.start) - timeToMinutes(b.start));

    const endOfDay = 24 * 60;
    const filled = [];
    let cursor = 0;

    for (const block of blocks) {
        const start = timeToMinutes(block.start);
        const end = timeToMinutes(block.end);
        if (start > cursor) {
            filled.push({ start: minutesToTime(cursor), end: minutesToTime(start), mode: 'eco' });
        }
        if (end > cursor) {
            // Use Math.max defensively in case blocks were entered with overlapping times
            filled.push({ start: minutesToTime(Math.max(start, cursor)), end: minutesToTime(end), mode: block.mode });
            cursor = end;
        }
    }
    if (cursor < endOfDay) {
        filled.push({ start: minutesToTime(cursor), end: '24:00', mode: 'eco' });
    }

    // Merge adjacent blocks with the same mode
    const merged = [];
    for (const block of filled) {
        if (merged.length > 0 &&
            merged[merged.length - 1].mode === block.mode &&
            merged[merged.length - 1].end === block.start) {
            merged[merged.length - 1] = { ...merged[merged.length - 1], end: block.end };
        } else {
            merged.push({ ...block });
        }
    }
    scheduleData[day] = merged;
}

function generateTimeSelectHtml(id, selectedValue, excludeFirst, excludeLast) {
    const times = generateTimeOptions().filter(t =>
        !(excludeFirst && t === '00:00') && !(excludeLast && t === '24:00')
    );
    return `<select id="${id}" class="time-select">${
        times.map(t => `<option value="${t}"${t === selectedValue ? ' selected' : ''}>${t}</option>`).join('')
    }</select>`;
}

// The four states a hub week profile can hold. "Off" turns the heating off
// entirely for that period — it is a schedule state only, and is not available
// as a manual override.
const SCHEDULE_MODES = ['comfort', 'eco', 'away', 'off'];
const SCHEDULE_MODE_ICONS = { comfort: '🔥', eco: '🌿', away: '🏖️', off: '⏻' };

function getScheduleModeIcon(mode) {
    return SCHEDULE_MODE_ICONS[mode] || '🔥';
}

function getModeButtonsHtml(containerId, selectedMode) {
    return SCHEDULE_MODES.map(mode => {
        const icon = getScheduleModeIcon(mode);
        const label = mode.charAt(0).toUpperCase() + mode.slice(1);
        return `<button type="button" class="mode-btn mode-btn-${mode}${selectedMode === mode ? ' active' : ''}"
            data-mode="${mode}" onclick="selectMode('${containerId}','${mode}')">${icon} ${label}</button>`;
    }).join('');
}

function buildAddFormHtml(day) {
    const startSelect = generateTimeSelectHtml(`formStart-${day}`, '00:00', false, true);
    const endSelect = generateTimeSelectHtml(`formEnd-${day}`, '24:00', true, false);
    const modeId = `formMode-${day}`;
    return `<div class="block-edit-form">
        <div class="block-edit-form-title">➕ Add Time Block</div>
        <div class="block-edit-fields">
            <label class="field-label">Start ${startSelect}</label>
            <label class="field-label">End ${endSelect}</label>
            <div class="field-label">Mode
                <div class="mode-selector" id="${modeId}" data-selected="comfort">
                    ${getModeButtonsHtml(modeId, 'comfort')}
                </div>
            </div>
        </div>
        <div class="block-edit-actions">
            <button class="btn btn-primary btn-sm" onclick="submitAddTimeBlock('${day}')">Add</button>
            <button class="btn btn-secondary btn-sm" onclick="closeAllForms()">Cancel</button>
        </div>
    </div>`;
}

function buildEditFormHtml(day, blockIndex, block, totalBlocks) {
    const startSelect = generateTimeSelectHtml(`editStart-${day}-${blockIndex}`, block.start, false, true);
    const endSelect = generateTimeSelectHtml(`editEnd-${day}-${blockIndex}`, block.end, true, false);
    const modeId = `editMode-${day}-${blockIndex}`;
    const canDelete = totalBlocks > 1;
    return `<div class="block-edit-form editing">
        <div class="block-edit-form-title">✏️ Edit Time Block</div>
        <div class="block-edit-fields">
            <label class="field-label">Start ${startSelect}</label>
            <label class="field-label">End ${endSelect}</label>
            <div class="field-label">Mode
                <div class="mode-selector" id="${modeId}" data-selected="${block.mode}">
                    ${getModeButtonsHtml(modeId, block.mode)}
                </div>
            </div>
        </div>
        <div class="block-edit-actions">
            <button class="btn btn-primary btn-sm" onclick="submitEditTimeBlock('${day}',${blockIndex})">Save</button>
            ${canDelete ? `<button class="btn btn-danger btn-sm" onclick="deleteTimeBlock('${day}',${blockIndex})">🗑 Delete</button>` : ''}
            <button class="btn btn-secondary btn-sm" onclick="closeAllForms()">Cancel</button>
        </div>
    </div>`;
}

function buildCopyDayPopoverHtml(sourceDay, days, dayNames) {
    const checkboxes = days
        .filter(d => d !== sourceDay)
        .map(d => {
            const name = dayNames[days.indexOf(d)];
            return `<label class="copy-day-check"><input type="checkbox" class="copy-to-check" value="${d}"> ${name}</label>`;
        }).join('');
    return `<div class="copy-day-popover" onclick="event.stopPropagation()">
        <div class="copy-quick-select">
            <button class="btn btn-xs" onclick="selectCopyDayGroup('weekdays','${sourceDay}')">Mon–Fri</button>
            <button class="btn btn-xs" onclick="selectCopyDayGroup('weekend','${sourceDay}')">Sat–Sun</button>
            <button class="btn btn-xs" onclick="selectCopyDayGroup('all','${sourceDay}')">All Days</button>
        </div>
        <div class="copy-day-checks">${checkboxes}</div>
        <div class="copy-day-footer">
            <button class="btn btn-primary btn-sm" onclick="confirmCopyDay('${sourceDay}')">Copy</button>
            <button class="btn btn-secondary btn-sm" onclick="closeCopyDayPopover()">Cancel</button>
        </div>
    </div>`;
}

function renderSchedule() {
    const scheduleDays = document.getElementById('scheduleDays');
    const days = SCHEDULE_DAYS;
    const dayNames = SCHEDULE_DAY_NAMES;

    // Default schedule if no data loaded
    if (!scheduleData || Object.keys(scheduleData).length === 0) {
        scheduleData = {};
        days.forEach(day => {
            scheduleData[day] = [
                { start: '00:00', end: '08:00', mode: 'comfort' },
                { start: '08:00', end: '21:00', mode: 'eco' },
                { start: '21:00', end: '24:00', mode: 'comfort' }
            ];
        });
    }

    scheduleDays.innerHTML = days.map((day, index) => {
        const blocks = scheduleData[day] || [];

        const blockHtml = blocks.map((block, bi) => {
            const duration = timeToMinutes(block.end) - timeToMinutes(block.start);
            const widthPercent = (duration / (24 * 60)) * 100;
            const modeIcon = getScheduleModeIcon(block.mode);
            const modeClass = `timeline-block-${block.mode}`;
            const isEditing = activeFormState && activeFormState.type === 'edit' &&
                activeFormState.day === day && activeFormState.blockIndex === bi;
            const modeLabel = block.mode.charAt(0).toUpperCase() + block.mode.slice(1);
            return `<div class="timeline-block ${modeClass}${isEditing ? ' editing' : ''}"
                style="width:${widthPercent.toFixed(2)}%"
                onclick="openEditTimeBlock('${day}',${bi})"
                title="${modeLabel}: ${block.start}–${block.end}">
                <span class="timeline-icon">${modeIcon}</span>
                <span class="timeline-label">${modeLabel}</span>
                <span class="timeline-time">${block.start}–${block.end}</span>
            </div>`;
        }).join('');

        let formHtml = '';
        if (activeFormState && activeFormState.day === day) {
            if (activeFormState.type === 'add') {
                formHtml = buildAddFormHtml(day);
            } else if (activeFormState.type === 'edit' && blocks[activeFormState.blockIndex]) {
                formHtml = buildEditFormHtml(day, activeFormState.blockIndex, blocks[activeFormState.blockIndex], blocks.length);
            }
        }

        const copyPopoverHtml = copyDayPopoverDay === day
            ? buildCopyDayPopoverHtml(day, days, dayNames)
            : '';

        const showAddBtn = !(activeFormState && activeFormState.type === 'add' && activeFormState.day === day);

        return `<div class="schedule-day" data-day="${day}">
            <div class="schedule-day-header">
                <div class="day-name">${dayNames[index]}</div>
                <div class="copy-day-wrapper">
                    <button class="btn btn-sm" onclick="copyDay('${day}')">Copy to ▼</button>
                    ${copyPopoverHtml}
                </div>
            </div>
            <div class="day-timeline">
                ${blockHtml || '<div class="timeline-block timeline-block-eco" style="width:100%">No schedule</div>'}
            </div>
            ${formHtml}
            ${showAddBtn ? `<button class="btn btn-sm add-block-btn" onclick="addTimeBlock('${day}')">+ Add Block</button>` : ''}
        </div>`;
    }).join('');
}

// ===== Schedule Block Editing =====

function addTimeBlock(day) {
    closeCopyDayPopover();
    activeFormState = { type: 'add', day };
    renderSchedule();
    const dayEl = document.querySelector(`.schedule-day[data-day="${day}"]`);
    if (dayEl) dayEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function openEditTimeBlock(day, blockIndex) {
    closeCopyDayPopover();
    activeFormState = { type: 'edit', day, blockIndex };
    renderSchedule();
    const dayEl = document.querySelector(`.schedule-day[data-day="${day}"]`);
    if (dayEl) dayEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function closeAllForms() {
    activeFormState = null;
    closeCopyDayPopover();
    renderSchedule();
}

function selectMode(containerId, mode) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.setAttribute('data-selected', mode);
    container.querySelectorAll('.mode-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.mode === mode);
    });
}

function resolveOverlaps(blocks, startMin, endMin) {
    // Returns a new blocks array with any blocks overlapping [startMin, endMin] trimmed, split, or removed.
    const result = [];
    for (const block of blocks) {
        const bStart = timeToMinutes(block.start);
        const bEnd = timeToMinutes(block.end);
        if (startMin >= bEnd || endMin <= bStart) {
            // No overlap — keep as-is
            result.push(block);
        } else if (startMin <= bStart && endMin >= bEnd) {
            // New block completely covers this block — remove it
        } else if (startMin > bStart && endMin < bEnd) {
            // New block is in the middle — split into two
            result.push({ start: block.start, end: minutesToTime(startMin), mode: block.mode });
            result.push({ start: minutesToTime(endMin), end: block.end, mode: block.mode });
        } else if (startMin <= bStart) {
            // New block cuts the start — trim existing block's start
            result.push({ start: minutesToTime(endMin), end: block.end, mode: block.mode });
        } else {
            // New block cuts the end — trim existing block's end
            result.push({ start: block.start, end: minutesToTime(startMin), mode: block.mode });
        }
    }
    return result;
}

function submitAddTimeBlock(day) {
    const startEl = document.getElementById(`formStart-${day}`);
    const endEl = document.getElementById(`formEnd-${day}`);
    const modeEl = document.getElementById(`formMode-${day}`);
    if (!startEl || !endEl || !modeEl) return;

    const start = startEl.value;
    const end = endEl.value;
    const mode = modeEl.getAttribute('data-selected') || 'comfort';

    if (timeToMinutes(start) >= timeToMinutes(end)) {
        showToast('Start time must be before end time', 'error');
        return;
    }

    if (!scheduleData[day]) scheduleData[day] = [];
    const startMin = timeToMinutes(start);
    const endMin = timeToMinutes(end);
    scheduleData[day] = resolveOverlaps(scheduleData[day], startMin, endMin);
    scheduleData[day].push({ start, end, mode });
    scheduleData[day].sort((a, b) => timeToMinutes(a.start) - timeToMinutes(b.start));
    fillGaps(day);
    activeFormState = null;
    renderSchedule();
    showToast(`Added ${mode} block ${start}–${end}`, 'success');
}

function submitEditTimeBlock(day, blockIndex) {
    const startEl = document.getElementById(`editStart-${day}-${blockIndex}`);
    const endEl = document.getElementById(`editEnd-${day}-${blockIndex}`);
    const modeEl = document.getElementById(`editMode-${day}-${blockIndex}`);
    if (!startEl || !endEl || !modeEl) return;

    const start = startEl.value;
    const end = endEl.value;
    const mode = modeEl.getAttribute('data-selected') || 'eco';

    if (timeToMinutes(start) >= timeToMinutes(end)) {
        showToast('Start time must be before end time', 'error');
        return;
    }

    // Remove the block being edited, then resolve overlaps among remaining blocks
    const remaining = (scheduleData[day] || []).filter((_, i) => i !== blockIndex);
    const startMin = timeToMinutes(start);
    const endMin = timeToMinutes(end);
    scheduleData[day] = resolveOverlaps(remaining, startMin, endMin);
    scheduleData[day].push({ start, end, mode });
    scheduleData[day].sort((a, b) => timeToMinutes(a.start) - timeToMinutes(b.start));
    fillGaps(day);
    activeFormState = null;
    renderSchedule();
    showToast(`Updated block to ${mode} ${start}–${end}`, 'success');
}

function deleteTimeBlock(day, blockIndex) {
    const blocks = scheduleData[day] || [];
    if (blocks.length <= 1) {
        showToast('Cannot delete the only block. Change its mode instead.', 'warning');
        return;
    }
    const removed = blocks[blockIndex];
    scheduleData[day].splice(blockIndex, 1);
    fillGaps(day);
    activeFormState = null;
    renderSchedule();
    showToast(`Deleted ${removed.mode} block (${removed.start}–${removed.end})`, 'info');
}

// ===== Copy Day =====

function copyDay(day) {
    activeFormState = null;
    if (copyDayPopoverDay === day) {
        copyDayPopoverDay = null;
    } else {
        copyDayPopoverDay = day;
    }
    renderSchedule();
}

function closeCopyDayPopover() {
    if (copyDayPopoverDay !== null) {
        copyDayPopoverDay = null;
        renderSchedule();
    }
}

function selectCopyDayGroup(group, sourceDay) {
    const popover = document.querySelector('.copy-day-popover');
    if (!popover) return;
    const weekdays = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday'];
    const weekend = ['saturday', 'sunday'];
    const allDays = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
    let targetDays;
    if (group === 'weekdays') targetDays = weekdays.filter(d => d !== sourceDay);
    else if (group === 'weekend') targetDays = weekend.filter(d => d !== sourceDay);
    else targetDays = allDays.filter(d => d !== sourceDay);
    popover.querySelectorAll('.copy-to-check').forEach(cb => {
        cb.checked = targetDays.includes(cb.value);
    });
}

function confirmCopyDay(sourceDay) {
    const popover = document.querySelector('.copy-day-popover');
    if (!popover) return;
    const selected = Array.from(popover.querySelectorAll('.copy-to-check:checked')).map(cb => cb.value);
    if (selected.length === 0) {
        showToast('Select at least one day to copy to', 'warning');
        return;
    }
    const sourceBlocks = (scheduleData[sourceDay] || []).map(b => ({ ...b }));
    selected.forEach(targetDay => {
        scheduleData[targetDay] = sourceBlocks.map(b => ({ ...b }));
    });
    copyDayPopoverDay = null;
    renderSchedule();
    const names = selected.map(d => SCHEDULE_DAY_NAMES[SCHEDULE_DAYS.indexOf(d)]);
    showToast(`Copied schedule to: ${names.join(', ')}`, 'success');
}

async function saveSchedule() {
    if (!currentScheduleZone) return;
    
    try {
        const response = await fetch(`/api/zones/${currentScheduleZone}/schedule`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ schedule: scheduleData })
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save schedule');
        }
        
        showToast('Schedule saved successfully', 'success');
        setTimeout(() => {
            closeScheduleModal();
        }, 1500);
    } catch (error) {
        console.error('Error saving schedule:', error);
        showToast(error.message, 'error');
    }
}

// ===== Devices Page =====
function loadDevicesPage() {
    // Load current hub connection settings
    fetchHubConfig();

    // Populate zone selector
    const zoneSelect = document.getElementById('deviceZone');
    zoneSelect.innerHTML = '<option value="">Select zone...</option>';
    
    zones.forEach(zone => {
        const option = document.createElement('option');
        option.value = zone.zone_id;
        option.textContent = zone.name;
        zoneSelect.appendChild(option);
    });
    
    // Render devices list
    renderDevicesList();
}

function renderDevicesList() {
    const devicesList = document.getElementById('devicesList');
    
    let devicesHtml = '';
    zones.forEach(zone => {
        if (zone.components && zone.components.length > 0) {
            zone.components.forEach((serial, idx) => {
                const displaySerial = zone.components_display ? zone.components_display[idx] : serial;
                const roomName = zone.rooms && zone.rooms[idx] ? zone.rooms[idx] : 'Device';
                const componentName = zone.components_names && zone.components_names[idx] ? zone.components_names[idx] : roomName;
                const componentType = zone.components_types && zone.components_types[idx] ? zone.components_types[idx] : (zone.device_type || 'Unknown');
                const supportsTemp = zone.supports_temp_adjust || false;
                const mode = zone.current_mode || 'normal';
                const imgTag = deviceImageTag(serial, componentType, 'device-list-img');

                // Mode badge
                const modeBadgeClass = mode === 'comfort' ? 'mode-badge-comfort'
                    : mode === 'eco' ? 'mode-badge-eco'
                    : mode === 'away' ? 'mode-badge-away'
                    : 'mode-badge-normal';
                const modeLabel = getModeLabel(mode);
                const tempLabel = supportsTemp ? 'Remote adjust ✅' : 'Manual only 🔧';

                devicesHtml += `
                    <div class="device-item">
                        <div class="device-image-col">
                            ${imgTag}
                        </div>
                        <div class="device-info">
                            <div class="device-name-row">
                                <span id="deviceNameDisplay-${serial}" class="device-serial">${escapeHtml(componentName)}</span>
                                <button class="btn btn-xs btn-icon" title="Edit name" onclick="startEditDeviceName('${serial}', '${zone.zone_id}', ${JSON.stringify(componentName).replace(/"/g, '&quot;')})">✏️</button>
                            </div>
                            <div id="deviceNameEditRow-${serial}" class="device-name-edit-row" style="display:none;">
                                <input type="text" id="deviceNameInput-${serial}" class="device-name-input" value="${escapeHtml(componentName)}" maxlength="64">
                                <button class="btn btn-xs btn-primary" onclick="saveDeviceName('${serial}', '${zone.zone_id}')">Save</button>
                                <button class="btn btn-xs btn-secondary" onclick="cancelEditDeviceName('${serial}', ${JSON.stringify(componentName).replace(/"/g, '&quot;')})">Cancel</button>
                            </div>
                            <div class="device-serial-row">${displaySerial}</div>
                            <div class="device-type">${componentType}</div>
                            <div class="device-zone">→ ${escapeHtml(zone.name)}</div>
                            <div class="device-status-row">
                                <span class="device-mode-badge ${modeBadgeClass}">${modeLabel}</span>
                                <span class="device-temp-support">${tempLabel}</span>
                            </div>
                        </div>
                        <div class="device-actions">
                            <button class="btn btn-sm" onclick="replaceDevice('${serial}', '${zone.zone_id}')">Replace</button>
                            <button class="btn btn-sm" onclick="moveDevice('${serial}', '${zone.zone_id}')">Move</button>
                            <button class="btn btn-sm btn-danger" onclick="removeDevice('${serial}', '${zone.zone_id}')">Remove</button>
                        </div>
                    </div>
                `;
            });
        }
    });
    
    if (devicesHtml === '') {
        devicesHtml = '<div class="loading-message">No devices registered</div>';
    }
    
    devicesList.innerHTML = devicesHtml;
}

function formatSerialInput(input) {
    let value = input.value.replace(/\s/g, '');
    
    if (value.length > 12) {
        value = value.slice(0, 12);
    }
    
    let formatted = '';
    for (let i = 0; i < value.length; i++) {
        if (i > 0 && i % 3 === 0) {
            formatted += ' ';
        }
        formatted += value[i];
    }
    
    input.value = formatted;
}

// ===== Inline Add Device (Zone Detail) =====
function openInlineAddDevice(zoneId) {
    const form = document.getElementById(`inlineAddDeviceForm-${zoneId}`);
    const btn = document.getElementById(`openInlineAddDeviceBtn-${zoneId}`);
    if (form) form.style.display = 'block';
    if (btn) btn.style.display = 'none';
}

function closeInlineAddDevice(zoneId) {
    const form = document.getElementById(`inlineAddDeviceForm-${zoneId}`);
    const btn = document.getElementById(`openInlineAddDeviceBtn-${zoneId}`);
    const serialInput = document.getElementById(`inlineDeviceSerial-${zoneId}`);
    const nameInput = document.getElementById(`inlineDeviceName-${zoneId}`);
    const modelSpan = document.getElementById(`inlineDetectedModel-${zoneId}`);
    if (form) form.style.display = 'none';
    if (btn) btn.style.display = '';
    if (serialInput) serialInput.value = '';
    if (nameInput) nameInput.value = '';
    if (modelSpan) modelSpan.textContent = '';
}

function detectInlineDeviceModel(zoneId) {
    const serialInput = document.getElementById(`inlineDeviceSerial-${zoneId}`);
    const detectedModel = document.getElementById(`inlineDetectedModel-${zoneId}`);
    if (!serialInput || !detectedModel) return;
    const serial = serialInput.value.replace(/\s/g, '');
    if (serial.length >= 3) {
        const prefix = serial.slice(0, 3);
        const model = DEVICE_MODELS[prefix];
        if (model) {
            detectedModel.textContent = `→ Auto-detected: ${model.name} ✅`;
            detectedModel.style.color = '#27ae60';
        } else {
            detectedModel.textContent = '→ Unknown device model';
            detectedModel.style.color = '#e74c3c';
        }
    } else {
        detectedModel.textContent = '';
    }
}

async function addDeviceToZone(zoneId) {
    const serialInput = document.getElementById(`inlineDeviceSerial-${zoneId}`);
    const nameInput = document.getElementById(`inlineDeviceName-${zoneId}`);
    if (!serialInput) return;
    const serial = serialInput.value.replace(/\s/g, '');
    const name = nameInput ? nameInput.value.trim() : '';

    if (!serial || serial.length !== 12) {
        showToast('Please enter a valid 12-digit serial number', 'warning');
        return;
    }

    try {
        const response = await fetch('/api/devices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ serial, zone_id: zoneId, name: name || undefined })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to add device');
        }

        await fetchZones();
        showToast('Device added successfully', 'success');
        // Re-render zone detail to reflect the new device
        renderZoneDetail(zoneId);
    } catch (error) {
        console.error('Error adding device to zone:', error);
        showToast(error.message, 'error');
    }
}

// ===== Add Zone Modal =====
function openAddZoneModal() {
    const nameInput = document.getElementById('newZoneName');
    const iconInput = document.getElementById('newZoneIcon');
    if (nameInput) nameInput.value = '';
    if (iconInput) iconInput.value = '';
    document.getElementById('addZoneModal').classList.add('show');
}

function closeAddZoneModal() {
    document.getElementById('addZoneModal').classList.remove('show');
}

async function addZone() {
    const nameInput = document.getElementById('newZoneName');
    const iconInput = document.getElementById('newZoneIcon');
    const name = nameInput ? nameInput.value.trim() : '';
    const icon = iconInput ? iconInput.value.trim() : '';

    if (!name) {
        showToast('Please enter a zone name', 'warning');
        return;
    }

    try {
        const response = await fetch('/api/zones', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, icon })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to add zone');
        }

        closeAddZoneModal();
        await fetchZones();
        showToast(`Zone "${name}" created`, 'success');
    } catch (error) {
        console.error('Error adding zone:', error);
        showToast(error.message, 'error');
    }
}

function detectDeviceModel() {
    const serialInput = document.getElementById('deviceSerial');
    const detectedModel = document.getElementById('detectedModel');
    const serial = serialInput.value.replace(/\s/g, '');
    
    if (serial.length >= 3) {
        const prefix = serial.slice(0, 3);
        const model = DEVICE_MODELS[prefix];
        if (model) {
            detectedModel.textContent = `→ Auto-detected: ${model.name} ✅`;
            detectedModel.style.color = '#27ae60';
        } else {
            detectedModel.textContent = '→ Unknown device model';
            detectedModel.style.color = '#e74c3c';
        }
    } else {
        detectedModel.textContent = '';
    }
}

// ===== Device Discovery (receiver search) =====
// The hub can listen for devices that are in pairing mode and report their
// serials, so the user picks from a list instead of reading twelve digits off
// the back of a receiver. Demo mode has no radio, so these controls are gated
// on the `discover_devices` capability.

let deviceSearchPoll = null;

function setDeviceSearchRunning(running) {
    const startBtn = document.getElementById('deviceSearchStart');
    const stopBtn = document.getElementById('deviceSearchStop');
    if (startBtn) startBtn.style.display = running ? 'none' : '';
    if (stopBtn) stopBtn.style.display = running ? '' : 'none';
}

function stopDeviceSearchPolling() {
    if (deviceSearchPoll) {
        clearInterval(deviceSearchPoll);
        deviceSearchPoll = null;
    }
}

async function startDeviceSearch() {
    const status = document.getElementById('deviceSearchStatus');
    const results = document.getElementById('deviceSearchResults');
    if (results) results.innerHTML = '';
    if (status) status.textContent = 'Starting search...';

    try {
        const response = await fetch('/api/devices/search', { method: 'POST' });
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || 'Failed to start the search');
        }
        const data = await response.json();
        setDeviceSearchRunning(true);
        if (status) {
            status.textContent = data.message ||
                'Searching. Put each device into pairing mode now.';
        }
        await pollDeviceSearch();
        stopDeviceSearchPolling();
        deviceSearchPoll = setInterval(pollDeviceSearch, 2000);
    } catch (error) {
        console.error('Error starting device search:', error);
        if (status) status.textContent = '';
        setDeviceSearchRunning(false);
        showToast(error.message, 'error');
    }
}

async function pollDeviceSearch() {
    const status = document.getElementById('deviceSearchStatus');
    const results = document.getElementById('deviceSearchResults');

    try {
        const response = await fetch('/api/devices/search');
        if (!response.ok) throw new Error('Failed to read the search results');
        const data = await response.json();

        if (results) {
            results.innerHTML = (data.devices || []).map(device => {
                const label = device.device_type || 'Unknown device';
                const known = device.already_registered;
                return `<div class="device-search-result">
                    <div class="device-search-result-info">
                        <span class="device-search-result-serial">${escapeHtml(device.serial_display)}</span>
                        <span class="device-search-result-type">${escapeHtml(label)}</span>
                    </div>
                    ${known
                        ? '<span class="device-search-result-known">Already added</span>'
                        : `<button class="btn btn-secondary btn-sm"
                             onclick="useDiscoveredDevice('${device.serial}')">Use this</button>`}
                </div>`;
            }).join('');
        }

        if (!data.searching) {
            stopDeviceSearchPolling();
            setDeviceSearchRunning(false);
            if (status) {
                status.textContent = (data.devices || []).length
                    ? 'Search finished. Pick a device below.'
                    : 'Search finished. No devices found — check they are in pairing mode and try again.';
            }
        } else if (status && (data.devices || []).length) {
            status.textContent = `Searching... found ${data.devices.length}.`;
        }
    } catch (error) {
        console.error('Error polling device search:', error);
        stopDeviceSearchPolling();
        setDeviceSearchRunning(false);
        if (status) status.textContent = '';
    }
}

async function stopDeviceSearch() {
    stopDeviceSearchPolling();
    setDeviceSearchRunning(false);
    const status = document.getElementById('deviceSearchStatus');
    try {
        const response = await fetch('/api/devices/search', { method: 'DELETE' });
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || 'Failed to stop the search');
        }
        if (status) status.textContent = 'Search stopped.';
    } catch (error) {
        console.error('Error stopping device search:', error);
        showToast(error.message, 'error');
    }
}

function useDiscoveredDevice(serial) {
    const serialInput = document.getElementById('deviceSerial');
    if (!serialInput) return;
    serialInput.value = serial;
    formatSerialInput(serialInput);
    detectDeviceModel();
    serialInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
    showToast('Serial filled in. Pick a zone and press Add Device.', 'info');
}

async function addDevice() {
    const serialInput = document.getElementById('deviceSerial');
    const nameInput = document.getElementById('deviceName');
    const zoneSelect = document.getElementById('deviceZone');
    
    const serial = serialInput.value.replace(/\s/g, '');
    const name = nameInput ? nameInput.value.trim() : '';
    const zoneId = zoneSelect.value;
    
    if (!serial || serial.length !== 12) {
        showToast('Please enter a valid 12-digit serial number', 'warning');
        return;
    }
    
    if (!zoneId) {
        showToast('Please select a zone', 'warning');
        return;
    }
    
    try {
        const response = await fetch('/api/devices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ serial, zone_id: zoneId, name: name || undefined })
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to add device');
        }
        
        // Clear form
        serialInput.value = '';
        if (nameInput) nameInput.value = '';
        zoneSelect.value = '';
        document.getElementById('detectedModel').textContent = '';
        
        // Refresh zones and devices list
        await fetchZones();
        renderDevicesList();
        
        showToast('Device added successfully', 'success');
    } catch (error) {
        console.error('Error adding device:', error);
        showToast(error.message, 'error');
    }
}

async function replaceDevice(serial, zoneId) {
    const newSerial = prompt('Enter new device serial number (12 digits):');
    if (!newSerial) return;
    
    const cleanNewSerial = newSerial.replace(/\s/g, '');
    if (cleanNewSerial.length !== 12) {
        showToast('Serial number must be 12 digits', 'warning');
        return;
    }
    
    const cleanSerial = serial.replace(/\s/g, '');
    
    try {
        const response = await fetch(`/api/devices/${cleanSerial}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ new_serial: cleanNewSerial })
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to replace device');
        }
        
        await fetchZones();
        if (currentPage === 'zoneDetail' && currentZoneDetail) {
            renderZoneDetail(currentZoneDetail);
        } else {
            renderDevicesList();
        }
        
        showToast('Device replaced successfully', 'success');
    } catch (error) {
        console.error('Error replacing device:', error);
        showToast(error.message, 'error');
    }
}

async function removeDevice(serial, zoneId) {
    if (!confirm(`Are you sure you want to remove device ${serial}?`)) {
        return;
    }
    
    const cleanSerial = serial.replace(/\s/g, '');
    
    try {
        const response = await fetch(`/api/devices/${cleanSerial}`, {
            method: 'DELETE'
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to remove device');
        }
        
        await fetchZones();
        if (currentPage === 'zoneDetail' && currentZoneDetail) {
            renderZoneDetail(currentZoneDetail);
        } else {
            renderDevicesList();
        }
        
        showToast('Device removed successfully', 'success');
    } catch (error) {
        console.error('Error removing device:', error);
        showToast(error.message, 'error');
    }
}

// ===== Move Device =====
let moveDeviceSerial = null;
let moveDeviceCurrentZoneId = null;

function formatSerialForDisplay(serial) {
    const clean = serial.replace(/\s/g, '');
    if (clean.length === 12) {
        return `${clean.slice(0,3)} ${clean.slice(3,6)} ${clean.slice(6,9)} ${clean.slice(9,12)}`;
    }
    return clean;
}

function moveDevice(serial, currentZoneId) {
    moveDeviceSerial = serial.replace(/\s/g, '');
    moveDeviceCurrentZoneId = currentZoneId;

    // Populate zone selector (excluding current zone)
    const select = document.getElementById('moveDeviceZone');
    select.innerHTML = '<option value="">Select zone...</option>';
    zones.forEach(zone => {
        if (zone.zone_id !== currentZoneId) {
            const option = document.createElement('option');
            option.value = zone.zone_id;
            option.textContent = zone.name;
            select.appendChild(option);
        }
    });

    // Show device info
    const currentZone = zones.find(z => z.zone_id === currentZoneId);
    document.getElementById('moveDeviceInfo').textContent =
        `Moving device ${formatSerialForDisplay(moveDeviceSerial)} from "${currentZone ? currentZone.name : 'Unknown'}"`;

    document.getElementById('moveDeviceModal').classList.add('show');
}

function closeMoveDeviceModal() {
    document.getElementById('moveDeviceModal').classList.remove('show');
    moveDeviceSerial = null;
    moveDeviceCurrentZoneId = null;
}

async function confirmMoveDevice() {
    const newZoneId = document.getElementById('moveDeviceZone').value;
    if (!newZoneId) {
        showToast('Please select a target zone', 'warning');
        return;
    }

    try {
        const response = await fetch(`/api/devices/${moveDeviceSerial}/move`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ new_zone_id: newZoneId })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to move device');
        }

        closeMoveDeviceModal();
        await fetchZones();

        if (currentPage === 'zoneDetail' && currentZoneDetail) {
            renderZoneDetail(currentZoneDetail);
        } else {
            renderDevicesList();
        }

        showToast('Device moved successfully', 'success');
    } catch (error) {
        console.error('Error moving device:', error);
        showToast(error.message, 'error');
    }
}

// ===== Device Name Edit =====
function startEditDeviceName(serial, zoneId, currentName) {
    const displayEl = document.getElementById(`deviceNameDisplay-${serial}`);
    const editRow = document.getElementById(`deviceNameEditRow-${serial}`);
    const input = document.getElementById(`deviceNameInput-${serial}`);
    if (displayEl) displayEl.style.display = 'none';
    if (editRow) editRow.style.display = '';
    if (input) {
        input.value = currentName;
        input.focus();
        const onKey = (e) => {
            if (e.key === 'Enter') {
                input.removeEventListener('keydown', onKey);
                saveDeviceName(serial, zoneId);
            } else if (e.key === 'Escape') {
                input.removeEventListener('keydown', onKey);
                cancelEditDeviceName(serial, currentName);
            }
        };
        input.addEventListener('keydown', onKey);
    }
}

function cancelEditDeviceName(serial, originalName) {
    const displayEl = document.getElementById(`deviceNameDisplay-${serial}`);
    const editRow = document.getElementById(`deviceNameEditRow-${serial}`);
    const input = document.getElementById(`deviceNameInput-${serial}`);
    if (displayEl) displayEl.style.display = '';
    if (editRow) editRow.style.display = 'none';
    if (input) input.value = originalName;
}

async function saveDeviceName(serial, zoneId) {
    const input = document.getElementById(`deviceNameInput-${serial}`);
    if (!input) return;
    const name = input.value.trim();
    if (!name) {
        showToast('Device name cannot be empty', 'warning');
        return;
    }

    try {
        const response = await fetch(`/api/devices/${serial}/name`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update device name');
        }

        await fetchZones();
        // Re-render whichever view is active
        if (currentPage === 'zoneDetail' && currentZoneDetail) {
            renderZoneDetail(currentZoneDetail);
        } else {
            renderDevicesList();
        }
        showToast(`Device renamed to "${name}"`, 'success');
    } catch (error) {
        console.error('Error saving device name:', error);
        showToast(error.message, 'error');
    }
}

// ===== Zone Edit & Delete =====
function startEditZone(zoneId) {
    document.getElementById(`zoneNameDisplay-${zoneId}`).style.display = 'none';
    document.getElementById(`zoneEditForm-${zoneId}`).style.display = 'block';
}

function cancelEditZone(zoneId) {
    document.getElementById(`zoneEditForm-${zoneId}`).style.display = 'none';
    document.getElementById(`zoneNameDisplay-${zoneId}`).style.display = '';
}

function selectZoneIcon(zoneId, icon) {
    const input = document.getElementById(`zoneEditIcon-${zoneId}`);
    if (input) input.value = icon;
}

async function saveZoneEdit(zoneId) {
    const nameInput = document.getElementById(`zoneEditName-${zoneId}`);
    const iconInput = document.getElementById(`zoneEditIcon-${zoneId}`);
    const name = nameInput ? nameInput.value.trim() : '';
    const icon = iconInput ? iconInput.value.trim() : '';

    if (!name) {
        showToast('Zone name cannot be empty', 'warning');
        return;
    }

    try {
        const response = await fetch(`/api/zones/${zoneId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, icon })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to update zone');
        }

        await fetchZones();
        showToast(`Zone renamed to "${name}"`, 'success');
        renderZoneDetail(zoneId);
    } catch (error) {
        console.error('Error saving zone edit:', error);
        showToast(error.message, 'error');
    }
}

async function deleteZone(zoneId) {
    const zone = zones.find(z => z.zone_id === zoneId);
    const zoneName = zone ? zone.name : 'this zone';

    if (!confirm(`Are you sure you want to delete zone '${zoneName}'? This will unassign all devices in this zone.`)) {
        return;
    }

    try {
        const response = await fetch(`/api/zones/${zoneId}`, {
            method: 'DELETE'
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to delete zone');
        }

        await fetchZones();
        navigateBack();
        showToast(`Zone '${zoneName}' deleted`, 'success');
    } catch (error) {
        console.error('Error deleting zone:', error);
        showToast(error.message, 'error');
    }
}

// ===== Log Page =====
function setLogFilter(filter) {
    logFilter = filter;
    document.querySelectorAll('.log-filter-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.filter === filter);
    });
    renderLogEntries();
}

function toggleLogAutoRefresh() {
    const checkbox = document.getElementById('logAutoRefresh');
    if (!checkbox) return;
    if (checkbox.checked) {
        logAutoRefreshTimer = setInterval(loadLogPage, 3000);
    } else {
        if (logAutoRefreshTimer) {
            clearInterval(logAutoRefreshTimer);
            logAutoRefreshTimer = null;
        }
    }
}

async function loadLogPage() {
    try {
        const response = await fetch('/api/log');
        if (!response.ok) throw new Error('Failed to load log');
        const data = await response.json();
        logEntries = data.entries || [];
        renderLogEntries();
    } catch (error) {
        console.error('Error loading log:', error);
        const container = document.getElementById('logEntries');
        if (container) container.innerHTML = '<div class="loading-message">Failed to load log</div>';
    }
}

function renderLogEntries() {
    const container = document.getElementById('logEntries');
    if (!container) return;

    const filtered = logEntries.filter(entry => {
        if (logFilter === 'all') return true;
        if (logFilter === 'sent') return entry.direction === 'sent';
        if (logFilter === 'received') return entry.direction === 'received';
        if (logFilter === 'error') return entry.direction === 'error';
        return true;
    });

    if (filtered.length === 0) {
        container.innerHTML = '<div class="loading-message">No log entries</div>';
        return;
    }

    container.innerHTML = filtered.map(entry => {
        const dirClass = entry.direction === 'sent' ? 'log-sent'
            : entry.direction === 'received' ? 'log-received'
            : 'log-error';
        const arrow = entry.direction === 'sent' ? '→' : entry.direction === 'received' ? '←' : '✕';
        const dirLabel = entry.direction === 'sent' ? 'SENT' : entry.direction === 'received' ? 'RECV' : 'ERROR';
        const tsParts = entry.timestamp ? entry.timestamp.split('T') : [];
        const ts = tsParts.length >= 2 ? tsParts[1] : (entry.timestamp || '');
        const rawHtml = entry.command ? `
            <details class="log-raw">
                <summary>Raw command</summary>
                <code>${escapeHtml(entry.command)}</code>
            </details>
        ` : '';
        return `
            <div class="log-entry ${dirClass}">
                <div class="log-entry-main">
                    <span class="log-timestamp">${escapeHtml(ts)}</span>
                    <span class="log-arrow">${arrow}</span>
                    <span class="log-dir">${dirLabel}</span>
                    <span class="log-description">${escapeHtml(entry.description)}</span>
                </div>
                ${rawHtml}
            </div>
        `;
    }).join('');
}

async function clearLog() {
    try {
        await fetch('/api/log/clear', { method: 'POST' });
        logEntries = [];
        renderLogEntries();
        showToast('Log cleared', 'success');
    } catch (error) {
        showToast('Failed to clear log', 'error');
    }
}

// ===== Toast Notifications =====
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const icons = {
        success: '✅',
        error: '❌',
        warning: '⚠️',
        info: 'ℹ️'
    };

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.setAttribute('role', 'alert');
    toast.innerHTML = `
        <span class="toast-icon" aria-hidden="true">${icons[type] || icons.info}</span>
        <div class="toast-body">
            <span class="toast-message">${escapeHtml(message)}</span>
        </div>
        <button class="toast-close" onclick="dismissToast(this.parentElement)" aria-label="Dismiss notification">×</button>
    `;

    container.appendChild(toast);

    // Auto-dismiss after 4 seconds
    const timeout = setTimeout(() => {
        dismissToast(toast);
    }, 4000);

    // Store timeout so manual close can clear it
    toast._dismissTimeout = timeout;
}

function dismissToast(toast) {
    if (!toast || toast._dismissed) return;
    toast._dismissed = true;
    if (toast._dismissTimeout) {
        clearTimeout(toast._dismissTimeout);
    }
    toast.classList.add('toast-dismissing');
    toast.addEventListener('animationend', () => {
        toast.remove();
    }, { once: true });
    // Fallback removal
    setTimeout(() => { toast.remove(); }, 400);
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}


// ===== Hub Connection Settings =====
let hubConfigCurrent = null;
let hubConfigPending = null;

async function fetchHubConfig() {
    const summary = document.getElementById('hubConfigSummary');
    try {
        const response = await fetch('/api/hub/config');
        if (!response.ok) throw new Error('Request failed');
        const cfg = await response.json();
        hubConfigCurrent = cfg;

        const demoRadio = document.querySelector('input[name="hubMode"][value="demo"]');
        const realRadio = document.querySelector('input[name="hubMode"][value="real"]');
        if (demoRadio) demoRadio.checked = cfg.demo_mode;
        if (realRadio) realRadio.checked = !cfg.demo_mode;

        const serialInput = document.getElementById('hubSerial');
        const ipInput = document.getElementById('hubIp');
        if (serialInput) serialInput.value = cfg.serial_display || '';
        if (ipInput) ipInput.value = cfg.ip || '';

        if (summary) {
            if (cfg.demo_mode) {
                summary.innerHTML = '🧪 <strong>Demo mode</strong> — showing simulated zones. No hub is being contacted.';
            } else if (cfg.connected) {
                summary.innerHTML = `✅ <strong>Connected</strong> to hub ${escapeHtml(cfg.serial_display)} at ${escapeHtml(cfg.ip)}`;
            } else {
                summary.innerHTML = `⚠️ <strong>Not connected</strong> — trying hub ${escapeHtml(cfg.serial_display)} at ${escapeHtml(cfg.ip)}`;
            }
        }

        onHubModeChange();
    } catch (error) {
        console.error('Error fetching hub config:', error);
        if (summary) summary.textContent = 'Could not load the current hub settings.';
    }
}

function onHubModeChange() {
    const realSelected = document.querySelector('input[name="hubMode"][value="real"]')?.checked;
    const realFields = document.getElementById('hubRealFields');
    if (realFields) realFields.style.display = realSelected ? '' : 'none';
}

function showHubConfigMessage(text, kind) {
    const messageEl = document.getElementById('hubConfigMessage');
    if (!messageEl) return;
    messageEl.textContent = text;
    messageEl.className = `hub-config-message ${kind || ''}`.trim();
}

function openHubConfigConfirm() {
    const realSelected = document.querySelector('input[name="hubMode"][value="real"]')?.checked;
    const demoMode = !realSelected;
    const serialRaw = (document.getElementById('hubSerial')?.value || '').replace(/\s/g, '');
    const ip = (document.getElementById('hubIp')?.value || '').trim();
    const messageEl = document.getElementById('hubConfigMessage');
    if (messageEl) messageEl.textContent = '';

    // Validate before asking for confirmation
    if (!demoMode) {
        if (!/^\d{12}$/.test(serialRaw)) {
            showToast('Serial number must be exactly 12 digits', 'error');
            return;
        }
        if (!isValidIpv4(ip)) {
            showToast('Enter a valid IP address, for example 192.168.1.100', 'error');
            return;
        }
    }

    hubConfigPending = { demo_mode: demoMode, serial: serialRaw, ip: ip };

    const wasDemo = hubConfigCurrent ? hubConfigCurrent.demo_mode : null;
    let body;
    if (demoMode) {
        body = `
            <p>Switch to <strong>demo mode</strong>?</p>
            <p>The system will stop talking to your Nobø Eco Hub and show simulated
            zones instead. Your real heating will keep running on its own settings,
            but you will not be able to control it from here until you switch back.</p>
        `;
    } else {
        const changing = wasDemo === true
            ? '<p>This will leave <strong>demo mode</strong> and connect to your real heating system.</p>'
            : '';
        body = `
            <p>Connect to your Nobø Eco Hub?</p>
            ${changing}
            <p><strong>Serial:</strong> ${escapeHtml(formatSerialForDisplay(serialRaw))}<br>
               <strong>IP address:</strong> ${escapeHtml(ip)}</p>
            <p>The official Nobø app can stay connected — the hub keeps both of you
            in sync. It allows two connections over the local network at once, and
            this will use one of them.</p>
            <p>Commands you send from here will change your actual heating.</p>
        `;
    }

    const bodyEl = document.getElementById('hubConfigConfirmBody');
    if (bodyEl) {
        bodyEl.innerHTML = body +
            '<p class="hub-config-signout-note">You will be signed out so the app can ' +
            'reload cleanly with the new settings. Log in again to continue.</p>';
    }
    document.getElementById('hubConfigModal')?.classList.add('show');
}

function closeHubConfigModal() {
    document.getElementById('hubConfigModal')?.classList.remove('show');
    hubConfigPending = null;
}

function isValidIpv4(value) {
    const parts = String(value).split('.');
    if (parts.length !== 4) return false;
    return parts.every(p => /^\d{1,3}$/.test(p) && Number(p) >= 0 && Number(p) <= 255);
}

function formatSerialForDisplay(serial) {
    const clean = String(serial).replace(/\s/g, '');
    if (clean.length !== 12) return clean;
    return `${clean.slice(0, 3)} ${clean.slice(3, 6)} ${clean.slice(6, 9)} ${clean.slice(9, 12)}`;
}

async function confirmHubConfig() {
    if (!hubConfigPending) return;

    const payload = hubConfigPending;
    const btn = document.getElementById('hubConfigConfirmBtn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Applying…';
    }

    try {
        const response = await fetch('/api/hub/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await response.json().catch(() => ({}));

        if (!response.ok) {
            const detail = data.detail || 'Could not save the hub settings';
            showToast(typeof detail === 'string' ? detail : 'Could not save the hub settings', 'error');
            return;
        }

        closeHubConfigModal();

        // The server ends the session on a mode change, so this page is now
        // unauthenticated and holding data from the old source. Tell the user
        // what happened, then send them to a clean login.
        let message;
        if (data.warning) {
            message = data.warning + ' Signing you out…';
        } else if (data.demo_mode) {
            message = 'Demo mode enabled. Signing you out…';
        } else {
            message = 'Connected to your Nobø hub. Signing you out…';
        }
        showToast(message, data.warning ? 'warning' : 'success');

        showHubConfigMessage(message + ' Please log in again.', data.warning ? 'warning' : 'success');

        // Close the socket first so its reconnect timer cannot fire mid-redirect.
        if (ws) {
            try { ws.close(); } catch (e) { /* already closing */ }
        }
        if (reconnectInterval) {
            clearInterval(reconnectInterval);
            reconnectInterval = null;
        }

        setTimeout(() => {
            window.location.href = '/login';
        }, data.warning ? 3500 : 1800);
        return;
    } catch (error) {
        console.error('Error saving hub config:', error);
        showToast('Could not save the hub settings', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Apply Change';
        }
    }
}

// Backwards compatibility alias
function showError(message) {
    // Detect success messages by leading emoji
    if (message && message.startsWith('✅')) {
        showToast(message.replace('✅ ', '').replace('✅', ''), 'success');
    } else {
        showToast(message, 'error');
    }
}

// ===== Ripple Effect =====
function addRipple(event) {
    const element = event.currentTarget;
    if (!element.classList.contains('ripple-container')) return;

    const rect = element.getBoundingClientRect();
    const ripple = document.createElement('span');
    ripple.className = 'ripple';
    ripple.style.left = `${event.clientX - rect.left}px`;
    ripple.style.top = `${event.clientY - rect.top}px`;
    element.appendChild(ripple);
    ripple.addEventListener('animationend', () => ripple.remove(), { once: true });
}

// Attach ripple to dynamically created elements via delegation
document.addEventListener('click', (event) => {
    const target = event.target.closest('.ripple-container');
    if (target) {
        addRipple({ currentTarget: target, clientX: event.clientX, clientY: event.clientY });
    }
    // Close copy-day popover when clicking outside it
    if (copyDayPopoverDay !== null && !event.target.closest('.copy-day-wrapper')) {
        copyDayPopoverDay = null;
        renderSchedule();
    }
});

// ===== Export functions to global scope =====
window.setGlobalMode = setGlobalMode;
window.setZoneOverride = setZoneOverride;
window.adjustTemperature = adjustTemperature;
window.navigateToZoneDetail = navigateToZoneDetail;
window.navigateBack = navigateBack;
window.openScheduleModal = openScheduleModal;
window.closeScheduleModal = closeScheduleModal;
window.addTimeBlock = addTimeBlock;
window.openEditTimeBlock = openEditTimeBlock;
window.closeAllForms = closeAllForms;
window.selectMode = selectMode;
window.submitAddTimeBlock = submitAddTimeBlock;
window.submitEditTimeBlock = submitEditTimeBlock;
window.deleteTimeBlock = deleteTimeBlock;
window.copyDay = copyDay;
window.closeCopyDayPopover = closeCopyDayPopover;
window.selectCopyDayGroup = selectCopyDayGroup;
window.confirmCopyDay = confirmCopyDay;
window.saveSchedule = saveSchedule;
window.formatSerialInput = formatSerialInput;
window.detectDeviceModel = detectDeviceModel;
window.addDevice = addDevice;
window.replaceDevice = replaceDevice;
window.removeDevice = removeDevice;
window.moveDevice = moveDevice;
window.closeMoveDeviceModal = closeMoveDeviceModal;
window.confirmMoveDevice = confirmMoveDevice;
window.dismissToast = dismissToast;
window.openInlineAddDevice = openInlineAddDevice;
window.closeInlineAddDevice = closeInlineAddDevice;
window.detectInlineDeviceModel = detectInlineDeviceModel;
window.addDeviceToZone = addDeviceToZone;
window.startEditDeviceName = startEditDeviceName;
window.cancelEditDeviceName = cancelEditDeviceName;
window.saveDeviceName = saveDeviceName;
window.openAddZoneModal = openAddZoneModal;
window.closeAddZoneModal = closeAddZoneModal;
window.addZone = addZone;
window.startEditZone = startEditZone;
window.cancelEditZone = cancelEditZone;
window.selectZoneIcon = selectZoneIcon;
window.saveZoneEdit = saveZoneEdit;
window.deleteZone = deleteZone;
window.loadLogPage = loadLogPage;
window.setLogFilter = setLogFilter;
window.toggleLogAutoRefresh = toggleLogAutoRefresh;
window.clearLog = clearLog;

// ===== Away Schedule =====

/**
 * Format an ISO datetime string for display as DD.MM.YYYY HH:mm (24-hour, local time).
 */
function formatIsoForDisplay(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        if (isNaN(d.getTime())) return isoStr;
        const pad = n => String(n).padStart(2, '0');
        return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    } catch (_) {
        return isoStr;
    }
}

/**
 * Extract the date part (DD.MM.YYYY) from an ISO datetime string in local time.
 */
function isoToDatePart(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        if (isNaN(d.getTime())) return '';
        const pad = n => String(n).padStart(2, '0');
        return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`;
    } catch (_) { return ''; }
}

/**
 * Extract the time part (HH:mm) from an ISO datetime string in local time.
 */
function isoToTimePart(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        if (isNaN(d.getTime())) return '';
        const pad = n => String(n).padStart(2, '0');
        return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    } catch (_) { return ''; }
}

/**
 * Auto-format a date input value as DD.MM.YYYY while typing.
 * Strips non-digits, limits to 8 digits, inserts dots at positions 2 and 4.
 */
function normalizeDateDigitsToDDMMYYYY(value) {
    let digits = value.replace(/\D/g, '').slice(0, 8);
    let result = '';
    for (let i = 0; i < digits.length; i++) {
        if (i === 2 || i === 4) result += '.';
        result += digits[i];
    }
    return result;
}

/**
 * Auto-format a time input value as HH:mm while typing.
 * Strips non-digits, limits to 4 digits, inserts colon at position 2.
 */
function normalizeTimeDigitsToHHmm(value) {
    let digits = value.replace(/\D/g, '').slice(0, 4);
    let result = '';
    for (let i = 0; i < digits.length; i++) {
        if (i === 2) result += ':';
        result += digits[i];
    }
    return result;
}

/**
 * Parse separate DD.MM.YYYY and HH:mm strings into an ISO-8601 string.
 * Returns null if either input is missing or doesn't match the expected format.
 */
function parseDDMMYYYYHHmm(dateStr, timeStr) {
    if (!dateStr || !timeStr) return null;
    const dm = dateStr.match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
    const tm = timeStr.match(/^(\d{2}):(\d{2})$/);
    if (!dm || !tm) return null;
    const day = parseInt(dm[1], 10);
    const month = parseInt(dm[2], 10) - 1;
    const year = parseInt(dm[3], 10);
    const hour = parseInt(tm[1], 10);
    const minute = parseInt(tm[2], 10);
    if (month < 0 || month > 11 || day < 1 || day > 31 || hour > 23 || minute > 59) return null;
    const d = new Date(year, month, day, hour, minute, 0);
    if (isNaN(d.getTime())) return null;
    // Validate that the date components match what was parsed (catches e.g. 31 Feb)
    if (d.getFullYear() !== year || d.getMonth() !== month || d.getDate() !== day) return null;
    return d.toISOString();
}

/**
 * Validate and parse separate DD.MM.YYYY and HH:mm strings into an ISO-8601 string.
 * Returns { iso: string, error: null } on success, or { iso: null, error: string } on failure.
 */
function validateAndParseLocalDDMMYYYYHHmmToIso(dateStr, timeStr) {
    if (!dateStr || !dateStr.trim()) return { iso: null, error: 'Date is empty' };
    if (!timeStr || !timeStr.trim()) return { iso: null, error: 'Time is empty' };
    dateStr = dateStr.trim();
    timeStr = timeStr.trim();

    const dm = dateStr.match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
    if (!dm) return { iso: null, error: 'Date must be DD.MM.YYYY (got: ' + dateStr + ')' };

    const tm = timeStr.match(/^(\d{2}):(\d{2})$/);
    if (!tm) return { iso: null, error: 'Time must be HH:mm (got: ' + timeStr + ')' };

    const day = parseInt(dm[1], 10);
    const month = parseInt(dm[2], 10) - 1;
    const year = parseInt(dm[3], 10);
    const hour = parseInt(tm[1], 10);
    const minute = parseInt(tm[2], 10);

    if (hour > 23 || minute > 59) return { iso: null, error: 'Invalid time: hours 0-23, minutes 0-59' };
    if (month < 0 || month > 11) return { iso: null, error: 'Invalid month: must be 01-12' };
    if (day < 1 || day > 31) return { iso: null, error: 'Invalid day: must be 01-31' };

    const d = new Date(year, month, day, hour, minute, 0);
    if (isNaN(d.getTime())) return { iso: null, error: 'Invalid date' };
    if (d.getFullYear() !== year || d.getMonth() !== month || d.getDate() !== day) {
        return { iso: null, error: 'Invalid date (e.g. Feb 30 does not exist)' };
    }
    return { iso: d.toISOString(), error: null };
}


function renderAwayScheduleStatus(data) {
    const statusEl = document.getElementById('scheduleAwayStatus');
    const clearBtn = document.getElementById('btnClearSchedule');
    const startDateInput = document.getElementById('scheduleStartDate');
    const startTimeInput = document.getElementById('scheduleStartTime');
    const endDateInput = document.getElementById('scheduleEndDate');
    const endTimeInput = document.getElementById('scheduleEndTime');

    if (!statusEl) return;

    if (data.currently_active) {
        statusEl.innerHTML = `✅ <strong>Schedule active</strong> — Away until ${escapeHtml(formatIsoForDisplay(data.end_at))}`;
        statusEl.style.color = 'var(--color-warning, #f59e0b)';
        // Badge on Away button
        const btnAway = document.getElementById('btnAway');
        if (btnAway && !btnAway.querySelector('.schedule-badge')) {
            const badge = document.createElement('span');
            badge.className = 'schedule-badge';
            badge.textContent = '📅';
            badge.title = 'Controlled by schedule';
            badge.style.cssText = 'font-size:0.7em;margin-left:4px;vertical-align:super;';
            btnAway.appendChild(badge);
        }
    } else if (data.enabled && data.start_at) {
        statusEl.innerHTML = `⏰ Scheduled: Away from ${escapeHtml(formatIsoForDisplay(data.start_at))} to ${escapeHtml(formatIsoForDisplay(data.end_at))}`;
        statusEl.style.color = '';
        // Remove badge
        const badge = document.querySelector('#btnAway .schedule-badge');
        if (badge) badge.remove();
    } else if (data.start_at) {
        statusEl.innerHTML = `⏸ Disabled: Away from ${escapeHtml(formatIsoForDisplay(data.start_at))} to ${escapeHtml(formatIsoForDisplay(data.end_at))}`;
        statusEl.style.color = 'var(--color-text-secondary)';
        const badge = document.querySelector('#btnAway .schedule-badge');
        if (badge) badge.remove();
    } else {
        statusEl.textContent = 'No schedule set';
        statusEl.style.color = 'var(--color-text-secondary)';
        const badge = document.querySelector('#btnAway .schedule-badge');
        if (badge) badge.remove();
    }

    // Show/hide clear button
    if (clearBtn) {
        clearBtn.style.display = (data.enabled || data.start_at) ? '' : 'none';
    }

    // Populate inputs with current schedule values (local time, DD.MM.YYYY HH:mm)
    if (startDateInput) startDateInput.value = data.start_at ? isoToDatePart(data.start_at) : '';
    if (startTimeInput) startTimeInput.value = data.start_at ? isoToTimePart(data.start_at) : '';
    if (endDateInput) endDateInput.value = data.end_at ? isoToDatePart(data.end_at) : '';
    if (endTimeInput) endTimeInput.value = data.end_at ? isoToTimePart(data.end_at) : '';
}

async function fetchAwaySchedule() {
    try {
        const r = await fetch('/api/global-mode/away-schedule');
        if (!r.ok) return;
        const data = await r.json();
        renderAwayScheduleStatus(data);
        // Auto-open the panel if a schedule is set (enabled or has dates)
        const panel = document.getElementById('scheduleAwayPanel');
        if (panel && (data.enabled || data.start_at)) {
            panel.setAttribute('open', '');
        }
    } catch (e) {
        console.error('fetchAwaySchedule error:', e);
    }
}

async function saveAwaySchedule() {
    const startDateEl = document.getElementById('scheduleStartDate');
    const startTimeEl = document.getElementById('scheduleStartTime');
    const endDateEl = document.getElementById('scheduleEndDate');
    const endTimeEl = document.getElementById('scheduleEndTime');

    const startDate = startDateEl ? startDateEl.value.trim() : '';
    const startTime = startTimeEl ? startTimeEl.value.trim() : '';
    const endDate = endDateEl ? endDateEl.value.trim() : '';
    const endTime = endTimeEl ? endTimeEl.value.trim() : '';

    console.log('saveAwaySchedule values:', { startDate, startTime, endDate, endTime });

    if (!startDate || !startTime || !endDate || !endTime) {
        showToast('Please set both start and end date/time (DD.MM.YYYY HH:mm)', 'error');
        return;
    }

    const startResult = validateAndParseLocalDDMMYYYYHHmmToIso(startDate, startTime);
    if (!startResult.iso) {
        showToast('Invalid start: ' + startResult.error, 'error');
        return;
    }

    const endResult = validateAndParseLocalDDMMYYYYHHmmToIso(endDate, endTime);
    if (!endResult.iso) {
        showToast('Invalid end: ' + endResult.error, 'error');
        return;
    }

    try {
        const r = await fetch('/api/global-mode/away-schedule', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: true, start_at: startResult.iso, end_at: endResult.iso }),
        });
        if (!r.ok) {
            const err = await r.json();
            throw new Error(err.detail || 'Failed to save schedule');
        }
        const data = await r.json();
        renderAwayScheduleStatus(data);
        showToast('Away schedule saved', 'success');
        if (data.currently_active) {
            globalMode = 'away';
            updateGlobalModeButtons();
            await fetchZones();
        }
    } catch (e) {
        console.error('saveAwaySchedule error:', e);
        showToast(e.message, 'error');
    }
}

async function clearAwaySchedule() {
    try {
        const r = await fetch('/api/global-mode/away-schedule', { method: 'DELETE' });
        if (!r.ok) {
            const err = await r.json();
            throw new Error(err.detail || 'Failed to clear schedule');
        }
        renderAwayScheduleStatus({ enabled: false, start_at: null, end_at: null, currently_active: false });
        // Clear inputs
        ['scheduleStartDate', 'scheduleStartTime', 'scheduleEndDate', 'scheduleEndTime'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.value = '';
        });
        showToast('Away schedule cleared', 'info');
        await fetchZones();
    } catch (e) {
        console.error('clearAwaySchedule error:', e);
        showToast(e.message, 'error');
    }
}

window.saveAwaySchedule = saveAwaySchedule;
window.clearAwaySchedule = clearAwaySchedule;
window.fetchAwaySchedule = fetchAwaySchedule;
