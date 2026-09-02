#!/bin/sh
set -u

interval="${ADD_WATCHDOG_INTERVAL_SECONDS:-10}"
observations="${ADD_WATCHDOG_UNHEALTHY_OBSERVATIONS:-1}"
grace="${ADD_WATCHDOG_RESTART_GRACE_SECONDS:-30}"
cooldown="${ADD_WATCHDOG_RESTART_COOLDOWN_SECONDS:-60}"

case "$interval:$observations:$grace:$cooldown" in
  *[!0-9:]*|*::*|:*|*:) echo "Invalid ADD watchdog numeric configuration." >&2; exit 2 ;;
esac
if [ "$interval" -lt 5 ] || [ "$observations" -lt 1 ] || [ "$grace" -lt 5 ] ||
  [ "$cooldown" -lt 30 ]; then
  echo "ADD watchdog settings are below their safe minimums." >&2
  exit 2
fi

echo "ADD stack watchdog started."
while true; do
  touch /tmp/watchdog-heartbeat
  ids="$(
    timeout 10 docker ps --quiet \
      --filter 'label=com.docker.compose.project=attendance-device-dashboard' \
      --filter 'label=add.selfheal=true' 2>/dev/null
  )"
  docker_status=$?
  if [ "$docker_status" -ne 0 ]; then
    echo "ADD watchdog cannot query Docker; it will retry." >&2
    sleep "$interval"
    continue
  fi

  for container_id in $ids; do
    health="$(
      timeout 10 docker inspect \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$container_id" 2>/dev/null
    )"
    name="$(timeout 10 docker inspect --format '{{.Name}}' "$container_id" 2>/dev/null)"
    counter_file="/tmp/unhealthy-$container_id"
    restart_file="/tmp/restarted-$container_id"

    if [ "$health" != "unhealthy" ]; then
      rm -f "$counter_file"
      continue
    fi

    count=0
    if [ -f "$counter_file" ]; then
      count="$(cat "$counter_file" 2>/dev/null || echo 0)"
    fi
    count=$((count + 1))
    printf '%s' "$count" > "$counter_file"
    echo "ADD watchdog observed unhealthy container name=$name count=$count." >&2

    if [ "$count" -ge "$observations" ]; then
      now="$(date +%s)"
      last_restart=0
      if [ -f "$restart_file" ]; then
        last_restart="$(cat "$restart_file" 2>/dev/null || echo 0)"
      fi
      if [ "$((now - last_restart))" -lt "$cooldown" ]; then
        echo "ADD watchdog restart cooldown active name=$name." >&2
        continue
      fi
      echo "ADD watchdog restarting unhealthy container name=$name." >&2
      timeout "$((grace + 15))" docker restart --time "$grace" "$container_id" >/dev/null
      restart_status=$?
      if [ "$restart_status" -ne 0 ]; then
        echo "ADD watchdog restart failed name=$name exit_code=$restart_status." >&2
      else
        printf '%s' "$now" > "$restart_file"
      fi
      rm -f "$counter_file"
    fi
  done

  sleep "$interval"
done
