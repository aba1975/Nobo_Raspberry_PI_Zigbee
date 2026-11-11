/*
 * house_logic.js — what the house view shows, without drawing any of it.
 *
 * Kept apart from house.js, which draws, so that every rule about *what*
 * appears can be tested in node without a browser or WebGL:
 *
 *   - a room's colour is its zone's mode, never "heating" or "on": the hub
 *     does not report whether a heater is drawing power;
 *   - "Actual" appears only where something measured it (zone
 *     current_temperature), and is never a set point standing in;
 *   - a door or window is drawn open only when a sensor that counts says so;
 *   - names come from the hub and the sensor registry, not from the floor
 *     plan, except for a room with no zone.
 *
 * Plain ES module, no imports.
 */

export const CUT_HEIGHT = 1.35;

/** The mode a zone is actually running, as core.js effectiveMode does. */
export function effectiveMode(zone) {
  if (!zone) return null;
  const mode = zone.current_mode || 'normal';
  if (mode !== 'normal') return mode;
  return zone.schedule_mode || 'comfort';
}

/** What the zone is aiming for now, or null (Off, or nothing to aim). */
export function targetTemp(zone) {
  switch (effectiveMode(zone)) {
    case 'eco': return zone.eco_temperature ?? null;
    case 'away': return zone.away_temperature ?? null;
    case 'off': return null;
    case null: return null;
    default: return zone.comfort_temperature ?? null;
  }
}

export function zonesById(zones) {
  const map = new Map();
  for (const zone of zones || []) map.set(String(zone.zone_id), zone);
  return map;
}

/** Every sensor the zones report, contacts and thermometers, by id. */
export function sensorsById(zones) {
  const map = new Map();
  for (const zone of zones || []) {
    for (const sensor of [...(zone.sensors || []), ...(zone.climate_sensors || [])]) {
      map.set(String(sensor.sensor_id), sensor);
    }
  }
  return map;
}

/** Which zone a heater serial belongs to, by the zones' component lists. */
export function heaterZones(zones) {
  const map = new Map();
  for (const zone of zones || []) {
    for (const serial of zone.components || []) map.set(String(serial), zone);
  }
  return map;
}

/**
 * The colour key for a room's floor: a mode ('comfort', 'eco', 'away',
 * 'off'), 'none' for a room with no heating, or 'unknown' when the room
 * points at a zone the hub no longer has.
 */
export function roomTint(room, zones) {
  if (room.zone_id == null) return 'none';
  const zone = zones.get(String(room.zone_id));
  if (!zone) return 'unknown';
  if (zone.no_schedule === true || !(zone.components || []).length) return 'none';
  return effectiveMode(zone) || 'none';
}

/** The name shown for a room: its zone's own, else its plan label, else nothing. */
export function roomName(room, zones) {
  const zone = room.zone_id == null ? null : zones.get(String(room.zone_id));
  return (zone && zone.name) || room.name || '';
}

/**
 * The words on a room's label. ``actual`` is only ever a measurement; a room
 * with no thermometer and no measuring heater has none, and says nothing.
 */
export function roomLabel(room, zones) {
  const zone = room.zone_id == null ? null : zones.get(String(room.zone_id));
  const tint = roomTint(room, zones);
  const heated = zone && tint !== 'none' && tint !== 'unknown';
  const actual = zone && typeof zone.current_temperature === 'number' ? zone.current_temperature : null;
  return {
    name: roomName(room, zones),
    tint,
    setpoint: heated ? targetTemp(zone) : null,
    actual,
  };
}

/** True when a contact is reporting open, is reachable, and counts. */
export function contactOpen(sensor) {
  return !!sensor && sensor.available !== false && sensor.counts !== false && sensor.state === 'open';
}

/**
 * What each door and window is doing, by its opening code: which sashes are
 * open, and the sensors that said so. Only placements on an opening count.
 */
export function openingStates(model, sensors) {
  const result = new Map();
  const placements = (model.placements && model.placements.sensors) || {};
  for (const [sensorId, placement] of Object.entries(placements)) {
    if (!placement || !placement.opening) continue;
    const sensor = sensors.get(String(sensorId));
    const entry = result.get(placement.opening) || { open: new Set(), sensors: [] };
    entry.sensors.push(sensorId);
    if (contactOpen(sensor)) entry.open.add(placement.sash || 'whole');
    result.set(placement.opening, entry);
  }
  return result;
}

