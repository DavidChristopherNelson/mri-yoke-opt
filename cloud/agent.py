"""VM agent (systemd unit mriyoke-agent). Runs the optimisation runs requested in blob storage by the cloud
dashboard, applies pause / resume / cancel, reports state and a heartbeat, and deallocates the VM after
AGENT_IDLE_MIN minutes without work (0 = never), so compute billing stops. See mriyoke/control.py for the protocol.

Each run is `scripts/<script> results_dir=results/<session>/<run> threads=<n> <args...>` with auto-resume and blob
persistence, so a paused, evicted or interrupted run continues from its last uploaded iteration. Pause and cancel
stop the process at once (the iteration in progress is lost, the checkpoint of the previous one is kept).

Environment: MRIYOKE_BLOB_URL (required), AGENT_POLL (s, default 20), AGENT_IDLE_MIN (default 15), AGENT_CORES."""
import json, os, signal, socket, subprocess, sys, time, urllib.request
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from mriyoke.persist import blob_url, container
from mriyoke.control import CTRL, SCRIPTS, RESERVED, Store, now, runnable

POLL = float(os.environ.get("AGENT_POLL", 20))
IDLE_MIN = float(os.environ.get("AGENT_IDLE_MIN", 15))
CORES = int(os.environ.get("AGENT_CORES", 0)) or os.cpu_count()
MAX_RETRIES = 2
IMDS = "http://169.254.169.254/metadata/"


def log(s):
    print(f"{time.strftime('%H:%M:%S')} {s}", flush=True)


def deallocate():
    """Deallocate this VM through its managed identity (Virtual Machine Contributor on its resource group)."""
    h = {"Metadata": "true"}
    tok = json.load(urllib.request.urlopen(urllib.request.Request(
        IMDS + "identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/", headers=h)))["access_token"]
    rid = urllib.request.urlopen(urllib.request.Request(
        IMDS + "instance/compute/resourceId?api-version=2021-02-01&format=text", headers=h)).read().decode()
    urllib.request.urlopen(urllib.request.Request(f"https://management.azure.com{rid}/deallocate?api-version=2024-07-01",
                                                  data=b"", method="POST", headers={"Authorization": "Bearer " + tok}))


