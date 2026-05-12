"""Wizard state machine: drives the steps in order, handles back-navigation.

Each step is a callable that takes `WizardState`, mutates it, and returns
either:
  - the next step's index (int) — usually i+1, but a step can jump
  - None — meaning "done, write config and exit"

The dispatcher catches GoBack (decrement index, skip blank steps if needed),
Skip (mark the field deferred, advance), and Quit (raise to caller).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from .state import WizardState
from .tui import GoBack, Quit, Skip, err, info, warn

log = logging.getLogger(__name__)


@dataclass
class Step:
    title: str
    fn: Callable[[WizardState], None]  # mutates state
    skippable: bool = True              # operator may type 'skip'
    deferred_field: str | None = None   # field to mark deferred on skip


class WizardAborted(Exception):
    """Operator typed 'quit' at any step. Caller should exit non-zero."""


def run(steps: list[Step], state: WizardState) -> bool:
    """Drive the wizard. Returns True if it completed, False if the operator
    aborted at the Review screen (which raises ReviewAborted internally)."""
    i = 0
    history: list[int] = []
    n = len(steps)

    while 0 <= i < n:
        step = steps[i]
        try:
            log.debug("wizard step %d/%d: %s", i + 1, n, step.title)
            step.fn(state)
            history.append(i)
            i += 1
        except GoBack:
            if not history:
                warn("(already at the first step)")
                continue
            i = history.pop()
        except Skip:
            if not step.skippable:
                err("This step can't be skipped.")
                continue
            if step.deferred_field:
                state.mark_deferred(step.deferred_field)
                info(f"Deferred {step.deferred_field}; you can set it later.")
            history.append(i)
            i += 1
        except Quit as e:
            raise WizardAborted("operator typed 'quit'") from e
        except ReviewAborted:
            return False

    return True


class ReviewAborted(Exception):
    """Raised by the Review step when the operator answers 'n'."""
