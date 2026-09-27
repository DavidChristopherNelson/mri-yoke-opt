"""Per-iteration outputs: VTK (ParaView), y=0 slice PNG, and frames for a self-contained 3D HTML viewer."""
import base64, json, os
import numpy as np
from ngsolve import VTKOutput
from skimage import measure
from .config import Config


def write_vtk(mesh, ms, ls, path):
    VTKOutput(mesh, coefs=[ls.psi, ms.cr, ms.B, ms.normB], names=["psi", "iron", "B", "normB"],
              filename=path, subdivision=0).Do()


def _grid(cfg: Config):
    h = cfg.viewer_h
    n = int(round(cfg.design_L / h)) + 1
    ax = np.arange(n) * h
    X, Y, Z = np.meshgrid(ax, ax, ax, indexing="ij")
    return ax, X, Y, Z


def _nondesign_mask(cfg: Config, X, Y, Z):
    rc = cfg.dsv_radius + cfg.clearance
    in_sphere = X ** 2 + Y ** 2 + Z ** 2 < rc ** 2
    in_mag = (X <= cfg.mag_x / 2) & (Y <= cfg.mag_y / 2) & (Z >= cfg.z_mag0) & (Z <= cfg.z_mag1)
    return in_sphere | in_mag


def sample_psi(mesh, psi, cfg: Config):
    ax, X, Y, Z = _grid(cfg)
    pts = np.stack([X.ravel(), Y.ravel(), Z.ravel()], 1) * (1 - 1e-9) + 1e-6
    vals = np.asarray(psi(mesh(pts[:, 0], pts[:, 1], pts[:, 2]))).ravel().reshape(X.shape)
    vals[_nondesign_mask(cfg, X, Y, Z)] = 1.0
    vals[-1, :, :] = vals[:, -1, :] = vals[:, :, -1] = 1.0   # close surface at design-box faces
    return vals


def mirror8(v):
    for axis in range(3):
        v = np.concatenate([np.flip(np.take(v, np.arange(1, v.shape[axis]), axis=axis), axis=axis), v], axis=axis)
    return v


def frame_from_psi(vals, cfg: Config):
    full = mirror8(vals)
    h = cfg.viewer_h
    x0 = -(vals.shape[0] - 1) * h
    if full.min() >= 0:
        return dict(pos="", idx="", ntri=0)
    verts, faces, _, _ = measure.marching_cubes(full, level=0.0, spacing=(h, h, h))
    verts = verts + x0
    D = cfg.design_L
    q = np.clip(np.round((verts + D) / (2 * D) * 65535), 0, 65535).astype(np.uint16)
    return dict(pos=base64.b64encode(q.tobytes()).decode(), idx=base64.b64encode(faces.astype(np.uint32).tobytes()).decode(),
                ntri=int(len(faces)))


