"""Rebuild viewer.html of every run under the given directories from its saved frames (frames/NNNN.json.gz as
downloaded from blob storage, or frames.jsonl.gz of a local run). azure.sh pull calls it after each download.

  python scripts/rebuild_viewer.py results/az1 [more dirs ...]"""
import dataclasses, glob, gzip, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mriyoke.config import Config
from mriyoke import export


def frames_of(d):
    frames = {}
    for p in sorted(glob.glob(os.path.join(d, "frames", "*.json.gz"))):
        try:
            fr = json.loads(gzip.decompress(open(p, "rb").read()))
            frames[fr["it"]] = fr
        except (OSError, ValueError, EOFError):
            continue
    if not frames and os.path.exists(os.path.join(d, "frames.jsonl.gz")):
        try:
            with gzip.open(os.path.join(d, "frames.jsonl.gz"), "rt") as f:
                for line in f:
                    try:
                        fr = json.loads(line); frames[fr["it"]] = fr
                    except ValueError:
                        continue
        except (OSError, EOFError):
            pass
    return [frames[k] for k in sorted(frames)]


def rebuild(d):
    frames = frames_of(d)
    if not frames:
        return False
    cfg = Config()
    try:
        saved = json.load(open(os.path.join(d, "config.json")))
        names = {f.name for f in dataclasses.fields(Config)}
        for k, v in saved.items():
            if k in names:
                setattr(cfg, k, v)
    except (OSError, ValueError):
        pass
    export.write_viewer(frames, cfg, os.path.join(d, "viewer.html"))
    return True


if __name__ == "__main__":
    n = 0
    for top in sys.argv[1:] or ["results"]:
        for hist in glob.glob(os.path.join(top, "**", "history.csv"), recursive=True):
            n += rebuild(os.path.dirname(hist))
    print(f"rebuilt {n} viewer(s)")
