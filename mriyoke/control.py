"""Control plane in blob storage, shared by the VM agent (cloud/agent.py) and the cloud dashboard
(scripts/cloud_dashboard.py). Nothing else talks between the laptop and the VM.

  _control/runs/<session>/<run>.json    request, written by the dashboard:
        {"spec": {"script": "run_coarse.py", "threads": 4, "args": {"init": "noise:3", "iter_max": 150, ...}},
         "desired": "run" | "pause" | "cancel", "gen": <int, +1 on every user action>, "created": ..., "updated": ...}
  _control/state/<session>/<run>.json   state, written by the agent only:
        {"state": "queued" | "running" | "paused" | "cancelled" | "done" | "failed", "gen": <request gen applied>,
         "detail": ..., "retries": ..., "updated": ...}
  _control/agent.json                    agent heartbeat: host, time, cores, running runs
  _control/limits.json                   spending cap, written by the dashboard: {"monthly_usd": 30 or null}
  _control/spend/<YYYY-MM>.json          compute spend this month, written by the agent:
        {"vm_hours": ..., "usd": ..., "price_per_hour": ..., "updated": ...}
Run data (status.json, history.csv, images, checkpoints) lives under <session>/<run>/ (mriyoke/persist.py)."""
import calendar, dataclasses, json, re, time
from .config import Config

CTRL = "_control"
SCRIPTS = ("run_coarse.py", "run_fine.py")
RESERVED = {"results_dir", "threads", "resume", "it_offset", "blob_url", "time_budget_h", "run_history"}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def age_s(stamp):
    try:
        return time.time() - calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def runnable(req, st):
    """Should the agent run this request now (given its last recorded state)?"""
    if not req or req.get("desired") != "run":
        return False
    state, gen = (st or {}).get("state"), (st or {}).get("gen", 0)
    if state in (None, "queued", "running"):
        return True                                  # new, or interrupted by an eviction / agent restart
    if state == "done":
        return False
    return req.get("gen", 0) > gen                   # paused / cancelled / failed, and the user asked again


def config_fields():
    """[(name, type name, default, comment)] of Config, from the dataclass and the comments in config.py."""
    import inspect
    from . import config as mod
    comments = {}
    for line in inspect.getsource(mod).splitlines():
        m = re.match(r"^\s+(\w+): \w+ = .*?#\s*(.*)$", line)
        if m:
            comments[m.group(1)] = m.group(2)
    return [(f.name, f.type if isinstance(f.type, str) else f.type.__name__, f.default, comments.get(f.name, ""))
            for f in dataclasses.fields(Config)]


def parse_args(args):
    """Validate and convert {name: value or string} against Config; raises ValueError."""
    types = {f.name: f.type for f in dataclasses.fields(Config)}
    out = {}
    for k, v in args.items():
        if k not in types:
            raise ValueError(f"unknown setting '{k}'")
        if k in RESERVED:
            raise ValueError(f"'{k}' is set by the system")
        t = types[k] if not isinstance(types[k], str) else {"float": float, "int": int, "str": str, "bool": bool}[types[k]]
        if t is bool:
            out[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        else:
            try:
                out[k] = t(v)
            except (TypeError, ValueError):
                raise ValueError(f"'{k}' must be {t.__name__}, got '{v}'")
    return out


class Store:
    """Small JSON objects in the container, with ETag-based caching of reads."""

    def __init__(self, container):
        self.c = container
        self._cache = {}                             # name -> (etag, obj)

    def get(self, name):
        try:
            return json.loads(self.c.download_blob(name).readall())
        except Exception:
            return None

    def put(self, name, obj):
        self.c.upload_blob(name, json.dumps(obj, indent=1).encode(), overwrite=True)

    def scan(self, prefix):
        """{key: obj} of every <prefix><key>.json, re-downloading only blobs whose ETag changed."""
        out, seen = {}, set()
        for b in self.c.list_blobs(name_starts_with=prefix):
            if not b.name.endswith(".json"):
                continue
            seen.add(b.name)
            hit = self._cache.get(b.name)
            if hit and hit[0] == b.etag:
                obj = hit[1]
            else:
                obj = self.get(b.name)
                self._cache[b.name] = (b.etag, obj)
            if obj is not None:
                out[b.name[len(prefix):-5]] = obj
        for n in [n for n in self._cache if n.startswith(prefix) and n not in seen]:
            del self._cache[n]
        return out

    def requests(self):
        return self.scan(f"{CTRL}/runs/")

    def states(self):
        return self.scan(f"{CTRL}/state/")


def month():
    return time.strftime("%Y-%m", time.gmtime())


def over_cap(limits, spend):
    cap = (limits or {}).get("monthly_usd")
    return cap is not None and (spend or {}).get("usd", 0.0) >= float(cap)
