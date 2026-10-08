"""Cloud dashboard: a local web page over the runs in Azure Blob Storage. Shows finished runs and runs in progress
(read straight from the cloud, cached only in memory), creates new runs, pauses, resumes and cancels runs, and
starts / stops the VM. The VM side is cloud/agent.py; both only talk through blob storage (mriyoke/control.py).

  .venv/bin/python scripts/cloud_dashboard.py            # opens http://localhost:8770/
  options: --port 8770 --url <container URL> --rg mriyoke-rg --vm mriyoke-f16 --no-vm --no-auto-start --no-browser

Needs `az login` (blob access through your Azure login, VM start/stop through the az CLI). With --no-vm the VM
controls are hidden. While the page server runs it also starts the VM when runs are waiting and the VM is off
(after a spot eviction, or after the agent switched it off when idle); --no-auto-start turns that off."""
import argparse, collections, concurrent.futures, csv, dataclasses, gzip, http.server, io, json, os, shutil, socketserver
import subprocess, sys, threading, time, urllib.parse, webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from mriyoke.persist import container
from mriyoke.control import CTRL, NAME, SCRIPTS, Store, age_s, config_fields, now, parse_args, runnable
from mriyoke.config import Config
from mriyoke import export

AGENT_ALIVE_S = 90


def clean(x):
    """JSON for browsers: NaN / Infinity (e.g. F with no green voxel) become null."""
    if isinstance(x, float):
        return x if x == x and abs(x) != float("inf") else None
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    return x


class LRU:
    def __init__(self, max_bytes):
        self.d, self.size, self.max = collections.OrderedDict(), 0, max_bytes
        self.lock = threading.Lock()

    def get(self, k):
        with self.lock:
            if k in self.d:
                self.d.move_to_end(k)
                return self.d[k]

    def put(self, k, v):
        with self.lock:
            if k in self.d:
                self.size -= len(self.d.pop(k))
            self.d[k] = v; self.size += len(v)
            while self.size > self.max and self.d:
                self.size -= len(self.d.popitem(last=False)[1])


