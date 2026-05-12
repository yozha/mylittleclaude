#!/usr/bin/env bash
# mylittleclaude installer entrypoint.
#
# Layered approach: bash handles distro detection, prereq install (via sudo),
# venv creation, and systemd unit deployment. Once the venv exists we hand
# control to the Python wizard (`python -m mylittleclaude.installer`) for the
# interactive parts — see priv/INSTALLERS_SPEC.md §2.

set -euo pipefail
IFS=$'\n\t'

# --- config ---------------------------------------------------------------

INSTALL_DIR="${MYLITTLECLAUDE_DIR:-$HOME/mylittleclaude}"
PYTHON_BIN="${MYLITTLECLAUDE_PYTHON:-python3}"
NO_SUDO="${MYLITTLECLAUDE_NO_SUDO:-0}"
LOG_FILE="/tmp/mylittleclaude-install-$(date -u +%Y%m%d-%H%M%S).log"

# --- ansi -----------------------------------------------------------------

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_CYAN=$'\033[36m'
    C_BOLD=$'\033[1m'
else
    C_RESET="" ; C_RED="" ; C_GREEN="" ; C_YELLOW="" ; C_CYAN="" ; C_BOLD=""
fi

say()  { printf '%s%s%s\n' "$C_CYAN" "$*" "$C_RESET"; }
good() { printf '%s%s%s\n' "$C_GREEN" "$*" "$C_RESET"; }
warn() { printf '%s%s%s\n' "$C_YELLOW" "$*" "$C_RESET" >&2; }
err()  { printf '%s%s%s\n' "$C_RED" "$*" "$C_RESET" >&2; }
fail() { err "$*"; exit 1; }

ask_yes() {
    local prompt="$1" default="${2:-Y}"
    local hint
    if [[ "$default" =~ ^[Yy]$ ]]; then hint="[Y/n]"; else hint="[y/N]"; fi
    local reply
    if [[ -t 0 ]]; then
        read -r -p "$prompt $hint " reply || reply=""
    else
        reply=""
    fi
    [[ -z "$reply" ]] && reply="$default"
    [[ "$reply" =~ ^[Yy]$ ]]
}

on_error() {
    local lineno="$1" cmd="$2" rc="$3"
    {
        echo "FAILED at line $lineno (exit $rc): $cmd"
        echo "INSTALL_DIR=$INSTALL_DIR"
        echo "PYTHON_BIN=$PYTHON_BIN"
        env | sort
    } >"$LOG_FILE" 2>&1 || true
    err "Installation failed at line $lineno: $cmd (exit $rc)"
    err "Full log: $LOG_FILE"
    exit "$rc"
}
trap 'on_error $LINENO "$BASH_COMMAND" $?' ERR

# --- phases ---------------------------------------------------------------

phase_root_check() {
    if [[ "$EUID" -eq 0 ]]; then
        fail "Run as the user that will own the bot, not as root."
    fi
    if [[ "$NO_SUDO" != "1" ]]; then
        if ! command -v sudo >/dev/null 2>&1; then
            warn "sudo not found; falling back to --no-sudo mode."
            NO_SUDO=1
        elif ! sudo -n true 2>/dev/null; then
            say "(sudo may prompt for your password)"
        fi
    fi
}

phase_detect_distro() {
    if [[ ! -e /etc/os-release ]]; then
        fail "/etc/os-release missing; can't detect distro."
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}:${ID_LIKE:-}" in
        debian:*|ubuntu:*|*:*debian*|*:*ubuntu*)
            DISTRO_FAMILY="debian"
            PKG="apt-get"
            ;;
        fedora:*|rhel:*|rocky:*|almalinux:*|centos:*|*:*rhel*|*:*fedora*)
            DISTRO_FAMILY="rhel"
            PKG=$(command -v dnf >/dev/null 2>&1 && echo dnf || echo yum)
            ;;
        *)
            fail "Unsupported distro: ${PRETTY_NAME:-$ID}. See README's manual install appendix."
            ;;
    esac
    say "Detected: ${PRETTY_NAME:-$ID} (family: $DISTRO_FAMILY, pkg: $PKG)"
}

