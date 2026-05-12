#!/usr/bin/env bash
# mylittleclaude installer entrypoint.
#
# Discipline (v0.2.1+):
#   - Every check is a *functional* probe (does X actually work?), not a
#     presence probe (is X on PATH?). v0.2.0 failed silently when `command -v
#     claude` succeeded against a working install but a second `command -v`
#     was the wrong check.
#   - Every non-zero exit writes /tmp/mylittleclaude-install-<ts>.log. The
#     log captures the failing command, the env, and the last 50 lines of
#     session output (we tee everything through $SESSION_LOG).
#   - Operator-driven aborts ('quit', 'skip', or refusing an install) are
#     distinct from script-bail. Aborts: return 0, log nothing. Bails: trip
#     the trap, write the /tmp log, exit non-zero.

set -euo pipefail
IFS=$'\n\t'

# --- config ---------------------------------------------------------------

INSTALL_DIR="${MYLITTLECLAUDE_DIR:-$HOME/mylittleclaude}"
PYTHON_BIN="${MYLITTLECLAUDE_PYTHON:-python3}"
NO_SUDO="${MYLITTLECLAUDE_NO_SUDO:-0}"
NPM_GLOBAL_BIN="$HOME/.npm-global/bin"
LOG_FILE="/tmp/mylittleclaude-install-$(date -u +%Y%m%d-%H%M%S).log"
SESSION_LOG="$(mktemp -t mylittleclaude-session.XXXXXX 2>/dev/null \
                || echo "/tmp/mylittleclaude-session.$$.log")"

# Tee all output through SESSION_LOG so the crash dump can include tail of it.
# Interactive prompts and `read -p` still work because stdin is untouched.
exec > >(tee -a "$SESSION_LOG") 2>&1

DISTRO_FAMILY=""   # set by phase_detect_distro
PKG=""             # set by phase_detect_distro
CLAUDE_BIN=""      # set by phase_claude_code

# --- ansi -----------------------------------------------------------------

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_CYAN=$'\033[36m'
    C_BOLD=$'\033[1m'
else
    C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""; C_BOLD=""
fi

say()  { printf '%s%s%s\n' "$C_CYAN" "$*" "$C_RESET"; }
good() { printf '%s%s%s\n' "$C_GREEN" "$*" "$C_RESET"; }
warn() { printf '%s%s%s\n' "$C_YELLOW" "$*" "$C_RESET" >&2; }
err()  { printf '%s%s%s\n' "$C_RED"    "$*" "$C_RESET" >&2; }

# --- error handling -------------------------------------------------------

ERROR_HANDLED=0   # guard against double-logging from trap+fail

dump_log() {
    local rc="$1" lineno="$2" cmd="$3" msg="${4:-}"
    {
        echo "mylittleclaude installer failure"
        echo "  time:        $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "  exit:        $rc"
        echo "  line:        $lineno"
        echo "  command:     $cmd"
        [[ -n "$msg" ]] && echo "  message:     $msg"
        echo
        echo "Context:"
        echo "  INSTALL_DIR  = $INSTALL_DIR"
        echo "  PYTHON_BIN   = $PYTHON_BIN"
        echo "  PATH         = $PATH"
        echo "  USER         = ${USER:-?}"
        echo "  HOME         = ${HOME:-?}"
        echo "  DISTRO_FAMILY= ${DISTRO_FAMILY:-?}"
        echo
        echo "---- last 50 lines of session output ----"
        if [[ -s "$SESSION_LOG" ]]; then
            tail -n 50 "$SESSION_LOG" 2>/dev/null || true
        else
            echo "(session log empty)"
        fi
        echo
        echo "---- environment ----"
        env | sort
    } > "$LOG_FILE" 2>&1 || true
}

on_error_trap() {
    local rc=$?
    local lineno=${BASH_LINENO[0]:-?}
    local cmd="${BASH_COMMAND:-?}"
    [[ "$ERROR_HANDLED" == "1" ]] && exit "$rc"
    ERROR_HANDLED=1
    sleep 0.05  # give the tee subprocess a moment to flush
    dump_log "$rc" "$lineno" "$cmd"
    err "Installation failed at line $lineno: $cmd (exit $rc)"
    err "Full log: $LOG_FILE"
    exit "$rc"
}
trap on_error_trap ERR