class Cloud:
    def __init__(self, url, rg, vm, use_vm, auto_start):
        self.c = container(url)
        self.store = Store(self.c)
        self.pool = concurrent.futures.ThreadPoolExecutor(16)
        self.big = LRU(400 << 20)                  # immutable blobs (iteration images, frames)
        self.small = {}                            # name -> (time, bytes), short TTL
        self.rg, self.vmname, self.use_vm, self.auto_start = rg, vm, use_vm, auto_start
        self.vm = dict(power="unknown" if use_vm else "disabled", action=None, error=None, checked=None, auto_started=None)
        self.overview_cache = (0, None)
        self.lock = threading.Lock()
        if use_vm:
            threading.Thread(target=self.vm_loop, daemon=True).start()

    # ---------------- blobs ----------------
    def blob(self, name, ttl=10.0, immutable=False):
        if immutable:
            hit = self.big.get(name)
            if hit is not None:
                return hit
        else:
            hit = self.small.get(name)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        try:
            data = self.c.download_blob(name).readall()
        except Exception:
            data = None
        if data is not None:
            if immutable:
                self.big.put(name, data)
            else:
                self.small[name] = (time.time(), data)
        return data

    def json_blob(self, name, ttl=10.0):
        b = self.blob(name, ttl)
        try:
            return json.loads(b) if b else None
        except ValueError:
            return None

    def tail(self, name, n=30000):
        try:
            bc = self.c.get_blob_client(name)
            size = bc.get_blob_properties().size
            return bc.download_blob(offset=max(0, size - n)).readall().decode(errors="replace")
        except Exception:
            return ""

    # ---------------- runs ----------------
    def run_keys(self):
        sessions = [p.name for p in self.c.walk_blobs(delimiter="/") if p.name.endswith("/") and not p.name.startswith("_")]
        keys = set()

        def sub(s):
            return [p.name.rstrip("/") for p in self.c.walk_blobs(name_starts_with=s, delimiter="/") if p.name.endswith("/")]
        for ks in self.pool.map(sub, sessions):
            keys.update(ks)
        return keys

    def overview(self):
        with self.lock:
            t, cached = self.overview_cache
            if cached and time.time() - t < 8:
                return cached
        reqs, states = self.store.requests(), self.store.states()
        keys = self.run_keys() | set(reqs)
        keys = {k for k in keys if "/" in k}                          # <session>/<run>
        statuses = dict(zip(keys, self.pool.map(lambda k: self.json_blob(f"{k}/status.json", 10), keys)))
        agent = self.json_blob(f"{CTRL}/agent.json", 5)
        agent_age = age_s(agent.get("time")) if agent else None
        alive = agent_age is not None and agent_age < AGENT_ALIVE_S
        rows = [self.row(k, reqs.get(k), states.get(k), statuses.get(k), alive) for k in keys]
        rows.sort(key=lambda r: (r["session"], r["name"]))
        out = dict(generated=now(), vm=dict(self.vm), agent=agent, agent_age_s=agent_age, agent_alive=alive, runs=rows,
                   pending=sum(1 for k in keys if runnable(reqs.get(k), states.get(k))))
        with self.lock:
            self.overview_cache = (time.time(), out)
        return out

    @staticmethod
    def row(key, req, st, status, alive):
        session, _, name = key.rpartition("/")
        status = status or {}
        if req:
            state = (st or {}).get("state") or "queued"
            if req.get("gen", 0) > (st or {}).get("gen", 0):
                state = {"pause": "pausing", "cancel": "cancelling"}.get(req.get("desired"), state if state in ("running", "queued") else "resuming")
            if state == "running" and not alive:
                state = "interrupted"
        else:
            s = status.get("state", "")
            if s.startswith("finished"):
                state = "done"
            elif s == "running":
                u = status.get("updated")
                stale = u and time.time() - time.mktime(time.strptime(u, "%Y-%m-%d %H:%M:%S")) > 1800
                state = "interrupted" if stale else "running"
            else:
                state = s or "unknown"
        spec = (req or {}).get("spec", {})
        args = spec.get("args", {})
        it = status.get("it")
        return dict(key=key, session=session, name=name, managed=bool(req), state=state,
                    detail=(st or {}).get("detail") or status.get("state", ""), desired=(req or {}).get("desired"),
                    init=args.get("init", ""), script=spec.get("script", ""), threads=spec.get("threads"),
                    it=it, iter_max=status.get("iter_max") or args.get("iter_max"), s_per_it=status.get("s_per_it"),
                    F=status.get("F"), N_green=status.get("N_green"), mean_mT=status.get("mean_mT"),
                    iron_kg=status.get("iron_kg"), ferrite_kg=status.get("ferrite_kg"), elapsed_h=status.get("elapsed_h"),
                    eta=status.get("eta_latest"), updated=status.get("updated"), created=(req or {}).get("created"),
                    thumb=f"{key}/iter_{it:04d}.png" if isinstance(it, int) else None)

    def run(self, key):
        ov = self.overview()
        row = next((r for r in ov["runs"] if r["key"] == key), None)
        hist = {}
        b = self.blob(f"{key}/history.csv", 10)
        if b:
            rows = list(csv.DictReader(io.StringIO(b.decode(errors="replace"))))
            for col in (rows[0].keys() if rows else []):
                vals = []
                for r in rows:
                    try:
                        v = float(r[col]); vals.append(v if v == v and abs(v) != float("inf") else None)
                    except (TypeError, ValueError):
                        vals.append(None)
                hist[col] = vals
        req = self.store.get(f"{CTRL}/runs/{key}.json")
        cfg = self.json_blob(f"{key}/config.json", 60)
        return dict(row=row, history=hist, request=req, config=cfg, log=self.tail(f"{key}/run.log"),
                    agent_log=self.tail(f"{key}/agent.log", 8000) if row and row["state"] == "failed" else "")

    def viewer(self, key, all_frames):
        b = self.blob(f"{key}/history.csv", 10)
        its = []
        if b:
            its = [int(float(r["it"])) for r in csv.DictReader(io.StringIO(b.decode(errors="replace")))]
        if not its:
            return None
        if not all_frames:
            its = its[-1:]
        frames = []
        for raw in self.pool.map(lambda i: self.blob(f"{key}/frames/{i:04d}.json.gz", immutable=True), its):
            if raw:
                try:
                    frames.append(json.loads(gzip.decompress(raw)))
                except (OSError, ValueError):
                    pass
        if not frames:
            return None
        cfg = Config()
        saved = self.json_blob(f"{key}/config.json", 600) or {}
        names = {f.name for f in dataclasses.fields(Config)}
        for k, v in saved.items():
            if k in names:
                setattr(cfg, k, v)
        return export.viewer_html(frames, cfg).encode()

    # ---------------- control ----------------
    def create(self, body):
        session = str(body.get("session", "")).strip()
        if not NAME.match(session):
            raise ValueError("session name: letters, digits, '-', '_' or '.', up to 64 characters")
        script = body.get("script", "run_coarse.py")
        if script not in SCRIPTS:
            raise ValueError("unknown mesh / script")
        threads = int(body.get("threads", 4))
        args = parse_args(body.get("args") or {})
        if body.get("iter_max"):
            args["iter_max"] = int(body["iter_max"])
        starts = body.get("starts") or []
        if not starts:
            raise ValueError("choose at least one start design")
        made, skipped = [], []
        for init in starts:
            kind, _, arg = init.partition(":")
            if kind not in ("hframe", "noise", "empty"):
                raise ValueError(f"unknown start design {init}")
            name = {"hframe": f"hframe_{arg}" if arg else "hframe", "noise": f"noise{arg}", "empty": "empty"}[kind]
            key = f"{session}/{name}"
            path = f"{CTRL}/runs/{key}.json"
            if self.store.get(path) is not None:
                skipped.append(key); continue
            self.store.put(path, dict(spec=dict(script=script, threads=threads, args=dict(args, init=init)),
                                      desired="run", gen=1, created=now(), updated=now()))
            made.append(key)
        self.overview_cache = (0, None)
        return dict(created=made, skipped=skipped)

    def control(self, body):
        action = body.get("action")
        desired = {"pause": "pause", "resume": "run", "cancel": "cancel"}.get(action)
        if not desired:
            raise ValueError("unknown action")
        done, refused = [], []
        for key in body.get("keys") or []:
            path = f"{CTRL}/runs/{key}.json"
            req = self.store.get(path)
            if req is None:
                refused.append(key); continue
            req.update(desired=desired, gen=req.get("gen", 0) + 1, updated=now())
            self.store.put(path, req)
            done.append(key)
        self.overview_cache = (0, None)
        return dict(done=done, refused=refused)

    # ---------------- VM ----------------
    def az(self, *args, timeout=180):
        r = subprocess.run(["az", *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "az failed")
        return r.stdout.strip()

    def vm_power(self):
        try:
            p = self.az("vm", "get-instance-view", "-g", self.rg, "-n", self.vmname, "--query",
                        "instanceView.statuses[?starts_with(code,'PowerState/')].displayStatus | [0]", "-o", "tsv", timeout=60)
            self.vm.update(power=p or "unknown", error=None, checked=now())
        except Exception as e:
            self.vm.update(power="unknown", error=str(e)[:300], checked=now())

    def vm_action(self, action):
        if not self.use_vm:
            raise ValueError("VM controls are disabled (--no-vm)")
        if action not in ("start", "stop"):
            raise ValueError("unknown VM action")

        def go():
            self.vm["action"] = "starting" if action == "start" else "stopping"
            try:
                self.az("vm", "start" if action == "start" else "deallocate", "-g", self.rg, "-n", self.vmname, "-o", "none", timeout=900)
            except Exception as e:
                self.vm["error"] = str(e)[:300]
            self.vm["action"] = None
            self.vm_power()
        threading.Thread(target=go, daemon=True).start()
        return dict(ok=True)

    def vm_loop(self):
        if not shutil.which("az"):
            self.vm.update(power="unknown", error="Azure CLI (az) not found: brew install azure-cli, then az login")
            return
        while True:
            if not self.vm["action"]:
                self.vm_power()
            try:
                ov = self.overview()
                last = self.vm.get("auto_started")
                if (self.auto_start and ov["pending"] and self.vm["power"] in ("VM deallocated", "VM stopped")
                        and not self.vm["action"] and (last is None or time.time() - last > 600)):
                    self.vm["auto_started"] = time.time()
                    self.vm_action("start")
            except Exception:
                pass
            time.sleep(30)


def make_handler(cloud):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json", cache="no-store"):
            if isinstance(body, (dict, list)):
                body = json.dumps(clean(body), allow_nan=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    return self.send(200, PAGE, "text/html; charset=utf-8")
                if u.path == "/api/overview":
                    return self.send(200, cloud.overview())
                if u.path == "/api/run":
                    return self.send(200, cloud.run(q["key"]))
                if u.path == "/api/fields":
                    return self.send(200, [dict(name=n, type=t, default=d, help=h) for n, t, d, h in config_fields()])
                if u.path == "/api/file":
                    name = q.get("name", "")
                    if ".." in name or name.startswith("/"):
                        return self.send(400, {"error": "bad name"})
                    immutable = "/iter_" in name or "/frames/" in name
                    data = cloud.blob(name, ttl=10, immutable=immutable)
                    if data is None:
                        return self.send(404, {"error": "not found"})
                    ct = "image/png" if name.endswith(".png") else "text/plain; charset=utf-8"
                    return self.send(200, data, ct, "public, max-age=31536000, immutable" if immutable else "max-age=10")
                if u.path == "/api/viewer":
                    html = cloud.viewer(q["key"], q.get("all") == "1")
                    if html is None:
                        return self.send(404, "no 3D frames for this run yet", "text/plain")
                    return self.send(200, html, "text/html; charset=utf-8")
                return self.send(404, {"error": "not found"})
            except Exception as e:
                return self.send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/runs":
                    return self.send(200, cloud.create(body))
                if self.path == "/api/control":
                    return self.send(200, cloud.control(body))
                if self.path == "/api/vm":
                    return self.send(200, cloud.vm_action(body.get("action")))
                return self.send(404, {"error": "not found"})
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:
                return self.send(500, {"error": f"{type(e).__name__}: {e}"})
    return H


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MRI magnet runs</title>
<style>
:root{--bg:#f4f4f2;--surface:#fcfcfb;--line:#e4e3df;--ink:#0b0b0b;--ink2:#52514e;--ink3:#8a8984;--accent:#2a78d6;--accent-ink:#fff;
--good:#0ca30c;--warn:#b77d00;--serious:#ec835a;--crit:#d03b3b;--chip:#ecebe7;--shadow:0 1px 2px rgba(0,0,0,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--surface:#1a1a19;--line:#33332f;--ink:#fff;--ink2:#c3c2b7;--ink3:#8f8e86;--accent:#3987e5;
--good:#0ca30c;--warn:#fab219;--serious:#ec835a;--crit:#e66767;--chip:#262624;--shadow:none}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;border:1px solid var(--line);background:var(--surface);border-radius:8px;padding:6px 12px}
button:hover{border-color:var(--ink3)}button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
button.danger{color:var(--crit)}button.small{padding:3px 9px;font-size:12.5px;border-radius:7px}button:disabled{opacity:.45;cursor:default}
header{position:sticky;top:0;z-index:10;background:var(--surface);border-bottom:1px solid var(--line);padding:10px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:650}.grow{flex:1}
.vm{display:flex;align-items:center;gap:10px;padding:5px 10px;border:1px solid var(--line);border-radius:10px;background:var(--bg)}
.vm .dot{width:9px;height:9px;border-radius:50%;background:var(--ink3)}.vm .dot.on{background:var(--good)}.vm .dot.busy{background:var(--warn)}
.muted{color:var(--ink2)}.faint{color:var(--ink3)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
main{max-width:1500px;margin:0 auto;padding:18px 20px 60px}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}.seg button{border:0;border-radius:0;background:var(--surface)}
.seg button.on{background:var(--chip);font-weight:600}
input[type=search],input[type=text],input[type=number],select,textarea{border:1px solid var(--line);background:var(--surface);border-radius:8px;padding:6px 9px}
.session{margin:22px 0 10px;display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.session h2{font-size:15px;margin:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer;box-shadow:var(--shadow);display:flex;flex-direction:column}
.card:hover{border-color:var(--ink3)}.thumb{aspect-ratio:3/1;background:#fff;border-bottom:1px solid var(--line);overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:cover;object-position:left center;display:block}.thumb.none{display:flex;align-items:center;justify-content:center;color:var(--ink3);background:var(--bg)}
.card .body{padding:10px 12px 12px;display:flex;flex-direction:column;gap:7px}
.title{display:flex;align-items:center;gap:8px;justify-content:space-between}.title b{font-size:14.5px}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:12px;padding:2px 8px;border-radius:999px;background:var(--chip);color:var(--ink2);white-space:nowrap}
.badge i{font-style:normal;font-size:11px}.badge.running{color:var(--accent)}.badge.done{color:var(--good)}.badge.failed{color:var(--crit)}
.badge.interrupted,.badge.pausing,.badge.cancelling,.badge.resuming{color:var(--warn)}
.bar{height:5px;border-radius:3px;background:var(--chip);overflow:hidden}.bar span{display:block;height:100%;background:var(--accent);border-radius:3px}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:4px 8px}.metrics div{display:flex;flex-direction:column}.metrics small{color:var(--ink3);font-size:11px}
.metrics b{font-weight:600;font-variant-numeric:tabular-nums}.actions{display:flex;gap:6px;flex-wrap:wrap}
.empty{padding:50px 20px;text-align:center;color:var(--ink2);background:var(--surface);border:1px dashed var(--line);border-radius:12px}
.detail .head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px}.detail .head h2{margin:0;font-size:18px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:9px 12px}.kpi small{color:var(--ink3);font-size:11.5px}
.kpi b{display:block;font-size:20px;font-weight:620;font-variant-numeric:tabular-nums;margin-top:2px}.kpi span{color:var(--ink2);font-size:12px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:14px}.panel h3{margin:0 0 10px;font-size:14px}
.player{display:flex;gap:10px;align-items:center;margin-bottom:8px}.player input{flex:1}
#frame{width:100%;border-radius:6px;background:#fff;display:block}
.charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.chart{position:relative}.chart .t{font-size:12.5px;color:var(--ink2);margin-bottom:2px}.chart svg{width:100%;height:130px;display:block;overflow:visible}
.tip{position:absolute;pointer-events:none;background:var(--ink);color:var(--surface);font-size:12px;padding:3px 7px;border-radius:6px;white-space:nowrap;transform:translate(-50%,-110%);display:none}
iframe{width:100%;height:600px;border:0;border-radius:8px;background:#111}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:380px;overflow:auto;margin:0;font-size:12px}
table.kv{border-collapse:collapse;width:100%;font-size:12.5px}table.kv td{border-bottom:1px solid var(--line);padding:4px 6px;vertical-align:top}table.kv td:first-child{color:var(--ink2);white-space:nowrap}
dialog{border:1px solid var(--line);border-radius:14px;padding:0;background:var(--surface);color:var(--ink);width:min(720px,94vw);max-height:92vh}
dialog::backdrop{background:rgba(0,0,0,.35)}dialog form{padding:18px 20px;display:flex;flex-direction:column;gap:14px;overflow:auto;max-height:92vh}
dialog h2{margin:0;font-size:17px}.row{display:flex;gap:14px;flex-wrap:wrap}.field{display:flex;flex-direction:column;gap:4px;min-width:130px;flex:1}
.field label,.fl{font-size:12.5px;color:var(--ink2)}.checks{display:flex;gap:14px;flex-wrap:wrap}.checks label{display:flex;gap:6px;align-items:center}
.summary{background:var(--bg);border-radius:8px;padding:9px 12px;color:var(--ink2);font-size:13px}
.err{color:var(--crit);font-size:13px;white-space:pre-wrap}.ref{max-height:200px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.ref table{border-collapse:collapse;width:100%;font-size:12px}.ref td{padding:3px 6px;border-bottom:1px solid var(--line);cursor:pointer}.ref tr:hover td{background:var(--chip)}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--surface);padding:8px 14px;border-radius:9px;display:none;z-index:50}
a{color:var(--accent)}details summary{cursor:pointer;color:var(--ink2)}
@media (max-width:640px){header{padding:8px 12px}main{padding:12px}.metrics{grid-template-columns:repeat(2,1fr)}iframe{height:420px}}
</style></head><body>
<header><h1>MRI magnet runs</h1>
<div class="vm" id="vm"><span class="dot" id="vmdot"></span><span id="vmtext">VM …</span><button class="small" id="vmbtn" style="display:none"></button></div>
<span class="muted" id="agent"></span><span class="grow"></span><span class="faint" id="updated"></span>
<button class="primary" id="newbtn">+ New runs</button></header>
<main id="main"></main>
<dialog id="dlg"><form method="dialog" id="newform">
<h2>New runs</h2>
<div class="row"><div class="field"><label for="f-session">Session name</label><input type="text" id="f-session" required></div>
<div class="field"><label for="f-mesh">Mesh</label><select id="f-mesh"><option value="run_coarse.py">coarse (fast, ~20k elements)</option><option value="run_fine.py">fine (~50k elements, ~5x slower)</option></select></div></div>
<div><div class="fl">Start designs</div><div class="checks" style="margin-top:6px">
<label><input type="checkbox" class="hf" value="thin" checked> H-frame thin</label><label><input type="checkbox" class="hf" value="medium" checked> H-frame medium</label>
<label><input type="checkbox" class="hf" value="thick" checked> H-frame thick</label></div>
<div class="row" style="margin-top:8px"><div class="field"><label for="f-nseed">Noise seeds</label><input type="number" id="f-nseed" min="0" max="64" value="8"></div>
<div class="field"><label for="f-seed0">First seed</label><input type="number" id="f-seed0" min="0" value="0"></div>
<div class="field"><label for="f-iter">Max iterations</label><input type="number" id="f-iter" min="1" value="150"></div>
<div class="field"><label for="f-threads">Cores per run</label><select id="f-threads"><option>2</option><option selected>4</option><option>8</option><option>16</option></select></div></div></div>
<details><summary>Advanced settings (optional)</summary><div style="display:flex;flex-direction:column;gap:8px;margin-top:8px">
<div class="fl">One setting per line, <span class="mono">name = value</span>. Click a row below to add it.</div>
<textarea id="f-args" rows="4" class="mono" placeholder="mass_step_frac_f = 0.1&#10;ferrite_cost_per_kg = 2.5"></textarea>
<input type="search" id="f-filter" placeholder="search settings"><div class="ref"><table id="f-ref"></table></div></div></details>
<div class="summary" id="f-summary"></div><div class="err" id="f-err"></div>
<div class="row" style="justify-content:flex-end"><button value="cancel" formnovalidate>Cancel</button><button class="primary" id="f-go" value="default">Create runs</button></div>
</form></dialog>
<div class="toast" id="toast"></div>
<script>
const $=s=>document.querySelector(s), el=(t,a={},...c)=>{const e=document.createElement(t);for(const[k,v]of Object.entries(a)){if(k==='class')e.className=v;else if(k.startsWith('on'))e[k]=v;else if(v!=null)e.setAttribute(k,v);}for(const x of c.flat())if(x!=null)e.append(x.nodeType?x:document.createTextNode(String(x)));return e;};
let OV=null, filt='all', q='', timer=null, detailTimer=null, FIELDS=null;
const fmt=(v,d=1)=>v==null||!isFinite(v)?'–':(Math.abs(v)>=1e5?v.toExponential(2):(+v).toFixed(d));
const money=v=>v==null||!isFinite(v)?'–':'$'+(v<100?(+v).toFixed(2):Math.round(v).toLocaleString());
const ICON={running:'●',queued:'○',paused:'❚❚',cancelled:'■',done:'✓',failed:'✕',interrupted:'!',pausing:'…',cancelling:'…',resuming:'…',unknown:'?'};
const LABEL={interrupted:'interrupted (VM off)'};
const ACTIVE=new Set(['running','queued','pausing','cancelling','resuming','interrupted']);
function badge(s){return el('span',{class:'badge '+s},el('i',{},ICON[s]||'•'),LABEL[s]||s);}
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',3500);}
async function api(path,body){const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});let j;try{j=await r.json();}catch(e){throw new Error('bad response from the dashboard server ('+r.status+')');}if(!r.ok)throw new Error(j.error||r.statusText);return j;}
function ago(s){if(s==null)return 'never';s=Math.max(0,s);return s<90?Math.round(s)+' s ago':s<5400?Math.round(s/60)+' min ago':(s/3600).toFixed(1)+' h ago';}
async function control(keys,action){if(action==='cancel'&&!confirm(`Cancel ${keys.length} run(s)? They stop now; results so far are kept.`))return;
  try{const r=await api('/api/control',{keys,action});toast(`${action}: ${r.done.length} run(s)`+(r.refused.length?`, ${r.refused.length} not controllable`:''));refresh();}catch(e){toast(e.message);}}
