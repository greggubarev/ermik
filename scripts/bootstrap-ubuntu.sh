#!/usr/bin/env bash
# Prepare a dedicated Ubuntu controller host. Run --check first, then --apply as root.
set -euo pipefail

if [[ $# -ne 1 || ( $1 != --check && $1 != --apply ) ]]; then
  echo 'Usage: bootstrap-ubuntu.sh --check|--apply' >&2
  exit 1
fi
mode=$1
if [[ $mode == --apply && $(id -u) -ne 0 ]]; then
  echo 'Run --apply as root.' >&2
  exit 1
fi
if [[ ! -r /etc/os-release ]]; then
  echo 'Cannot identify the operating system.' >&2
  exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
if [[ ${ID:-} != ubuntu ]] || ! dpkg --compare-versions "${VERSION_ID:-0}" ge 24.04; then
  echo 'This script supports Ubuntu 24.04 or newer.' >&2
  exit 1
fi

controller_user=ermikctl
state_dir=/var/lib/ermik
workspace_dir=/srv/ermik/workspace

for path in "$state_dir" /srv/ermik "$workspace_dir"; do
  if [[ -L $path ]]; then
    echo "Refusing symlink: $path" >&2
    exit 1
  fi
  if [[ -e $path && ! -d $path ]]; then
    echo "Refusing non-directory path: $path" >&2
    exit 1
  fi
done
if getent passwd "$controller_user" >/dev/null; then
  if [[ $(id -u "$controller_user") -eq 0 ]]; then
    echo 'Controller account must not be root.' >&2
    exit 1
  fi
  if [[ $(getent passwd "$controller_user" | cut -d: -f6) != /home/ermikctl ]]; then
    echo 'Existing controller account has an unexpected home directory.' >&2
    exit 1
  fi
else
  for path in "$state_dir" /srv/ermik; do
    if [[ -e $path ]]; then
      echo "Refusing existing path before account creation: $path" >&2
      exit 1
    fi
  done
fi
for path in "$state_dir" "$workspace_dir"; do
  if [[ -e $path && $(stat -c %U "$path") != "$controller_user" ]]; then
    echo "Refusing path owned by another account: $path" >&2
    exit 1
  fi
done
if [[ -e /srv/ermik && $(stat -c %U /srv/ermik) != root ]]; then
  echo 'Refusing /srv/ermik owned by another account.' >&2
  exit 1
fi
if [[ -e /srv/ermik ]]; then
  parent_mode=$(stat -c %a /srv/ermik)
  if (( (8#$parent_mode & 8#022) != 0 )); then
    echo 'Refusing group- or world-writable /srv/ermik.' >&2
    exit 1
  fi
fi

for package in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
  if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
    echo "Existing conflicting package $package; inspect Docker's Ubuntu installation guide." >&2
    exit 1
  fi
done
if [[ $mode == --check ]]; then
  printf 'Host: %s %s\nController account: %s\nPrivate paths: %s, %s\n' "$ID" "$VERSION_ID" "$controller_user" "$state_dir" "$workspace_dir"
  for program in python3 git curl gh docker; do
    if command -v "$program" >/dev/null; then
      printf '%s: installed\n' "$program"
    else
      printf '%s: installation required\n' "$program"
    fi
  done
  echo 'Apply will install missing host packages, create the dedicated account and private paths, and enable linger for that account.'
  echo 'Docker daemon access for ermikctl still needs rootless Docker or a deliberate docker-group grant and must be verified as that account.'
  echo 'Apply will not authenticate, clone, configure or start polling.'
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 git ca-certificates curl

if ! command -v gh >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  key_file=$(mktemp)
  trap 'rm -f "$key_file"' EXIT
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$key_file"
  install -m 0644 "$key_file" /etc/apt/keyrings/githubcli-archive-keyring.gpg
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/github-cli.list
  apt-get update
  apt-get install -y gh
fi

if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11 or newer is required')
PY
if ! getent passwd "$controller_user" >/dev/null; then
  adduser --disabled-password --gecos '' "$controller_user"
fi
if [[ ! -e /srv/ermik ]]; then
  install -d -o root -g root -m 0755 /srv/ermik
fi
install -d -o "$controller_user" -g "$controller_user" -m 0700 "$state_dir" "$workspace_dir"
loginctl enable-linger "$controller_user"

echo 'Host packages and controller account have been prepared.'
echo 'Set up rootless Docker or deliberately grant docker-group access, then verify docker info as ermikctl.'
echo 'Continue with controller-account authentication, configuration, image build and systemd preflight in docs/setup.md.'