# fail(): explicit "we detected a precondition we can't recover from."
# Distinct from "the operator chose to skip something", which is normal.
fail() {
    local msg="$*"
    local lineno=${BASH_LINENO[0]:-?}
    [[ "$ERROR_HANDLED" == "1" ]] && exit 1
    ERROR_HANDLED=1
    sleep 0.05
    dump_log 1 "$lineno" "(explicit fail)" "$msg"
    err "Installation aborted: $msg"
    err "Full log: $LOG_FILE"
    exit 1
}

# --- input helpers --------------------------------------------------------

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

# --- functional probes ----------------------------------------------------

probe_python_ok() {
    "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
        >/dev/null 2>&1
}

probe_python_minor() {
    "$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' \
        2>/dev/null
}

probe_venv_ok() {
    # The check that matters. `import venv` succeeds even when ensurepip is
    # missing — that's the Ubuntu 26.04 / 3.14 failure we hit. `-m venv --help`
    # exits non-zero iff the whole subsystem is functional.
    "$PYTHON_BIN" -m venv --help >/dev/null 2>&1
}

probe_node_ok() {
    command -v node >/dev/null 2>&1 \
        && node --version 2>/dev/null | grep -Eq '^v(2[0-9]|[3-9][0-9])\.'
}

# Where is `claude`? Prefer the canonical npm-global path; PATH-resolved as
# a fallback. v0.2.0's bug: it only looked at PATH after manipulating the
# *parent* shell's PATH — which doesn't carry across `npm install -g`.
resolve_claude_bin() {
    if [[ -x "$NPM_GLOBAL_BIN/claude" ]]; then
        echo "$NPM_GLOBAL_BIN/claude"
        return 0
    fi
    # Also accept whatever `command -v` finds (covers system installs, asdf,
    # nvm, etc.). Resolving symlinks ourselves: not worth it — npm installs
    # `claude` as a wrapper script and may use `claude.exe` shim internally;
    # the wrapper itself is what's `executable`.
    if command -v claude >/dev/null 2>&1; then
        command -v claude
        return 0
    fi
    return 1
}

probe_claude_works() {
    local bin="$1"
    [[ -n "$bin" && -x "$bin" ]] || return 1
    "$bin" --version >/dev/null 2>&1
}

# --- package install helpers ----------------------------------------------

pkg_install() {
    if [[ "$NO_SUDO" == "1" ]]; then
        fail "package install required but --no-sudo is set: $*"
    fi
    if [[ "$DISTRO_FAMILY" == "debian" ]]; then
        sudo "$PKG" update -y >/dev/null
        sudo "$PKG" install -y "$@"
    else
        sudo "$PKG" install -y "$@"
    fi
}

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
            fail "Unsupported distro: ${PRETTY_NAME:-${ID:-unknown}}. See README's manual install appendix."
            ;;
    esac
    say "Detected: ${PRETTY_NAME:-${ID:-unknown}} (family: $DISTRO_FAMILY, pkg: $PKG)"
}

# Pass 1: make sure Python 3.11+ is usable.
ensure_python() {
    if command -v "$PYTHON_BIN" >/dev/null 2>&1 && probe_python_ok; then
        good "Python OK: $($PYTHON_BIN --version 2>&1 || true)"
        return 0
    fi

    say "  Python 3.11+ missing or too old."
    local pkg
    if [[ "$DISTRO_FAMILY" == "debian" ]]; then
        pkg="python3"
    else
        pkg="python3.11"
    fi
    if ! ask_yes "Install $pkg with $PKG?" Y; then
        fail "Python 3.11+ is required. Aborting."
    fi
    pkg_install "$pkg"

    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        fail "After installing '$pkg', '$PYTHON_BIN' is still not on PATH. \
Set MYLITTLECLAUDE_PYTHON=/path/to/python3.11 and re-run."
    fi
    if ! probe_python_ok; then
        local ver
        ver=$("$PYTHON_BIN" --version 2>&1 || echo unknown)
        fail "After installing '$pkg', '$PYTHON_BIN' is $ver (< 3.11). \
Set MYLITTLECLAUDE_PYTHON=/path/to/python3.11 and re-run."
    fi
    good "Python installed: $($PYTHON_BIN --version 2>&1 || true)"
}

