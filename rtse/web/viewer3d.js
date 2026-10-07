// 3D gun viewer. Every gun type is one shared "gestalt" mesh holding all its parts; the game draws a
// part by showing a slice of that mesh's triangle list. models/parts.json says which slice is which
// part, so a gun is built by drawing only the slices its parts name. Cel-shaded with ink outlines.

import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

let manifestPromise = null;
const modelCache = new Map(); // type key -> Promise<{ prims, jointNames }>

function manifest() {
  manifestPromise ||= fetch("models/parts.json").then((r) => {
    if (!r.ok) throw new Error("models/parts.json missing");
    return r.json();
  });
  return manifestPromise;
}

export async function hasModel(typeKey) {
  try {
    return Boolean((await manifest())[typeKey]);
  } catch {
    return false;
  }
}

async function loadModel(typeKey) {
  if (!modelCache.has(typeKey)) {
    modelCache.set(typeKey, (async () => {
      const entry = (await manifest())[typeKey];
      const gltf = await new GLTFLoader().loadAsync(`models/${entry.file}`);
      const prims = [];
      let jointNames = [];
      gltf.scene.traverse((o) => {
        if (o.isSkinnedMesh) jointNames = o.skeleton.bones.map((b) => b.name);
        if (o.isMesh || o.isSkinnedMesh) prims.push(o.geometry);
      });
      return { entry, prims, jointNames };
    })());
  }
  return modelCache.get(typeKey);
}

function toonGradient() {
  const data = new Uint8Array([70, 135, 200, 255]);
  const texture = new THREE.DataTexture(data, data.length, 1, THREE.RedFormat);
  texture.minFilter = texture.magFilter = THREE.NearestFilter;
  texture.needsUpdate = true;
  return texture;
}

const OUTLINE_VERTEX = `
  uniform float thickness;
  void main() {
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position + normal * thickness, 1.0);
  }`;
const OUTLINE_FRAGMENT = "void main() { gl_FragColor = vec4(0.043, 0.039, 0.035, 1.0); }";