VIEWER_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>MRI yoke iterations</title>
<style>body{margin:0;font-family:system-ui,sans-serif;background:#111;color:#ddd}#ui{position:absolute;top:0;left:0;right:0;padding:8px 12px;background:rgba(0,0,0,.6);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
input[type=range]{flex:1;min-width:200px}#stats{font-size:13px;white-space:pre}button{background:#333;color:#ddd;border:1px solid #666;padding:4px 8px}</style></head>
<body><div id="ui"><button id="play">play</button><input type="range" id="sl" min="0" max="0" value="0"><span id="stats"></span></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
const DATA = __DATA__;
const D = DATA.D;
const scene = new THREE.Scene(); scene.background = new THREE.Color(0x111111);
const cam = new THREE.PerspectiveCamera(45, innerWidth/innerHeight, 0.01, 100); cam.position.set(2.2*D, -2.6*D, 1.6*D); cam.up.set(0,0,1); cam.lookAt(0,0,0);
const ren = new THREE.WebGLRenderer({antialias:true}); ren.setSize(innerWidth, innerHeight); document.body.appendChild(ren.domElement);
scene.add(new THREE.AmbientLight(0xffffff, .45)); const dl = new THREE.DirectionalLight(0xffffff, .9); dl.position.set(1,-2,3); scene.add(dl);
const dl2 = new THREE.DirectionalLight(0xffffff, .4); dl2.position.set(-2,1,-1); scene.add(dl2);
// static: magnets, DSV, design box, axes
const magGeo = new THREE.BoxGeometry(DATA.mag[0], DATA.mag[1], DATA.mag[2]);
for (const s of [1,-1]) { const m = new THREE.Mesh(magGeo, new THREE.MeshPhongMaterial({color:0xd04040})); m.position.set(0,0,s*DATA.mag[3]); scene.add(m); }
scene.add(new THREE.Mesh(new THREE.SphereGeometry(DATA.r, 48, 32), new THREE.MeshPhongMaterial({color:0x4080ff, transparent:true, opacity:.35})));
scene.add(new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(2*D,2*D,2*D)), new THREE.LineBasicMaterial({color:0x555555})));
scene.add(new THREE.AxesHelper(D*1.3));
const ironMat = new THREE.MeshPhongMaterial({color:0x9a9a9a, side:THREE.DoubleSide, flatShading:false});
let iron = null;
function b64(s, T){ const b = atob(s); const u = new Uint8Array(b.length); for (let i=0;i<b.length;i++) u[i]=b.charCodeAt(i); return new T(u.buffer); }
function show(i){
  const f = DATA.frames[i];
  if (iron) { scene.remove(iron); iron.geometry.dispose(); }
  if (f.ntri > 0) {
    const q = b64(f.pos, Uint16Array); const pos = new Float32Array(q.length);
    for (let k=0;k<q.length;k++) pos[k] = q[k]/65535*2*D - D;
    const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(pos,3)); g.setIndex(new THREE.BufferAttribute(b64(f.idx, Uint32Array),1)); g.computeVertexNormals();
    iron = new THREE.Mesh(g, ironMat); scene.add(iron);
  } else iron = null;
  const s = f.stats;
  document.getElementById('stats').textContent = `iter ${f.it}   mean B ${(s.mean_B*1e3).toFixed(2)} mT   ppm ${s.ppm.toFixed(0)}   iron ${s.iron_kg.toFixed(1)} kg   $${s.cost.toFixed(0)}   J ${s.J.toExponential(3)}   w ${s.w.toExponential(1)}   kappa ${s.kappa.toFixed(3)}`;
}
const sl = document.getElementById('sl'); sl.max = DATA.frames.length-1; sl.oninput = () => show(+sl.value);
let playing=false, t0=0; document.getElementById('play').onclick = () => { playing=!playing; };
// minimal orbit control
let drag=false, px=0, py=0, theta=Math.atan2(cam.position.y, cam.position.x), phi=Math.acos(cam.position.z/cam.position.length()), rad=cam.position.length();
function place(){ cam.position.set(rad*Math.sin(phi)*Math.cos(theta), rad*Math.sin(phi)*Math.sin(theta), rad*Math.cos(phi)); cam.lookAt(0,0,0); }
ren.domElement.onmousedown = e => { drag=true; px=e.clientX; py=e.clientY; };
window.onmouseup = () => drag=false;
window.onmousemove = e => { if(!drag) return; theta -= (e.clientX-px)*0.01; phi = Math.min(Math.PI-0.05, Math.max(0.05, phi - (e.clientY-py)*0.01)); px=e.clientX; py=e.clientY; place(); };
ren.domElement.onwheel = e => { rad *= Math.exp(e.deltaY*0.001); place(); e.preventDefault(); };
window.onresize = () => { cam.aspect = innerWidth/innerHeight; cam.updateProjectionMatrix(); ren.setSize(innerWidth, innerHeight); };
function loop(t){ requestAnimationFrame(loop); if (playing && t - t0 > 400) { t0 = t; sl.value = (+sl.value + 1) % DATA.frames.length; show(+sl.value); } ren.render(scene, cam); }
show(0); loop(0);
</script></body></html>
"""


def write_viewer(frames, cfg: Config, path):
    data = dict(D=cfg.design_L, r=cfg.dsv_radius, mag=[cfg.mag_x, cfg.mag_y, cfg.mag_t, (cfg.z_mag0 + cfg.z_mag1) / 2],
                frames=frames)
    with open(path, "w") as f:
        f.write(VIEWER_HTML.replace("__DATA__", json.dumps(data)))


def write_slice_png(mesh, ms, ls, cfg: Config, path, title=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n = cfg.slice_res
    D = cfg.design_L
    ax_ = np.linspace(1e-6, D * (1 - 1e-6), n)
    X, Z = np.meshgrid(ax_, ax_, indexing="ij")
    mp = mesh(X.ravel(), 1e-6 + 0 * X.ravel(), Z.ravel())
    nB = np.asarray(ms.normB(mp)).ravel().reshape(X.shape) * 1e3
    cr = np.asarray(ms.cr(mp)).ravel().reshape(X.shape)
    # mirror to full x-z plane
    nBf = np.concatenate([np.flip(nB[1:], 0), nB], 0); nBf = np.concatenate([np.flip(nBf[:, 1:], 1), nBf], 1)
    crf = np.concatenate([np.flip(cr[1:], 0), cr], 0); crf = np.concatenate([np.flip(crf[:, 1:], 1), crf], 1)
    ext = [-D, D, -D, D]
    fig, axs = plt.subplots(1, 2, figsize=(12, 5.5))
    im = axs[0].imshow(nBf.T, origin="lower", extent=ext, cmap="viridis", vmin=0, vmax=max(cfg.B0 * 1e3 * 2, 1))
    axs[0].set_title("|B| [mT], plane y=0"); plt.colorbar(im, ax=axs[0])
    axs[1].imshow(crf.T, origin="lower", extent=ext, cmap="Greys", vmin=0, vmax=1)
    axs[1].set_title("iron fraction, plane y=0")
    for a in axs:
        for s in (1, -1):
            a.add_patch(plt.Rectangle((-cfg.mag_x / 2, min(s * cfg.z_mag0, s * cfg.z_mag1)), cfg.mag_x, cfg.mag_t, fill=False, ec="red", lw=1.5))
        a.add_patch(plt.Circle((0, 0), cfg.dsv_radius, fill=False, ec="deepskyblue", lw=1.5))
        a.set_xlabel("x [m]"); a.set_ylabel("z [m]")
    fig.suptitle(title); fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def write_history_png(hist, cfg: Config, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    it = [h["it"] for h in hist]
    fig, axs = plt.subplots(2, 2, figsize=(11, 7))
    axs[0, 0].semilogy(it, [h["ppm"] for h in hist], "o-"); axs[0, 0].axhline(cfg.ppm_max, c="r", ls="--"); axs[0, 0].set_title("(max-min)/mean [ppm]")
    axs[0, 1].plot(it, [h["mean_B"] * 1e3 for h in hist], "o-"); axs[0, 1].axhline(cfg.B0 * 1e3, c="r", ls="--"); axs[0, 1].set_title("mean |B| on DSV surface [mT]")
    axs[1, 0].plot(it, [h["iron_kg"] for h in hist], "o-"); axs[1, 0].set_title("iron mass, full magnet [kg]")
    axs[1, 1].semilogy(it, [h["J"] for h in hist], "o-", label="J"); axs[1, 1].semilogy(it, [h["w"] for h in hist], "s--", label="w"); axs[1, 1].legend(); axs[1, 1].set_title("objective / penalty weight")
    for a in axs.ravel():
        a.set_xlabel("iteration"); a.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
