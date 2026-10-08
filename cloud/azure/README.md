# Running on an Azure spot VM (Standard_F16als_v7, eastus2) with results in Azure Blob Storage

**What you get**
- One VM with 16 AMD Turin cores (no SMT), 32 GiB RAM, running Ubuntu 24.04, on spot pricing.
- Results stored in Azure Blob Storage, separate from the VM.

**How it works**
1. On first boot the VM installs the repo by itself and starts an agent (`cloud/agent.py`).
2. You create, pause, resume and cancel runs in the **cloud dashboard** on your laptop (`azure.sh dashboard`). It
   reads results straight from blob storage and keeps nothing on disk.
3. The agent picks the runs up from blob storage, runs them several at a time, and uploads every iteration as it
   happens.
4. After 15 minutes with nothing to do, the agent switches the VM off (deallocates). While the dashboard is open it
   starts the VM again when runs are waiting, e.g. after a spot eviction.

A spot eviction loses at most the iteration in progress. Once the VM runs again, each run continues from its last
uploaded checkpoint; this works even on a brand-new VM.

**Rough costs** (check current prices; spot prices float)

| item | cost |
|---|---|
| VM, spot, while running | ~$0.18/h (on demand ~$0.97/h) |
| VM disk, 64 GB Standard SSD | ~$5/month, even while deallocated |
| public IP | ~$4/month, even while deallocated |
| blob storage | ~$0.02 per GB per month (a multi-start session is ~1–2 GB) |
| downloading to the laptop | first ~100 GB/month free, then ~$0.09/GB |

`azure.sh delete` removes the VM, disk and IP and keeps the results. `azure.sh delete-data` removes the results too.

---

## A. Create the Azure account (browser, once, ~15 min)

1. **Sign up:** go to <https://azure.microsoft.com/free> → *Start free*. Sign in with an existing Microsoft account
   or create one with your email.
   - You need a phone number for verification and a credit card. Nothing is charged while you stay on the free credit.
   - The free account gives ~$200 of credit for 30 days.
2. **Upgrade to pay-as-you-go:** spot VMs are not available on the free trial (and the trial is capped at 4 vCPU).
   - Portal <https://portal.azure.com> → search *Subscriptions* → your subscription → *Upgrade*.
   - Any remaining free credit is kept and used first.
   - Alternatively, sign up for pay-as-you-go directly at <https://azure.microsoft.com/pricing/purchase-options/pay-as-you-go>.
3. **Budget alert (recommended):** portal → search *Budgets* → *Add*.
   - Scope: your subscription. Amount: e.g. $30/month.
   - Alert at 50 % and 100 % to your email.
   - This only notifies you; it does not stop anything.
4. **Spot vCPU quota:** portal → search *Quotas* → *Compute* → filter region **East US 2**.
   - Search "Spot". It is shown as *Total Regional Spot vCPUs* or *Total Regional Low-priority vCPUs*.
   - Select it → *New quota request* → enter **16**. Enter 176 instead if you will later move to HB176rs_v4.
   - Spot VMs count only against this regional limit, not against the per-family limits.
   - Small requests are usually approved within minutes. If it opens a support request, it can take a day.

## B. Laptop tools (once)

```
brew install azure-cli azcopy
az login                                    # browser sign-in
az account list -o table                    # if you have several subscriptions:
az account set --subscription "<name or id>"
az provider register --namespace Microsoft.Compute --wait
az provider register --namespace Microsoft.Storage --wait
az provider register --namespace Microsoft.Network --wait
cd "/Users/apple/Dropbox/2026/MRI Machine"
.venv/bin/pip install -r requirements.txt   # adds the Azure storage packages used by the code
cloud/azure/azure.sh check
```

`check` must show:
- `Standard_F16als_v7` with an empty restrictions column;
- a spot vCPU limit of at least 16.

If the size is restricted, prefix every later command with another region or size, e.g.
`LOC=westus3 cloud/azure/azure.sh ...` or `SIZE=Standard_F16as_v7 cloud/azure/azure.sh ...`.

## C. Create storage and VM (once, ~10 min)

```
cloud/azure/azure.sh create
```

This creates:
- **Resource group `mriyoke-data`:** storage account `mriyoke<12 characters of your subscription id>` with a private
  container `results`. Account keys are switched off; access is by Azure login only.