/** The triangles of one part: its slices of the shared index buffer, minus triangles on hidden bones. */
function sliceGeometry(model, part, hidden) {
  // Primitives are laid out one after another, so work out which primitive each slice lands in.
  const slicesByPrim = new Map();
  for (const [start, tris] of part.slices) {
    let first = start;
    let which = 0;
    while (which < model.prims.length - 1 && first >= model.prims[which].index.count) {
      first -= model.prims[which].index.count;
      which++;
    }
    if (!slicesByPrim.has(which)) slicesByPrim.set(which, []);
    slicesByPrim.get(which).push(model.prims[which].index.array.slice(first, first + tris * 3));
  }
  // One draw per primitive keeps vertex buffers shared; a part spanning primitives is rare.
  const [which, chunks] = [...slicesByPrim.entries()].sort((a, b) => b[1].length - a[1].length)[0];
  const source = model.prims[which];
  const total = chunks.reduce((n, c) => n + c.length, 0);
  let indices = new chunks[0].constructor(total);
  let at = 0;
  for (const chunk of chunks) { indices.set(chunk, at); at += chunk.length; }

  if (hidden.size && source.getAttribute("skinIndex")) {
    const skinIndex = source.getAttribute("skinIndex");
    const skinWeight = source.getAttribute("skinWeight");
    const hiddenJoints = new Set(model.jointNames.map((n, i) => (hidden.has(n) ? i : -1)).filter((i) => i >= 0));
    const dominant = (v) => {
      let best = 0;
      let bestWeight = -1;
      for (let k = 0; k < 4; k++) {
        const weight = [skinWeight.getX, skinWeight.getY, skinWeight.getZ, skinWeight.getW][k].call(skinWeight, v);
        if (weight > bestWeight) { bestWeight = weight; best = [skinIndex.getX, skinIndex.getY, skinIndex.getZ, skinIndex.getW][k].call(skinIndex, v); }
      }
      return best;
    };
    const kept = [];
    for (let t = 0; t < indices.length; t += 3) {
      if (![0, 1, 2].some((c) => hiddenJoints.has(dominant(indices[t + c])))) kept.push(indices[t], indices[t + 1], indices[t + 2]);
    }
    indices = new indices.constructor(kept);
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", source.getAttribute("position"));
  if (source.getAttribute("normal")) geometry.setAttribute("normal", source.getAttribute("normal"));
  geometry.setIndex(new THREE.BufferAttribute(indices, 1));

  const box = new THREE.Box3();
  const v = new THREE.Vector3();
  const position = source.getAttribute("position");
  for (let i = 0; i < indices.length; i++) box.expandByPoint(v.fromBufferAttribute(position, indices[i]));
  return { geometry, box };
}

function outlineMaterial(thickness) {
  return new THREE.ShaderMaterial({
    vertexShader: OUTLINE_VERTEX, fragmentShader: OUTLINE_FRAGMENT, side: THREE.BackSide, uniforms: { thickness: { value: thickness } },
  });
}

const VIEWS = { side: [1, 0.05, 0], angle: [1, 0.55, 0.85], top: [0.001, 1, 0], front: [0.001, 0.05, 1] };

export function createViewer(host, { onPick = () => {}, onHover = () => {} } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.domElement.className = "stage-canvas";
  host.append(renderer.domElement);

  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x6a5a40, 1.35));
  const sun = new THREE.DirectionalLight(0xffffff, 2.1);
  sun.position.set(2, 3, 2.5);
  scene.add(sun);

  const camera = new THREE.PerspectiveCamera(28, 1, 0.01, 100);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enablePan = false;
  controls.minPolarAngle = 0.15;
  controls.maxPolarAngle = Math.PI - 0.15;

  const gradient = toonGradient();
  const group = new THREE.Group();
  scene.add(group);

  let typeKey = null;
  let hidden = new Set();
  let build = []; // [{ slot, mesh, color }]
  let preview = null; // { slot, pieces }
  let hotSlot = null;
  let frame = { center: new THREE.Vector3(), size: 1 };
  let fittedType = null;
  let dirty = false; // true while a frame is already scheduled
  let buildToken = 0;
  let report = { drawn: 0, missing: [], box: null };

  const geometryCache = new Map();
  const bboxCache = new WeakMap();

  function request() {
    if (dirty) return;
    dirty = true;
    requestAnimationFrame(render);
  }

  function render() {
    dirty = false;
    renderer.render(scene, camera);
  }

  function resize() {
    const w = host.clientWidth || 1;
    const h = host.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    request();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(host);
  controls.addEventListener("change", request);

  // Part geometry: the slice of the shared mesh's triangle list, minus triangles on hidden bones.
  function geometryFor(model, meshName) {
    const hiddenKey = [...hidden].sort().join("|");
    const key = `${typeKey}/${meshName}/${hiddenKey}`;
    if (geometryCache.has(key)) return geometryCache.get(key);
    const part = model.entry.parts[meshName];
    const built = part ? sliceGeometry(model, part, hidden) : null;
    if (built) bboxCache.set(built.geometry, built.box);
    geometryCache.set(key, built && built.geometry);
    return built && built.geometry;
  }

  function clearGroup() {
    for (const child of [...group.children]) {
      group.remove(child);
      child.traverse((o) => { if (o.material) o.material.dispose(); });
    }
  }

  async function rebuild() {
    const token = ++buildToken;
    if (!typeKey) return;
    const model = await loadModel(typeKey);
    if (token !== buildToken) return;

    clearGroup();
    const pieces = preview ? [...build.filter((p) => p.slot !== preview.slot), ...preview.pieces] : build;
    const box = new THREE.Box3();
    const drawn = [];
    const missing = [];
    for (const piece of pieces) {
      const geometry = geometryFor(model, piece.mesh);
      if (!geometry) { missing.push(piece.mesh); continue; }
      drawn.push({ piece, geometry });
      box.union(bboxCache.get(geometry));
    }
    report = { drawn: drawn.length, missing, box: box.isEmpty() ? null : box.getSize(new THREE.Vector3()).toArray().map((n) => +n.toFixed(3)) };
    if (box.isEmpty()) { request(); return; }

    const size = box.getSize(new THREE.Vector3());
    const thickness = Math.max(size.x, size.y, size.z) * 0.012;
    for (const { piece, geometry } of drawn) {
      const material = new THREE.MeshToonMaterial({ color: piece.color, gradientMap: gradient, side: THREE.DoubleSide });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.userData = { slot: piece.slot, mesh: piece.mesh };
      mesh.add(new THREE.Mesh(geometry, outlineMaterial(thickness)));
      group.add(mesh);
    }
    applyHighlight();

    // Only re-frame when the gun type changes, so swapping a part never moves the camera you set.
    if (fittedType !== typeKey) {
      frame = { center: box.getCenter(new THREE.Vector3()), size: Math.max(size.x, size.y, size.z) };
      fittedType = typeKey;
      setView("angle");
    }
    request();
  }

  function applyHighlight() {
    for (const mesh of group.children) {
      mesh.material.emissive?.setHex(mesh.userData.slot === hotSlot ? 0x4a3a00 : 0x000000);
    }
    request();
  }

  function setView(name) {
    const dir = new THREE.Vector3(...(VIEWS[name] || VIEWS.angle)).normalize();
    const distance = frame.size * 1.85;
    controls.target.copy(frame.center);
    camera.position.copy(frame.center).addScaledVector(dir, distance);
    controls.minDistance = frame.size * 0.7;
    controls.maxDistance = frame.size * 5;
    camera.near = frame.size * 0.05;
    camera.far = frame.size * 40;
    camera.updateProjectionMatrix();
    controls.update();
    request();
  }

  // Click a part to pick its slot; hover to highlight it. A drag (orbit) is not a click.
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let downAt = null;
  function slotAt(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(group.children, false)[0];
    return hit ? hit.object.userData.slot : null;
  }
  renderer.domElement.addEventListener("pointerdown", (e) => { downAt = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener("pointerup", (e) => {
    if (downAt && Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) < 5) {
      const slot = slotAt(e);
      if (slot) onPick(slot);
    }
    downAt = null;
  });
  renderer.domElement.addEventListener("pointermove", (e) => {
    if (e.buttons) return;
    const slot = slotAt(e);
    renderer.domElement.style.cursor = slot ? "pointer" : "grab";
    onHover(slot);
  });
  renderer.domElement.addEventListener("pointerleave", () => onHover(null));

  resize();
  return {
    canvas: renderer.domElement,
    /** pieces: [{ slot, mesh, color }]; hideBones: bone names the game hides on the mesh. */
    async setBuild(newType, pieces, hideBones = []) {
      typeKey = newType;
      hidden = new Set(hideBones);
      build = pieces;
      await rebuild();
      return report;
    },
    /** Temporarily swaps one slot's pieces (used when hovering a candidate part). */
    async setPreview(slot, pieces) {
      preview = pieces ? { slot, pieces: pieces.map((p) => ({ ...p, slot })) } : null;
      await rebuild();
    },
    highlight(slot) { hotSlot = slot; applyHighlight(); },
    setView,
    resize,
    dispose() {
      observer.disconnect();
      clearGroup();
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}

// ---------- part thumbnails ----------
// One small offscreen renderer draws each part on its own and hands back a PNG, one at a time.

let thumbRenderer = null;
const thumbCache = new Map(); // "mesh,mesh|colour|size" -> Promise<dataURL | null>
let thumbQueue = Promise.resolve();
let meshOwners = null; // mesh name -> model key

async function modelKeyOfMesh(mesh) {
  if (!meshOwners) {
    meshOwners = new Map();
    for (const [key, entry] of Object.entries(await manifest())) {
      for (const name of Object.keys(entry.parts || {})) meshOwners.set(name, key);
    }
  }
  return meshOwners.get(mesh) || null;
}

function renderThumb(model, pieces, hidden, view, width, height) {
  const geometries = pieces
    .map((p) => model.entry.parts[p.mesh] && { ...sliceGeometry(model, model.entry.parts[p.mesh], hidden), color: p.color })
    .filter((g) => g && g.geometry);
  if (!geometries.length) return null;

  if (!thumbRenderer) {
    thumbRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    thumbRenderer.setPixelRatio(2);
  }
  thumbRenderer.setSize(width, height, false);

  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x6a5a40, 1.35));
  const sun = new THREE.DirectionalLight(0xffffff, 2.1);
  sun.position.set(2, 3, 2.5);
  scene.add(sun);

  const box = new THREE.Box3();
  for (const { box: b } of geometries) box.union(b);
  const size = box.getSize(new THREE.Vector3());
  const thickness = Math.max(size.x, size.y, size.z) * 0.014;
  const gradient = toonGradient();
  const materials = [];
  for (const { geometry, color } of geometries) {
    const material = new THREE.MeshToonMaterial({ color, gradientMap: gradient, side: THREE.DoubleSide });
    const outline = outlineMaterial(thickness);
    materials.push(material, outline);
    const mesh = new THREE.Mesh(geometry, material);
    mesh.add(new THREE.Mesh(geometry, outline));
    scene.add(mesh);
  }

  // Look at the part from a three-quarter angle and back the camera off until its box fills the frame.
  const camera = new THREE.PerspectiveCamera(28, width / height, 0.001, 1000);
  const center = box.getCenter(new THREE.Vector3());
  const dir = new THREE.Vector3(...(VIEWS[view] || VIEWS.angle)).normalize();
  const corners = [];
  for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) corners.push(new THREE.Vector3(x, y, z));
  let distance = Math.max(size.x, size.y, size.z) * 3;
  for (let pass = 0; pass < 4; pass++) {
    camera.position.copy(center).addScaledVector(dir, distance);
    camera.lookAt(center);
    camera.updateMatrixWorld();
    camera.updateProjectionMatrix();
    let extent = 0;
    for (const corner of corners) {
      const ndc = corner.clone().project(camera);
      extent = Math.max(extent, Math.abs(ndc.x), Math.abs(ndc.y));
    }
    distance *= extent / 0.9;
  }
  camera.position.copy(center).addScaledVector(dir, distance);
  camera.lookAt(center);
  camera.updateMatrixWorld();

  thumbRenderer.render(scene, camera);
  const url = thumbRenderer.domElement.toDataURL("image/png");
  for (const material of materials) material.dispose();
  for (const { geometry } of geometries) geometry.dispose();
  gradient.dispose();
  return url;
}

