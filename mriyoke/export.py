"""Per-iteration outputs: VTK (ParaView), y=0 slice PNG, and frames for a self-contained 3D HTML viewer."""
import base64, json, os
import numpy as np
from ngsolve import VTKOutput
from skimage import measure
from .config import Config


def write_vtk(mesh, ms, des, path):
    VTKOutput(mesh, coefs=[des.fe.psi, des.f.psi, ms.cr, ms.crf, ms.pol, ms.B, ms.normB],
              names=["psi", "psi_f", "iron", "ferrite", "polarity", "B", "normB"], filename=path, subdivision=0).Do()


def _grid(cfg: Config):
    h = cfg.viewer_h
    n = int(round(cfg.design_L / h)) + 1
    ax = np.arange(n) * h
    X, Y, Z = np.meshgrid(ax, ax, ax, indexing="ij")
    return ax, X, Y, Z


_mapped = {}


def sample_design(mesh, ms, des, cfg: Config):
    """Level-set-like fields on the viewer grid (1/8 model), negative inside the material:
    fe = iron, fp = ferrite magnetized +z, fn = ferrite magnetized -z. Ferrite wins overlaps."""
    ax, X, Y, Z = _grid(cfg)
    if id(mesh) not in _mapped:                                # point location is the slow part: do it once
        pts = np.stack([X.ravel(), Y.ravel(), Z.ravel()], 1) * (1 - 1e-9) + 1e-6
        _mapped[id(mesh)] = mesh(pts[:, 0], pts[:, 1], pts[:, 2])
    mp = _mapped[id(mesh)]
    ev = lambda cf: np.asarray(cf(mp)).ravel().reshape(X.shape)
    psi, psi_f, pol = ev(des.fe.psi), ev(des.f.psi), ev(ms.pol)
    out = dict(fe=np.maximum(psi, -psi_f), fp=np.where(pol > 0, psi_f, 1.0), fn=np.where(pol > 0, 1.0, psi_f))
    keep_out = (X <= cfg.env_x / 2) & (Y <= cfg.env_y / 2) & (Z <= cfg.env_z / 2)
    for v in out.values():
        v[keep_out] = 1.0
        v[-1, :, :] = v[:, -1, :] = v[:, :, -1] = 1.0          # close surface at design-box faces
    return out


def material_mask(fields):
    """int8 grid: 0 air, 1 iron, 2 ferrite +z, 3 ferrite -z."""
    m = np.zeros(fields["fe"].shape, dtype=np.int8)
    m[fields["fe"] < 0] = 1; m[fields["fp"] < 0] = 2; m[fields["fn"] < 0] = 3
    return m


def mirror8(v):
    for axis in range(3):
        v = np.concatenate([np.flip(np.take(v, np.arange(1, v.shape[axis]), axis=axis), axis=axis), v], axis=axis)
    return v


def surface(vals, h, cfg: Config, level=0.0):
    """Marching-cubes surface of {vals < level} mirrored to the full magnet, packed for the viewer.
    vals on a 1/8-model grid with spacing h whose first node lies on the symmetry planes."""
    full = mirror8(vals)
    if full.min() >= level:
        return dict(pos="", idx="", ntri=0)
    verts, faces, _, _ = measure.marching_cubes(full, level=level, spacing=(h, h, h))
    verts = verts - (vals.shape[0] - 1) * h
    D = cfg.design_L
    q = np.clip(np.round((verts + D) / (2 * D) * 65535), 0, 65535).astype(np.uint16)
    return dict(pos=base64.b64encode(q.tobytes()).decode(), idx=base64.b64encode(faces.astype(np.uint32).tobytes()).decode(),
                ntri=int(len(faces)))


def frame_from_fields(fields, cfg: Config):
    return {k: surface(v, cfg.viewer_h, cfg) for k, v in fields.items()}


VIEWER_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>MRI magnet iterations</title>
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
// static: patient/bed envelope, design box, axes
scene.add(new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(DATA.env[0], DATA.env[1], DATA.env[2])), new THREE.LineBasicMaterial({color:0x4080ff})));
scene.add(new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(2*D,2*D,2*D)), new THREE.LineBasicMaterial({color:0x555555})));
scene.add(new THREE.AxesHelper(D*1.3));
// iron grey, ferrite +z red, ferrite -z blue, largest green component translucent green
const mats = {fe: new THREE.MeshPhongMaterial({color:0x9a9a9a, side:THREE.DoubleSide}), fp: new THREE.MeshPhongMaterial({color:0xd04040, side:THREE.DoubleSide}),
  fn: new THREE.MeshPhongMaterial({color:0x4060d0, side:THREE.DoubleSide}), gr: new THREE.MeshPhongMaterial({color:0x30d060, side:THREE.DoubleSide, transparent:true, opacity:.45, depthWrite:false})};
