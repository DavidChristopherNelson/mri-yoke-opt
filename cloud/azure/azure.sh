#!/usr/bin/env bash
# Control script for running mri-yoke-opt on one Azure spot VM (default Standard_F16als_v7, eastus2).
# Runs on your laptop; needs the Azure CLI (`brew install azure-cli`, `az login`). See cloud/azure/README.md.
#
#   cloud/azure/azure.sh check                    account, VM size availability, spot vCPU quota
#   cloud/azure/azure.sh create                   create resource group + spot VM, wait for setup, smoke test
#   cloud/azure/azure.sh submit <args...>         start a job, e.g. scripts/multistart.py az1 --n 8 --batch 4 --hours 1000 iter_max=150
#   cloud/azure/azure.sh status                   power state, job state, tail of the job log, session table
#   cloud/azure/azure.sh watch <session>          loop: restart after eviction, pull results and deallocate when done
#   cloud/azure/azure.sh pull <session>           copy results/<session> to this machine (no .vtu)
#   cloud/azure/azure.sh dashboard                run the dashboard on the VM, tunnelled to http://localhost:8766/dashboard/
#   cloud/azure/azure.sh ssh [cmd]                shell on the VM
#   cloud/azure/azure.sh start | stop | delete    start / deallocate (stops compute billing) / delete everything
#
# Settings (environment variables): RG, LOC, VM, SIZE, IMAGE, DISK_GB, MAX_PRICE, DISK_CTRL, GRACE_MIN.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
RG=${RG:-mriyoke-rg}
LOC=${LOC:-eastus2}
VM=${VM:-mriyoke-f16}
SIZE=${SIZE:-Standard_F16als_v7}
IMAGE=${IMAGE:-Canonical:ubuntu-24_04-lts:server:latest}
DISK_GB=${DISK_GB:-64}
MAX_PRICE=${MAX_PRICE:--1}          # -1: pay the current spot price up to the on-demand price, never evicted for price
DISK_CTRL=${DISK_CTRL:-NVMe}        # v6/v7 AMD sizes use NVMe disks; set DISK_CTRL= (empty) if az rejects the flag
GRACE_MIN=${GRACE_MIN:-60}          # minutes the VM stays up after the job ends (to pull results) before it deallocates
U=azureuser

ip()    { az vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv; }
sshvm() { ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 "$U@$(ip)" "$@"; }
power() { az vm get-instance-view -g "$RG" -n "$VM" --query "instanceView.statuses[?starts_with(code,'PowerState/')].displayStatus | [0]" -o tsv; }
tag()   { az vm show -g "$RG" -n "$VM" --query "tags.mriyoke" -o tsv 2>/dev/null || true; }
pull()  { mkdir -p "$ROOT/results/$1"
          rsync -az --info=progress2 --exclude '*.vtu' --exclude 'frames.jsonl.gz' --exclude '*.tmp*' \
                -e "ssh -o StrictHostKeyChecking=accept-new" "$U@$(ip):mri-yoke-opt/results/$1/" "$ROOT/results/$1/"; }