function queueThumb(key, build) {
  if (!thumbCache.has(key)) {
    const job = thumbQueue.then(async () => {
      try {
        return await build();
      } catch (error) {
        console.warn("thumbnail failed:", error);
        return null;
      }
    });
    thumbQueue = job.then(() => new Promise((r) => requestAnimationFrame(() => r()))); // yield between renders
    thumbCache.set(key, job);
  }
  return thumbCache.get(key);
}

/** A PNG data URL of one part (given the mesh names it draws), or null if we have no model for it. */
export function partThumbnail(meshes, color = "#c9a25a", width = 128, height = 80) {
  return queueThumb(`part|${meshes.join(",")}|${color}|${width}x${height}`, async () => {
    const type = await modelKeyOfMesh(meshes[0]);
    if (!type) return null;
    return renderThumb(await loadModel(type), meshes.map((mesh) => ({ mesh, color })), new Set(), "angle", width, height);
  });
}

/** A PNG data URL of a whole gun: pieces are [{ mesh, color }], hideBones as in setBuild. */
export function buildThumbnail(typeKey, pieces, hideBones = [], view = "side", width = 96, height = 56) {
  const signature = pieces.map((p) => `${p.mesh}:${p.color}`).join(",");
  return queueThumb(`build|${typeKey}|${signature}|${hideBones.join(",")}|${view}|${width}x${height}`, async () => {
    if (!(await hasModel(typeKey))) return null;
    return renderThumb(await loadModel(typeKey), pieces, new Set(hideBones), view, width, height);
  });
}