- **Resource group `mriyoke-rg`:** the spot VM `mriyoke-f16`, with eviction policy *Deallocate*, an SSH key in
  `~/.ssh` (only if you don't have one), and a managed identity.
- **Permissions:**
  - you: *Storage Blob Data Contributor* on the storage account, to download;
  - the VM: the same role, to upload;
  - the VM: *Virtual Machine Contributor* on its own resource group, to switch itself off.

It then waits for the first-boot setup, starts the agent, and runs two checks:
- One forward solve. It must print the NGSolve version, `cores 16` and a converged solve.
- A blob round trip. It must print `VM -> blob: ok` and `blob -> laptop: ok`. New role assignments can take a few
  minutes to apply; if the blob test fails, wait 5 minutes and run `cloud/azure/azure.sh blobtest`.

If something fails:
- `az vm create` rejects `--disk-controller-type`: run `DISK_CTRL= cloud/azure/azure.sh create`.
- *SkuNotAvailable* or a capacity error: spot capacity is short right now. Retry later or use another region.
- `az ad signed-in-user show` fails: give yourself the role by hand. Portal → the storage account → *Access control
  (IAM)* → *Add role assignment* → *Storage Blob Data Contributor* → your user.

## D. Run, follow and control runs: the cloud dashboard

```
cloud/azure/azure.sh dashboard         # opens http://localhost:8770/ ; Ctrl-C in the terminal stops it
```

- **Overview:** every run in blob storage, grouped by session, with the latest slice image, state (queued, running,
  paused, cancelled, done, failed), progress, $ per good voxel, good voxels, centre field and masses. It refreshes
  every 15 s. Filters: in progress / finished / paused or cancelled; search box.
- **New runs:** button at the top right. Pick a session name, the start designs (3 H-frames and/or N noise seeds),
  mesh (coarse or fine), max iterations and cores per run. Any other setting goes under *Advanced settings* (one
  `name = value` per line; the searchable table lists every setting with its default and meaning). The summary line
  shows what will be created.
- **Pause / Resume / Cancel:** on each card, in the run's detail view, and for a whole session.
  - Pause and cancel stop the run within ~20 s. The iteration in progress is lost; everything up to it is kept.
  - Resume (or Restart, after a cancel or a failure) continues from the last iteration.
  - A failed run is retried twice automatically; its error output appears in the detail view.
- **Run detail** (click a card): key numbers; iteration player over all slice images; history charts (hover for
  values); the 3D model (last iteration, or all iterations); settings; end of the log.
- **VM:** state and Start / Stop button at the top. Stopping while runs are in progress pauses them; they continue
  when the VM starts again. The agent line shows how many runs are running and waiting, and the cores in use.

The agent runs as many runs at once as the cores allow (16 cores, 4 per run → 4 at a time); the rest wait, in the
order they were created. Runs started outside the dashboard (e.g. older multistart sessions) are shown read-only.

Commit and push code changes before creating runs: the agent pulls the latest code from GitHub when it starts.
To make a running VM pick up new code: `cloud/azure/azure.sh ssh sudo systemctl restart mriyoke-agent`. This
interrupts running runs; they resume by themselves.

Without the dashboard: `cloud/azure/azure.sh status` shows the VM state, the agent heartbeat and the agent's log.

## E. Copies on the laptop (optional)

The dashboard needs no local copy. To keep one anyway:

```
cloud/azure/azure.sh pull az1          # download results/az1 (only new files), rebuild 3D viewers
cloud/azure/azure.sh pull-all          # every session
```

To browse the raw files: Azure Storage Explorer (`brew install --cask microsoft-azure-storage-explorer`), or in the
portal under storage account → *Containers* → `results`.

## F. Evictions and idle shutdown

- **Spot eviction:** Azure can evict the VM with 30 s notice. It is then deallocated: no compute charge, disk kept.
  Azure does not restart it, but the dashboard does if it is open and runs are waiting (it checks every 30 s and
  starts the VM at most once per 10 min). Without the dashboard: `cloud/azure/azure.sh start`.
- **Idle shutdown:** the agent deallocates the VM after 15 minutes with no running or waiting runs (`AGENT_IDLE_MIN`
  in `~/agent.env` on the VM; 0 = never).
- Creating runs while the VM is off: the dashboard starts it, and the runs begin a few minutes later.

## G. Stop paying

```
cloud/azure/azure.sh stop          # deallocate now (if it is still running)
cloud/azure/azure.sh delete        # delete VM, disk and IP (~$9/month); the results in blob storage stay
cloud/azure/azure.sh delete-data   # delete the storage account and ALL results in it (pull first)
```

After `delete`, `cloud/azure/azure.sh create` builds a fresh VM. Submitting the same session continues every
unfinished run from blob storage.

---

**Notes**
- **Not tested on a real Azure subscription yet.** The code was tested against Azurite, the local Azure Storage
  emulator, with the agent running on the laptop: creating runs from the dashboard, upload of every iteration,
  pause / resume / cancel of running and waiting runs, resume on a wiped disk from storage, and the 3D viewer from
  stored frames. Not tested: VM start/stop and idle shutdown, which need Azure. Most likely to need a tweak: the
  `az vm create` flags (`DISK_CTRL`) and the VM size name.
- **`MAX_PRICE=-1` (default):** the VM is never evicted for price, only for capacity, and never costs more than on
  demand.
- **Resume:** the mesh is stored with the checkpoint (`mesh.pkl`), so a run resumed on another VM continues on exactly
  the same mesh.
- **Files:**
  - laptop: `azure.sh`, `scripts/cloud_dashboard.py`;
  - VM: `cloud-init.yaml` (first boot), `mriyoke-agent.service` + `cloud/agent.py` (runs the runs);
  - shared: `mriyoke/control.py` (request/state protocol in blob storage), `mriyoke/persist.py` (uploads),
    `scripts/rebuild_viewer.py` (viewers from the stored frames).
