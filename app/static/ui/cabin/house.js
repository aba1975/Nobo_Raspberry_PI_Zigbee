/*
 * house.js — the Dollhouse: one floor of the building, roof off and walls cut
 * low, with each room tinted by its mode and every heater and sensor where it
 * really is. docs/HOUSE.md is the design.
 *
 * Loaded with import() only by someone who turned the house view on, so
 * three.js costs nobody else anything. Everything about *what* is shown lives
 * in house_logic.js and is tested there; this file only draws it.
 *
 * Live changes recolour and swing doors in place. The scene is rebuilt only
 * when the floor plan itself changes, never on a zone update: rebuilding
 * under somebody's finger mid-orbit is the week-editor bug all over again.
 */

import * as THREE from 'three';
import { OrbitControls } from '/static/ui/shared/vendor/three/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from '/static/ui/shared/vendor/three/CSS2DRenderer.js';
import * as L from '/static/ui/cabin/house_logic.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ICON = {
  window: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 2.5h11v11h-11zM8 2.5v11M2.5 8h11" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
  door: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.5h8v13H4z" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="10" cy="8.5" r="1" fill="currentColor"/></svg>',
};

/** Whether this browser can draw the house at all. */
export function canRender() {
  try {
    const canvas = document.createElement('canvas');
    return !!(window.WebGLRenderingContext
      && (canvas.getContext('webgl2') || canvas.getContext('webgl')));
  } catch (_) {
    return false;
  }
}

function palette() {
  const css = getComputedStyle(document.body);
  const v = (name, fallback) => (css.getPropertyValue(name) || '').trim() || fallback;
  const col = (name, fallback) => new THREE.Color(v(name, fallback));
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  return {
    dark,
    wall: col('--card', '#FFFDF9'),
    edge: col('--ink-faint', '#948C80'),
    paper: col('--paper', '#F6F2EA'),
    rule: col('--rule', '#E3DCCF'),
    glass: col('--frost', '#4A7C9B'),
    open: col('--danger', '#C9453C'),
    pine: col('--pine', '#2F5D50'),
    off: col('--off', '#B3ADA2'),
    mode: {
      comfort: col('--m-comfort', '#C0413A'),
      eco: col('--m-eco', '#2E8B57'),
      away: col('--m-away', '#2C6FB8'),
      off: col('--off', '#B3ADA2'),
    },
    wash: {
      comfort: col('--m-comfort-wash', '#FAE8E6'),
      eco: col('--m-eco-wash', '#E4F2E9'),
      away: col('--m-away-wash', '#E4EEF8'),
      off: col('--paper', '#F6F2EA'),
      none: col('--paper', '#F6F2EA'),
      unknown: col('--rule', '#E3DCCF'),
    },
  };
}

