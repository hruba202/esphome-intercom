"""RFC 3515 REFER and RFC 3891 Replaces value objects."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode

from . import sip


@dataclass(frozen=True, slots=True)
class SipTransferResult:
    """Final outcome reported by a REFER subscription."""

    accepted: bool
    status: int
    state: str


class ReferSubscription:
    """One bounded transfer outcome, independent of dialog direction."""

    def __init__(self) -> None:
        self.result: asyncio.Future[SipTransferResult] = asyncio.get_running_loop().create_future()
        self.refer_cseq: int | None = None
        self.allow_missing_id = False
        self._expiry: asyncio.TimerHandle | None = None
        self.expires_at: float | None = None

    def bind(self, cseq: int, *, first: bool) -> None:
        """Bind before sending, including a new CSeq after a digest challenge."""
        self.refer_cseq = cseq
        self.allow_missing_id = first

    def notify(self, request: sip.SipMessage) -> tuple[int, str]:
        if len(request.header_values("Event")) != 1:
            return 400, "Bad Request"
        event = request.header("Event").split(";")
        if event[0].strip().casefold() != "refer":
            return 489, "Bad Event"
        ids = [part.split("=", 1)[1].strip() if "=" in part else ""
               for part in event[1:] if part.split("=", 1)[0].strip().casefold() == "id"]
        if len(ids) > 1:
            return 400, "Bad Request"
        if (self.refer_cseq is None or self.result.done()
                or (ids and ids[0] != str(self.refer_cseq))
                or (not ids and not self.allow_missing_id)):
            return 481, "Subscription Does Not Exist"
        if len(request.header_values("Subscription-State")) != 1:
            return 400, "Bad Request"
        state_parts = request.header("Subscription-State").split(";")
        state = state_parts[0].strip().casefold()
        if state not in {"active", "pending", "terminated"}:
            return 400, "Bad Request"
        if request.header("Content-Type").split(";", 1)[0].strip().casefold() != "message/sipfrag":
            return 415, "Unsupported Media Type"
        try:
            status = parse_sipfrag_status(request.body)
        except (TypeError, ValueError, sip.SipError):
            return 400, "Bad Request"
        terminated = state == "terminated"
        if not terminated:
            expirations = [part.partition("=")[2].strip() for part in state_parts[1:]
                           if part.partition("=")[0].strip().casefold() == "expires"]
            if len(expirations) > 1:
                return 400, "Bad Request"
            if expirations:
                value = expirations[0]
                if not value.isascii() or not value.isdigit() or len(value) > 10:
                    return 400, "Bad Request"
                if self._expiry is not None:
                    self._expiry.cancel()
                loop = asyncio.get_running_loop()
                self.expires_at = loop.time() + int(value)
                self._expiry = loop.call_at(self.expires_at, self.close)
        if not self.result.done():
            if status >= 200:
                self.result.set_result(SipTransferResult(
                    200 <= status < 300, status,
                    "completed" if status < 300 else "failed",
                ))
            elif terminated:
                self.result.set_result(SipTransferResult(False, status, "terminated"))
        if self.result.done() and self._expiry is not None:
            self._expiry.cancel()
        return 200, "OK"

    def close(self) -> None:
        if self._expiry is not None:
            self._expiry.cancel()
        if not self.result.done():
            self.result.set_result(SipTransferResult(False, 0, "terminated"))


@dataclass(frozen=True, slots=True)
class SipReplaces:
    """One dialog identifier carried by a Replaces header."""

    call_id: str
    to_tag: str
    from_tag: str
    early_only: bool = False

    def __str__(self) -> str:
        parts = [self.call_id, f"to-tag={self.to_tag}", f"from-tag={self.from_tag}"]
        if self.early_only:
            parts.append("early-only")
        return ";".join(parts)


@dataclass(frozen=True, slots=True)
class SipReferTarget:
    """Normalized target of one REFER request."""

    uri: str
    replaces: SipReplaces | None = None

    def as_header(self) -> str:
        query = ""
        if self.replaces is not None:
            query = "?" + urlencode(
                {"Replaces": str(self.replaces)},
                quote_via=quote,
                safe="",
            )
        return f"<{self.uri}{query}>"


def parse_replaces(value: str) -> SipReplaces:
    """Parse a Replaces value with mandatory dialog tags."""

    # A SIP header is already decoded. URI decoding belongs to Refer-To only;
    # decoding again changes valid percent sequences in the dialog identity.
    parts = [part.strip() for part in (value or "").split(";")]
    call_id = parts[0] if parts else ""
    params: dict[str, str] = {}
    flags: set[str] = set()
    for part in parts[1:]:
        if not part:
            continue
        if "=" in part:
            key, item = part.split("=", 1)
            key, item = key.strip().lower(), item.strip()
            if key in params or key in flags:
                raise sip.SipError("duplicate Replaces parameter")
            params[key] = item
        else:
            key = part.lower()
            if key in params or key in flags:
                raise sip.SipError("duplicate Replaces parameter")
            flags.add(key)
    if not call_id or any(char.isspace() for char in call_id) or not all(
        sip.is_sip_token(params.get(key, "")) for key in ("to-tag", "from-tag")
    ) or "early-only" in params:
        raise sip.SipError("Replaces requires call-id, to-tag and from-tag")
    return SipReplaces(
        call_id=call_id,
        to_tag=params["to-tag"],
        from_tag=params["from-tag"],
        early_only="early-only" in flags,
    )


def request_replaces(request: sip.SipMessage) -> SipReplaces | None:
    """Read the single INVITE-only RFC 3891 header without URI decoding."""
    values = request.header_values("Replaces")
    if not values:
        return None
    if request.method != "INVITE" or len(values) != 1:
        raise sip.SipError("Replaces is allowed once, on INVITE only")
    return parse_replaces(values[0])


def parse_refer_to(value: str) -> SipReferTarget:
    """Parse one Refer-To name-address and its optional Replaces header."""

    raw = str(value or "").strip()
    if raw.startswith("<") and ">" in raw:
        raw = raw[1 : raw.index(">")].strip()
    uri_text, separator, query = raw.partition("?")
    uri = str(sip.parse_sip_uri(uri_text))
    replaces: SipReplaces | None = None
    if separator:
        for key, item in parse_qsl(query, keep_blank_values=True):
            if key.casefold() == "replaces":
                if replaces is not None:
                    raise sip.SipError("Refer-To contains duplicate Replaces")
                replaces = parse_replaces(item)
    return SipReferTarget(uri=uri, replaces=replaces)


def parse_sipfrag_status(body: bytes | str) -> int:
    """Return the status code from a message/sipfrag status line."""

    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    first_line = text.replace("\r\n", "\n").split("\n", 1)[0].strip()
    parts = first_line.split(None, 2)
    if len(parts) < 2 or parts[0].upper() != "SIP/2.0":
        raise sip.SipError("invalid message/sipfrag status line")
    try:
        status = int(parts[1])
    except ValueError as err:
        raise sip.SipError("invalid message/sipfrag status code") from err
    if not 100 <= status <= 699:
        raise sip.SipError("message/sipfrag status is outside SIP range")
    return status