/** Every opening with a code, with where it sits on its wall. */
export function openingsByCode(model) {
  const map = new Map();
  for (const wall of model.walls || []) {
    for (const opening of wall.openings || []) {
      if (opening.code) map.set(opening.code, { wall, opening });
    }
  }
  return map;
}

/** A point ``t`` metres along a wall from its start. */
export function alongWall(wall, t) {
  const [ax, ay] = wall.a;
  const [bx, by] = wall.b;
  const len = Math.hypot(bx - ax, by - ay) || 1;
  return [ax + ((bx - ax) * t) / len, ay + ((by - ay) * t) / len];
}

/** The plan's extent on one level, or of the whole plan when ``level`` is omitted. */
export function bounds(model, level) {
  let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
  const take = ([x, y]) => {
    minX = Math.min(minX, x); minY = Math.min(minY, y);
    maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
  };
  for (const wall of model.walls || []) {
    if (level && wall.level !== level) continue;
    take(wall.a); take(wall.b);
  }
  for (const room of model.rooms || []) {
    if (level && room.level !== level) continue;
    room.polygon.forEach(take);
  }
  if (!Number.isFinite(minX)) return { minX: 0, minY: 0, maxX: 1, maxY: 1, cx: 0.5, cy: 0.5, size: 1 };
  return {
    minX, minY, maxX, maxY,
    cx: (minX + maxX) / 2, cy: (minY + maxY) / 2,
    size: Math.max(maxX - minX, maxY - minY, 1),
  };
}

/** A room's label point: the centroid of its outline, which is inside for the
 *  rectangles and L-shapes a building is made of. */
export function roomCentre(room) {
  const pts = room.polygon;
  let area = 0; let x = 0; let y = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x0, y0] = pts[i];
    const [x1, y1] = pts[(i + 1) % pts.length];
    const cross = x0 * y1 - x1 * y0;
    area += cross; x += (x0 + x1) * cross; y += (y0 + y1) * cross;
  }
  if (Math.abs(area) < 1e-9) return pts[0];
  return [x / (3 * area), y / (3 * area)];
}

/** Levels that have something on them, lowest first. */
export function levelsInUse(model) {
  const used = new Set([...(model.walls || []).map(w => w.level), ...(model.rooms || []).map(r => r.level)]);
  return (model.levels || []).filter(level => used.has(level.id))
    .slice().sort((a, b) => a.elevation - b.elevation);
}

/** Which level a placement is drawn on, or null if it is not drawn. */
export function placementLevel(model, placement) {
  if (!placement || placement.outside) return null;
  if (placement.level) return placement.level;
  if (placement.wall) {
    const wall = (model.walls || []).find(w => w.id === placement.wall);
    return wall ? wall.level : null;
  }
  if (placement.floor) {
    const room = (model.rooms || []).find(r => r.id === placement.floor);
    return room ? room.level : null;
  }
  if (placement.opening) {
    const found = openingsByCode(model).get(placement.opening);
    return found ? found.wall.level : null;
  }
  return null;
}

