/*
 * Concept A - "At a Glance"
 *
 * The whole house is one grid. Selecting a room opens a drawer that answers
 * "which device is this, how warm is it, and what do I change" in one view.
 *
 * Reads and writes the existing API only. Zone/device/hub management is not
 * reimplemented here - the drawer links back to the production screens.
 */

(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = Nobo.escapeHtml;

  let zones = [];
  let openZoneId = null;
  let drawerOpen = false;
  let currentSchedule = null;
  let editing = null;      // 'comfort' | 'eco' - which setpoint the slider edits
  let pending = false;     // suppress re-render while a write is in flight
  let lastFocus = null;

  /* =============================================================
   * House summary
   * ============================================================= */

  function houseSentence(s) {
    if (!s.zoneCount) return 'No zones are configured yet.';
    if (s.heatingCount === 0) {
      return 'Every room has reached its target temperature.';
    }
    const room = s.heatingCount === 1 ? 'room is' : 'rooms are';
    const cold = s.coldest
      ? ` Coldest right now is <em>${esc(s.coldest.name)}</em> at ${Nobo.fmtTemp(s.coldest.current_temperature)}\u00B0.`
      : '';
    return `<em>${s.heatingCount} ${room}</em> warming up.${cold}`;
  }

  function renderHouse() {
    const s = Nobo.houseSummary(zones);
    $('houseLine').innerHTML = houseSentence(s);

    $('houseStats').innerHTML = `
      <div>
        <div class="stat-k">Average</div>
        <div class="stat-v">${Nobo.fmtTemp(s.averageTemp)}<small>\u00B0C</small></div>
      </div>
      <div>
        <div class="stat-k">Heating</div>
        <div class="stat-v">${s.heatingCount}<small> of ${s.zoneCount}</small></div>
      </div>
      <div>
        <div class="stat-k">Overridden</div>
        <div class="stat-v">${s.overriddenCount}<small> of ${s.zoneCount}</small></div>
      </div>`;

    // The house mode is derived from the zones rather than remembered in a
    // variable, so it survives a page reload and cannot claim "Home" while
    // every zone is actually on Away.
    document.querySelectorAll('#houseMode .seg').forEach(btn => {
      btn.setAttribute('aria-pressed', String(btn.dataset.mode === s.mode));
    });
    const label = $('houseModeLabel');
    label.textContent = s.mode === 'mixed'
      ? 'House mode \u2014 rooms differ'
      : 'House mode';
  }

  /* =============================================================
   * Room tiles
   * ============================================================= */

  function gaugeWidth(zone) {
    // How close the room is to its target, floored so the bar never reads
    // as empty when a room is only slightly cold.
    const cur = zone.current_temperature;
    const target = Nobo.targetTemp(zone);
    if (cur == null || target == null) return 0;
    if (cur >= target) return 100;
    const gap = Math.min(4, target - cur);      // 4 degrees below target = empty
    return Math.max(6, Math.round((1 - gap / 4) * 100));
  }

  function tileHtml(zone) {
    const heat = Nobo.heatState(zone);
    const heatInfo = Nobo.HEAT_STATE[heat];
    const mode = zone.current_mode || 'normal';
    const effective = Nobo.effectiveMode(zone);
    const target = Nobo.targetTemp(zone);

    const modeText = mode === 'normal'
      ? `Schedule \u00B7 ${Nobo.MODES[effective].label}`
      : `${Nobo.MODES[mode].label} override`;

    const serials = zone.components || [];
    const primary = serials[0];
    const model = primary ? Nobo.deviceName(primary) : 'No device';
    const extra = serials.length > 1 ? `+${serials.length - 1} more in this room` : 'in this room';

    const deviceBand = primary ? `
      <div class="tile-devices">
        <div class="np-device">${Nobo.deviceImg(primary)}</div>
        <div class="device-meta">
          <div class="device-model">${esc(model)}</div>
          <div class="device-count">${esc(extra)}</div>
        </div>
        <span class="tile-more" aria-hidden="true">\u203A</span>
      </div>` : `
      <div class="tile-devices">
        <div class="device-meta"><div class="device-count">No devices assigned</div></div>
        <span class="tile-more" aria-hidden="true">\u203A</span>
      </div>`;

    return `
      <button class="tile" type="button" data-heat="${heat}" data-zone="${esc(zone.zone_id)}"
              aria-label="${esc(zone.name)}, ${Nobo.fmtTemp(zone.current_temperature)} degrees, ${heatInfo.label}, ${modeText}. Open controls.">
        <div class="tile-body">
          <div class="tile-head">
            <span class="tile-name">
              ${zone.icon ? `<span class="room-icon" aria-hidden="true">${esc(zone.icon)}</span>` : ''}
              <span class="room-text">${esc(zone.name)}</span>
            </span>
            <span class="chip" data-heat="${heat}" title="${esc(heatInfo.hint)}">
              <span class="chip-glyph" aria-hidden="true">${heat === 'heating' ? '\u25B2' : heat === 'holding' ? '\u25CF' : '\u2014'}</span>
              ${heatInfo.label}
            </span>
          </div>
          <div class="tile-temp">
            <span class="temp-now">${Nobo.bigTemp(zone.current_temperature)}</span>
            <span class="temp-target">Target <b>${target == null ? '\u2014' : Nobo.fmtTemp(target) + '\u00B0'}</b></span>
          </div>
          <div class="gauge" aria-hidden="true"><div class="gauge-fill" style="width:${gaugeWidth(zone)}%"></div></div>
          <div class="tile-mode">
            <span class="dot" data-mode="${mode}" aria-hidden="true"></span>${esc(modeText)}
          </div>
        </div>
        ${deviceBand}
      </button>`;
  }

  function renderGrid() {
    const grid = $('grid');
    grid.setAttribute('aria-busy', 'false');
    if (!zones.length) {
      grid.innerHTML = `<div class="empty">
        <h3>No zones yet</h3>
        <p>Add a zone in the current app, then come back to this concept.</p>
      </div>`;
      $('roomsHint').textContent = '';
      return;
    }
    grid.innerHTML = zones.map(tileHtml).join('');
    const s = Nobo.houseSummary(zones);
    $('roomsHint').textContent = `${s.zoneCount} rooms \u00B7 select one to control it`;
  }

  /* =============================================================
   * Drawer
   * ============================================================= */

  function scheduleStrip(schedule) {
    const today = Nobo.todaySchedule(schedule);
    if (!today || !today.blocks.length) {
      return '<p class="note">No schedule blocks for today.</p>';
    }
    const segs = today.blocks.map(b => {
      const from = Nobo.minutesOf(b.start);
      const to = Nobo.minutesOf(b.end);
      const width = ((to - from) / 1440) * 100;
      return `<div class="strip-seg" data-mode="${esc(b.mode)}" style="width:${width}%"
                   title="${esc(b.start)}\u2013${esc(b.end)} ${esc(Nobo.MODES[b.mode] ? Nobo.MODES[b.mode].label : b.mode)}"></div>`;
    }).join('');
    const nowPct = (today.nowMin / 1440) * 100;
    const next = today.next
      ? `<p class="next-change">Next change: <b>${esc(Nobo.MODES[today.next.mode] ? Nobo.MODES[today.next.mode].label : today.next.mode)}</b> at <b>${esc(today.next.start)}</b></p>`
      : '<p class="next-change">No further changes today.</p>';
    return `
      <div class="strip" role="img" aria-label="Today's schedule">
        ${segs}<div class="strip-now" style="left:${nowPct}%"></div>
      </div>
      <div class="strip-legend"><span>00:00</span><span>12:00</span><span>24:00</span></div>
      ${next}`;
  }

  function setterBlock(zone) {
    const effective = Nobo.effectiveMode(zone);
    const which = editing || (effective === 'eco' ? 'eco' : 'comfort');
    editing = which;

    if (!zone.supports_temp_adjust) {
      return `<div class="block">
        <h3>Temperature</h3>
        <p class="note">The devices in this room have their comfort and eco temperatures set
        by hand on the device itself, so there is nothing to change here.</p>
        <div class="locked-row"><span>Away temperature</span>
          <b>${Nobo.fmtTemp(zone.away_temperature != null ? zone.away_temperature : 7)}\u00B0C</b></div>
      </div>`;
    }

    const value = which === 'eco' ? zone.eco_temperature : zone.comfort_temperature;
    const shown = value == null ? 21 : value;

    return `<div class="block">
      <h3>Temperature</h3>
      <div class="segmented" role="group" aria-label="Which temperature to change" style="margin-bottom:var(--sp-4)">
        <button class="seg np-tap" type="button" data-edit="comfort" aria-pressed="${which === 'comfort'}">Comfort</button>
        <button class="seg np-tap" type="button" data-edit="eco" aria-pressed="${which === 'eco'}">Eco</button>
      </div>
      <div class="setter">
        <button class="step-btn" type="button" id="stepDown" aria-label="Lower by half a degree">\u2212</button>
        <div class="setter-mid">
          <div class="setter-value" id="setVal">${Nobo.fmtTemp(shown)}<span class="deg">\u00B0C</span></div>
          <input type="range" id="tempRange"
                 min="${Nobo.TEMP_MIN}" max="${Nobo.TEMP_MAX}" step="0.5" value="${shown}"
                 aria-label="${which === 'eco' ? 'Eco' : 'Comfort'} temperature in degrees Celsius">
          <div class="scale" aria-hidden="true"><span>${Nobo.TEMP_MIN}\u00B0</span><span>${Nobo.TEMP_MAX}\u00B0</span></div>
        </div>
        <button class="step-btn" type="button" id="stepUp" aria-label="Raise by half a degree">+</button>
      </div>
      <p class="setter-caption">${which === 'comfort'
        ? 'Used when the room is in Comfort, either from the schedule or from an override.'
        : 'Used when the room is in Eco.'}</p>
      <div class="locked-row"><span>Away temperature</span>
        <b>${Nobo.fmtTemp(zone.away_temperature != null ? zone.away_temperature : 7)}\u00B0C</b></div>
      <p class="note">Away is fixed by the Nob\u00F8 system and cannot be changed.</p>
    </div>`;
  }

  function drawerHtml(zone, schedule) {
    const heat = Nobo.heatState(zone);
    const heatInfo = Nobo.HEAT_STATE[heat];
    const mode = zone.current_mode || 'normal';
    const target = Nobo.targetTemp(zone);
    const serials = zone.components || [];
    const primary = serials[0];

    const hero = primary ? `
      <div class="hero">
        <div class="np-device">${Nobo.deviceImg(primary)}</div>
        <div class="hero-meta">
          <div class="hero-model">${esc(Nobo.deviceName(primary))}</div>
          <div class="hero-serial">${esc((zone.components_display || [])[0] || primary)}</div>
        </div>
      </div>` : '';

    const devices = serials.map((serial, i) => `
      <div class="dev-row">
        <div class="np-device">${Nobo.deviceImg(serial)}</div>
        <div>
          <div class="dev-row-name">${esc((zone.components_names || [])[i] || Nobo.deviceName(serial))}</div>
          <div class="dev-row-sub">${esc(Nobo.deviceName(serial))} \u00B7 ${esc((zone.components_display || [])[i] || serial)}</div>
        </div>
      </div>`).join('') || '<p class="note">No devices are assigned to this room.</p>';

    const modes = ['normal', 'comfort', 'eco', 'away'].map(m => `
      <button class="mode-btn" type="button" data-mode="${m}" aria-pressed="${mode === m}">
        <span class="mb-glyph" aria-hidden="true">${Nobo.MODES[m].glyph}</span>
        ${Nobo.MODES[m].label}
      </button>`).join('');

    return `
      <div class="drawer-head">
        <h2 id="drawerTitle">
          ${zone.icon ? `<span aria-hidden="true">${esc(zone.icon)}</span>` : ''}${esc(zone.name)}
        </h2>
        <button class="close-btn" type="button" id="drawerClose" aria-label="Close">\u2715</button>
      </div>

      ${hero}

      <div class="readout">
        <span class="readout-now">${Nobo.bigTemp(zone.current_temperature)}</span>
        <span class="chip" data-heat="${heat}" title="${esc(heatInfo.hint)}">
          <span class="chip-glyph" aria-hidden="true">${heat === 'heating' ? '\u25B2' : heat === 'holding' ? '\u25CF' : '\u2014'}</span>
          ${heatInfo.label}
        </span>
      </div>
      <p class="setter-caption">Measured now \u00B7 aiming for ${target == null ? '\u2014' : Nobo.fmtTemp(target) + '\u00B0C'}</p>

      ${setterBlock(zone)}

      <div class="block">
        <h3>Mode</h3>
        <div class="modes" role="group" aria-label="Room mode">${modes}</div>
        <p class="setter-caption">Schedule follows the weekly plan. The other three hold until you change them back.</p>
      </div>

      <div class="block">
        <h3>Today</h3>
        ${scheduleStrip(schedule)}
      </div>

      <div class="block">
        <h3>Devices in this room</h3>
        ${devices}
      </div>

      <details class="advanced">
        <summary>Room settings</summary>
        <p>Renaming the room, moving or replacing devices, editing the weekly schedule
           and deleting the room all live in the current app. This prototype focuses on
           everyday control.</p>
        <a class="link-btn" href="/#devices">Open the current app</a>
      </details>

      <p class="note">Heating state is estimated by comparing the measured temperature with the
         target. The hub does not report element power directly.</p>`;
  }

  async function openDrawer(zoneId) {
    const zone = zones.find(z => z.zone_id === zoneId);
    if (!zone) return;
    openZoneId = zoneId;
    editing = null;
    currentSchedule = null;
    drawerOpen = true;
    lastFocus = document.activeElement;

    const drawer = $('drawer');
    const scrim = $('scrim');
    drawer.hidden = false;
    scrim.hidden = false;
    document.body.style.overflow = 'hidden';
    $('drawerInner').innerHTML = drawerHtml(zone, null);
    wireDrawer();
    $('drawerClose').focus();

    // Schedule is a second request; render without it first so the drawer
    // opens instantly rather than waiting on the network.
    try {
      const res = await Nobo.api.schedule(zoneId);
      if (openZoneId === zoneId) {
        currentSchedule = res.schedule;
        const fresh = zones.find(z => z.zone_id === zoneId);
        $('drawerInner').innerHTML = drawerHtml(fresh || zone, currentSchedule);
        wireDrawer();
      }
    } catch (err) {
      /* Schedule is supplementary; the drawer stays usable without it. */
    }
  }

  function closeDrawer() {
    openZoneId = null;
    drawerOpen = false;
    currentSchedule = null;
    $('drawer').hidden = true;
    $('scrim').hidden = true;
    document.body.style.overflow = '';
    if (lastFocus && lastFocus.isConnected) lastFocus.focus();
  }

  function refreshDrawer() {
    if (!openZoneId || pending) return;
    const zone = zones.find(z => z.zone_id === openZoneId);
    if (!zone) { closeDrawer(); return; }
    const range = $('tempRange');
    if (range && document.activeElement === range) return;  // do not fight the user
    const scroll = $('drawerInner').scrollTop;
    $('drawerInner').innerHTML = drawerHtml(zone, currentSchedule);
    wireDrawer();
    $('drawerInner').scrollTop = scroll;
  }

  /* =============================================================
   * Drawer interactions
   * ============================================================= */

  const commitTemp = Nobo.debounce(async (zoneId, which, value) => {
    pending = true;
    try {
      await Nobo.api.setTemps(zoneId, { [which]: value });
      Nobo.toast(`${which === 'eco' ? 'Eco' : 'Comfort'} set to ${value.toFixed(1)}\u00B0C`, 'success');
      zones = await Nobo.api.zones();
      renderHouse(); renderGrid();
    } catch (err) {
      Nobo.toast(`Could not change the temperature: ${err.message}`, 'error');
    } finally {
      pending = false;
    }
  }, 450);

  function wireDrawer() {
    const inner = $('drawerInner');

    const close = $('drawerClose');
    if (close) close.onclick = closeDrawer;

    inner.querySelectorAll('[data-edit]').forEach(btn => {
      btn.onclick = () => { editing = btn.dataset.edit; refreshDrawer(); };
    });

    const range = $('tempRange');
    const label = $('setVal');
    if (range && label) {
      const apply = (v) => {
        const value = Nobo.clampTemp(v);
        range.value = value;
        label.firstChild.textContent = value.toFixed(1);
        commitTemp(openZoneId, editing, value);
      };
      range.oninput  = () => { label.firstChild.textContent = parseFloat(range.value).toFixed(1); };
      range.onchange = () => apply(parseFloat(range.value));
      const down = $('stepDown');
      const up = $('stepUp');
      if (down) down.onclick = () => apply(parseFloat(range.value) - 0.5);
      if (up)   up.onclick   = () => apply(parseFloat(range.value) + 0.5);
    }

    inner.querySelectorAll('.mode-btn').forEach(btn => {
      btn.onclick = async () => {
        const mode = btn.dataset.mode;
        inner.querySelectorAll('.mode-btn').forEach(b =>
          b.setAttribute('aria-pressed', String(b === btn)));
        try {
          await Nobo.api.setOverride(openZoneId, mode);
          Nobo.toast(mode === 'normal'
            ? 'Back on the schedule'
            : `Holding ${Nobo.MODES[mode].label}`, 'success');
          zones = await Nobo.api.zones();
          renderHouse(); renderGrid(); refreshDrawer();
        } catch (err) {
          Nobo.toast(`Could not change the mode: ${err.message}`, 'error');
          refreshDrawer();
        }
      };
    });
  }

  /* =============================================================
   * Wiring
   * ============================================================= */

  function wireStatic() {
    $('grid').addEventListener('click', (ev) => {
      const tile = ev.target.closest('.tile');
      if (tile) openDrawer(tile.dataset.zone);
    });

    $('scrim').addEventListener('click', closeDrawer);

    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && drawerOpen) closeDrawer();
    });

    document.querySelectorAll('#houseMode .seg').forEach(btn => {
      btn.onclick = async () => {
        const mode = btn.dataset.mode;
        try {
          await Nobo.api.setGlobalMode(mode);
          Nobo.toast(mode === 'home'
            ? 'All rooms back on their schedules'
            : `Whole house set to ${Nobo.MODES[mode] ? Nobo.MODES[mode].label : mode}`, 'success');
          zones = await Nobo.api.zones();
          renderHouse(); renderGrid();
        } catch (err) {
          Nobo.toast(`Could not change the house mode: ${err.message}`, 'error');
          renderHouse();
        }
      };
    });

    $('settingsBtn').onclick = openSettings;
  }

  async function openSettings() {
    lastFocus = document.activeElement;
    drawerOpen = true;
    openZoneId = null;
    const drawer = $('drawer');
    drawer.hidden = false;
    $('scrim').hidden = false;
    document.body.style.overflow = 'hidden';
    $('drawerInner').innerHTML = `
      <div class="drawer-head">
        <h2 id="drawerTitle">Settings</h2>
        <button class="close-btn" type="button" id="drawerClose" aria-label="Close">\u2715</button>
      </div>
      <p class="note">Loading\u2026</p>`;
    $('drawerClose').onclick = closeDrawer;

    try {
      const [hub, status, me] = await Promise.all([
        Nobo.api.hub(), Nobo.api.status(), Nobo.api.me().catch(() => null),
      ]);
      $('drawerInner').innerHTML = `
        <div class="drawer-head">
          <h2 id="drawerTitle">Settings</h2>
          <button class="close-btn" type="button" id="drawerClose" aria-label="Close">\u2715</button>
        </div>
        <div class="block" style="border-top:none;margin-top:0;padding-top:0">
          <h3>Hub</h3>
          <dl class="kv">
            <dt>Name</dt><dd>${esc(hub.name)}</dd>
          </dl>
          <dl class="kv"><dt>Serial</dt><dd>${esc(hub.serial)}</dd></dl>
          <dl class="kv"><dt>Software</dt><dd>${esc(hub.software_version)}</dd></dl>
          <dl class="kv"><dt>Mode</dt><dd>${status.demo_mode ? 'Demo (simulated)' : 'Connected to a real hub'}</dd></dl>
          <dl class="kv"><dt>Time zone</dt><dd>${esc(status.timezone)}</dd></dl>
        </div>
        <div class="block">
          <h3>Account</h3>
          <dl class="kv"><dt>Signed in as</dt><dd>${esc(me ? (me.username || me.name || 'unknown') : 'unknown')}</dd></dl>
        </div>
        <div class="block">
          <h3>Manage</h3>
          <p class="note" style="margin-top:0">Hub connection, users, devices, the weekly
             schedule and the command log all stay in the current app. This concept covers
             everyday control only.</p>
          <a class="link-btn" href="/">Open the current app</a>
        </div>`;
      $('drawerClose').onclick = closeDrawer;
    } catch (err) {
      Nobo.toast(`Could not read settings: ${err.message}`, 'error');
    }
  }

  function setConnection(ok, how) {
    const el = $('conn');
    el.classList.toggle('is-live', ok);
    el.classList.toggle('is-offline', !ok);
    $('connText').textContent = !ok ? 'Reconnecting'
      : how === 'polling' ? 'Live (polling)' : 'Live';
  }

  async function init() {
    $('grid').innerHTML = Array.from({ length: 6 },
      () => '<div class="skel" aria-hidden="true"></div>').join('');
    wireStatic();

    try {
      zones = await Nobo.api.zones();
      renderHouse();
      renderGrid();
    } catch (err) {
      $('grid').innerHTML = `<div class="errorbox">
        <h3>Could not reach the heating system</h3>
        <p>${esc(err.message)}</p>
      </div>`;
      $('houseLine').textContent = 'The heating system is not responding.';
      return;
    }

    Nobo.subscribe((fresh) => {
      if (pending) return;
      zones = fresh;
      renderHouse();
      renderGrid();
      refreshDrawer();
    }, setConnection);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