function pointInPolygon([x, y], polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/**
 * Draw the house into ``host`` and return a controller.
 *
 * opts.model        the floor plan from GET /api/house
 * opts.zones        the zone list, as /api/zones and the socket deliver it
 * opts.level        the level id to start on (default: the lowest)
 * opts.onSelect     called with {room, zone} when a room is tapped, or null
 * opts.onError      called with a message if drawing becomes impossible
 * opts.active       false to start paused (another screen is showing)
 */
export function mountHouse(host, opts) {
  let model = opts.model;
  let zones = L.zonesById(opts.zones);
  let sensors = L.sensorsById(opts.zones);
  let heaterZone = L.heaterZones(opts.zones);
  let pal = palette();
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let levels = L.levelsInUse(model);
  let level = (levels.find(l => l.id === opts.level) || levels[0] || { id: null }).id;
  let selectedRoom = null;
  let active = opts.active !== false;
  let dirty = true;
  let lastCollision = 0;
  let needsFrame = false;

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.localClippingEnabled = true;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.domElement.className = 'house-canvas';
  host.appendChild(renderer.domElement);
  const labels = new CSS2DRenderer();
  labels.domElement.className = 'house-labels';
  host.appendChild(labels.domElement);

  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 400);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.maxPolarAngle = Math.PI * 0.47;
  controls.screenSpacePanning = false;
  controls.addEventListener('change', () => { dirty = true; });

  let whole = L.bounds(model);
  const V = (x, y, z = 0) => new THREE.Vector3(x - whole.cx, z, -(y - whole.cy));

  let scene = null;
  let parts = null;   // what update() recolours, rebuilt with the scene

  function disposeScene() {
    if (!scene) return;
    scene.traverse(obj => {
      if (obj.geometry) obj.geometry.dispose();
      const mats = Array.isArray(obj.material) ? obj.material : obj.material ? [obj.material] : [];
      mats.forEach(m => { if (m.map) m.map.dispose(); m.dispose(); });
      if (obj.isCSS2DObject && obj.element) obj.element.remove();
    });
    scene = null;
  }

  function flatShape(polygon) {
    const shape = new THREE.Shape(polygon.map(([x, y]) => new THREE.Vector2(x - whole.cx, y - whole.cy)));
    const g = new THREE.ShapeGeometry(shape);
    g.rotateX(-Math.PI / 2);
    return g;
  }

  function insideNormal(wall, roomsHere) {
    const [ax, ay] = wall.a; const [bx, by] = wall.b;
    const len = Math.hypot(bx - ax, by - ay) || 1;
    const n = [-(by - ay) / len, (bx - ax) / len];
    const mid = [(ax + bx) / 2 + n[0] * 0.4, (ay + by) / 2 + n[1] * 0.4];
    const inside = roomsHere.some(room => pointInPolygon(mid, room.polygon));
    return inside ? n : [-n[0], -n[1]];
  }

  function build() {
    disposeScene();
    pal = palette();
    scene = new THREE.Scene();
    parts = { rooms: new Map(), heaters: [], leaves: new Map(), pins: [], select: null };
    scene.add(new THREE.AmbientLight(0xffffff, pal.dark ? 1.7 : 1.9));
    const sun = new THREE.DirectionalLight(0xffffff, pal.dark ? 1.4 : 1.6);
    sun.position.set(9, 18, 10);
    scene.add(sun);

    const clip = [new THREE.Plane(new THREE.Vector3(0, -1, 0), L.CUT_HEIGHT)];
    const mats = {
      wall: new THREE.MeshLambertMaterial({ color: pal.wall, side: THREE.DoubleSide, clippingPlanes: clip }),
      edge: new THREE.LineBasicMaterial({ color: pal.edge, transparent: true, opacity: 0.45, clippingPlanes: clip }),
      glass: new THREE.MeshBasicMaterial({ color: pal.glass, transparent: true, opacity: 0.35, side: THREE.DoubleSide, depthWrite: false, clippingPlanes: clip }),
      leaf: new THREE.MeshLambertMaterial({ color: pal.rule, side: THREE.DoubleSide, clippingPlanes: clip }),
      fixture: new THREE.MeshLambertMaterial({ color: pal.rule, clippingPlanes: clip }),
    };

    if (!pal.dark) {
      const c = document.createElement('canvas'); c.width = c.height = 128;
      const x = c.getContext('2d');
      const gr = x.createRadialGradient(64, 64, 10, 64, 64, 64);
      gr.addColorStop(0, 'rgba(60,45,25,.24)'); gr.addColorStop(1, 'rgba(60,45,25,0)');
      x.fillStyle = gr; x.fillRect(0, 0, 128, 128);
      const shadow = new THREE.Mesh(new THREE.PlaneGeometry(whole.size * 2.1, whole.size * 2.1),
        new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false }));
      shadow.rotation.x = -Math.PI / 2; shadow.position.y = -0.05;
      scene.add(shadow);
    }

    const roomsHere = model.rooms.filter(r => r.level === level);
    const wallsHere = model.walls.filter(w => w.level === level);
    const placedSensors = (model.placements && model.placements.sensors) || {};

    // Which sashes need to be drawn separately: those a sensor watches.
    const sashed = new Map();
    for (const placement of Object.values(placedSensors)) {
      if (placement && placement.opening && placement.sash && placement.sash !== 'whole') {
        sashed.set(placement.opening, true);
      }
    }

    for (const room of roomsHere) {
      const mat = new THREE.MeshLambertMaterial({ color: pal.wash.none, side: THREE.DoubleSide });
      const floor = new THREE.Mesh(flatShape(room.polygon), mat);
      floor.position.y = 0.01;
      floor.userData = { roomId: room.id };
      scene.add(floor);
      const el = document.createElement('button');
      el.type = 'button';
      el.className = 'house-label';
      el.onclick = (e) => { e.stopPropagation(); choose(room.id); };
      const label = new CSS2DObject(el);
      const [cx, cy] = L.roomCentre(room);
      label.position.copy(V(cx, cy, 0.3));
      scene.add(label);
      parts.rooms.set(room.id, { room, floor, mat, label, el });
    }

    for (const wall of wallsHere) {
      const [ax, ay] = wall.a; const [bx, by] = wall.b;
      const len = Math.hypot(bx - ax, by - ay);
      const t = wall.thickness || 0.1;
      const top = Math.max(L.CUT_HEIGHT + 0.5, 2.4);
      const shape = new THREE.Shape();
      shape.moveTo(0, 0);
      const doors = (wall.openings || []).filter(o => o.sill <= 0.001).sort((p, q) => p.at - q.at);
      for (const o of doors) {
        shape.lineTo(o.at, 0); shape.lineTo(o.at, o.height);
        shape.lineTo(o.at + o.width, o.height); shape.lineTo(o.at + o.width, 0);
      }
      shape.lineTo(len, 0); shape.lineTo(len, top); shape.lineTo(0, top); shape.closePath();
      for (const o of (wall.openings || []).filter(o => o.sill > 0.001)) {
        const hole = new THREE.Path();
        hole.moveTo(o.at, o.sill); hole.lineTo(o.at + o.width, o.sill);
        hole.lineTo(o.at + o.width, o.sill + o.height); hole.lineTo(o.at, o.sill + o.height);
        hole.closePath();
        shape.holes.push(hole);
      }
      const geo = new THREE.ExtrudeGeometry(shape, { depth: t, bevelEnabled: false });
      geo.translate(0, 0, -t / 2);
      const holder = new THREE.Object3D();
      holder.position.copy(V(ax, ay));
      holder.rotation.y = Math.atan2(by - ay, bx - ax);
      holder.add(new THREE.Mesh(geo, mats.wall));
      holder.add(new THREE.LineSegments(new THREE.EdgesGeometry(geo, 20), mats.edge));

      // Local +z on a wall is the right-hand side of a→b; outside is the
      // side away from the rooms on this level.
      const nIn = insideNormal(wall, roomsHere);
      const right = [(by - ay) / (len || 1), -(bx - ax) / (len || 1)];
      const insideIsPlusZ = nIn[0] * right[0] + nIn[1] * right[1] > 0;
      // Left as seen from inside, looking out.
      const out = [-nIn[0], -nIn[1]];
      const leftDir = [-out[1], out[0]];
      const leftIsHigherU = ((bx - ax) * leftDir[0] + (by - ay) * leftDir[1]) > 0;

      for (const o of wall.openings || []) {
        if (o.kind === 'gap' || !o.code) continue;
        const isDoor = o.kind === 'door';
        const split = !isDoor && sashed.has(o.code);
        const halves = split
          ? [{ sash: leftIsHigherU ? 'right' : 'left', u0: o.at, u1: o.at + o.width / 2, hingeLow: true },
             { sash: leftIsHigherU ? 'left' : 'right', u0: o.at + o.width / 2, u1: o.at + o.width, hingeLow: false }]
          : [{ sash: 'whole', u0: o.at, u1: o.at + o.width, hingeLow: true }];
        const swingOut = isDoor ? o.swing === 'out' : true;   // Nordic windows open outwards
        const towardPlusZ = swingOut ? !insideIsPlusZ : insideIsPlusZ;
        const leaves = [];
        for (const half of halves) {
          const w = half.u1 - half.u0;
          const pivot = new THREE.Object3D();
          pivot.position.set(half.hingeLow ? half.u0 : half.u1, o.sill, towardPlusZ ? t / 2 : -t / 2);
          const leafGeo = isDoor
            ? new THREE.BoxGeometry(w, o.height, 0.04)
            : new THREE.PlaneGeometry(w, o.height);
          leafGeo.translate(half.hingeLow ? w / 2 : -w / 2, o.height / 2, 0);
          const leafMat = (isDoor ? mats.leaf : mats.glass).clone();
          const mesh = new THREE.Mesh(leafGeo, leafMat);
          pivot.add(mesh);
          holder.add(pivot);
          // Rotating about y moves +x towards -z, so the sign follows the side.
          const sign = (half.hingeLow ? 1 : -1) * (towardPlusZ ? -1 : 1);
          leaves.push({ sash: half.sash, pivot, mat: leafMat, base: leafMat.color.clone(),
            baseOpacity: leafMat.opacity, angle: 0, target: 0, openAngle: sign * (isDoor ? 1.15 : 0.6) });
        }
        parts.leaves.set(o.code, { leaves, isDoor });
      }
      scene.add(holder);
    }

    for (const fixture of (model.fixtures || []).filter(f => f.level === level)) {
      const [x0, y0, x1, y1] = fixture.box;
      const g = new THREE.BoxGeometry(x1 - x0, fixture.height, y1 - y0);
      const m = new THREE.Mesh(g, mats.fixture);
      m.position.copy(V((x0 + x1) / 2, (y0 + y1) / 2, fixture.height / 2));
      scene.add(m);
    }

    const placedHeaters = (model.placements && model.placements.heaters) || {};
    for (const [serial, placement] of Object.entries(placedHeaters)) {
      if (L.placementLevel(model, placement) !== level) continue;
      let mesh = null;
      if (placement.floor) {
        const room = roomsHere.find(r => r.id === placement.floor);
        if (!room) continue;
        const pts = [];
        for (const [x0, x1, y] of L.floorStripes(room.polygon, 0.28, 0.1)) {
          pts.push(V(x0, y, 0.03), V(x1, y, 0.03));
        }
        const mat = new THREE.LineBasicMaterial({ color: pal.off, transparent: true, opacity: 0.75 });
        mesh = new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(pts), mat);
        parts.heaters.push({ serial, mat, kind: 'floor' });
      } else if (placement.wall) {
        const wall = wallsHere.find(w => w.id === placement.wall);
        if (!wall) continue;
        const [ax, ay] = wall.a; const [bx, by] = wall.b;
        const nIn = insideNormal(wall, roomsHere);
        const off = (wall.thickness || 0.1) / 2 + 0.06;
        const [sx, sy] = L.alongWall(wall, placement.at);
        const [ex, ey] = L.alongWall(wall, placement.at + placement.length);
        const mat = new THREE.MeshLambertMaterial({ color: pal.off });
        mesh = new THREE.Mesh(new THREE.BoxGeometry(placement.length, 0.42, 0.08), mat);
        mesh.position.copy(V((sx + ex) / 2 + nIn[0] * off, (sy + ey) / 2 + nIn[1] * off, 0.38));
        mesh.rotation.y = Math.atan2(by - ay, bx - ax);
        parts.heaters.push({ serial, mat, kind: 'wall' });
      } else if (placement.point) {
        const [x, y] = placement.point;
        const mat = new THREE.MeshLambertMaterial({ color: pal.off });
        mesh = new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.16, 0.04), mat);
        mesh.position.copy(V(x, y, Math.min(placement.point[2], L.CUT_HEIGHT - 0.1)));
        parts.heaters.push({ serial, mat, kind: 'point' });
      }
      if (mesh) scene.add(mesh);
    }

    const codes = L.openingsByCode(model);
    for (const [sensorId, placement] of Object.entries(placedSensors)) {
      if (L.placementLevel(model, placement) !== level) continue;
      let pos;
      let kind = 'point';
      if (placement.opening) {
        const found = codes.get(placement.opening);
        if (!found) continue;
        const { wall, opening } = found;
        const nIn = insideNormal(wall, roomsHere);
        let u = opening.at + opening.width / 2;
        if (placement.sash === 'left' || placement.sash === 'right') {
          // Which half is "left" was decided when the leaves were built.
          const low = ((parts.leaves.get(opening.code) || { leaves: [] }).leaves)
            .find(l => Math.abs(l.pivot.position.x - opening.at) < 1e-6);
          const lowIsLeft = low ? low.sash === 'left' : true;
          const quarter = opening.width / 4;
          u = opening.at + ((placement.sash === 'left') === lowIsLeft ? quarter : 3 * quarter);
        }
        const [x, y] = L.alongWall(wall, u);
        const top = Math.min(opening.sill + opening.height, L.CUT_HEIGHT);
        pos = V(x + nIn[0] * 0.12, y + nIn[1] * 0.12, top + 0.05);
        kind = opening.kind === 'door' ? 'door' : 'window';
      } else if (placement.point) {
        const [x, y, z] = placement.point;
        pos = V(x, y, Math.min(z, L.CUT_HEIGHT + 0.2));
      } else {
        continue;
      }
      const dot = new THREE.Mesh(new THREE.SphereGeometry(0.07, 14, 10),
        new THREE.MeshBasicMaterial({ color: pal.pine }));
      dot.position.copy(pos);
      scene.add(dot);
      const el = document.createElement('button');
      el.type = 'button';
      el.className = 'house-pin';
      el.hidden = true;
      el.innerHTML = `${ICON[kind] || ''}<span></span>`;
      const pin = new CSS2DObject(el);
      pin.position.copy(pos).add(new THREE.Vector3(0, 0.35, 0));
      scene.add(pin);
      parts.pins.push({ sensorId, dot, el, kind });
    }

    frame();
    recolour();
    if (selectedRoom) highlight();
    dirty = true;
  }

  function frame() {
    const rect = host.getBoundingClientRect();
    // Hidden (another screen is showing): frame when it is next given a size.
    if (!rect.width || !rect.height) { needsFrame = true; return; }
    needsFrame = false;
    const w = rect.width; const h = rect.height;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    controls.target.set(0, 0.3, 0);
    // Steep enough to see into rooms, and on a tall screen from the end of
    // the building, so its long side runs up the screen instead of across.
    const wide = (whole.maxX - whole.minX) >= (whole.maxY - whole.minY);
    const portrait = camera.aspect < 0.9;
    const dir = (wide === portrait
      ? new THREE.Vector3(1, 1.75, 0.25)
      : new THREE.Vector3(0.25, 1.75, 1)).normalize();
    // The nearest distance at which every corner of the cut building, and
    // the room labels standing above it, are on screen with a margin.
    const corners = [];
    for (const x of [whole.minX, whole.maxX]) {
      for (const y of [whole.minY, whole.maxY]) {
        for (const z of [0, L.CUT_HEIGHT + 0.6]) corners.push(V(x, y, z));
      }
    }
    const fits = (distance) => {
      camera.position.copy(dir).multiplyScalar(distance).add(controls.target);
      camera.lookAt(controls.target);
      camera.updateMatrixWorld();
      return corners.every(c => {
        const p = c.clone().project(camera);
        return Math.abs(p.x) <= 0.9 && Math.abs(p.y) <= 0.8;
      });
    };
    let near = whole.size * 0.3; let far = whole.size * 8;
    for (let i = 0; i < 24; i++) {
      const mid = (near + far) / 2;
      if (fits(mid)) far = mid; else near = mid;
    }
    fits(far);
    controls.minDistance = far * 0.35;
    controls.maxDistance = far * 2.5;
    controls.update();
  }

  function sensorRoomId(sensorId) {
    const sensor = sensors.get(String(sensorId));
    if (!sensor || sensor.zone_id == null) return null;
    const room = model.rooms.find(r => r.level === level && String(r.zone_id) === String(sensor.zone_id));
    return room ? room.id : null;
  }

  function recolour() {
    if (!parts) return;
    for (const { room, mat, el } of parts.rooms.values()) {
      const label = L.roomLabel(room, zones);
      mat.color.copy(pal.wash[label.tint] || pal.wash.none);
      const dot = pal.mode[label.tint];
      const parts2 = [];
      if (label.setpoint != null) parts2.push(L.fmtSet(label.setpoint));
      if (label.actual != null) parts2.push(`Actual ${L.fmtActual(label.actual)}`);
      el.innerHTML = `<strong>${esc(label.name || '')}</strong>${parts2.length || dot
        ? `<small>${dot ? `<i class="house-dot" style="background:#${dot.getHexString()}"></i>` : ''}${esc(parts2.join(' · '))}</small>` : ''}`;
      el.setAttribute('aria-label', [label.name, label.setpoint != null ? `set to ${L.fmtSet(label.setpoint)}` : '',
        label.actual != null ? `actual ${L.fmtActual(label.actual)}` : ''].filter(Boolean).join(', '));
      el.hidden = !label.name;
    }
    for (const heater of parts.heaters) {
      const zone = heaterZone.get(String(heater.serial));
      const mode = zone && zone.no_schedule !== true ? L.effectiveMode(zone) : null;
      heater.mat.color.copy(pal.mode[mode] || pal.off);
    }
    const states = L.openingStates(model, sensors);
    for (const [code, entry] of parts.leaves) {
      const state = states.get(code);
      for (const leaf of entry.leaves) {
        const open = !!state && (state.open.has(leaf.sash) || state.open.has('whole')
          || (leaf.sash === 'whole' && state.open.size > 0));
        leaf.target = open ? leaf.openAngle : 0;
        leaf.mat.color.copy(open ? pal.open : leaf.base);
        leaf.mat.opacity = open ? 0.75 : leaf.baseOpacity;
        if (reduceMotion) { leaf.angle = leaf.target; leaf.pivot.rotation.y = leaf.angle; }
      }
    }
    for (const pin of parts.pins) {
      const sensor = sensors.get(String(pin.sensorId));
      const open = (pin.kind === 'door' || pin.kind === 'window') && L.contactOpen(sensor);
      pin.dot.material.color.copy(open ? pal.open : pal.pine);
      pin.el.hidden = !open;
      pin.el.classList.toggle('is-open', open);
      if (open) {
        pin.el.querySelector('span').textContent = L.openFor(sensor);
        pin.el.setAttribute('aria-label', `${sensor.name || 'Sensor'}: ${L.openFor(sensor)}`);
        pin.el.onclick = (e) => { e.stopPropagation(); const id = sensorRoomId(pin.sensorId); if (id) choose(id); };
      }
    }
    dirty = true;
  }

  function highlight() {
    if (parts && parts.select) {
      scene.remove(parts.select);
      parts.select.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
      parts.select = null;
    }
    const entry = selectedRoom && parts && parts.rooms.get(selectedRoom);
    if (!entry) return;
    const pts = entry.room.polygon.map(([x, y]) => V(x, y, 0.04));
    pts.push(pts[0]);
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: pal.pine }));
    const fill = new THREE.Mesh(flatShape(entry.room.polygon),
      new THREE.MeshBasicMaterial({ color: pal.pine, transparent: true, opacity: 0.14, depthWrite: false }));
    fill.position.y = 0.035;
    line.add(fill);
    scene.add(line);
    parts.select = line;
    dirty = true;
  }

  function choose(roomId) {
    selectedRoom = roomId;
    highlight();
    const entry = roomId && parts.rooms.get(roomId);
    if (opts.onSelect) {
      opts.onSelect(entry ? { room: entry.room, zone: entry.room.zone_id == null ? null : zones.get(String(entry.room.zone_id)) || null } : null);
    }
  }

  // A tap, not a drag, picks the room under the finger.
  let down = null;
  renderer.domElement.addEventListener('pointerdown', (e) => { down = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener('pointerup', (e) => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 6) return;
    const rect = renderer.domElement.getBoundingClientRect();
    const ray = new THREE.Raycaster();
    ray.setFromCamera(new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1,
      -((e.clientY - rect.top) / rect.height) * 2 + 1), camera);
    const floors = [...parts.rooms.values()].map(r => r.floor);
    const hit = ray.intersectObjects(floors, false)[0];
    choose(hit ? hit.object.userData.roomId : null);
  });

  renderer.domElement.addEventListener('webglcontextlost', (e) => {
    e.preventDefault();
    if (opts.onError) opts.onError('The house view stopped drawing. Showing the rooms as a list.');
  });

  /* Labels that would sit on top of each other. Open doors and windows always
     show; rooms follow, the biggest first. A room that would collide is first
     shortened to its name, and only hidden if even that collides. A hidden
     room is still one tap away. */
  function separateLabels() {
    const now = performance.now();
    if (now - lastCollision < 120) return;
    lastCollision = now;
    const area = (room) => Math.abs(room.polygon.reduce((sum, [x0, y0], i, pts) => {
      const [x1, y1] = pts[(i + 1) % pts.length];
      return sum + x0 * y1 - x1 * y0;
    }, 0));
    const rooms = [...parts.rooms.values()].filter(r => !r.el.hidden)
      .sort((a, b) => area(b.room) - area(a.room)).map(r => r.el);
    const pins = parts.pins.filter(p => !p.el.hidden).map(p => p.el);
    [...pins, ...rooms].forEach(el => el.classList.remove('is-crowded', 'is-compact'));
    const kept = [];
    const clashes = (r) => kept.some(k => r.left < k.right - 2 && r.right > k.left + 2
      && r.top < k.bottom - 2 && r.bottom > k.top + 2);
    for (const el of pins) {
      const r = el.getBoundingClientRect();
      if (r.width) kept.push(r);
    }
    for (const el of rooms) {
      let r = el.getBoundingClientRect();
      if (!r.width) continue;
      if (clashes(r)) {
        el.classList.add('is-compact');
        r = el.getBoundingClientRect();
        if (clashes(r)) { el.classList.add('is-crowded'); continue; }
      }
      kept.push(r);
    }
  }

  function tick() {
    if (!active) return;
    controls.update();
    if (parts) {
      for (const entry of parts.leaves.values()) {
        for (const leaf of entry.leaves) {
          if (Math.abs(leaf.target - leaf.angle) > 0.001) {
            leaf.angle += (leaf.target - leaf.angle) * 0.12;
            leaf.pivot.rotation.y = leaf.angle;
            dirty = true;
          }
        }
      }
    }
    if (!dirty) return;
    dirty = false;
    renderer.render(scene, camera);
    labels.render(scene, camera);
    separateLabels();
  }

  function resize() {
    const rect = host.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    renderer.setSize(rect.width, rect.height);
    labels.setSize(rect.width, rect.height);
    camera.aspect = rect.width / rect.height;
    camera.updateProjectionMatrix();
    if (needsFrame) frame();
    dirty = true;
  }

  const observer = new ResizeObserver(resize);
  observer.observe(host);
  const scheme = matchMedia('(prefers-color-scheme: dark)');
  const onScheme = () => build();
  scheme.addEventListener('change', onScheme);

  resize();
  build();
  renderer.setAnimationLoop(active ? tick : null);

  return {
    get levels() { return levels; },
    get level() { return level; },
    setLevel(id) {
      if (id === level || !levels.some(l => l.id === id)) return;
      level = id;
      selectedRoom = null;
      build();
    },
    update(nextZones) {
      zones = L.zonesById(nextZones);
      sensors = L.sensorsById(nextZones);
      heaterZone = L.heaterZones(nextZones);
      recolour();
    },
    setModel(nextModel) {
      model = nextModel;
      levels = L.levelsInUse(model);
      whole = L.bounds(model);
      if (!levels.some(l => l.id === level)) level = (levels[0] || { id: null }).id;
      if (selectedRoom && !(model.rooms || []).some(r => r.id === selectedRoom && r.level === level)) {
        selectedRoom = null;
      }
      build();
    },
    select(roomId) { selectedRoom = roomId; highlight(); },
    reframe() { resize(); frame(); dirty = true; },
    setActive(on) {
      active = !!on;
      renderer.setAnimationLoop(active ? tick : null);
      if (active) { resize(); dirty = true; }
    },
    destroy() {
      renderer.setAnimationLoop(null);
      observer.disconnect();
      scheme.removeEventListener('change', onScheme);
      controls.dispose();
      disposeScene();
      renderer.dispose();
      renderer.domElement.remove();
      labels.domElement.remove();
    },
  };
}
