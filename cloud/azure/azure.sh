#!/usr/bin/env bash
# Control script for running mri-yoke-opt on one Azure spot VM (default Standard_F16als_v7, eastus2).
# Runs on your laptop; needs the Azure CLI (`brew install azure-cli`, `az login`). See cloud/azure/README.md.
#
#   cloud/azure/azure.sh check                    account, VM size availability, spot vCPU quota
#   cloud/azure/azure.sh create                   storage account + container, spot VM, roles; wait for setup, smoke test
#   cloud/azure/azure.sh submit <args...>         start a job, e.g. scripts/multistart.py az1 --n 8 --batch 4 --hours 1000 iter_max=150
#   cloud/azure/azure.sh status                   power state, job state, tail of the job log, session table
#   cloud/azure/azure.sh watch <session>          loop: restart the VM after an eviction, pull results when done
#   cloud/azure/azure.sh pull <session>           download results/<session> from blob storage (azcopy sync), rebuild viewers
#   cloud/azure/azure.sh pull-all                 download every session in blob storage
#   cloud/azure/azure.sh dashboard                run the dashboard on the VM, tunnelled to http://localhost:8766/dashboard/
#   cloud/azure/azure.sh ssh [cmd]                shell on the VM
#   cloud/azure/azure.sh start | stop | delete    start / deallocate (stops compute billing) / delete the VM (results in
#                                                 blob storage are kept; delete-data removes them too)
#
# Settings (environment variables): RG, DATA_RG, SA, LOC, VM, SIZE, IMAGE, DISK_GB, MAX_PRICE, DISK_CTRL.
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
DATA_RG=${DATA_RG:-mriyoke-data}    # storage lives in its own resource group, so deleting the VM keeps the results
CONTAINER=results
U=azureuser
sa()    { [ -n "${SA:-}" ] && { echo "$SA"; return; }        # storage account name: globally unique, derived from the subscription
          echo "mriyoke$(az account show --query id -o tsv | tr -d '-' | cut -c1-12)"; }
burl()  { echo "https://$(sa).blob.core.windows.net/$CONTAINER"; }

