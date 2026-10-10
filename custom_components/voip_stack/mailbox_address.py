"""Mailbox addresses shared by PBX routing and configuration validation."""


def parse_mailbox_address(target):
    raw = str(target or "").strip()
    lower = raw.casefold()
    if "@" in raw or lower.startswith(("sip:", "sips:")):
        return None
    if lower in {"*97", "mailbox"}:
        return "own", ""
    if lower == "*98":
        return "login", ""
    for prefix, mode in (("mailbox.", "access"), ("voicemail.", "deposit")):
        if lower.startswith(prefix):
            return mode, raw[len(prefix):]
    if raw.startswith("*60") and raw[3:].isdigit():
        return "deposit", raw[3:]
    return None