# Pass 2: make sure `python -m venv` actually works. The package name depends
# on the running Python's minor version on Debian-family.
ensure_venv() {
    if probe_venv_ok; then
        good "Python venv module functional."
        return 0
    fi

    if [[ "$DISTRO_FAMILY" != "debian" ]]; then
        local trace
        trace=$("$PYTHON_BIN" -m venv --help 2>&1 | head -20 || true)
        fail "'$PYTHON_BIN -m venv --help' failed but no apt-equivalent on this distro to fix it.
Output:
$trace"
    fi

    local minor pkg
    minor=$(probe_python_minor) || fail "Could not query Python version."
    pkg="python${minor}-venv"
    say "  Python venv not functional — will install ${pkg}."

    if ! ask_yes "Install ${pkg} with $PKG?" Y; then
        fail "${pkg} is required to create the .venv. Aborting."
    fi
    pkg_install "$pkg"

    if ! probe_venv_ok; then
        local trace
        trace=$("$PYTHON_BIN" -m venv --help 2>&1 | head -20 || true)
        fail "After installing ${pkg}, '$PYTHON_BIN -m venv --help' still fails.
Output:
$trace"
    fi
    good "Python venv functional ($pkg installed)."
}

# Pass 3: every other tool. Functional check, install if missing, re-verify.
ensure_other_tools() {
    local missing=()
    command -v git    >/dev/null 2>&1 || missing+=(git)
    command -v curl   >/dev/null 2>&1 || missing+=(curl)
    command -v rsync  >/dev/null 2>&1 || missing+=(rsync)
    if ! command -v ssh >/dev/null 2>&1; then
        if [[ "$DISTRO_FAMILY" == "debian" ]]; then
            missing+=(openssh-client)
        else
            missing+=(openssh-clients)
        fi
    fi
    if ! probe_node_ok; then
        missing+=(nodejs)
    fi
    command -v npm >/dev/null 2>&1 || missing+=(npm)

    if (( ${#missing[@]} == 0 )); then
        good "Other tools present (git, curl, rsync, ssh, node, npm)."
        return 0
    fi

    say "Missing packages: ${missing[*]}"
    if ! ask_yes "Install with $PKG?" Y; then
        fail "Aborting — install the missing packages and re-run."
    fi
    pkg_install "${missing[@]}"

    # Re-verify each.
    local still_missing=()
    command -v git    >/dev/null 2>&1 || still_missing+=(git)
    command -v curl   >/dev/null 2>&1 || still_missing+=(curl)
    command -v rsync  >/dev/null 2>&1 || still_missing+=(rsync)
    command -v ssh    >/dev/null 2>&1 || still_missing+=(ssh)
    probe_node_ok                    || still_missing+=(node)
    command -v npm    >/dev/null 2>&1 || still_missing+=(npm)
    if (( ${#still_missing[@]} > 0 )); then
        fail "After install, still missing or broken: ${still_missing[*]}"
    fi
    good "All other tools verified."
}

phase_prereqs() {
    say "Checking system prereqs..."
    ensure_python
    ensure_venv
    ensure_other_tools
}

bashrc_has_npm_global() {
    [[ -f "$HOME/.bashrc" ]] && grep -Fq '.npm-global/bin' "$HOME/.bashrc"
}

# Make sure ~/.npm-global/bin is on PATH for *this* install session, and offer
# to persist it so future shells see `claude` and `mylittleclaude-setup`.
ensure_npm_global_in_path() {
    case ":$PATH:" in
        *":$NPM_GLOBAL_BIN:"*) ;;
        *) export PATH="$NPM_GLOBAL_BIN:$PATH" ;;
    esac

    if bashrc_has_npm_global; then
        return 0
    fi
    if ! [[ -t 0 ]]; then
        return 0
    fi
    if ask_yes "Append 'export PATH=\$HOME/.npm-global/bin:\$PATH' to ~/.bashrc?" Y; then
        {
            echo
            echo "# Added by mylittleclaude installer ($(date -u +%Y-%m-%d))"
            echo 'export PATH="$HOME/.npm-global/bin:$PATH"'
        } >> "$HOME/.bashrc"
        good "  ✓ Appended to ~/.bashrc (re-source it or open a new shell to pick it up)."
    else
        warn "  ~/.bashrc not modified; remember to add it manually:"
        warn '    export PATH="$HOME/.npm-global/bin:$PATH"'
    fi
}

phase_claude_code() {
    # Try to find an existing claude first, then verify it actually runs.
    local existing
    if existing=$(resolve_claude_bin); then
        if probe_claude_works "$existing"; then
            CLAUDE_BIN="$existing"
            good "Claude Code already installed: $("$CLAUDE_BIN" --version 2>&1 || true)"
            ensure_npm_global_in_path
            return 0
        fi
        warn "Found claude at $existing but '--version' failed; will reinstall."
    fi

    say "Installing @anthropic-ai/claude-code via npm..."

    # Switch npm prefix to ~/.npm-global so we don't need sudo for global packages.
    local prefix
    prefix=$(npm config get prefix 2>/dev/null || echo "")
    if [[ "$prefix" != "$HOME"* ]]; then
        warn "npm prefix is $prefix — switching to ~/.npm-global to avoid sudo."
        if ! npm config set prefix "$HOME/.npm-global"; then
            fail "Could not set npm prefix to $HOME/.npm-global."
        fi
    fi

    ensure_npm_global_in_path

    if ! npm install -g @anthropic-ai/claude-code; then
        fail "'npm install -g @anthropic-ai/claude-code' failed. \
Check network / npm registry access."
    fi

    # Functional re-verify: resolve the binary, then actually run it.
    local resolved
    if ! resolved=$(resolve_claude_bin); then
        local listing
        listing=$(ls -la "$NPM_GLOBAL_BIN" 2>&1 | head -30 || true)
        fail "claude binary not found after npm install. \
Expected at $NPM_GLOBAL_BIN/claude.
$NPM_GLOBAL_BIN listing:
$listing"
    fi
    if ! probe_claude_works "$resolved"; then
        local trace
        trace=$("$resolved" --version 2>&1 | head -20 || true)
        fail "claude installed at $resolved but '--version' failed.
Output:
$trace"
    fi

    CLAUDE_BIN="$resolved"
    good "Claude Code installed at $CLAUDE_BIN: $("$CLAUDE_BIN" --version 2>&1 || true)"
}

phase_claude_login() {
    if [[ -z "$CLAUDE_BIN" ]]; then
        fail "phase_claude_login called before phase_claude_code set CLAUDE_BIN."
    fi

    local out
    out=$("$CLAUDE_BIN" -p --output-format json "ping" </dev/null 2>&1) || true
    if grep -q '"is_error":false' <<< "$out"; then
        good "Claude Code is logged in."
        return 0
    fi

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
            skip)
                warn "Claude login deferred — bot prompts will fail until you authenticate."
                return 0
                ;;
            quit)
                fail "Operator aborted at Claude login step."
                ;;
            *)
                out=$("$CLAUDE_BIN" -p --output-format json "ping" </dev/null 2>&1) || true
                if grep -q '"is_error":false' <<< "$out"; then
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
        if ! "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"; then
            fail "'$PYTHON_BIN -m venv $INSTALL_DIR/.venv' failed even though \
the venv module reported functional. Check disk space / permissions."
        fi
    fi
    if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
        fail ".venv created but $INSTALL_DIR/.venv/bin/python is missing or not executable."
    fi

    say "Installing project (pip install -e .)..."
    if ! "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip >/dev/null; then
        fail "pip self-upgrade failed."
    fi
    if ! "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR"; then
        fail "'pip install -e $INSTALL_DIR' failed."
    fi

    if [[ ! -x "$INSTALL_DIR/.venv/bin/mylittleclaude-setup" ]]; then
        fail "Console script 'mylittleclaude-setup' was not registered. \
Was pip install silently partial? Check pyproject.toml [project.scripts]."
    fi
    good "venv ready: $INSTALL_DIR/.venv/bin/python"
}

phase_wizard() {
    say "Launching configuration wizard..."
    # Wizard exit codes (see installer/cli.py):
    #   0   -> success OR operator-driven clean abort (quit / review-aborted)
    #   2   -> unexpected error (the Python side wrote its own /tmp log)
    #   130 -> SIGINT
    # Anything else trips our crash trap as a script-side bug.
    set +e
    "$INSTALL_DIR/.venv/bin/python" -m mylittleclaude.installer reconfigure
    local rc=$?
    set -e
    case "$rc" in
        0)   good "Wizard finished." ;;
        130) warn "Wizard interrupted. Re-run 'mylittleclaude-setup' later to continue." ;;
        2)   fail "Wizard hit an unexpected error (see /tmp/mylittleclaude-install-*.log)." ;;
        *)   fail "Wizard exited with unexpected code $rc." ;;
    esac
}

