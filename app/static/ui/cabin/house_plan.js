/*
 * house_plan.js — the Blueprint: the floor plan from above, for placing
 * heaters and sensors (docs/HOUSE.md, stage 3). Administrators only.
 *
 * SVG rather than WebGL. It is flat, crisp at any size, works where the
 * house view cannot, and every door and window is an element a finger can
 * hit. ``planSvg`` only builds a string, so it is tested in node; ``mountPlan``
 * puts it on screen and turns a tap into a point on the plan.
 *
 * It only ever shows. What a tap means is decided by cabin.js, and every
 * placement is confirmed in a sheet before anything is saved.
 */

import * as L from '/static/ui/cabin/house_logic.js';

const esc = (value) => String(value).replace(/[&<>"']/g, ch => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[ch]));

const n = (value) => (Math.round(value * 1000) / 1000).toString();

/* Monospace capitals with a little letter spacing: about this wide per
   character, as a share of the font size. */
const CHAR_WIDTH = 0.66;

/**
 * How to write a room's name inside a ``w`` by ``h`` metre box: the lines,
 * the font size and whether to turn it on its side. As large as fits, up to
 * ``size``, wrapped at spaces onto at most three lines, and never below half
 * of ``size``: a name that small would not be read, and the room is still
 * findable by its doors.
 */
export function fitLabel(name, w, h, size) {
  const words = String(name).trim().split(/\s+/).filter(Boolean);
  if (!words.length) return null;
  const layouts = [];
  for (let count = 1; count <= Math.min(3, words.length); count++) {
    // Greedy wrap into ``count`` lines of roughly equal length.
    const target = words.join(' ').length / count;
    const lines = [];
    let line = '';
    for (const word of words) {
      if (line && (line.length + 1 + word.length) > target && lines.length < count - 1) {
        lines.push(line);
        line = word;
      } else {
        line = line ? `${line} ${word}` : word;
      }
    }
    lines.push(line);
    const longest = Math.max(...lines.map(l => l.length));
    for (const rotate of [false, true]) {
      const [bw, bh] = rotate ? [h, w] : [w, h];
      const fit = Math.min(size, (bw * 0.88) / (longest * CHAR_WIDTH), (bh * 0.8) / (lines.length * 1.2));
      layouts.push({ lines, size: fit, rotate });
    }
  }
  // Largest type wins; ties go to fewer lines, then to reading flat.
  layouts.sort((a, b) => (b.size - a.size) || (a.lines.length - b.lines.length) || (a.rotate - b.rotate));
  const best = layouts[0];
  return { lines: best.lines, size: Math.max(best.size, size * 0.5), rotate: best.rotate };
}

/* The plan's y axis is north, and SVG's points down, so y is drawn negated. */
const P = ([x, y]) => `${n(x)},${n(-y)}`;

function wallNormal(wall) {
  const [ax, ay] = wall.a;
  const [bx, by] = wall.b;
  const len = Math.hypot(bx - ax, by - ay) || 1;
  return [-(by - ay) / len, (bx - ax) / len];
}

/** Which way is into a room from this wall, ``t`` along it: +1 left, -1 right, 0 neither. */
function insideSide(model, wall, t, preferZone) {
  const [left, right] = L.roomsBeside(model, wall, t);
  if (preferZone != null) {
    if (left && String(left.zone_id) === String(preferZone)) return 1;
    if (right && String(right.zone_id) === String(preferZone)) return -1;
  }
  if (left && !right) return 1;
  if (right && !left) return -1;
  return left ? 1 : 0;
}

/**
 * The plan for one level, as an SVG string.
 *
 * opts.zones      Map of zone id to zone (names only)
 * opts.heaters    Map of serial to device, for which zone a heater is in
 * opts.selected   { kind, id } being placed, or null
 * opts.target     'opening' | 'point' | 'heater' | null: what a tap places on
 */
export function planSvg(model, level, opts = {}) {
  const zones = opts.zones || new Map();
  const heaters = opts.heaters || new Map();
  const selected = opts.selected || null;
  const target = opts.target || null;
  const b = L.bounds(model, level);
  const s = b.size;
  const chipH = s / 21;
  const fs = s / 34;
  const pad = Math.max(s * 0.07, chipH * 2.2);
  const box = [b.minX - pad, -(b.maxY + pad), b.maxX - b.minX + 2 * pad, b.maxY - b.minY + 2 * pad];
  const isSel = (kind, id) => !!selected && selected.kind === kind && String(selected.id) === String(id);

  const rooms = (model.rooms || []).filter(r => r.level === level && r.polygon.length >= 3);
  const walls = (model.walls || []).filter(w => w.level === level);
  const placed = model.placements || { heaters: {}, sensors: {} };
  const floorHeated = new Set(Object.values(placed.heaters || {}).filter(p => p && p.floor).map(p => p.floor));
  const onOpening = L.sensorsOnOpenings(model);

  const out = [];
  out.push(`<svg class="plan-svg${target ? ` is-placing is-target-${target}` : ''}"`
    + ` viewBox="${box.map(n).join(' ')}" role="img" aria-label="Floor plan">`);
  out.push(`<defs><pattern id="plHatch" patternUnits="userSpaceOnUse" width="${n(s / 45)}" height="${n(s / 45)}"`
    + ` patternTransform="rotate(45)"><line class="pl-hatch" x1="0" y1="0" x2="0" y2="${n(s / 45)}"`
    + ` stroke-width="${n(s / 300)}"/></pattern></defs>`);
  out.push(`<rect class="pl-bg" x="${n(box[0])}" y="${n(box[1])}" width="${n(box[2])}" height="${n(box[3])}"/>`);

  for (const room of rooms) {
    const pts = room.polygon.map(P).join(' ');
    out.push(`<polygon class="pl-room" data-room="${esc(room.id)}" points="${pts}"/>`);
    if (floorHeated.has(room.id)) {
      const heater = Object.entries(placed.heaters).find(([, p]) => p && p.floor === room.id);
      out.push(`<polygon class="pl-floorheat${heater && isSel('heaters', heater[0]) ? ' is-selected' : ''}"`
        + ` points="${pts}" fill="url(#plHatch)"/>`);
    }
  }

  for (const f of (model.fixtures || []).filter(f => f.level === level)) {
    const [x0, y0, x1, y1] = f.box;
    out.push(`<rect class="pl-fixture" x="${n(Math.min(x0, x1))}" y="${n(-Math.max(y0, y1))}"`
      + ` width="${n(Math.abs(x1 - x0))}" height="${n(Math.abs(y1 - y0))}"/>`);
  }

  for (const wall of walls) {
    const thick = Math.max(wall.thickness || 0.12, s / 160);
    out.push(`<line class="pl-wall${wall.exterior ? ' is-ext' : ''}" x1="${n(wall.a[0])}" y1="${n(-wall.a[1])}"`
      + ` x2="${n(wall.b[0])}" y2="${n(-wall.b[1])}" stroke-width="${n(thick)}"/>`);
    for (const o of wall.openings || []) {
      const p0 = L.alongWall(wall, o.at);
      const p1 = L.alongWall(wall, o.at + o.width);
      out.push(`<line class="pl-gap" x1="${n(p0[0])}" y1="${n(-p0[1])}" x2="${n(p1[0])}" y2="${n(-p1[1])}"`
        + ` stroke-width="${n(thick + s / 400)}"/>`);
      if (o.kind === 'window') {
        out.push(`<line class="pl-window" x1="${n(p0[0])}" y1="${n(-p0[1])}" x2="${n(p1[0])}" y2="${n(-p1[1])}"`
          + ` stroke-width="${n(thick * 0.45)}"/>`);
      } else if (o.kind === 'door') {
        // A quarter circle into the room, hinged at the opening's start.
        const side = insideSide(model, wall, o.at + o.width / 2) || 1;
        const swing = (o.swing === 'out' ? -1 : 1) * side;
        const [nx, ny] = wallNormal(wall);
        const tip = [p0[0] + nx * o.width * swing, p0[1] + ny * o.width * swing];
        // Drawn y-down: the short way round from the leaf's tip to the far jamb.
        const v1 = [tip[0] - p0[0], -(tip[1] - p0[1])];
        const v2 = [p1[0] - p0[0], -(p1[1] - p0[1])];
        const sweep = v1[0] * v2[1] - v1[1] * v2[0] > 0 ? 1 : 0;
        out.push(`<path class="pl-door" d="M ${P(tip)} A ${n(o.width)} ${n(o.width)} 0 0 ${sweep} ${P(p1)}`
          + ` M ${P(p0)} L ${P(tip)}" stroke-width="${n(s / 400)}"/>`);
      }
    }
  }

  // Heaters on walls: a bar just inside the wall.
  for (const [serial, p] of Object.entries(placed.heaters || {})) {
    if (!p || !p.wall) continue;
    const wall = walls.find(w => w.id === p.wall);
    if (!wall) continue;
    const device = heaters.get(String(serial));
    const side = insideSide(model, wall, p.at + p.length / 2, device && device.zone_id) || 1;
    const [nx, ny] = wallNormal(wall);
    const off = ((wall.thickness || 0.12) / 2 + s / 110) * side;
    const a = L.alongWall(wall, p.at);
    const c = L.alongWall(wall, p.at + p.length);
    out.push(`<line class="pl-heater${isSel('heaters', serial) ? ' is-selected' : ''}" data-heater="${esc(serial)}"`
      + ` x1="${n(a[0] + nx * off)}" y1="${n(-(a[1] + ny * off))}" x2="${n(c[0] + nx * off)}" y2="${n(-(c[1] + ny * off))}"`
      + ` stroke-width="${n(s / 70)}"/>`);
  }

  // Thermometers and other sensors at a point.
  for (const [id, p] of Object.entries(placed.sensors || {})) {
    if (!p || !p.point || p.level !== level) continue;
    out.push(`<circle class="pl-sensor${isSel('sensors', id) ? ' is-selected' : ''}" data-sensor="${esc(id)}"`
      + ` cx="${n(p.point[0])}" cy="${n(-p.point[1])}" r="${n(s / 80)}"/>`);
  }

  for (const room of rooms) {
    const name = L.roomName(room, zones);
    if (!name) continue;
    const [cx, cy] = L.roomCentre(room);
    const xs = room.polygon.map(p => p[0]);
    const ys = room.polygon.map(p => p[1]);
    // The box around the centre that stays inside the room's outline.
    const w = 2 * Math.min(cx - Math.min(...xs), Math.max(...xs) - cx);
    const h = 2 * Math.min(cy - Math.min(...ys), Math.max(...ys) - cy);
    const fit = fitLabel(name, w, h, fs);
    if (!fit) continue;
    const lead = fit.size * 1.2;
    const turn = fit.rotate ? ` transform="rotate(-90 ${n(cx)} ${n(-cy)})"` : '';
    const spans = fit.lines.map((line, i) => `<tspan x="${n(cx)}" dy="${n(i === 0
      ? -lead * (fit.lines.length - 1) / 2 : lead)}">${esc(line)}</tspan>`).join('');
    out.push(`<text class="pl-label" x="${n(cx)}" y="${n(-cy)}" font-size="${n(fit.size)}"${turn}>${spans}</text>`);
  }

  // Door and window codes last, so they are on top and take the tap.
  for (const wall of walls) {
    for (const o of wall.openings || []) {
      if (!o.code || o.kind === 'gap') continue;
      const mid = o.at + o.width / 2;
      const [cx, cy] = L.alongWall(wall, mid);
      const [left, right] = L.roomsBeside(model, wall, mid);
      const [nx, ny] = wallNormal(wall);
      // Outside walls carry their code outside, so it does not cover a room.
      const outward = left && !right ? -1 : right && !left ? 1 : 0;
      const at = [cx + nx * outward * chipH * 0.95, cy + ny * outward * chipH * 0.95];
      const ids = onOpening.get(o.code) || [];
      const w = Math.max(chipH * 1.5, chipH * 0.55 * (o.code.length + 0.8));
      const classes = ['pl-chip', `is-${o.kind}`, ids.length ? 'is-used' : 'is-free'];
      if (ids.some(id => isSel('sensors', id))) classes.push('is-selected');
      const label = `${o.kind === 'door' ? 'Door' : 'Window'} ${o.code}${ids.length
        ? `, ${ids.length} ${ids.length === 1 ? 'sensor' : 'sensors'}` : ', no sensor'}`;
      out.push(`<g class="${classes.join(' ')}" data-opening="${esc(o.code)}" tabindex="0" role="button"`
        + ` aria-label="${esc(label)}" transform="translate(${n(at[0])} ${n(-at[1])})">`
        + `<rect x="${n(-w / 2)}" y="${n(-chipH / 2)}" width="${n(w)}" height="${n(chipH)}" rx="${n(chipH * 0.3)}"/>`
        + `<text font-size="${n(chipH * 0.58)}" dy="0.35em">${esc(o.code)}</text></g>`);
    }
  }

  out.push('</svg>');
  return out.join('');
}

/**
 * Show the plan in ``host``. ``onTap({pt, opening})`` is called with the
 * point tapped, in plan metres, and the door or window code if a code was
 * tapped (then ``pt`` is that opening's centre).
 */
export function mountPlan(host, opts) {
  let model = opts.model;
  let level = opts.level;
  let view = { zones: opts.zones, heaters: opts.heaters, selected: null, target: null };

  function draw() {
    host.innerHTML = planSvg(model, level, view);
  }

  function toPlan(svg, clientX, clientY) {
    const m = svg.getScreenCTM();
    if (!m) return null;
    const p = new DOMPoint(clientX, clientY).matrixTransform(m.inverse());
    return [p.x, -p.y];
  }

  function chipCentre(code) {
    const found = L.openingsByCode(model).get(code);
    return found ? L.openingCentre(found.wall, found.opening) : null;
  }

  function onClick(e) {
    const svg = host.querySelector('svg');
    if (!svg) return;
    const chip = e.target.closest && e.target.closest('[data-opening]');
    // A code is only a target for a contact. Placing anything else, the
    // finger's own point counts, even if a code happens to be under it.
    if (chip && (view.target === 'opening' || !view.target)) {
      const code = chip.getAttribute('data-opening');
      opts.onTap({ pt: chipCentre(code), opening: code });
      return;
    }
    const pt = toPlan(svg, e.clientX, e.clientY);
    if (pt) opts.onTap({ pt, opening: null });
  }

  function onKey(e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    if (view.target && view.target !== 'opening') return;
    const chip = e.target.closest && e.target.closest('[data-opening]');
    if (!chip) return;
    e.preventDefault();
    const code = chip.getAttribute('data-opening');
    opts.onTap({ pt: chipCentre(code), opening: code });
  }

  host.addEventListener('click', onClick);
  host.addEventListener('keydown', onKey);
  draw();

  return {
    get level() { return level; },
    setLevel(id) { level = id; draw(); },
    setModel(next) { model = next; draw(); },
    update(next) { view = { ...view, ...next }; draw(); },
    destroy() {
      host.removeEventListener('click', onClick);
      host.removeEventListener('keydown', onKey);
      host.innerHTML = '';
    },
  };
}
