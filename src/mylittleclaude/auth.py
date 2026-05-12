from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import Update

from .models import AppConfig

log = logging.getLogger(__name__)


@dataclass
class AuthResult:
    user_ok: bool
    group_ok: bool

    @property
    def fully_ok(self) -> bool:
        return self.user_ok and self.group_ok


def check_update(cfg: AppConfig, update: Update) -> AuthResult:
    """Check if the update sender + chat are whitelisted.

    Drop silently if either fails — do not reply to unauthorized actors.
    """
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    user_ok = user_id is not None and user_id in cfg.allowed_user_ids
    if cfg.bootstrap_mode:
        # No group whitelist yet — allow only /chatid from allowed users.
        group_ok = user_ok
    else:
        group_ok = chat_id is not None and chat_id in cfg.allowed_group_ids

    return AuthResult(user_ok=user_ok, group_ok=group_ok)


def is_authorized(cfg: AppConfig, update: Update) -> bool:
    return check_update(cfg, update).fully_ok
