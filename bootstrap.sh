#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export GIT_TERMINAL_PROMPT=0   # fail instead of hanging on a credentials prompt

# ---- config ----
MANIFEST=dependencies.txt
SDK=sdk/ameba-rtos-pro2
TOOLCHAIN_URL="https://github.com/Ameba-AIoT/ameba-toolchain/releases/download/V10.4.1-ameba-rtos-pro2/asdk-10.4.1-linux-newlib-build-3768-x86_64.tar.bz2"
TOOLCHAIN_DEST=$SDK/tools
TOOLCHAIN_CACHE=tool_chain
FORCE=${FORCE:-0}   # FORCE=1 ./bootstrap.sh to reinstall the toolchain
# Our own examples, kept at the repo root and copied into the SDK after cloning
EXAMPLES=(hello_world)
EXAMPLE_DEST=$SDK/component/example

LINE="##########################################################"
step() {
  echo
  echo "$LINE"
  echo "==> $1"
  echo "$LINE"
}

trim() {
  local s=$1
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

command -v git  >/dev/null || { echo "git not found. Run: sudo apt install -y git"; exit 1; }
command -v curl >/dev/null || { echo "curl not found. Run: sudo apt install -y curl"; exit 1; }
command -v tar  >/dev/null || { echo "tar not found. Run: sudo apt install -y tar bzip2"; exit 1; }
[ -f "$MANIFEST" ] || { echo "Manifest not found: $MANIFEST"; exit 1; }

# ---- clone / update one repo ----
sync_repo() {
  local path=$1 url=$2 ref=$3 subs=$4
  if [ ! -d "$path/.git" ]; then
    mkdir -p "$(dirname "$path")"
    local args=(--progress -b "$ref")
    if [ "$subs" = "yes" ]; then args+=(--recursive); fi
    git clone "${args[@]}" "$url" "$path"
  else
    echo "    already cloned, updating"
    git -C "$path" fetch --tags --prune
    git -C "$path" checkout "$ref"
    # fast-forward only when ref is a branch (tags stay detached)
    if git -C "$path" show-ref --verify --quiet "refs/remotes/origin/$ref"; then
      git -C "$path" pull --ff-only
    fi
    if [ "$subs" = "yes" ]; then
      git -C "$path" submodule update --init --recursive
    fi
  fi
}

# ---- 1. Clone repositories from the manifest (in file order) ----
while IFS='|' read -r -u 3 path url ref subs; do
  path=$(trim "$path")
  [ -z "$path" ] && continue
  case $path in \#*) continue;; esac
  url=$(trim "$url"); ref=$(trim "$ref"); subs=$(trim "$subs")
  if [ -z "$url" ] || [ -z "$ref" ] || [ -z "$subs" ]; then
    echo "Bad manifest line (need 4 fields): $path" >&2; exit 1
  fi
  step "Repo: $path ($ref, submodules: $subs)"
  sync_repo "$path" "$url" "$ref" "$subs"
done 3< "$MANIFEST"

# ---- 2. Move test example -> SDK ----
# Copy (not move) so the originals stay tracked in this repo.
# Skipped if the example already exists in the SDK, so re-running never overwrites it.
step "Examples -> $EXAMPLE_DEST"
for ex in "${EXAMPLES[@]}"; do
  if [ -d "$EXAMPLE_DEST/$ex" ]; then
    echo "    $ex already in SDK, skipping"
    continue
  fi
  [ -d "$ex" ] || { echo "Example folder not found: $ex" >&2; exit 1; }
  cp -r "$ex" "$EXAMPLE_DEST/$ex"
  echo "    $ex -> $EXAMPLE_DEST/$ex  (build with -DEXAMPLE=$ex)"
done

# ---- 3. Setup toolchain ----
step "Toolchain"
ARCHIVE_NAME=${TOOLCHAIN_URL##*/}
ARCHIVE=$TOOLCHAIN_CACHE/$ARCHIVE_NAME
TOP_DIR=$(echo "$ARCHIVE_NAME" | sed -E 's/^(asdk-[0-9]+(\.[0-9]+)*)-.*/\1/')
BIN_DIR=$TOOLCHAIN_DEST/$TOP_DIR/linux/newlib/bin

if [ "$FORCE" != "1" ] && compgen -G "$BIN_DIR/*-gcc" >/dev/null; then
  echo "    $TOP_DIR already installed, skipping (FORCE=1 to reinstall)"
else
  mkdir -p "$TOOLCHAIN_CACHE" "$TOOLCHAIN_DEST"
  if [ -f "$ARCHIVE" ] && [ "$FORCE" != "1" ]; then
    echo "    using cached $ARCHIVE"
  else
    echo "    downloading $ARCHIVE_NAME"
    curl -fL -C - --retry 5 --retry-delay 5 --retry-all-errors --progress-bar \
      -o "$ARCHIVE.part" "$TOOLCHAIN_URL"
    mv "$ARCHIVE.part" "$ARCHIVE"
  fi
  echo "    extracting to $TOOLCHAIN_DEST (this takes a minute)"
  tar -jxf "$ARCHIVE" -C "$TOOLCHAIN_DEST"
fi

# ---- 4. Verify + cleanup ----
step "Verify"
GCC=$(compgen -G "$BIN_DIR/*-gcc" | head -n1 || true)
[ -n "$GCC" ] || { echo "Error: no compiler (*-gcc) found in $BIN_DIR" >&2; exit 1; }
echo "    OK: $(basename "$GCC")"

# Only clean up after verification
rm -f "$ARCHIVE"
rmdir "$TOOLCHAIN_CACHE" 2>/dev/null || true   # remove the cache folder if empty
for old in "$TOOLCHAIN_DEST"/asdk-*; do
  if [ -d "$old" ] && [ "$old" != "$TOOLCHAIN_DEST/$TOP_DIR" ]; then
    rm -rf "$old"
    echo "    removed old toolchain $old"
  fi
done

echo
echo "$LINE"
echo "==> Done. Add the toolchain to PATH:"
echo "    export PATH=$(pwd)/$BIN_DIR:\$PATH"
echo "$LINE"