cmd=${1:-help}; shift || true
case "$cmd" in
  check)
    az account show --query "{subscription:name, id:id, user:user.name}" -o table
    echo; echo "VM size in $LOC (empty or a 'NotAvailableForSubscription' restriction = not usable):"
    az vm list-skus -l "$LOC" --size "$SIZE" --resource-type virtualMachines \
       --query "[].{name:name, restrictions:join(',', restrictions[].reasonCode)}" -o table
    echo; echo "Spot vCPU quota in $LOC (needs limit >= 16):"
    az vm list-usage -l "$LOC" --query "[?contains(name.localizedValue,'Spot') || contains(name.localizedValue,'Low-priority')].{name:name.localizedValue, used:currentValue, limit:limit}" -o table
    ;;
  create)
    az group create -n "$RG" -l "$LOC" -o none
    scope=$(az group show -n "$RG" --query id -o tsv)
    extra=(); [ -n "$DISK_CTRL" ] && extra+=(--disk-controller-type "$DISK_CTRL")
    az vm create -g "$RG" -n "$VM" -l "$LOC" --size "$SIZE" --image "$IMAGE" \
      --priority Spot --eviction-policy Deallocate --max-price "$MAX_PRICE" \
      --admin-username "$U" --generate-ssh-keys --custom-data "$HERE/cloud-init.yaml" \
      --os-disk-size-gb "$DISK_GB" --storage-sku StandardSSD_LRS --public-ip-sku Standard \
      --assign-identity '[system]' --role "Virtual Machine Contributor" --scope "$scope" \
      --tags mriyoke=idle ${extra[@]+"${extra[@]}"} -o table
    echo "waiting for first-boot setup (packages + NGSolve, ~5-10 min)..."
    sleep 30
    sshvm 'cloud-init status --wait; tail -n 3 /var/log/cloud-init-output.log'
    sshvm 'cd mri-yoke-opt && .venv/bin/python -c "import ngsolve, scipy, skimage, matplotlib, os; print(\"ngsolve\", ngsolve.__version__, \"cores\", os.cpu_count())" && .venv/bin/python scripts/test_forward.py | tail -3'
    echo "VM ready: $(ip)"
    ;;
  submit)
    [ $# -gt 0 ] || { echo "usage: azure.sh submit scripts/multistart.py <session> [options] [key=value ...]"; exit 1; }
    f=$(mktemp); printf 'JOB=%q\nGRACE_MIN=%q\n' "$*" "$GRACE_MIN" > "$f"
    scp -q -o StrictHostKeyChecking=accept-new "$f" "$U@$(ip):job.env"; rm -f "$f"
    az vm update -g "$RG" -n "$VM" --set tags.mriyoke=running -o none
    sshvm 'rm -f ~/job.done ~/job.failcount; cd mri-yoke-opt && git pull -q --ff-only && sudo systemctl restart mriyoke-job && sleep 5 && systemctl is-active mriyoke-job && tail -n 3 ~/job.log'
    ;;
  status)
    echo "power: $(power)   tag: $(tag)"
    if [ "$(power)" = "VM running" ]; then
      sshvm 'echo "service: $(systemctl is-active mriyoke-job)"; [ -f ~/job.done ] && echo "done at $(cat ~/job.done)"; echo "--- job.log"; tail -n 8 ~/job.log | tr -d "\033" | sed "s/\[2J\[H//"; s=$(ls -td mri-yoke-opt/results/*/status.txt 2>/dev/null | head -1); [ -n "$s" ] && { echo "--- $s"; cat "$s"; }'
    fi
    ;;
  watch)
    session=${1:?usage: azure.sh watch <session>}
    while true; do
      p=$(power); t=$(tag); echo "$(date +%H:%M) $p, job $t"
      if [ "$p" = "VM deallocated" ] || [ "$p" = "VM stopped" ]; then
        if [ "$t" = done ] || [ "$t" = failed ]; then echo "job $t and VM deallocated; to fetch results: azure.sh start && azure.sh pull $session && azure.sh stop"; exit 0; fi
        echo "VM is off but the job is not finished (spot eviction): starting it again"
        az vm start -g "$RG" -n "$VM" -o none || echo "start failed (no spot capacity?); retrying in 10 min"
      elif [ "$p" = "VM running" ] && { [ "$t" = done ] || [ "$t" = failed ]; }; then
        echo "job $t: pulling results"; pull "$session"
        az vm deallocate -g "$RG" -n "$VM" -o none; echo "results in results/$session; VM deallocated"; exit 0
      fi
      sleep 600
    done
    ;;
  pull)      pull "${1:?usage: azure.sh pull <session>}" ;;
  dashboard) echo "open http://localhost:8766/dashboard/  (Ctrl-C to stop)"
             ssh -o StrictHostKeyChecking=accept-new -L 8766:localhost:8766 "$U@$(ip)" 'cd mri-yoke-opt && .venv/bin/python scripts/dashboard.py --serve 8766' ;;
  ssh)       sshvm "$@" ;;
  start)     az vm start -g "$RG" -n "$VM" -o none; echo "running: $(ip)" ;;
  stop)      az vm deallocate -g "$RG" -n "$VM" -o none; echo "deallocated (disk kept, compute billing stopped)" ;;
  delete)    read -r -p "delete resource group $RG with the VM, its disk and all results on it? [y/N] " a
             [ "$a" = y ] && az group delete -n "$RG" --yes --no-wait && echo "deleting $RG" ;;
  *)         sed -n '2,17p' "$0" ;;
esac