phase_systemd() {
    local unit_src="$INSTALL_DIR/systemd/mylittleclaude.service"
    local unit_dst="/etc/systemd/system/mylittleclaude.service"

    if [[ "$NO_SUDO" == "1" ]]; then
        warn "Skipping systemd install (--no-sudo). Unit file at: $unit_src"
        return 0
    fi
    if [[ ! -f "$unit_src" ]]; then
        warn "No unit file at $unit_src; skipping systemd."
        return 0
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemctl not found; skipping systemd integration."
        return 0
    fi

    say "Installing systemd unit..."
    local tmp_unit
    tmp_unit=$(mktemp) || fail "mktemp failed"
    sed \
        -e "s|^User=.*|User=$USER|" \
        -e "s|^WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" \
        -e "s|^EnvironmentFile=.*|EnvironmentFile=$INSTALL_DIR/.env|" \
        -e "s|^ExecStart=.*|ExecStart=$INSTALL_DIR/.venv/bin/python -m mylittleclaude|" \
        -e "s|^ReadWritePaths=.*|ReadWritePaths=$INSTALL_DIR $HOME/projects|" \
        "$unit_src" > "$tmp_unit"
    if ! sudo install -m 0644 "$tmp_unit" "$unit_dst"; then
        rm -f "$tmp_unit"
        fail "Could not install systemd unit to $unit_dst (sudo refused?)."
    fi
    rm -f "$tmp_unit"

    if ! sudo systemctl daemon-reload; then
        fail "'systemctl daemon-reload' failed."
    fi
    if ! sudo systemctl enable mylittleclaude >/dev/null 2>&1; then
        fail "'systemctl enable mylittleclaude' failed."
    fi
    good "systemd unit installed and enabled."

    if ask_yes "Start the service now?" Y; then
        if sudo systemctl start mylittleclaude; then
            sleep 2
            if systemctl is-active --quiet mylittleclaude; then
                good "Service is active."
            else
                warn "Service did not become active. Recent logs:"
                journalctl -u mylittleclaude -n 30 --no-pager 2>/dev/null || true
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
    if [[ ! -x "$target" ]]; then
        fail "$target is missing or not executable (pip install partial?)."
    fi
    if [[ -L "$link" || -f "$link" ]]; then
        rm -f "$link"
    fi
    ln -s "$target" "$link"

    # Verify the symlink actually runs.
    if ! "$link" version >/dev/null 2>&1; then
        fail "Symlink $link -> $target installed but 'version' subcommand failed."
    fi

    good "Setup command available at: $link"
    case ":$PATH:" in
        *":$bin:"*) ;;
        *)
            warn "Add to your shell rc to use 'mylittleclaude-setup' directly:"
            warn '    export PATH="$HOME/.local/bin:$PATH"'
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
    say "Session log: $SESSION_LOG"
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
