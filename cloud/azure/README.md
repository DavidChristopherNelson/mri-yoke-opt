# Running on an Azure spot VM (Standard_F16als_v7, eastus2)

One VM: 16 AMD Turin cores (no SMT), 32 GiB RAM, Ubuntu 24.04, spot price (~$0.18/h at the time of the 2026-10-04
price survey; on demand ~$0.97/h). The VM installs the repo itself on first boot, runs the job you submit as a
systemd service, continues by itself after a spot eviction once it is started again (every run resumes from its
last checkpoint), and deallocates itself when the job is done so compute billing stops.

What it costs while nothing runs: the 64 GB Standard SSD (~$5/month) and the public IP (~$4/month). `azure.sh delete`
removes everything.

## 1. One-time account setup (in a browser)

1. Azure account with a **pay-as-you-go** subscription (portal.azure.com → Subscriptions). Spot VMs are not
   available on the free-trial subscription, and the trial is capped at 4 vCPU; upgrading keeps any remaining credit.
2. Spot vCPU quota in **East US 2**: portal → *Quotas* → *Compute* → region *East US 2* → search "Spot"
   (shown as "Total Regional Spot vCPUs" or "Total Regional Low-priority vCPUs") → *New quota request* → **16**
   (or 176 if you will later move to HB176rs_v4). Spot VMs count only against this regional limit, not against the
   VM-family limits. Small increases are usually approved within minutes; larger ones can take a support ticket.

## 2. One-time laptop setup

```
brew install azure-cli
az login                                   # opens the browser
az account set --subscription "<subscription name or id>"   # only if you have several
cd "/Users/apple/Dropbox/2026/MRI Machine"
cloud/azure/azure.sh check                 # size available in eastus2 without restriction? spot quota >= 16?
```

`check` must show `Standard_F16als_v7` with an empty restrictions column and a spot quota limit of at least 16.
If the size is restricted for your subscription, try `LOC=westus3` or `SIZE=Standard_F16as_v7` (prefix every
later command with the same setting, e.g. `LOC=westus3 cloud/azure/azure.sh create`).

## 3. Create the VM

```
cloud/azure/azure.sh create
```

Creates resource group `mriyoke-rg`, the spot VM `mriyoke-f16` (eviction policy *Deallocate*: on eviction the
disk and all results are kept), an SSH key in `~/.ssh` if you have none, and a managed identity that lets the VM
tag and deallocate itself. It then waits for the first-boot setup (packages, `pip install -r requirements.txt`,
~5–10 min) and runs one forward solve as a smoke test; the last lines must show the NGSolve version, `cores 16`
and a converged solve.

If `az vm create` rejects `--disk-controller-type`, run `DISK_CTRL= cloud/azure/azure.sh create`.
If it fails with *SkuNotAvailable* / *capacity*, spot capacity is short in that region at the moment: retry later
or use another region (step 2).

## 4. Run a job

```
cloud/azure/azure.sh submit scripts/multistart.py az1 --n 8 --batch 4 --hours 1000 iter_max=150
```

Anything after `submit` is the command line of a script in `scripts/`, exactly as on the laptop (without
`.venv/bin/python` and without `caffeinate`). Examples:

```
cloud/azure/azure.sh submit scripts/multistart.py az1 --n 8 --batch 4 --hours 1000 iter_max=150      # 3 H-frames + 8 noise seeds
cloud/azure/azure.sh submit scripts/run_fine.py results_dir=results/az_fine init=noise:7 iter_max=250 # one fine-mesh run, all 16 cores
cloud/azure/azure.sh submit scripts/session.py az2 --hours 1000 --script run_fine.py "a:init=noise:2" "b:init=noise:7"
```

`--batch 4` runs 4 at a time with 4 threads each. Use a large `--hours`: the time budget restarts after every
eviction, and the VM stops itself when the job is done anyway. `submit` pulls the latest code from GitHub first,
so commit and push changes before submitting. Submitting again replaces the job (runs that already finished in
the same session directory are skipped, interrupted ones continue).

## 5. Follow it

```
cloud/azure/azure.sh status                # power state, job state, last log lines, live session table
cloud/azure/azure.sh dashboard             # then open http://localhost:8766/dashboard/ (Ctrl-C ends the tunnel)
cloud/azure/azure.sh pull az1              # copy results/az1 to the laptop at any time (without .vtu files)
cloud/azure/azure.sh ssh                   # shell on the VM; the job log is ~/job.log
```

## 6. Evictions and the end of the job

Azure evicts spot VMs with 30 s notice when it needs the capacity. The VM is then *deallocated* (no compute
charge), the disk is kept, and the job continues where it stopped once the VM runs again; at most the iteration
in progress is lost. Azure does not restart it by itself. Either start it by hand:

```
cloud/azure/azure.sh start
```

or leave this running on the laptop (it checks every 10 min, restarts the VM after an eviction, and at the end
pulls the results and deallocates the VM):

```
caffeinate -s -i cloud/azure/azure.sh watch az1
```

When the job ends the VM tags itself `mriyoke=done` (or `failed` after 3 crashes of the job command), waits
`GRACE_MIN` = 60 min so `watch` can pull the results, then deallocates itself. If you were not watching:

```
cloud/azure/azure.sh start && cloud/azure/azure.sh pull az1 && cloud/azure/azure.sh stop
```

## 7. Stop paying

```
cloud/azure/azure.sh stop                  # deallocate: compute billing stops, disk (~$5/month) and results stay
cloud/azure/azure.sh delete                # delete VM, disk, IP and results on the VM (pull first)
```

## Notes

- Nothing here has been run against a real Azure subscription yet (no Azure account was available when it was
  written); the parts most likely to need a tweak are the `az vm create` flags (`DISK_CTRL`) and the size name.
- A spot VM's price can change; `MAX_PRICE=-1` (default) means it is never evicted for price, only for capacity, and
  never costs more than on demand.
- Resume needs the same mesh, i.e. the same machine and NGSolve version; it is meant for continuing on the same VM.
  `requirements.txt` pins the versions used on the laptop.
- Files: `azure.sh` (laptop), `cloud-init.yaml` (first boot), `mriyoke-job.service` + `job.sh` (job runner on the VM).
