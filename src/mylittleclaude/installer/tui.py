"""Terminal UI primitives for the installer wizard.

Stdlib-only: input() loops + ANSI escapes. Respects NO_COLOR and TTY detection.
Every input helper recognizes the universal control tokens 'back', 'skip',
'quit' and surfaces them as a typed sentinel rather than a raw string, so the
wizard state machine can route them without re-parsing.
"""

from __future__ import annotations

import os
import sys
import textwrap
from dataclasses import dataclass
from typing import Callable

# --- color / width -----------------------------------------------------------

_TTY = sys.stdout.isatty()
_USE_COLOR = _TTY and "NO_COLOR" not in os.environ

WIDTH = 64  # spec §4 — wizard text is laid out at 64 cols (±4 tolerated).


def _ansi(code: str) -> str:
    return code if _USE_COLOR else ""


C_RESET = _ansi("\033[0m")
C_BOLD = _ansi("\033[1m")
C_DIM = _ansi("\033[2m")
C_RED = _ansi("\033[31m")
C_GREEN = _ansi("\033[32m")
C_YELLOW = _ansi("\033[33m")
C_BLUE = _ansi("\033[34m")
C_CYAN = _ansi("\033[36m")


# --- output ------------------------------------------------------------------

def say(msg: str = "") -> None:
    print(msg)


def info(msg: str) -> None:
    print(f"{C_CYAN}{msg}{C_RESET}")


def good(msg: str) -> None:
    print(f"{C_GREEN}{msg}{C_RESET}")


def warn(msg: str) -> None:
    print(f"{C_YELLOW}{msg}{C_RESET}")


def err(msg: str) -> None:
    print(f"{C_RED}{msg}{C_RESET}", file=sys.stderr)


def header(step_num: int | None, step_total: int | None, title: str) -> None:
    """Render `─── Step N of M: title ───` with a trailing rule to WIDTH."""
    if step_num is not None and step_total is not None:
        label = f"Step {step_num} of {step_total}: {title}"
    else:
        label = title
    prefix = "─── "
    line = f"{prefix}{label} "
    pad = max(3, WIDTH - len(line))
    print()
    print(f"{C_BOLD}{line}{'─' * pad}{C_RESET}")
    print()


def block(text: str) -> None:
    """Print an indented body block at the wizard's width."""
    wrapped = textwrap.dedent(text).strip("\n")
    print(wrapped)
    print()


# --- input -------------------------------------------------------------------

class WizardControl(Exception):
    """Base for non-value control flow (back/skip/quit)."""


class GoBack(WizardControl):
    pass


class Skip(WizardControl):
    pass


class Quit(WizardControl):
    pass


@dataclass
class AskOptions:
    allow_skip: bool = True
    allow_back: bool = True
    allow_quit: bool = True
    default: str | None = None  # shown in brackets; Enter accepts it
    secret: bool = False        # hint we'd hide the echoed value (we don't)


def _control_tokens(o: AskOptions) -> list[str]:
    toks = []
    if o.allow_skip:
        toks.append("'skip'")
    if o.allow_back:
        toks.append("'back'")
    if o.allow_quit:
        toks.append("'quit'")
    return toks


def _prompt_suffix(o: AskOptions) -> str:
    toks = _control_tokens(o)
    bits: list[str] = []
    if o.default is not None:
        bits.append(f"[{o.default}]")
    if toks:
        bits.append(f"[type {', '.join(toks)}]")
    return f" {' '.join(bits)}" if bits else ""


def ask(
    prompt: str,
    *,
    options: AskOptions | None = None,
    validate: Callable[[str], str | None] | None = None,
    transform: Callable[[str], str] | None = None,
    max_attempts: int = 3,
) -> str:
    """Prompt the operator for a string. Raises GoBack/Skip/Quit on those.

    `validate` returns None on success, or an error string to show the operator.
    `transform` is applied to accepted input before returning (e.g., int parse).
    After `max_attempts` invalid attempts the operator is offered to skip
    (if allowed); otherwise the prompt keeps re-asking until a valid value
    arrives.
    """
    o = options or AskOptions()
    attempt = 0
    while True:
        attempt += 1
        try:
            raw = input(f"{prompt}{_prompt_suffix(o)}: ").strip()
        except EOFError as e:
            raise Quit() from e
        except KeyboardInterrupt as e:
            print()
            raise Quit() from e

        lowered = raw.lower()
        if not raw and o.default is not None:
            raw = o.default
        elif lowered == "back" and o.allow_back:
            raise GoBack()
        elif lowered == "skip" and o.allow_skip:
            raise Skip()
        elif lowered == "quit" and o.allow_quit:
            raise Quit()

        if not raw:
            err("(empty input — please enter a value or use a control word)")
            continue

        if validate is not None:
            error = validate(raw)
            if error is not None:
                err(f"  ✗ {error}")
                if attempt >= max_attempts and o.allow_skip:
                    if confirm("Skip this field for now?", default=False):
                        raise Skip()
                    attempt = 0
                continue

        return transform(raw) if transform else raw


def confirm(prompt: str, *, default: bool = True) -> bool:
    """Yes/No prompt. Default fires on empty input. Accepts y/yes/n/no."""
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        try:
            raw = input(f"{prompt}{suffix}: ").strip().lower()
        except EOFError:
            return default
        except KeyboardInterrupt:
            print()
            return False
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        err("  ✗ please answer y or n")


def choose(
    prompt: str,
    choices: list[tuple[str, str]],
    *,
    default: str | None = None,
) -> str:
    """Pick one of `choices` (key, label). Returns the chosen key."""
    keys = {k.lower() for k, _ in choices}
    say(prompt)
    for k, label in choices:
        marker = "*" if default and k.lower() == default.lower() else " "
        say(f"  {marker} [{k}] {label}")
    while True:
        try:
            raw = input("> ").strip().lower()
        except EOFError as e:
            raise Quit() from e
        except KeyboardInterrupt as e:
            print()
            raise Quit() from e
        if not raw and default is not None:
            return default
        if raw in keys:
            return raw
        err(f"  ✗ pick one of: {', '.join(sorted(keys))}")
