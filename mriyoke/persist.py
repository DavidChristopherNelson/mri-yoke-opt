"""Persist every iteration of a run to Azure Blob Storage, so nothing depends on the VM's disk surviving.

Layout under <container>/<run prefix>/ (run prefix = results_dir relative to results/, e.g. az1/noise3):
  config.json, mesh.pkl                       once at the start
  iter_NNNN.png, frames/NNNN.json.gz          per iteration, written once (immutable)
  ckpt/NNNN/{psi,psif,mdir}.npy               per-iteration checkpoint, written once
  history.csv, history.png, status.json, run.log   overwritten every iteration
  latest.json                                 {"it": N}, written LAST and only if that iteration's checkpoint uploaded
  final files (psi/psif/mdir_final.npy, mask_final.npy, final.json) at the end
Uploads run in one background thread in submission order, so the optimiser does not wait; data is captured in memory
when it is queued, so later overwrites of the local files cannot corrupt an upload. Failed uploads are retried with
backoff; the optimiser never fails because of storage.

Where: Config.blob_url or environment MRIYOKE_BLOB_URL = container URL, e.g. https://<account>.blob.core.windows.net/results.
Auth: DefaultAzureCredential (managed identity on the VM, `az login` on a laptop); a container URL with a SAS token
(…/results?sv=…) works without it; MRIYOKE_BLOB_CONN = connection string (e.g. the Azurite emulator, for tests).

CLI:  python -m mriyoke.persist put <local file> <blob name>"""
import gzip, io, json, os, queue, sys, threading, time
import numpy as np

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")


def blob_url(cfg=None):
    return (getattr(cfg, "blob_url", "") if cfg is not None else "") or os.environ.get("MRIYOKE_BLOB_URL", "")


def run_prefix(results_dir):
    rel = os.path.relpath(os.path.abspath(results_dir), RESULTS)
    return (os.path.basename(os.path.abspath(results_dir)) if rel.startswith("..") else rel).replace(os.sep, "/")


def container(url):
    from azure.storage.blob import ContainerClient
    conn = os.environ.get("MRIYOKE_BLOB_CONN")
    if conn:
        return ContainerClient.from_connection_string(conn, container_name=url.split("?")[0].rstrip("/").split("/")[-1])
    if "?" in url:                                             # SAS token in the URL
        return ContainerClient.from_container_url(url)
    from azure.identity import DefaultAzureCredential
    return ContainerClient.from_container_url(url, credential=DefaultAzureCredential(exclude_interactive_browser_credential=True))


class BlobSink:
    def __init__(self, url, prefix, log=print):
        self.c, self.prefix, self.log = container(url), prefix.strip("/"), log
        self.q = queue.Queue()
        self.bad = False                                       # an upload of the current iteration failed for good
        self.failures = 0
        threading.Thread(target=self._work, daemon=True).start()

    def name(self, rel):
        return f"{self.prefix}/{rel}" if self.prefix else rel

    # ---- queueing (data captured now) ----
    def put_bytes(self, rel, data, commit=False):
        self.q.put((rel, bytes(data), commit))

    def put_file(self, rel, path, commit=False):
        try:
            with open(path, "rb") as f:
                self.put_bytes(rel, f.read(), commit)
        except OSError:
            pass

    def put_array(self, rel, arr):
        b = io.BytesIO(); np.save(b, arr); self.put_bytes(rel, b.getvalue())

    def commit(self, rel, data):
        """Pointer object: uploaded only if every upload queued since the previous commit succeeded."""
        self.put_bytes(rel, data, commit=True)

    def _work(self):
        while True:
            rel, data, commit = self.q.get()
            try:
                if commit and self.bad:
                    self.log(f"    blob: {rel} not updated (an upload of this iteration failed)")
                else:
                    self._upload(rel, data)
            finally:
                if commit:
                    self.bad = False
                self.q.task_done()

    def _upload(self, rel, data):
        for attempt in range(8):
            try:
                self.c.upload_blob(self.name(rel), data, overwrite=True)
                return
            except Exception as e:                             # network, throttling, auth propagation, ...
                if attempt == 7:
                    self.bad = True; self.failures += 1
                    self.log(f"    blob: upload of {rel} failed for good: {type(e).__name__}: {str(e)[:200]}")
                    return
                time.sleep(min(60, 2 ** attempt))

    def flush(self):
        self.q.join()

    # ---- reading ----
    def get(self, rel):
        try:
            return self.c.download_blob(self.name(rel)).readall()
        except Exception:
            return None

    def list(self, sub):
        p = self.name(sub).rstrip("/") + "/"
        return [b.name[len(self.name("")):] if self.prefix else b.name for b in self.c.list_blobs(name_starts_with=p)]

    def fetch_resume(self, d):
        """Restore the local files a resume needs from the store into directory d. False if there is no checkpoint."""
        lj = self.get("latest.json")
        if lj is None:
            return False
        it = json.loads(lj)["it"]
        os.makedirs(d, exist_ok=True)
        for n in ("psi", "psif", "mdir"):
            b = self.get(f"ckpt/{it:04d}/{n}.npy")
            if b is None:
                return False
            with open(os.path.join(d, f"{n}_latest.npy"), "wb") as f:
                f.write(b)
        for rel in ("status.json", "history.csv", "run.log", "mesh.pkl", "config.json"):
            b = self.get(rel)
            if b is not None:
                with open(os.path.join(d, rel), "wb") as f:
                    f.write(b)
        with gzip.open(os.path.join(d, "frames.jsonl.gz"), "wt") as f:
            for rel in sorted(self.list("frames")):
                b = self.get(rel)
                if b is not None:
                    f.write(gzip.decompress(b).decode() + "\n")
        with open(os.path.join(d, "latest.json"), "wb") as f:    # last: marks the local copy complete
            f.write(lj)
        return True


def sink_for(cfg, log=print):
    url = blob_url(cfg)
    return BlobSink(url, run_prefix(cfg.results_dir), log) if url else None


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "put":
        s = BlobSink(blob_url(), "")
        s.put_file(sys.argv[3], sys.argv[2]); s.flush()
        sys.exit(1 if s.failures else 0)
    print(__doc__.split("CLI:")[1]); sys.exit(2)