apt_install() {
    sudo "$PKG" update -y >/dev/null
    sudo "$PKG" install -y "$@"
}

dnf_install() {
    sudo "$PKG" install -y "$@"
}

pkg_install() {
    if [[ "$NO_SUDO" == "1" ]]; then
        warn "skipping install of $* (--no-sudo)"
        return 0
    fi
    if [[ "$DISTRO_FAMILY" == "debian" ]]; then
        apt_install "$@"
    else
        dnf_install "$@"
    fi
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1
}

phase_prereqs() {
    say "Checking system prereqs..."
    local missing=()

    # python 3.11+
    if ! need_cmd "$PYTHON_BIN" || ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
        if [[ "$DISTRO_FAMILY" == "debian" ]]; then
            missing+=(python3)
        else
            missing+=(python3.11)
        fi
    fi
    # venv module
    if ! "$PYTHON_BIN" -c 'import venv' 2>/dev/null; then
        [[ "$DISTRO_FAMILY" == "debian" ]] && missing+=(python3-venv)
    fi
    need_cmd git    || missing+=(git)
    need_cmd curl   || missing+=(curl)
    need_cmd rsync  || missing+=(rsync)
    need_cmd ssh    || missing+=( $( [[ "$DISTRO_FAMILY" == "debian" ]] && echo openssh-client || echo openssh-clients ) )
    # node 20+
    if ! need_cmd node || ! node --version | grep -Eq '^v(2[0-9]|[3-9][0-9])\.'; then
        missing+=(nodejs)
    fi
    need_cmd npm    || missing+=(npm)

    if (( ${#missing[@]} == 0 )); then
        good "All prereqs present."
        return 0
    fi

    say "Missing packages: ${missing[*]}"
    if ask_yes "Install with $PKG?" Y; then
        pkg_install "${missing[@]}"
    else
        fail "Aborting — install the missing packages and re-run."
    fi
}

phase_claude_code() {
    if need_cmd claude && claude --version >/dev/null 2>&1; then
        good "Claude Code already installed: $(claude --version)"
        return 0
    fi

    say "Installing @anthropic-ai/claude-code via npm..."
    local prefix
    prefix=$(npm config get prefix 2>/dev/null || echo "")
    if [[ "$prefix" != "$HOME"* ]]; then
        warn "npm prefix is $prefix — switching to ~/.npm-global to avoid sudo."
        npm config set prefix "$HOME/.npm-global"
        case ":$PATH:" in
            *":$HOME/.npm-global/bin:"*) : ;;
            *)
                warn "Add this to your shell rc:"
                warn "    export PATH=\"\$HOME/.npm-global/bin:\$PATH\""
                ;;
        esac
        export PATH="$HOME/.npm-global/bin:$PATH"
    fi
    npm install -g @anthropic-ai/claude-code
    need_cmd claude || fail "claude binary not on PATH after install; check ~/.npm-global/bin."
    good "Claude Code installed: $(claude --version || echo unknown)"
}

phase_claude_login() {
    if claude -p --output-format json "ping" </dev/null >/tmp/mlc-claude-test.$$ 2>&1; then
        if grep -q '"is_error":false' /tmp/mlc-claude-test.$$; then
            rm -f /tmp/mlc-claude-test.$$
            good "Claude Code is logged in."
            return 0
        fi
    fi
    rm -f /tmp/mlc-claude-test.$$

    cat <<EOF

${C_BOLD}Claude Code needs to be logged in.${C_RESET}

In a separate terminal on this machine, run:

    claude

…and complete the login flow. Then press Enter here. (Type 'skip' to defer —
the bot will install but prompts will fail until you authenticate.)

EOF
    local reply
    while true; do
        if [[ -t 0 ]]; then
            read -r -p "Press Enter when ready, or 'skip' / 'quit': " reply || reply="skip"
        else
            reply="skip"
        fi
        case "${reply,,}" in
            skip) warn "Claude login deferred."; return 0 ;;
            quit) fail "Aborted by operator." ;;
            *)
                if claude -p --output-format json "ping" </dev/null 2>/dev/null | grep -q '"is_error":false'; then
                    good "Claude Code is logged in."
                    return 0
                fi
                warn "Still failing. Try again or 'skip'."
                ;;
        esac
    done
}

