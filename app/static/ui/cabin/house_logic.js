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
