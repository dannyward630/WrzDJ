#!/usr/bin/env bash
# Regression for #809 (offending 7f6eb254): cancellation must abort before the upgrade starts
# destructive work, while the EXIT cleanup trap still removes containers.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
upgrade="$root/deploy/scripts/upgrade-postgres-volume.sh"
compose_file="$root/deploy/docker-compose.yml"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

run_signal_case() { # SIGNAL EXPECTED_STATUS
  local signal=$1 expected_status=$2 case_dir="$scratch/$1"
  mkdir -p "$case_dir/bin"
  cat >"$case_dir/bin/docker" <<'DOCKER'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"

if [ "$1" = compose ]; then
  shift
  command_line="$*"
  case "$command_line" in
    *'run --detach'*) echo fake-db; exit 0 ;;
    *'run --rm'*'--entrypoint cat'*) echo 16; exit 0 ;;
    *'run --rm'*'echo "$PG_MAJOR"'*) echo 18; exit 0 ;;
    *'run --rm'*) exit 3 ;;
    *'ps --services'*) exit 0 ;;
    *'stop'*)
      : >"$FAKE_DOCKER_READY"
      printf 'signal=%s pid=%s ppid=%s\n' "$FAKE_SIGNAL" "$$" "$PPID" >>"$FAKE_DOCKER_LOG"
      kill -"$FAKE_SIGNAL" "$PPID"
      exit 0
      ;;
  esac
fi

case "$1" in
  inspect)
    if [[ "$*" == *'.Mounts'* ]]; then echo "${COMPOSE_PROJECT_NAME}_postgres_data"; else
      printf '%s\n' 'POSTGRES_USER=wrzdj' 'POSTGRES_DB=wrzdj'
    fi
    ;;
  pull) ;;
  run)
    if [[ "$*" == *'du -sk /v'* ]]; then echo 1; elif [[ "$*" == *'df -Pk /v'* ]]; then echo 100000; fi
    ;;
  volume)
    [ "$2" = inspect ] && exit 1
    ;;
  rm) ;;
esac
DOCKER
  chmod +x "$case_dir/bin/docker"

  export COMPOSE_PROJECT_NAME="wrzdj-signal-${signal,,}"
  export POSTGRES_UPGRADE_BACKUP_DIR="$case_dir/backups"
  export FAKE_DOCKER_LOG="$case_dir/docker.log"
  export FAKE_DOCKER_READY="$case_dir/ready"
  export FAKE_SIGNAL="$signal"
  # shellcheck disable=SC2016 # Perl receives the quoted signal-reset program.
  PATH="$case_dir/bin:$PATH" timeout --kill-after=2s 15s setsid perl -e \
    '$SIG{INT} = "DEFAULT"; $SIG{TERM} = "DEFAULT"; exec @ARGV or die $!' \
    -- bash "$upgrade" "$compose_file" >"$case_dir/upgrade.log" 2>&1 &
  local pid=$! status
  for ((attempt = 0; attempt < 100; attempt++)); do
    [ -e "$FAKE_DOCKER_READY" ] && break
    sleep 0.05
  done
  [ -e "$FAKE_DOCKER_READY" ] || { echo "$signal case never reached the signal boundary" >&2; return 1; }
  set +e
  wait "$pid"
  status=$?
  set -e
  if [ "$status" != "$expected_status" ]; then
    echo "$signal exited $status, expected $expected_status" >&2
    cat "$case_dir/upgrade.log" >&2
    cat "$FAKE_DOCKER_LOG" >&2
    return 1
  fi
  grep -q 'Interrupted.' "$case_dir/upgrade.log"
  grep -q 'rm --force' "$FAKE_DOCKER_LOG"
  ! grep -q '^run --detach --name' "$FAKE_DOCKER_LOG"
}

run_signal_case INT 130
run_signal_case TERM 143
echo 'INT and TERM abort before destructive work and retain EXIT cleanup.'
