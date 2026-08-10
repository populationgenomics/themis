/**
 * The seek-and-give-up animation on the not-found page.
 *
 * A gold mascot walks on, searches with a magnifying glass, drops it, shrugs, sits
 * down and sheds a tear. The timeline is a pure function of time — every track is a
 * keyframe sampler, so any frame can be evaluated in isolation (which is what the
 * reduced-motion path does).
 */

import * as THREE from "three";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

const E = {
  linear: (u: number) => u,
  inQuad: (u: number) => u * u,
  outQuad: (u: number) => u * (2 - u),
  inOutQuad: (u: number) => (u < 0.5 ? 2 * u * u : 1 - (-2 * u + 2) ** 2 / 2),
  inCubic: (u: number) => u * u * u,
  outCubic: (u: number) => 1 - (1 - u) ** 3,
  inOutCubic: (u: number) =>
    u < 0.5 ? 4 * u * u * u : 1 - (-2 * u + 2) ** 3 / 2,
  outBack: (u: number) => {
    const c = 1.70158;
    return 1 + (c + 1) * (u - 1) ** 3 + c * (u - 1) ** 2;
  },
  outBounce: (u: number) => {
    const n = 7.5625;
    const d = 2.75;
    if (u < 1 / d) return n * u * u;
    if (u < 2 / d) {
      const v = u - 1.5 / d;
      return n * v * v + 0.75;
    }
    if (u < 2.5 / d) {
      const v = u - 2.25 / d;
      return n * v * v + 0.9375;
    }
    const v = u - 2.625 / d;
    return n * v * v + 0.984375;
  },
} as const;

type EaseName = keyof typeof E;
/** `[time, value, easeIntoThisKey]` — the ease defaults to linear. */
type Keyframe = readonly [number, number, EaseName?];
type Track = (t: number) => number;

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));
const lerp = (a: number, b: number, u: number) => a + (b - a) * u;

export function keyframes(keys: readonly Keyframe[]): Track {
  for (let i = 1; i < keys.length; i++) {
    if (keys[i][0] <= keys[i - 1][0]) {
      throw new Error(
        `keyframe times must strictly increase: ${keys[i - 1][0]} → ${keys[i][0]}`,
      );
    }
  }
  return (t: number) => {
    if (t <= keys[0][0]) return keys[0][1];
    for (let i = 1; i < keys.length; i++) {
      const [t1, v1, ease] = keys[i];
      if (t <= t1) {
        const [t0, v0] = keys[i - 1];
        return v0 + (v1 - v0) * E[ease ?? "linear"]((t - t0) / (t1 - t0));
      }
    }
    return keys[keys.length - 1][1];
  };
}

/** Smooth 0→1→0 excursion centred in `[t0, t0 + dur]`; 0 outside it. */
export function pulse(t: number, t0: number, dur: number): number {
  const u = (t - t0) / dur;
  if (u <= 0 || u >= 1) return 0;
  return 0.5 - 0.5 * Math.cos(u * Math.PI * 2);
}

/** Pill centreline for the body ring: `r === hw` gives semicircular ends. */
function roundedRectCurve(
  hw: number,
  hh: number,
  r: number,
): THREE.Curve<THREE.Vector3> {
  const p = new THREE.Path();
  p.moveTo(-hw + r, -hh);
  p.lineTo(hw - r, -hh);
  p.absarc(hw - r, -hh + r, r, -Math.PI / 2, 0);
  p.lineTo(hw, hh - r);
  p.absarc(hw - r, hh - r, r, 0, Math.PI / 2);
  p.lineTo(-hw + r, hh);
  p.absarc(-hw + r, hh - r, r, Math.PI / 2, Math.PI);
  p.lineTo(-hw, -hh + r);
  p.absarc(-hw + r, -hh + r, r, Math.PI, Math.PI * 1.5);
  const pts = p.getSpacedPoints(140).map((v) => new THREE.Vector3(v.x, v.y, 0));
  pts.pop(); // the closed curve supplies the joining segment
  return new THREE.CatmullRomCurve3(pts, true, "centripetal");
}

/** Polygon of `[x, y, cornerRadius]` → shape with quadratic corner fillets. */
function roundedShape(
  pts: readonly (readonly [number, number, number])[],
): THREE.Shape {
  const s = new THREE.Shape();
  const n = pts.length;
  const P = (i: number) => {
    const p = pts[(i + n) % n];
    return new THREE.Vector2(p[0], p[1]);
  };
  for (let i = 0; i < n; i++) {
    const prev = P(i - 1);
    const cur = P(i);
    const next = P(i + 1);
    const r = pts[i][2];
    const d0 = cur.clone().sub(prev).normalize();
    const d1 = next.clone().sub(cur).normalize();
    const a = cur.clone().sub(d0.clone().multiplyScalar(r));
    const b = cur.clone().add(d1.clone().multiplyScalar(r));
    if (i === 0) s.moveTo(a.x, a.y);
    else s.lineTo(a.x, a.y);
    if (r > 0) s.quadraticCurveTo(cur.x, cur.y, b.x, b.y);
  }
  s.closePath();
  return s;
}

function tubeAlong(
  points: THREE.Vector3[],
  radius: number,
  segs = 24,
): THREE.TubeGeometry {
  return new THREE.TubeGeometry(
    new THREE.CatmullRomCurve3(points),
    segs,
    radius,
    10,
    false,
  );
}

function sphere(r: number, mat: THREE.Material, w = 24, h = 16): THREE.Mesh {
  return new THREE.Mesh(new THREE.SphereGeometry(r, w, h), mat);
}

