"""Imaging voxels in the patient/bed envelope: exact green-voxel count and its smooth, differentiable surrogate.
A voxel is green when  | |B| - B_c | <= dB_band / 2  and  | d|B|/dr | <= s_max  at its centre (r = readout axis).
Exact count: |B| sampled from the FE solution at the voxel centres, slope by central differences on that grid,
largest 6-connected component with the symmetry planes as mirrors.
Smooth count: product of two logistic sigmoids of the even-harmonic polynomial projection of |B|,
b(x) = sum_k a_k phi_k(x), so that it is a function of the mode coefficients a_k only. The projection region is a
sphere at the centre of the envelope (Config.proj_radius): over the whole envelope box the projection does not
converge, because the sources touch the box."""
import numpy as np
from scipy import ndimage
from ngsolve import x, y, z
from .config import Config


def even_harmonics(L):
    """Harmonic polynomials even in x, y, z up to degree L, as lists of ((i, j, k), coef) for x^2i y^2j z^2k."""
    out = []
    for h in range(L // 2 + 1):
        mono = [(i, j, h - i - j) for i in range(h + 1) for j in range(h + 1 - i)]
        if h == 0:
            out.append([(mono[0], 1.0)]); continue
        low = {m: n for n, m in enumerate((i, j, h - 1 - i - j) for i in range(h) for j in range(h - i))}
        lap = np.zeros((len(low), len(mono)))
        for c, m in enumerate(mono):
            for ax in range(3):
                if m[ax] > 0:
                    t = list(m); t[ax] -= 1
                    lap[low[tuple(t)], c] += 2 * m[ax] * (2 * m[ax] - 1)
        _, sv, vt = np.linalg.svd(lap)
        for vec in vt[len(low):]:                         # null space of the Laplacian: h + 1 polynomials
            out.append([(m, float(c)) for m, c in zip(mono, vec)])
    return out


class EnvelopeBasis:
    """Even harmonic polynomials up to degree L, orthonormal in L2(region) / V (phi_0 = 1); region = sphere of
    radius proj_radius, or the envelope box if proj_radius = 0.
    phi_k = sum_m C[k, m] (x/s)^2i (y/s)^2j (z/s)^2k over the monomials m = (i, j, k); Gram matrix analytic."""

    def __init__(self, cfg: Config):
        self.half = np.array([cfg.env_x, cfg.env_y, cfg.env_z]) / 2
        self.R = cfg.proj_radius
        self.s = self.R if self.R > 0 else float(self.half.max())
        polys = even_harmonics(cfg.harm_order)
        self.monos = sorted({m for p in polys for m, _ in p})
        col = {m: n for n, m in enumerate(self.monos)}
        P = np.zeros((len(polys), len(self.monos)))
        for k, p in enumerate(polys):
            for m, c in p:
                P[k, col[m]] = c
        r = self.half / self.s
        dfact = lambda n: float(np.prod(np.arange(n, 0, -2))) if n > 0 else 1.0
        if self.R > 0:      # ball mean of x^2i y^2j z^2k (unit radius): 3/(n+3) (2i-1)!!(2j-1)!!(2k-1)!!/(n+1)!!, n = 2(i+j+k)
            mean = lambda e: 3 / (2 * sum(e) + 3) * np.prod([dfact(2 * q - 1) for q in e]) / dfact(2 * sum(e) + 1)
        else:               # box mean
            mean = lambda e: np.prod([r[a] ** (2 * e[a]) / (2 * e[a] + 1) for a in range(3)])
        gm = np.array([[mean(tuple(a + b for a, b in zip(m1, m2))) for m2 in self.monos] for m1 in self.monos])
        self.C = np.linalg.inv(np.linalg.cholesky(P @ gm @ P.T)) @ P
        self.n = len(polys)

    def cfs(self):
        """The basis as NGSolve coefficient functions."""
        X = [x / self.s, y / self.s, z / self.s]
        mono = [X[0] ** (2 * i) * X[1] ** (2 * j) * X[2] ** (2 * k) for i, j, k in self.monos]
        return [sum(float(c) * m for c, m in zip(row, mono) if c != 0.0) for row in self.C]

    def at(self, pts, deriv=None):
        """(npts, K) values of the basis at pts (npts, 3), or of its derivative along axis deriv."""
        U = pts / self.s
        cols = []
        for e in self.monos:
            v = np.ones(len(pts))
            for a in range(3):
                n = 2 * e[a]
                if a == deriv:
                    v = v * (n * U[:, a] ** (n - 1) / self.s if n > 0 else 0.0)
                else:
                    v = v * U[:, a] ** n
            cols.append(v)
        return np.stack(cols, 1) @ self.C.T


def _log_sigmoid(u):
    return np.where(u > 0, -np.log1p(np.exp(-np.abs(u))), u - np.log1p(np.exp(-np.abs(u))))


class Imaging:
    def __init__(self, mesh, ms, cfg: Config, basis: EnvelopeBasis):
        self.mesh, self.ms, self.cfg = mesh, ms, cfg
        h = cfg.dx_img
        self.shape = tuple(int(np.floor(l / h + 1e-9)) for l in basis.half)
        ax = [(np.arange(n) + 0.5) * h for n in self.shape]
        X, Y, Z = np.meshgrid(*ax, indexing="ij")
        self.pts = np.stack([X.ravel(), Y.ravel(), Z.ravel()], 1)
        self.mp = mesh(self.pts[:, 0], self.pts[:, 1], self.pts[:, 2])   # point location once
        self.axis = "xyz".index(cfg.readout_axis)
        rad2 = (self.pts ** 2).sum(1)
        # voxels of the projection region: the smooth count and everything that uses the projection is restricted to them
        self.proj = np.flatnonzero(rad2 < basis.R ** 2) if basis.R > 0 else np.arange(len(self.pts))
        self.Phi = basis.at(self.pts[self.proj])
        self.dPhi = basis.at(self.pts[self.proj], deriv=self.axis)
        self.fallback = rad2[self.proj] < cfg.blob_fallback_r ** 2

    def sample(self):
        """|B| of the current FE solution at the voxel centres (flat)."""
        return np.asarray(self.ms.normB(self.mp)).ravel()

    def green(self, nB):
        """Exact test at every voxel: boolean grid."""
        cfg = self.cfg
        g = nB.reshape(self.shape)
        pad = [(1, 1) if a == self.axis else (0, 0) for a in range(3)]
        gp = np.pad(g, pad, mode="symmetric")                  # mirror at the symmetry plane (and at the far face)
        sl = lambda lo, hi: tuple(slice(lo, hi) if a == self.axis else slice(None) for a in range(3))
        slope = (gp[sl(2, None)] - gp[sl(0, -2)]) / (2 * cfg.dx_img)
        return (np.abs(g - cfg.B_c) <= cfg.dB_band / 2) & (np.abs(slope) <= cfg.s_max)

    def largest(self, mask):
        """Largest connected green component (6-connectivity, symmetry planes as mirrors).
        Returns (boolean grid of the component in the 1/8 model, voxel count in the full magnet).
        A component touching t of the three symmetry planes joins 2^t of its 8 mirror images."""
        lab, n = ndimage.label(mask)
        if n == 0:
            return np.zeros(self.shape, bool), 0
        size = np.bincount(lab.ravel(), minlength=n + 1).astype(np.int64)
        for a in range(3):
            touch = np.unique(np.take(lab, 0, axis=a))
            size[touch[touch > 0]] *= 2
        size[0] = 0
        k = int(size.argmax())
        return lab == k, int(size[k])

    def log_count(self, a, w_b, w_s):
        """Smooth count of the 1/8 model: ln N_s and d ln N_s / d a_k, N_s = sum_x sig_b(x) sig_s(x) with
        sig_b = sigmoid((dB_band/2 - |b - B_c|) / w_b), sig_s = sigmoid((s_max - |db/dr|) / w_s), b = Phi a."""
        cfg = self.cfg
        b, db = self.Phi @ a, self.dPhi @ a
        ub, us = (cfg.dB_band / 2 - np.abs(b - cfg.B_c)) / w_b, (cfg.s_max - np.abs(db)) / w_s
        lb, ls = _log_sigmoid(ub), _log_sigmoid(us)
        l = lb + ls
        lmax = l.max()
        pi = np.exp(l - lmax)
        logN = lmax + np.log(pi.sum())
        pi /= pi.sum()
        # d log sigmoid(u)/du = 1 - sigmoid(u)
        grad = (pi * (1 - np.exp(lb)) * (-np.sign(b - cfg.B_c) / w_b)) @ self.Phi \
            + (pi * (1 - np.exp(ls)) * (-np.sign(db) / w_s)) @ self.dPhi
        return float(logN), grad

    def _sel(self, blob):
        """Blob voxels inside the projection region (as a mask over self.proj); fallback sphere if there are fewer
        than blob_min_voxels of them (a voxel or two at the band edge is noise, not an anchor)."""
        sel = blob.ravel()[self.proj]
        return sel if sel.sum() >= self.cfg.blob_min_voxels else self.fallback

    def blob_coefs(self, blob):
        """c with (mean of the projection b over the blob voxels) = c . a."""
        return self.Phi[self._sel(blob)].mean(axis=0)

    def spread(self, a, blob):
        """rms of b - B_c over the blob voxels [T]."""
        return float(np.sqrt(np.mean((self.Phi[self._sel(blob)] @ a - self.cfg.B_c) ** 2)))

    def residual(self, nB, a, blob):
        """Largest |FE - projection| over the blob voxels [T]."""
        sel = self._sel(blob)
        return float(np.abs(nB[self.proj][sel] - self.Phi[sel] @ a).max())
