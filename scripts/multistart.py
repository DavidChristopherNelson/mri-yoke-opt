"""Multi-start: N noise seeds plus the three H-frame seeds, run through scripts/session.py, then a summary.

  python scripts/multistart.py <session> [--n 8] [--hours 10] [--batch 4] [--script run_coarse.py] [key=value ...]

Runs go to results/<session>/<hframe_thin | hframe_medium | hframe_thick | noiseK>/. They are started `batch` at a
time, H-frames first (each batch is one session.py call that splits the machine's threads between its runs and
gets `hours`).
Afterwards results/<session>/summary.csv holds final F, cost, N_green, iron kg, ferrite kg, iterations and stop
reason per run, and iou_iron.csv / iou_ferrite.csv the pairwise intersection-over-union of the final iron and
ferrite masks (viewer grid), so that runs can be clustered. key=value pairs are passed to every run.
--summary-only rebuilds the summary from existing results."""
import argparse, csv, json, os, subprocess, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument("session")
ap.add_argument("overrides", nargs="*")
ap.add_argument("--n", type=int, default=8)
ap.add_argument("--hours", type=float, default=10.0)
ap.add_argument("--batch", type=int, default=4)
ap.add_argument("--script", default="run_coarse.py")
ap.add_argument("--summary-only", action="store_true")
a = ap.parse_args()

seeds = [(f"hframe_{v}", f"hframe:{v}") for v in ("thin", "medium", "thick")] + [(f"noise{k}", f"noise:{k}") for k in range(a.n)]
variants = [name + ":" + ",".join([f"init={init}"] + a.overrides) for name, init in seeds]
if not a.summary_only:
    for i in range(0, len(variants), a.batch):
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "session.py"), a.session, "--hours", str(a.hours),
                        "--script", a.script] + variants[i:i + a.batch], cwd=ROOT)

sdir = os.path.join(ROOT, "results", a.session)
rows, masks = [], {}
for name, init in seeds:
    d = os.path.join(sdir, name)
    try:
        with open(os.path.join(d, "history.csv")) as f:
            last = list(csv.DictReader(f))[-1]
        with open(os.path.join(d, "status.json")) as f:
            state = json.load(f)["state"]
    except (OSError, IndexError, ValueError):
        rows.append(dict(run=name, init=init, stop_reason="no result")); continue
    rows.append(dict(run=name, init=init, F=last["F"], cost=last["cost"], N_green=last["N_green"], iron_kg=last["iron_kg"],
                     ferrite_kg=last["ferrite_kg"], iterations=last["it"], stop_reason=state.replace("finished: ", "")))
    if os.path.exists(os.path.join(d, "mask_final.npy")):
        masks[name] = np.load(os.path.join(d, "mask_final.npy"))
with open(os.path.join(sdir, "summary.csv"), "w", newline="") as f:
    wr = csv.DictWriter(f, fieldnames=["run", "init", "F", "cost", "N_green", "iron_kg", "ferrite_kg", "iterations", "stop_reason"])
    wr.writeheader(); wr.writerows(rows)
names = list(masks)
for label, sel in (("iron", lambda m: m == 1), ("ferrite", lambda m: m >= 2)):
    with open(os.path.join(sdir, f"iou_{label}.csv"), "w", newline="") as f:
        wr = csv.writer(f); wr.writerow([""] + names)
        for n1 in names:
            a1 = sel(masks[n1])
            iou = [(a1 & sel(masks[n2])).sum() / max((a1 | sel(masks[n2])).sum(), 1) for n2 in names]
            wr.writerow([n1] + [f"{v:.3f}" for v in iou])
if os.environ.get("MRIYOKE_BLOB_URL"):                     # session-level files next to the runs in blob storage
    sys.path.insert(0, ROOT)
    from mriyoke.persist import BlobSink
    sink = BlobSink(os.environ["MRIYOKE_BLOB_URL"], a.session)
    for f in ("summary.csv", "iou_iron.csv", "iou_ferrite.csv", "status.txt"):
        sink.put_file(f, os.path.join(sdir, f))
    sink.flush()
print(open(os.path.join(sdir, "summary.csv")).read())