let shown = [];
function b64(s, T){ const b = atob(s); const u = new Uint8Array(b.length); for (let i=0;i<b.length;i++) u[i]=b.charCodeAt(i); return new T(u.buffer); }
function show(i){
  const f = DATA.frames[i];
  for (const o of shown) { scene.remove(o); o.geometry.dispose(); }
  shown = [];
  for (const k in mats) {
    const m = f.meshes[k];
    if (!m || m.ntri == 0) continue;
    const q = b64(m.pos, Uint16Array); const pos = new Float32Array(q.length);
    for (let j=0;j<q.length;j++) pos[j] = q[j]/65535*2*D - D;
    const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(pos,3)); g.setIndex(new THREE.BufferAttribute(b64(m.idx, Uint32Array),1)); g.computeVertexNormals();
    const o = new THREE.Mesh(g, mats[k]); scene.add(o); shown.push(o);
  }
  document.getElementById('stats').textContent = f.label;
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
    """frames: list of dict(it, label, meshes={fe, fp, fn[, gr]: surface(...)})."""
    data = dict(D=cfg.design_L, env=[cfg.env_x, cfg.env_y, cfg.env_z], frames=frames)
    with open(path, "w") as f:
        f.write(VIEWER_HTML.replace("__DATA__", json.dumps(data)))


def write_slice_png(mesh, ms, des, cfg: Config, path, title=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n = cfg.slice_res
    D = cfg.design_L
    ax_ = np.linspace(1e-6, D * (1 - 1e-6), n)
    X, Z = np.meshgrid(ax_, ax_, indexing="ij")
    mp = mesh(X.ravel(), 1e-6 + 0 * X.ravel(), Z.ravel())
    ev = lambda cf: np.asarray(cf(mp)).ravel().reshape(X.shape)
    full = lambda a: np.concatenate([np.flip(np.concatenate([np.flip(a[1:], 0), a], 0)[:, 1:], 1), np.concatenate([np.flip(a[1:], 0), a], 0)], 1)
    nBf, crf, ferf = full(ev(ms.normB) * 1e3), full(ev(ms.cr)), full(ev(ms.crf) * ev(ms.pol))
    ext = [-D, D, -D, D]
    fig, axs = plt.subplots(1, 3, figsize=(17, 5.5))
    im = axs[0].imshow(nBf.T, origin="lower", extent=ext, cmap="viridis", vmin=0, vmax=max(cfg.B0 * 1e3 * 2, 1))
    axs[0].set_title("|B| [mT], plane y=0"); plt.colorbar(im, ax=axs[0])
    axs[1].imshow(crf.T, origin="lower", extent=ext, cmap="Greys", vmin=0, vmax=1)
    axs[1].set_title("iron fraction, plane y=0")
    axs[2].imshow(ferf.T, origin="lower", extent=ext, cmap="bwr", vmin=-1, vmax=1)
    axs[2].set_title("ferrite fraction x polarity (red +z, blue -z), plane y=0")
    for a in axs:
        a.add_patch(plt.Rectangle((-cfg.env_x / 2, -cfg.env_z / 2), cfg.env_x, cfg.env_z, fill=False, ec="deepskyblue", lw=1.5))
        a.add_patch(plt.Circle((0, 0), cfg.dsv_radius, fill=False, ec="deepskyblue", lw=1.0, ls="--"))
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
    axs[1, 0].plot(it, [h["iron_kg"] for h in hist], "o-", label="iron"); axs[1, 0].plot(it, [h["ferrite_kg"] for h in hist], "s-", label="ferrite")
    axs[1, 0].legend(); axs[1, 0].set_title("mass, full magnet [kg]")
    axs[1, 1].semilogy(it, [h["J"] for h in hist], "o-", label="J"); axs[1, 1].semilogy(it, [h["w"] for h in hist], "s--", label="w"); axs[1, 1].semilogy(it, [h["f"] for h in hist], "^-", label="misfit f"); axs[1, 1].legend(); axs[1, 1].set_title("objective J = w f + c + demag penalty / penalty weight / misfit")
    for a in axs.ravel():
        a.set_xlabel("iteration"); a.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