ip()    { az vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv; }
sshvm() { ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 "$U@$(ip)" "$@"; }
power() { az vm get-instance-view -g "$RG" -n "$VM" --query "instanceView.statuses[?starts_with(code,'PowerState/')].displayStatus | [0]" -o tsv; }
tag()   { az vm show -g "$RG" -n "$VM" --query "tags.mriyoke" -o tsv 2>/dev/null || true; }
pull()  { mkdir -p "$ROOT/results/$1"
          command -v azcopy > /dev/null || { echo "azcopy missing: brew install azcopy"; exit 1; }
          AZCOPY_AUTO_LOGIN_TYPE=AZCLI azcopy sync "$(burl)/$1" "$ROOT/results/$1" --recursive --log-level ERROR \
            || { echo "azcopy failed; if it is an auth error run 'azcopy login' once and retry"; exit 1; }
          "$ROOT/.venv/bin/python" "$ROOT/scripts/rebuild_viewer.py" "$ROOT/results/$1"; }

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
    az group create -n "$DATA_RG" -l "$LOC" -o none
    az group create -n "$RG" -l "$LOC" -o none
    SA_NAME=$(sa)
    if ! az storage account show -n "$SA_NAME" -g "$DATA_RG" -o none 2>/dev/null; then
      echo "creating storage account $SA_NAME"
      az storage account create -n "$SA_NAME" -g "$DATA_RG" -l "$LOC" --sku Standard_LRS --kind StorageV2 \
        --min-tls-version TLS1_2 --allow-blob-public-access false --allow-shared-key-access false -o none
    fi
    az storage container-rm create --storage-account "$SA_NAME" -g "$DATA_RG" -n "$CONTAINER" -o none 2>/dev/null || true
    sa_id=$(az storage account show -n "$SA_NAME" -g "$DATA_RG" --query id -o tsv)
    me=$(az ad signed-in-user show --query id -o tsv)
    az role assignment create --assignee-object-id "$me" --assignee-principal-type User \
      --role "Storage Blob Data Contributor" --scope "$sa_id" -o none 2>/dev/null || true      # you: read/download
    scope=$(az group show -n "$RG" --query id -o tsv)
    extra=(); [ -n "$DISK_CTRL" ] && extra+=(--disk-controller-type "$DISK_CTRL")
    az vm create -g "$RG" -n "$VM" -l "$LOC" --size "$SIZE" --image "$IMAGE" \
      --priority Spot --eviction-policy Deallocate --max-price "$MAX_PRICE" \
      --admin-username "$U" --generate-ssh-keys --custom-data "$HERE/cloud-init.yaml" \
      --os-disk-size-gb "$DISK_GB" --storage-sku StandardSSD_LRS --public-ip-sku Standard \
      --assign-identity '[system]' --role "Virtual Machine Contributor" --scope "$scope" \
      --tags mriyoke=idle ${extra[@]+"${extra[@]}"} -o table
    vm_id=$(az vm show -g "$RG" -n "$VM" --query identity.principalId -o tsv)
    az role assignment create --assignee-object-id "$vm_id" --assignee-principal-type ServicePrincipal \
      --role "Storage Blob Data Contributor" --scope "$sa_id" -o none                     # VM: upload results
    echo "waiting for first-boot setup (packages + NGSolve, ~5-10 min)..."
    sleep 30
    sshvm 'cloud-init status --wait; tail -n 3 /var/log/cloud-init-output.log'
    sshvm 'cd mri-yoke-opt && .venv/bin/python -c "import ngsolve, scipy, skimage, matplotlib, azure.storage.blob, os; print(\"ngsolve\", ngsolve.__version__, \"cores\", os.cpu_count())" && .venv/bin/python scripts/test_forward.py | tail -3'
    echo "blob storage test (role assignments can take a few minutes to apply; rerun 'azure.sh blobtest' if this fails):"
    "$0" blobtest || true
    echo "VM ready: $(ip)   results go to $(burl)"
    ;;
  blobtest)
    sshvm "cd mri-yoke-opt && date > /tmp/hello.txt && MRIYOKE_BLOB_URL=$(burl) .venv/bin/python -m mriyoke.persist put /tmp/hello.txt _test/hello-from-vm.txt && echo 'VM -> blob: ok'"
    az storage blob show --account-name "$(sa)" -c "$CONTAINER" -n _test/hello-from-vm.txt --auth-mode login -o none 2>/dev/null \
      && echo "blob -> laptop: ok" || echo "blob -> laptop: failed (wait a few minutes for the role assignment, then retry)"
    ;;
  submit)
    [ $# -gt 0 ] || { echo "usage: azure.sh submit scripts/multistart.py <session> [options] [key=value ...]"; exit 1; }
    f=$(mktemp); printf 'JOB=%q\nMRIYOKE_BLOB_URL=%q\n' "$*" "$(burl)" > "$f"
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
      if [ "$t" = done ] || [ "$t" = failed ]; then
        echo "job $t: downloading results"; pull "$session"; echo "results in results/$session"; exit 0
      fi
      if [ "$p" = "VM deallocated" ] || [ "$p" = "VM stopped" ]; then
        echo "VM is off but the job is not finished (spot eviction): starting it again"
        az vm start -g "$RG" -n "$VM" -o none || echo "start failed (no spot capacity?); retrying in 10 min"
      fi
      sleep 600
    done
    ;;
  pull)      pull "${1:?usage: azure.sh pull <session>}" ;;
  pull-all)  for sdir in $(az storage blob list --account-name "$(sa)" -c "$CONTAINER" --auth-mode login --delimiter / --query "[].name" -o tsv); do
               sdir=${sdir%/}; [ "$sdir" = _test ] || [ "$sdir" = _jobs ] || pull "$sdir"; done ;;
  dashboard) echo "open http://localhost:8766/dashboard/  (Ctrl-C to stop)"
             ssh -o StrictHostKeyChecking=accept-new -L 8766:localhost:8766 "$U@$(ip)" 'cd mri-yoke-opt && .venv/bin/python scripts/dashboard.py --serve 8766' ;;
  ssh)       sshvm "$@" ;;
  start)     az vm start -g "$RG" -n "$VM" -o none; echo "running: $(ip)" ;;
  stop)      az vm deallocate -g "$RG" -n "$VM" -o none; echo "deallocated (disk kept, compute billing stopped)" ;;
  delete)    read -r -p "delete resource group $RG (VM, disk, IP)? Results in blob storage ($DATA_RG) are kept. [y/N] " a
             [ "$a" = y ] && az group delete -n "$RG" --yes --no-wait && echo "deleting $RG" ;;
  delete-data) read -r -p "delete resource group $DATA_RG with storage account $(sa) and ALL results in it? type yes: " a
             [ "$a" = yes ] && az group delete -n "$DATA_RG" --yes --no-wait && echo "deleting $DATA_RG" ;;
  *)         sed -n '2,16p' "$0" ;;
esac
