#!/usr/bin/env bash
# mylittleclaude curl|bash bootstrap.
#
# Tiny: clone repo (or pull if it exists), checkout target tag, exec install.sh.
# All install logic lives in install.sh — this script never duplicates it.

set -euo pipefail
IFS=$'\n\t'

REPO_URL="${MYLITTLECLAUDE_REPO:-https://github.com/yozha/mylittleclaude.git}"
INSTALL_DIR="${MYLITTLECLAUDE_DIR:-$HOME/mylittleclaude}"
TARGET_TAG="${MYLITTLECLAUDE_TAG:-}"
TARGET_BRANCH="${MYLITTLECLAUDE_BRANCH:-}"

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_CYAN=$'\033[36m'
else
    C_RESET=""; C_RED=""; C_GREEN=""; C_CYAN=""
fi
say()  { printf '%s%s%s\n' "$C_CYAN" "$*" "$C_RESET"; }
good() { printf '%s%s%s\n' "$C_GREEN" "$*" "$C_RESET"; }
err()  { printf '%s%s%s\n' "$C_RED" "$*" "$C_RESET" >&2; }
fail() { err "$*"; exit 1; }

[[ "$EUID" -ne 0 ]] || fail "Run as the user that will own the bot, not as root."

command -v git >/dev/null 2>&1 || fail "git not installed. Install git and re-run."

say "mylittleclaude bootstrap"
say "  Repo:        $REPO_URL"
say "  Install dir: $INSTALL_DIR"

if [[ -d "$INSTALL_DIR/.git" ]]; then
    say "  (existing clone — pulling)"
    git -C "$INSTALL_DIR" fetch --all --tags
elif [[ -d "$INSTALL_DIR" ]]; then
    fail "$INSTALL_DIR exists and is not a git checkout; refusing to overwrite."
else
    say "  cloning..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

if [[ -n "$TARGET_BRANCH" ]]; then
    say "  checking out branch $TARGET_BRANCH"
    git checkout "$TARGET_BRANCH"
    git pull --ff-only origin "$TARGET_BRANCH"
else
    if [[ -z "$TARGET_TAG" ]]; then
        TARGET_TAG=$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -n 1 || true)
    fi
    if [[ -z "$TARGET_TAG" ]]; then
        say "  (no release tags found — staying on default branch HEAD)"
    else
        say "  checking out tag $TARGET_TAG"
        git checkout "$TARGET_TAG"
    fi
fi

chmod +x "$INSTALL_DIR/install.sh"
good "Repository ready; handing off to install.sh"
exec "$INSTALL_DIR/install.sh"
