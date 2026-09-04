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
    `To keep a zone warmer than that, use Eco and set its Eco temperature.`;

  /* ------------------------------------------------------------------
   * State
   * ---------------------------------------------------------------- */

  const state = {
    zones: [],
    devices: [],
    status: null,
    hub: null,
    caps: null,
    view: 'home',        // 'home' | 'zone' | 'log' | 'settings'
    zoneId: null,
    site: null,          // what the household calls this place
    me: null,            // the signed-in user, cached for re-renders
    schedule: null,
    scheduleMeta: null,
    weekProfiles: [],
    /* The command log is only fetched when its view is opened - it is
       diagnostics, and there is no reason to poll for it on the home screen. */
    log: null,
    logError: null,
    logFilter: 'all',
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
    const [zones, status, hub, hubInfo, caps, devices, site, weekProfiles] = await Promise.all([
      Nobo.api.zones().catch(() => []),
      Nobo.api.status().catch(() => null),
      Nobo.api.hubConfig().catch(() => null),
      /* Configuration and identity are two different endpoints: hubConfig is
         what we were told to connect to, this is what the hub says it is. It
         answers 503 while the hub is unreachable, hence the catch. */
      Nobo.api.hub().catch(() => null),
      Nobo.api.capabilities().catch(() => null),
      Nobo.api.devices().catch(() => []),
      Nobo.api.site().catch(() => null),
      Nobo.api.weekProfiles().catch(() => []),
    ]);
    state.zones = zones || [];
    state.status = status;
    state.hub = hub;
    state.hubInfo = hubInfo;
    state.caps = caps;
    state.devices = devices || [];
    state.weekProfiles = weekProfiles || [];
    if (site) state.site = site;
    applySiteName();
  }

  /* ------------------------------------------------------------------
   * What this place is called
   *
   * Two forms are needed and they are not interchangeable. SITE() is the name
   * standing on its own - the header, the trip heading, the page title - and
   * falls back to "Cabin". SITE_IN() is the name mid-sentence - "Warm all of
   * the cabin?" - and falls back to "the cabin", because "Warm all of Cabin?"
   * reads like a bug.
   *
   * Every string below is written to take a name rather than to be one, so a
   * user's name substitutes without any string needing a special case.
   * ---------------------------------------------------------------- */

  const SITE    = () => (state.site && state.site.display_name) || 'Cabin';
  const SITE_IN = () => (state.site && state.site.inline_name) || 'the cabin';

  /* Offered in Settings. Not a limit — the server accepts any language tag —
     but a list saves people looking one up. Nordic first: this is a Nobø
     system, and Nobø is sold mainly in Norway and the rest of the Nordics. */
  const LOCALE_CHOICES = [
    ['nb-NO', 'Norsk (bokmål) — 30. aug. 2026'],
    ['nn-NO', 'Norsk (nynorsk) — 30. aug. 2026'],
    ['sv-SE', 'Svenska — 30 aug. 2026'],
    ['da-DK', 'Dansk — 30. aug. 2026'],
    ['fi-FI', 'Suomi — 30.8.2026'],
    ['is-IS', 'Íslenska — 30. ágú. 2026'],
    ['de-DE', 'Deutsch — 30. Aug. 2026'],
    ['en-GB', 'English (UK) — 30 Aug 2026'],
    ['en-US', 'English (US) — Aug 30, 2026'],
    ['', 'Follow each browser'],
  ];

  function applySiteName() {
    document.title = `${SITE()} - Nobø Control`;
    // iOS reads this when the user adds the app to the home screen, which
    // happens long after load, so updating it here is enough.
    const appleTitle = document.querySelector('meta[name="apple-mobile-web-app-title"]');
    if (appleTitle) appleTitle.setAttribute('content', SITE());
    const tripHeading = $('#tripHeading');
    if (tripHeading) tripHeading.textContent = SITE();
    const modesHeading = $('#modesHeading');
    if (modesHeading) modesHeading.textContent = `All of ${SITE_IN()}`;
    if (state.view === 'home') {
      const t = $('#topTitle');
      if (t) t.textContent = SITE();
    }
    // Dates are written the installation's way, not each browser's, so the
    // same schedule reads identically on every device in the house.
    Nobo.setLocale(state.site && state.site.locale);
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
      el.title = 'Demo mode - example zones and devices, no hub connected.';
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
      detail.textContent  = `Every zone is holding at the away temperature. Normal schedules resume ${Nobo.fmtUntil(a.end_at)}.`;
      drawTimeline(a.start_at, a.end_at);
      actions.innerHTML = `
        <button class="btn btn-primary" data-act="arrive" type="button">I'm back now</button>
        <button class="btn" data-act="plan" type="button">Change return</button>
        <button class="btn btn-danger" data-act="delete-trip" type="button">Delete away period</button>`;

    } else if (a.enabled && a.start_at) {
      card.classList.add('is-away');
      stateEl.textContent = 'Away from ' + Nobo.fmtWhen(a.start_at);
      detail.textContent  = `Starts ${Nobo.fmtUntil(a.start_at)}, back ${Nobo.fmtWhen(a.end_at)}. Until then zones follow their normal schedules.`;
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
        detail.textContent  = 'Every zone is on away and nothing will bring the heating back automatically. Set a return date and it will warm up before you arrive.';
        actions.innerHTML = `
          <button class="btn btn-primary" data-act="arrive" type="button">I'm back now</button>
          <button class="btn" data-act="plan" type="button">Set a return date</button>`;
        wireTripActions(actions);
        return;
      } else if (mode === 'comfort') {
        card.classList.add('is-heat');
        stateEl.textContent = `Warming all of ${SITE_IN()}`;
        detail.textContent  = 'Every zone is held at its comfort temperature until you change it.';
      } else if (mode === 'eco') {
        stateEl.textContent = 'Ticking over on eco';
        detail.textContent  = 'Every zone is held at its eco temperature.';
      } else if (mode === 'mixed') {
        stateEl.textContent = 'Zones set individually';
        detail.textContent  = 'Some zones are overridden and some are following their schedule.';
      } else {
        stateEl.textContent = "Someone's here";
        detail.textContent  = 'Zones are following their normal schedules.';
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
      `The dates are removed and ${SITE_IN()} goes back to its normal schedules straight away.`,
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
      <p class="zd-sub">Every zone drops to Away — a fixed ${AWAY_TEMP_LABEL()} anti-frost
      temperature set by Nobø — and returns to its normal schedule when you get back.
      Zones that must stay warmer can be held on Eco instead, under Settings.</p>

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
        <small class="field-hint">The same as the Away button: every zone holds the away
        temperature until you come back and end it yourself.</small>
      </div>

      ${a.enabled ? `
      <div class="sheet-alt">
        <button class="btn btn-danger btn-wide" data-act="delete" type="button">Delete this away period</button>
        <small class="field-hint">Removes the dates and returns ${SITE_IN()} to its normal schedules.</small>
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
      'The away period ends now and every zone returns to its normal schedule.',
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
      'Every zone drops to the away temperature and stays there until you end it yourself. Any away period you had planned is removed.',
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

  /* The house row is static markup, so its icons are filled in from the same
     set the zone row uses. One source, so the two rows cannot drift apart
     again -- they previously carried different symbols for the same four
     instructions. */
  document.querySelectorAll('.mode-glyph[data-icon]').forEach(el => {
    el.innerHTML = Nobo.icon(el.dataset.icon);
  });

  document.querySelectorAll('[data-global]').forEach(btn => {
    btn.addEventListener('click', () => {
      const mode = btn.dataset.global;
      const labels = {
        home:    ['Back to schedules?', 'Every zone returns to its own weekly schedule.'],
        comfort: [`Warm all of ${SITE_IN()}?`, 'Every zone is held at its comfort temperature until you change it.'],
        eco:     [`All of ${SITE_IN()} on eco?`, 'Every zone is held at its eco temperature.'],
        away:    [`All of ${SITE_IN()} on away?`, 'Every zone drops to the away temperature and stays there until you change it. To have the heating come back on its own, use "I\u2019m leaving" instead.'],
      };
      const [title, msg] = labels[mode];
      confirmSheet(title, msg, 'Yes, ' + mode, async () => {
        hold();
        try {
          await Nobo.api.setGlobalMode(mode);
          Nobo.toast(`All of ${SITE_IN()} set to ` + mode);
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

  function fmtSetpointTemp(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '\u2014';
    return (Number.isInteger(n) ? String(n) : n.toFixed(1)) + '\u00B0C';
  }

  function setpointDriftEntries(zone) {
    const changed = zone.setpoint_changed_outside || {};
    return ['comfort', 'eco']
      .filter(key => changed[key])
      .map(key => ({
        key,
        label: key === 'comfort' ? 'Comfort' : 'Eco',
        intended: changed[key].intended,
        actual: changed[key].actual,
      }));
  }

  function setpointDriftText(zone) {
    return setpointDriftEntries(zone).map(entry =>
      `${entry.label} is ${fmtSetpointTemp(entry.actual)} here, but ${fmtSetpointTemp(entry.intended)} was set in this app.`
    ).join(' ');
  }

  function setpointDriftAction(zone, field) {
    const values = setpointDriftEntries(zone).map(entry => fmtSetpointTemp(entry[field]));
    if (!values.length) return field === 'intended' ? 'Restore' : 'Keep';
    return (field === 'intended' ? 'Restore ' : 'Keep ') + values.join(' / ');
  }

  function renderSetpointDriftBadge(zone) {
    if (!zone.setpoint_changed_outside) return '';
    return `<span class="badge badge-drift" title="${esc(setpointDriftText(zone))} Open the zone to restore or keep it.">Changed outside app</span>`;
  }

  function renderSetpointDrift(zone) {
    if (!zone.setpoint_changed_outside) return '';
    return `
      <div class="note note-warn">
        <strong>Changed outside this app</strong>
        <div>${esc(setpointDriftText(zone))} That change was made on a heater or in the Nobø app — the hub does not record which.</div>
        <div class="sheet-actions" style="margin-top:.7rem">
          <button class="btn btn-primary" type="button" data-setpoint-action="restore" data-zone="${esc(zone.zone_id)}">${esc(setpointDriftAction(zone, 'intended'))}</button>
          <button class="btn" type="button" data-setpoint-action="accept" data-zone="${esc(zone.zone_id)}">${esc(setpointDriftAction(zone, 'actual'))}</button>
        </div>
      </div>`;
  }

  async function postSetpointDecision(zoneId, action) {
    const path = `/api/zones/${encodeURIComponent(zoneId)}/${action === 'restore' ? 'restore-setpoints' : 'accept-setpoints'}`;
    const res = await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
    });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body && body.detail) detail = body.detail;
      } catch (_) { /* response had no JSON body */ }
      throw new Error(detail);
    }
    return res.json();
  }

  async function handleSetpointDecision(zoneId, action) {
    hold();
    try {
      await postSetpointDecision(zoneId, action);
      Nobo.toast(action === 'restore' ? 'Setpoints restored' : 'Setpoints kept');
      await refresh(true);
    } catch (e) { Nobo.toast(e.message, 'error'); }
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
      ? `<span class="badge badge-manual" title="No heater in this zone can be adjusted from here. Turn the dial on the heater to change its temperature.">Set on heater</span>`
      : (zone.has_manual_devices
          ? `<span class="badge badge-manual" title="Some heaters in this zone have no remote temperature control. Their temperature is set by a dial on the heater itself.">Some dial-only</span>`
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
          ? 'Turn the dial on the heater to change this zone'
          : 'Away uses a fixed system temperature');

    return `
      <li class="zone" data-zone="${esc(zone.zone_id)}">
        <button class="zone-open" type="button" data-open="${esc(zone.zone_id)}">
          <span>${esc(zone.name)}</span><span class="chev" aria-hidden="true">›</span>
        </button>
        <div class="zone-meta">${modeBadge}${manualBadge}${renderSetpointDriftBadge(zone)}</div>
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
      list.innerHTML = `<li class="zone"><div class="zone-meta">No zones yet.
        Add one, then put its heaters in it.</div></li>`;
      $('#roomsNote').textContent = '';
      return;
    }
    list.innerHTML = state.zones.map(zoneRow).join('');
    const manual = state.zones.filter(z => z.has_manual_devices).length;
    const zoneCount = `${state.zones.length} ${state.zones.length === 1 ? 'zone' : 'zones'}`;
    $('#roomsNote').textContent = manual
      ? `${zoneCount} · ${manual} with a dial-only heater`
      : zoneCount;

    list.querySelectorAll('[data-open]').forEach(b => {
      b.onclick = () => showZone(b.dataset.open);
    });
    list.querySelectorAll('[data-step]').forEach(b => {
      b.onclick = () => stepZone(b.dataset.zone, b.dataset.step === 'up' ? 1 : -1);
    });
    list.querySelectorAll('[data-setpoint-action]').forEach(b => {
      b.onclick = () => handleSetpointDecision(b.dataset.zone, b.dataset.setpointAction);
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

  /**
   * Move a zone's set point by whole degrees.
   *
   * The hub stores set points as whole degrees, so half-degree steps were never
   * reachable: pressing + moved the room a full degree, and pressing - moved it
   * nowhere at all, because the server rounds to nearest and 18 - 0.5 rounds
   * straight back to 18. The minus button had simply never worked.
   *
   * The current value is rounded before stepping as well, so a half degree
   * arriving from the official app cannot produce another one here.
   */
  function stepZone(zoneId, delta) {
    const zone = state.zones.find(z => String(z.zone_id) === String(zoneId));
    if (!zone) return;
    const key = setpointKey(zone);
    if (!key) return;
    const field = key === 'eco' ? 'eco_temperature' : 'comfort_temperature';
    const next = Nobo.clampTemp(Math.round(zone[field] ?? 20) + delta);
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
    const hub = state.hub || {};
    const info = state.hubInfo || {};
    const rows = [
      ['Zones', String(s.zoneCount)],
      ['Average temperature', s.averageTemp == null ? 'No sensors' : Nobo.fmtTemp(s.averageTemp) + '\u00B0'],
      ['Coldest zone', s.coldest ? `${s.coldest.name} at ${Nobo.fmtTemp(s.coldest.current_temperature)}\u00B0` : 'Unknown'],
      ['Likely heating now', `${s.heatingCount} of ${s.zoneCount} (estimated from temperatures)`],
      ['Zones overridden', String(s.overriddenCount)],
      ['Hub', hub.demo_mode ? 'Demo mode' : (hub.serial_display || 'Unknown')],
      ['Time zone', st.timezone || 'Unknown'],
    ];
    /* Everything the hub will tell us about itself. Only shown when it is
       actually there: the firmware version in particular is worth being able to
       read without the official app, because 115 has a fault that stops the hub
       reaching the update service and the only outward sign is a blinking LED. */
    for (const [label, value] of [
      ['Hub name', info.name],
      ['Firmware', info.software_version],
      ['Hardware', info.hardware_version],
      ['Made', fmtHubDate(info.production_date)],
      ['Protocol', info.api_version],
      ['Hub address', hub.ip],
    ]) {
      if (value) rows.push([label, String(value)]);
    }
    $('#sysGrid').innerHTML = rows
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  }

  /* The server sends an ISO date; the household's own format is a browser
     setting, so the conversion belongs here rather than in the API. */
  function fmtHubDate(iso) {
    if (!iso) return '';
    const d = new Date(`${iso}T00:00:00`);
    return isNaN(d) ? iso : d.toLocaleDateString();
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
    if (!zone) { root.innerHTML = `<div class="card">This zone is no longer available.</div>`; return; }

    $('#topTitle').textContent = zone.name;
    $('#topSub').textContent = 'Zone';

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
      ? 'The temperature in this zone is set by the dial on each heater'
      : (zone.current_temperature == null
          ? 'No temperature sensor in this zone'
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
          ? 'No heater in this zone can be adjusted from here. You can still switch the zone between comfort, eco, away and its schedule - turn the dial on the heater to change the temperature itself.'
          : `Away is a fixed ${AWAY_TEMP_LABEL()} anti-frost temperature set by Nobø and cannot be changed per zone. To hold this zone warmer while you are away, put it on Eco, or list it under Settings as a zone that must not get cold.`}</div>`}
        ${renderSetpointDrift(zone)}
        <div class="mode-row" style="margin-top:1rem" role="group" aria-label="Mode for this zone">
          ${['comfort', 'eco', 'away', 'normal'].map(m => `
            <button class="mode-btn" type="button" data-zmode="${m}"
              aria-pressed="${(zone.current_mode || 'normal') === m}">
              <span class="mode-glyph" aria-hidden="true">${Nobo.icon(m)}</span>
              ${esc((Nobo.MODES[m] || {}).label || m)}
            </button>`).join('')}
        </div>
      </section>

      <section class="card">
        <h2>Heaters in this zone (${devices.length})</h2>
        ${devices.length ? `<ul class="dev-list">${devices.map(devRow).join('')}</ul>`
          : `<p class="zd-sub">No heaters are assigned to this zone. Add one by typing the
             12-digit serial printed on it.</p>`}
        <div class="sheet-actions">
          <button class="btn" type="button" data-act="add-device">Add a heater</button>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <h2>This zone's week</h2>
          <button class="btn" type="button" data-act="edit-week">Edit week</button>
        </div>
        ${renderScheduleSummary()}
        <div class="sheet-actions schedule-actions">
          <button class="btn" type="button" data-act="change-schedule" ${state.scheduleMeta ? '' : 'disabled'}>
            Use a different schedule
          </button>
        </div>
        ${renderSchedule()}
      </section>

      <section class="card">
        <h2>Zone settings</h2>
        <div class="sheet-actions">
          <button class="btn" type="button" data-act="rename-zone">Rename zone</button>
          <button class="btn btn-danger" type="button" data-act="delete-zone">Delete zone</button>
        </div>
      </section>`;

    root.querySelectorAll('[data-zstep]').forEach(b => {
      b.onclick = () => stepZone(zone.zone_id, b.dataset.zstep === 'up' ? 1 : -1);
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
    root.querySelectorAll('[data-setpoint-action]').forEach(b => {
      b.onclick = () => handleSetpointDecision(b.dataset.zone, b.dataset.setpointAction);
    });
    root.querySelectorAll('[data-remove-device]').forEach(b => {
      b.onclick = () => removeDevice(b.dataset.removeDevice);
    });
    root.querySelectorAll('[data-move-device]').forEach(b => {
      b.onclick = () => moveDevice(b.dataset.moveDevice);
    });
    root.querySelectorAll('[data-rename-device]').forEach(b => {
      b.onclick = () => renameDevice(b.dataset.renameDevice);
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
      weekBtn.onclick = () => editZoneWeek(zone);
    }
    const changeScheduleBtn = root.querySelector('[data-act="change-schedule"]');
    if (changeScheduleBtn) changeScheduleBtn.onclick = () => changeZoneSchedule(zone);
  }

  function devRow(d) {
    const manual = Nobo.isManualDevice(d);
    const name = d.display_name || d.name || d.device_type || 'Heater';
    const tags = [
      manual
        ? `<span class="badge badge-manual" title="This heater has no remote temperature control. Turn the dial on the heater to change its temperature.">Dial on heater</span>`
        : `<span class="badge badge-ok">Adjustable</span>`,
      d.current_mode ? `<span class="badge badge-mode-${esc(d.current_mode)}">${esc((Nobo.MODES[d.current_mode] || {}).label || d.current_mode)}</span>` : '',
    ].join('');

    return `
      <li class="dev">
        <span class="np-device">${Nobo.deviceImg(d.serial, '', name + ' - ' + (d.device_type || 'heating device'))}</span>
        <div>
          <div class="dev-name">${esc(name)}</div>
          <div class="dev-meta">${esc(d.device_type || 'Unknown model')} &middot; ${esc(d.serial_display || d.serial)}</div>
          <div class="dev-tags">${tags}</div>
        </div>
        <div class="dev-actions">
          <button class="icon-btn act-rename" type="button" data-rename-device="${esc(d.serial)}"
            title="Rename this heater" aria-label="Rename ${esc(name)}">${Nobo.icon('rename')}</button>
          <button class="icon-btn act-move" type="button" data-move-device="${esc(d.serial)}"
            title="Move to another zone" aria-label="Move ${esc(name)} to another zone">${Nobo.icon('move')}</button>
          <button class="icon-btn act-replace" type="button" data-replace-device="${esc(d.serial)}"
            title="Replace with a different heater" aria-label="Replace ${esc(name)}">${Nobo.icon('replace')}</button>
          <button class="icon-btn act-remove" type="button" data-remove-device="${esc(d.serial)}"
            title="Remove from the hub" aria-label="Remove ${esc(name)}">${Nobo.icon('remove')}</button>
        </div>
      </li>`;
  }

  /* Monday first: the week starts on Monday everywhere this is sold, and a
     heating schedule reads oddly beginning on Sunday. Nobo.dayNames() returns
     English labels — the app's language, not the date format's. */
  function scheduleDays() {
    const byKey = Object.fromEntries(Nobo.dayNames('short'));
    return ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
      .map(key => [key, byKey[key] || key]);
  }

  function listSentence(items) {
    const clean = (items || []).filter(Boolean);
    if (clean.length <= 1) return clean[0] || '';
    if (clean.length === 2) return clean.join(' and ');
    return clean.slice(0, -1).join(', ') + ' and ' + clean[clean.length - 1];
  }

  function renderScheduleSummary() {
    if (!state.scheduleMeta) return `<p class="zd-sub">Loading schedule details…</p>`;
    const name = state.scheduleMeta.week_profile_name || state.scheduleMeta.name || 'Unnamed schedule';
    const shared = (state.scheduleMeta && state.scheduleMeta.shared_with_zones) || [];
    return `<p class="zd-sub schedule-follow">
      ${Nobo.icon('normal')} <span>Follows &quot;${esc(name)}&quot;${shared.length
        ? ` — also used by ${esc(listSentence(shared))}`
        : ''}</span>
    </p>`;
  }

  function profileUsage(profile) {
    const used = (profile && profile.used_by) || [];
    return used.length ? 'Used by ' + listSentence(used.map(z => typeof z === 'string' ? z : (z.name || z.zone_id))) : 'Not used';
  }

  async function changeZoneSchedule(zone) {
    let profiles = state.weekProfiles || [];
    try {
      profiles = await Nobo.api.weekProfiles();
      state.weekProfiles = profiles || [];
    } catch (e) {
      Nobo.toast(e.message, 'error');
      return;
    }

    const currentId = String((state.scheduleMeta && state.scheduleMeta.week_profile_id) || '');
    openSheet('Use a different schedule', `
      <p class="zd-sub">Choose the schedule ${esc(zone.name)} should follow.</p>
      <div class="schedule-choices">
        ${profiles.map(profile => {
          const id = String(profile.profile_id);
          const current = id === currentId;
          const name = profile.name || (profile.profile && profile.profile.name) || 'Unnamed schedule';
          return `<button class="schedule-choice" type="button" data-profile="${esc(id)}" ${current ? 'disabled' : ''}>
            <span class="schedule-choice-icon" aria-hidden="true">${Nobo.icon('normal')}</span>
            <span class="schedule-copy">
              <strong>${esc(name)}${current ? ' · Current' : ''}</strong>
              <small>${esc(profileUsage(profile))}</small>
              ${renderSchedule(profile.schedule, null, { compact: true, showKey: false, unreadable: profile.unreadable, loadingText: 'Schedule details unavailable.' })}
            </span>
          </button>`;
        }).join('') || '<p class="zd-sub">No schedules are available.</p>'}
      </div>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
      </div>`, (root) => {
      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      root.querySelectorAll('[data-profile]').forEach(btn => {
        btn.onclick = async () => {
          btn.disabled = true;
          hold(6000);
          try {
            await Nobo.api.assignWeekProfile(zone.zone_id, btn.dataset.profile);
            closeSheet();
            Nobo.toast('Schedule changed');
            await loadSchedule(zone.zone_id);
            await refresh(true);
          } catch (e) {
            btn.disabled = false;
            Nobo.toast(e.message, 'error');
          }
        };
      });
    });
  }

  function renderSchedule(schedule = state.schedule, meta = state.scheduleMeta, opts = {}) {
    if (opts.unreadable) return `<p class="zd-sub schedule-unreadable">${esc(opts.unreadable)}</p>`;
    if (!schedule) return `<p class="zd-sub">${esc(opts.loadingText || 'Loading the weekly schedule…')}</p>`;
    const days = scheduleDays();
    const hasOff = SCHED_DAY_KEYS.some(key => (schedule[key] || []).some(b => b.mode === 'off'));
    const rows = days.map(([key, label]) => {
      const blocks = schedule[key] || [];
      const segs = blocks.map(b => {
        const from = Nobo.minutesOf(b.start);
        const to = Nobo.minutesOf(b.end);
        const w = Math.max(0, (to - from)) / 14.4;
        return `<span class="sched-seg m-${esc(b.mode)}" style="width:${w}%"
          title="${esc(b.start)}-${esc(b.end)} ${esc(b.mode)}"></span>`;
      }).join('');
      return `<div class="sched-day"><span>${esc(label)}</span><div class="sched-bar">${segs}</div></div>`;
    }).join('');

    /* An hour axis, because a coloured bar with no scale only says "warm in the
       middle" -- you cannot read 09:00 off it. Labelled every six hours, which
       is as many as fits across a phone, with an hourly rule drawn over the
       bars themselves so a block edge can be placed against it. */
    const axis = `<div class="sched-day sched-axis" aria-hidden="true">
      <span></span>
      <div class="sched-scale">
        ${[0, 6, 12, 18, 24].map(h => `<i style="left:${(h / 24) * 100}%">${
          String(h).padStart(2, '0')}</i>`).join('')}
      </div>
    </div>`;

    const shared = (meta && meta.shared_with_zones) || [];
    const sharedNote = !opts.compact && shared.length
      ? `<div class="note note-warn">This schedule is shared with ${esc(listSentence(shared))}.
         When you save, choose whether to change just this zone or every zone using it.</div>`
      : '';

    return `<div class="sched${opts.compact ? ' sched-compact' : ''}">${rows}${axis}</div>
      ${opts.showKey === false ? '' : `<div class="sched-key">
        <span><i style="background:var(--m-comfort)"></i>Comfort</span>
        <span><i style="background:var(--m-eco)"></i>Eco</span>
        <span><i style="background:var(--m-away)"></i>Away · ${AWAY_TEMP_LABEL()}</span>
        ${hasOff ? '<span><i style="background:var(--off)"></i>Off</span>' : ''}
      </div>
      <p class="zd-sub sched-away-note">${AWAY_EXPLAINER()}</p>`}
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

  /* Monday first, so the weekday/weekend slices below stay meaningful.
     Labels are English, the app's language, rather than the date format's. */
  const SCHED_DAY_KEYS = [
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
  ];
  const schedDays = () => scheduleDays();
  const SCHED_MODES = [['comfort', 'Comfort'], ['eco', 'Eco'], ['away', 'Away']];

  /** Blocks -> switch points. Only the start of each block carries meaning. */
  function pointsOfDay(blocks) {
    const pts = (blocks || []).map(b => ({ at: b.start, mode: b.mode }));
    if (!pts.length || pts[0].at !== '00:00') pts.unshift({ at: '00:00', mode: 'eco' });
    return pts;
  }

  function startingWeekSchedule() {
    const schedule = {};
    SCHED_DAY_KEYS.forEach((key) => {
      schedule[key] = [{ start: '00:00', end: '24:00', mode: 'comfort' }];
    });
    return schedule;
  }

  function offeredScheduleModes(currentMode) {
    const modes = SCHED_MODES.slice();
    if (currentMode === 'off') modes.push(['off', 'Off']);
    return modes;
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

  function editZoneWeek(zone) {
    if (!state.schedule) { Nobo.toast('The weekly schedule has not loaded yet', 'error'); return; }
    const shared = (state.scheduleMeta && state.scheduleMeta.shared_with_zones) || [];
    editWeek({
      title: `${zone.name} · weekly schedule`,
      schedule: state.schedule,
      beforeHtml: `
        ${shared.length ? `<div class="note note-warn">This schedule is shared with
          ${esc(listSentence(shared))}. You will choose who to change when you save.</div>` : ''}
        <p class="zd-sub">Each row says what the zone does from that time until the next
        change. The day always starts at 00:00, so there can never be a gap.</p>`,
      onSave: async (payload, root) => {
        if (shared.length) {
          chooseScheduleSaveScope(zone, payload, shared);
          return;
        }
        chooseUnsharedScheduleSave(zone, payload);
      },
    });
  }

  function editWeek(editor) {
    const draft = {};
    SCHED_DAY_KEYS.forEach((key) => { draft[key] = pointsOfDay(editor.schedule[key]); });
    let day = SCHED_DAY_KEYS[new Date().getDay() === 0 ? 6 : new Date().getDay() - 1];

    openSheet(editor.title, `
      ${editor.beforeHtml || ''}

      <div class="day-tabs" role="tablist" aria-label="Day of the week">
        ${schedDays().map(([k, l]) => `<button class="day-tab" type="button" role="tab"
          data-day="${k}" aria-selected="false">${esc(l)}</button>`).join('')}
      </div>

      <div class="sched-bar sched-preview" id="weekPreview"></div>
      <!-- The same hour scale the week bars carry. This is the bar you are
           actually editing, so it is the one that most needs a ruler. -->
      <div class="sched-scale sched-preview-scale" aria-hidden="true">
        ${[0, 6, 12, 18, 24].map(h => `<i style="left:${(h / 24) * 100}%">${
          String(h).padStart(2, '0')}</i>`).join('')}
      </div>

      <div id="weekRows" class="week-rows"></div>

      <p class="zd-sub away-hint" id="weekAwayHint" hidden>${AWAY_EXPLAINER()}</p>

      ${editor.nameField ? `<label class="field">
        <span>Schedule name</span>
        <input type="text" id="weekProfileName" value="${esc(editor.nameValue || '')}"
          autocomplete="off" placeholder="${esc(editor.namePlaceholder || 'Weekend warm-up')}">
      </label>` : ''}

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

      /* Drawing is split in two on purpose.
       *
       * The bar and the hint can be redrawn as often as we like -- nobody is
       * touching them. The rows cannot: rebuilding them replaces the very
       * <input> the user is interacting with, and a time input fires `change`
       * *while* a native picker is being spun on a phone or tablet, not only
       * when it is dismissed. Redrawing on every change therefore tore the
       * picker out from under the user's finger, so a time could not be set at
       * all on a touch device and the row list looked frozen.
       *
       * Rows are rebuilt only when the list itself changes shape -- a row added
       * or removed, or a time edit finished and the day needing re-sorting. */
      const paintPreview = () => {
        // Sorted copy, not the draft itself: while a time is being changed the
        // points can be briefly out of order, and the bar should still read
        // correctly without the row order jumping around underneath the finger.
        const pts = draft[day].slice()
          .sort((a, b) => Nobo.minutesOf(a.at) - Nobo.minutesOf(b.at));
        root.querySelector('#weekPreview').innerHTML = blocksOfPoints(pts).map(b => {
          const w = Math.max(0, Nobo.minutesOf(b.end) - Nobo.minutesOf(b.start)) / 14.4;
          return `<span class="sched-seg m-${esc(b.mode)}" style="width:${w}%"
            title="${esc(b.start)}-${esc(b.end)} ${esc(b.mode)}"></span>`;
        }).join('');
        // The 7 C explanation only earns its space once Away is actually in use.
        const hint = root.querySelector('#weekAwayHint');
        if (hint) hint.hidden = !pts.some(p => p.mode === 'away');
      };

      const sortDay = () => {
        draft[day] = draft[day].slice()
          .sort((a, b) => Nobo.minutesOf(a.at) - Nobo.minutesOf(b.at));
      };

      const paint = () => {
        root.querySelectorAll('.day-tab').forEach(t => {
          t.setAttribute('aria-selected', String(t.dataset.day === day));
        });

        sortDay();
        const pts = draft[day];
        paintPreview();

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
                ${offeredScheduleModes(p.mode).map(([v, l]) => `<option value="${v}"${p.mode === v ? ' selected' : ''}>${l}</option>`).join('')}
              </select>
            </label>
            <button class="btn btn-danger week-del" type="button" data-del="${i}"
              ${i === 0 ? 'disabled title="The first change of the day cannot be removed"' : ''}
              aria-label="Remove this change">&times;</button>
          </div>`).join('');

        rowsEl.querySelectorAll('[data-at]').forEach(inp => {
          const i = Number(inp.dataset.at);
          /* While the picker is open: record the time and redraw the bar only,
             so the bar follows the finger without the input being replaced. */
          inp.oninput = () => {
            if (!inp.value) return;
            draft[day][i].at = inp.value;
            paintPreview();
          };
          /* When the user has finished: snap, check, and only then rebuild -- by
             which point the day may need re-sorting and the row may move. */
          inp.onblur = () => {
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
          // Changing a mode cannot reorder the day, so the rows stay as they
          // are and only the bar is redrawn.
          sel.onchange = () => {
            draft[day][Number(sel.dataset.mode)].mode = sel.value;
            paintPreview();
          };
        });

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
        if (at == null) { Nobo.toast('This day has no space left for another change', 'error'); return; }
        const hhmm = String(Math.floor(at / 60)).padStart(2, '0') + ':' + String(at % 60).padStart(2, '0');
        draft[day].push({ at: hhmm, mode: splitMode === 'comfort' ? 'eco' : 'comfort' });
        paint();
      };

      root.querySelector('#weekCopy').onchange = (e) => {
        const which = e.target.value;
        e.target.value = '';
        if (!which) return;
        const targets = which === 'week' ? SCHED_DAY_KEYS.slice(0, 5)
          : which === 'weekend' ? SCHED_DAY_KEYS.slice(5)
          : SCHED_DAY_KEYS.slice();
        targets.forEach(t => { draft[t] = draft[day].map(p => ({ at: p.at, mode: p.mode })); });
        Nobo.toast('Copied to ' + targets.length + ' days');
        paint();
      };

      root.querySelector('[data-act="dismiss"]').onclick = closeSheet;
      root.querySelector('[data-act="save"]').onclick = async () => {
        const payload = {};
        for (const [key, label] of schedDays()) {
          const pts = draft[key];
          const times = pts.map(p => p.at);
          if (new Set(times).size !== times.length) {
            Nobo.toast(label + ' has two changes at the same time', 'error'); return;
          }
          if (!times.includes('00:00')) { Nobo.toast(label + ' has to start at 00:00', 'error'); return; }
          payload[key] = blocksOfPoints(pts);
        }
        const nameEl = root.querySelector('#weekProfileName');
        if (editor.nameField) {
          const name = nameEl.value.trim();
          if (!name) { Nobo.toast('Give the schedule a name', 'error'); return; }
          await editor.onSave(payload, root, name);
          return;
        }
        await editor.onSave(payload, root);
      };

      paint();
    });
  }

  function askScheduleName(title, message, onSave) {
    openSheet(title, `
      <p class="zd-sub">${esc(message)}</p>
      <label class="field"><span>Schedule name</span>
        <input type="text" id="newScheduleName" autocomplete="off" placeholder="Weekend warm-up"></label>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
        <button class="btn btn-primary" data-act="ok" type="button">Save as a new schedule</button>
      </div>`, (root) => {
      const nameEl = root.querySelector('#newScheduleName');
      const okBtn = root.querySelector('[data-act="ok"]');
      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      okBtn.onclick = async () => {
        const name = nameEl.value.trim();
        if (!name) { Nobo.toast('Give the schedule a name', 'error'); return; }
        okBtn.disabled = true;
        await onSave(name, () => { okBtn.disabled = false; });
      };
      nameEl.onkeydown = (ev) => { if (ev.key === 'Enter') okBtn.click(); };
    });
  }

  function chooseUnsharedScheduleSave(zone, payload) {
    const name = (state.scheduleMeta && (state.scheduleMeta.week_profile_name || state.scheduleMeta.name)) || 'this schedule';
    openSheet('Save this edit?', `
      <p class="zd-sub">Choose how ${esc(zone.name)} should use this edited week.</p>
      <div class="schedule-choices">
        <button class="schedule-choice" type="button" data-apply-to="profile">
          <span class="schedule-choice-icon" aria-hidden="true">${Nobo.icon('normal')}</span>
          <span>
            <strong>Change "${esc(name)}"</strong>
            <small>The usual choice. ${esc(zone.name)} keeps following this schedule.</small>
          </span>
        </button>
        <button class="schedule-choice" type="button" data-apply-to="new">
          <span class="schedule-choice-icon" aria-hidden="true">${Nobo.icon('normal')}</span>
          <span>
            <strong>Save as a new schedule</strong>
            <small>Add it under Settings and make ${esc(zone.name)} follow it.</small>
          </span>
        </button>
      </div>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
      </div>`, (root) => {
      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      root.querySelector('[data-apply-to="profile"]').onclick = async (ev) => {
        const btn = ev.currentTarget;
        btn.disabled = true;
        await saveWeekSchedule(zone, payload, undefined, () => { btn.disabled = false; });
      };
      root.querySelector('[data-apply-to="new"]').onclick = () => saveAsNewScheduleForZone(zone, payload);
    });
  }

  function chooseScheduleSaveScope(zone, payload, shared) {
    const everyone = listSentence([zone.name].concat(shared));
    openSheet('Who should this change?', `
      <p class="zd-sub">This schedule is shared. Choose whether this edit changes one zone or every zone using it.</p>
      <div class="schedule-choices">
        <button class="schedule-choice" type="button" data-apply-to="zone">
          <span class="schedule-choice-icon" aria-hidden="true">${Nobo.icon('normal')}</span>
          <span>
            <strong>Just this zone</strong>
            <small>${esc(zone.name)} gets its own copy. The other zones keep the schedule as it is.</small>
          </span>
        </button>
        <button class="schedule-choice" type="button" data-apply-to="profile">
          <span class="schedule-choice-icon" aria-hidden="true">${Nobo.icon('normal')}</span>
          <span>
            <strong>Every zone using it</strong>
            <small>Changes ${esc(everyone)}.</small>
          </span>
        </button>
        <button class="schedule-choice" type="button" data-apply-to="new">
          <span class="schedule-choice-icon" aria-hidden="true">${Nobo.icon('normal')}</span>
          <span>
            <strong>Save as a new schedule</strong>
            <small>Add it under Settings and make ${esc(zone.name)} follow it.</small>
          </span>
        </button>
      </div>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
      </div>`, (root) => {
      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      root.querySelectorAll('[data-apply-to]').forEach(btn => {
        btn.onclick = async () => {
          if (btn.dataset.applyTo === 'new') {
            saveAsNewScheduleForZone(zone, payload);
            return;
          }
          btn.disabled = true;
          await saveWeekSchedule(zone, payload, btn.dataset.applyTo, () => { btn.disabled = false; });
        };
      });
    });
  }

  function saveAsNewScheduleForZone(zone, payload) {
    askScheduleName('Save as a new schedule', `Name the schedule ${zone.name} should follow.`, async (name, onError) => {
      hold(6000);
      try {
        const res = await Nobo.api.createWeekProfile({ name, schedule: payload });
        const profileId = res && res.profile_id;
        await Nobo.api.assignWeekProfile(zone.zone_id, profileId);
        closeSheet();
        Nobo.toast('New schedule saved');
        await loadSchedule(zone.zone_id);
        await refresh(true);
      } catch (e) {
        if (onError) onError();
        Nobo.toast(e.message, 'error');
      }
    });
  }

  async function saveWeekSchedule(zone, payload, applyTo, onError) {
    const body = { schedule: payload };
    if (applyTo) body.apply_to = applyTo;
    hold(6000);
    try {
      await Nobo.api.setSchedule(zone.zone_id, body);
      closeSheet();
      Nobo.toast('Weekly schedule saved');
      await loadSchedule(zone.zone_id);
      await refresh(true);
    } catch (e) {
      if (onError) onError();
      Nobo.toast(e.message, 'error');
    }
  }

  function editWeekProfile(profileId) {
    const profile = (state.weekProfiles || []).find(p => String(p.profile_id) === String(profileId));
    if (!profile) { Nobo.toast('That schedule is no longer here', 'error'); return; }
    const name = profile.name || (profile.profile && profile.profile.name) || 'Unnamed schedule';
    if (profile.can_edit === false) {
      Nobo.toast(profile.why_not_edit || 'This schedule cannot be edited.', 'error');
      return;
    }

    if (profile.unreadable || !profile.schedule) {
      openSheet(`${name} · schedule`, `
        <div class="note note-warn">${esc(profile.unreadable || 'The hub did not return a readable schedule.')}</div>
        <div class="sheet-actions">
          <button class="btn" data-act="dismiss" type="button">Close</button>
        </div>`, (root) => {
        root.querySelector('[data-act="dismiss"]').onclick = closeSheet;
      });
      return;
    }

    const used = ((profile && profile.used_by) || [])
      .map(z => typeof z === 'string' ? z : (z.name || z.zone_id))
      .filter(Boolean);
    const affected = used.length
      ? `Changes ${listSentence(used)}.`
      : 'This schedule is not used by any zone.';

    editWeek({
      title: `${name} · schedule`,
      schedule: profile.schedule,
      beforeHtml: `
        <p class="zd-sub">${esc(affected)}</p>
        <p class="zd-sub">Each row says what zones using this schedule do from that
        time until the next change. The day always starts at 00:00, so there can
        never be a gap.</p>`,
      onSave: async (payload, root) => {
        const btn = root.querySelector('[data-act="save"]');
        btn.disabled = true;
        await saveWeekProfileSchedule(profileId, payload, () => { btn.disabled = false; });
      },
    });
  }

  function addWeekProfile() {
    editWeek({
      title: 'Add a schedule',
      schedule: startingWeekSchedule(),
      nameField: true,
      beforeHtml: `
        <p class="zd-sub">Create a named schedule that can be assigned to any zone.</p>
        <p class="zd-sub">It starts as Comfort all day, every day.</p>`,
      onSave: async (payload, root, name) => {
        const btn = root.querySelector('[data-act="save"]');
        btn.disabled = true;
        hold(6000);
        try {
          await Nobo.api.createWeekProfile({ name, schedule: payload });
          closeSheet();
          Nobo.toast('Schedule added');
          await refresh(true);
        } catch (e) {
          btn.disabled = false;
          Nobo.toast(e.message, 'error');
        }
      },
    });
  }

  async function saveWeekProfileSchedule(profileId, payload, onError) {
    const openProfileId = String((state.scheduleMeta && state.scheduleMeta.week_profile_id) || '');
    const openZoneId = state.zoneId;
    hold(6000);
    try {
      await Nobo.api.updateWeekProfile(profileId, { schedule: payload });
      closeSheet();
      Nobo.toast('Schedule saved');
      await refresh(true);
      if (openZoneId && openProfileId === String(profileId)) await loadSchedule(openZoneId);
    } catch (e) {
      if (onError) onError();
      Nobo.toast(e.message, 'error');
    }
  }

  /* ------------------------------------------------------------------
   * Activity log - reached from inside System status
   *
   * One server buffer holds three kinds of entry, told apart by `source`:
   * a change made through the app ('api'), the away scheduler acting on its
   * own ('schedule'), and the hub connection itself ('hub'). Anything with
   * direction 'error' is a problem whatever its source. The filters are built
   * on those fields rather than on the wording of the message.
   * ---------------------------------------------------------------- */

  const LOG_FILTERS = {
    all:      { label: 'Everything', match: () => true },
    changes:  { label: 'Changes',    match: e => e.source === 'api' || e.source === 'schedule' },
    hub:      { label: 'Hub',        match: e => e.source === 'hub' },
    problems: { label: 'Problems',   match: e => e.direction === 'error' },
  };

  async function showLog() {
    state.view = 'log';
    $('#topTitle').textContent = 'Activity log';
    $('#topSub').textContent = 'What the system did, and when';
    switchView();
    renderLog();
    await loadLog();
  }

  async function loadLog() {
    try {
      const data = await Nobo.api.log(300);
      state.log = data.entries || [];
      state.logError = null;
    } catch (e) {
      state.logError = e.message;
    }
    if (state.view === 'log') renderLog();
  }

  function renderLog() {
    const root = $('#viewLog');
    const filter = LOG_FILTERS[state.logFilter] || LOG_FILTERS.all;

    const chips = Object.entries(LOG_FILTERS).map(([key, f]) => {
      const n = state.log ? state.log.filter(f.match).length : 0;
      return `<button class="log-chip" type="button" data-logfilter="${key}"
        aria-pressed="${state.logFilter === key}">${esc(f.label)} (${n})</button>`;
    }).join('');

    let body;
    if (state.logError) {
      body = `<div class="note note-warn">${esc(state.logError)}</div>`;
    } else if (state.log === null) {
      body = `<p class="zd-sub">Reading the log…</p>`;
    } else {
      const shown = state.log.filter(filter.match);
      body = shown.length
        ? `<ul class="log-list">${shown.map(logRow).join('')}</ul>`
        : `<p class="zd-sub">Nothing recorded under “${esc(filter.label)}”.</p>`;
    }

    root.innerHTML = `
      <section class="card">
        <p class="zd-sub">Every change made through this app, everything the away
        schedule did on its own, and the state of the connection to the hub. Newest
        first. The log lives in memory, so it starts again empty when the system
        restarts.</p>
        <div class="log-filters" role="group" aria-label="Filter the log">${chips}</div>
        ${body}
        <div class="log-foot">
          <span class="zd-sub">${state.log ? state.log.length : 0} entries kept</span>
          <span class="sheet-actions">
            <button class="btn" type="button" data-act="log-refresh">Refresh</button>
            <button class="btn btn-danger" type="button" data-act="log-clear">Clear the log</button>
          </span>
        </div>
      </section>`;

    root.querySelectorAll('[data-logfilter]').forEach(b => {
      b.onclick = () => { state.logFilter = b.dataset.logfilter; renderLog(); };
    });
    root.querySelector('[data-act="log-refresh"]').onclick = () => loadLog();
    root.querySelector('[data-act="log-clear"]').onclick = () => {
      confirmSheet('Clear the log?',
        'Every entry is discarded. This does not change any setting or any zone - it only throws away the record of what happened.',
        'Clear', async () => {
          try {
            await Nobo.api.clearLog();
            Nobo.toast('Log cleared');
            await loadLog();
          } catch (e) { Nobo.toast(e.message, 'error'); }
        }, true);
    };
  }

  function logRow(e) {
    const when = fmtLogTime(e.timestamp);
    const isError = e.direction === 'error';
    const dir = isError ? 'Problem'
      : e.direction === 'received' ? 'From hub'
      : e.source === 'schedule' ? 'Schedule'
      : e.source === 'hub' ? 'Hub'
      : 'Change';
    const badgeClass = isError ? 'badge-mode-comfort'
      : e.source === 'hub' || e.direction === 'received' ? 'badge-mode-away'
      : e.source === 'schedule' ? 'badge-mode-normal'
      : 'badge-ok';

    return `
      <li class="log-entry${isError ? ' is-error' : ''}">
        <span class="log-time">${esc(when)}</span>
        <span class="badge ${badgeClass}">${esc(dir)}</span>
        <span class="log-what">${esc(e.description || '')}</span>
        ${e.command ? `<span class="log-cmd">${esc(e.command)}</span>` : ''}
      </li>`;
  }

  /**
   * The server writes its timestamps in the Pi's own timezone with no offset,
   * so they must not be treated as UTC. Read the fields out of the string
   * rather than letting Date guess, then rebuild a local Date from those exact
   * parts — which is safe, because the local constructor does no conversion.
   */
  function fmtLogTime(ts) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/.exec(String(ts || ''));
    if (!m) return String(ts || '');
    const [, y, mo, d, hh, mm, ss] = m;
    const today = new Date();
    const sameDay = today.getFullYear() === +y
      && today.getMonth() + 1 === +mo
      && today.getDate() === +d;
    // Seconds matter in a diagnostic log, and Intl would drop them from the
    // short forms, so the clock is built by hand. It is 24-hour regardless.
    const clock = `${hh}:${mm}:${ss}`;
    if (sameDay) return clock;
    const local = new Date(+y, +mo - 1, +d, +hh, +mm, +ss);
    return `${Nobo.fmtDayMonth(local)} ${clock}`;
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
      <label class="field"><span>Zone</span><select id="mvZone">${options}</select></label>
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
      had - the same zone, the same schedule and the same temperatures.</p>
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
    openSheet('Rename zone', `
      <label class="field"><span>Zone name</span>
        <input type="text" id="rzName" value="${esc(zone.name)}" autocomplete="off"></label>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
        <button class="btn btn-primary" data-act="ok" type="button">Save</button>
      </div>`, (root) => {
      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      root.querySelector('[data-act="ok"]').onclick = async () => {
        const name = root.querySelector('#rzName').value.trim();
        if (!name) { Nobo.toast('Give the zone a name', 'error'); return; }
        try {
          await Nobo.api.updateZone(zone.zone_id, { name });
          closeSheet();
          Nobo.toast('Zone renamed');
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      };
    });
  }

  /**
   * Rename one heater.
   *
   * Worth having separately from the zone name: a zone is usually several
   * rooms that share a schedule, so the heater name is where the actual room
   * gets recorded -- "Soverom Master", "Soverom Køyeseng Bad". The official app
   * allows this, and without it those names could only be set from there.
   */
  function renameDevice(serial) {
    const d = state.devices.find(x => x.serial === serial);
    if (!d) { Nobo.toast('That heater is no longer here', 'error'); return; }
    const current = d.display_name || d.name || '';

    openSheet('Rename heater', `
      <p class="zd-sub">${esc(d.device_type || 'Heater')} &middot; ${esc(d.serial_display || serial)}</p>
      <label class="field"><span>Heater name</span>
        <input type="text" id="rdName" value="${esc(current)}" autocomplete="off"></label>
      <small class="field-hint">Often the room it stands in, when a zone covers more than one.</small>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
        <button class="btn btn-primary" data-act="ok" type="button">Save</button>
      </div>`, (root) => {
      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      root.querySelector('[data-act="ok"]').onclick = async () => {
        const name = root.querySelector('#rdName').value.trim();
        if (!name) { Nobo.toast('Give the heater a name', 'error'); return; }
        try {
          await Nobo.api.renameDevice(serial, name);
          closeSheet();
          Nobo.toast('Heater renamed');
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      };
    });
  }

  /**
   * Create a zone.
   *
   * The hub assigns the id, not the client, so the new zone is found by
   * reloading rather than by trusting anything echoed back. A zone starts
   * empty: heaters are moved or added into it afterwards, from inside it.
   */
  function addZoneSheet() {
    openSheet('Add a zone', `
      <p class="zd-sub">A new zone starts empty and on the standard week. Open it
      afterwards to add its heaters and set its temperatures.</p>
      <label class="field">
        <span>Zone name</span>
        <input type="text" id="azName" autocomplete="off" placeholder="e.g. Loft">
      </label>
      <label class="field">
        <span>Icon (optional)</span>
        <input type="text" id="azIcon" autocomplete="off" maxlength="2" placeholder="e.g. \u{1F6CF}">
        <small class="field-hint">Stored for the current app, which shows an icon per zone.
        Concept D identifies a zone by its name alone.</small>
      </label>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
        <button class="btn btn-primary" data-act="ok" type="button">Add zone</button>
      </div>`, (root) => {
      const nameEl = root.querySelector('#azName');
      const okBtn = root.querySelector('[data-act="ok"]');

      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      okBtn.onclick = async () => {
        const name = nameEl.value.trim();
        const icon = root.querySelector('#azIcon').value.trim();
        if (!name) { Nobo.toast('Give the zone a name', 'error'); return; }
        okBtn.disabled = true;
        try {
          await Nobo.api.addZone({ name, icon: icon || undefined });
          closeSheet();
          Nobo.toast(`${name} added`);
          await refresh(true);
        } catch (e) {
          okBtn.disabled = false;
          Nobo.toast(e.message, 'error');
        }
      };
      nameEl.onkeydown = (ev) => { if (ev.key === 'Enter') okBtn.click(); };
    });
  }

  function deleteZone(zone) {
    confirmSheet('Delete this zone?',
      `${zone.name} and its schedule are removed. Its heaters are not deleted.`,
      'Delete zone', async () => {
        try {
          await Nobo.api.removeZone(zone.zone_id);
          Nobo.toast('Zone deleted');
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
      // Cached so later re-renders (after saving, say) keep the right identity
      // and do not flash an ordinary user's disabled controls back to enabled.
      state.me = await Nobo.api.me();
      renderSettings();
    } catch (_) { /* the static render is already correct enough */ }
  }

  function renderSettings(me = state.me) {
    const hub = state.hub || {};
    const site = state.site || {};
    const isAdmin = !me || me.role === undefined || me.role === 'admin';
    $('#topTitle').textContent = 'Settings';
    $('#topSub').textContent = 'Name, hub, schedules and users';

    $('#viewSettings').innerHTML = `
      <section class="card">
        <h2>What this place is called</h2>
        <p class="zd-sub">Used across the app and on the sign-in page. A nickname,
        a street address, whatever you call it — "The Lodge", "Lakeside",
        "Main Street 12".</p>
        <label class="field">
          <span>Name</span>
          <input type="text" id="stSiteName" value="${esc(site.name || '')}"
                 maxlength="${site.max_length || 40}" autocomplete="off"
                 placeholder="Cabin" ${isAdmin ? '' : 'disabled'}>
          <small class="field-hint">Leave it empty to go back to "Cabin".</small>
        </label>
        <label class="exc-row">
          <input type="checkbox" id="stSiteLogin"
                 ${site.show_on_login === false ? '' : 'checked'} ${isAdmin ? '' : 'disabled'}>
          <span class="exc-name">Show it on the sign-in page</span>
        </label>
        <small class="field-hint">The sign-in page is shown to anyone who can reach
        this Pi, before any password. Fine for a nickname; turn this off if the name
        is your address and the network is shared.</small>

        <label class="field" style="margin-top:1rem">
          <span>Date format</span>
          <select id="stSiteLocale" ${isAdmin ? '' : 'disabled'}>
            ${LOCALE_CHOICES.map(([tag, label]) => `
              <option value="${esc(tag)}" ${(site.locale || '') === tag ? 'selected' : ''}>${esc(label)}</option>
            `).join('')}
          </select>
          <small class="field-hint">Decides how dates are written and in what
          language the days of the week appear. Set on the system rather than per
          browser, so every device in the house shows the same thing.
          Example: <strong id="stDateSample">${esc(Nobo.fmtWhen(new Date().toISOString()))}</strong></small>
        </label>
        <small class="field-hint">The clock is always 24-hour and temperatures are
        always Celsius. Neither is a preference: the hub's own schedules are
        "HHMM" and its specification states temperatures are in Celsius, so there
        is nothing else to choose.</small>

        <div class="sheet-actions">
          <button class="btn btn-primary" type="button" data-act="save-site"
            ${isAdmin ? '' : 'disabled'}>Save</button>
        </div>
        ${isAdmin ? '' : '<div class="note">Only an administrator can change these.</div>'}
      </section>

      <section class="card">
        <h2>Where the data comes from</h2>
        <div class="switch">
          <div class="switch-text">
            <strong>Demo mode</strong>
            <span>Example zones and heaters, so you can try the app without a hub.</span>
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
        <h2>Zones that must not get cold</h2>
        <p class="zd-sub">${AWAY_EXPLAINER()} Pick the zones that should hold their
        Eco temperature instead of dropping to ${AWAY_TEMP_LABEL()} whenever ${SITE_IN()}
        goes Away — a bathroom with pipes, a workshop, a wine store.</p>
        <div id="awayExc" class="exc-list">
          <p class="zd-sub">Loading zones…</p>
        </div>
        <div class="sheet-actions">
          <button class="btn btn-primary" type="button" data-act="save-exc">Save exceptions</button>
        </div>
        <small class="field-hint">This applies both when you press Away and when a
        planned away period starts while nobody is looking at the app.</small>
      </section>

      <section class="card">
        <div class="section-head">
          <h2>Schedules</h2>
          <button class="btn btn-add" type="button" data-act="add-schedule">Add a schedule</button>
        </div>
        <p class="zd-sub">Schedules can be shared by several zones. Open one here to
        see and edit the week it contains.</p>
        ${renderScheduleSettings()}
      </section>

      <section class="card">
        <h2>Telling you when something is wrong</h2>
        <p class="zd-sub">Optional email alerts. The Nobø hub reports very little
        about individual heaters, so this can tell you when the hub itself goes
        away and when settings are changed from another app — but it cannot see a
        cold room, a heater without power, or a thermostat switched off at the wall.
        Everything here is off unless you turn it on.</p>

        <div class="switch">
          <div class="switch-text">
            <strong>Send alerts</strong>
            <span>Off until a mail server and a recipient are set below.</span>
          </div>
          <button class="btn" type="button" data-act="toggle-notify"
            aria-pressed="false" ${isAdmin ? '' : 'disabled'}>Off</button>
        </div>

        <div id="notifyBox" class="notify-box">
          <p class="zd-sub">Loading…</p>
        </div>

        ${isAdmin ? '' : '<div class="note">Only an administrator can change these.</div>'}
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
        <small class="field-hint">User management opens the classic interface, which
        still has the full user administration screen.</small>
      </section>

      <section class="card">
        <h2>Diagnostics</h2>
        <p class="zd-sub">A record of every change made through this app, everything
        the away schedule did on its own, and the state of the connection to the hub.
        Worth opening when something has not behaved.</p>
        <div class="sheet-actions">
          <button class="btn" type="button" data-act="open-log">Open the activity log</button>
        </div>
      </section>

      <section class="card">
        <h2>About this interface</h2>
        <p class="zd-sub">This is the Cabin interface. The previous one is still
        installed and is always reachable at <a href="/classic">/classic</a> — nothing
        was removed. To make it the default again, set <code>NOBO_UI=classic</code> in
        the server's <code>.env</code> file and restart.</p>
      </section>`;

    const root = $('#viewSettings');
    root.querySelector('[data-act="toggle-demo"]').onclick = () => toggleDemo(!hub.demo_mode);
    root.querySelector('[data-act="save-hub"]').onclick = saveHub;
    root.querySelector('[data-act="save-site"]').onclick = saveSite;
    root.querySelector('[data-act="open-log"]').onclick = showLog;
    root.querySelector('[data-act="add-schedule"]').onclick = addWeekProfile;
    root.querySelector('[data-act="signout"]').onclick = async () => {
      try { await Nobo.api.logout(); } catch (_) {}
      window.location.href = '/login';
    };
    // The classic UI is no longer at "/", so this must name it explicitly or it
    // would simply reload Cabin.
    root.querySelector('[data-act="open-users"]').onclick = () => { window.location.href = '/classic#settings'; };
    root.querySelector('[data-act="save-exc"]').onclick = saveAwayExceptions;
    root.querySelectorAll('[data-rename-profile]').forEach(b => {
      b.onclick = () => renameWeekProfile(b.dataset.renameProfile);
    });
    root.querySelectorAll('[data-edit-profile]').forEach(b => {
      b.onclick = () => editWeekProfile(b.dataset.editProfile);
    });
    root.querySelectorAll('[data-delete-profile]').forEach(b => {
      b.onclick = () => deleteWeekProfile(b.dataset.deleteProfile);
    });
    // Show the chosen format before it is saved, so picking one is not a guess.
    const localeSel = root.querySelector('#stSiteLocale');
    const sample = root.querySelector('#stDateSample');
    if (localeSel && sample) {
      localeSel.onchange = () => {
        const chosen = state.site && state.site.locale;
        Nobo.setLocale(localeSel.value);
        sample.textContent = Nobo.fmtWhen(new Date().toISOString());
        // Put it back until Save, so nothing else on screen changes yet.
        Nobo.setLocale(chosen);
      };
    }
    loadAwayExceptions();
    const notifyToggle = root.querySelector('[data-act="toggle-notify"]');
    if (notifyToggle) notifyToggle.onclick = () => toggleNotifications();
    loadNotifications(isAdmin);
  }

  function renderScheduleSettings() {
    const profiles = state.weekProfiles || [];
    if (!profiles.length) return `<p class="zd-sub">No schedules were returned by the hub.</p>`;
    return `<ul class="schedule-list">
      ${profiles.map(profile => {
        const id = String(profile.profile_id);
        const name = profile.name || (profile.profile && profile.profile.name) || 'Unnamed schedule';
        const canDelete = profile.can_delete !== false;
        const whyNot = profile.why_not || 'This schedule cannot be deleted.';
        const canEdit = profile.can_edit !== false;
        const whyNotEdit = profile.why_not_edit || 'This schedule cannot be edited.';
        return `<li class="schedule-row">
          <div class="schedule-main">
            <span class="schedule-icon" aria-hidden="true">${Nobo.icon('normal')}</span>
            <span class="schedule-copy">
              <strong>${esc(name)}</strong>
              <small>${esc(profileUsage(profile))}</small>
            </span>
          </div>
          <div class="schedule-row-actions">
            <button class="btn schedule-edit" type="button" data-edit-profile="${esc(id)}"
              ${canEdit ? '' : `disabled title="${esc(whyNotEdit)}"`}>Edit</button>
            <button class="icon-btn act-rename" type="button" data-rename-profile="${esc(id)}"
              ${canEdit ? 'title="Rename schedule"' : `disabled title="${esc(whyNotEdit)}"`}
              aria-label="Rename ${esc(name)}">${Nobo.icon('rename')}</button>
            <button class="icon-btn act-remove" type="button" data-delete-profile="${esc(id)}"
              ${canDelete ? 'title="Delete schedule"' : `disabled title="${esc(whyNot)}"`}
              aria-label="Delete ${esc(name)}">${Nobo.icon('remove')}</button>
          </div>
        </li>`;
      }).join('')}
    </ul>`;
  }

  function renameWeekProfile(profileId) {
    const profile = (state.weekProfiles || []).find(p => String(p.profile_id) === String(profileId));
    if (!profile) { Nobo.toast('That schedule is no longer here', 'error'); return; }
    if (profile.can_edit === false) {
      Nobo.toast(profile.why_not_edit || 'This schedule cannot be edited.', 'error');
      return;
    }
    const current = profile.name || (profile.profile && profile.profile.name) || '';

    openSheet('Rename schedule', `
      <label class="field"><span>Schedule name</span>
        <input type="text" id="rwName" value="${esc(current)}" autocomplete="off"></label>
      <div class="sheet-actions">
        <button class="btn" data-act="cancel" type="button">Cancel</button>
        <button class="btn btn-primary" data-act="ok" type="button">Save</button>
      </div>`, (root) => {
      const nameEl = root.querySelector('#rwName');
      const okBtn = root.querySelector('[data-act="ok"]');
      root.querySelector('[data-act="cancel"]').onclick = closeSheet;
      okBtn.onclick = async () => {
        const name = nameEl.value.trim();
        if (!name) { Nobo.toast('Give the schedule a name', 'error'); return; }
        okBtn.disabled = true;
        try {
          await Nobo.api.updateWeekProfile(profileId, { name });
          closeSheet();
          Nobo.toast('Schedule renamed');
          await refresh(true);
        } catch (e) {
          okBtn.disabled = false;
          Nobo.toast(e.message, 'error');
        }
      };
      nameEl.onkeydown = (ev) => { if (ev.key === 'Enter') okBtn.click(); };
    });
  }

  function deleteWeekProfile(profileId) {
    const profile = (state.weekProfiles || []).find(p => String(p.profile_id) === String(profileId));
    if (!profile) { Nobo.toast('That schedule is no longer here', 'error'); return; }
    const name = profile.name || (profile.profile && profile.profile.name) || 'Unnamed schedule';
    confirmSheet('Delete this schedule?',
      `Delete "${name}"? This cannot be undone.`,
      'Delete schedule', async () => {
        try {
          await Nobo.api.deleteWeekProfile(profileId);
          Nobo.toast('Schedule deleted');
          await refresh(true);
        } catch (e) { Nobo.toast(e.message, 'error'); }
      }, true);
  }

  /* ------------------------------------------------------------------
   * Notifications
   *
   * What can honestly be detected, and what cannot, is decided by the Nobø
   * protocol rather than by us:
   *
   *   - An override carries no source, so "who changed this?" is answered by
   *     elimination. The server records its own writes; anything else came
   *     from somewhere else, and the wording never guesses which app.
   *   - A component's Status field is "not yet implemented", so there is no
   *     signal at all when somebody presses the button on a thermostat.
   *   - But a temperature can arrive as "N/A" once the hub's stored value goes
   *     stale, which is what a switched-off thermostat eventually looks like.
   *
   * So the two alerts that matter for a cabin - a cold room and a thermostat
   * that has gone quiet - are both inferred, and the copy here says so rather
   * than implying the system knows more than it does.
   * ---------------------------------------------------------------- */

  const NOTIFY_SECURITY = [
    ['starttls', 'STARTTLS (port 587)'],
    ['ssl', 'SSL/TLS (port 465)'],
    ['none', 'None (not recommended)'],
  ];

  async function loadNotifications(isAdmin) {
    const box = $('#notifyBox');
    if (!box) return;
    try {
      state.notify = await Nobo.api.notifications();
      state.notifyExpanded = !!state.notify.enabled;
      renderNotifications(isAdmin);
    } catch (e) {
      // An ordinary user is not allowed to read these, which is not an error
      // worth shouting about - just hide the panel.
      box.closest('.card').hidden = true;
    }
  }

  function renderNotifications(isAdmin) {
    const box = $('#notifyBox');
    const n = state.notify;
    if (!box || !n) return;
    const dis = isAdmin ? '' : 'disabled';

    const toggle = $('[data-act="toggle-notify"]');
    if (toggle) {
      toggle.textContent = n.enabled ? 'On' : 'Off';
      toggle.setAttribute('aria-pressed', String(!!n.enabled));
    }

    const types = n.event_types || {};

    // Everything below the master switch is hidden until alerts are actually
    // on. Asked for, and right: a page of mail-server fields is noise to
    // somebody who has not decided they want alerts yet.
    //
    // The exception is the moment of turning them on. The settings have to be
    // fillable *before* they can be enabled - the server refuses to enable a
    // configuration that could not deliver - so pressing the switch reveals the
    // form even though nothing is saved yet.
    if (!n.enabled && !state.notifyExpanded) {
      box.innerHTML = `
        <p class="zd-sub">Turn <strong>Send alerts</strong> on to choose what to be
        told about and where to send it.</p>`;
      return;
    }

    const pending = !n.enabled;

    box.innerHTML = `
      ${pending ? `<div class="note">Fill these in and press <strong>Save alerts</strong>
        to switch them on. Nothing is sent until then.</div>` : ''}
      <h3 class="notify-head">What to tell me about</h3>
      <div class="exc-list">
        ${Object.keys(types).map(key => `
          <label class="exc-row notify-row">
            <input type="checkbox" data-ev="${esc(key)}"
                   ${n.events[key] ? 'checked' : ''} ${dis}>
            <span class="exc-name">${esc(types[key].label)}
              <small class="field-hint">${esc(types[key].help)}</small>
            </span>
          </label>`).join('')}
      </div>

      <h3 class="notify-head">Where to send it</h3>
      <div class="notify-grid">
        <label class="field">
          <span>Send to</span>
          <input type="text" id="ntTo" value="${esc((n.email.to_addrs || []).join(', '))}"
                 placeholder="you@example.com" autocomplete="off" ${dis}>
          <small class="field-hint">Separate several addresses with commas.</small>
        </label>
        <label class="field">
          <span>Mail server</span>
          <input type="text" id="ntHost" value="${esc(n.email.host || '')}"
                 placeholder="smtp.gmail.com" autocomplete="off" ${dis}>
        </label>
        <label class="field">
          <span>Port</span>
          <input type="number" id="ntPort" value="${esc(String(n.email.port || 587))}" ${dis}>
        </label>
        <label class="field">
          <span>Encryption</span>
          <select id="ntSec" ${dis}>
            ${NOTIFY_SECURITY.map(([v, l]) =>
              `<option value="${v}"${n.email.security === v ? ' selected' : ''}>${l}</option>`).join('')}
          </select>
        </label>
        <label class="field">
          <span>Username</span>
          <input type="text" id="ntUser" value="${esc(n.email.username || '')}"
                 autocomplete="off" ${dis}>
        </label>
        <label class="field">
          <span>Password</span>
          <input type="password" id="ntPass" value="" autocomplete="new-password"
                 placeholder="${n.email.password_set ? 'Unchanged' : ''}" ${dis}>
          <small class="field-hint">${n.email.password_set
            ? 'Leave empty to keep the password you already saved.'
            : 'With Gmail this is an app password, not your normal one.'}</small>
        </label>
        <label class="field">
          <span>From</span>
          <input type="text" id="ntFrom" value="${esc(n.email.from_addr || '')}"
                 placeholder="pi@example.com" autocomplete="off" ${dis}>
        </label>
      </div>

      <label class="exc-row notify-row">
        <input type="checkbox" id="ntQuiet" ${n.quiet_hours.enabled ? 'checked' : ''} ${dis}>
        <span class="exc-name">Stay quiet overnight
          <small class="field-hint">From ${esc(n.quiet_hours.start)} to ${esc(n.quiet_hours.end)}.
          Anything urgent is still sent — it would be no use in the morning.</small>
        </span>
      </label>

      <div class="sheet-actions">
        <button class="btn" type="button" data-act="test-notify" ${dis}>Send a test email</button>
        <button class="btn btn-primary" type="button" data-act="save-notify" ${dis}>Save alerts</button>
      </div>`;

    if (!isAdmin) return;
    box.querySelector('[data-act="save-notify"]').onclick = () => saveNotifications();
    box.querySelector('[data-act="test-notify"]').onclick = testNotifications;
  }

  function readNotifyForm() {
    const val = (id) => { const el = $(id); return el ? el.value.trim() : ''; };
    const num = (id, fallback) => {
      const el = $(id);
      const v = el ? Number(el.value) : NaN;
      return Number.isFinite(v) ? v : fallback;
    };
    const n = state.notify;
    const events = {};
    document.querySelectorAll('#notifyBox [data-ev]').forEach(cb => {
      // A checkbox disabled for want of a thermometer is left out entirely, so
      // the stored preference survives. Add an SW4 later and the choice the
      // user originally made comes back rather than having been quietly wiped.
      if (cb.disabled) return;
      events[cb.dataset.ev] = cb.checked;
    });
    const body = {
      events,
      quiet_hours: { ...n.quiet_hours, enabled: !!($('#ntQuiet') && $('#ntQuiet').checked) },
      email: {
        host: val('#ntHost'),
        port: num('#ntPort', 587),
        security: val('#ntSec') || 'starttls',
        username: val('#ntUser'),
        from_addr: val('#ntFrom'),
        to_addrs: val('#ntTo').split(',').map(s => s.trim()).filter(Boolean),
      },
    };
    // An empty box means "keep what is saved", not "clear it" - the browser is
    // never given the password, so it cannot send it back.
    const pass = val('#ntPass');
    if (pass) body.email.password = pass;
    return body;
  }

  async function saveNotifications(enabledOverride) {
    const body = readNotifyForm();
    // Saving from the expanded form means "I want these on", which is the only
    // way to enable them: the toggle alone cannot, because the server rightly
    // refuses a configuration that could not deliver.
    body.enabled = enabledOverride !== undefined ? enabledOverride : true;
    try {
      state.notify = await Nobo.api.setNotifications(body);
      state.notifyExpanded = !!state.notify.enabled;
      Nobo.toast(state.notify.enabled ? 'Alerts are on' : 'Alerts are off');
      renderNotifications(true);
    } catch (e) {
      Nobo.toast(e.message, 'error');
    }
  }

  function toggleNotifications() {
    const n = state.notify;
    if (!n) return;
    if (n.enabled) {
      state.notifyExpanded = false;
      saveNotifications(false);
      return;
    }
    // Reveal the form rather than trying to save straight away, which would
    // fail for anybody who has not entered a mail server yet.
    state.notifyExpanded = true;
    renderNotifications(true);
  }

  async function testNotifications() {
    const btn = $('#notifyBox [data-act="test-notify"]');
    if (btn) { btn.disabled = true; btn.textContent = 'Sending…'; }
    try {
      const res = await Nobo.api.testNotification(readNotifyForm());
      Nobo.toast(`Sent to ${(res.sent_to || []).join(', ')}. Check your inbox.`);
    } catch (e) {
      Nobo.toast(e.message, 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Send a test email'; }
    }
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
        box.innerHTML = `<p class="zd-sub">No zones to choose from yet.</p>`;
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
      box.innerHTML = `<p class="zd-sub">Could not load the zones: ${esc(e.message)}</p>`;
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
        ? `${zone_ids.length} zone${zone_ids.length > 1 ? 's' : ''} will stay on Eco when away`
        : 'Every zone will follow Away');
      if (res && res.applied_now && res.applied_now.length) {
        Nobo.toast(`Applied now, because ${SITE_IN()} is away`);
      }
      refresh();
    } catch (e) { Nobo.toast(e.message, 'error'); }
  }

  function toggleDemo(next) {
    confirmSheet(next ? 'Switch to demo mode?' : 'Connect to a real hub?',
      next
        ? 'The app shows example zones instead of your hub. You will be signed out so it reloads cleanly.'
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

  async function saveSite() {
    const nameEl = $('#stSiteName');
    const loginEl = $('#stSiteLogin');
    const localeEl = $('#stSiteLocale');
    if (!nameEl) return;
    try {
      state.site = await Nobo.api.setSite({
        name: nameEl.value,
        show_on_login: !!(loginEl && loginEl.checked),
        locale: localeEl ? localeEl.value : undefined,
      });
      // Re-label everything immediately. The name appears in the header, the
      // trip card and half the confirmations, and the locale changes every
      // date on screen, so waiting for a reload would leave the app half done.
      applySiteName();
      renderSettings();
      Nobo.toast(state.site.is_named ? `Now called ${state.site.name}` : 'Settings saved');
    } catch (e) { Nobo.toast(e.message, 'error'); }
  }

  /* ------------------------------------------------------------------
   * View switching
   * ---------------------------------------------------------------- */

  function switchView() {
    $('#viewHome').hidden     = state.view !== 'home';
    $('#viewZone').hidden     = state.view !== 'zone';
    $('#viewLog').hidden      = state.view !== 'log';
    $('#viewSettings').hidden = state.view !== 'settings';
    $('#btnBack').hidden      = state.view === 'home';
    window.scrollTo(0, 0);
  }

  function showHome() {
    state.view = 'home';
    state.zoneId = null;
    $('#topTitle').textContent = SITE();
    $('#topSub').textContent = 'Nobø Control';
    switchView();
    renderHome();
  }

  $('#btnBack').addEventListener('click', () => {
    // The log is only reachable from Settings, so Back belongs there, not Home.
    if (state.view === 'log') showSettings(); else showHome();
  });
  $('#btnSettings').addEventListener('click', () => {
    if (state.view === 'settings') showHome(); else showSettings();
  });
  $('#btnAddZone').addEventListener('click', addZoneSheet);

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
    else if (state.view === 'settings') renderSettings();
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

    /* The log only matters while you are looking at it, so it is polled only
       then - and never while a sheet is open, which would move the list out
       from under a confirmation. */
    setInterval(() => {
      if (state.view === 'log' && sheetEl.hidden) loadLog();
    }, 10000);
  })();

})();
