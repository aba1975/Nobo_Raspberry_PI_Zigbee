/*
 * Shared plumbing for the three UI design concepts.
 *
 * This file deliberately contains NO layout and NO visual design. It is the
 * data layer the three prototypes have in common: talking to the existing
 * API, mapping a serial number to a device picture, and deriving the handful
 * of display values the API does not return directly.
 *
 * Everything here is read/write against the EXISTING endpoints. No endpoint,
 * payload or backend behaviour is changed by the design exploration.
 */

const Nobo = (() => {

  /* ---------------------------------------------------------------
   * 1. API client - existing endpoints only
   * ------------------------------------------------------------- */

  async function req(path, options = {}) {
    const res = await fetch(path, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    if (res.status === 401) {
      window.location.href = '/login';
      throw new Error('Not authenticated');
    }
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body && body.detail) detail = body.detail;
      } catch (_) { /* response had no JSON body */ }
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  }

  const api = {
    zones:         ()              => req('/api/zones').then(r => r.zones || r),
    status:        ()              => req('/api/status'),
    hub:           ()              => req('/api/hub'),
    capabilities:  ()              => req('/api/capabilities'),
    schedule:      (zoneId)        => req(`/api/zones/${encodeURIComponent(zoneId)}/schedule`),
    setOverride:   (zoneId, mode)  => req(`/api/zones/${encodeURIComponent(zoneId)}/override/${mode}`, { method: 'POST' }),
    setGlobalMode: (mode)          => req(`/api/global/override/${mode}`, { method: 'POST' }),
    setTemps:      (zoneId, temps) => req(`/api/zones/${encodeURIComponent(zoneId)}/temperature`, {
                                       method: 'POST', body: JSON.stringify(temps) }),
    logout:        ()              => req('/auth/logout', { method: 'POST' }),
    me:            ()              => req('/auth/me'),
  };

  /* ---------------------------------------------------------------
   * 2. Device pictures
   *
   * Copied verbatim from the production app.js so the prototypes stay
   * self-contained and cannot break the live UI. The first three digits
   * of the serial identify the model; the model names the image file.
   * Images live in /static/images as PNG line drawings, with an SVG and
   * finally a generic placeholder as fallbacks.
   * ------------------------------------------------------------- */

  const DEVICE_MODELS = {
    '000': { name: 'NTB-2R',               image: 'NTB-2R'      },
    '120': { name: 'RS 700',               image: 'RS-700'      },
    '121': { name: 'RSX 700',              image: 'RSX-700'     },
    '130': { name: 'RCE 700',              image: 'RCE-700'     },
    '160': { name: 'R80 RDC 700',          image: 'R80-RDC-700' },
    '165': { name: 'R80 RDC 700 LST (GB)', image: 'R80-RDC-700' },
    '168': { name: 'NCU-2R',               image: 'NCU-2R'      },
    '169': { name: 'DCU-2R',               image: 'DCU-2R'      },
    '170': { name: 'Serie 18, ewt touch',  image: 'SERIE-18'    },
    '180': { name: '2NC9 700',             image: '2NC9-700'    },
    '182': { name: 'R80 RSC 700',          image: 'R80-RSC-700' },
    '183': { name: 'R80 RSC 700',          image: 'R80-RSC-700' },
    '184': { name: 'NCU-1R',               image: 'NCU-1R'      },
    '186': { name: 'DCU-1R',               image: 'DCU-1R'      },
    '190': { name: 'Safir',                image: 'SAFIR'       },
    '192': { name: 'R80 TXF 700',          image: 'R80-TXF-700' },
    '194': { name: 'R80 RXC 700',          image: 'R80-RXC-700' },
    '198': { name: 'NCU-ER',               image: 'NCU-ER'      },
    '199': { name: 'DCU-ER',               image: 'DCU-ER'      },
    '200': { name: 'TRB 36 700',           image: 'TRB-36-700'  },
    '210': { name: 'NTB-2R',               image: 'NTB-2R'      },
    '220': { name: 'TR36',                 image: 'TR36'        },
    '230': { name: 'TCU 700',              image: 'TCU-700'     },
    '231': { name: 'THB 700',              image: 'THB-700'     },
    '232': { name: 'TXB 700',              image: 'TXB-700'     },
    '234': { name: 'SW4',                  image: 'SW4'         },
  };

  function deviceModel(serial) {
    const prefix = String(serial || '').replace(/\s/g, '').slice(0, 3);
    return DEVICE_MODELS[prefix] || null;
  }

  function deviceName(serial) {
    const model = deviceModel(serial);
    return model ? model.name : 'Unknown model';
  }

  /**
   * Build an <img> for a device.
   *
   * The source artwork is a pale grey isometric line drawing with a roughly
   * 2:1 landscape aspect. All three concepts therefore give it a wide, light
   * "plinth" to sit on rather than squeezing it into a square avatar.
   */
  function deviceImg(serial, cssClass = '', altOverride = null) {
    const model = deviceModel(serial);
    const slug = model ? model.image : 'placeholder';
    const lower = slug.toLowerCase();
    const alt = altOverride || (model ? `${model.name} heating device` : 'Heating device');
    return `<img src="/static/images/${slug}.png" alt="${escapeHtml(alt)}" loading="lazy"` +
           ` class="${cssClass}"` +
           ` onerror="this.onerror=null;this.src='/static/images/${lower}.svg';` +
           `this.onerror=function(){this.src='/static/images/placeholder.svg';};">`;
  }

  /* ---------------------------------------------------------------
   * 3. Derived display values
   *
   * The API does not report whether an element is drawing power right now,
   * and it does not report the effective house mode. Both are inferred here
   * from data the API does return. Anything inferred is labelled as an
   * estimate in the UI - see docs/UI_REDESIGN.md, "Implementation
   * considerations".
   * ------------------------------------------------------------- */

  const MODES = {
    comfort: { label: 'Comfort',  glyph: '\u25B2' },
    eco:     { label: 'Eco',      glyph: '\u25CF' },
    away:    { label: 'Away',     glyph: '\u25BC' },
    off:     { label: 'Off',      glyph: '\u25A0' },
    normal:  { label: 'Schedule', glyph: '\u25F4' },
  };

  /** The mode the zone is actually running right now. */
  function effectiveMode(zone) {
    const mode = zone.current_mode || 'normal';
    if (mode !== 'normal') return mode;
    return zone.schedule_mode || 'comfort';
  }

  /** The temperature the zone is currently aiming for, or null. */
  function targetTemp(zone) {
    switch (effectiveMode(zone)) {
      case 'eco':  return zone.eco_temperature;
      case 'away': return zone.away_temperature;
      case 'off':  return null;
      default:     return zone.comfort_temperature;
    }
  }

  /**
   * Estimate whether the zone is calling for heat.
   *
   * 'heating'  - measured temperature is below target
   * 'holding'  - measured temperature has reached target
   * 'unknown'  - no sensor in this zone, so we genuinely cannot say
   */
  function heatState(zone) {
    const cur = zone.current_temperature;
    const target = targetTemp(zone);
    if (cur == null || target == null || !zone.supports_temp_adjust) return 'unknown';
    return cur < target - 0.15 ? 'heating' : 'holding';
  }

  const HEAT_STATE = {
    heating: { label: 'Heating',        hint: 'Measured temperature is below the target, so this zone is calling for heat.' },
    holding: { label: 'At temperature', hint: 'The zone has reached its target temperature.' },
    unknown: { label: 'No sensor',      hint: 'This zone has no temperature sensor, so its state cannot be reported.' },
  };

  /**
   * Work out the house mode from the zones themselves.
   *
   * The production UI keeps this in a JavaScript variable, so it resets to
   * "Home" on every page reload even when every zone is overridden. Deriving
   * it makes the control honest without touching the backend.
   */
  function houseMode(zones) {
    if (!zones.length) return 'home';
    const modes = new Set(zones.map(z => z.current_mode || 'normal'));
    if (modes.size === 1) {
      const only = [...modes][0];
      return only === 'normal' ? 'home' : only;
    }
    return 'mixed';
  }

  function houseSummary(zones) {
    const withSensor = zones.filter(z => z.current_temperature != null);
    const avg = withSensor.length
      ? withSensor.reduce((s, z) => s + z.current_temperature, 0) / withSensor.length
      : null;
    const coldest = withSensor.slice()
      .sort((a, b) => a.current_temperature - b.current_temperature)[0] || null;
    return {
      averageTemp: avg,
      heatingCount: zones.filter(z => heatState(z) === 'heating').length,
      zoneCount: zones.length,
      coldest,
      overriddenCount: zones.filter(z => (z.current_mode || 'normal') !== 'normal').length,
      mode: houseMode(zones),
    };
  }

  /* ---------------------------------------------------------------
   * 4. Today's schedule - what happens next
   * ------------------------------------------------------------- */

  const DAY_KEYS = ['sunday', 'monday', 'tuesday', 'wednesday',
                    'thursday', 'friday', 'saturday'];

  function minutesOf(hhmm) {
    const [h, m] = String(hhmm).split(':').map(Number);
    return (h * 60) + (m || 0);
  }

  function fmtClock(minutes) {
    const h = String(Math.floor(minutes / 60) % 24).padStart(2, '0');
    const m = String(minutes % 60).padStart(2, '0');
    return `${h}:${m}`;
  }

  /** Blocks for today, plus the block running now and the next transition. */
  function todaySchedule(schedule) {
    if (!schedule) return null;
    const now = new Date();
    const blocks = schedule[DAY_KEYS[now.getDay()]] || [];
    const nowMin = now.getHours() * 60 + now.getMinutes();
    return {
      blocks,
      nowMin,
      next: blocks.find(b => minutesOf(b.start) > nowMin) || null,
      current: blocks.find(b => minutesOf(b.start) <= nowMin && minutesOf(b.end) > nowMin) || null,
    };
  }

  /* ---------------------------------------------------------------
   * 5. Live updates
   * ------------------------------------------------------------- */

  /**
   * Subscribe to zone updates over the existing /ws feed, falling back to
   * polling if the socket cannot be established.
   */
  function subscribe(onZones, onConnection) {
    let socket = null;
    let pollTimer = null;
    let closed = false;

    const emitZones = (data) => {
      const zones = Array.isArray(data) ? data : (data && data.zones) || [];
      onZones(zones);
    };

    function startPolling() {
      if (pollTimer) return;
      pollTimer = setInterval(async () => {
        try {
          emitZones(await api.zones());
          onConnection && onConnection(true, 'polling');
        } catch (_) {
          onConnection && onConnection(false, 'offline');
        }
      }, 10000);
    }

    function stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function connect() {
      if (closed) return;
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
      try {
        socket = new WebSocket(`${proto}://${window.location.host}/ws`);
      } catch (_) {
        startPolling();
        return;
      }
      socket.onopen = () => { stopPolling(); onConnection && onConnection(true, 'live'); };
      socket.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'zones_update') emitZones(msg.data);
        } catch (_) { /* ignore malformed frames */ }
      };
      socket.onclose = () => {
        if (closed) return;
        onConnection && onConnection(false, 'reconnecting');
        startPolling();
        setTimeout(connect, 5000);
      };
      socket.onerror = () => socket && socket.close();
    }

    connect();
    return () => {
      closed = true;
      stopPolling();
      if (socket) socket.close();
    };
  }

  /* ---------------------------------------------------------------
   * 6. Small helpers
   * ------------------------------------------------------------- */

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtTemp(value, digits = 1) {
    return value == null ? '\u2014' : value.toFixed(digits);
  }

  /** Debounce with a flush, so a slider can commit immediately on release. */
  function debounce(fn, wait) {
    let timer = null;
    let lastArgs = null;
    const wrapped = (...args) => {
      lastArgs = args;
      clearTimeout(timer);
      timer = setTimeout(() => { timer = null; fn(...lastArgs); }, wait);
    };
    wrapped.flush = () => {
      if (timer) { clearTimeout(timer); timer = null; fn(...lastArgs); }
    };
    return wrapped;
  }

  /** Accessible toast. Each concept styles .np-toast itself. */
  function toast(message, kind = 'info') {
    let host = document.getElementById('npToasts');
    if (!host) {
      host = document.createElement('div');
      host.id = 'npToasts';
      host.className = 'np-toasts';
      host.setAttribute('role', 'status');
      host.setAttribute('aria-live', 'polite');
      document.body.appendChild(host);
    }
    const el = document.createElement('div');
    el.className = `np-toast np-toast-${kind}`;
    el.textContent = message;
    host.appendChild(el);
    setTimeout(() => {
      el.classList.add('np-toast-out');
      setTimeout(() => el.remove(), 300);
    }, kind === 'error' ? 6000 : 3000);
  }

  /* The Nobo system accepts 5-30 C in half degrees. */
  const TEMP_MIN = 5;
  const TEMP_MAX = 30;
  const clampTemp = (v) => Math.min(TEMP_MAX, Math.max(TEMP_MIN, Math.round(v * 2) / 2));

  return {
    api, DEVICE_MODELS, deviceModel, deviceName, deviceImg,
    MODES, effectiveMode, targetTemp, heatState, HEAT_STATE,
    houseMode, houseSummary, todaySchedule, DAY_KEYS, minutesOf, fmtClock,
    subscribe, escapeHtml, fmtTemp, debounce, toast,
    TEMP_MIN, TEMP_MAX, clampTemp,
  };
})();