/** "Open · 4 min", "Open · 2 h" — how long a contact has been open. */
export function openFor(sensor, now = Date.now()) {
  const since = sensor && sensor.changed_at ? Date.parse(sensor.changed_at) : NaN;
  if (!Number.isFinite(since)) return 'Open';
  const minutes = Math.max(0, Math.round((now - since) / 60000));
  if (minutes < 1) return 'Open · now';
  if (minutes < 60) return `Open · ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `Open · ${hours} h`;
  return `Open · ${Math.round(hours / 24)} d`;
}

/** Whole degrees for a set point, one decimal for a measurement. */
export function fmtSet(value) {
  return value == null ? '' : `${Math.round(value)}°`;
}

export function fmtActual(value) {
  return value == null ? '' : `${value.toFixed(1)}°`;
}

/* ------------------------------------------------------------------
 * Placing (stage 3). Where a tap on the plan lands, and how a place is
 * said in words. Nothing here decides how anything heats or warns: a
 * sensor's room is always its own setting, never the room it is drawn in.
 * ---------------------------------------------------------------- */

export function pointInPolygon([x, y], polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function polygonArea(polygon) {
  let area = 0;
  for (let i = 0; i < polygon.length; i++) {
    const [x0, y0] = polygon[i];
    const [x1, y1] = polygon[(i + 1) % polygon.length];
    area += x0 * y1 - x1 * y0;
  }
  return Math.abs(area) / 2;
}

/** The room on ``level`` that contains ``pt``; the smallest, if rooms overlap. */
export function roomAt(model, level, pt) {
  const found = (model.rooms || []).filter(r => r.level === level && r.polygon.length >= 3
    && pointInPolygon(pt, r.polygon));
  found.sort((a, b) => polygonArea(a.polygon) - polygonArea(b.polygon));
  return found[0] || null;
}

/** Where ``pt`` falls along a wall: ``t`` metres from its start (clamped), and how far off it is. */
export function projectOnWall(wall, [px, py]) {
  const [ax, ay] = wall.a;
  const [bx, by] = wall.b;
  const len = Math.hypot(bx - ax, by - ay) || 1;
  const ux = (bx - ax) / len; const uy = (by - ay) / len;
  const t = Math.max(0, Math.min(len, (px - ax) * ux + (py - ay) * uy));
  const dist = Math.hypot(ax + ux * t - px, ay + uy * t - py);
  return { t, dist, len };
}

/** The wall on ``level`` nearest ``pt``, or null if there is none. */
export function nearestWall(model, level, pt) {
  let best = null;
  for (const wall of model.walls || []) {
    if (wall.level !== level) continue;
    const p = projectOnWall(wall, pt);
    if (!best || p.dist < best.dist) best = { wall, t: p.t, dist: p.dist };
  }
  return best;
}

export function openingCentre(wall, opening) {
  return alongWall(wall, opening.at + opening.width / 2);
}

/**
 * The door or window on ``level`` nearest ``pt`` within ``maxDist`` metres,
 * or null. Open doorways (``gap``) and openings without a code cannot carry
 * a sensor, so they are never offered.
 */
export function nearestOpening(model, level, pt, maxDist = 1.5) {
  let best = null;
  for (const wall of model.walls || []) {
    if (wall.level !== level) continue;
    for (const opening of wall.openings || []) {
      if (!opening.code || opening.kind === 'gap') continue;
      const [cx, cy] = openingCentre(wall, opening);
      const dist = Math.hypot(cx - pt[0], cy - pt[1]);
      if (dist <= maxDist && (!best || dist < best.dist)) best = { wall, opening, dist };
    }
  }
  return best;
}

/**
 * The rooms either side of a wall, ``t`` metres along it: [left, right] as
 * seen walking from its start to its end. Either may be null (outside).
 */
export function roomsBeside(model, wall, t, offset = 0.3) {
  const [ax, ay] = wall.a;
  const [bx, by] = wall.b;
  const len = Math.hypot(bx - ax, by - ay) || 1;
  const nx = -(by - ay) / len; const ny = (bx - ax) / len;
  const [px, py] = alongWall(wall, t);
  return [
    roomAt(model, wall.level, [px + nx * offset, py + ny * offset]),
    roomAt(model, wall.level, [px - nx * offset, py - ny * offset]),
  ];
}

/** "north", "east", "south" or "west": the plan's y axis is north. */
export function compass([dx, dy]) {
  if (Math.abs(dx) > Math.abs(dy)) return dx > 0 ? 'east' : 'west';
  return dy > 0 ? 'north' : 'south';
}

/**
 * A wall in words, from inside ``room``: "north wall" for an outside wall,
 * "wall to Kitchen" for one between two rooms.
 */
export function wallWords(model, wall, t, zones, room = null) {
  const [left, right] = roomsBeside(model, wall, t);
  const [ax, ay] = wall.a;
  const [bx, by] = wall.b;
  const len = Math.hypot(bx - ax, by - ay) || 1;
  const n = [-(by - ay) / len, (bx - ax) / len];
  const here = room || left || right;
  const other = here && left && here.id === left.id ? right : left;
  if (here && other && other.id !== here.id) {
    const name = roomName(other, zones);
    if (name) return `wall to ${name}`;
  }
  // Facing out of the room we are in.
  const out = here && left && here.id === left.id ? [-n[0], -n[1]] : n;
  return `${compass(out)} wall`;
}

/** A heater ``length`` long centred ``t`` metres along a wall, kept on it. */
export function heaterSpan(wall, t, length) {
  const wallLength = Math.hypot(wall.b[0] - wall.a[0], wall.b[1] - wall.a[1]);
  const len = Math.max(0.1, Math.min(length, Math.floor(wallLength * 100) / 100));
  const at = Math.max(0, Math.min(wallLength - len, t - len / 2));
  return { at: Math.round(at * 100) / 100, length: len };
}

/** What a device is placed on: ``opening`` (contacts), ``point`` (thermometers) or ``heater``. */
export function targetFor(kind, device) {
  if (kind === 'heaters') return 'heater';
  return device && device.kind === 'climate' ? 'point' : 'opening';
}

/** An NTB-2R heats a floor; everything else hangs on a wall. */
export function isFloorHeater(device) {
  return !!device && /^NTB/i.test(String(device.device_type || ''));
}

/** Heights a thermometer is usually put at, from the level's floor. */
export function pointHeights(levelHeight) {
  return [
    { id: 'wall', label: 'On a wall', z: 1.5 },
    { id: 'door', label: 'Above a door', z: 2.1 },
    { id: 'ceiling', label: 'Ceiling', z: Math.max(0.1, Math.round((levelHeight - 0.05) * 100) / 100) },
  ];
}

/**
 * A placement in a few words, for lists: "Under W4", "Window W7, left sash",
 * "Floor heating". ``ownRoom`` is the name of the room the device belongs to:
 * a place in that room does not say its name again.
 */
export function placementWords(model, placement, zones, ownRoom = null) {
  if (!placement) return 'Not placed yet';
  if (placement.outside) return 'Not in this building';
  const named = (room) => {
    const name = room ? roomName(room, zones) : '';
    return name && name !== ownRoom ? name : '';
  };
  const cap = (text) => text.charAt(0).toUpperCase() + text.slice(1);
  if (placement.floor) {
    const name = named((model.rooms || []).find(r => r.id === placement.floor));
    return name ? `Floor heating, ${name}` : 'Floor heating';
  }
  if (placement.opening) {
    const found = openingsByCode(model).get(placement.opening);
    const kind = found && found.opening.kind === 'door' ? 'Door' : 'Window';
    return placement.sash && placement.sash !== 'whole'
      ? `${kind} ${placement.opening}, ${placement.sash} sash` : `${kind} ${placement.opening}`;
  }
  if (placement.point) {
    const name = named(roomAt(model, placement.level, placement.point));
    const height = `${placement.point[2].toFixed(1)} m up`;
    return name ? `${name}, ${height}` : cap(height);
  }
  if (placement.wall) {
    const wall = (model.walls || []).find(w => w.id === placement.wall);
    if (!wall) return 'On a wall';
    const under = (wall.openings || []).find(o => o.code && o.kind === 'window'
      && o.at < placement.at + placement.length && placement.at < o.at + o.width);
    if (under) return `Under ${under.code}`;
    const mid = placement.at + placement.length / 2;
    const sides = roomsBeside(model, wall, mid);
    const room = sides.find(r => r && roomName(r, zones) === ownRoom) || sides.find(Boolean);
    const words = wallWords(model, wall, mid, zones, room);
    const name = named(room);
    return name ? `${name}, ${words}` : cap(words);
  }
  return 'Placed';
}

/** Every sensor id placed on each opening code. */
export function sensorsOnOpenings(model) {
  const map = new Map();
  const placements = (model.placements && model.placements.sensors) || {};
  for (const [id, placement] of Object.entries(placements)) {
    if (!placement || !placement.opening) continue;
    if (!map.has(placement.opening)) map.set(placement.opening, []);
    map.get(placement.opening).push(id);
  }
  return map;
}

/**
 * Horizontal stripes across a room's outline, every ``spacing`` metres and
 * kept ``inset`` from its edges: how floor heating is drawn. Each stripe is
 * [x0, x1, y]. Scanline, so an L-shaped hallway is striped and the corner it
 * wraps round is not.
 */
export function floorStripes(polygon, spacing = 0.28, inset = 0.1) {
  const ys = polygon.map(p => p[1]);
  const top = Math.max(...ys) - inset;
  const stripes = [];
  for (let y = Math.min(...ys) + spacing / 2 + inset / 2; y < top; y += spacing) {
    const xs = [];
    for (let i = 0; i < polygon.length; i++) {
      const [x0, y0] = polygon[i];
      const [x1, y1] = polygon[(i + 1) % polygon.length];
      if ((y0 <= y && y1 > y) || (y1 <= y && y0 > y)) xs.push(x0 + ((y - y0) * (x1 - x0)) / (y1 - y0));
    }
    xs.sort((a, b) => a - b);
    for (let i = 0; i + 1 < xs.length; i += 2) {
      if (xs[i + 1] - xs[i] > 2 * inset) stripes.push([xs[i] + inset, xs[i + 1] - inset, y]);
    }
  }
  return stripes;
}
