# Running on an Azure spot VM (Standard_F16als_v7, eastus2) with results in Azure Blob Storage

**What you get**
- One VM with 16 AMD Turin cores (no SMT), 32 GiB RAM, running Ubuntu 24.04, on spot pricing.
- Results stored in Azure Blob Storage, separate from the VM.

**How it works**
1. On first boot the VM installs the repo by itself.
2. It runs the job you submit as a service.
3. It uploads every iteration of every run to blob storage as it happens.
4. When the job is done, it switches itself off (deallocates).

A spot eviction loses at most the iteration in progress. Once the VM runs again, each run continues from its last
uploaded checkpoint; this works even on a brand-new VM. You download results to the laptop with
`azure.sh pull <session>`, at any time, including while the VM is off.

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

It then waits for the first-boot setup and runs two checks:
- One forward solve. It must print the NGSolve version, `cores 16` and a converged solve.
- A blob round trip. It must print `VM -> blob: ok` and `blob -> laptop: ok`. New role assignments can take a few
  minutes to apply; if the blob test fails, wait 5 minutes and run `cloud/azure/azure.sh blobtest`.

If something fails:
- `az vm create` rejects `--disk-controller-type`: run `DISK_CTRL= cloud/azure/azure.sh create`.
- *SkuNotAvailable* or a capacity error: spot capacity is short right now. Retry later or use another region.
- `az ad signed-in-user show` fails: give yourself the role by hand. Portal → the storage account → *Access control
  (IAM)* → *Add role assignment* → *Storage Blob Data Contributor* → your user.

## D. Run a job

Commit and push your code first: the VM pulls the latest code from GitHub on every submit. Then:

```
cloud/azure/azure.sh submit scripts/multistart.py az1 --n 8 --batch 4 --hours 1000 iter_max=150
```

Anything after `submit` is a command line for a script in `scripts/`, exactly as on the laptop (without
`.venv/bin/python` and `caffeinate`). Other examples:

```
cloud/azure/azure.sh submit scripts/multistart.py az0 --n 2 --batch 4 --hours 1000 iter_max=40       # cheap first test
cloud/azure/azure.sh submit scripts/run_fine.py results_dir=results/az_fine init=noise:7 iter_max=250 # one fine-mesh run, 16 cores
```

- `--batch 4` runs 4 at a time with 4 cores each.
- Use a large `--hours`: the time budget restarts after every eviction, and the VM stops itself when the job is done.
- Submitting again replaces the job. In the same session directory, finished runs are skipped and interrupted ones
  continue.

## E. Follow it and get the results

```
cloud/azure/azure.sh status            # power state, job state, last log lines, live session table
cloud/azure/azure.sh pull az1          # download results/az1 from blob storage (only new files), rebuild 3D viewers
.venv/bin/python scripts/dashboard.py --serve 8765    # then http://localhost:8765/dashboard/
cloud/azure/azure.sh dashboard         # or: live dashboard on the VM, http://localhost:8766/dashboard/
```

`pull` works whether the VM is on or off. You can run it repeatedly during a job; it only fetches what is new.

To browse files without downloading:
- **Azure Storage Explorer:** `brew install --cask microsoft-azure-storage-explorer`, sign in with the same account.
- **Portal:** storage account → *Containers* → `results`.

Neither includes the `.vtu` files unless you submit with `blob_vtu=1`.

## F. Evictions and the end of the job

Azure can evict a spot VM with 30 s notice. The VM is then deallocated: no compute charge, disk kept. Azure does not
restart it by itself. Either:

```
cloud/azure/azure.sh start                       # by hand, whenever you notice
caffeinate -s -i cloud/azure/azure.sh watch az1  # or keep this running on the laptop: checks every 10 min, restarts
                                                 # after an eviction, downloads everything when the job is done
```

When the job ends:
1. The VM uploads its job log to `results/_jobs/` in the container.
2. It tags itself `mriyoke=done` (or `failed`, after 3 crashes of the job command).
3. It deallocates itself straight away.

Nothing waits for you; the results are already in storage.

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
  emulator: upload of every iteration, and resume on a wiped disk from storage. Most likely to need a tweak: the
  `az vm create` flags (`DISK_CTRL`) and the VM size name.
- **`MAX_PRICE=-1` (default):** the VM is never evicted for price, only for capacity, and never costs more than on
  demand.
- **Resume:** the mesh is stored with the checkpoint (`mesh.pkl`), so a run resumed on another VM continues on exactly
  the same mesh.
- **Files:** `azure.sh` (laptop), `cloud-init.yaml` (first boot), `mriyoke-job.service` + `job.sh` (job runner on the
  VM), `mriyoke/persist.py` (uploads), `scripts/rebuild_viewer.py` (viewers from the stored frames).