function actions(r,small=true){const b=(label,act,cls='')=>el('button',{class:(small?'small ':'')+cls,onclick:e=>{e.stopPropagation();control([r.key],act);}},label);
  if(!r.managed)return [el('span',{class:'faint',style:'font-size:12px'},'started outside the dashboard')];
  const s=r.state,out=[];
  if(['running','queued','interrupted','resuming'].includes(s))out.push(b('Pause','pause'));
  if(['paused','cancelled','failed'].includes(s))out.push(b(s==='paused'?'Resume':'Restart','resume'));
  if(!['done','cancelled','cancelling'].includes(s))out.push(b('Cancel','cancel','danger'));
  return out;}
async function refresh(){try{OV=await api('/api/overview');}catch(e){$('#updated').textContent='cannot reach blob storage: '+e.message;return;}
  renderTop();if(!location.hash)renderList();}
function renderTop(){const v=OV.vm,dot=$('#vmdot'),btn=$('#vmbtn');
  if(v.power==='disabled'){$('#vm').style.display='none';}
  else{const on=v.power==='VM running',busy=!!v.action||/starting|stopping|deallocating/i.test(v.power);
    dot.className='dot'+(on?' on':busy?' busy':'');
    $('#vmtext').textContent=v.action?('VM '+v.action+'…'):(v.power==='unknown'?'VM state unknown':v.power.replace('VM ','VM '));
    $('#vmtext').title=v.error||'';btn.style.display=busy?'none':'';btn.textContent=on?'Stop VM':'Start VM';
    btn.onclick=async()=>{if(on&&OV.runs.some(r=>r.state==='running')&&!confirm('Runs are in progress. Stop the VM anyway? They continue from their last iteration when it starts again.'))return;
      try{await api('/api/vm',{action:on?'stop':'start'});toast(on?'Stopping VM…':'Starting VM…');setTimeout(refresh,1500);}catch(e){toast(e.message);}};}
  const a=OV.agent;$('#agent').textContent=!a?'agent: not seen yet':OV.agent_alive?`agent: ${a.running.length} running, ${a.waiting} waiting, ${a.used}/${a.cores} cores · ${ago(OV.agent_age_s)}`:`agent offline (last seen ${ago(OV.agent_age_s)})`+(OV.pending?` · ${OV.pending} run(s) waiting for the VM`:'');
  $('#updated').textContent='updated '+new Date().toLocaleTimeString();}
