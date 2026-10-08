#!/usr/bin/env bash
# Runs on the VM (systemd unit mriyoke-job). Executes $JOB from ~/job.env with auto-resume, so after a spot
# eviction + restart every run continues from its last checkpoint and finished runs are skipped.
# When the job is done (or failed 3 times) it tags the VM mriyoke=done|failed and, after GRACE_MIN minutes (time for
# `azure.sh watch` / `azure.sh pull` to copy the results), deallocates the VM itself so compute billing stops.
set -u
cd "$HOME/mri-yoke-opt"
[ -f "$HOME/job.env" ] || { echo "no ~/job.env: nothing to run"; exit 0; }
[ -f "$HOME/job.done" ] && { echo "job already done: $(cat "$HOME/job.done")"; exit 0; }
# shellcheck disable=SC1091
source "$HOME/job.env"                      # JOB="scripts/multistart.py ..."  GRACE_MIN=60
export MRIYOKE_AUTO_RESUME=1 PYTHONUNBUFFERED=1

imds() { curl -s -H Metadata:true "http://169.254.169.254/metadata/$1"; }
arm() {                                     # arm METHOD URL-SUFFIX [JSON]  (managed identity of the VM)
  local tok id
  tok=$(imds "identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/" | jq -r .access_token)
  id=$(imds "instance/compute/resourceId?api-version=2021-02-01&format=text")
  if [ -n "${3:-}" ]; then
    curl -s -X "$1" -H "Authorization: Bearer $tok" -H "Content-Type: application/json" -d "$3" "https://management.azure.com${id}$2"
  else
    curl -s -X "$1" -H "Authorization: Bearer $tok" -H "Content-Length: 0" "https://management.azure.com${id}$2"
  fi
}
finish() {                                  # finish done|failed
  echo "$(date -u +%FT%TZ) job $1" | tee -a "$HOME/job.log"
  [ "$1" = done ] && date -u +%FT%TZ > "$HOME/job.done"
  arm PATCH "?api-version=2024-07-01" "{\"tags\":{\"mriyoke\":\"$1\"}}" > /dev/null
  echo "deallocating in ${GRACE_MIN:-60} min" | tee -a "$HOME/job.log"
  sleep $(( ${GRACE_MIN:-60} * 60 ))
  arm POST "/deallocate?api-version=2024-07-01" > /dev/null
}

echo "$(date -u +%FT%TZ) start: $JOB" >> "$HOME/job.log"
# shellcheck disable=SC2086
.venv/bin/python $JOB >> "$HOME/job.log" 2>&1
rc=$?
if [ $rc -eq 0 ]; then
  rm -f "$HOME/job.failcount"
  finish done
  exit 0
fi
n=$(( $(cat "$HOME/job.failcount" 2>/dev/null || echo 0) + 1 )); echo $n > "$HOME/job.failcount"
echo "$(date -u +%FT%TZ) job exited with $rc (failure $n of 3)" >> "$HOME/job.log"
if [ $n -ge 3 ]; then finish failed; exit 0; fi
exit $rc                                    # systemd restarts it in 60 s
