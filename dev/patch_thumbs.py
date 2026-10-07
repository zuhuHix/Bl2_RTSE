from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:80]!r} (found {text.count(old)})"
    return text.replace(old, new)


v = Path("rtse/web/viewer3d.js")
t = v.read_text(encoding="utf-8")

# 1. lift the slice-building out of the viewer so thumbnails can use it too
start = t.index("  // Part geometry: the slice of the shared mesh's triangle list, minus triangles on hidden bones.")
end = t.index("  function clearGroup() {")
t = t[:start] + '''  // Part geometry: the slice of the shared mesh's triangle list, minus triangles on hidden bones.
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

''' + t[end:]

helper = '''/** The triangles of one part: its slices of the shared index buffer, minus triangles on hidden bones. */
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

'''
t = replace_once(t, "const VIEWS = {", helper + "const VIEWS = {")

t = replace_once(
    t,
    '''      mesh.add(new THREE.Mesh(geometry, new THREE.ShaderMaterial({
        vertexShader: OUTLINE_VERTEX, fragmentShader: OUTLINE_FRAGMENT, side: THREE.BackSide, uniforms: { thickness: { value: thickness } },
      })));''',
    "      mesh.add(new THREE.Mesh(geometry, outlineMaterial(thickness)));",
)

t += '''
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

function renderThumb(model, meshes, color, width, height) {
  const geometries = meshes.map((m) => model.entry.parts[m] && sliceGeometry(model, model.entry.parts[m], new Set())).filter(Boolean);
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
  for (const { geometry } of geometries) {
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
  const dir = new THREE.Vector3(...VIEWS.angle).normalize();
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

/** A PNG data URL of one part (given the mesh names it draws), or null if we have no model for it. */
export function partThumbnail(meshes, color = "#c9a25a", width = 128, height = 80) {
  const key = `${meshes.join(",")}|${color}|${width}x${height}`;
  if (!thumbCache.has(key)) {
    const job = thumbQueue.then(async () => {
      try {
        const type = await modelKeyOfMesh(meshes[0]);
        if (!type) return null;
        const model = await loadModel(type);
        return renderThumb(model, meshes, color, width, height);
      } catch (error) {
        console.warn("thumbnail failed:", error);
        return null;
      }
    });
    thumbQueue = job.then(() => new Promise((r) => requestAnimationFrame(() => r()))); // yield between parts
    thumbCache.set(key, job);
  }
  return thumbCache.get(key);
}
'''
v.write_text(t, encoding="utf-8")
print("ok")
