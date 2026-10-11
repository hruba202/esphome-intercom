"""Application-level SIP methods owned by the integration runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import secrets
import time
from xml.etree import ElementTree

from homeassistant.core import HomeAssistant, callback

from .const import EVENT_SIP_MESSAGE, EVENT_SIP_PRESENCE
from .core import sip
from .sip_listener import SipFollowUpRequest, SipRequestResult
from .sip_registrar import SipRegistrar


@dataclass(slots=True)
class SipApplicationMethods:
    """Handle non-call requests without adding another SIP state machine."""

    hass: HomeAssistant
    registrar: SipRegistrar
    publications: dict[str, "SipPresencePublication"] | None = None
    subscriptions: dict[str, "SipPresenceSubscription"] | None = None
    tasks: set[asyncio.Task[object]] | None = None
    publication_timers: dict[str, asyncio.Task[object]] | None = None
    subscription_timers: dict[str, asyncio.Task[object]] | None = None
    mailbox_archive: object | None = None
    mailbox_unsubscribe: Callable[[], None] | None = None
    dialog_unsubscribe: Callable[[], None] | None = None
    endpoint_unsubscribe: Callable[[], None] | None = None
    parking_unsubscribe: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        self.publications = {}
        self.subscriptions = {}
        self.tasks = set()
        self.publication_timers = {}
        self.subscription_timers = {}

    async def handle(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
        transport: str,
        follow_up_sender: "SipFollowUpSender | None" = None,
    ) -> SipRequestResult:
        account = self.registrar.account_for_source(addr, transport)
        if account is None:
            return SipRequestResult(403, "Forbidden")
        if request.method == "MESSAGE":
            return self._message(request, account.username)
        if request.method == "PUBLISH":
            return self._publish(request, account.username)
        if request.method == "SUBSCRIBE":
            mailbox_id = ""
            dialog_endpoint_id = ""
            event = request.header("Event").partition(";")[0].strip().lower()
            if event == "dialog":
                from .runtime_data import endpoint_directory
                from .parking import parking_address

                try:
                    target = sip.parse_sip_uri(request.uri).user
                    endpoint = endpoint_directory(self.hass).resolve(target)
                    parking = parking_address(self.hass, target)
                except (ValueError, sip.SipError):
                    return SipRequestResult(400, "Bad Request")
                if parking is not None and parking[1]:
                    dialog_endpoint_id = "parking:" + parking[1]
                elif endpoint is not None:
                    dialog_endpoint_id = endpoint.endpoint_id
                else:
                    return SipRequestResult(404, "Not Found")
                if request.body:
                    return SipRequestResult(415, "Unsupported Media Type")
            if event == "message-summary":
                from .mailbox import get_archive, mailbox_settings, EVENT_VOICEMAIL_CHANGED
                from .runtime_data import endpoint_directory
                from .voicemail_config import voicemail_config

                try:
                    target = sip.parse_sip_uri(request.uri).user
                except (ValueError, sip.SipError):
                    return SipRequestResult(400, "Bad Request")
                if target.casefold() != account.username.casefold():
                    return SipRequestResult(403, "Forbidden")
                endpoint = endpoint_directory(self.hass).by_username(account.username)
                if (endpoint is None or not voicemail_config(self.hass).get("enabled")
                        or not mailbox_settings(self.hass, endpoint.endpoint_id).get("enabled")):
                    return SipRequestResult(404, "Not Found")
                self.mailbox_archive = await get_archive(self.hass)
                mailbox_id = endpoint.endpoint_id
                if self.mailbox_unsubscribe is None:
                    self.mailbox_unsubscribe = self.hass.bus.async_listen(EVENT_VOICEMAIL_CHANGED, self._mailbox_changed)
            result = self._subscribe(
                request,
                addr,
                account.username,
                follow_up_sender,
                mailbox_id=mailbox_id,
                dialog_endpoint_id=dialog_endpoint_id,
            )
            if (event == "dialog" and result.status == 200 and self.dialog_unsubscribe is None
                    and any(item.event == "dialog" for item in self.subscriptions.values())):
                from .websocket_api import CALL_EVENT
                from .runtime_data import endpoint_directory

                self.dialog_unsubscribe = self.hass.bus.async_listen(CALL_EVENT, self._dialog_changed)
                self.endpoint_unsubscribe = endpoint_directory(self.hass).subscribe(self._dialog_endpoint_changed)
                self.parking_unsubscribe = self.hass.bus.async_listen("voip_stack.parking_changed", self._dialog_changed)
            return result
        return SipRequestResult(405, "Method Not Allowed")

    def _message(self, request: sip.SipMessage, sender: str) -> SipRequestResult:
        """Publish one authenticated text message to Home Assistant."""

        content_type = request.header("Content-Type").split(";", 1)[0].lower()
        if content_type != "text/plain":
            return SipRequestResult(415, "Unsupported Media Type")
        try:
            message = request.body.decode("utf-8")
            recipient = sip.parse_sip_uri(request.uri).user
        except (UnicodeDecodeError, ValueError, sip.SipError):
            return SipRequestResult(400, "Bad Request")
        self.hass.bus.async_fire(
            EVENT_SIP_MESSAGE,
            {
                "sender": sender,
                "recipient": recipient,
                "content_type": content_type,
                "message": message,
            },
        )
        return SipRequestResult()

    def _publish(self, request: sip.SipMessage, sender: str) -> SipRequestResult:
        """Apply one RFC 3903 presence publication atomically."""

        if request.header("Event").strip().lower() != "presence":
            return SipRequestResult(489, "Bad Event")
        try:
            target = sip.parse_sip_uri(request.uri).user
            expires = _publication_expires(request.header("Expires"))
        except (TypeError, ValueError, sip.SipError):
            return SipRequestResult(400, "Bad Request")
        if target.lower() != sender.lower():
            return SipRequestResult(403, "Forbidden")

        publications = self.publications
        assert publications is not None
        current = publications.get(target.lower())
        if current is not None and current.expires_at <= time.time():
            publications.pop(target.lower(), None)
            current = None
        match = request.header("SIP-If-Match").strip()
        if match and (current is None or match != current.entity_tag):
            return SipRequestResult(412, "Conditional Request Failed")
        if current is not None and not match:
            return SipRequestResult(412, "Conditional Request Failed")

        body = request.body
        if body:
            if request.header("Content-Type").split(";", 1)[0].lower() != "application/pidf+xml":
                return SipRequestResult(415, "Unsupported Media Type")
            if not _valid_pidf(body, target):
                return SipRequestResult(400, "Bad Request")
        elif not match:
            return SipRequestResult(400, "Bad Request")

        if expires == 0:
            publications.pop(target.lower(), None)
            self._cancel_timer(self.publication_timers, target.lower())
            entity_tag = current.entity_tag if current is not None else secrets.token_hex(8)
        else:
            entity_tag = current.entity_tag if current is not None else secrets.token_hex(8)
            publications[target.lower()] = SipPresencePublication(
                entity_tag=entity_tag,
                body=body or (current.body if current is not None else b""),
                expires_at=time.time() + expires,
            )
            self._replace_timer(
                self.publication_timers,
                target.lower(),
                self._expire_publication(target, entity_tag, expires),
                f"voip-sip-publish-expiry-{target}",
            )
        self.hass.bus.async_fire(
            EVENT_SIP_PRESENCE,
            {"account": sender, "published": bool(expires), "expires": expires},
        )
        self._notify_subscribers(target)
        return SipRequestResult(
            headers=(("SIP-ETag", entity_tag), ("Expires", str(expires)))
        )

    def _subscribe(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
        subscriber: str,
        follow_up_sender: "SipFollowUpSender | None",
        *, mailbox_id: str = "", dialog_endpoint_id: str = "",
    ) -> SipRequestResult:
        """Create, refresh or terminate one RFC 6665 presence subscription."""

        event = request.header("Event").partition(";")[0].strip().lower()
        if event not in {"presence", "message-summary", "dialog"}:
            return SipRequestResult(489, "Bad Event")
        content_type = {"presence": "application/pidf+xml", "message-summary": "application/simple-message-summary",
                        "dialog": "application/dialog-info+xml"}[event]
        accepted = request.header("Accept")
        if accepted and not any(item.split(";", 1)[0].strip().lower() in {content_type, "application/*", "*/*"}
                                for item in accepted.split(",")):
            return SipRequestResult(406, "Not Acceptable", headers=(("Accept", content_type),))
        if follow_up_sender is None or not request.header("Contact"):
            return SipRequestResult(400, "Bad Request")
        try:
            target = sip.parse_sip_uri(request.uri).user
            from_user = sip.parse_sip_uri(
                _uri_from_header(request.header("From"))
            ).user
            expires = _publication_expires(request.header("Expires"))
            from .dialog_info import subscription_filter

            dialog_filter = subscription_filter(request.header("Event")) if event == "dialog" else ("", "", "")
        except (TypeError, ValueError, sip.SipError):
            return SipRequestResult(400, "Bad Request")
        if from_user.lower() != subscriber.lower():
            return SipRequestResult(403, "Forbidden")
        target_account = self.registrar.accounts.get(target.lower())
        if event != "dialog" and (target_account is None or not target_account.enabled):
            return SipRequestResult(404, "Not Found")

        _package, parameters = sip._split_semicolon_params(request.header("Event"))
        event_id = dict(parameters).get("id") or ""
        key = f"{request.header('Call-ID')}:{sip.extract_tag(request.header('From'))}:{event}:{event_id}"
        subscriptions = self.subscriptions
        assert subscriptions is not None
        current = subscriptions.get(key)
        request_to_tag = sip.extract_tag(request.header("To"))
        if (current is not None and request_to_tag != current.to_tag) or (current is None and request_to_tag):
            return SipRequestResult(481, "Call/Transaction Does Not Exist")
        to_tag = current.to_tag if current is not None else secrets.token_hex(8)
        subscription = SipPresenceSubscription(
            key=key,
            event=event,
            mailbox_id=mailbox_id,
            dialog_endpoint_id=dialog_endpoint_id,
            dialog_filter=dialog_filter,
            dialog_version=current.dialog_version if current is not None else 0,
            target=target,
            subscriber=subscriber,
            to_tag=to_tag,
            request=request,
            addr=addr,
            expires_at=time.time() + expires,
            sender=follow_up_sender,
            next_cseq=current.next_cseq if current is not None else 1,
            notify_lock=current.notify_lock if current is not None else asyncio.Lock(),
        )
        if expires:
            subscriptions[key] = subscription
            self._replace_timer(
                self.subscription_timers,
                key,
                self._expire_subscription(key, to_tag, expires),
                f"voip-sip-subscription-expiry-{key}",
            )
        else:
            subscriptions.pop(key, None)
            self._cancel_timer(self.subscription_timers, key)
            self._release_dialog_observers_if_unused()
        return self._notification_result(
            subscription,
            expires=expires,
            terminated=not expires,
        )

    def _notification_result(
        self,
        subscription: "SipPresenceSubscription",
        *,
        expires: int,
        terminated: bool = False,
        termination_reason: str = "timeout",
    ) -> SipRequestResult:
        if subscription.event == "dialog":
            from .dialog_info import document, snapshot
            from .runtime_data import require_runtime_data

            states = snapshot(require_runtime_data(self.hass), subscription.dialog_endpoint_id, subscription.dialog_filter)
            body = document(subscription.request.uri, subscription.dialog_version, states)
            subscription.dialog_version = (subscription.dialog_version + 1) & 0xFFFFFFFF
            subscription.dialog_snapshot = states
            content_type = "application/dialog-info+xml"
        elif subscription.event == "message-summary":
            from dataclasses import replace

            new, old = self.mailbox_archive.counts(subscription.mailbox_id)
            account_uri = replace(sip.parse_sip_uri(subscription.request.uri), user=f"mailbox.{subscription.target}")
            body = (f"Messages-Waiting: {'yes' if new else 'no'}\r\n"
                    f"Message-Account: {account_uri}\r\n"
                    f"Voice-Message: {min(new, 4294967295)}/{min(old, 4294967295)} (0/0)\r\n").encode()
            content_type = "application/simple-message-summary"
        else:
            publication = self._publication(subscription.target)
            body = publication.body if publication is not None else _closed_pidf(subscription.target)
            content_type = "application/pidf+xml"
        state = f"terminated;reason={termination_reason}" if terminated else f"active;expires={expires}"
        cseq = subscription.next_cseq
        subscription.next_cseq += 1
        return SipRequestResult(
            headers=(("Expires", str(expires)),),
            to_tag=subscription.to_tag,
            follow_up=SipFollowUpRequest(
                method="NOTIFY",
                headers=(("Event", subscription.request.header("Event")), ("Subscription-State", state)),
                body=body,
                content_type=content_type,
                cseq=cseq,
            ),
            follow_up_lock=subscription.notify_lock,
        )

    def _publication(self, target: str) -> "SipPresencePublication | None":
        publications = self.publications
        assert publications is not None
        publication = publications.get(target.lower())
        if publication is not None and publication.expires_at <= time.time():
            publications.pop(target.lower(), None)
            self._cancel_timer(self.publication_timers, target.lower())
            return None
        return publication

    @callback
    def _dialog_changed(self, _event) -> None:
        self._notify_dialog_changes()

    @callback
    def _dialog_endpoint_changed(self, event) -> None:
        self._notify_dialog_changes(event.endpoint.endpoint_id)

    def _notify_dialog_changes(self, endpoint_id=""):
        if self.dialog_unsubscribe is None or not self.subscriptions:
            return
        from .dialog_info import snapshot
        from .runtime_data import require_runtime_data

        runtime = require_runtime_data(self.hass)
        for subscription in tuple((self.subscriptions or {}).values()):
            if subscription.event != "dialog" or (endpoint_id and endpoint_id != subscription.dialog_endpoint_id):
                continue
            removed = runtime.endpoints.get(subscription.dialog_endpoint_id) is None
            if subscription.dialog_endpoint_id.startswith("parking:"):
                from .parking import parking_address
                removed = parking_address(self.hass, subscription.dialog_endpoint_id.removeprefix("parking:")) is None
            if not removed and snapshot(runtime, subscription.dialog_endpoint_id, subscription.dialog_filter) == subscription.dialog_snapshot:
                continue
            if removed:
                self.subscriptions.pop(subscription.key, None)
                self._cancel_timer(self.subscription_timers, subscription.key)
            result = self._notification_result(
                subscription, expires=0 if removed else max(0, int(subscription.expires_at - time.time())),
                terminated=removed, termination_reason="noresource",
            )
            self._track(subscription.sender(subscription.request, subscription.addr, result),
                        f"voip-sip-notify-{subscription.key}")
        self._release_dialog_observers_if_unused()

    def _release_dialog_observers_if_unused(self):
        if any(item.event == "dialog" for item in (self.subscriptions or {}).values()):
            return
        for name in ("dialog_unsubscribe", "endpoint_unsubscribe", "parking_unsubscribe"):
            if (unsubscribe := getattr(self, name)) is not None:
                unsubscribe()
                setattr(self, name, None)

    @callback
    def _mailbox_changed(self, event) -> None:
        if self.mailbox_archive is None:
            return
        for subscription in tuple((self.subscriptions or {}).values()):
            if subscription.event == "message-summary" and subscription.mailbox_id == event.data.get("endpoint_id"):
                if event.data.get("removed"):
                    self.subscriptions.pop(subscription.key, None)
                    self._cancel_timer(self.subscription_timers, subscription.key)
                    result = self._notification_result(
                        subscription, expires=0, terminated=True, termination_reason="noresource",
                    )
                    self._track(
                        subscription.sender(subscription.request, subscription.addr, result),
                        f"voip-sip-notify-{subscription.key}",
                    )
                    continue
                self._notify_subscribers(subscription.target, event="message-summary")
                break

    def _notify_subscribers(self, target: str, *, event="presence") -> None:
        subscriptions = self.subscriptions
        assert subscriptions is not None
        now = time.time()
        for key, subscription in tuple(subscriptions.items()):
            if subscription.expires_at <= now:
                subscriptions.pop(key, None)
                self._cancel_timer(self.subscription_timers, key)
                continue
            if subscription.event != event or subscription.target.lower() != target.lower():
                continue
            result = self._notification_result(
                subscription,
                expires=max(0, int(subscription.expires_at - now)),
            )
            self._track(
                subscription.sender(subscription.request, subscription.addr, result),
                f"voip-sip-notify-{subscription.key}",
            )

    async def _expire_publication(
        self, target: str, entity_tag: str, expires: int
    ) -> None:
        await asyncio.sleep(expires)
        publications = self.publications
        if publications is None:
            return
        current = publications.get(target.lower())
        if current is None or current.entity_tag != entity_tag:
            return
        if current.expires_at > time.time():
            return
        publications.pop(target.lower(), None)
        self._notify_subscribers(target)

    async def _expire_subscription(
        self, key: str, to_tag: str, expires: int
    ) -> None:
        await asyncio.sleep(expires)
        subscriptions = self.subscriptions
        if subscriptions is None:
            return
        current = subscriptions.get(key)
        if current is None or current.to_tag != to_tag:
            return
        if current.expires_at > time.time():
            return
        subscriptions.pop(key, None)
        self._release_dialog_observers_if_unused()
        result = self._notification_result(current, expires=0, terminated=True)
        await current.sender(current.request, current.addr, result)

    def _track(self, awaitable: Awaitable[object], name: str) -> None:
        tasks = self.tasks
        assert tasks is not None
        task = asyncio.create_task(awaitable, name=name)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def _replace_timer(
        self,
        store: dict[str, asyncio.Task[object]] | None,
        key: str,
        awaitable: Awaitable[object],
        name: str,
    ) -> None:
        assert store is not None
        self._cancel_timer(store, key)
        task = asyncio.create_task(awaitable, name=name)
        store[key] = task
        tasks = self.tasks
        assert tasks is not None
        tasks.add(task)

        def _done(completed: asyncio.Task[object]) -> None:
            tasks.discard(completed)
            if store.get(key) is completed:
                store.pop(key, None)

        task.add_done_callback(_done)

    def _cancel_timer(
        self,
        store: dict[str, asyncio.Task[object]] | None, key: str
    ) -> None:
        if store is None:
            return
        task = store.pop(key, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            if self.tasks is not None:
                self.tasks.discard(task)

    async def stop(self) -> None:
        """Cancel application transactions and discard expiring state."""

        if self.mailbox_unsubscribe is not None:
            self.mailbox_unsubscribe()
            self.mailbox_unsubscribe = None
        self.mailbox_archive = None
        for name in ("dialog_unsubscribe", "endpoint_unsubscribe", "parking_unsubscribe"):
            if (unsubscribe := getattr(self, name)) is not None:
                unsubscribe()
                setattr(self, name, None)
        tasks = self.tasks
        if tasks:
            for task in tuple(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            tasks.clear()
        if self.subscriptions is not None:
            self.subscriptions.clear()
        if self.publications is not None:
            self.publications.clear()
        if self.publication_timers is not None:
            self.publication_timers.clear()
        if self.subscription_timers is not None:
            self.subscription_timers.clear()


@dataclass(frozen=True, slots=True)
class SipPresencePublication:
    """One bounded presence publication owned by the application surface."""

    entity_tag: str
    body: bytes
    expires_at: float


SipFollowUpSender = Callable[
    [sip.SipMessage, tuple[str, int], SipRequestResult],
    Awaitable[sip.SipMessage | None],
]


@dataclass(slots=True)
class SipPresenceSubscription:
    """One authenticated presence dialog and its notification transport."""

    key: str
    target: str
    subscriber: str
    to_tag: str
    request: sip.SipMessage
    addr: tuple[str, int]
    expires_at: float
    sender: SipFollowUpSender
    next_cseq: int
    notify_lock: asyncio.Lock
    event: str = "presence"
    mailbox_id: str = ""
    dialog_endpoint_id: str = ""
    dialog_filter: tuple[str, str, str] = ("", "", "")
    dialog_version: int = 0
    dialog_snapshot: tuple = ()


def _publication_expires(value: str) -> int:
    expires = 3600 if not value.strip() else int(value)
    if expires < 0:
        raise ValueError("invalid presence expiry")
    return min(expires, 3600)


def _valid_pidf(body: bytes, target: str) -> bool:
    if len(body) > 64 * 1024:
        return False
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return False
    if root.tag != "{urn:ietf:params:xml:ns:pidf}presence":
        return False
    try:
        entity = sip.parse_sip_uri(root.attrib.get("entity", ""))
    except (ValueError, sip.SipError):
        return False
    return entity.user.lower() == target.lower()


def _uri_from_header(value: str) -> str:
    start = value.find("<")
    end = value.find(">", start + 1)
    if start >= 0 and end > start:
        return value[start + 1 : end].strip()
    return value.split(";", 1)[0].strip()


def _closed_pidf(target: str) -> bytes:
    return (
        '<presence xmlns="urn:ietf:params:xml:ns:pidf" '
        f'entity="sip:{target}@localhost"><tuple id="phone">'
        "<status><basic>closed</basic></status></tuple></presence>"
    ).encode()
