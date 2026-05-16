"""
Telegram outbound security guard.

Ensures the exchange Telethon account only sends messages to:
  1. The single authorised group (@cyrusGlobalExchange)
  2. ADMIN_ID (via Bot API — not gated here; via Telethon — allowed)
  3. Customers who first appeared in @cyrusGlobalExchange

All other Telethon send targets are blocked and logged.
"""
import logging

log = logging.getLogger("telegram_guard")

_allowed_group_id: int | None = None
_admin_id: int | None = None
_customer_whitelist: set[int] = set()


def init(group_id: int, admin_id: int) -> None:
    global _allowed_group_id, _admin_id
    _allowed_group_id = group_id
    _admin_id = admin_id
    log.info("[GUARD] Initialised — allowed_group=%s admin=%s", group_id, admin_id)


def allowed_group_id() -> int | None:
    return _allowed_group_id


def register_customer(uid: int) -> None:
    """Allow private DMs to/from a user seen in @cyrusGlobalExchange."""
    if uid not in _customer_whitelist:
        _customer_whitelist.add(uid)
        log.info("[GUARD] Customer registered for private DMs: uid=%s", uid)


def is_allowed_group(chat_id: int) -> bool:
    return _allowed_group_id is not None and chat_id == _allowed_group_id


def is_allowed_target(target: int) -> bool:
    """True if a Telethon send to this target is permitted."""
    if _admin_id and target == _admin_id:
        return True
    if _allowed_group_id and target == _allowed_group_id:
        return True
    return target in _customer_whitelist


def check_send(target: int, action: str = "send") -> bool:
    """
    Call before every client.send_message / send_file.
    Returns True if allowed, False (and logs) if blocked.
    """
    if is_allowed_target(target):
        return True
    log.warning("[GUARD] Blocked unauthorized Telegram target: %s (action=%s)", target, action)
    return False
