"""Precondition for Plan X: is the SNR sufficient for the green-voxel definition the optimiser uses?
The optimiser itself never uses SNR; this script is the only place it appears.

  python scripts/precheck.py [key=value ...]        e.g. T_scan=600 R_coil=0.5 SNR_star=20

Check:  K0 * sqrt(T_scan) / sqrt(1 + gamma * t_ov * dB_band / (4 pi delta))  >=  SNR_star * (1 + kappa_s)

  dB_band = min(ism_fraction * ism_band, 2 delta dx_img G_max)         usable field band [T] (same as Config.dB_band)
  G_r     = dB_band / (2 delta dx_img)                                 readout gradient [T/m]
  per-voxel readout bandwidth = gamma G_r dx_img / 2 pi = gamma dB_band / (4 pi delta); one readout lasts its
  inverse, each readout costs t_ov of overhead, hence the duty-cycle factor under the square root.
  K0 = SNR of one voxel per sqrt(second of acquisition):  ASSUMPTION, the Plan X brief does not define it; here
       K0 = omega0 B1_minus M0 dx_img^3 f_seq / (F_rx sqrt(4 k_B (T_coil R_coil + T_body R_body)))
       M0 = rho gamma^2 hbar^2 B_c / (4 k_B T_body),   f_seq = (1 - exp(-TR / T1)) * kappa_T2
All inputs are measurable; the defaults are PLACEHOLDERS."""
import math, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mriyoke.config import Config

GAMMA = 2.6752218744e8        # 1H gyromagnetic ratio [rad/s/T]
HBAR = 1.054571817e-34
KB = 1.380649e-23

cfg = Config()
p = dict(rho=6.69e28,         # proton density of water [1/m^3]
         T_body=310.0, T_coil=293.0,          # temperatures [K]
         R_coil=0.5, R_body=0.1,              # series resistances of the receive coil and of the body loading [ohm]
         F_rx=1.12,                           # receiver noise factor (amplitude)
         B1_minus=2e-5,                       # receive sensitivity at the voxel [T/A]
         T1=0.35, T2=0.08, TR=0.5,            # [s]
         kappa_T2=0.7,                        # mean transverse signal fraction over the readout
         t_ov=2e-3,                           # overhead per readout [s]
         T_scan=600.0,                        # total scan time [s]
         dx_img=cfg.dx_img, delta=cfg.delta, G_max=cfg.G_max, SNR_star=10.0)
for kv in sys.argv[1:]:
    k, v = kv.split("="); p[k] = float(v)
cfg.dx_img, cfg.delta, cfg.G_max = p["dx_img"], p["delta"], p["G_max"]

dB_band, G_r = cfg.dB_band, cfg.dB_band / (2 * p["delta"] * p["dx_img"])
omega0 = GAMMA * cfg.B_c
M0 = p["rho"] * GAMMA ** 2 * HBAR ** 2 * cfg.B_c / (4 * KB * p["T_body"])
f_seq = (1 - math.exp(-p["TR"] / p["T1"])) * p["kappa_T2"]
noise = p["F_rx"] * math.sqrt(4 * KB * (p["T_coil"] * p["R_coil"] + p["T_body"] * p["R_body"]))
K0 = omega0 * p["B1_minus"] * M0 * p["dx_img"] ** 3 * f_seq / noise
bw = GAMMA * dB_band / (4 * math.pi * p["delta"])
snr = K0 * math.sqrt(p["T_scan"]) / math.sqrt(1 + p["t_ov"] * bw)
need = p["SNR_star"] * (1 + cfg.kappa_s)
print(f"dB_band = {dB_band * 1e3:.3f} mT ({cfg.band_limit} limit binds)   G_r = {G_r * 1e3:.2f} mT/m   s_max = {cfg.s_max * 1e3:.2f} mT/m")
print(f"readout bandwidth per voxel = {bw:.0f} Hz (readout {1e3 / bw:.2f} ms, overhead {p['t_ov'] * 1e3:.2f} ms, duty {1 / (1 + p['t_ov'] * bw):.2f})")
if 1 / bw > p["T2"]:
    print(f"warning: one readout ({1e3 / bw:.1f} ms) is longer than T2 ({p['T2'] * 1e3:.0f} ms)")
print(f"K0 = {K0:.3f} per sqrt(s)   (M0 = {M0:.3e} A/m, f_seq = {f_seq:.2f}, noise = {noise:.2e} V/sqrt(Hz))")
print(f"SNR = {snr:.1f}   required {need:.1f} = SNR_star (1 + kappa_s)")
print(f"{'PASS' if snr >= need else 'FAIL'}: margin x{snr / need:.2f}; scan time needed {p['T_scan'] * (need / snr) ** 2:.0f} s")