phase_venv() {
    if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
        say "Creating venv at $INSTALL_DIR/.venv..."
        "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
    fi
    say "Installing project (pip install -e .)..."
    "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
    "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR"
}

phase_wizard() {
    say "Launching configuration wizard..."
    "$INSTALL_DIR/.venv/bin/python" -m mylittleclaude.installer reconfigure
}

phase_systemd() {
    local unit_src="$INSTALL_DIR/systemd/mylittleclaude.service"
    local unit_dst="/etc/systemd/system/mylittleclaude.service"

    if [[ "$NO_SUDO" == "1" ]]; then
        warn "Skipping systemd install (--no-sudo). Unit file is at: $unit_src"
        return 0
    fi
    if [[ ! -f "$unit_src" ]]; then
        warn "no unit file at $unit_src; skipping systemd."
        return 0
    fi

    say "Installing systemd unit..."
    # The unit ships with hard-coded paths; rewrite them for the operator's install.
    local tmp_unit
    tmp_unit=$(mktemp)
    sed \
        -e "s|^User=.*|User=$USER|" \
        -e "s|^WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" \
        -e "s|^EnvironmentFile=.*|EnvironmentFile=$INSTALL_DIR/.env|" \
        -e "s|^ExecStart=.*|ExecStart=$INSTALL_DIR/.venv/bin/python -m mylittleclaude|" \
        -e "s|^ReadWritePaths=.*|ReadWritePaths=$INSTALL_DIR $HOME/projects|" \
        "$unit_src" > "$tmp_unit"
    sudo install -m 0644 "$tmp_unit" "$unit_dst"
    rm -f "$tmp_unit"
    sudo systemctl daemon-reload
    sudo systemctl enable mylittleclaude
    good "systemd unit installed and enabled."

    if ask_yes "Start the service now?" Y; then
        if sudo systemctl start mylittleclaude; then
            sleep 2
            if systemctl is-active --quiet mylittleclaude; then
                good "Service is active."
            else
                warn "Service did not become active. Recent logs:"
                journalctl -u mylittleclaude -n 30 --no-pager || true
            fi
        else
            warn "Service start failed. Inspect with: journalctl -u mylittleclaude -n 50"
        fi
    fi
}

phase_setup_link() {
    local bin="$HOME/.local/bin"
    mkdir -p "$bin"
    local link="$bin/mylittleclaude-setup"
    local target="$INSTALL_DIR/.venv/bin/mylittleclaude-setup"
    if [[ ! -f "$target" ]]; then
        warn "$target not found; skipping symlink."
        return 0
    fi
    if [[ -L "$link" || -f "$link" ]]; then
        rm -f "$link"
    fi
    ln -s "$target" "$link"
    good "Setup command available at: $link"
    case ":$PATH:" in
        *":$bin:"*) ;;
        *)
            warn "Add to your shell rc to use 'mylittleclaude-setup' directly:"
            warn "    export PATH=\"\$HOME/.local/bin:\$PATH\""
            ;;
    esac
}

phase_summary() {
    cat <<EOF

${C_BOLD}=== Installed ===${C_RESET}

  Install dir:  $INSTALL_DIR
  Setup CLI:    ~/.local/bin/mylittleclaude-setup

  Useful commands:
    mylittleclaude-setup status     # service + config snapshot
    mylittleclaude-setup logs       # tail journalctl
    mylittleclaude-setup update     # update to the latest tag
    mylittleclaude-setup uninstall  # remove the bot

EOF
}

main() {
    say "${C_BOLD}mylittleclaude installer${C_RESET}"
    say "Install dir: $INSTALL_DIR"
    say

    [[ -d "$INSTALL_DIR" ]] || fail "Install dir not found: $INSTALL_DIR (run bootstrap.sh or git clone first)"

    phase_root_check
    phase_detect_distro
    phase_prereqs
    phase_claude_code
    phase_claude_login
    phase_venv
    phase_wizard
    phase_systemd
    phase_setup_link
    phase_summary
}

main "$@"