function blobTexture(): THREE.CanvasTexture {
  const c = document.createElement("canvas");
  c.width = 256;
  c.height = 256;
  const g = c.getContext("2d");
  if (!g) throw new Error("2d canvas context unavailable for the shadow blob");
  const grad = g.createRadialGradient(128, 128, 8, 128, 128, 126);
  grad.addColorStop(0, "rgba(70,60,42,0.55)");
  grad.addColorStop(0.45, "rgba(70,60,42,0.30)");
  grad.addColorStop(0.75, "rgba(70,60,42,0.10)");
  grad.addColorStop(1, "rgba(70,60,42,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 256, 256);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

const RING_HW = 0.47;
const RING_HH = 0.65;
const STAND_Y = 1.06;
const SIT_Y = 0.76;
const EYE_DOME_H = 0.062; // flat, watch-glass bulge
const EYE_INSET = -0.018; // rim recessed behind the bezel's hole wall
const BROW_Y = 0.44;
const MOUTH_Y = -0.245;
const LENS_R = 0.26;
const END = 13.1;
const DROP_T = 7.08;

function createMaterials() {
  const gold = new THREE.MeshPhysicalMaterial({
    color: 0xe6b84f,
    metalness: 1.0,
    roughness: 0.21,
    clearcoat: 0.5,
    clearcoatRoughness: 0.25,
    envMapIntensity: 1.35,
  });
  const goldDS = gold.clone();
  goldDS.side = THREE.DoubleSide;
  return {
    gold,
    goldDS,
    eyeWhite: new THREE.MeshPhysicalMaterial({
      color: 0xffffff,
      roughness: 0.38,
      metalness: 0,
      clearcoat: 0.25,
      clearcoatRoughness: 0.35,
      envMapIntensity: 0.5, // porcelain: stop the white mirroring the gold surroundings
    }),
    pupil: new THREE.MeshPhysicalMaterial({
      color: 0x17110b,
      roughness: 0.1,
      metalness: 0,
      clearcoat: 1,
      clearcoatRoughness: 0.05,
    }),
    mouthDark: new THREE.MeshStandardMaterial({
      color: 0x241708,
      roughness: 0.6,
    }),
    handle: new THREE.MeshPhysicalMaterial({
      color: 0x17171a,
      roughness: 0.35,
      metalness: 0.25,
      clearcoat: 0.6,
      clearcoatRoughness: 0.2,
    }),
    glass: new THREE.MeshPhysicalMaterial({
      color: 0xffffff,
      transmission: 1,
      thickness: 0.3,
      ior: 1.52,
      roughness: 0.03,
      metalness: 0,
      specularIntensity: 1,
    }),
    catchlight: new THREE.MeshBasicMaterial({ color: 0xffffff }),
  };
}

type Materials = ReturnType<typeof createMaterials>;

/**
 * Body ring: a squircle strip swept along the pill path. Radial width is modulated by
 * path direction — horizontal runs get ~30% extra so they don't read thinner than the
 * sides under camera foreshortening.
 */
function ringGeometry(): THREE.BufferGeometry {
  const profile = roundedShape([
    [-0.058, -0.0575, 0.03],
    [0.058, -0.0575, 0.03],
    [0.058, 0.0575, 0.03],
    [-0.058, 0.0575, 0.03],
  ]);
  const curve = roundedRectCurve(RING_HW, RING_HH, RING_HW);
  const prof = profile.getPoints(10);
  if (prof.length > 1 && prof[0].distanceTo(prof[prof.length - 1]) < 1e-6)
    prof.pop();
  const N = 220;
  const M = prof.length;
  const pos = new Float32Array(N * M * 3);
  for (let i = 0; i < N; i++) {
    const P = curve.getPointAt(i / N);
    const T = curve.getTangentAt(i / N).normalize();
    const nx = T.y;
    const ny = -T.x;
    const wS = 1 + 0.3 * T.x * T.x;
    for (let j = 0; j < M; j++) {
      const o = (i * M + j) * 3;
      pos[o] = P.x + nx * prof[j].x * wS;
      pos[o + 1] = P.y + ny * prof[j].x * wS;
      pos[o + 2] = prof[j].y;
    }
  }
  const idx: number[] = [];
  for (let i = 0; i < N; i++) {
    for (let j = 0; j < M; j++) {
      const a = i * M + j;
      const b = ((i + 1) % N) * M + j;
      const c = ((i + 1) % N) * M + ((j + 1) % M);
      const d = i * M + ((j + 1) % M);
      idx.push(a, b, d, b, c, d);
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return g;
}

/**
 * Radial lookup r(phi) from the eye window's centre to its boundary, so domes can be
 * clipped to the triangular socket.
 */
function radialTable(shape: THREE.Shape): (phi: number) => number {
  const pts = shape.getPoints(160);
  const N = 256;
  const out = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    const phi = (i / N) * Math.PI * 2;
    const dx = Math.cos(phi);
    const dy = Math.sin(phi);
    let best = 0.05;
    for (let j = 0; j < pts.length; j++) {
      const a = pts[j];
      const b = pts[(j + 1) % pts.length];
      const ex = b.x - a.x;
      const ey = b.y - a.y;
      const den = dx * ey - dy * ex;
      if (Math.abs(den) < 1e-9) continue;
      const t = (a.x * ey - a.y * ex) / den;
      const s = (a.x * dy - a.y * dx) / den;
      if (t > 0 && s >= 0 && s <= 1) best = Math.max(best, t);
    }
    out[i] = best;
  }
  return (phi) => out[((Math.round((phi / (Math.PI * 2)) * N) % N) + N) % N];
}

/**
 * Dome whose rim is exactly the window outline, trending to spherical above it: contours
 * blend triangle → circle with elevation, so the ball sits deeper in the corners.
 */
function makeDome(
  rwAt: (phi: number) => number,
  mat: THREE.Material,
  scaleR: number,
  height: number,
  roundR: number,
): THREE.Mesh {
  const g = new THREE.SphereGeometry(1, 64, 26, 0, Math.PI * 2, 0, Math.PI / 2);
  const pos = g.attributes.position;
  for (let i = 0; i < pos.count; i++) {
    const x = pos.getX(i);
    const y = pos.getY(i); // cos(theta), 0 at the rim
    const z = pos.getZ(i);
    const rTri = rwAt(Math.atan2(-z, x)) * scaleR;
    // the blend spans most of the height: only the crown becomes fully spherical
    const w = clamp((y - 0.15) / 0.7, 0, 1);
    const ws = w * w * (3 - 2 * w);
    const r = rTri + (roundR - rTri) * ws;
    pos.setXYZ(i, x * r, -z * r, y * y * height);
  }
  g.computeVertexNormals();
  return new THREE.Mesh(g, mat);
}

function createCharacter(mat: Materials, scene: THREE.Scene) {
  const root = new THREE.Group();
  const yawN = new THREE.Group();
  const leanN = new THREE.Group();
  root.add(yawN);
  yawN.add(leanN);
  scene.add(root);
  leanN.add(new THREE.Mesh(ringGeometry(), mat.gold));

  const face = new THREE.Group();
  face.position.z = 0.02;
  leanN.add(face);

  const eyeUnit = new THREE.Group();
  eyeUnit.position.set(0, 0.175, 0.05);
  eyeUnit.scale.setScalar(1.18);
  face.add(eyeUnit);

  // socket: rounded-triangle picture frame, apex down — the logo mark
  const eyeWindow = roundedShape([
    [-0.185, 0.115, 0.07],
    [0.185, 0.115, 0.07],
    [0, -0.205, 0.07],
  ]);
  const frameShape = roundedShape([
    [-0.211, 0.141, 0.075],
    [0.211, 0.141, 0.075],
    [0, -0.234, 0.075],
  ]);
  frameShape.holes.push(eyeWindow);
  // near-zero depth so the front/back bevel shoulders merge into ONE rim; a deep slab
  // reads as two stacked triangle ridges from 3/4 angles
  eyeUnit.add(
    new THREE.Mesh(
      new THREE.ExtrudeGeometry(frameShape, {
        depth: 0.012,
        bevelEnabled: true,
        bevelThickness: 0.01,
        bevelSize: 0.01,
        bevelSegments: 3,
      }),
      mat.gold,
    ),
  );
  // solid gold pod closes the back of the socket — the eyeball has no drawn back.
  // Oversized and pulled forward so it radially overlaps the bezel's hole wall, sealing
  // the seam that otherwise leaks backdrop at glancing angles.
  const pod = new THREE.Mesh(
    new THREE.ExtrudeGeometry(eyeWindow, {
      depth: 0.04,
      bevelEnabled: true,
      bevelThickness: 0.03,
      bevelSize: 0.026,
      bevelSegments: 4,
    }),
    mat.gold,
  );
  pod.scale.set(1.06, 1.06, 1);
  pod.position.z = -0.088;
  eyeUnit.add(pod);

  const rwAt = radialTable(eyeWindow);
  const eyeWhite = makeDome(rwAt, mat.eyeWhite, 1.01, EYE_DOME_H, 0.14);
  eyeWhite.position.z = EYE_INSET;
  eyeUnit.add(eyeWhite);

  // lids: the same dome surface in gold, revealed by clipping planes, so the border
  // reads as smoothly extending over the eye; fully closed = solid gold pod
  const planes = {
    lidTop: new THREE.Plane(new THREE.Vector3(0, 1, 0), 1),
    lidBot: new THREE.Plane(new THREE.Vector3(0, -1, 0), 1),
    openTop: new THREE.Plane(new THREE.Vector3(0, -1, 0), 1),
    openBot: new THREE.Plane(new THREE.Vector3(0, 1, 0), 1),
  };
  const lidMTop = mat.gold.clone();
  lidMTop.side = THREE.DoubleSide;
  lidMTop.clippingPlanes = [planes.lidTop];
  const lidMBot = mat.gold.clone();
  lidMBot.side = THREE.DoubleSide;
  lidMBot.clippingPlanes = [planes.lidBot];
  const lidTop = makeDome(rwAt, lidMTop, 1.035, 0.068, 0.146);
  const lidBot = makeDome(rwAt, lidMBot, 1.035, 0.068, 0.146);
  lidTop.position.z = EYE_INSET + 0.004;
  lidBot.position.z = EYE_INSET + 0.004;
  eyeUnit.add(lidTop, lidBot);

  // pupil: glossy bead sliding on the dome, wiped by the lid edge as it closes
  mat.pupil.clippingPlanes = [planes.openTop, planes.openBot];
  mat.catchlight.clippingPlanes = [planes.openTop, planes.openBot];
  const pupil = sphere(0.084, mat.pupil, 24, 16);
  pupil.scale.z = 0.4;
  const catchlight = sphere(0.016, mat.catchlight, 10, 8);
  eyeUnit.add(pupil, catchlight);

  // mono-brow: a flat arc floating above the eye
  const brow = new THREE.Group();
  brow.position.set(0, BROW_Y, 0.17);
  face.add(brow);
  const browScaler = new THREE.Group(); // arc flattening without touching the pop scale
  brow.add(browScaler);
  const browA0 = Math.PI * (0.5 - 0.36);
  const browArc = new THREE.Mesh(
    new THREE.TorusGeometry(0.17, 0.042, 12, 40, Math.PI * 0.72),
    mat.gold,
  );
  browArc.rotation.z = browA0;
  browScaler.add(browArc);
  for (const a of [browA0, browA0 + Math.PI * 0.72]) {
    const cap = sphere(0.042, mat.gold, 12, 10);
    cap.position.set(Math.cos(a) * 0.17, Math.sin(a) * 0.17, 0);
    browScaler.add(cap);
  }
  browScaler.position.y = -0.06; // pivot near the arc chord
  browScaler.scale.y = 0.62;

  // squiggle mouth — rebuilt each frame for a living wobble
  const squig = new THREE.Group();
  squig.position.set(0, MOUTH_Y, 0.05);
  const squigTube = new THREE.Mesh(new THREE.BufferGeometry(), mat.gold);
  const squigCapA = sphere(0.047, mat.gold, 12, 10);
  const squigCapB = sphere(0.047, mat.gold, 12, 10);
  squig.add(squigTube, squigCapA, squigCapB);
  face.add(squig);

  const ohMouth = new THREE.Group();
  ohMouth.position.set(0, MOUTH_Y, 0.05);
  const ohRim = new THREE.Mesh(
    new THREE.TorusGeometry(0.13, 0.045, 12, 30),
    mat.gold,
  );
  ohRim.scale.set(0.9, 1.1, 1);
  const ohDark = new THREE.Mesh(
    new THREE.CircleGeometry(0.125, 24),
    mat.mouthDark,
  );
  ohDark.scale.set(0.9, 1.1, 1);
  ohDark.position.z = -0.012;
  ohMouth.add(ohRim, ohDark);
  ohMouth.scale.setScalar(0.001);
  face.add(ohMouth);

  const frown = new THREE.Group();
  const frownArc = new THREE.Mesh(
    new THREE.TorusGeometry(0.23, 0.048, 12, 36, Math.PI * 0.62),
    mat.gold,
  );
  const frownA0 = Math.PI * (0.5 - 0.31);
  frownArc.rotation.z = frownA0;
  frown.add(frownArc);
  for (const a of [frownA0, frownA0 + Math.PI * 0.62]) {
    const cap = sphere(0.048, mat.gold, 12, 10);
    cap.position.set(Math.cos(a) * 0.23, Math.sin(a) * 0.23, 0);
    frown.add(cap);
  }
  frown.position.set(0, MOUTH_Y - 0.14, 0.05); // ∩ arc: lift the chord to mouth height
  frown.scale.setScalar(0.001);
  face.add(frown);

  const tear = new THREE.Group();
  const drop = sphere(0.055, mat.gold, 16, 12);
  drop.scale.set(0.8, 1.15, 0.8);
  const tip = new THREE.Mesh(new THREE.ConeGeometry(0.042, 0.09, 12), mat.gold);
  tip.position.y = 0.075;
  tear.add(drop, tip);
  tear.scale.setScalar(0.001);
  face.add(tear);

  const shoulderL = new THREE.Object3D();
  shoulderL.position.set(-0.47, 0.05, 0);
  const shoulderR = new THREE.Object3D();
  shoulderR.position.set(0.47, 0.05, 0);
  const hipL = new THREE.Object3D();
  hipL.position.set(-0.2, -0.63, 0);
  const hipR = new THREE.Object3D();
  hipR.position.set(0.2, -0.63, 0);
  leanN.add(shoulderL, shoulderR, hipL, hipR);

  return {
    root,
    yawN,
    leanN,
    face,
    eyeUnit,
    lidTop,
    lidBot,
    pupil,
    catchlight,
    planes,
    brow,
    browScaler,
    squig,
    squigTube,
    squigCapA,
    squigCapB,
    ohMouth,
    frown,
    tear,
    shoulderL,
    shoulderR,
    hipL,
    hipR,
  };
}

type Character = ReturnType<typeof createCharacter>;

function makeHose(mat: Materials, scene: THREE.Scene, radius: number) {
  const mesh = new THREE.Mesh(new THREE.BufferGeometry(), mat.gold);
  scene.add(mesh);
  return {
    mesh,
    update(p0: THREE.Vector3, p1: THREE.Vector3, midOffset: THREE.Vector3) {
      const mid = p0.clone().lerp(p1, 0.5).add(midOffset);
      mid.y = Math.max(mid.y, radius + 0.012); // sag rests on the floor, never below
      mesh.geometry.dispose();
      mesh.geometry = tubeAlong([p0, mid, p1], radius, 18);
    },
  };
}

function makeHand(
  mat: Materials,
  scene: THREE.Scene,
  side: number,
): THREE.Group {
  const g = new THREE.Group();
  const palm = sphere(0.095, mat.gold, 20, 14);
  palm.scale.set(1, 0.82, 0.66);
  const thumb = sphere(0.042, mat.gold, 12, 10);
  thumb.position.set(side * -0.06, 0.015, 0.05);
  g.add(palm, thumb);
  scene.add(g);
  return g;
}

/** Boot foot: a rounded loaf with a flat sole, group origin at the sole. */
function makeFoot(mat: Materials, scene: THREE.Scene): THREE.Group {
  const g = new THREE.Group();
  const dome = new THREE.Mesh(
    new THREE.SphereGeometry(0.105, 24, 14, 0, Math.PI * 2, 0, Math.PI / 2),
    mat.gold,
  );
  dome.scale.set(0.85, 1.0, 1.3);
  const sole = new THREE.Mesh(new THREE.CircleGeometry(0.105, 24), mat.goldDS);
  sole.scale.set(0.85, 1.3, 1);
  sole.rotation.x = Math.PI / 2;
  sole.position.y = 0.0015;
  g.add(dome, sole);
  scene.add(g);
  return g;
}

function createMagnifier(mat: Materials, scene: THREE.Scene): THREE.Group {
  const magnifier = new THREE.Group(); // origin at the grip point on the handle
  const lens = new THREE.Group();
  lens.position.y = 0.46;
  const rimProfile = roundedShape([
    [-0.026, -0.0125, 0.008],
    [0.026, -0.0125, 0.008],
    [0.026, 0.0125, 0.008],
    [-0.026, 0.0125, 0.008],
  ]);
  const rimPath = new THREE.CatmullRomCurve3(
    Array.from({ length: 48 }, (_, i) => {
      const a = (i / 48) * Math.PI * 2;
      return new THREE.Vector3(Math.cos(a) * LENS_R, Math.sin(a) * LENS_R, 0);
    }),
    true,
    "centripetal",
  );
  lens.add(
    new THREE.Mesh(
      new THREE.ExtrudeGeometry(rimProfile, {
        steps: 96,
        bevelEnabled: false,
        extrudePath: rimPath,
      }),
      mat.gold,
    ),
  );
  const glass = new THREE.Mesh(
    new THREE.CylinderGeometry(LENS_R - 0.01, LENS_R - 0.01, 0.018, 48),
    mat.glass,
  );
  glass.rotation.x = Math.PI / 2;
  lens.add(glass);
  const ferrule = new THREE.Mesh(
    new THREE.CylinderGeometry(0.05, 0.055, 0.09, 16),
    mat.gold,
  );
  ferrule.position.y = 0.155;
  const handle = new THREE.Mesh(
    new THREE.CylinderGeometry(0.048, 0.056, 0.34, 16),
    mat.handle,
  );
  handle.position.y = -0.02;
  const buttCap = sphere(0.056, mat.handle, 14, 10);
  buttCap.position.y = -0.19;
  magnifier.add(lens, ferrule, handle, buttCap);
  scene.add(magnifier);
  return magnifier;
}

function makeBlob(
  scene: THREE.Scene,
  w: number,
  d: number,
): THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial> {
  const m = new THREE.Mesh(
    new THREE.PlaneGeometry(w, d),
    new THREE.MeshBasicMaterial({
      map: blobTexture(),
      transparent: true,
      depthWrite: false,
    }),
  );
  m.rotation.x = -Math.PI / 2;
  m.position.y = 0.004;
  scene.add(m);
  return m;
}

// The inferred key set is load-bearing: it is what makes every `Tr.<name>` read below a
// checked access. Widening it to `Record<string, Track>` silently unchecks all of them.
/** Every animated quantity, as a keyframe sampler over the 13.1s timeline. */
export function createTimeline() {
  return {
    rootX: keyframes([
      [0, -4.6],
      [2.0, -0.4, "linear"],
      [2.45, 0, "outCubic"],
      [4.6, 0],
      [5.05, -0.34, "inOutQuad"],
      [5.95, -0.34],
      [6.42, 0.26, "inOutQuad"],
      [7.0, 0, "inOutQuad"],
    ]),
    rootY: keyframes([
      [0, STAND_Y],
      [9.6, STAND_Y],
      [9.78, STAND_Y + 0.06, "outQuad"],
      [10.3, SIT_Y, "outBounce"],
    ]),
    yaw: keyframes([
      [0, 0.95],
      [2.0, 0.95],
      [2.5, 0, "inOutCubic"],
      [2.85, -0.24, "inOutCubic"],
      [3.2, 0.24, "inOutCubic"],
      [3.6, 0, "inOutCubic"],
      [4.6, 0],
      [5.05, -0.62, "inOutCubic"],
      [5.95, -0.62],
      [6.45, 0.55, "inOutCubic"],
      [6.95, 0, "inOutCubic"],
    ]),
    pitch: keyframes([
      [0, 0.07],
      [2.0, 0.07],
      [2.45, 0, "outQuad"],
      [4.65, 0],
      [5.1, 0.3, "inOutCubic"],
      [5.5, 0.44, "inOutQuad"],
      [6.05, 0.32, "inOutQuad"],
      [6.5, 0.3],
      [6.95, 0, "inOutCubic"],
      [9.1, 0],
      [9.28, 0.1, "outQuad"],
      [9.7, 0.04, "inOutQuad"],
      [10.05, 0.04],
      [10.45, 0.13, "outQuad"],
    ]),
    squash: keyframes([
      [9.6, 1],
      [9.78, 1.04, "outQuad"],
      [10.12, 0.93, "inQuad"],
      [10.5, 1, "outBack"],
    ]),
    sitW: keyframes([
      [9.62, 0],
      [10.28, 1, "inOutCubic"],
    ]),
    kickW: keyframes([
      [5.02, 0],
      [5.4, 1, "inOutCubic"],
      [5.9, 1],
      [6.3, 0, "inOutCubic"],
    ]),

    browRaise: keyframes([
      [0, 0.35],
      [2.4, 0.35],
      [2.8, 0.6, "inOutQuad"],
      [3.5, 0.9, "outQuad"],
      [4.3, 0.6, "inOutQuad"],
      [6.55, 0.6],
      [6.9, 1.0, "outQuad"],
      [7.55, 0.15, "inOutQuad"],
      [8.3, 0.4, "inOutQuad"],
      [9.6, 0.15, "inOutQuad"],
    ]),
    browSad: keyframes([
      [0, 0.1],
      [6.95, 0.1],
      [7.6, 0.8, "inOutQuad"],
      [8.5, 0.95, "inOutQuad"],
    ]),
    browFurrow: keyframes([
      [4.95, 0],
      [5.35, 0.85, "inOutQuad"],
      [6.25, 0.85],
      [6.75, 0, "inOutQuad"],
    ]),
    lookX: keyframes([
      [0, 0.6],
      [2.25, 0.6],
      [2.7, -0.75, "inOutCubic"],
      [3.15, 0.75, "inOutCubic"],
      [3.55, 0.1, "inOutCubic"],
      [4.6, 0.1],
      [5.0, -0.6, "inOutCubic"],
      [5.9, -0.4],
      [6.4, 0.55, "inOutCubic"],
      [6.8, 0.05, "inOutCubic"],
      [9.15, 0.05],
      [9.3, 0.1],
      [10.35, 0, "inOutCubic"],
    ]),
    lookY: keyframes([
      [0, 0.05],
      [3.6, 0.05],
      [4.1, -0.1],
      [4.9, -0.75, "inOutCubic"],
      [6.35, -0.75],
      [6.8, 0.1, "inOutCubic"],
      [7.6, -0.1, "inOutQuad"],
      [9.15, -0.1],
      [9.32, 0.95, "outCubic"],
      [10.0, 0.95],
      [10.5, -0.55, "inOutCubic"],
    ]),
    lidBase: keyframes([
      [0, 0],
      [7.45, 0],
      [8.05, 0.22, "inOutQuad"],
      [10.3, 0.26],
      [10.9, 0.38, "inOutQuad"],
    ]),

    wSquig: keyframes([
      [0, 1],
      [3.55, 1],
      [3.75, 0, "inQuad"],
      [4.9, 0],
      [5.15, 1, "outBack"],
      [6.3, 1],
      [6.55, 0, "inQuad"],
    ]),
    wOh: keyframes([
      [3.6, 0],
      [3.85, 1, "outBack"],
      [4.9, 1],
      [5.12, 0, "inQuad"],
      [6.5, 0],
      [6.75, 1, "outBack"],
      [7.4, 1],
      [7.62, 0, "inQuad"],
    ]),
    wFrown: keyframes([
      [7.55, 0],
      [7.85, 1, "outBack"],
    ]),
    squigAmp: keyframes([
      [0, 1],
      [4.9, 1],
      [5.3, 1.8],
      [6.2, 1.8],
      [6.6, 1],
    ]),

    wRaiseR: keyframes([
      [3.48, 0],
      [3.9, 1, "outCubic"],
      [4.55, 1],
      [5.05, 0, "inOutCubic"],
    ]),
    wSweepR: keyframes([
      [4.55, 0],
      [5.05, 1, "inOutCubic"],
      [6.42, 1],
      [6.9, 0, "inOutCubic"],
    ]),
    wDropR: keyframes([
      [6.45, 0],
      [6.9, 1, "inOutCubic"],
      [7.25, 1],
      [7.7, 0, "inOutCubic"],
    ]),
    wShrug: keyframes([
      [7.95, 0],
      [8.4, 1, "outBack"],
      [9.55, 1],
      [10.2, 0, "inOutCubic"],
    ]),
    wSitHand: keyframes([
      [10.05, 0],
      [10.5, 1, "outCubic"],
    ]),
    wBalanceL: keyframes([
      [4.55, 0],
      [5.1, 1, "inOutCubic"],
      [6.42, 1],
      [6.9, 0, "inOutCubic"],
    ]),
    sweepX: keyframes([
      [4.9, -0.55],
      [5.5, -0.2, "inOutQuad"],
      [5.95, -0.35, "inOutQuad"],
      [6.45, 0.6, "inOutCubic"],
      [6.85, 0.4],
    ]),

    glassScale: keyframes([
      [3.52, 0],
      [3.95, 1, "outBack"],
    ]),
    glassRotX: keyframes([
      [3.9, 0.08],
      [4.65, 0.08],
      [5.2, 1.3, "inOutCubic"],
      [6.35, 1.3],
      [6.9, 0.5, "inOutCubic"],
    ]),
    glassRotZ: keyframes([
      [3.9, 0.55],
      [4.65, 0.55],
      [5.2, 0.12, "inOutCubic"],
      [6.9, 0.06, "inOutCubic"],
    ]),

    gFallY: keyframes([
      [DROP_T, 0.52],
      [DROP_T + 0.2, 0.06, "inQuad"],
      [DROP_T + 0.3, 0.16, "outQuad"],
      [DROP_T + 0.4, 0.056, "inQuad"],
      [DROP_T + 0.47, 0.095, "outQuad"],
      [DROP_T + 0.54, 0.055, "inQuad"],
    ]),
    gFallX: keyframes([
      [DROP_T, 0.62],
      [DROP_T + 0.54, 1.05, "outQuad"],
    ]),
    gFallZ: keyframes([
      [DROP_T, 0.34],
      [DROP_T + 0.54, 0.55, "outQuad"],
    ]),
    // rest tips a hair past flat so the slim rim's far edge grounds while the fatter
    // handle butt rests
    gFallRX: keyframes([
      [DROP_T, 0.5],
      [DROP_T + 0.3, -1.15, "outQuad"],
      [DROP_T + 0.54, -(Math.PI / 2 + 0.05), "outQuad"],
    ]),
    gFallRZ: keyframes([
      [DROP_T, 0.06],
      [DROP_T + 0.54, -0.6, "outQuad"],
    ]),

    tearScale: keyframes([
      [10.65, 0],
      [11.4, 1, "outQuad"],
      [11.52, 1],
      [11.55, 0.001],
    ]),
    tearY: keyframes([
      [11.5, 0],
      [11.86, -0.72, "inQuad"],
    ]),
    splashS: keyframes([
      [11.84, 0.001],
      [11.92, 1.1, "outQuad"],
      [12.7, 2.0, "outQuad"],
    ]),
    splashO: keyframes([
      [11.84, 0.95],
      [12.35, 0.85],
      [13.0, 0, "inQuad"],
    ]),
    fade: keyframes([
      [0, 1],
      [0.55, 0, "outQuad"],
    ]),
  };
}

const BLINKS: readonly (readonly [number, number])[] = [
  [1.5, 0.24],
  [2.98, 0.24],
  [4.35, 0.26],
  [6.6, 0.22],
  [7.5, 0.4],
  [9.42, 0.16],
  [9.62, 0.16],
  [10.55, 0.45],
];

function squigGeometry(amp: number, phase: number): THREE.TubeGeometry {
  const pts: THREE.Vector3[] = [];
  for (let i = 0; i <= 20; i++) {
    const u = i / 20;
    pts.push(
      new THREE.Vector3(
        (u - 0.5) * 0.58,
        Math.sin(u * Math.PI * 2 * 1.6 + phase) * 0.054 * amp,
        0,
      ),
    );
  }
  return tubeAlong(pts, 0.047, 26);
}

interface Foot {
  side: number;
  planted: THREE.Vector3;
  swing: {
    t0: number;
    dur: number;
    from: THREE.Vector3;
    to: THREE.Vector3;
  } | null;
}

export interface MountOptions {
  /** Render a single settled frame instead of running the timeline. */
  reducedMotion: boolean;
}

/**
 * Build the scene inside `host` and start it. `host` must be positioned and carry a
 * resolved background colour: the scene, its fog and its floor all derive from that
 * colour so the render blends into whatever surface token the caller applied.
 *
 * Returns a disposer that cancels the loop, drops listeners and frees GPU resources.
 */
export function mountNotFoundScene(
  host: HTMLElement,
  options: MountOptions,
): () => void {
  const hostBg = getComputedStyle(host).backgroundColor;
  const bg = new THREE.Color();
  if (!hostBg || hostBg === "rgba(0, 0, 0, 0)" || hostBg === "transparent") {
    throw new Error("not-found scene host needs an opaque background colour");
  }
  bg.setStyle(hostBg);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.12;
  renderer.localClippingEnabled = true;
  renderer.domElement.style.display = "block";
  host.prepend(renderer.domElement);

  const vignette = document.createElement("div");
  vignette.style.cssText =
    "position:absolute;inset:0;pointer-events:none;background:radial-gradient(ellipse 90% 80% at 50% 42%, rgba(0,0,0,0) 52%, rgba(84,78,66,0.16) 100%)";
  const fadeEl = document.createElement("div");
  fadeEl.style.cssText = `position:absolute;inset:0;pointer-events:none;background:${hostBg};opacity:1`;
  host.append(vignette, fadeEl);

  const scene = new THREE.Scene();
  scene.background = bg;
  scene.fog = new THREE.Fog(bg, 14, 42);

  const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 100);
  const CAM_POS = new THREE.Vector3(0, 1.26, 5.75);
  const CAM_AIM = new THREE.Vector3(0, 1.1, 0);

  const pmrem = new THREE.PMREMGenerator(renderer);
  const envTexture = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environment = envTexture;

  const key = new THREE.DirectionalLight(0xfff4e0, 1.6);
  key.position.set(2.5, 5, 4);
  const rim = new THREE.DirectionalLight(0xdfe8ff, 0.7);
  rim.position.set(-3, 3, -2.5);
  scene.add(key, rim, new THREE.HemisphereLight(0xffffff, 0xcfc8ba, 0.35));

  // floor matches the backdrop so it reads as a seamless cyc
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(200, 200),
    new THREE.MeshStandardMaterial({
      color: bg,
      roughness: 0.96,
      metalness: 0,
    }),
  );
  floor.rotation.x = -Math.PI / 2;
  scene.add(floor);

  const mat = createMaterials();
  const ch: Character = createCharacter(mat, scene);
  const magnifier = createMagnifier(mat, scene);
  const armLHose = makeHose(mat, scene, 0.035);
  const armRHose = makeHose(mat, scene, 0.035);
  const legLHose = makeHose(mat, scene, 0.044);
  const legRHose = makeHose(mat, scene, 0.044);
  const handL = makeHand(mat, scene, -1);
  const handR = makeHand(mat, scene, 1);
  const footL = makeFoot(mat, scene);
  const footR = makeFoot(mat, scene);
  const charBlob = makeBlob(scene, 1.55, 0.85);
  const glassBlob = makeBlob(scene, 0.85, 0.6);
  glassBlob.material.opacity = 0;

  const splash = new THREE.Mesh(
    new THREE.CircleGeometry(0.09, 24),
    new THREE.MeshStandardMaterial({
      color: 0xc9992c,
      metalness: 0.85,
      roughness: 0.34,
      transparent: true,
    }),
  );
  splash.rotation.x = -Math.PI / 2;
  splash.position.y = 0.006;
  splash.scale.setScalar(0.001);
  scene.add(splash);

  const Tr = createTimeline();

  function lidAt(t: number): number {
    let v = Tr.lidBase(t);
    for (const [bt, bd] of BLINKS) v = Math.max(v, pulse(t, bt, bd));
    return clamp(v, 0, 1);
  }

  const feet: Foot[] = [
    { side: -1, planted: new THREE.Vector3(-4.77, 0, 0.02), swing: null },
    { side: 1, planted: new THREE.Vector3(-4.43, 0, 0.02), swing: null },
  ];
  let prevRootX: number | null = null;
  let prevT: number | null = null;

  function stepperReset(t: number) {
    const rx = Tr.rootX(t);
    const yaw = Tr.yaw(t);
    for (const f of feet) {
      f.swing = null;
      f.planted.set(
        rx + Math.cos(yaw) * f.side * 0.17,
        0,
        Math.sin(-yaw) * f.side * 0.17 + 0.02,
      );
    }
    prevRootX = rx;
    prevT = t;
  }
  stepperReset(0);

  function stepperUpdate(t: number, rx: number, yaw: number, speed: number) {
    const stance = (f: Foot) =>
      new THREE.Vector3(
        rx + Math.cos(yaw) * f.side * 0.17 + speed * 0.09,
        0,
        Math.sin(-yaw) * f.side * 0.17 + 0.02,
      );
    for (let i = 0; i < 2; i++) {
      const f = feet[i];
      const other = feet[1 - i];
      if (f.swing && (t - f.swing.t0) / f.swing.dur >= 1) {
        f.planted.copy(f.swing.to);
        f.swing = null;
      }
      if (!f.swing && !other.swing) {
        const want = stance(f);
        if (f.planted.distanceTo(want) > 0.3) {
          f.swing = {
            t0: t,
            dur: 0.2,
            from: f.planted.clone(),
            to: want.add(new THREE.Vector3(speed * 0.1, 0, 0)),
          };
        }
      }
    }
  }

  function footPos(f: Foot, t: number): THREE.Vector3 {
    if (!f.swing) return f.planted.clone();
    const u = clamp((t - f.swing.t0) / f.swing.dur, 0, 1);
    const p = f.swing.from.clone().lerp(f.swing.to, E.inOutQuad(u));
    p.y = Math.sin(u * Math.PI) * 0.15;
    return p;
  }

  const par = { x: 0, y: 0, tx: 0, ty: 0 };
  const onPointerMove = (e: PointerEvent) => {
    par.tx = (e.clientX / window.innerWidth - 0.5) * 2;
    par.ty = (e.clientY / window.innerHeight - 0.5) * 2;
  };
  if (!options.reducedMotion)
    window.addEventListener("pointermove", onPointerMove);

  const V = () => new THREE.Vector3();
  const localToWorld = (
    node: THREE.Object3D,
    x: number,
    y: number,
    z: number,
  ) => node.localToWorld(new THREE.Vector3(x, y, z));

  let lastT = 0;

  /** `t` is clamped timeline time; `tw` is wall time, which keeps the final hold alive. */
  function update(t: number, tw: number) {
    if (Math.abs(t - lastT) > 0.5) stepperReset(t);
    lastT = t;

    const rx = Tr.rootX(t);
    const speed =
      prevT !== null && prevRootX !== null && t > prevT
        ? (rx - prevRootX) / Math.max(1e-4, t - prevT)
        : 0;
    prevRootX = rx;
    prevT = t;
    const speedF = clamp(Math.abs(speed) / 2, 0, 1);
    const yaw = Tr.yaw(t);
    const sitW = Tr.sitW(t);

    const gaitPh = (rx / 0.55) * Math.PI; // gait phase from distance travelled
    const bob = Math.abs(Math.sin(gaitPh)) * 0.06 * speedF;
    const breathe = Math.sin(tw * 2.2) * (0.008 + sitW * 0.01);

    ch.root.position.set(rx, Tr.rootY(t) + bob + breathe, 0);
    ch.yawN.rotation.y = yaw;
    ch.leanN.rotation.x = Tr.pitch(t) + sitW * Math.sin(tw * 1.5) * 0.02;
    ch.leanN.rotation.z =
      Math.sin(gaitPh) * 0.06 * speedF + sitW * Math.sin(tw * 1.1) * 0.012;
    const sq = Tr.squash(t);
    ch.leanN.scale.set(1 / Math.sqrt(sq), sq, 1 / Math.sqrt(sq));
    ch.leanN.updateWorldMatrix(true, true);

    const sacc = Math.sin(tw * 7.3) * 0.02 + Math.sin(tw * 11.7) * 0.012;
    const px = (Tr.lookX(t) + sacc) * 0.085;
    const py = (Tr.lookY(t) + sacc * 0.5) * 0.075;
    const uR = Math.min(1, Math.hypot(px, py) / 0.14); // crown is ~spherical, radius 0.14
    const dz = EYE_INSET + EYE_DOME_H * Math.max(0, 1 - uR * uR);
    ch.pupil.position.set(px, py, dz - 0.006);
    ch.catchlight.position.set(px - 0.03, py + 0.034, dz + 0.012);

    let lid = lidAt(t);
    if (t >= END - 1e-3) lid = Math.max(lid, pulse(tw % 4.2, 3.3, 0.3)); // idle blinks on hold
    const cutTop = lerp(0.16, -0.03, lid);
    const cutBot = lerp(-0.26, -0.02, lid);
    const eyeM = ch.eyeUnit.matrixWorld;
    ch.planes.lidTop
      .set(new THREE.Vector3(0, 1, 0), -cutTop)
      .applyMatrix4(eyeM);
    ch.planes.lidBot
      .set(new THREE.Vector3(0, -1, 0), cutBot)
      .applyMatrix4(eyeM);
    ch.planes.openTop
      .set(new THREE.Vector3(0, -1, 0), cutTop + 0.008)
      .applyMatrix4(eyeM);
    ch.planes.openBot
      .set(new THREE.Vector3(0, 1, 0), -(cutBot - 0.008))
      .applyMatrix4(eyeM);

    const bRaise = Tr.browRaise(t);
    const bSad = Tr.browSad(t);
    const bFur = Tr.browFurrow(t);
    ch.brow.position.set(
      bSad * -0.045,
      BROW_Y + bRaise * 0.055 - bFur * 0.05 - bSad * 0.015,
      0.17,
    );
    ch.brow.rotation.z = bSad * -0.35;
    ch.brow.scale.set(
      1 + bFur * 0.08,
      1 - bFur * 0.3 + bRaise * 0.15 - bSad * 0.28,
      1,
    );

    const wS = Tr.wSquig(t);
    const wO = Tr.wOh(t);
    const wF = Tr.wFrown(t);
    ch.squig.visible = wS > 0.02;
    if (ch.squig.visible) {
      const amp = Tr.squigAmp(t) * wS;
      const ph = tw * 6;
      ch.squigTube.geometry.dispose();
      ch.squigTube.geometry = squigGeometry(amp, ph);
      ch.squigCapA.position.set(-0.29, Math.sin(ph) * 0.054 * amp, 0);
      ch.squigCapB.position.set(
        0.29,
        Math.sin(Math.PI * 3.2 + ph) * 0.054 * amp,
        0,
      );
      ch.squig.scale.setScalar(Math.max(0.001, wS));
    }
    ch.ohMouth.scale.setScalar(Math.max(0.001, wO));
    ch.ohMouth.visible = wO > 0.02;
    ch.frown.scale.setScalar(Math.max(0.001, wF));
    ch.frown.visible = wF > 0.02;

    const shL = V();
    const shR = V();
    ch.shoulderL.getWorldPosition(shL);
    ch.shoulderR.getWorldPosition(shR);

    const swing = Math.sin(gaitPh) * 0.3 * speedF;
    let handRT = localToWorld(ch.leanN, 0.68, -0.4 - swing * 0.3, 0.05 + swing);
    let handLT = localToWorld(
      ch.leanN,
      -0.68,
      -0.4 + swing * 0.3,
      0.05 - swing,
    );

    const wRaise = Tr.wRaiseR(t);
    const wSweep = Tr.wSweepR(t);
    const wDrop = Tr.wDropR(t);
    const wShrug = Tr.wShrug(t);
    const wSitH = Tr.wSitHand(t);
    const wIdleR = clamp(1 - wRaise - wSweep - wDrop - wShrug - wSitH, 0, 1);
    handRT = V()
      .addScaledVector(handRT, wIdleR)
      .addScaledVector(localToWorld(ch.leanN, 0.34, -0.28, 0.55), wRaise)
      .addScaledVector(new THREE.Vector3(rx + Tr.sweepX(t), 0.46, 0.66), wSweep)
      .addScaledVector(new THREE.Vector3(rx + 0.62, 0.52, 0.34), wDrop)
      .addScaledVector(localToWorld(ch.leanN, 0.88, -0.3, 0.22), wShrug)
      .addScaledVector(new THREE.Vector3(rx + 0.6, 0.09, 0.18), wSitH);
    const handRRot = new THREE.Euler(
      wSweep * 0.9 + wSitH * -0.4,
      0,
      -0.3 * wIdleR + wShrug * -1.9 + wRaise * 0.4,
    );

    const wBal = Tr.wBalanceL(t);
    const wIdleL = clamp(1 - wBal - wShrug - wSitH, 0, 1);
    handLT = V()
      .addScaledVector(handLT, wIdleL)
      .addScaledVector(localToWorld(ch.leanN, -0.85, 0.05, -0.15), wBal)
      .addScaledVector(localToWorld(ch.leanN, -0.88, -0.3, 0.22), wShrug)
      .addScaledVector(new THREE.Vector3(rx - 0.58, 0.09, 0.16), wSitH);
    const handLRot = new THREE.Euler(
      wSitH * -0.4,
      0,
      0.3 * wIdleL + wShrug * 1.9 - wBal * 0.6,
    );

    handR.position.copy(handRT);
    handR.rotation.copy(handRRot);
    handR.rotation.y = yaw * 0.6;
    handL.position.copy(handLT);
    handL.rotation.copy(handLRot);
    handL.rotation.y = yaw * 0.6;

    const armSlackR = Math.max(0, 0.62 - shR.distanceTo(handRT));
    const armSlackL = Math.max(0, 0.62 - shL.distanceTo(handLT));
    armRHose.update(
      shR,
      handRT,
      new THREE.Vector3(0.05, -armSlackR * 0.55 - 0.02, 0.02),
    );
    armLHose.update(
      shL,
      handLT,
      new THREE.Vector3(-0.05, -armSlackL * 0.55 - 0.02, 0.02),
    );

    stepperUpdate(t, rx, yaw, speed);
    const hipLW = V();
    const hipRW = V();
    ch.hipL.getWorldPosition(hipLW);
    ch.hipR.getWorldPosition(hipRW);

    const fL = footPos(feet[0], t);
    const fR = footPos(feet[1], t);
    const kickW = Tr.kickW(t);
    if (kickW > 0) {
      // one-leg deep peer: the right leg kicks back while facing left
      fR.lerp(new THREE.Vector3(rx + 0.55, 0.36, -0.18), kickW);
      fL.lerp(new THREE.Vector3(rx - 0.06, 0, 0.06), kickW * 0.8);
    }
    if (sitW > 0) {
      fL.lerp(new THREE.Vector3(rx - 0.48, 0.005, 0.52), sitW);
      fR.lerp(new THREE.Vector3(rx + 0.52, 0.005, 0.48), sitW);
    }

    footL.position.copy(fL).add(new THREE.Vector3(0, 0.003, 0));
    footR.position.copy(fR).add(new THREE.Vector3(0, 0.003, 0));
    const walkFootYaw = Math.abs(speed) > 0.1 ? yaw : yaw * 0.4;
    footL.rotation.set(0, walkFootYaw - sitW * 0.55, 0);
    footR.rotation.set(kickW * 0.7, walkFootYaw + sitW * 0.55, 0);
    footL.updateMatrixWorld();
    footR.updateMatrixWorld();
    const ankleL = footL.localToWorld(new THREE.Vector3(0, 0.075, -0.03));
    const ankleR = footR.localToWorld(new THREE.Vector3(0, 0.075, -0.03));

    const legSag = sitW > 0 ? 0.3 * sitW : 0;
    legLHose.update(
      hipLW,
      ankleL,
      new THREE.Vector3(
        -0.03 * (1 - sitW) - sitW * 0.12,
        -legSag * 0.5,
        sitW * 0.22,
      ),
    );
    legRHose.update(
      hipRW,
      ankleR,
      new THREE.Vector3(
        0.03 * (1 - sitW) + sitW * 0.12,
        -legSag * 0.5 + kickW * 0.12,
        sitW * 0.22,
      ),
    );

    magnifier.scale.setScalar(Math.max(0.001, Tr.glassScale(t)));
    if (t < DROP_T) {
      magnifier.position.copy(handRT); // held: follows the right hand at the grip
      magnifier.rotation.set(Tr.glassRotX(t), yaw * 0.5, Tr.glassRotZ(t));
    } else {
      magnifier.position.set(Tr.gFallX(t), Tr.gFallY(t), Tr.gFallZ(t));
      // rotY wiggles mid-tumble but returns to 0 so the lens plane ends dead flat
      const fallU = clamp((t - DROP_T) / 0.54, 0, 1);
      magnifier.rotation.set(
        Tr.gFallRX(t),
        Math.sin(fallU * Math.PI) * 0.3,
        Tr.gFallRZ(t),
      );
    }

    const ts = Tr.tearScale(t);
    const ty = Tr.tearY(t);
    let tearS = 1.25 * ts * (1 + Math.sin(tw * 9) * 0.06);
    if (ty < -0.01) tearS = 0.9; // detached, falling
    if (t >= 11.86) tearS = 0.001; // landed, merged into the puddle
    ch.tear.scale.setScalar(Math.max(0.001, tearS));
    ch.tear.position.set(0.045, 0.015 + ty, 0.22);
    ch.tear.visible = tearS > 0.01;
    const spS = Tr.splashS(t);
    splash.scale.set(spS * 1.15, spS * 0.8, 1); // oval puddle
    splash.material.opacity = Tr.splashO(t);
    splash.position.set(rx + 0.045, 0.006, 0.22);

    charBlob.position.set(rx, 0.004, 0.05);
    const airiness = clamp(
      (Tr.rootY(t) + bob - SIT_Y) / (STAND_Y - SIT_Y),
      0,
      1.4,
    );
    charBlob.scale.setScalar(lerp(1.35, 1.0, airiness * 0.7));
    charBlob.material.opacity = lerp(0.95, 0.72, airiness * 0.5) - bob * 2;
    if (t < DROP_T) {
      glassBlob.material.opacity = 0;
    } else {
      glassBlob.position.set(Tr.gFallX(t), 0.005, Tr.gFallZ(t));
      glassBlob.material.opacity = clamp(
        1 - (Tr.gFallY(t) - 0.075) * 2.5,
        0,
        0.9,
      );
    }

    par.x = lerp(par.x, par.tx, 0.04);
    par.y = lerp(par.y, par.ty, 0.04);
    camera.position.set(
      CAM_POS.x + Math.sin(tw * 0.4) * 0.05 + par.x * 0.22,
      CAM_POS.y + Math.sin(tw * 0.27) * 0.03 - par.y * 0.12,
      CAM_POS.z,
    );
    camera.lookAt(CAM_AIM);

    fadeEl.style.opacity = Tr.fade(t).toFixed(3);
  }

  /** The frame the timeline holds on, and all the reduced-motion path ever shows. */
  function renderSettled() {
    update(END, END);
    renderer.render(scene, camera);
  }

  function resize() {
    const w = host.clientWidth;
    const h = host.clientHeight;
    if (w === 0 || h === 0) return;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    // Sizing lives only here. setSize multiplies by the *stored* pixel ratio, so the
    // ratio has to be re-read here too, or the buffer keeps whatever density was in
    // force at mount.
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(w, h);
    // setSize reallocates the drawing buffer, so a new size arrives blank; with no loop
    // running there is nothing else to repaint it
    if (options.reducedMotion) renderSettled();
  }

  // A density change alone leaves the CSS box alone — the host is width-capped, so
  // ResizeObserver stays silent through a zoom or a move to a display of another
  // density. Watching the resolution query is what catches those.
  let ratioQuery: MediaQueryList | null = null;
  function onRatioChange() {
    watchPixelRatio();
    resize();
  }
  function watchPixelRatio() {
    ratioQuery?.removeEventListener("change", onRatioChange);
    ratioQuery = window.matchMedia(
      `(resolution: ${window.devicePixelRatio}dppx)`,
    );
    ratioQuery.addEventListener("change", onRatioChange);
  }

  resize();
  watchPixelRatio();
  const observer = new ResizeObserver(resize);
  observer.observe(host);

  const clock = new THREE.Clock();
  let rafId = 0;
  function frame() {
    rafId = requestAnimationFrame(frame);
    // The resolution query is the only density signal on the reduced-motion path, but
    // while the loop runs a direct comparison is cheaper than trusting it fired.
    if (renderer.getPixelRatio() !== Math.min(window.devicePixelRatio, 2))
      resize();
    const raw = clock.getElapsedTime();
    update(clamp(raw, 0, END), raw);
    renderer.render(scene, camera);
  }

  if (options.reducedMotion) renderSettled();
  else frame();

  return () => {
    cancelAnimationFrame(rafId);
    observer.disconnect();
    ratioQuery?.removeEventListener("change", onRatioChange);
    window.removeEventListener("pointermove", onPointerMove);
    scene.traverse((obj) => {
      if (!(obj instanceof THREE.Mesh)) return;
      obj.geometry.dispose();
      for (const m of Array.isArray(obj.material)
        ? obj.material
        : [obj.material]) {
        if (m instanceof THREE.MeshBasicMaterial && m.map) m.map.dispose();
        m.dispose();
      }
    });
    envTexture.dispose();
    pmrem.dispose();
    renderer.dispose();
    renderer.domElement.remove();
    vignette.remove();
    fadeEl.remove();
  };
}
