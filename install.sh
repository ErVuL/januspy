#!/usr/bin/env bash
#
# install.sh — set up januspy: the real-time JANUS modem (web UI + CLI).
#
# Workflow:  git clone --recursive  ->  ./install.sh  (needs internet)  ->  run offline.
# Installation downloads the C reference's submodule and pip dependencies; once installed,
# januspy runs fully offline (local binaries + local Python, no network).
#
# Idempotent. Steps:
#   1. check system build deps (lists any that are missing, then stops)
#   2. initialise the reference submodule if it wasn't cloned (--recursive)
#   3. build the CMRE C reference (third_party/reference/c) if not already built
#   4. create the Python venv (.venv) if missing
#   5. pip install -e the januspy package (uses pyproject.toml)
#   6. probe live-audio availability
#   7. smoke-test the install
#
# Usage:
#   ./install.sh                   # full setup
#   ./install.sh --skip-reference  # don't (re)build the C reference
#   ./install.sh --venv PATH       # use a different venv location
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
SKIP_REFERENCE=0
PYTHON="${PYTHON:-python3}"

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-reference) SKIP_REFERENCE=1; shift ;;
    --venv) VENV="$2"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx \033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. system dependencies -------------------------------------------------
say "Checking system dependencies"
pkg_check() { pkg-config --exists "$1" 2>/dev/null; }

# Required to build the reference + create the venv. Map missing -> apt package.
req_pkgs=()
command -v cmake >/dev/null 2>&1 || req_pkgs+=("cmake")
{ command -v gcc >/dev/null 2>&1 && command -v make >/dev/null 2>&1; } || req_pkgs+=("build-essential")
"$PYTHON" -m venv --help >/dev/null 2>&1 || req_pkgs+=("python3-venv")
if command -v pkg-config >/dev/null 2>&1; then
  pkg_check fftw3   || req_pkgs+=("libfftw3-dev")
  pkg_check sndfile || req_pkgs+=("libsndfile1-dev")
else
  req_pkgs+=("pkg-config" "libfftw3-dev" "libsndfile1-dev")
fi

if [ "${#req_pkgs[@]}" -gt 0 ]; then
  die "Missing build dependencies: ${req_pkgs[*]}
Install them, then re-run ./install.sh. On Debian/Ubuntu:
  sudo apt-get install ${req_pkgs[*]} libportaudio2
(libportaudio2 is optional — only needed for live mic/speaker audio.)"
fi
# PortAudio (for live sound-card I/O) is probed accurately after install, via sounddevice.

# --- 2. reference submodule -------------------------------------------------
if [ ! -f "$ROOT/third_party/reference/c/CMakeLists.txt" ]; then
  if [ -f "$ROOT/.gitmodules" ] && command -v git >/dev/null 2>&1; then
    say "Fetching the reference submodule (git submodule update --init --recursive)"
    git -C "$ROOT" submodule update --init --recursive || die "failed to fetch reference submodule"
  else
    die "reference/ is missing. Clone with submodules:  git clone --recursive <repo>"
  fi
fi

# --- 3. build the C reference ----------------------------------------------
REF_BIN="$ROOT/third_party/reference/c/local-install/bin"
if [ "$SKIP_REFERENCE" -eq 0 ]; then
  if [ -x "$REF_BIN/janus-tx" ] && [ -x "$REF_BIN/janus-rx" ]; then
    say "C reference already built ($REF_BIN)"
  else
    say "Building the CMRE JANUS C reference"
    cd "$ROOT/third_party/reference/c"
    mkdir -p build local-install
    ( cd build &&
      cmake -DCMAKE_INSTALL_PREFIX=../local-install -DCMAKE_BUILD_TYPE=Release .. &&
      make -j"$(nproc 2>/dev/null || echo 4)" &&
      make install ) || die "reference build failed"
    cd "$ROOT"
  fi
  [ -x "$REF_BIN/janus-tx" ] || die "janus-tx not built — check the build output above"
else
  say "Skipping reference build (--skip-reference)"
fi

# --- 4. python venv ---------------------------------------------------------
if [ -x "$VENV/bin/python" ]; then
  say "Using existing venv ($VENV)"
else
  say "Creating venv ($VENV)"
  "$PYTHON" -m venv "$VENV"
fi
VPY="$VENV/bin/python"
"$VPY" -m pip install --quiet --upgrade pip setuptools wheel

# --- 5. install januspy (editable, via pyproject.toml) ----------------------
say "Installing januspy (pip install -e)"
"$VPY" -m pip install -e "$ROOT"

# --- 6. probe live-audio availability (accurate: actually load PortAudio) ----
audio=$("$VPY" - <<'PY' 2>/dev/null || true
try:
    import sounddevice as sd
except Exception:
    print("NOLIB"); raise SystemExit
try:
    print("OK", len(sd.query_devices()))
except Exception:
    print("OK 0")
PY
)
case "$audio" in
  "OK 0") warn "PortAudio loaded but no audio devices were found — live capture/playback may not work here (file decode, software loopback, and the web UI still do)." ;;
  OK*)    say "Live audio ready (${audio#OK } devices)." ;;
  *)      warn "Live mic/speaker audio unavailable — install libportaudio2 (apt: libportaudio2) to enable it. File decode, software loopback, and the web UI still work without it." ;;
esac

# --- 7. smoke test ----------------------------------------------------------
say "Smoke test (software loopback through the reference)"
if [ -x "$REF_BIN/janus-tx" ]; then
  if "$VENV/bin/januspy" loopback "install.sh smoke test"; then
    say "OK"
  else
    warn "loopback smoke test did not decode — check the reference build"
  fi
else
  warn "reference not built; skipping smoke test"
fi

cat <<EOF

$(say "januspy installed — runs fully offline from here on.")
  Activate:   source "$VENV/bin/activate"
  Web UI:     januspy serve         # then open http://127.0.0.1:8000
  Decode mic: januspy rx
  Transmit:   januspy tx "Hello JANUS" --play
  Help:       januspy --help
EOF
