"""Per-iteration outputs: VTK (ParaView), y=0 slice PNG, and frames for a self-contained 3D HTML viewer."""
import base64, json, os
import numpy as np
from ngsolve import VTKOutput
from skimage import measure
from .config import Config


def write_vtk(mesh, ms, des, path):
    VTKOutput(mesh, coefs=[des.fe.psi, des.f.psi, ms.cr, ms.crf, ms.m_cf, ms.B, ms.normB],
              names=["psi", "psi_f", "iron", "ferrite", "m", "B", "normB"], filename=path, subdivision=0).Do()


def _grid(cfg: Config):
    h = cfg.viewer_h
    n = int(round(cfg.design_L / h)) + 1
    ax = np.arange(n) * h
    X, Y, Z = np.meshgrid(ax, ax, ax, indexing="ij")
    return ax, X, Y, Z


_mapped = {}


def keep_out(cfg: Config, X, Y, Z):
    foot = (X <= cfg.env_x / 2) & (Z <= cfg.env_z / 2)
    return foot & ((Y <= cfg.env_y / 2) | cfg.corridor)


def sample_design(mesh, ms, des, cfg: Config):
    """Level-set-like fields on the viewer grid (1/8 model), negative inside the material: fe = iron, ff = ferrite
    (ferrite wins overlaps), plus m = magnetization direction (3, grid) sampled on the same grid."""
    ax, X, Y, Z = _grid(cfg)
    if id(mesh) not in _mapped:                                # point location is the slow part: do it once
        pts = np.stack([X.ravel(), Y.ravel(), Z.ravel()], 1) * (1 - 1e-9) + 1e-6
        _mapped[id(mesh)] = mesh(pts[:, 0], pts[:, 1], pts[:, 2])
    mp = _mapped[id(mesh)]
    ev = lambda cf: np.asarray(cf(mp)).ravel().reshape(X.shape)
    psi, psi_f = ev(des.fe.psi), ev(des.f.psi)
    out = dict(fe=np.maximum(psi, -psi_f), ff=psi_f.copy())
    ko = keep_out(cfg, X, Y, Z)
    for v in out.values():
        v[ko] = 1.0
        v[-1, :, :] = v[:, -1, :] = v[:, :, -1] = 1.0          # close surface at design-box faces
    out["m"] = np.stack([ev(g) for g in ms.mdir])
    return out


def material_mask(fields):
    """int8 grid: 0 air, 1 iron, 2 ferrite with m_z >= 0, 3 ferrite with m_z < 0."""
    m = np.zeros(fields["fe"].shape, dtype=np.int8)
    m[fields["fe"] < 0] = 1
    f = fields["ff"] < 0
    m[f & (fields["m"][2] >= 0)] = 2; m[f & (fields["m"][2] < 0)] = 3
    return m


def mirror8(v, comp=None):
    """Mirror a 1/8-model grid (first node on the symmetry planes) to the full magnet. comp: component index of a
    magnetization / B-like vector, whose sign flips under the mirrors as the field's symmetry demands
    (B normal to z = 0, tangential to x = 0 and y = 0)."""
    for axis in range(3):
        half = np.take(v, np.arange(1, v.shape[axis]), axis=axis)
        if comp is not None and ((axis == 2) != (comp == 2)):
            half = -half
        v = np.concatenate([np.flip(half, axis=axis), v], axis=axis)
    return v


def _pack(verts, faces, cfg: Config, extra=None):
    D = cfg.design_L
    q = np.clip(np.round((verts + D) / (2 * D) * 65535), 0, 65535).astype(np.uint16)
    out = dict(pos=base64.b64encode(q.tobytes()).decode(), idx=base64.b64encode(faces.astype(np.uint32).tobytes()).decode(),
               ntri=int(len(faces)))
    if extra:
        out.update(extra)
    return out


def surface(vals, h, cfg: Config, level=0.0, mz=None):
    """Marching-cubes surface of {vals < level} mirrored to the full magnet, packed for the viewer.
    vals on a 1/8-model grid with spacing h whose first node lies on the symmetry planes. With mz (same grid),
    every vertex gets a colour from the z component of the magnetization there (red +z, white 0, blue -z)."""
    full = mirror8(vals)
    if full.min() >= level:
        return dict(pos="", idx="", ntri=0)
    verts, faces, _, _ = measure.marching_cubes(full, level=level, spacing=(h, h, h))
    extra = None
    if mz is not None:
        from scipy import ndimage
        z = ndimage.map_coordinates(mirror8(mz, comp=2), (verts / h).T, order=1, mode="nearest")
        col = np.stack([np.clip(1 + z, 0, 1), np.clip(1 - np.abs(z), 0, 1), np.clip(1 - z, 0, 1)], 1)
        extra = dict(col=base64.b64encode((col * 255).astype(np.uint8).tobytes()).decode())
    return _pack(verts - (vals.shape[0] - 1) * h, faces, cfg, extra)