function renderList(){clearInterval(detailTimer);const m=$('#main');m.innerHTML='';
  const tb=el('div',{class:'toolbar'});const seg=el('div',{class:'seg'});
  for(const[k,l]of[['all','All'],['active','In progress'],['done','Finished'],['stopped','Paused / cancelled']])seg.append(el('button',{class:filt===k?'on':'',onclick:()=>{filt=k;renderList();}},l));
  const search=el('input',{type:'search',placeholder:'search runs',value:q,oninput:e=>{q=e.target.value;renderList();setTimeout(()=>{const s=document.querySelector('input[type=search]');s&&s.focus();s&&s.setSelectionRange(q.length,q.length);},0);}});
  tb.append(seg,search);m.append(tb);
  let runs=OV.runs.filter(r=>(filt==='all'||(filt==='active'&&ACTIVE.has(r.state))||(filt==='done'&&r.state==='done')||(filt==='stopped'&&['paused','cancelled','failed'].includes(r.state)))&&(!q||(r.key+' '+r.init+' '+r.state).toLowerCase().includes(q.toLowerCase())));
  if(!OV.runs.length){m.append(el('div',{class:'empty'},el('p',{},'No runs in blob storage yet.'),el('button',{class:'primary',onclick:openNew},'+ Create the first runs')));return;}
  if(!runs.length){m.append(el('div',{class:'empty'},'No runs match.'));return;}
  const by={};for(const r of runs)(by[r.session]=by[r.session]||[]).push(r);
  for(const s of Object.keys(by).sort().reverse()){const rs=by[s],cnt={};for(const r of rs)cnt[r.state]=(cnt[r.state]||0)+1;
    const managed=rs.filter(r=>r.managed).map(r=>r.key);
    const h=el('div',{class:'session'},el('h2',{},s),el('span',{class:'muted'},`${rs.length} run${rs.length>1?'s':''} · `+Object.entries(cnt).map(([k,v])=>`${v} ${k}`).join(', ')));
    if(managed.length){const act=rs.filter(r=>r.managed&&ACTIVE.has(r.state)).map(r=>r.key),stp=rs.filter(r=>r.managed&&['paused','cancelled','failed'].includes(r.state)).map(r=>r.key);
      if(act.length)h.append(el('button',{class:'small',onclick:()=>control(act,'pause')},'Pause all'),el('button',{class:'small danger',onclick:()=>control(act,'cancel')},'Cancel all'));
      if(stp.length)h.append(el('button',{class:'small',onclick:()=>control(stp,'resume')},'Resume all'));}
    m.append(h);const g=el('div',{class:'grid'});
    rs.sort((a,b)=>(a.F??Infinity)-(b.F??Infinity));
    for(const r of rs){const pct=r.it!=null&&r.iter_max?Math.min(100,100*r.it/r.iter_max):0;
      g.append(el('div',{class:'card',onclick:()=>location.hash='run='+encodeURIComponent(r.key)},
        r.thumb?el('div',{class:'thumb'},el('img',{loading:'lazy',src:'/api/file?name='+encodeURIComponent(r.thumb),alt:''})):el('div',{class:'thumb none'},r.state==='queued'?'waiting to start':'no image yet'),
        el('div',{class:'body'},el('div',{class:'title'},el('b',{},r.name),badge(r.state)),
          el('div',{class:'muted',style:'font-size:12.5px'},(r.init||'')+(r.it!=null?` · iteration ${r.it}${r.iter_max?' / '+r.iter_max:''}`:'')+(r.state==='running'&&r.s_per_it?` · ${Math.round(r.s_per_it)} s/it`:'')),
          el('div',{class:'bar'},el('span',{style:`width:${pct}%`})),
          el('div',{class:'metrics'},el('div',{},el('small',{},'$ per good voxel'),el('b',{},money(r.F))),el('div',{},el('small',{},'good voxels'),el('b',{},fmt(r.N_green,0))),el('div',{},el('small',{},'centre field'),el('b',{},r.mean_mT!=null?fmt(r.mean_mT,1)+' mT':'–')),
            el('div',{},el('small',{},'iron'),el('b',{},r.iron_kg!=null?fmt(r.iron_kg,0)+' kg':'–')),el('div',{},el('small',{},'ferrite'),el('b',{},r.ferrite_kg!=null?fmt(r.ferrite_kg,0)+' kg':'–')),el('div',{},el('small',{},'running for'),el('b',{},r.elapsed_h!=null?fmt(r.elapsed_h,1)+' h':'–'))),
          el('div',{class:'actions'},actions(r)))));}
    m.append(g);}}
