/*
 * Concept B - "Room First"
 *
 * One room fills the screen. Navigation is the rail across the top; there is
 * no page hierarchy at all. The dial is the primary control, and the device
 * artwork is presented as the physical object it represents.
 *
 * Reads and writes the existing API only.
 */

(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = Nobo.escapeHtml;

  let zones = [];
  let currentId = null;
  let deviceIndex = 0;
  let editing = null;        // 'comfort' | 'eco'
  let schedules = {};        // zone_id -> schedule, cached per room
  let pending = false;
  let sheetOpen = false;
  let lastFocus = null;

  /* Dial geometry: a 270 degree sweep with the gap at the bottom, which is
     where a wall thermostat puts it and where a thumb does not cover it. */
  const DIAL = { cx: 130, cy: 130, r: 100, start: 135, sweep: 270 };

  const tempToAngle = (t) =>
    DIAL.start + ((t - Nobo.TEMP_MIN) / (Nobo.TEMP_MAX - Nobo.TEMP_MIN)) * DIAL.sweep;

  const polar = (angle, radius = DIAL.r) => {
    const rad = (angle * Math.PI) / 180;
    return { x: DIAL.cx + radius * Math.cos(rad), y: DIAL.cy + radius * Math.sin(rad) };
  };

  function arcPath(fromAngle, toAngle, radius = DIAL.r) {
    const a = polar(fromAngle, radius);
    const b = polar(toAngle, radius);
    const large = Math.abs(toAngle - fromAngle) > 180 ? 1 : 0;
    return `M ${a.x.toFixed(2)} ${a.y.toFixed(2)} A ${radius} ${radius} 0 ${large} 1 ${b.x.toFixed(2)} ${b.y.toFixed(2)}`;
  }

  function angleToTemp(angle) {
    // Normalise into the sweep, then reject the dead zone at the bottom so a
    // stray touch below the dial cannot jump the setpoint from 30 to 5.
    let a = angle;
    while (a < DIAL.start) a += 360;
    while (a > DIAL.start + 360) a -= 360;
    const offset = a - DIAL.start;
    if (offset > DIAL.sweep) {
      return offset < DIAL.sweep + (360 - DIAL.sweep) / 2 ? Nobo.TEMP_MAX : Nobo.TEMP_MIN;
    }
    return Nobo.clampTemp(Nobo.TEMP_MIN + (offset / DIAL.sweep) * (Nobo.TEMP_MAX - Nobo.TEMP_MIN));
  }

  /* =============================================================
   * Room rail
   * ============================================================= */

  function renderRail() {
    const rail = $('rail');
    rail.innerHTML = zones.map(z => {
      const heat = Nobo.heatState(z);
      return `
        <button class="rail-chip" type="button" role="tab" data-zone="${esc(z.zone_id)}"
                data-heat="${heat}" aria-selected="${z.zone_id === currentId}">
          <span class="rail-chip-name">
            ${z.icon ? `<span aria-hidden="true">${esc(z.icon)}</span>` : ''}${esc(z.name)}
          </span>
          <span class="rail-chip-temp">
            <span class="rail-chip-dot" aria-hidden="true"></span>
            ${Nobo.fmtTemp(z.current_temperature)}\u00B0 \u00B7 ${Nobo.HEAT_STATE[heat].label}
          </span>
        </button>`;
    }).join('');

    rail.querySelectorAll('.rail-chip').forEach(chip => {
      chip.onclick = () => selectRoom(chip.dataset.zone);
    });

    const idx = zones.findIndex(z => z.zone_id === currentId);
    $('prevRoom').disabled = idx <= 0;
    $('nextRoom').disabled = idx < 0 || idx >= zones.length - 1;

    const active = rail.querySelector('[aria-selected="true"]');
    if (active) active.scrollIntoView({ block: 'nearest', inline: 'center' });
  }

  /* =============================================================
   * The stage
   * ============================================================= */

  function hardwareHtml(zone) {
    const serials = zone.components || [];
    if (!serials.length) {
      return `
        <div class="hardware">
          <h1 class="room-title">
            ${zone.icon ? `<span class="room-icon" aria-hidden="true">${esc(zone.icon)}</span>` : ''}${esc(zone.name)}
          </h1>
          <div class="device-stage">
            <p class="note">No devices are assigned to this room yet, so there is no hardware to show.</p>
          </div>
        </div>`;
    }

    const i = Math.min(deviceIndex, serials.length - 1);
    const serial = serials[i];
    const model = Nobo.deviceName(serial);
    const name = (zone.components_names || [])[i] || model;
    const display = (zone.components_display || [])[i] || serial;

    const picker = serials.length > 1 ? `
      <div class="device-picker" role="group" aria-label="Devices in this room">
        ${serials.map((s, n) => `
          <button class="device-thumb" type="button" data-dev="${n}" aria-pressed="${n === i}"
                  aria-label="Show ${esc(Nobo.deviceName(s))}">
            <div class="np-device">${Nobo.deviceImg(s)}</div>
            <span>${esc(Nobo.deviceName(s))}</span>
          </button>`).join('')}
      </div>` : '';

    return `
      <div class="hardware">
        <h1 class="room-title">
          ${zone.icon ? `<span class="room-icon" aria-hidden="true">${esc(zone.icon)}</span>` : ''}${esc(zone.name)}
        </h1>
        <p class="room-sub">${serials.length} device${serials.length === 1 ? '' : 's'} in this room</p>
        <div class="device-stage">
          <div class="np-device">${Nobo.deviceImg(serial, '', `${model} in ${zone.name}`)}</div>
          <div class="device-facts">
            <div><div class="fact-k">Called</div><div class="fact-v">${esc(name)}</div></div>
            <div><div class="fact-k">Model</div><div class="fact-v">${esc(model)}</div></div>
            <div><div class="fact-k">Serial</div><div class="fact-v">${esc(display)}</div></div>
          </div>
          ${picker}
        </div>
      </div>`;
  }

  function dialHtml(zone) {
    const heat = Nobo.heatState(zone);
    const heatInfo = Nobo.HEAT_STATE[heat];
    const effective = Nobo.effectiveMode(zone);
    const adjustable = zone.supports_temp_adjust && effective !== 'away' && effective !== 'off';

    const which = adjustable
      ? (editing || (effective === 'eco' ? 'eco' : 'comfort'))
      : null;
    if (adjustable) editing = which;

    const setpoint = which === 'eco' ? zone.eco_temperature : zone.comfort_temperature;
    const shown = setpoint == null ? 21 : setpoint;
    const displayed = adjustable ? shown : Nobo.targetTemp(zone);

    const angle = tempToAngle(displayed == null ? Nobo.TEMP_MIN : displayed);
    const handle = polar(angle);
    const curAngle = zone.current_temperature != null
      ? tempToAngle(Math.max(Nobo.TEMP_MIN, Math.min(Nobo.TEMP_MAX, zone.current_temperature)))
      : null;
    const curInner = curAngle != null ? polar(curAngle, DIAL.r - 15) : null;
    const curOuter = curAngle != null ? polar(curAngle, DIAL.r - 4) : null;

    const ticks = [5, 10, 15, 20, 25, 30].map(t => {
      const a = tempToAngle(t);
      const p1 = polar(a, DIAL.r + 8);
      const p2 = polar(a, DIAL.r + 13);
      const lp = polar(a, DIAL.r + 26);
      return `<line class="dial-tick" x1="${p1.x.toFixed(1)}" y1="${p1.y.toFixed(1)}" x2="${p2.x.toFixed(1)}" y2="${p2.y.toFixed(1)}"></line>
              <text class="dial-tick-label" x="${lp.x.toFixed(1)}" y="${lp.y.toFixed(1)}" text-anchor="middle" dominant-baseline="middle">${t}</text>`;
    }).join('');

    const controls = adjustable ? `
      <div class="setpoint-switch" role="group" aria-label="Which temperature the dial sets">
        <button type="button" data-edit="comfort" aria-pressed="${which === 'comfort'}">Comfort</button>
        <button type="button" data-edit="eco" aria-pressed="${which === 'eco'}">Eco</button>
      </div>
      <div class="nudge">
        <button class="nudge-btn" type="button" id="dialDown" aria-label="Lower by half a degree">\u2212</button>
        <p class="dial-target">Set to <b id="dialTargetText">${Nobo.fmtTemp(shown)}\u00B0C</b></p>
        <button class="nudge-btn" type="button" id="dialUp" aria-label="Raise by half a degree">+</button>
      </div>`
      : `<p class="locked-msg">${zone.supports_temp_adjust
          ? 'Away is fixed at ' + Nobo.fmtTemp(zone.away_temperature != null ? zone.away_temperature : 7) +
            '\u00B0C by the Nob\u00F8 system. Switch to Comfort, Eco or Schedule to set a temperature.'
          : 'The devices in this room have their comfort and eco temperatures set by hand on the device itself.'}</p>`;

    return `
      <div class="control">
        <div class="dial-wrap">
          <svg class="dial" viewBox="0 0 260 260" id="dial"
               role="slider" tabindex="${adjustable ? 0 : -1}"
               aria-label="${which === 'eco' ? 'Eco' : 'Comfort'} temperature for ${esc(zone.name)}"
               aria-valuemin="${Nobo.TEMP_MIN}" aria-valuemax="${Nobo.TEMP_MAX}"
               aria-valuenow="${displayed == null ? '' : displayed}"
               aria-valuetext="${displayed == null ? 'not set' : Nobo.fmtTemp(displayed) + ' degrees Celsius'}"
               aria-disabled="${!adjustable}">
            ${ticks}
            <path class="dial-track" d="${arcPath(DIAL.start, DIAL.start + DIAL.sweep)}"
                  fill="none" stroke-width="14" stroke-linecap="round"></path>
            <path class="dial-progress" id="dialProgress" data-mode="${effective}"
                  d="${arcPath(DIAL.start, Math.max(DIAL.start + 0.1, angle))}"
                  fill="none" stroke-width="14" stroke-linecap="round"></path>
            ${curInner ? `<line class="dial-current" x1="${curInner.x.toFixed(1)}" y1="${curInner.y.toFixed(1)}"
                                x2="${curOuter.x.toFixed(1)}" y2="${curOuter.y.toFixed(1)}"></line>` : ''}
            <circle class="dial-handle" id="dialHandle" cx="${handle.x.toFixed(2)}" cy="${handle.y.toFixed(2)}" r="13"></circle>
          </svg>
          <div class="dial-readout">
            <span class="dial-caption">Now</span>
            <span class="dial-now">${Nobo.bigTemp(zone.current_temperature)}</span>
            <span class="chip" data-heat="${heat}" title="${esc(heatInfo.hint)}">
              <span class="chip-glyph" aria-hidden="true">${heat === 'heating' ? '\u25B2' : heat === 'holding' ? '\u25CF' : '\u2014'}</span>
              ${heatInfo.label}
            </span>
          </div>
        </div>
        ${controls}
      </div>`;
  }

  function belowHtml(zone) {
    const mode = zone.current_mode || 'normal';
    const modes = ['normal', 'comfort', 'eco', 'away'].map(m => `
      <button class="mode-btn" type="button" data-mode="${m}" aria-pressed="${mode === m}">
        <span class="mb-glyph" aria-hidden="true">${Nobo.MODES[m].glyph}</span>${Nobo.MODES[m].label}
      </button>`).join('');

    const sched = schedules[zone.zone_id];
    const today = Nobo.todaySchedule(sched);
    let timeline = '<p class="note">Loading today\u2019s schedule\u2026</p>';
    if (today && today.blocks.length) {
      const segs = today.blocks.map(b => {
        const width = ((Nobo.minutesOf(b.end) - Nobo.minutesOf(b.start)) / 1440) * 100;
        const label = Nobo.MODES[b.mode] ? Nobo.MODES[b.mode].label : b.mode;
        return `<div class="timeline-seg" data-mode="${esc(b.mode)}" style="width:${width}%"
                     title="${esc(b.start)}\u2013${esc(b.end)} ${esc(label)}">${width > 11 ? esc(label) : ''}</div>`;
      }).join('');
      const next = today.next
        ? `<p class="next-change">Next: <b>${esc(Nobo.MODES[today.next.mode] ? Nobo.MODES[today.next.mode].label : today.next.mode)}</b> at <b>${esc(today.next.start)}</b></p>`
        : '<p class="next-change">No further changes today.</p>';
      timeline = `
        <div class="timeline" role="img" aria-label="Today\u2019s schedule for this room">
          ${segs}<div class="timeline-now" style="left:${(today.nowMin / 1440) * 100}%"></div>
        </div>
        <div class="timeline-legend"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
        ${next}`;
    } else if (sched) {
      timeline = '<p class="note">No schedule blocks for today.</p>';
    }

    return `
      <div class="below">
        <div>
          <p class="block-label">Mode</p>
          <div class="modes" role="group" aria-label="Room mode">${modes}</div>
        </div>
        <div>
          <p class="block-label">Today</p>
          ${timeline}
        </div>
        <p class="note">Heating state is estimated by comparing the measured temperature with the target;
           the hub does not report element power directly. Renaming rooms, managing devices and editing the
           weekly schedule stay in the current app.</p>
      </div>`;
  }

  function renderStage() {
    const zone = zones.find(z => z.zone_id === currentId);
    const stage = $('stage');
    if (!zone) {
      stage.innerHTML = '<p class="stage-loading">No rooms are configured yet.</p>';
      return;
    }
    stage.innerHTML = hardwareHtml(zone) + dialHtml(zone) + belowHtml(zone);
    wireStage(zone);
  }

  /* =============================================================
   * Interaction
   * ============================================================= */

  const commitTemp = Nobo.debounce(async (zoneId, which, value) => {
    pending = true;
    try {
      await Nobo.api.setTemps(zoneId, { [which]: value });
      Nobo.toast(`${which === 'eco' ? 'Eco' : 'Comfort'} set to ${value.toFixed(1)}\u00B0C`, 'success');
      zones = await Nobo.api.zones();
      renderRail();
    } catch (err) {
      Nobo.toast(`Could not change the temperature: ${err.message}`, 'error');
      zones = await Nobo.api.zones().catch(() => zones);
    } finally {
      pending = false;
      renderStage();
    }
  }, 450);

  function wireStage(zone) {
    const stage = $('stage');

    stage.querySelectorAll('[data-dev]').forEach(btn => {
      btn.onclick = () => { deviceIndex = Number(btn.dataset.dev); renderStage(); };
    });

    stage.querySelectorAll('[data-edit]').forEach(btn => {
      btn.onclick = () => { editing = btn.dataset.edit; renderStage(); };
    });

    stage.querySelectorAll('.mode-btn').forEach(btn => {
      btn.onclick = async () => {
        const mode = btn.dataset.mode;
        try {
          await Nobo.api.setOverride(zone.zone_id, mode);
          Nobo.toast(mode === 'normal' ? 'Back on the schedule' : `Holding ${Nobo.MODES[mode].label}`, 'success');
          zones = await Nobo.api.zones();
        } catch (err) {
          Nobo.toast(`Could not change the mode: ${err.message}`, 'error');
        }
        renderRail(); renderStage();
      };
    });

    const dial = $('dial');
    if (!dial || dial.getAttribute('aria-disabled') === 'true') return;

    const setFromTemp = (value) => {
      const t = Nobo.clampTemp(value);
      const angle = tempToAngle(t);
      const handle = polar(angle);
      $('dialHandle').setAttribute('cx', handle.x.toFixed(2));
      $('dialHandle').setAttribute('cy', handle.y.toFixed(2));
      $('dialProgress').setAttribute('d', arcPath(DIAL.start, Math.max(DIAL.start + 0.1, angle)));
      const text = $('dialTargetText');
      if (text) text.textContent = `${t.toFixed(1)}\u00B0C`;
      dial.setAttribute('aria-valuenow', t);
      dial.setAttribute('aria-valuetext', `${t.toFixed(1)} degrees Celsius`);
      commitTemp(zone.zone_id, editing, t);
    };

    const currentValue = () => parseFloat(dial.getAttribute('aria-valuenow')) || 21;

    const fromPointer = (ev) => {
      const rect = dial.getBoundingClientRect();
      // The SVG is a square viewBox scaled to the rendered box, so the
      // centre is simply the middle of that box.
      const dx = ev.clientX - (rect.left + rect.width / 2);
      const dy = ev.clientY - (rect.top + rect.height / 2);
      const angle = (Math.atan2(dy, dx) * 180) / Math.PI;
      setFromTemp(angleToTemp(angle));
    };

    let dragging = false;
    dial.addEventListener('pointerdown', (ev) => {
      dragging = true;
      dial.setPointerCapture(ev.pointerId);
      dial.focus();
      fromPointer(ev);
    });
    dial.addEventListener('pointermove', (ev) => { if (dragging) fromPointer(ev); });
    const stop = (ev) => {
      if (!dragging) return;
      dragging = false;
      try { dial.releasePointerCapture(ev.pointerId); } catch (_) { /* already released */ }
      commitTemp.flush();
    };
    dial.addEventListener('pointerup', stop);
    dial.addEventListener('pointercancel', stop);

    dial.addEventListener('keydown', (ev) => {
      const step = ev.shiftKey ? 1 : 0.5;
      if (ev.key === 'ArrowUp' || ev.key === 'ArrowRight') { setFromTemp(currentValue() + step); ev.preventDefault(); }
      else if (ev.key === 'ArrowDown' || ev.key === 'ArrowLeft') { setFromTemp(currentValue() - step); ev.preventDefault(); }
      else if (ev.key === 'Home') { setFromTemp(Nobo.TEMP_MIN); ev.preventDefault(); }
      else if (ev.key === 'End') { setFromTemp(Nobo.TEMP_MAX); ev.preventDefault(); }
    });

    const down = $('dialDown');
    const up = $('dialUp');
    if (down) down.onclick = () => setFromTemp(currentValue() - 0.5);
    if (up)   up.onclick   = () => setFromTemp(currentValue() + 0.5);
  }

  /* =============================================================
   * Room selection
   * ============================================================= */

  async function selectRoom(zoneId) {
    if (!zones.some(z => z.zone_id === zoneId)) return;
    currentId = zoneId;
    deviceIndex = 0;
    editing = null;
    renderRail();
    renderStage();

    if (!schedules[zoneId]) {
      try {
        const res = await Nobo.api.schedule(zoneId);
        schedules[zoneId] = res.schedule;
        if (currentId === zoneId) renderStage();
      } catch (_) { /* the room stays usable without today's plan */ }
    }
  }

  function step(delta) {
    const idx = zones.findIndex(z => z.zone_id === currentId);
    const next = zones[idx + delta];
    if (next) selectRoom(next.zone_id);
  }

  /* =============================================================
   * Sheets
   * ============================================================= */

  function openSheet(html) {
    lastFocus = document.activeElement;
    sheetOpen = true;
    $('sheet').hidden = false;
    $('scrim').hidden = false;
    document.body.style.overflow = 'hidden';
    $('sheetInner').innerHTML = html;
    const close = $('sheetClose');
    if (close) { close.onclick = closeSheet; close.focus(); }
  }

  function closeSheet() {
    sheetOpen = false;
    $('sheet').hidden = true;
    $('scrim').hidden = true;
    document.body.style.overflow = '';
    if (lastFocus && lastFocus.isConnected) lastFocus.focus();
  }

  function openAllRooms() {
    const cards = zones.map(z => {
      const heat = Nobo.heatState(z);
      const target = Nobo.targetTemp(z);
      return `
        <button class="mini" type="button" data-zone="${esc(z.zone_id)}" data-heat="${heat}">
          <div class="mini-name">${z.icon ? esc(z.icon) + ' ' : ''}${esc(z.name)}</div>
          <div class="mini-temp">${Nobo.fmtTemp(z.current_temperature)}<span class="deg">\u00B0C</span></div>
          <div class="mini-sub">${Nobo.HEAT_STATE[heat].label} \u00B7 target ${target == null ? '\u2014' : Nobo.fmtTemp(target) + '\u00B0'}</div>
        </button>`;
    }).join('');

    openSheet(`
      <div class="sheet-head">
        <h2 id="sheetTitle">All rooms</h2>
        <button class="close-btn" type="button" id="sheetClose" aria-label="Close">\u2715</button>
      </div>
      <div class="mini-grid">${cards || '<p class="note">No rooms yet.</p>'}</div>`);

    $('sheetInner').querySelectorAll('.mini').forEach(btn => {
      btn.onclick = () => { closeSheet(); selectRoom(btn.dataset.zone); };
    });
  }

  async function openHouse() {
    const s = Nobo.houseSummary(zones);
    openSheet(`
      <div class="sheet-head">
        <h2 id="sheetTitle">House</h2>
        <button class="close-btn" type="button" id="sheetClose" aria-label="Close">\u2715</button>
      </div>
      <p class="note" style="margin-bottom:var(--sp-4)">
        ${s.heatingCount} of ${s.zoneCount} rooms warming up \u00B7 average ${Nobo.fmtTemp(s.averageTemp)}\u00B0C
      </p>
      <p class="block-label">Set every room</p>
      <div class="segmented" role="group" aria-label="House mode" id="houseMode">
        <button type="button" data-mode="home"    aria-pressed="${s.mode === 'home'}">Schedule</button>
        <button type="button" data-mode="comfort" aria-pressed="${s.mode === 'comfort'}">Comfort</button>
        <button type="button" data-mode="eco"     aria-pressed="${s.mode === 'eco'}">Eco</button>
        <button type="button" data-mode="away"    aria-pressed="${s.mode === 'away'}">Away</button>
      </div>
      ${s.mode === 'mixed' ? '<p class="note" style="margin-top:var(--sp-2)">Rooms are currently in different modes.</p>' : ''}
      <div id="houseFacts" style="margin-top:var(--sp-5)"></div>
      <p class="block-label" style="margin-top:var(--sp-5)">Manage</p>
      <a class="link-btn" href="/">Open the current app</a>
      <p class="note" style="margin-top:var(--sp-3)">Hub connection, users, devices, the weekly schedule
         and the command log stay in the current app.</p>`);

    $('sheetInner').querySelectorAll('#houseMode button').forEach(btn => {
      btn.onclick = async () => {
        try {
          await Nobo.api.setGlobalMode(btn.dataset.mode);
          Nobo.toast(btn.dataset.mode === 'home'
            ? 'All rooms back on their schedules'
            : `Whole house set to ${Nobo.MODES[btn.dataset.mode].label}`, 'success');
          zones = await Nobo.api.zones();
          renderRail(); renderStage(); closeSheet();
        } catch (err) {
          Nobo.toast(`Could not change the house mode: ${err.message}`, 'error');
        }
      };
    });

    try {
      const [hub, status, me] = await Promise.all([
        Nobo.api.hub(), Nobo.api.status(), Nobo.api.me().catch(() => null),
      ]);
      const facts = $('houseFacts');
      if (facts) {
        facts.innerHTML = `
          <p class="block-label">System</p>
          <dl class="kv"><dt>Hub</dt><dd>${esc(hub.name)} \u00B7 ${esc(hub.software_version)}</dd></dl>
          <dl class="kv"><dt>Serial</dt><dd>${esc(hub.serial)}</dd></dl>
          <dl class="kv"><dt>Mode</dt><dd>${status.demo_mode ? 'Demo (simulated)' : 'Real hub'}</dd></dl>
          <dl class="kv"><dt>Signed in</dt><dd>${esc(me ? (me.username || me.name || 'unknown') : 'unknown')}</dd></dl>`;
      }
    } catch (_) { /* the sheet is still useful without system facts */ }
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
    $('prevRoom').onclick = () => step(-1);
    $('nextRoom').onclick = () => step(1);
    $('allRoomsBtn').onclick = openAllRooms;
    $('houseBtn').onclick = openHouse;
    $('scrim').onclick = closeSheet;
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && sheetOpen) closeSheet();
    });

    try {
      zones = await Nobo.api.zones();
    } catch (err) {
      $('stage').innerHTML = `<p class="stage-error">Could not reach the heating system: ${esc(err.message)}</p>`;
      return;
    }

    if (zones.length) selectRoom(zones[0].zone_id);
    else { renderRail(); renderStage(); }

    Nobo.subscribe((fresh) => {
      if (pending) return;
      zones = fresh;
      if (!zones.some(z => z.zone_id === currentId) && zones.length) currentId = zones[0].zone_id;
      renderRail();
      // Do not redraw the dial mid-drag or while it has focus.
      const dial = $('dial');
      if (!dial || document.activeElement !== dial) renderStage();
    }, setConnection);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
