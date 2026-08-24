/*
 * Concept D - "Cabin"
 *
 * Design premise: a cabin stands empty most of the year, so the question the
 * owner opens the app to answer is almost never "what is the living room set
 * to". It is "is it empty, when am I back, and will it be warm when I get
 * there". The trip is therefore the hero, whole-cabin modes come second, and
 * per-room detail sits underneath.
 *
 * Two deliberate inversions from the earlier concepts, both asked for:
 *   - the SET temperature is the big number; the measured temperature is
 *     secondary support underneath it.
 *   - devices whose temperature can only be turned by hand are called out in
 *     words on the room row and again on each device.
 *
 * Read/write against existing endpoints only, with one exception: the away
 * exceptions list (rooms held on Eco during Away) is stored and applied on the
 * server, because an away period starts in a background loop with no browser
 * open. See docs/UI_REDESIGN.md.
 */

(() => {
  'use strict';

  const $  = (sel, root = document) => root.querySelector(sel);
  const esc = Nobo.escapeHtml;

  /* Away is a fixed 7 C anti-frost temperature set by Nobø. It is not a
     configurable setpoint, and users reasonably assume it is - so every place
     Away appears says so, and points at Eco as the way to hold a room warmer.
     The real value is read from the API and overwrites this default. */
  let AWAY_TEMP = 7;
  const AWAY_TEMP_LABEL = () => `${AWAY_TEMP}°C`;
  const AWAY_EXPLAINER = () =>
    `Away is a fixed ${AWAY_TEMP}°C anti-frost setting from Nobø and cannot be raised. ` +
    `To keep a room warmer than that, use Eco and set its Eco temperature.`;

  /* ------------------------------------------------------------------
   * State
   * ---------------------------------------------------------------- */

  const state = {
    zones: [],
    devices: [],
    status: null,
    hub: null,
    caps: null,
    view: 'home',        // 'home' | 'zone' | 'settings'
    zoneId: null,
    schedule: null,
    scheduleMeta: null,
    /* While a write is in flight, or the user is mid-gesture, incoming live
       updates must not redraw the control out from under them. */
    pending: new Set(),
    holdUntil: 0,
  };

  const hold = (ms = 2500) => { state.holdUntil = Date.now() + ms; };
  const held = () => Date.now() < state.holdUntil || state.pending.size > 0;

  /* ------------------------------------------------------------------
   * Bottom sheet
   * ---------------------------------------------------------------- */

  const sheetEl   = $('#sheet');
  const scrimEl   = $('#sheetScrim');
  const sheetBody = $('#sheetBody');
  let lastFocus = null;

  /**
   * A sheet may leave something running behind it - the device search polls
   * the hub every two seconds. Whatever opened the sheet registers a cleanup
   * here and closeSheet() runs it, so nothing keeps polling once the sheet is
   * dismissed by the scrim, by Escape, or by a button.
   */
  let sheetCleanup = null;
  const onSheetClose = (fn) => { sheetCleanup = fn; };

  function openSheet(title, html, wire) {
    lastFocus = document.activeElement;
    $('#sheetTitle').textContent = title;
    sheetBody.innerHTML = html;
    sheetEl.hidden = false;
    scrimEl.hidden = false;
    if (wire) wire(sheetBody);
    const first = sheetBody.querySelector('input, select, button');
    if (first) first.focus();
    document.addEventListener('keydown', onSheetKey);
  }

  function closeSheet() {
    if (sheetCleanup) { const fn = sheetCleanup; sheetCleanup = null; fn(); }
    sheetEl.hidden = true;
    scrimEl.hidden = true;
    sheetBody.innerHTML = '';
    document.removeEventListener('keydown', onSheetKey);
    if (lastFocus && lastFocus.isConnected) lastFocus.focus();
  }

  function onSheetKey(e) { if (e.key === 'Escape') closeSheet(); }
  scrimEl.addEventListener('click', closeSheet);

  /** Ask before anything that changes the whole cabin or destroys data. */
  function confirmSheet(title, message, confirmLabel, onConfirm, danger = false) {
    openSheet(title, `
      <p class="zd-sub">${esc(message)}</p>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
        <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-act="ok" type="button">${esc(confirmLabel)}</button>
      </div>`, (root) => {
      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      root.querySelector('[data-act="ok"]').onclick = async () => {
        closeSheet();
        await onConfirm();
      };
    });
  }

  /* ------------------------------------------------------------------
   * Loading
   * ---------------------------------------------------------------- */

  async function loadAll() {
    const [zones, status, hub, caps, devices] = await Promise.all([
      Nobo.api.zones().catch(() => []),
      Nobo.api.status().catch(() => null),
      Nobo.api.hubConfig().catch(() => null),
      Nobo.api.capabilities().catch(() => null),
      Nobo.api.devices().catch(() => []),
    ]);
    state.zones = zones || [];
    state.status = status;
    state.hub = hub;
    state.caps = caps;
    state.devices = devices || [];
  }

  const away = () => (state.status && state.status.away_schedule) || { enabled: false };

  /* ------------------------------------------------------------------
   * Connection pill
   * ---------------------------------------------------------------- */

  function renderLink() {
    const el = $('#linkState');
    const text = el.querySelector('.link-text');
    el.classList.remove('is-ok', 'is-down', 'is-demo');

    if (state.hub && state.hub.demo_mode) {
      el.classList.add('is-demo');
      text.textContent = 'Demo';
      el.title = 'Demo mode - example rooms and devices, no hub connected.';
    } else if (state.status && state.status.connected) {
      el.classList.add('is-ok');
      text.textContent = 'Hub';
      el.title = `Connected to hub ${state.hub && state.hub.serial_display ? state.hub.serial_display : ''}`.trim();
    } else {
      el.classList.add('is-down');
      text.textContent = 'No hub';
      el.title = 'Not connected to the hub.';
    }
  }

  /* ------------------------------------------------------------------
   * The trip card
   * ---------------------------------------------------------------- */

  function renderTrip() {
    const a = away();
    const card    = $('#trip');
    const stateEl = $('#tripState');
    const detail  = $('#tripDetail');
    const actions = $('#tripActions');
    const tl      = $('#tripTimeline');

    card.classList.remove('is-away', 'is-heat');
    tl.hidden = true;

    const mode = Nobo.houseMode(state.zones);

    if (a.enabled && a.currently_active) {
      card.classList.add('is-away');
      stateEl.textContent = 'Empty until ' + Nobo.fmtWhen(a.end_at);
      detail.textContent  = `Every room is holding at the away temperature. Normal schedules resume ${Nobo.fmtUntil(a.end_at)}.`;
      drawTimeline(a.start_at, a.end_at);
      actions.innerHTML = `
        <button class="btn btn-primary" data-act="arrive" type="button">I'm back now</button>
        <button class="btn" data-act="plan" type="button">Change return</button>
        <button class="btn btn-danger" data-act="delete-trip" type="button">Delete away period</button>`;

    } else if (a.enabled && a.start_at) {
      card.classList.add('is-away');
      stateEl.textContent = 'Away from ' + Nobo.fmtWhen(a.start_at);
      detail.textContent  = `Starts ${Nobo.fmtUntil(a.start_at)}, back ${Nobo.fmtWhen(a.end_at)}. Until then rooms follow their normal schedules.`;
      drawTimeline(a.start_at, a.end_at);
      actions.innerHTML = `
        <button class="btn btn-primary" data-act="plan" type="button">Change plan</button>
        <button class="btn btn-danger" data-act="delete-trip" type="button">Delete away period</button>`;

    } else {
      if (mode === 'away') {
        /* Away with no window: the same state the "Away" mode button produces.
           It never ends by itself, so the way out has to be on this card. */
        card.classList.add('is-away');
        stateEl.textContent = 'Away until you say otherwise';
        detail.textContent  = 'Every room is on away and nothing will bring the heating back automatically. Set a return date and it will warm up before you arrive.';
        actions.innerHTML = `
          <button class="btn btn-primary" data-act="arrive" type="button">I'm back now</button>
          <button class="btn" data-act="plan" type="button">Set a return date</button>`;
        wireTripActions(actions);
        return;
      } else if (mode === 'comfort') {
        card.classList.add('is-heat');
        stateEl.textContent = 'Warming the whole cabin';
        detail.textContent  = 'Every room is held at its comfort temperature until you change it.';
      } else if (mode === 'eco') {
        stateEl.textContent = 'Ticking over on eco';
        detail.textContent  = 'Every room is held at its eco temperature.';
      } else if (mode === 'mixed') {
        stateEl.textContent = 'Rooms set individually';
        detail.textContent  = 'Some rooms are overridden and some are following their schedule.';
      } else {
        stateEl.textContent = "Someone's here";
        detail.textContent  = 'Rooms are following their normal schedules.';
      }
      actions.innerHTML = `<button class="btn btn-primary" data-act="leave" type="button">I'm leaving &rarr;</button>`;
    }

    wireTripActions(actions);
  }

  function wireTripActions(actions) {
    actions.querySelectorAll('button').forEach(b => {
      b.onclick = () => {
        const act = b.dataset.act;
        if (act === 'leave' || act === 'plan') openTripSheet();
        if (act === 'arrive') arriveNow();
        if (act === 'delete-trip') deleteAwayPeriod();
      };
    });
  }

  /** Remove the away window entirely. Reachable from the card and the sheet. */
  function deleteAwayPeriod() {
    confirmSheet('Delete the away period?',
      'The dates are removed and the cabin goes back to its normal schedules straight away.',
      'Delete away period', async () => {
        try {
          await Nobo.api.clearAwaySchedule();
          Nobo.toast('Away period deleted');
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      }, true);
  }

  function drawTimeline(startIso, endIso) {
    const start = new Date(startIso).getTime();
    const end   = new Date(endIso).getTime();
    if (!start || !end || end <= start) return;
    const now  = Date.now();
    const pct  = Math.min(100, Math.max(0, ((now - start) / (end - start)) * 100));
    $('#tlFill').style.width = pct + '%';
    $('#tlNow').style.left = `calc(${pct}% - 1px)`;
    $('#tlStart').textContent = Nobo.fmtWhen(startIso);
    $('#tlEnd').textContent   = Nobo.fmtWhen(endIso);
    $('#tripTimeline').hidden = false;
  }

  /**
   * "I'm leaving" - the single most used flow in a cabin.
   *
   * The away schedule is a window during which the house is forced to away,
   * and normal schedules resume at the end of it. So the return date IS the
   * end of the window, and warming up early simply means ending the window
   * a few hours sooner. That is explained in the sheet rather than hidden.
   */
  function openTripSheet() {
    const a = away();
    const now = new Date();
    const startIso = (a.enabled && a.start_at) ? a.start_at : now.toISOString();

    let back = a.enabled && a.end_at ? new Date(a.end_at) : null;
    if (!back || back.getTime() < Date.now()) {
      back = new Date(now.getTime() + 7 * 86400000);
      back.setHours(16, 0, 0, 0);
    }
    const b = Nobo.fromIsoInstant(back.toISOString());
    const s = Nobo.fromIsoInstant(startIso);
    const leavingNow = !a.enabled || !a.start_at || new Date(a.start_at) <= now;

    openSheet(a.enabled ? 'Change your away period' : "You're leaving", `
      <p class="zd-sub">Every room drops to Away — a fixed ${AWAY_TEMP_LABEL()} anti-frost
      temperature set by Nobø — and returns to its normal schedule when you get back.
      Rooms that must stay warmer can be held on Eco instead, under Settings.</p>

      <label class="field">
        <span>Leaving</span>
        <div class="field-row">
          <input type="date" id="tsStartDate" value="${esc(s.date)}">
          <input type="time" id="tsStartTime" value="${esc(s.time)}">
        </div>
        <small class="field-hint">${leavingNow ? 'Leave as it is to start right now.' : ''}</small>
      </label>

      <label class="field">
        <span>Back</span>
        <div class="field-row">
          <input type="date" id="tsEndDate" value="${esc(b.date)}">
          <input type="time" id="tsEndTime" value="${esc(b.time)}">
        </div>
      </label>

      <label class="field">
        <span>Start heating before I arrive</span>
        <select id="tsHead">
          <option value="0">When I arrive</option>
          <option value="2">2 hours early</option>
          <option value="4" selected>4 hours early</option>
          <option value="8">8 hours early</option>
          <option value="12">12 hours early</option>
          <option value="24">A day early</option>
        </select>
        <small class="field-hint" id="tsHint"></small>
      </label>

      <div class="sheet-actions">
        <button class="btn" data-act="dismiss" type="button">Close</button>
        <button class="btn btn-primary" data-act="save" type="button">Set away period</button>
      </div>

      <div class="sheet-alt">
        <p class="zd-sub">Not sure when you are back?</p>
        <button class="btn btn-wide" data-act="constant" type="button">Stay away with no return date</button>
        <small class="field-hint">The same as the Away button: every room holds the away
        temperature until you come back and end it yourself.</small>
      </div>

      ${a.enabled ? `
      <div class="sheet-alt">
        <button class="btn btn-danger btn-wide" data-act="delete" type="button">Delete this away period</button>
        <small class="field-hint">Removes the dates and returns the cabin to its normal schedules.</small>
      </div>` : ''}
    `, (root) => {
      const hint = root.querySelector('#tsHint');

      const computedEnd = () => {
        const iso = Nobo.toIsoInstant(root.querySelector('#tsEndDate').value,
                                      root.querySelector('#tsEndTime').value);
        if (!iso) return null;
        const headHours = Number(root.querySelector('#tsHead').value || 0);
        return new Date(new Date(iso).getTime() - headHours * 3600000).toISOString();
      };

      const updateHint = () => {
        const end = computedEnd();
        hint.textContent = end
          ? 'Heating resumes ' + Nobo.fmtWhen(end) + '.'
          : 'Pick the date you are coming back.';
      };
      root.querySelectorAll('input, select').forEach(el => el.addEventListener('input', updateHint));
      updateHint();

      root.querySelector('[data-act="dismiss"]').onclick = closeSheet;
      root.querySelector('[data-act="constant"]').onclick = () => stayAwayIndefinitely();
      const del = root.querySelector('[data-act="delete"]');
      if (del) del.onclick = () => deleteAwayPeriod();
      root.querySelector('[data-act="save"]').onclick = async () => {
        const start = Nobo.toIsoInstant(root.querySelector('#tsStartDate').value,
                                        root.querySelector('#tsStartTime').value);
        const end = computedEnd();
        if (!start || !end) { Nobo.toast('Enter both a leaving and a return date', 'error'); return; }
        if (new Date(end) <= new Date(start)) {
          Nobo.toast('Your return has to be after you leave', 'error'); return;
        }
        if (new Date(end) <= new Date()) {
          Nobo.toast('That return time has already passed - check the year', 'error'); return;
        }
        try {
          await Nobo.api.setAwaySchedule({ enabled: true, start_at: start, end_at: end });
          closeSheet();
          Nobo.toast('Away period saved');
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      };
    });
  }

  async function arriveNow() {
    confirmSheet("You're back",
      'The away period ends now and every room returns to its normal schedule.',
      "I'm back", async () => {
        try {
          /* There may be no window to clear - the cabin can be on constant
             away from the mode button - so a 404 here is not a failure. */
          await Nobo.api.clearAwaySchedule().catch(() => {});
          await Nobo.api.setGlobalMode('home');
          Nobo.toast('Welcome back - heating resumed');
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      });
  }

  /**
   * Away with no end date. Identical to pressing the Away mode button, offered
   * here as well so the two ways of leaving are not confused with each other:
   * a period has a return date and ends itself, this one does not.
   */
  function stayAwayIndefinitely() {
    confirmSheet('Stay away with no return date?',
      'Every room drops to the away temperature and stays there until you end it yourself. Any away period you had planned is removed.',
      'Stay away', async () => {
        try {
          await Nobo.api.clearAwaySchedule().catch(() => {});
          await Nobo.api.setGlobalMode('away');
          Nobo.toast('Away - no return date set');
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      });
  }

  /* ------------------------------------------------------------------
   * Whole-cabin modes
   * ---------------------------------------------------------------- */

  function renderModes() {
    const mode = Nobo.houseMode(state.zones);
    const activeAway = away().currently_active;
    document.querySelectorAll('[data-global]').forEach(btn => {
      const m = btn.dataset.global;
      const on = activeAway ? (m === 'away') : (m === mode || (m === 'home' && mode === 'home'));
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  document.querySelectorAll('[data-global]').forEach(btn => {
    btn.addEventListener('click', () => {
      const mode = btn.dataset.global;
      const labels = {
        home:    ['Back to schedules?', 'Every room returns to its own weekly schedule.'],
        comfort: ['Warm the whole cabin?', 'Every room is held at its comfort temperature until you change it.'],
        eco:     ['Whole cabin on eco?', 'Every room is held at its eco temperature.'],
        away:    ['Whole cabin on away?', 'Every room drops to the away temperature and stays there until you change it. To have the heating come back on its own, use "I\u2019m leaving" instead.'],
      };
      const [title, msg] = labels[mode];
      confirmSheet(title, msg, 'Yes, ' + mode, async () => {
        hold();
        try {
          await Nobo.api.setGlobalMode(mode);
          Nobo.toast('Whole cabin set to ' + mode);
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      });
    });
  });

  /* ------------------------------------------------------------------
   * Rooms
   * ---------------------------------------------------------------- */

  /**
   * Which setpoint the +/- buttons should move.
   *
   * Away and off are not adjustable per room - away is a fixed system
   * temperature - so the buttons are disabled and say why rather than
   * silently doing nothing.
   */
  function setpointKey(zone) {
    const mode = Nobo.effectiveMode(zone);
    if (mode === 'comfort') return 'comfort';
    if (mode === 'eco') return 'eco';
    return null;
  }

  function devicesOfZone(zoneId) {
    return state.devices.filter(d => String(d.zone_id) === String(zoneId));
  }

  function zoneRow(zone) {
    const mode = Nobo.effectiveMode(zone);
    const key = setpointKey(zone);
    const target = Nobo.targetTemp(zone);
    /* When no heater in the room can be adjusted remotely, the room still
       reports a comfort temperature - but nothing can act on it. Showing it
       as "set to" would be a straight lie, so these rooms lead with the mode
       they are running and say where the temperature actually comes from. */
    const remote = zone.supports_temp_adjust !== false;
    const adjustable = key !== null && remote;

    const scheduled = (zone.current_mode || 'normal') === 'normal';
    const modeLabel = (Nobo.MODES[mode] || {}).label || mode;
    const modeBadge = `<span class="badge badge-mode-${esc(mode)}">${scheduled ? 'Schedule &middot; ' : ''}${esc(modeLabel)}</span>`;

    const manualBadge = !remote
      ? `<span class="badge badge-manual" title="No heater in this room can be adjusted from here. Turn the dial on the heater to change its temperature.">Set on heater</span>`
      : (zone.has_manual_devices
          ? `<span class="badge badge-manual" title="Some heaters in this room have no remote temperature control. Their temperature is set by a dial on the heater itself.">Some dial-only</span>`
          : '');

    const comps = zone.components || [];
    const thumbs = comps.slice(0, 3)
      .map(c => `<span class="np-device">${Nobo.deviceImg(c)}</span>`).join('');
    const more = comps.length > 3 ? `<span class="more">+${comps.length - 3}</span>` : '';

    let label, setBlock;
    if (!remote) {
      label = 'Running';
      setBlock = `<span class="set-mode">${esc(modeLabel)}</span>`;
    } else {
      label = 'Set to';
      setBlock = target == null
        ? `<span class="set-none">Not set</span>`
        : `<span class="set-value">${Nobo.bigTemp(target)}</span>`;
    }

    const nowBlock = zone.current_temperature == null
      ? `<span class="set-now">${remote ? 'No sensor' : 'Dial sets the temperature'}</span>`
      : `<span class="set-now">now ${Nobo.fmtTemp(zone.current_temperature)}&deg;</span>`;

    const stepTitle = adjustable
      ? ''
      : (!remote
          ? 'Turn the dial on the heater to change this room'
          : 'Away uses a fixed system temperature');

    return `
      <li class="zone" data-zone="${esc(zone.zone_id)}">
        <button class="zone-open" type="button" data-open="${esc(zone.zone_id)}">
          <span>${esc(zone.name)}</span><span class="chev" aria-hidden="true">›</span>
        </button>
        <div class="zone-meta">${modeBadge}${manualBadge}</div>
        <div class="zone-set">
          <span class="set-label">${esc(label)}</span>
          ${setBlock}
          ${nowBlock}
        </div>
        ${remote ? `
        <div class="stepper">
          <button class="step-btn" type="button" data-step="down" data-zone="${esc(zone.zone_id)}"
            ${adjustable ? '' : 'disabled'} title="${esc(stepTitle)}"
            aria-label="Lower ${esc(zone.name)} set temperature">&minus;</button>
          <button class="step-btn" type="button" data-step="up" data-zone="${esc(zone.zone_id)}"
            ${adjustable ? '' : 'disabled'} title="${esc(stepTitle)}"
            aria-label="Raise ${esc(zone.name)} set temperature">+</button>
        </div>` : ''}
        <div class="zone-devices">${thumbs}${more}</div>
      </li>`;
  }

  function renderZones() {
    const list = $('#zoneList');
    if (!state.zones.length) {
      list.innerHTML = `<li class="zone"><div class="zone-meta">No rooms configured yet.</div></li>`;
      $('#roomsNote').textContent = '';
      return;
    }
    list.innerHTML = state.zones.map(zoneRow).join('');
    const manual = state.zones.filter(z => z.has_manual_devices).length;
    $('#roomsNote').textContent = manual
      ? `${state.zones.length} rooms · ${manual} with a dial-only heater`
      : `${state.zones.length} rooms`;

    list.querySelectorAll('[data-open]').forEach(b => {
      b.onclick = () => showZone(b.dataset.open);
    });
    list.querySelectorAll('[data-step]').forEach(b => {
      b.onclick = () => stepZone(b.dataset.zone, b.dataset.step === 'up' ? 0.5 : -0.5);
    });
  }

  /* Optimistic, debounced, and never fights an in-flight write. */
  const commitTemp = Nobo.debounce(async (zoneId) => {
    const zone = state.zones.find(z => String(z.zone_id) === String(zoneId));
    if (!zone) return;
    const key = setpointKey(zone);
    if (!key) return;
    const body = key === 'eco'
      ? { eco: zone.eco_temperature }
      : { comfort: zone.comfort_temperature };
    try {
      await Nobo.api.setTemps(zoneId, body);
      Nobo.toast(`${zone.name} set to ${Nobo.fmtTemp(Nobo.targetTemp(zone))}\u00B0`);
    } catch (e) {
      Nobo.toast(e.message, 'error');
      await refresh(true);
    } finally {
      state.pending.delete(String(zoneId));
    }
  }, 700);

  function stepZone(zoneId, delta) {
    const zone = state.zones.find(z => String(z.zone_id) === String(zoneId));
    if (!zone) return;
    const key = setpointKey(zone);
    if (!key) return;
    const field = key === 'eco' ? 'eco_temperature' : 'comfort_temperature';
    const next = Nobo.clampTemp((zone[field] ?? 20) + delta);
    zone[field] = next;
    state.pending.add(String(zoneId));
    hold();
    if (state.view === 'zone') renderZoneDetail(); else renderZones();
    commitTemp(zoneId);
  }

  /* ------------------------------------------------------------------
   * System status - last, on purpose
   * ---------------------------------------------------------------- */

  function renderSystem() {
    const s = Nobo.houseSummary(state.zones);
    const st = state.status || {};
    const rows = [
      ['Rooms', String(s.zoneCount)],
      ['Average temperature', s.averageTemp == null ? 'No sensors' : Nobo.fmtTemp(s.averageTemp) + '\u00B0'],
      ['Coldest room', s.coldest ? `${s.coldest.name} at ${Nobo.fmtTemp(s.coldest.current_temperature)}\u00B0` : 'Unknown'],
      ['Likely heating now', `${s.heatingCount} of ${s.zoneCount} (estimated from temperatures)`],
      ['Rooms overridden', String(s.overriddenCount)],
      ['Hub', state.hub && state.hub.demo_mode ? 'Demo mode' : (state.hub && state.hub.serial_display) || 'Unknown'],
      ['Time zone', st.timezone || 'Unknown'],
    ];
    $('#sysGrid').innerHTML = rows
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  }

  /* ------------------------------------------------------------------
   * Zone detail
   * ---------------------------------------------------------------- */

  async function showZone(zoneId) {
    state.view = 'zone';
    state.zoneId = String(zoneId);
    state.schedule = null;
    state.scheduleMeta = null;
    switchView();
    renderZoneDetail();
    await loadSchedule(zoneId);
  }

  /** Load the week for one room. A profile can be shared, so keep the whole
      response - the editor has to warn which other rooms it also changes. */
  async function loadSchedule(zoneId) {
    try {
      const res = await Nobo.api.schedule(zoneId);
      state.schedule = res && res.schedule ? res.schedule : null;
      state.scheduleMeta = res || null;
    } catch (_) { state.schedule = null; state.scheduleMeta = null; }
    if (state.view === 'zone') renderZoneDetail();
  }

  function renderZoneDetail() {
    const zone = state.zones.find(z => String(z.zone_id) === state.zoneId);
    const root = $('#viewZone');
    if (!zone) { root.innerHTML = `<div class="card">This room is no longer available.</div>`; return; }

    $('#topTitle').textContent = zone.name;
    $('#topSub').textContent = 'Room';

    const mode = Nobo.effectiveMode(zone);
    const key = setpointKey(zone);
    const target = Nobo.targetTemp(zone);
    const remote = zone.supports_temp_adjust !== false;
    const adjustable = key !== null && remote;
    const devices = devicesOfZone(zone.zone_id);
    const modeLabel = (Nobo.MODES[mode] || {}).label || mode;

    const whichSetpoint = key === 'eco' ? 'eco temperature' : key === 'comfort' ? 'comfort temperature' : 'away temperature';

    const headLabel = remote ? `Set to (${whichSetpoint})` : 'Running';
    const headValue = remote
      ? (target == null ? '<span class="set-none">Not set</span>' : Nobo.bigTemp(target))
      : `<span class="zd-mode">${esc(modeLabel)}</span>`;
    const headSub = !remote
      ? 'The temperature in this room is set by the dial on each heater'
      : (zone.current_temperature == null
          ? 'No temperature sensor in this room'
          : 'Measuring ' + Nobo.fmtTemp(zone.current_temperature) + '\u00B0 right now');

    root.innerHTML = `
      <section class="zd-head">
        <span class="set-label">${esc(headLabel)}</span>
        <div class="zd-set">
          <div>
            <div class="zd-big">${headValue}</div>
            <p class="zd-sub">${esc(headSub)}</p>
          </div>
          <div class="zd-steps">
            <button class="step-btn" type="button" data-zstep="down" ${adjustable ? '' : 'disabled'}
              aria-label="Lower set temperature">&minus;</button>
            <button class="step-btn" type="button" data-zstep="up" ${adjustable ? '' : 'disabled'}
              aria-label="Raise set temperature">+</button>
          </div>
        </div>
        ${adjustable ? '' : `<div class="note note-warn">${!remote
          ? 'No heater in this room can be adjusted from here. You can still switch the room between comfort, eco, away and its schedule - turn the dial on the heater to change the temperature itself.'
          : `Away is a fixed ${AWAY_TEMP_LABEL()} anti-frost temperature set by Nobø and cannot be changed per room. To hold this room warmer while you are away, put it on Eco, or list it under Settings as a room that must not get cold.`}</div>`}
        <div class="mode-row" style="margin-top:1rem" role="group" aria-label="Mode for this room">
          ${['comfort', 'eco', 'away', 'normal'].map(m => `
            <button class="mode-btn" type="button" data-zmode="${m}"
              aria-pressed="${(zone.current_mode || 'normal') === m}">
              ${esc((Nobo.MODES[m] || {}).label || m)}
            </button>`).join('')}
        </div>
      </section>

      <section class="card">
        <h2>Heaters in this room (${devices.length})</h2>
        ${devices.length ? `<ul class="dev-list">${devices.map(devRow).join('')}</ul>`
          : `<p class="zd-sub">No heaters are assigned to this room. Add one by typing the
             12-digit serial printed on it.</p>`}
        <div class="sheet-actions">
          <button class="btn" type="button" data-act="add-device">Add a heater</button>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <h2>This room's week</h2>
          <button class="btn" type="button" data-act="edit-week">Edit week</button>
        </div>
        ${renderSchedule()}
      </section>

      <section class="card">
        <h2>Room settings</h2>
        <div class="sheet-actions">
          <button class="btn" type="button" data-act="rename-zone">Rename room</button>
          <button class="btn btn-danger" type="button" data-act="delete-zone">Delete room</button>
        </div>
      </section>`;

    root.querySelectorAll('[data-zstep]').forEach(b => {
      b.onclick = () => stepZone(zone.zone_id, b.dataset.zstep === 'up' ? 0.5 : -0.5);
    });
    root.querySelectorAll('[data-zmode]').forEach(b => {
      b.onclick = async () => {
        hold();
        try {
          await Nobo.api.setOverride(zone.zone_id, b.dataset.zmode);
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      };
    });
    root.querySelectorAll('[data-remove-device]').forEach(b => {
      b.onclick = () => removeDevice(b.dataset.removeDevice);
    });
    root.querySelectorAll('[data-move-device]').forEach(b => {
      b.onclick = () => moveDevice(b.dataset.moveDevice);
    });
    root.querySelectorAll('[data-replace-device]').forEach(b => {
      b.onclick = () => replaceDevice(b.dataset.replaceDevice);
    });
    const addBtn = root.querySelector('[data-act="add-device"]');
    if (addBtn) addBtn.onclick = () => addDeviceSheet(zone);
    root.querySelector('[data-act="rename-zone"]').onclick = () => renameZone(zone);
    root.querySelector('[data-act="delete-zone"]').onclick = () => deleteZone(zone);
    const weekBtn = root.querySelector('[data-act="edit-week"]');
    if (weekBtn) {
      weekBtn.disabled = !state.schedule;
      weekBtn.onclick = () => editWeek(zone);
    }
  }

  function devRow(d) {
    const manual = Nobo.isManualDevice(d);
    const tags = [
      manual
        ? `<span class="badge badge-manual" title="This heater has no remote temperature control. Turn the dial on the heater to change its temperature.">Dial on heater</span>`
        : `<span class="badge badge-ok">Adjustable</span>`,
      d.current_mode ? `<span class="badge badge-mode-${esc(d.current_mode)}">${esc((Nobo.MODES[d.current_mode] || {}).label || d.current_mode)}</span>` : '',
    ].join('');

    return `
      <li class="dev">
        <span class="np-device">${Nobo.deviceImg(d.serial, '', (d.display_name || d.name || 'Heater') + ' - ' + (d.device_type || 'heating device'))}</span>
        <div>
          <div class="dev-name">${esc(d.display_name || d.name || d.device_type || 'Heater')}</div>
          <div class="dev-meta">${esc(d.device_type || 'Unknown model')} &middot; ${esc(d.serial_display || d.serial)}</div>
          <div class="dev-tags">${tags}</div>
        </div>
        <div class="dev-actions">
          <button class="btn" type="button" data-move-device="${esc(d.serial)}">Move</button>
          <button class="btn" type="button" data-replace-device="${esc(d.serial)}">Replace</button>
          <button class="btn btn-danger" type="button" data-remove-device="${esc(d.serial)}">Remove</button>
        </div>
      </li>`;
  }

  function renderSchedule() {
    if (!state.schedule) return `<p class="zd-sub">Loading the weekly schedule…</p>`;
    const days = [['monday', 'Mon'], ['tuesday', 'Tue'], ['wednesday', 'Wed'], ['thursday', 'Thu'],
                  ['friday', 'Fri'], ['saturday', 'Sat'], ['sunday', 'Sun']];
    const rows = days.map(([key, label]) => {
      const blocks = state.schedule[key] || [];
      const segs = blocks.map(b => {
        const from = Nobo.minutesOf(b.start);
        const to = Nobo.minutesOf(b.end);
        const w = Math.max(0, (to - from)) / 14.4;
        return `<span class="sched-seg m-${esc(b.mode)}" style="width:${w}%"
          title="${esc(b.start)}-${esc(b.end)} ${esc(b.mode)}"></span>`;
      }).join('');
      return `<div class="sched-day"><span>${label}</span><div class="sched-bar">${segs}</div></div>`;
    }).join('');

    const shared = (state.scheduleMeta && state.scheduleMeta.shared_with_zones) || [];
    const sharedNote = shared.length
      ? `<div class="note note-warn">This week is shared with ${esc(shared.join(', '))}.
         Editing it here changes those rooms too.</div>`
      : '';

    return `<div class="sched">${rows}</div>
      <div class="sched-key">
        <span><i style="background:var(--m-comfort)"></i>Comfort</span>
        <span><i style="background:var(--m-eco)"></i>Eco</span>
        <span><i style="background:var(--m-away)"></i>Away · ${AWAY_TEMP_LABEL()}</span>
        <span><i style="background:var(--off)"></i>Off</span>
      </div>
      <p class="zd-sub sched-away-note">${AWAY_EXPLAINER()}</p>
      ${sharedNote}`;
  }

  /* ------------------------------------------------------------------
   * Editing the week
   *
   * The hub models a week as switch points - "from this moment, be in this
   * state" - and the server insists every day is covered from 00:00 to 24:00
   * with no gaps or overlaps. Editing blocks with a start AND an end makes it
   * far too easy to build a week the hub rejects, so the editor works in
   * switch points directly: a day is a list of "from HH:MM, <mode>" rows, the
   * first one pinned to 00:00. Gaps are then impossible by construction and
   * the payload is derived on save.
   * ---------------------------------------------------------------- */

  const SCHED_DAYS = [
    ['monday', 'Mon'], ['tuesday', 'Tue'], ['wednesday', 'Wed'], ['thursday', 'Thu'],
    ['friday', 'Fri'], ['saturday', 'Sat'], ['sunday', 'Sun'],
  ];
  const SCHED_MODES = [['comfort', 'Comfort'], ['eco', 'Eco'], ['away', 'Away'], ['off', 'Off']];

  /** Blocks -> switch points. Only the start of each block carries meaning. */
  function pointsOfDay(blocks) {
    const pts = (blocks || []).map(b => ({ at: b.start, mode: b.mode }));
    if (!pts.length || pts[0].at !== '00:00') pts.unshift({ at: '00:00', mode: 'eco' });
    return pts;
  }

  /** Switch points -> blocks, with each ending where the next begins. */
  function blocksOfPoints(pts) {
    const sorted = pts.slice().sort((a, b) => Nobo.minutesOf(a.at) - Nobo.minutesOf(b.at));
    return sorted.map((p, i) => ({
      start: p.at,
      end: i + 1 < sorted.length ? sorted[i + 1].at : '24:00',
      mode: p.mode,
    }));
  }

  const snap15 = (hhmm) => {
    const m = Nobo.minutesOf(hhmm);
    if (m == null || isNaN(m)) return null;
    const s = Math.min(1425, Math.max(0, Math.round(m / 15) * 15));
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  };

  function editWeek(zone) {
    if (!state.schedule) { Nobo.toast('The weekly schedule has not loaded yet', 'error'); return; }

    const draft = {};
    SCHED_DAYS.forEach(([key]) => { draft[key] = pointsOfDay(state.schedule[key]); });
    let day = SCHED_DAYS[new Date().getDay() === 0 ? 6 : new Date().getDay() - 1][0];

    const shared = (state.scheduleMeta && state.scheduleMeta.shared_with_zones) || [];

    openSheet(`${zone.name} · weekly schedule`, `
      ${shared.length ? `<div class="note note-warn">This schedule is shared with
        ${esc(shared.join(', '))}. Saving changes those rooms as well.</div>` : ''}
      <p class="zd-sub">Each row says what the room does from that time until the next
      change. The day always starts at 00:00, so there can never be a gap.</p>

      <div class="day-tabs" role="tablist" aria-label="Day of the week">
        ${SCHED_DAYS.map(([k, l]) => `<button class="day-tab" type="button" role="tab"
          data-day="${k}" aria-selected="false">${l}</button>`).join('')}
      </div>

      <div class="sched-bar sched-preview" id="weekPreview"></div>

      <div id="weekRows" class="week-rows"></div>

      <p class="zd-sub away-hint" id="weekAwayHint" hidden>${AWAY_EXPLAINER()}</p>

      <div class="week-tools">
        <button class="btn" type="button" data-act="add">Add a change</button>
        <select id="weekCopy" aria-label="Copy this day to other days">
          <option value="">Copy this day to…</option>
          <option value="week">Monday to Friday</option>
          <option value="weekend">Saturday and Sunday</option>
          <option value="all">Every day</option>
        </select>
      </div>

      <div class="sheet-actions">
        <button class="btn" data-act="dismiss" type="button">Close</button>
        <button class="btn btn-primary" data-act="save" type="button">Save schedule</button>
      </div>
    `, (root) => {
      const rowsEl = root.querySelector('#weekRows');

      const paint = () => {
        root.querySelectorAll('.day-tab').forEach(t => {
          t.setAttribute('aria-selected', String(t.dataset.day === day));
        });

        const pts = draft[day].slice().sort((a, b) => Nobo.minutesOf(a.at) - Nobo.minutesOf(b.at));
        draft[day] = pts;

        root.querySelector('#weekPreview').innerHTML = blocksOfPoints(pts).map(b => {
          const w = Math.max(0, Nobo.minutesOf(b.end) - Nobo.minutesOf(b.start)) / 14.4;
          return `<span class="sched-seg m-${esc(b.mode)}" style="width:${w}%"
            title="${esc(b.start)}-${esc(b.end)} ${esc(b.mode)}"></span>`;
        }).join('');

        rowsEl.innerHTML = pts.map((p, i) => `
          <div class="week-row">
            <label class="week-from">
              <span>From</span>
              ${i === 0
                ? `<input type="time" value="00:00" disabled title="Every day starts at midnight">`
                : `<input type="time" step="900" value="${esc(p.at)}" data-at="${i}">`}
            </label>
            <label class="week-mode">
              <span>Run</span>
              <select data-mode="${i}">
                ${SCHED_MODES.map(([v, l]) => `<option value="${v}"${p.mode === v ? ' selected' : ''}>${l}</option>`).join('')}
              </select>
            </label>
            <button class="btn btn-danger week-del" type="button" data-del="${i}"
              ${i === 0 ? 'disabled title="The first change of the day cannot be removed"' : ''}
              aria-label="Remove this change">&times;</button>
          </div>`).join('');

        rowsEl.querySelectorAll('[data-at]').forEach(inp => {
          inp.onchange = () => {
            const i = Number(inp.dataset.at);
            const snapped = snap15(inp.value);
            if (!snapped) { paint(); return; }
            if (snapped === '00:00') {
              Nobo.toast('00:00 is already the start of the day', 'error');
              paint(); return;
            }
            if (draft[day].some((q, j) => j !== i && q.at === snapped)) {
              Nobo.toast('There is already a change at ' + snapped, 'error');
              paint(); return;
            }
            if (snapped !== inp.value) Nobo.toast('The hub only accepts quarter hours - moved to ' + snapped);
            draft[day][i].at = snapped;
            paint();
          };
        });
        rowsEl.querySelectorAll('[data-mode]').forEach(sel => {
          sel.onchange = () => { draft[day][Number(sel.dataset.mode)].mode = sel.value; paint(); };
        });

        // The 7 C explanation only earns its space once Away is actually in use.
        const hint = root.querySelector('#weekAwayHint');
        if (hint) hint.hidden = !pts.some(p => p.mode === 'away');
        rowsEl.querySelectorAll('[data-del]').forEach(b => {
          b.onclick = () => { draft[day].splice(Number(b.dataset.del), 1); paint(); };
        });
      };

      root.querySelectorAll('.day-tab').forEach(t => {
        t.onclick = () => { day = t.dataset.day; paint(); };
      });

      root.querySelector('[data-act="add"]').onclick = () => {
        /* Drop the new change in the middle of the longest stretch of the day
           and flip the mode, so one tap produces a change you can see rather
           than a second switch point that does nothing. */
        const pts = draft[day];
        const bounds = pts.map(p => Nobo.minutesOf(p.at)).concat(1440);
        let best = -1, at = null, splitMode = pts[0].mode;
        for (let i = 0; i < pts.length; i++) {
          const span = bounds[i + 1] - bounds[i];
          const mid = Math.round((bounds[i] + bounds[i + 1]) / 2 / 15) * 15;
          if (span > best && mid > bounds[i] && mid < bounds[i + 1]) {
            best = span; at = mid; splitMode = pts[i].mode;
          }
        }
        if (at == null) { Nobo.toast('This day has no room left for another change', 'error'); return; }
        const hhmm = String(Math.floor(at / 60)).padStart(2, '0') + ':' + String(at % 60).padStart(2, '0');
        draft[day].push({ at: hhmm, mode: splitMode === 'comfort' ? 'eco' : 'comfort' });
        paint();
      };

      root.querySelector('#weekCopy').onchange = (e) => {
        const which = e.target.value;
        e.target.value = '';
        if (!which) return;
        const targets = which === 'week' ? SCHED_DAYS.slice(0, 5).map(d => d[0])
          : which === 'weekend' ? SCHED_DAYS.slice(5).map(d => d[0])
          : SCHED_DAYS.map(d => d[0]);
        targets.forEach(t => { draft[t] = draft[day].map(p => ({ at: p.at, mode: p.mode })); });
        Nobo.toast('Copied to ' + targets.length + ' days');
        paint();
      };

      root.querySelector('[data-act="dismiss"]').onclick = closeSheet;
      root.querySelector('[data-act="save"]').onclick = async () => {
        const payload = {};
        for (const [key, label] of SCHED_DAYS) {
          const pts = draft[key];
          const times = pts.map(p => p.at);
          if (new Set(times).size !== times.length) {
            Nobo.toast(label + ' has two changes at the same time', 'error'); return;
          }
          if (!times.includes('00:00')) { Nobo.toast(label + ' has to start at 00:00', 'error'); return; }
          payload[key] = blocksOfPoints(pts);
        }
        const btn = root.querySelector('[data-act="save"]');
        btn.disabled = true;
        hold(6000);
        try {
          await Nobo.api.setSchedule(zone.zone_id, { schedule: payload });
          closeSheet();
          Nobo.toast('Weekly schedule saved');
          await loadSchedule(zone.zone_id);
          await refresh(true);
        } catch (e) {
          btn.disabled = false;
          Nobo.toast(e.message, 'error');
        }
      };

      paint();
    });
  }

  /* ------------------------------------------------------------------
   * Device and room management
   * ---------------------------------------------------------------- */

  function removeDevice(serial) {
    const d = state.devices.find(x => x.serial === serial);
    confirmSheet('Remove this heater?',
      `${(d && (d.display_name || d.name)) || serial} is removed from the system. You can add it again later.`,
      'Remove', async () => {
        try {
          await Nobo.api.removeDevice(serial);
          Nobo.toast('Heater removed');
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      }, true);
  }

  function moveDevice(serial) {
    const d = state.devices.find(x => x.serial === serial);
    const options = state.zones
      .map(z => `<option value="${esc(z.zone_id)}" ${String(z.zone_id) === String(d && d.zone_id) ? 'selected' : ''}>${esc(z.name)}</option>`)
      .join('');
    openSheet('Move heater', `
      <p class="zd-sub">${esc((d && (d.display_name || d.name)) || serial)}</p>
      <label class="field"><span>Room</span><select id="mvZone">${options}</select></label>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
        <button class="btn btn-primary" data-act="ok" type="button">Move</button>
      </div>`, (root) => {
      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      root.querySelector('[data-act="ok"]').onclick = async () => {
        const zoneId = root.querySelector('#mvZone').value;
        try {
          await Nobo.api.moveDevice(serial, { zone_id: zoneId });
          closeSheet();
          Nobo.toast('Heater moved');
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      };
    });
  }

  /**
   * Adding a heater.
   *
   * There are two ways in, and an earlier draft of this concept confused them.
   *
   *   - **Manual registration** types the 12-digit serial printed on the
   *     heater. It is `POST /api/devices` and it needs no radio, so it works
   *     in every mode. This is the only way in for the many Nobø devices that
   *     do not answer an automatic search at all (see Manual_Nobo.pdf), and it
   *     is how the original app and the Nobø app both do it.
   *   - **Automatic search** asks the hub to listen for devices in pairing
   *     mode. That needs the hub's radio, so `/api/capabilities` reports it as
   *     unavailable in demo mode.
   *
   * Only the second is ever gated. The manual form is always present and
   * always enabled; when search is unavailable the sheet says so in one line
   * and leaves the form alone.
   *
   * The first three digits of the serial identify the model, so the model name
   * and its picture are shown as soon as they are typed - the same
   * confirmation the original app gives, and a check on the digits before the
   * hub is asked to pair.
   */
  function addDeviceSheet(zone) {
    const feat = state.caps && state.caps.features && state.caps.features.discover_devices;
    const canSearch = !feat || feat.supported;

    openSheet('Add a heater', `
      <p class="zd-sub">The heater is added to <strong>${esc(zone.name)}</strong>.</p>

      <div class="add-dev-section">
        <h3 class="add-dev-h">Type the serial number</h3>
        <p class="zd-sub">Every Nobø heater has a 12-digit serial printed on a label on
        the device. Not all models answer an automatic search, so this always works.</p>
        <label class="field">
          <span>Serial number</span>
          <input type="text" id="adSerial" inputmode="numeric" autocomplete="off"
            maxlength="16" placeholder="210 000 016 247">
          <small class="field-hint">Spaces are fine.</small>
        </label>
        <div class="dev-detect" id="adDetect" aria-live="polite"></div>
        <label class="field">
          <span>Name (optional)</span>
          <input type="text" id="adName" autocomplete="off" placeholder="e.g. Window heater">
        </label>
        <div class="sheet-actions">
          <button class="btn" data-act="cancel" type="button">Cancel</button>
          <button class="btn btn-primary" data-act="ok" type="button" disabled>Add heater</button>
        </div>
      </div>

      <div class="add-dev-section">
        <h3 class="add-dev-h">Or let the hub find it</h3>
        ${canSearch
          ? `<p class="zd-sub">Put the heater into pairing mode, then start the search.
             Devices that do not support pairing will not appear - type the serial above
             instead.</p>
             <div class="sheet-actions">
               <button class="btn" id="adSearch" type="button">Search for heaters</button>
               <button class="btn" id="adSearchStop" type="button" hidden>Stop</button>
             </div>
             <p class="zd-sub" id="adSearchStatus"></p>
             <ul class="dev-list" id="adSearchResults"></ul>`
          : `<div class="note note-warn">${esc(feat.reason ||
             'Searching for nearby heaters needs the hub\u2019s radio.')}</div>
             <p class="zd-sub">Typing the serial above still works.</p>`}
      </div>`, (root) => {

      const serialEl = root.querySelector('#adSerial');
      const detectEl = root.querySelector('#adDetect');
      const okBtn    = root.querySelector('[data-act="ok"]');

      /** Recognise the model from the serial prefix and show its picture. */
      function detect() {
        const serial = serialEl.value.replace(/\s/g, '');
        const model = serial.length >= 3 ? Nobo.deviceModel(serial) : null;

        if (serial.length < 3) {
          detectEl.className = 'dev-detect';
          detectEl.innerHTML = '';
        } else if (model) {
          detectEl.className = 'dev-detect is-known';
          detectEl.innerHTML =
            `<span class="np-device">${Nobo.deviceImg(serial, '', model.name + ' heating device')}</span>` +
            `<span><strong>${esc(model.name)}</strong><br>Recognised from the first three digits</span>`;
        } else {
          detectEl.className = 'dev-detect is-unknown';
          detectEl.innerHTML =
            `<span>No Nobø model uses the prefix <strong>${esc(serial.slice(0, 3))}</strong>. ` +
            `Check the label - the hub will refuse a serial it does not recognise.</span>`;
        }

        okBtn.disabled = !(serial.length === 12 && model);
      }

      serialEl.oninput = detect;
      detect();

      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      okBtn.onclick = async () => {
        const serial = serialEl.value.replace(/\s/g, '');
        const name = root.querySelector('#adName').value.trim();
        if (serial.length !== 12) { Nobo.toast('A serial number is 12 digits', 'error'); return; }
        okBtn.disabled = true;
        try {
          await Nobo.api.addDevice({ serial, zone_id: zone.zone_id, name: name || undefined });
          closeSheet();
          Nobo.toast('Heater added');
          await refresh(true);
        } catch (e) {
          okBtn.disabled = false;
          Nobo.toast(e.message, 'error');
        }
      };

      if (canSearch) wireDeviceSearch(root, zone);
    });
  }

  /**
   * The hub listens for devices in pairing mode and reports what it hears.
   * It is a poll, not a push, so it has to be stopped again - both when the
   * search ends by itself and when the sheet closes underneath it.
   */
  function wireDeviceSearch(root, zone) {
    const startBtn  = root.querySelector('#adSearch');
    const stopBtn   = root.querySelector('#adSearchStop');
    const statusEl  = root.querySelector('#adSearchStatus');
    const resultsEl = root.querySelector('#adSearchResults');
    let timer = null;

    const stopPolling = () => { if (timer) { clearInterval(timer); timer = null; } };

    const running = (on) => {
      startBtn.hidden = on;
      stopBtn.hidden = !on;
    };

    onSheetClose(() => {
      stopPolling();
      // Leave the hub's radio off if the sheet is dismissed mid-search.
      Nobo.api.stopDeviceSearch().catch(() => {});
    });

    async function poll() {
      let data;
      try {
        data = await Nobo.api.deviceSearch();
      } catch (e) {
        stopPolling();
        running(false);
        statusEl.textContent = e.message;
        return;
      }

      const found = data.devices || [];
      resultsEl.innerHTML = found.map(d => `
        <li class="dev">
          <span class="np-device">${Nobo.deviceImg(d.serial, '', (d.device_type || 'Heating device'))}</span>
          <div>
            <div class="dev-name">${esc(d.device_type || Nobo.deviceName(d.serial))}</div>
            <div class="dev-meta">${esc(d.serial_display || d.serial)}</div>
          </div>
          <div class="dev-actions">
            ${d.already_registered
              ? '<span class="badge badge-manual">Already added</span>'
              : `<button class="btn btn-primary" type="button" data-found="${esc(d.serial)}">Add</button>`}
          </div>
        </li>`).join('');

      resultsEl.querySelectorAll('[data-found]').forEach(b => {
        b.onclick = async () => {
          b.disabled = true;
          try {
            await Nobo.api.addDevice({ serial: b.dataset.found, zone_id: zone.zone_id });
            closeSheet();
            Nobo.toast('Heater added');
            await refresh(true);
          } catch (e) { b.disabled = false; Nobo.toast(e.message, 'error'); }
        };
      });

      if (!data.searching) {
        stopPolling();
        running(false);
        statusEl.textContent = found.length
          ? 'Search finished. Pick a heater above.'
          : 'Search finished. Nothing answered \u2014 check the heater is in pairing mode, or type its serial above.';
      } else {
        statusEl.textContent = `Searching\u2026 found ${found.length}.`;
      }
    }

    startBtn.onclick = async () => {
      statusEl.textContent = 'Starting the search\u2026';
      resultsEl.innerHTML = '';
      try {
        await Nobo.api.startDeviceSearch();
      } catch (e) { statusEl.textContent = e.message; return; }
      running(true);
      statusEl.textContent = 'Searching. Put each heater into pairing mode now.';
      await poll();
      stopPolling();
      timer = setInterval(poll, 2000);
    };

    stopBtn.onclick = async () => {
      stopPolling();
      running(false);
      statusEl.textContent = 'Search stopped.';
      try { await Nobo.api.stopDeviceSearch(); } catch (e) { /* already stopped */ }
    };
  }

  /**
   * Replace a broken heater with a new one.
   *
   * A device's serial cannot be changed, so the server pairs the new one first
   * and only removes the old one once that has succeeded - a failed
   * replacement leaves the room as it was. Removing the old one and adding a
   * new one separately is the safer habit, and the sheet says so.
   */
  function replaceDevice(serial) {
    const d = state.devices.find(x => x.serial === serial);
    const label = (d && (d.display_name || d.name)) || (d && d.serial_display) || serial;

    openSheet('Replace this heater', `
      <p class="zd-sub">The new heater takes over everything <strong>${esc(label)}</strong>
      had - the same room, the same schedule and the same temperatures.</p>
      <label class="field">
        <span>Serial number of the new heater</span>
        <input type="text" id="rpSerial" inputmode="numeric" autocomplete="off"
          maxlength="16" placeholder="210 000 016 247">
        <small class="field-hint">Spaces are fine.</small>
      </label>
      <div class="dev-detect" id="rpDetect" aria-live="polite"></div>
      <div class="note">The old heater is only removed once the new one has paired,
      so a failed replacement changes nothing.</div>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
        <button class="btn btn-primary" data-act="ok" type="button" disabled>Replace</button>
      </div>`, (root) => {

      const serialEl = root.querySelector('#rpSerial');
      const detectEl = root.querySelector('#rpDetect');
      const okBtn    = root.querySelector('[data-act="ok"]');

      function detect() {
        const next = serialEl.value.replace(/\s/g, '');
        const model = next.length >= 3 ? Nobo.deviceModel(next) : null;

        if (next.length < 3) {
          detectEl.className = 'dev-detect';
          detectEl.innerHTML = '';
        } else if (model) {
          detectEl.className = 'dev-detect is-known';
          detectEl.innerHTML =
            `<span class="np-device">${Nobo.deviceImg(next, '', model.name + ' heating device')}</span>` +
            `<span><strong>${esc(model.name)}</strong><br>Recognised from the first three digits</span>`;
        } else {
          detectEl.className = 'dev-detect is-unknown';
          detectEl.innerHTML =
            `<span>No Nobø model uses the prefix <strong>${esc(next.slice(0, 3))}</strong>.</span>`;
        }

        okBtn.disabled = !(next.length === 12 && model && next !== serial);
      }

      serialEl.oninput = detect;
      detect();

      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      okBtn.onclick = async () => {
        const next = serialEl.value.replace(/\s/g, '');
        okBtn.disabled = true;
        try {
          await Nobo.api.updateDevice(serial, { new_serial: next });
          closeSheet();
          Nobo.toast('Heater replaced');
          await refresh(true);
        } catch (e) {
          okBtn.disabled = false;
          Nobo.toast(e.message, 'error');
        }
      };
    });
  }

  function renameZone(zone) {
    openSheet('Rename room', `
      <label class="field"><span>Room name</span>
        <input type="text" id="rzName" value="${esc(zone.name)}" autocomplete="off"></label>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
        <button class="btn btn-primary" data-act="ok" type="button">Save</button>
      </div>`, (root) => {
      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      root.querySelector('[data-act="ok"]').onclick = async () => {
        const name = root.querySelector('#rzName').value.trim();
        if (!name) { Nobo.toast('Give the room a name', 'error'); return; }
        try {
          await Nobo.api.updateZone(zone.zone_id, { name });
          closeSheet();
          Nobo.toast('Room renamed');
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      };
    });
  }

  function deleteZone(zone) {
    confirmSheet('Delete this room?',
      `${zone.name} and its schedule are removed. Its heaters are not deleted.`,
      'Delete room', async () => {
        try {
          await Nobo.api.removeZone(zone.zone_id);
          Nobo.toast('Room deleted');
          showHome();
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      }, true);
  }

  /* ------------------------------------------------------------------
   * Settings - ordered by what actually gets changed
   * ---------------------------------------------------------------- */

  async function showSettings() {
    state.view = 'settings';
    switchView();
    renderSettings();
    try {
      renderSettings(await Nobo.api.me());
    } catch (_) { /* the static render is already correct enough */ }
  }

  function renderSettings(me) {
    const hub = state.hub || {};
    $('#topTitle').textContent = 'Settings';
    $('#topSub').textContent = 'Hub, mode and users';

    $('#viewSettings').innerHTML = `
      <section class="card">
        <h2>Where the data comes from</h2>
        <div class="switch">
          <div class="switch-text">
            <strong>Demo mode</strong>
            <span>Example rooms and heaters, so you can try the app without a hub.</span>
          </div>
          <button class="btn" type="button" data-act="toggle-demo"
            aria-pressed="${hub.demo_mode ? 'true' : 'false'}">
            ${hub.demo_mode ? 'On' : 'Off'}
          </button>
        </div>

        ${hub.demo_mode ? `<div class="note">Nothing you change here reaches a real heater while demo mode is on.</div>` : ''}

        <label class="field">
          <span>Hub serial number</span>
          <input type="text" id="stSerial" value="${esc(hub.serial_display || hub.serial || '')}"
                 inputmode="numeric" autocomplete="off" placeholder="123 456 789 012">
          <small class="field-hint">The 12 digits printed underneath the Nobø Ecohub.</small>
        </label>
        <label class="field">
          <span>Hub IP address</span>
          <input type="text" id="stIp" value="${esc(hub.ip || '')}" autocomplete="off" placeholder="192.168.1.50">
          <small class="field-hint">Leave empty to search the local network automatically.</small>
        </label>
        <div class="sheet-actions">
          <button class="btn btn-primary" type="button" data-act="save-hub">Save hub connection</button>
        </div>
        <div class="note">Changing between demo mode and a real hub signs you out, so the app
        reloads cleanly against the new source.</div>
      </section>

      <section class="card">
        <h2>Rooms that must not get cold</h2>
        <p class="zd-sub">${AWAY_EXPLAINER()} Pick the rooms that should hold their
        Eco temperature instead of dropping to ${AWAY_TEMP_LABEL()} whenever the cabin
        goes Away — a bathroom with pipes, a workshop, a wine store.</p>
        <div id="awayExc" class="exc-list">
          <p class="zd-sub">Loading rooms…</p>
        </div>
        <div class="sheet-actions">
          <button class="btn btn-primary" type="button" data-act="save-exc">Save exceptions</button>
        </div>
        <small class="field-hint">This applies both when you press Away and when a
        planned away period starts while nobody is looking at the app.</small>
      </section>

      <section class="card">
        <h2>Your account</h2>
        <div class="user-row">
          <div><strong id="stUser">${esc((me && (me.username || me.name)) || 'Signed in')}</strong></div>
          <button class="btn" type="button" data-act="signout">Sign out</button>
        </div>
        <div class="sheet-actions">
          <button class="btn" type="button" data-act="open-users">Manage users</button>
        </div>
        <small class="field-hint">User management opens the full settings page in the main app.</small>
      </section>

      <section class="card">
        <h2>About this screen</h2>
        <p class="zd-sub">This is design concept D, an exploration. The live system is at
        <a href="/">the main app</a>.</p>
      </section>`;

    const root = $('#viewSettings');
    root.querySelector('[data-act="toggle-demo"]').onclick = () => toggleDemo(!hub.demo_mode);
    root.querySelector('[data-act="save-hub"]').onclick = saveHub;
    root.querySelector('[data-act="signout"]').onclick = async () => {
      try { await Nobo.api.logout(); } catch (_) {}
      window.location.href = '/login';
    };
    root.querySelector('[data-act="open-users"]').onclick = () => { window.location.href = '/#settings'; };
    root.querySelector('[data-act="save-exc"]').onclick = saveAwayExceptions;
    loadAwayExceptions();
  }

  /* ------------------------------------------------------------------
   * Away exceptions
   *
   * Nobø's Away is a fixed 7 C and cannot be raised, which is a real problem
   * for a room that must stay warmer. The only warmer setting the hub offers
   * is the room's own Eco temperature, so an exception simply means "put this
   * room on Eco while everywhere else goes Away".
   *
   * The list is stored and applied on the server on purpose: an away period
   * starts in a background loop, typically with no browser open, so a purely
   * client-side version would silently do nothing on exactly the trips it was
   * bought for.
   * ---------------------------------------------------------------- */

  async function loadAwayExceptions() {
    const box = $('#awayExc');
    if (!box) return;
    try {
      const data = await Nobo.api.awayExceptions();
      if (typeof data.away_temperature === 'number') AWAY_TEMP = data.away_temperature;
      const chosen = new Set((data.zone_ids || []).map(String));
      const zones = state.zones.length ? state.zones : (await Nobo.api.zones() || []);
      if (!zones.length) {
        box.innerHTML = `<p class="zd-sub">No rooms to choose from yet.</p>`;
        return;
      }
      box.innerHTML = zones.map(z => `
        <label class="exc-row">
          <input type="checkbox" data-exc="${esc(String(z.zone_id))}"
                 ${chosen.has(String(z.zone_id)) ? 'checked' : ''}>
          <span class="exc-name">${esc(z.name)}</span>
          <span class="exc-note">${z.eco_temperature != null
            ? `Eco ${esc(String(z.eco_temperature))}°C`
            : 'Eco temperature'}</span>
        </label>`).join('');
    } catch (e) {
      box.innerHTML = `<p class="zd-sub">Could not load the rooms: ${esc(e.message)}</p>`;
    }
  }

  async function saveAwayExceptions() {
    const box = $('#awayExc');
    if (!box) return;
    const zone_ids = Array.from(box.querySelectorAll('[data-exc]'))
      .filter(cb => cb.checked)
      .map(cb => cb.dataset.exc);
    try {
      const res = await Nobo.api.setAwayExceptions(zone_ids);
      Nobo.toast(zone_ids.length
        ? `${zone_ids.length} room${zone_ids.length > 1 ? 's' : ''} will stay on Eco when away`
        : 'Every room will follow Away');
      if (res && res.applied_now && res.applied_now.length) {
        Nobo.toast('Applied now, because the cabin is away');
      }
      refresh();
    } catch (e) { Nobo.toast(e.message, 'error'); }
  }

  function toggleDemo(next) {
    confirmSheet(next ? 'Switch to demo mode?' : 'Connect to a real hub?',
      next
        ? 'The app shows example rooms instead of your hub. You will be signed out so it reloads cleanly.'
        : 'The app connects to the hub using the serial and IP below. You will be signed out so it reloads cleanly.',
      next ? 'Switch to demo' : 'Connect to hub', async () => {
        const serial = ($('#stSerial') && $('#stSerial').value || '').replace(/\s/g, '');
        const ip = ($('#stIp') && $('#stIp').value || '').trim();
        try {
          await Nobo.api.setHubConfig({ demo_mode: next, serial, ip });
          Nobo.toast(next ? 'Demo mode on' : 'Connecting to the hub');
          try { await Nobo.api.logout(); } catch (_) {}
          window.location.href = '/login';
        } catch (e) { Nobo.toast(e.message, 'error'); }
      });
  }

  async function saveHub() {
    const serial = $('#stSerial').value.replace(/\s/g, '');
    const ip = $('#stIp').value.trim();
    if (serial && serial.length !== 12) { Nobo.toast('A hub serial is 12 digits', 'error'); return; }
    try {
      await Nobo.api.setHubConfig({ demo_mode: !!(state.hub && state.hub.demo_mode), serial, ip });
      Nobo.toast('Hub connection saved');
      await refresh(true);
      renderSettings();
    } catch (e) { Nobo.toast(e.message, 'error'); }
  }

  /* ------------------------------------------------------------------
   * View switching
   * ---------------------------------------------------------------- */

  function switchView() {
    $('#viewHome').hidden     = state.view !== 'home';
    $('#viewZone').hidden     = state.view !== 'zone';
    $('#viewSettings').hidden = state.view !== 'settings';
    $('#btnBack').hidden      = state.view === 'home';
    window.scrollTo(0, 0);
  }

  function showHome() {
    state.view = 'home';
    state.zoneId = null;
    $('#topTitle').textContent = 'Cabin';
    $('#topSub').textContent = 'Nobø Control';
    switchView();
    renderHome();
  }

  $('#btnBack').addEventListener('click', showHome);
  $('#btnSettings').addEventListener('click', () => {
    if (state.view === 'settings') showHome(); else showSettings();
  });

  function renderHome() {
    renderTrip();
    renderModes();
    renderZones();
    renderSystem();
  }

  function renderCurrent() {
    renderLink();
    if (state.view === 'home') renderHome();
    else if (state.view === 'zone') renderZoneDetail();
  }

  async function refresh(force = false) {
    if (!force && held()) return;
    await loadAll();
    renderCurrent();
  }

  /* ------------------------------------------------------------------
   * Boot
   * ---------------------------------------------------------------- */

  (async function boot() {
    await loadAll();
    showHome();
    renderLink();

    Nobo.subscribe(
      (zones) => {
        if (held()) return;              // never redraw mid-gesture
        if (!sheetEl.hidden) return;     // or while a sheet is open
        state.zones = zones;
        renderCurrent();
      },
      () => renderLink(),
    );

    // Keep the trip countdown honest without hammering the API.
    setInterval(() => {
      if (state.view === 'home' && sheetEl.hidden && !held()) renderTrip();
    }, 60000);

    // The away window is server-side state, so re-read it periodically.
    setInterval(async () => {
      if (held() || !sheetEl.hidden) return;
      try {
        state.status = await Nobo.api.status();
        if (state.view === 'home') { renderTrip(); renderSystem(); }
        renderLink();
      } catch (_) { /* the connection pill already reports this */ }
    }, 30000);
  })();

})();
