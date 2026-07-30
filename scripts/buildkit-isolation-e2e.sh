#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

init_script_logging "buildkit-isolation-e2e"

output_dir="//tmp/devpush-buildkit-isolation"
cache_gc_storage=""
buildkit_proxy_url=""

cleanup() {
  local code=$?
  trap - EXIT
  if declare -p COMPOSE_BASE >/dev/null 2>&1; then
    "${COMPOSE_BASE[@]}" exec -T worker-jobs \
      rm -rf "$output_dir" >/dev/null 2>&1 || true
  fi
  exit "$code"
}
trap cleanup EXIT

verify_daemon_boundary() {
  local buildkit_id buildkit_networks buildkit_mounts buildkit_command
  local app_mounts worker_groups
  local egress_id egress_networks
  if ((VERBOSE == 1)); then set -x; fi
  buildkit_id="$("${COMPOSE_BASE[@]}" ps -q buildkitd)"
  [[ -n "$buildkit_id" ]]

  [[ "$(docker inspect --format '{{.Config.User}}' "$buildkit_id")" == "1000:1000" ]]
  [[ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$buildkit_id")" == "true" ]]
  [[ "$(docker inspect --format '{{.HostConfig.Privileged}}' "$buildkit_id")" == "false" ]]
  buildkit_command="$(docker inspect --format '{{json .Config.Cmd}}' "$buildkit_id")"
  grep -Fq -- '--oci-worker-gc-keepstorage' <<<"$buildkit_command"
  grep -Fq "$cache_gc_storage" <<<"$buildkit_command"

  buildkit_mounts="$(docker inspect --format '{{range .Mounts}}{{println .Destination}}{{end}}' "$buildkit_id")"
  ! grep -Fq '/var/run/docker.sock' <<<"$buildkit_mounts"

  buildkit_networks="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$buildkit_id")"
  [[ "$(wc -w <<<"$buildkit_networks")" == "1" ]]
  ! grep -Eq 'devpush_(default|internal|runner)' <<<"$buildkit_networks"
  [[ "$(docker network inspect "$buildkit_networks" --format '{{.Internal}}')" == "true" ]]

  egress_id="$("${COMPOSE_BASE[@]}" ps -q buildkit-egress)"
  [[ -n "$egress_id" ]]
  [[ "$(docker inspect --format '{{.Config.User}}' "$egress_id")" == "13:13" ]]
  [[ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$egress_id")" == "true" ]]
  [[ "$(docker inspect --format '{{.HostConfig.Privileged}}' "$egress_id")" == "false" ]]
  [[ "$(docker inspect --format '{{json .HostConfig.CapDrop}}' "$egress_id")" == '["ALL"]' ]]
  egress_networks="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$egress_id")"
  grep -Fq "$buildkit_networks" <<<"$egress_networks"
  ! grep -Eq 'devpush_(default|internal|runner)' <<<"$egress_networks"

  app_mounts="$(docker inspect --format '{{range .Mounts}}{{println .Destination}}{{end}}' "$("${COMPOSE_BASE[@]}" ps -q app)")"
  ! grep -Fq '/run/buildkit' <<<"$app_mounts"

  worker_groups="$("${COMPOSE_BASE[@]}" exec -T worker-jobs id -G)"
  grep -Eq '(^|[[:space:]])1000([[:space:]]|$)' <<<"$worker_groups"
  "${COMPOSE_BASE[@]}" exec -T worker-jobs sh -ec \
    'test "$DOCKER_CONFIG" = /tmp/layerrail-docker-config && mkdir -p "$DOCKER_CONFIG" && test -w "$DOCKER_CONFIG"'
  "${COMPOSE_BASE[@]}" exec -T worker-jobs \
    stat -c '%a %u %g' //run/buildkit/buildkitd.sock | grep -Fxq '660 1000 1000'
  "${COMPOSE_BASE[@]}" exec -T worker-jobs buildctl \
    --addr unix:///run/buildkit/buildkitd.sock debug workers >/dev/null
  if ((VERBOSE == 1)); then set +x; fi
}