class Agent:
    def __init__(self):
        url = blob_url()
        if not url:
            sys.exit("MRIYOKE_BLOB_URL is not set")
        self.c = container(url)
        self.store = Store(self.c)
        self.procs = {}                                   # key -> dict(p, threads, stopping, since, log)
        self.states = self.store.states()                 # the agent is the only writer: keep them in memory
        self.idle_since = time.time()
        self.host = socket.gethostname()

    def set_state(self, key, **kw):
        st = dict(self.states.get(key) or {})
        st.update(kw, updated=now())
        self.states[key] = st
        self.store.put(f"{CTRL}/state/{key}.json", st)

    def used(self):
        return sum(p["threads"] for p in self.procs.values())

    def start(self, key, req):
        spec = req.get("spec", {})
        script, threads = spec.get("script", "run_coarse.py"), min(int(spec.get("threads", 4)), CORES)
        args = {k: v for k, v in (spec.get("args") or {}).items() if k not in RESERVED}
        if script not in SCRIPTS:
            self.set_state(key, state="failed", gen=req.get("gen", 0), detail=f"unknown script {script}"); return
        d = os.path.join("results", key)
        os.makedirs(os.path.join(ROOT, d), exist_ok=True)
        cmd = [sys.executable, os.path.join(ROOT, "scripts", script), f"results_dir={d}", f"threads={threads}"] \
            + [f"{k}={v}" for k, v in args.items()]
        out = open(os.path.join(ROOT, d, "agent_stdout.txt"), "a")
        env = dict(os.environ, MRIYOKE_AUTO_RESUME="1", PYTHONUNBUFFERED="1")
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        self.procs[key] = dict(p=p, threads=threads, stopping=None, since=None, log=out)
        retries = (self.states.get(key) or {}).get("retries", 0) if (self.states.get(key) or {}).get("gen") == req.get("gen") else 0
        self.set_state(key, state="running", gen=req.get("gen", 0), detail=f"{threads} threads", retries=retries,
                       started=now(), host=self.host)
        log(f"start {key}: {' '.join(cmd[1:])}")

    def stop(self, key, action):
        pr = self.procs[key]
        if pr["stopping"] is None:
            pr["stopping"], pr["since"] = action, time.time()
            os.killpg(pr["p"].pid, signal.SIGTERM)
            log(f"{action} {key}")
        elif time.time() - pr["since"] > 20:
            os.killpg(pr["p"].pid, signal.SIGKILL)

    def reap(self, key, req):
        pr = self.procs[key]
        rc = pr["p"].poll()
        if rc is None:
            return
        pr["log"].close()
        del self.procs[key]
        gen = (req or {}).get("gen", 0)
        if pr["stopping"]:
            self.set_state(key, state="paused" if pr["stopping"] == "pause" else "cancelled", gen=gen, detail="by user")
            return
        try:
            status = json.load(open(os.path.join(ROOT, "results", key, "status.json"))).get("state", "")
        except (OSError, ValueError):
            status = ""
        if rc == 0:
            self.set_state(key, state="done", gen=gen, detail=status.replace("finished: ", "") or "finished")
            log(f"done {key}: {status}")
            return
        tail = ""
        try:
            with open(os.path.join(ROOT, "results", key, "agent_stdout.txt"), "rb") as f:
                f.seek(0, 2); f.seek(max(0, f.tell() - 20000)); tail = f.read()
            self.c.upload_blob(f"{key}/agent.log", tail, overwrite=True)
        except OSError:
            pass
        retries = (self.states.get(key) or {}).get("retries", 0) + 1
        if retries > MAX_RETRIES:
            self.set_state(key, state="failed", gen=gen, retries=retries, detail=f"exit code {rc}, see agent.log")
        else:
            self.set_state(key, state="queued", gen=gen, retries=retries, detail=f"exit code {rc}, retry {retries}")
        log(f"exit {key}: rc={rc}, retries={retries}")

    def step(self):
        reqs = self.store.requests()
        for key in list(self.procs):
            self.reap(key, reqs.get(key))
        for key, req in reqs.items():                     # apply pause / cancel
            if req.get("desired") not in ("pause", "cancel"):
                continue
            if key in self.procs:
                self.stop(key, req["desired"])
                continue
            st = self.states.get(key) or {}
            if st.get("state") != "done" and (st.get("state") not in ("paused", "cancelled") or st.get("gen", 0) < req.get("gen", 0)):
                self.set_state(key, state="paused" if req["desired"] == "pause" else "cancelled", gen=req.get("gen", 0), detail="by user")
        waiting = sorted((r.get("created", ""), k) for k, r in reqs.items()
                         if k not in self.procs and runnable(r, self.states.get(k)))
        n_wait = 0
        for _, key in waiting:
            req = reqs[key]
            need = min(int(req.get("spec", {}).get("threads", 4)), CORES)
            if self.used() + need <= CORES:
                self.start(key, req)
            else:
                n_wait += 1
                st = self.states.get(key) or {}
                if st.get("state") != "queued" or st.get("gen", 0) != req.get("gen", 0):
                    self.set_state(key, state="queued", gen=req.get("gen", 0), detail="waiting for free cores")
        busy = bool(self.procs) or n_wait > 0
        if busy:
            self.idle_since = time.time()
        idle = (time.time() - self.idle_since) / 60
        self.store.put(f"{CTRL}/agent.json", dict(host=self.host, time=now(), cores=CORES, used=self.used(),
                                                  running=sorted(self.procs), waiting=n_wait,
                                                  idle_min=round(idle, 1), idle_limit_min=IDLE_MIN))
        if not busy and IDLE_MIN > 0 and idle >= IDLE_MIN:
            log(f"idle for {idle:.0f} min: deallocating the VM")
            self.store.put(f"{CTRL}/agent.json", dict(host=self.host, time=now(), cores=CORES, used=0, running=[],
                                                      waiting=0, idle_min=round(idle, 1), idle_limit_min=IDLE_MIN, note="deallocating"))
            try:
                deallocate()
            except Exception as e:
                log(f"deallocate failed: {e}")
            time.sleep(300)

    def loop(self):
        log(f"agent on {self.host}: {CORES} cores, poll {POLL} s, idle shutdown after {IDLE_MIN} min")
        while True:
            try:
                self.step()
            except Exception as e:                        # storage hiccup etc.: keep going
                log(f"step failed: {type(e).__name__}: {e}")
            time.sleep(POLL)


if __name__ == "__main__":
    Agent().loop()