def arrows(fields, cfg: Config, every=2):
    """Magnetization arrows at every `every`-th viewer-grid node inside ferrite, mirrored to the full magnet:
    packed positions (uint16) and directions (int8)."""
    h = cfg.viewer_h
    inside = mirror8(fields["ff"]) < 0
    m = np.stack([mirror8(fields["m"][i], comp=i) for i in range(3)])
    n = inside.shape[0]
    idx = np.argwhere(inside)
    idx = idx[(idx % every == 0).all(axis=1)]
    pos = (idx - (n - 1) / 2) * h
    d = m[:, idx[:, 0], idx[:, 1], idx[:, 2]].T
    D = cfg.design_L
    q = np.clip(np.round((pos + D) / (2 * D) * 65535), 0, 65535).astype(np.uint16)
    return dict(pos=base64.b64encode(q.tobytes()).decode(), dir=base64.b64encode(np.clip(np.round(d * 127), -127, 127).astype(np.int8).tobytes()).decode(),
                n=int(len(idx)), len=float(every * h * 0.8))


def green_surface(blob, cfg: Config):
    """Surface of the largest green component (boolean voxel grid of the 1/8 model, voxel centres), block-averaged
    to ~9 mm and mirrored to the full magnet."""
    f = max(1, int(round(0.009 / cfg.dx_img)))
    n = [-(-d // f) * f for d in blob.shape]
    b = np.zeros(n); b[:blob.shape[0], :blob.shape[1], :blob.shape[2]] = blob
    b = b.reshape(n[0] // f, f, n[1] // f, f, n[2] // f, f).mean(axis=(1, 3, 5))
    for axis in range(3):                                      # cell-centred: mirror without dropping a layer
        b = np.concatenate([np.flip(b, axis=axis), b], axis=axis)
    b = np.pad(1.0 - b, 1, constant_values=1.0)
    if b.min() >= 0.5:
        return dict(pos="", idx="", ntri=0)
    h = f * cfg.dx_img
    verts, faces, _, _ = measure.marching_cubes(b, level=0.5, spacing=(h, h, h))
    return _pack(verts - (np.array(b.shape) / 2 - 0.5) * h, faces, cfg)


def frame_from_fields(fields, cfg: Config):
    return dict(fe=surface(fields["fe"], cfg.viewer_h, cfg), ff=surface(fields["ff"], cfg.viewer_h, cfg, mz=fields["m"][2]),
                ar=arrows(fields, cfg))


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
// static: patient/bed envelope (blue), access corridor (dashed blue), design box (grey), axes (x red, y green, z blue)
scene.add(new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(DATA.env[0], DATA.env[1], DATA.env[2])), new THREE.LineBasicMaterial({color:0x4080ff})));
if (DATA.corridor) { const c = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(DATA.env[0], 2*D, DATA.env[2])), new THREE.LineDashedMaterial({color:0x4080ff, dashSize:0.02, gapSize:0.02})); c.computeLineDistances(); scene.add(c); }
scene.add(new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(2*D,2*D,2*D)), new THREE.LineBasicMaterial({color:0x555555})));
scene.add(new THREE.AxesHelper(D*1.3));
// iron grey; ferrite coloured by the z component of its magnetization (red +z, white transverse, blue -z) with
// black arrows for the direction; largest green component translucent green
const mats = {fe: new THREE.MeshPhongMaterial({color:0x9a9a9a, side:THREE.DoubleSide}), ff: new THREE.MeshPhongMaterial({vertexColors:true, side:THREE.DoubleSide}),
  gr: new THREE.MeshPhongMaterial({color:0x30d060, side:THREE.DoubleSide, transparent:true, opacity:.45, depthWrite:false})};
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
    if (m.col) { const c = b64(m.col, Uint8Array); const col = new Float32Array(c.length); for (let j=0;j<c.length;j++) col[j] = c[j]/255; g.setAttribute('color', new THREE.BufferAttribute(col,3)); }
    const o = new THREE.Mesh(g, mats[k]); scene.add(o); shown.push(o);
  }
  const a = f.meshes.ar;
  if (a && a.n > 0) {
    const q = b64(a.pos, Uint16Array), d = b64(a.dir, Int8Array); const seg = new Float32Array(a.n*6);
    for (let j=0;j<a.n;j++) { for (let c=0;c<3;c++) { const p = q[3*j+c]/65535*2*D - D, v = d[3*j+c]/127*a.len; seg[6*j+c] = p - v/2; seg[6*j+3+c] = p + v/2; } }
    const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(seg,3));
    const o = new THREE.LineSegments(g, new THREE.LineBasicMaterial({color:0x101010})); scene.add(o); shown.push(o);
    const tip = new THREE.BufferGeometry(); tip.setAttribute('position', new THREE.BufferAttribute(seg.filter((_, i) => i % 6 >= 3), 3));
    const t = new THREE.Points(tip, new THREE.PointsMaterial({color:0x101010, size:0.006})); scene.add(t); shown.push(t);
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
    """frames: list of dict(it, label, meshes={fe, ff[, gr]: surface(...), ar: arrows(...)})."""
    data = dict(D=cfg.design_L, env=[cfg.env_x, cfg.env_y, cfg.env_z], corridor=cfg.corridor, frames=frames)
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
    nBf, crf, cff = full(ev(ms.normB) * 1e3), full(ev(ms.cr)), full(ev(ms.crf))
    mx, mz = ev(ms.mdir[0]), ev(ms.mdir[2])
    fullm = lambda a, odd_x, odd_z: np.concatenate([np.flip(np.concatenate([(-1 if odd_x else 1) * np.flip(a[1:], 0), a], 0)[:, 1:], 1) * (-1 if odd_z else 1),
                                                   np.concatenate([(-1 if odd_x else 1) * np.flip(a[1:], 0), a], 0)], 1)
    mxf, mzf = fullm(mx, True, True), fullm(mz, False, False)
    ferf = cff * mzf
    ext = [-D, D, -D, D]
    fig, axs = plt.subplots(1, 3, figsize=(17, 5.5))
    im = axs[0].imshow(nBf.T, origin="lower", extent=ext, cmap="viridis", vmin=0, vmax=cfg.B_c * 2e3)
    lv = [(cfg.B_c - cfg.dB_band / 2) * 1e3, (cfg.B_c + cfg.dB_band / 2) * 1e3]
    axs[0].contour(nBf.T, levels=lv, extent=ext, colors=["lime", "white"], linewidths=0.8)
    axs[0].set_title("|B| [mT], plane y=0; band edges: green (low), white (high)"); plt.colorbar(im, ax=axs[0])
    axs[1].imshow(crf.T, origin="lower", extent=ext, cmap="Greys", vmin=0, vmax=1)
    axs[1].set_title("iron fraction, plane y=0")
    axs[2].imshow(ferf.T, origin="lower", extent=ext, cmap="bwr", vmin=-1, vmax=1)
    k = max(1, (2 * n - 1) // 30)
    xs = np.linspace(-D, D, 2 * n - 1)
    sel = cff[::k, ::k] > 0.3
    Xg, Zg = np.meshgrid(xs[::k], xs[::k], indexing="ij")
    axs[2].quiver(Xg[sel], Zg[sel], mxf[::k, ::k][sel], mzf[::k, ::k][sel], color="k", scale=25, width=0.004)
    axs[2].set_title("ferrite fraction x m_z (red +z, blue -z), arrows: m in the plane, y=0")
    for a in axs:
        a.add_patch(plt.Rectangle((-cfg.env_x / 2, -cfg.env_z / 2), cfg.env_x, cfg.env_z, fill=False, ec="deepskyblue", lw=1.5))
        a.set_xlabel("x [m]"); a.set_ylabel("z [m]")
    fig.suptitle(title, fontsize=10); fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def write_history_png(hist, cfg: Config, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    it = [h["it"] for h in hist]
    fig, axs = plt.subplots(2, 2, figsize=(11, 7))
    axs[0, 0].semilogy(it, [max(h["N_green"], 0.5) for h in hist], "o-", label="N_green (exact, largest component)")
    axs[0, 0].semilogy(it, [max(h["N_smooth"], 0.5) for h in hist], "s--", label="N_smooth"); axs[0, 0].legend(); axs[0, 0].set_title("good imaging voxels, full magnet")
    axs[0, 1].plot(it, [h["blob_mean"] * 1e3 for h in hist], "o-"); axs[0, 1].axhline(cfg.B_c * 1e3, c="r", ls="--"); axs[0, 1].set_title("mean |B| of the green blob (projection) [mT]")
    axs[1, 0].plot(it, [h["iron_kg"] for h in hist], "o-", label="iron"); axs[1, 0].plot(it, [h["ferrite_kg"] for h in hist], "s-", label="ferrite")
    axs[1, 0].legend(); axs[1, 0].set_title("mass, full magnet [kg]")
    fin = [(i, h["F"]) for i, h in zip(it, hist) if np.isfinite(h["F"])]
    if fin:
        axs[1, 1].semilogy(*zip(*fin), "o-", label="F = cost / N_green")
    fs = [(i, h["F_smooth"]) for i, h in zip(it, hist) if np.isfinite(h["F_smooth"]) and h["F_smooth"] < 1e12]
    if fs:
        axs[1, 1].semilogy(*zip(*fs), "s--", label="cost / N_smooth")
    axs[1, 1].legend(); axs[1, 1].set_title("cost per good voxel [$]")
    for a in axs.ravel():
        a.set_xlabel("iteration"); a.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