function lineChart(title,xs,ys,{log=false,target=null,unit='',digits=2}={}){
  const wrap=el('div',{class:'chart'},el('div',{class:'t'},title));const pts=xs.map((x,i)=>[x,ys[i]]).filter(p=>p[1]!=null&&isFinite(p[1])&&(!log||p[1]>0));
  if(pts.length<1){wrap.append(el('div',{class:'faint'},'no data'));return wrap;}
  const W=320,H=130,L=44,R=8,T=8,B=20,tv=v=>log?Math.log10(v):v;let y0=Math.min(...pts.map(p=>tv(p[1]))),y1=Math.max(...pts.map(p=>tv(p[1])));
  if(target!=null){y0=Math.min(y0,tv(target));y1=Math.max(y1,tv(target));}if(y1-y0<1e-12){y0-=1;y1+=1;}
  const x0=pts[0][0],x1=Math.max(pts[pts.length-1][0],x0+1),X=x=>L+(x-x0)/(x1-x0)*(W-L-R),Y=v=>T+(1-(tv(v)-y0)/(y1-y0))*(H-T-B);
  const ns='http://www.w3.org/2000/svg',S=(t,a)=>{const e=document.createElementNS(ns,t);for(const k in a)e.setAttribute(k,a[k]);return e;};
  const svg=S('svg',{viewBox:`0 0 ${W} ${H}`,preserveAspectRatio:'none',role:'img','aria-label':title});
  const lab=v=>log?('1e'+v.toFixed(1)):(Math.abs(v)>=1000?Math.round(v).toLocaleString():(+v).toFixed(digits));
  for(const v of[y0,y1]){svg.append(S('line',{x1:L,x2:W-R,y1:T+(1-(v-y0)/(y1-y0))*(H-T-B),y2:T+(1-(v-y0)/(y1-y0))*(H-T-B),stroke:'var(--line)','stroke-width':1}));
    const t=S('text',{x:L-6,y:T+(1-(v-y0)/(y1-y0))*(H-T-B)+4,'text-anchor':'end','font-size':10.5,fill:'var(--ink3)'});t.textContent=lab(v);svg.append(t);}
  const tx=S('text',{x:W-R,y:H-4,'text-anchor':'end','font-size':10.5,fill:'var(--ink3)'});tx.textContent='iteration '+x1;svg.append(tx);
  if(target!=null)svg.append(S('line',{x1:L,x2:W-R,y1:Y(target),y2:Y(target),stroke:'var(--ink3)','stroke-dasharray':'4 3','stroke-width':1}));
  svg.append(S('path',{d:pts.map((p,i)=>(i?'L':'M')+X(p[0]).toFixed(1)+' '+Y(p[1]).toFixed(1)).join(''),fill:'none',stroke:'var(--accent)','stroke-width':2,'stroke-linejoin':'round','vector-effect':'non-scaling-stroke'}));
  const last=pts[pts.length-1];svg.append(S('circle',{cx:X(last[0]),cy:Y(last[1]),r:3.5,fill:'var(--accent)',stroke:'var(--surface)','stroke-width':2}));
  const hair=S('line',{y1:T,y2:H-B,stroke:'var(--ink3)','stroke-width':1,visibility:'hidden'}),dot=S('circle',{r:4,fill:'var(--accent)',stroke:'var(--surface)','stroke-width':2,visibility:'hidden'});svg.append(hair,dot);
  const tip=el('div',{class:'tip'});wrap.append(svg,tip);
  svg.addEventListener('pointermove',e=>{const r=svg.getBoundingClientRect(),xv=x0+((e.clientX-r.left)/r.width*W-L)/(W-L-R)*(x1-x0);
    let best=pts[0];for(const p of pts)if(Math.abs(p[0]-xv)<Math.abs(best[0]-xv))best=p;
    hair.setAttribute('x1',X(best[0]));hair.setAttribute('x2',X(best[0]));hair.setAttribute('visibility','visible');dot.setAttribute('cx',X(best[0]));dot.setAttribute('cy',Y(best[1]));dot.setAttribute('visibility','visible');
    tip.textContent=`it ${best[0]}: ${Math.abs(best[1])>=1e5?best[1].toExponential(3):(+best[1]).toFixed(digits)}${unit}`;tip.style.left=(X(best[0])/W*r.width)+'px';tip.style.top=(Y(best[1])/H*r.height)+'px';tip.style.display='block';});
  svg.addEventListener('pointerleave',()=>{hair.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');tip.style.display='none';});
  return wrap;}
async function renderDetail(key){const m=$('#main');let d;try{d=await api('/api/run?key='+encodeURIComponent(key));}catch(e){m.innerHTML='';m.append(el('div',{class:'empty'},e.message));return;}
  const r=d.row||{key,name:key.split('/').pop(),session:key.split('/').slice(0,-1).join('/'),state:'unknown',managed:!!d.request};const h=d.history,its=(h.it||[]).map(Math.round);
  const keepIdx=document.querySelector('#sl')?+document.querySelector('#sl').value:null,wasLast=!document.querySelector('#sl')||+document.querySelector('#sl').value===+document.querySelector('#sl').max;
  m.innerHTML='';const det=el('div',{class:'detail'});m.append(det);
  det.append(el('div',{class:'head'},el('button',{onclick:()=>location.hash=''},'← All runs'),el('h2',{},r.name),el('span',{class:'muted'},r.session),badge(r.state),el('span',{class:'grow'}),...actions(r,false)));
  const DET={iter_max:'stopped at the iteration limit',converged:'converged','no descent step':'stopped: no further improvement found','by user':'by you','time budget':'stopped at its time budget'};
  if(r.detail)det.append(el('div',{class:'muted',style:'margin:-6px 0 12px'},DET[r.detail]||r.detail));
  const last=k=>h[k]&&h[k].length?h[k][h[k].length-1]:null,kpi=(t,v,s='')=>el('div',{class:'kpi'},el('small',{},t),el('b',{},v),el('span',{},s));
  const bestN=h.N_green?Math.max(...h.N_green.filter(v=>v!=null)):null;
  det.append(el('div',{class:'kpis'},kpi('$ per good voxel',money(last('F')),'cost / good voxels'),kpi('good voxels',fmt(last('N_green'),0),bestN!=null?'best '+fmt(bestN,0):''),
    kpi('centre field',last('blob_mean')!=null?fmt(last('blob_mean')*1e3,2)+' mT':'–','target 159.2 mT'),kpi('cost',money(last('cost')),`iron ${fmt(last('iron_kg'),0)} kg · ferrite ${fmt(last('ferrite_kg'),0)} kg`),
    kpi('iteration',r.it!=null?`${r.it}${r.iter_max?' / '+r.iter_max:''}`:'–',r.s_per_it?Math.round(r.s_per_it)+' s per iteration':''),kpi('demag fraction',fmt(last('demag_frac'),2),'ferrite past the gate')));
  if(its.length){const p=el('div',{class:'panel'},el('h3',{},'Iterations'));const sl=el('input',{type:'range',id:'sl',min:0,max:its.length-1,value:wasLast||keepIdx==null?its.length-1:Math.min(keepIdx,its.length-1)});
    const lab=el('span',{class:'muted mono'}),img=el('img',{id:'frame',alt:'slice through the magnet at y = 0'});let play=null;
    const show=()=>{const i=its[+sl.value];img.src='/api/file?name='+encodeURIComponent(`${key}/iter_${String(i).padStart(4,'0')}.png`);lab.textContent=`iteration ${i}`;};
    const pb=el('button',{class:'small',onclick:()=>{if(play){clearInterval(play);play=null;pb.textContent='▶ Play';return;}pb.textContent='❚❚ Pause';if(+sl.value>=its.length-1)sl.value=0;play=setInterval(()=>{if(+sl.value>=its.length-1){clearInterval(play);play=null;pb.textContent='▶ Play';return;}sl.value=+sl.value+1;show();},400);}},'▶ Play');
    sl.oninput=show;p.append(el('div',{class:'player'},pb,sl,lab),img);det.append(p);show();}
  const ch=el('div',{class:'charts'});
  if(its.length){ch.append(lineChart('$ per good voxel (log scale)',its,h.F,{log:true,unit:' $'}),lineChart('good voxels (log scale)',its,h.N_green,{log:true,digits:0}),
    lineChart('centre field [mT]',its,(h.blob_mean||[]).map(v=>v==null?null:v*1e3),{target:159.2,unit:' mT'}),lineChart('cost [$]',its,h.cost,{digits:0,unit:' $'}),
    lineChart('iron [kg]',its,h.iron_kg,{digits:0,unit:' kg'}),lineChart('ferrite [kg]',its,h.ferrite_kg,{digits:0,unit:' kg'}));
    det.append(el('div',{class:'panel'},el('h3',{},'History'),ch));}
  const vp=el('div',{class:'panel'},el('h3',{},'3D model'),el('div',{class:'muted',style:'font-size:12.5px;margin-bottom:8px'},'Iron grey · ferrite coloured by magnetization z (red up, white sideways, blue down) with arrows · green: largest usable imaging region · blue box: patient space, dashed: access corridor.'));
  const vbox=el('div');vp.append(el('div',{class:'actions',style:'margin-bottom:8px'},el('button',{class:'small',onclick:()=>{vbox.innerHTML='';vbox.append(el('iframe',{src:'/api/viewer?key='+encodeURIComponent(key)}));}},'Show last iteration'),
    el('button',{class:'small',onclick:()=>{vbox.innerHTML='';vbox.append(el('iframe',{src:'/api/viewer?all=1&key='+encodeURIComponent(key)}));}},'Show all iterations (slower)')),vbox);det.append(vp);
  const args=(d.request&&d.request.spec&&d.request.spec.args)||{};const kv=el('table',{class:'kv'});
  if(d.request)kv.append(el('tr',{},el('td',{},'mesh'),el('td',{},d.request.spec.script.replace('run_','').replace('.py',''))),el('tr',{},el('td',{},'cores'),el('td',{},d.request.spec.threads)),el('tr',{},el('td',{},'created'),el('td',{},d.request.created)));
  for(const[k,v]of Object.entries(args))kv.append(el('tr',{},el('td',{},k),el('td',{class:'mono'},JSON.stringify(v))));
  det.append(el('div',{class:'panel'},el('h3',{},'Settings'),kv,d.config?el('details',{style:'margin-top:8px'},el('summary',{},'all settings of this run'),el('pre',{},JSON.stringify(d.config,null,1))):null));
  if(d.agent_log)det.append(el('div',{class:'panel'},el('h3',{},'Error output'),el('pre',{},d.agent_log)));
  const lg=el('pre',{},d.log||'no log yet');det.append(el('div',{class:'panel'},el('h3',{},'Log (end)'),lg));lg.scrollTop=lg.scrollHeight;
  clearInterval(detailTimer);if(ACTIVE.has(r.state))detailTimer=setInterval(()=>{if(location.hash==='#run='+encodeURIComponent(key)&&!document.querySelector('iframe'))renderDetail(key);},20000);}
function route(){const h=decodeURIComponent(location.hash.slice(1));if(h.startsWith('run='))renderDetail(h.slice(4));else if(OV)renderList();window.scrollTo(0,0);}
// ---- new runs ----
function starts(){const s=[...document.querySelectorAll('.hf:checked')].map(c=>'hframe:'+c.value),n=+$('#f-nseed').value||0,s0=+$('#f-seed0').value||0;for(let i=0;i<n;i++)s.push('noise:'+(s0+i));return s;}
function parseArgs(){const out={},bad=[];for(const line of $('#f-args').value.split('\n')){const t=line.replace(/#.*/,'').trim();if(!t)continue;const m=t.match(/^([A-Za-z_]\w*)\s*[=:]\s*(.+)$/);if(!m){bad.push(line);continue;}out[m[1]]=m[2].trim();}return[out,bad];}
function updSummary(){const st=starts(),th=+$('#f-threads').value,cores=(OV&&OV.agent&&OV.agent.cores)||16,par=Math.max(1,Math.floor(cores/th)),[a,bad]=parseArgs();
  $('#f-summary').textContent=st.length?`${st.length} run${st.length>1?'s':''} in session “${$('#f-session').value}”: ${st.map(s=>s.replace('hframe:','H-frame ').replace('noise:','seed ')).join(', ')}. Up to ${par} at a time on ${cores} cores.`+(Object.keys(a).length?` Extra settings: ${Object.keys(a).join(', ')}.`:''):'Choose at least one start design.';
  $('#f-err').textContent=bad.length?'Not understood: '+bad.join(' | '):'';}
async function openNew(){const d=new Date(),p=n=>String(n).padStart(2,'0');$('#f-session').value=`s${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;$('#f-err').textContent='';
  if(!FIELDS){try{FIELDS=await api('/api/fields');}catch(e){FIELDS=[];}renderRef();}updSummary();$('#dlg').showModal();}
function renderRef(){const t=$('#f-ref'),f=($('#f-filter').value||'').toLowerCase();t.innerHTML='';const skip=new Set(['results_dir','threads','resume','it_offset','blob_url','time_budget_h','run_history','init','iter_max']);
  for(const x of FIELDS){if(skip.has(x.name)||(f&&!(x.name+' '+x.help).toLowerCase().includes(f)))continue;
    t.append(el('tr',{onclick:()=>{const ta=$('#f-args');ta.value=(ta.value&&!ta.value.endsWith('\n')?ta.value+'\n':ta.value)+`${x.name} = ${x.default}`;updSummary();ta.focus();}},el('td',{class:'mono'},x.name),el('td',{class:'mono faint'},String(x.default)),el('td',{class:'muted'},x.help)));}}
$('#newbtn').onclick=openNew;$('#f-filter').oninput=renderRef;for(const s of['#f-session','#f-nseed','#f-seed0','#f-threads','#f-args'])$(s).addEventListener('input',updSummary);document.querySelectorAll('.hf').forEach(c=>c.onchange=updSummary);
$('#newform').addEventListener('submit',async e=>{if(e.submitter&&e.submitter.value==='cancel')return;e.preventDefault();const[a,bad]=parseArgs();if(bad.length){$('#f-err').textContent='Not understood: '+bad.join(' | ');return;}
  const go=$('#f-go');go.disabled=true;try{const r=await api('/api/runs',{session:$('#f-session').value.trim(),script:$('#f-mesh').value,threads:+$('#f-threads').value,iter_max:+$('#f-iter').value,starts:starts(),args:a});
    $('#dlg').close();toast(`Created ${r.created.length} run(s)`+(r.skipped.length?`, ${r.skipped.length} already existed`:'')+((OV&&OV.vm.power!=='VM running'&&OV.vm.power!=='disabled')?' · the VM starts automatically':''));location.hash='';refresh();}
  catch(err){$('#f-err').textContent=err.message;}finally{go.disabled=false;}});
window.addEventListener('hashchange',route);
refresh().then(route);timer=setInterval(refresh,15000);
</script></body></html>
"""


def default_url():
    if os.environ.get("MRIYOKE_BLOB_URL"):
        return os.environ["MRIYOKE_BLOB_URL"]
    if not shutil.which("az"):
        sys.exit("give --url (container URL) or install the Azure CLI and run `az login`")
    sub = subprocess.run(["az", "account", "show", "--query", "id", "-o", "tsv"], capture_output=True, text=True)
    if sub.returncode:
        sys.exit("run `az login` first")
    return f"https://mriyoke{sub.stdout.strip().replace('-', '')[:12]}.blob.core.windows.net/results"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--url", default="")
    ap.add_argument("--rg", default=os.environ.get("RG", "mriyoke-rg"))
    ap.add_argument("--vm", default=os.environ.get("VM", "mriyoke-f16"))
    ap.add_argument("--no-vm", action="store_true")
    ap.add_argument("--no-auto-start", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    url = a.url or default_url()
    cloud = Cloud(url, a.rg, a.vm, use_vm=not a.no_vm, auto_start=not a.no_auto_start)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", a.port), make_handler(cloud))
    srv.daemon_threads = True
    link = f"http://localhost:{a.port}/"
    print(f"cloud dashboard on {link}  (data: {url.split('?')[0]})", flush=True)
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(link)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
