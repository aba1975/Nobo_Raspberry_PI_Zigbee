/*
 * Concept C - "Heating Board"
 *
 * One surface. Rows can be adjusted where they sit; opening a room expands
 * it in place on a phone and fills a second pane on a wide screen. There is
 * no page navigation anywhere in this concept.
 *
 * Reads and writes the existing API only.
 */

(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = Nobo.escapeHtml;

  let zones = [];
  let openId = null;
  let schedules = {};
  let sortBy = 'name';
  let onlyHeating = false;
  let pending = 0;              // outstanding writes; suppresses live redraws
  let sheetOpen = false;
  let lastFocus = null;

  const wide = window.matchMedia('(min-width: 1100px)');

  /* =============================================================
   * Header
   * ============================================================= */

  function renderHead() {
    const s = Nobo.houseSummary(zones);

    let sentence;
    if (!s.zoneCount) {
      sentence = 'No rooms are configured yet.';
    } else if (s.heatingCount === 0) {
      sentence = 'Every room has reached its target. Nothing is calling for heat.';
    } else {
      const verb = s.heatingCount === 1 ? 'room is' : 'rooms are';
      sentence = `<b class="warm">${s.heatingCount} ${verb}</b> warming up`;
      if (s.coldest) {
        sentence += `, coldest is <b>${esc(s.coldest.name)}</b> at ${Nobo.fmtTemp(s.coldest.current_temperature)}\u00B0`;
      }
      sentence += '.';
    }

    // The next scheduled change across the whole house, if we know it.
    const upcoming = zones
      .map(z => {
        const t = Nobo.todaySchedule(schedules[z.zone_id]);
        return t && t.next ? { zone: z, at: t.next.start, mode: t.next.mode } : null;
      })
      .filter(Boolean)
      .sort((a, b) => Nobo.minutesOf(a.at) - Nobo.minutesOf(b.at))[0];

    if (upcoming) {
      const label = Nobo.MODES[upcoming.mode] ? Nobo.MODES[upcoming.mode].label : upcoming.mode;
      sentence += ` Next change: <b>${esc(upcoming.zone.name)}</b> to <b>${esc(label)}</b> at <b>${esc(upcoming.at)}</b>.`;
    }

    if (s.overriddenCount) {
      sentence += ` ${s.overriddenCount} room${s.overriddenCount === 1 ? ' is' : 's are'} on a manual override.`;
    }

    $('headStatus').innerHTML = sentence;

    document.querySelectorAll('#houseMode .seg').forEach(btn => {
      btn.setAttribute('aria-pressed', String(btn.dataset.mode === s.mode));
    });
    $('houseModeLabel').textContent = s.mode === 'mixed' ? 'All rooms (mixed)' : 'All rooms';
  }

  /* =============================================================
   * Rows
   * ============================================================= */

  function visibleZones() {
    let list = zones.slice();
    if (onlyHeating) list = list.filter(z => Nobo.heatState(z) === 'heating');
    if (sortBy === 'coldest') {
      list.sort((a, b) => (a.current_temperature ?? 99) - (b.current_temperature ?? 99));
    } else if (sortBy === 'gap') {
      const gap = (z) => {
        const t = Nobo.targetTemp(z);
        return (t == null || z.current_temperature == null) ? -99 : t - z.current_temperature;
      };
      list.sort((a, b) => gap(b) - gap(a));
    } else {
      list.sort((a, b) => a.name.localeCompare(b.name));
    }
    return list;
  }

  /** Which setpoint the inline stepper writes to, or null when locked. */
  function activeSetpoint(zone) {
    if (!zone.supports_temp_adjust) return null;
    const eff = Nobo.effectiveMode(zone);
    if (eff === 'away' || eff === 'off') return null;
    return eff === 'eco' ? 'eco' : 'comfort';
  }

  function rowHtml(zone) {
    const heat = Nobo.heatState(zone);
    const mode = zone.current_mode || 'normal';
    const eff = Nobo.effectiveMode(zone);
    const which = activeSetpoint(zone);
    const setValue = which === 'eco' ? zone.eco_temperature : zone.comfort_temperature;
    const target = Nobo.targetTemp(zone);
    const isOpen = zone.zone_id === openId;

    const modeText = mode === 'normal'
      ? `Schedule \u00B7 ${Nobo.MODES[eff].label}`
      : `${Nobo.MODES[mode].label} override`;

    const serials = zone.components || [];
    const thumbs = serials.slice(0, 3).map(s =>
      `<div class="np-device" title="${esc(Nobo.deviceName(s))}">${Nobo.deviceImg(s)}</div>`).join('');
    const more = serials.length > 3 ? `<span class="more">+${serials.length - 3}</span>` : '';

    const setter = which ? `
      <div class="row-set">
        <button class="set-btn" type="button" data-step="-0.5" data-zone="${esc(zone.zone_id)}"
                aria-label="Lower ${which} temperature in ${esc(zone.name)}">\u2212</button>
        <span class="set-value" id="setVal-${esc(zone.zone_id)}">
          ${Nobo.fmtTemp(setValue)}\u00B0<small>${which === 'eco' ? 'Eco' : 'Comfort'}</small>
        </span>
        <button class="set-btn" type="button" data-step="0.5" data-zone="${esc(zone.zone_id)}"
                aria-label="Raise ${which} temperature in ${esc(zone.name)}">+</button>
      </div>`
      : `<div class="row-set">
        <span class="set-value">${target == null ? '\u2014' : Nobo.fmtTemp(target) + '\u00B0'}
          <small>${zone.supports_temp_adjust ? 'Locked' : 'Manual'}</small></span>
      </div>`;

    return `
      <div class="row ${isOpen ? 'is-open' : ''}" data-heat="${heat}" data-zone="${esc(zone.zone_id)}">
        <div class="row-main">
          <span class="row-stripe" aria-hidden="true"></span>
          <div class="row-id">
            ${zone.icon ? `<span class="row-icon" aria-hidden="true">${esc(zone.icon)}</span>` : ''}
            <div class="row-text">
              <div class="row-name">${esc(zone.name)}</div>
              <div class="row-mode"><span class="dot" data-mode="${mode}" aria-hidden="true"></span>${esc(modeText)}</div>
            </div>
          </div>
          <div class="row-devices" aria-hidden="true">${thumbs}${more}</div>
          <div class="row-temp">
            <span class="row-now">${Nobo.fmtTemp(zone.current_temperature)}<span class="deg">\u00B0C</span></span>
            <span class="row-state">${Nobo.HEAT_STATE[heat].label}</span>
          </div>
          ${setter}
          <button class="row-open" type="button" data-open="${esc(zone.zone_id)}"
                  aria-expanded="${isOpen}" aria-label="${isOpen ? 'Collapse' : 'Expand'} ${esc(zone.name)}">\u203A</button>
        </div>
        ${isOpen && !wide.matches ? `<div class="row-detail">${detailHtml(zone)}</div>` : ''}
      </div>`;
  }

  function renderList() {
    const list = $('list');
    list.setAttribute('aria-busy', 'false');
    const items = visibleZones();
    if (!zones.length) {
      list.innerHTML = '<div class="empty"><h3>No rooms yet</h3><p>Add a zone in the current app, then come back.</p></div>';
    } else if (!items.length) {
      list.innerHTML = '<div class="empty"><p>No rooms are heating right now.</p></div>';
    } else {
      list.innerHTML = items.map(rowHtml).join('');
    }
    renderDetailPane();
  }

  /* =============================================================
   * Detail
   * ============================================================= */

  function scheduleStrip(zone) {
    const today = Nobo.todaySchedule(schedules[zone.zone_id]);
    if (!today) return '<p class="note">Loading today\u2019s schedule\u2026</p>';
    if (!today.blocks.length) return '<p class="note">No schedule blocks for today.</p>';
    const segs = today.blocks.map(b => {
      const width = ((Nobo.minutesOf(b.end) - Nobo.minutesOf(b.start)) / 1440) * 100;
      const label = Nobo.MODES[b.mode] ? Nobo.MODES[b.mode].label : b.mode;
      return `<div class="strip-seg" data-mode="${esc(b.mode)}" style="width:${width}%"
                   title="${esc(b.start)}\u2013${esc(b.end)} ${esc(label)}"></div>`;
    }).join('');
    const next = today.next
      ? `<p class="next-change">Next: <b>${esc(Nobo.MODES[today.next.mode] ? Nobo.MODES[today.next.mode].label : today.next.mode)}</b> at <b>${esc(today.next.start)}</b></p>`
      : '<p class="next-change">No further changes today.</p>';
    return `
      <div class="strip" role="img" aria-label="Today\u2019s schedule">
        ${segs}<div class="strip-now" style="left:${(today.nowMin / 1440) * 100}%"></div>
      </div>
      <div class="strip-legend"><span>00:00</span><span>12:00</span><span>24:00</span></div>
      ${next}`;
  }

  function detailHtml(zone) {
    const mode = zone.current_mode || 'normal';
    const serials = zone.components || [];
    const adjustable = zone.supports_temp_adjust;

    const gallery = serials.length ? serials.map((s, i) => `
      <div class="gcard">
        <div class="np-device">${Nobo.deviceImg(s)}</div>
        <div class="gcard-name">${esc((zone.components_names || [])[i] || Nobo.deviceName(s))}</div>
        <div class="gcard-sub">${esc(Nobo.deviceName(s))}</div>
        <div class="gcard-sub">${esc((zone.components_display || [])[i] || s)}</div>
      </div>`).join('') : '<p class="note">No devices are assigned to this room.</p>';

    const tempRow = (key, label, hint, value, editable) => `
      <div class="temp-row">
        <span class="temp-row-label">${label}<small>${hint}</small></span>
        <input class="num-input" type="number" inputmode="decimal"
               min="${Nobo.TEMP_MIN}" max="${Nobo.TEMP_MAX}" step="0.5"
               value="${value == null ? '' : value.toFixed(1)}"
               data-set="${key}" data-zone="${esc(zone.zone_id)}"
               aria-label="${label} temperature for ${esc(zone.name)}"
               ${editable ? '' : 'disabled'}>
      </div>`;

    const temps = adjustable
      ? tempRow('comfort', 'Comfort', 'When the room is warm', zone.comfort_temperature, true) +
        tempRow('eco', 'Eco', 'When the room is idling', zone.eco_temperature, true) +
        tempRow('away', 'Away', 'Fixed by the Nob\u00F8 system', zone.away_temperature != null ? zone.away_temperature : 7, false)
      : `<p class="note">The devices in this room have their comfort and eco temperatures set by hand
         on the device itself, so they cannot be changed from here.</p>` +
        tempRow('away', 'Away', 'Fixed by the Nob\u00F8 system', zone.away_temperature != null ? zone.away_temperature : 7, false);

    const modes = ['normal', 'comfort', 'eco', 'away'].map(m => `
      <button class="mode-btn" type="button" data-mode="${m}" data-zone="${esc(zone.zone_id)}" aria-pressed="${mode === m}">
        <span class="mb-glyph" aria-hidden="true">${Nobo.MODES[m].glyph}</span>${Nobo.MODES[m].label}
      </button>`).join('');

    return `
      <div class="detail-grid">
        <div class="dblock">
          <h3>Devices in ${esc(zone.name)}</h3>
          <div class="gallery">${gallery}</div>
        </div>
        <div class="dblock">
          <h3>Temperatures</h3>
          <div class="temp-rows">${temps}</div>
          <p class="note" style="margin-top:var(--sp-2)">Type a value or use the stepper in the row above.</p>
        </div>
        <div class="dblock">
          <h3>Mode</h3>
          <div class="modes" role="group" aria-label="Mode for ${esc(zone.name)}">${modes}</div>
        </div>
        <div class="dblock">
          <h3>Today</h3>
          ${scheduleStrip(zone)}
        </div>
      </div>
      <p class="note" style="margin-top:var(--sp-4)">
        Heating state is estimated by comparing the measured temperature with the target; the hub does not
        report element power directly. Renaming the room, managing devices and editing the weekly schedule
        stay in the current app. <a class="link-btn" href="/#devices" style="margin-left:6px">Open it</a>
      </p>`;
  }

  function renderDetailPane() {
    const pane = $('detail');
    if (!wide.matches) { pane.innerHTML = ''; wireDetail($('list')); return; }
    const zone = zones.find(z => z.zone_id === openId);
    pane.innerHTML = zone
      ? `<div class="detail-inner"><h2 style="font-size:19px;font-weight:650;margin-bottom:var(--sp-4)">
           ${zone.icon ? esc(zone.icon) + ' ' : ''}${esc(zone.name)}</h2>${detailHtml(zone)}</div>`
      : '<div class="detail-inner"><p class="detail-empty">Select a room to see its devices, temperatures and schedule.</p></div>';
    wireDetail(pane);
    wireDetail($('list'));
  }

  /* =============================================================
   * Writes
   * ============================================================= */

  async function write(fn, successMessage) {
    pending += 1;
    try {
      await fn();
      if (successMessage) Nobo.toast(successMessage, 'success');
      zones = await Nobo.api.zones();
      renderHead(); renderList();
    } catch (err) {
      Nobo.toast(err.message, 'error');
      try { zones = await Nobo.api.zones(); } catch (_) { /* keep what we have */ }
      renderHead(); renderList();
    } finally {
      pending -= 1;
    }
  }

  /* Steppers batch: hold to repeat, then send one request when released. */
  const stepState = { zoneId: null, which: null, value: null };

  const commitStep = Nobo.debounce(() => {
    if (stepState.zoneId == null) return;
    const { zoneId, which, value } = stepState;
    stepState.zoneId = null;
    write(() => Nobo.api.setTemps(zoneId, { [which]: value }),
          `${which === 'eco' ? 'Eco' : 'Comfort'} set to ${value.toFixed(1)}\u00B0C`);
  }, 600);

  function applyStep(zoneId, delta) {
    const zone = zones.find(z => z.zone_id === zoneId);
    if (!zone) return;
    const which = activeSetpoint(zone);
    if (!which) return;
    const base = stepState.zoneId === zoneId && stepState.which === which
      ? stepState.value
      : (which === 'eco' ? zone.eco_temperature : zone.comfort_temperature);
    const next = Nobo.clampTemp((base == null ? 21 : base) + delta);
    stepState.zoneId = zoneId;
    stepState.which = which;
    stepState.value = next;

    const label = $(`setVal-${zoneId}`);
    if (label) label.firstChild.textContent = `${next.toFixed(1)}\u00B0`;
    commitStep();
  }

  /* =============================================================
   * Wiring
   * ============================================================= */

  function wireDetail(root) {
    if (!root) return;

    root.querySelectorAll('.mode-btn').forEach(btn => {
      btn.onclick = () => {
        const mode = btn.dataset.mode;
        write(() => Nobo.api.setOverride(btn.dataset.zone, mode),
              mode === 'normal' ? 'Back on the schedule' : `Holding ${Nobo.MODES[mode].label}`);
      };
    });

    root.querySelectorAll('.num-input[data-set]').forEach(input => {
      input.onchange = () => {
        const value = Nobo.clampTemp(parseFloat(input.value));
        if (Number.isNaN(value)) { renderList(); return; }
        input.value = value.toFixed(1);
        write(() => Nobo.api.setTemps(input.dataset.zone, { [input.dataset.set]: value }),
              `${input.dataset.set === 'eco' ? 'Eco' : 'Comfort'} set to ${value.toFixed(1)}\u00B0C`);
      };
    });
  }

  async function toggleRow(zoneId) {
    openId = openId === zoneId ? null : zoneId;
    renderList();
    if (openId && !schedules[openId]) {
      try {
        const res = await Nobo.api.schedule(openId);
        schedules[openId] = res.schedule;
        renderHead(); renderList();
      } catch (_) { /* the room stays usable without today's plan */ }
    }
  }

  function wireBoard() {
    const list = $('list');

    list.addEventListener('click', (ev) => {
      const open = ev.target.closest('[data-open]');
      if (open) { toggleRow(open.dataset.open); return; }

      const step = ev.target.closest('[data-step]');
      if (step) { applyStep(step.dataset.zone, parseFloat(step.dataset.step)); return; }

      const row = ev.target.closest('.row-main');
      if (row && !ev.target.closest('.row-set')) {
        toggleRow(row.parentElement.dataset.zone);
      }
    });

    // Press and hold to repeat, so a five degree change is one gesture.
    let holdTimer = null;
    let repeatTimer = null;
    const stopHold = () => {
      clearTimeout(holdTimer); clearInterval(repeatTimer);
      holdTimer = repeatTimer = null;
    };
    list.addEventListener('pointerdown', (ev) => {
      const step = ev.target.closest('[data-step]');
      if (!step) return;
      const delta = parseFloat(step.dataset.step);
      const zoneId = step.dataset.zone;
      holdTimer = setTimeout(() => {
        repeatTimer = setInterval(() => applyStep(zoneId, delta), 110);
      }, 450);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(
      e => list.addEventListener(e, stopHold));

    document.querySelectorAll('#houseMode .seg').forEach(btn => {
      btn.onclick = () => {
        const mode = btn.dataset.mode;
        write(() => Nobo.api.setGlobalMode(mode),
              mode === 'home' ? 'All rooms back on their schedules'
                              : `Whole house set to ${Nobo.MODES[mode].label}`);
      };
    });

    $('sortBy').onchange = (ev) => { sortBy = ev.target.value; renderList(); };

    $('onlyHeating').onclick = () => {
      onlyHeating = !onlyHeating;
      $('onlyHeating').setAttribute('aria-pressed', String(onlyHeating));
      renderList();
    };

    $('systemBtn').onclick = openSystem;
    $('scrim').onclick = closeSheet;
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') { if (sheetOpen) closeSheet(); else if (openId) toggleRow(openId); }
    });

    wide.addEventListener('change', renderList);
  }

  /* =============================================================
   * System sheet
   * ============================================================= */

  function closeSheet() {
    sheetOpen = false;
    $('sheet').hidden = true;
    $('scrim').hidden = true;
    document.body.style.overflow = '';
    if (lastFocus && lastFocus.isConnected) lastFocus.focus();
  }

  async function openSystem() {
    lastFocus = document.activeElement;
    sheetOpen = true;
    $('sheet').hidden = false;
    $('scrim').hidden = false;
    document.body.style.overflow = 'hidden';

    const head = `
      <div class="sheet-head">
        <h2 id="sheetTitle">System</h2>
        <button class="close-btn" type="button" id="sheetClose" aria-label="Close">\u2715</button>
      </div>`;
    $('sheetInner').innerHTML = head + '<p class="note">Loading\u2026</p>';
    $('sheetClose').onclick = closeSheet;
    $('sheetClose').focus();

    try {
      const [hub, status, me] = await Promise.all([
        Nobo.api.hub(), Nobo.api.status(), Nobo.api.me().catch(() => null),
      ]);
      const away = status.away_schedule || {};
      $('sheetInner').innerHTML = head + `
        <dl class="kv"><dt>Hub</dt><dd>${esc(hub.name)}</dd></dl>
        <dl class="kv"><dt>Serial</dt><dd>${esc(hub.serial)}</dd></dl>
        <dl class="kv"><dt>Software</dt><dd>${esc(hub.software_version)}</dd></dl>
        <dl class="kv"><dt>Connection</dt><dd>${status.demo_mode ? 'Demo (simulated)' : 'Real hub'}</dd></dl>
        <dl class="kv"><dt>Time zone</dt><dd>${esc(status.timezone)}</dd></dl>
        <dl class="kv"><dt>Away schedule</dt><dd>${away.enabled ? 'On' : 'Off'}</dd></dl>
        <dl class="kv"><dt>Signed in</dt><dd>${esc(me ? (me.username || me.name || 'unknown') : 'unknown')}</dd></dl>
        <p class="note" style="margin-top:var(--sp-4)">Hub connection, users, devices, the weekly schedule
           and the command log all stay in the current app. This concept covers everyday control.</p>
        <p style="margin-top:var(--sp-3)"><a class="link-btn" href="/">Open the current app</a></p>`;
      $('sheetClose').onclick = closeSheet;
    } catch (err) {
      Nobo.toast(`Could not read system information: ${err.message}`, 'error');
    }
  }

  /* =============================================================
   * Boot
   * ============================================================= */

  function setConnection(ok, how) {
    const el = $('conn');
    el.classList.toggle('is-live', ok);
    el.classList.toggle('is-offline', !ok);
    $('connText').textContent = !ok ? 'Reconnecting' : how === 'polling' ? 'Live (polling)' : 'Live';
  }

  async function init() {
    $('list').innerHTML = Array.from({ length: 6 },
      () => '<div class="skel-row" aria-hidden="true"></div>').join('');
    wireBoard();

    try {
      zones = await Nobo.api.zones();
    } catch (err) {
      $('list').innerHTML = `<div class="errorbox"><h3>Could not reach the heating system</h3><p>${esc(err.message)}</p></div>`;
      $('headStatus').textContent = 'The heating system is not responding.';
      return;
    }

    renderHead();
    renderList();

    // Today's plan for every room, so the header can name the next change
    // across the whole house rather than only for an opened room.
    Promise.all(zones.map(z =>
      Nobo.api.schedule(z.zone_id)
        .then(res => { schedules[z.zone_id] = res.schedule; })
        .catch(() => {})
    )).then(() => { renderHead(); renderList(); });

    Nobo.subscribe((fresh) => {
      if (pending > 0) return;
      if (document.activeElement && document.activeElement.classList.contains('num-input')) return;
      zones = fresh;
      renderHead();
      renderList();
    }, setConnection);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