verify_proxy_policy() {
  local status
  if ((VERBOSE == 1)); then set -x; fi
  status="$("${COMPOSE_BASE[@]}" exec -T worker-jobs \
    sh -ec 'curl -sS -w "\n%{http_code}\n" -X POST \
      http://docker-proxy:2375/build | tail -n 1')"
  [[ "$status" == "403" ]]
  if ((VERBOSE == 1)); then set +x; fi
}

run_isolation_build() {
  local app_id app_ip
  if ((VERBOSE == 1)); then set -x; fi
  app_id="$("${COMPOSE_BASE[@]}" ps -q app)"
  app_ip="$(docker inspect --format '{{(index .NetworkSettings.Networks "devpush_internal").IPAddress}}' "$app_id")"
  [[ -n "$app_ip" ]]
  [[ "$buildkit_proxy_url" =~ ^http://(10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+):3128$ ]]
  "${COMPOSE_BASE[@]}" exec -T worker-jobs rm -rf "$output_dir"
  "${COMPOSE_BASE[@]}" exec -T worker-jobs buildctl \
    --addr unix:///run/buildkit/buildkitd.sock build \
    --progress=plain \
    --no-cache \
    --frontend=dockerfile.v0 \
    --local context=//app/tests/fixtures/buildkit-isolation \
    --local dockerfile=//app/tests/fixtures/buildkit-isolation \
    --opt "build-arg:CONTROL_PLANE_IP=$app_ip" \
    --opt "build-arg:HTTP_PROXY=$buildkit_proxy_url" \
    --opt "build-arg:HTTPS_PROXY=$buildkit_proxy_url" \
    --opt "build-arg:http_proxy=$buildkit_proxy_url" \
    --opt "build-arg:https_proxy=$buildkit_proxy_url" \
    --opt build-arg:NO_PROXY=localhost,127.0.0.1,::1 \
    --opt build-arg:no_proxy=localhost,127.0.0.1,::1 \
    --output "type=local,dest=$output_dir"
  "${COMPOSE_BASE[@]}" exec -T worker-jobs \
    grep -Fxq 'buildkit-isolation-passed' "$output_dir/proof.txt"
  if ((VERBOSE == 1)); then set +x; fi
}

wait_for_buildkit() {
  local attempt buildkit_id status
  for attempt in $(seq 1 45); do
    buildkit_id="$("${COMPOSE_BASE[@]}" ps -q buildkitd)"
    if [[ -n "$buildkit_id" ]]; then
      status="$(docker inspect --format '{{.State.Status}}:{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$buildkit_id" 2>/dev/null || true)"
      if [[ "$status" == "running:healthy" ]]; then
        return 0
      fi
      case "$status" in
        exited:*|dead:*) break ;;
      esac
    fi
    sleep 2
  done

  "${COMPOSE_BASE[@]}" ps buildkit-volume-init buildkit-egress buildkitd >&2 || true
  "${COMPOSE_BASE[@]}" logs --no-color --tail 200 \
    buildkit-volume-init buildkit-egress buildkitd >&2 || true
  return 1
}

set_compose_base
cache_gc_storage="${BUILDKIT_CACHE_GC_STORAGE:-$(read_env_value "$ENV_FILE" BUILDKIT_CACHE_GC_STORAGE)}"
cache_gc_storage="${cache_gc_storage:-2048,10240,20480}"
buildkit_proxy_ip="${BUILDKIT_PROXY_IP:-$(read_env_value "$ENV_FILE" BUILDKIT_PROXY_IP)}"
buildkit_proxy_url="http://${buildkit_proxy_ip:-10.250.0.2}:3128"

run_cmd "Starting isolated BuildKit services..." \
  "${COMPOSE_BASE[@]}" up -d buildkit-egress buildkitd docker-proxy
run_cmd "Waiting for the rootless BuildKit daemon..." wait_for_buildkit
run_cmd "Starting the isolated jobs worker..." \
  "${COMPOSE_BASE[@]}" up -d --wait --wait-timeout 120 worker-jobs
run_cmd "Verifying BuildKit daemon boundary..." verify_daemon_boundary
run_cmd "Verifying host Docker build denial..." verify_proxy_policy
run_cmd "Blocking build-step control-plane and socket access..." run_isolation_build

printf "${GRN}BuildKit isolation E2E passed.${NC}\n"
