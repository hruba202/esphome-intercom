# HA PBX development branch

Development takes place on `feature/ha-pbx`, starting at
`16714a9b5000126278ad90d87bff1ec9cd1b6c2a`. This document tracks unfinished
implementation; it is not a released-feature guide.

## Agreed contract

VoIP Stack remains the PBX inside Home Assistant. Asterisk and FreeSWITCH are
protocol references and test peers, never runtime dependencies. The existing
session registry owns calls, generations, controllers, timers and cleanup.
Card settings, HA actions and SIP feature codes use the same backend commands.

Phone settings are persistent defaults for future calls. Explicit authorized
automation commands override those defaults for their own call. An automation
that only sends a notification does not claim the call. Authorization and
external-call restrictions remain in force after forwarding.

No configured ringing timeout means no application-imposed ringing limit.
SIP transaction timers and explicit outgoing-action answer deadlines remain
separate. Group members do not apply personal forwarding by default; the group
owns its final fallback and first-answer arbitration.

Stable endpoint/trunk identities back human-facing names and HA selectors.
Renaming must preserve policies, mailbox contents and trunk preferences.
Calls requiring PBX policy use the existing HA routing hint in the phonebook.
Direct calls to a device IP remain outside HA routing control.

## Delivery checkpoints

- [ ] Phone defaults, persistence, card controls, native triggers, session-owned
  no-answer deadlines and explicit automation takeover. Remove hidden default
  ESP ringing limits from maintained profiles; preserve user-selected limits.
- [ ] Multiple named trunks with one default, validated registration, independent
  transport ownership and inbound identification. Outbound precedence is action,
  endpoint, default; an unavailable selected trunk never silently fails over.
  Extend existing Automation origination to external calls. Correlate explicit
  ESP action overrides through a session-owned Call-ID, not a next-call flag.
- [ ] Mailboxes, recording application, card playback/delete, SIP access menu and
  RFC 3842 MWI through the existing RFC 6665 subscription owner. Proposed aliases:
  `*97` own access, `*98` PIN-protected other access, `*60<extension>` deposit,
  `voicemail.<phone>` deposit and `mailbox.<phone>` authenticated access.
  Validate alias collisions before enabling them.
- [ ] Optional conversation recording through existing media owners, separate
  from voicemail counts. Bounded off-loop WAV writes, quotas, atomic metadata,
  restart recovery and authenticated per-phone access. No public `/www` files.
- [ ] Parking, unpark and directed pickup using existing REFER/Replaces and call
  ownership. Atomic slot acquisition; configurable timeout/return destination;
  optional HA-media hold music and RFC 4235 dialog subscriptions for BLF.
- [ ] Native HA events/entities, optional card shortcuts, documented examples,
  README organized around HA first and optional ESP hardware second, generated
  documentation site using the same Markdown, and standard ESPHome sound-action
  compatibility qualification for Audio Stack.

## Defaults

All new optional routing and recording features start disabled. Mailbox
enablement does not automatically enable forwarding. Own-mailbox SIP access
requires the authenticated account; other-mailbox access requires a configured
PIN. SIP From/display-name alone does not authorize access.

Initial configurable limits: voicemail 180 seconds, conversation recording
60 minutes, combined storage 1 GiB, parking 180 seconds followed by a 30-second
return call. No automatic recording deletion. On disk failure, stop recording
and report the error without stopping the ordinary conversation. No new idle
polling or tasks for unused features.

## Current implementation evidence

The first increment adds an immutable policy value to phone snapshots and
validates persistence before writing. The shared pre-ring resolver handles
unconditional, busy and unavailable contact forwarding, stable target identity,
loop rejection and explicit automation bypass. Browser origination and inbound
SIP now call that resolver before their transport-specific dispatch.

The public `set_phone_settings` action and collapsed card routing controls now
edit unconditional, busy and unavailable destinations. ESP settings use the
existing entry without creating duplicate devices or reloading SIP. Legacy SIP
offline rules normalize into this same policy, so clearing a rule really clears
it. The phonebook uses the existing HA-routing hint for controlled destinations.
No-answer execution, mailbox applications and subsequent checkpoints remain
unfinished. Unavailable mailbox applications fail closed instead of dialing the
ordinary phone or an external trunk.

The next increment introduces session-owned no-answer timers, an optional native
trigger duration and card controls for explicit timeout/outcome selection. A
native notification leaves fallback enabled; an accepted owner/state change
invalidates it. The initial timer test exposed an incorrect resource lookup
name; timer ownership now follows the existing `kind:call_id` convention.

Real SIP qualification also exposed missing provisional-state publication for
Automation origination. That path now publishes remote ringing before waiting
for final acceptance, so endpoint policies can observe it without treating
early media as an answer. A one-second phone deadline cancels a real INVITE
before the action's five-second limit, without starting TTS.

Unconfigured normal SIP calls no longer acquire the old 60-second application
ringing cap. Initial signaling, CANCEL, explicit action deadlines and other SIP
transactions remain bounded. A real UDP peer answered successfully after 61
seconds and completed BYE. Maintained YAML local 30-second guards are now
commented examples, not enabled defaults. Group-specific routing limits still
need qualification separately.

Native TTS takeover now attaches an Automation application to an unanswered
incoming SIP or local browser call. It retains the original session and releases
the previous destination. A native execution supplies the controller and call
generation, including for an implicit `hangup` after speaking. A naked Call-ID
does not grant application ownership. Late answers from the former destination
are rejected once an application owns the call.

Forwarding to another Automation contact reuses the current application media
and RTP socket, but releases the old execution's control. Browser callers retain
their card's hangup command. Announcement synthesis waits for that browser's
current media connection; hanging up during this wait releases the application
without starting synthesis.

Remaining before completing phone policies: outgoing-call retargeting across
every source type and integration of later mailbox destinations.
Forwarded endpoint identities now stay in the existing session metadata, including
local-browser and group origins, so later trunk legs retain their access limits.
Do not promote this branch or claim the full six-phase plan is implemented.

Further startup tests reproduced two boundaries: final SIP acceptance must end
ringing before media preparation finishes, and remote BYE must cancel media
preparation itself. Automation origination now updates its internal SIP state
after final acceptance and owns media startup as a session task. Its public action
still waits for media readiness before allowing TTS. Both controlled startup
scenarios pass with a real UDP SIP peer.

The second increment's protocol/frontend/documentation run passed 238 tests and
21 subtests; the HA run passed 347 tests, including the 61-second SIP witness.
Later focused runs cover the startup changes and forwarding provenance. The
long-running witness and failure/recovery evidence are under the ignored
`test_captures/ha-pbx/` directory.

The third increment passed 356 HA tests before the final implicit-hangup and
browser-cancellation witnesses. The resulting focused run passed 55 tests,
including real UDP announcement samples, browser-source handoff and application
transfer without a second receiver. These use a simulated browser RTP adapter,
not a physical browser or phone qualification. The implicit-hangup witness first
reproduced a missing generation binding; the fix extends the existing native
action binder instead of weakening the application's ownership checks.

The final application/routing subset passed 92 tests (one slow witness excluded).
It checks that a phone answered, terminated, reassigned or replaced while the
advertised address is being resolved cannot be claimed by the old execution.
A takeover also drops the former destination from command scope while preserving
the caller. The software suite passed 1931 tests and 162 subtests (4 deselected).

Outgoing Automation announcements now use the existing `DialForkController` to
arbitrate a SIP answer against a no-answer forwarding decision. The same
application session and generation survive retargeting; each destination gets
its own SIP Call-ID. The losing leg finishes CANCEL/487 or ACK/BYE before the
next leg is used. No browser, extra RTP receiver or second SIP state machine is
created. The original action's answer deadline covers the complete sequence.

Five real UDP tests cover successful retargeting, a late 200 during CANCEL,
busy and unanswered replacement phones, and cancellation during replacement
ringing. They check exact announcement PCM on the selected phone, no audio on
the abandoned phone, retained application Call-ID and complete cleanup. This
path currently supports the configured no-answer fallback with failure ending
the announcement. Resuming a cancelled destination, browser-originated SIP
retargeting and mailbox destinations remain unfinished.

Investigation: the original witness reached the phone deadline but failed with
`call_not_forwardable`, because the forwarding entry point only accepted an
incoming INVITE or a local browser bridge. RFC 5359 section 2.9 shows closing the
unanswered destination attempt before trying the next destination. The local
`DialForkController` already owns this arbitration and cancellation barrier, so
the outgoing application now supplies it with a SIP candidate and the existing
pending-route decision future. The falsifiable check is that the first peer gets
CANCEL (and BYE after a late answer), the second gets a distinct INVITE, and the
HA call token stays unchanged. This passes with exact RTP audio verification.
Replacement legs use `add_leg` before attaching their client and watcher, so
their wire Call-IDs resolve to the original application session.
The complete HA suite after this increment passed 368 tests; the independent
fork/controller regression suite passed 20 tests and 4 subtests.

Browser-originated SIP attempts can now hand their real browser source to the
existing forwarder. The original session retains its generation, caller and
card media identity. Closing the destination uses `close_leg`, including its
watcher and CANCEL/late-answer cleanup; no second phone is registered. The same
source adapter survives consecutive no-answer destinations. Supported targets
include another SIP endpoint, a local browser and an Automation application.
Native unanswered TTS actions can also take this browser source directly.

The UDP witnesses cover both RTP directions after SIP forwarding, chained
timeouts into an Automation contact, caller hangup, late SIP acceptance and
answer during route resolution. The latter initially left a pending source
adapter attached after abort; rollback now removes it while keeping the
answered call intact. Cancellation after retiring a destination uses the
session's termination barrier to release the caller. Explicit camera intent is
retained for browser forwarding and SIP video offers; this tests signaling and
audio fallback, not physical video presentation. Failed replacement routes
currently require termination rather than resuming a cancelled SIP attempt.

The initial outgoing 180 witness also exposed an older projection mismatch:
the SIP client returned `ringing`, but the outgoing publisher only recognized
`remote_ringing`, publishing `calling` before the watcher updated the session.
The outgoing boundary now converts the result before publishing it. Incoming
ringing semantics and the shared SIP state vocabulary remain unchanged.
This increment passed 380 HA tests and the software suite's 1931 tests plus
162 subtests (4 deselected). The source handoff is covered by twelve browser
policy scenarios, including camera-intent preservation and cancellation after
the old SIP leg has closed. No physical device or production HA was changed.

## Named trunks in progress

The parent configuration's former single trunk migrates to a stable native
trunk subentry. New lines use HA's Add SIP trunk flow and Device selectors.
Credentials and registration lifecycle remain in `SipTrunkClient`, with one
client per line owned by the existing SIP runtime. The old enable-trunk branch
of the global configuration wizard is removed; global edits preserve phone
policies and default-line selection.

Validation uses the client's existing registration owner. Real UDP Digest
tests verify that the validated client is adopted without a second REGISTER,
and that a failed password edit leaves the original registration and persisted
credentials intact. A fault-injection test reproduced a partial multi-line
update; reconciliation now prepares all clients before atomically changing
ownership, and closes prepared replacements on failure.

PJSIP's Contact `line` approach identifies registrations sharing a provider IP,
after source trust checks. RFC 5626 additionally keys TCP/TLS bindings by
instance/reg-id. Logical trunk identity now gives a stable SIP instance; a
validated replacement alternates two persisted flow labels. A TCP registrar
oracle compares those RFC keys and proves that unregistering the old flow does
not remove its replacement.

Call actions, phone preferences and default selection now drive browser and
Automation origination. The card reuses HA's device registry to show named line
choices. An independent SIP phone has also called through the real HA listener
using its preferred second trunk, with exact PCM checks in both RTP directions.
Closing calls preserves both registrations. An unavailable explicit selection
does not dial the healthy default. Access restrictions now recognize every
configured provider URI, including non-default lines.

Native per-line readiness sensors use lifecycle events, not polling. Diagnostic
exports enumerate lines without provider identities; detailed card diagnostics
retain separate client snapshots. Call-leg trunk identity tracks lines actually
in use, separately from the preferred line carried by a logical call.

Browser forwarding can resume a previous SIP destination by redialing it after
the failed replacement has finished cleanup. The logical call identity and
caller remain unchanged; cancelled SIP transactions are never reused. The test
verifies two distinct INVITEs to the original peer around a rejected replacement.

The combined gate at this point passed 1938 software tests with 162 subtests
(4 deselected) and 396 HA tests. No production HA or firmware was deployed.

Still required before declaring the trunk phase complete: finish all incoming,
forward/group and transfer selection witnesses,
explicit per-action ESP correlation and firmware qualification. The explicit
ESP selector currently rejects unsupported use instead of silently ignoring it.
Mailbox, recording and parking work remains pending.

References for this increment:

- https://docs.asterisk.org/Configuration/Channel-Drivers/SIP/Configuring-res_pjsip/Configuring-Outbound-Registrations/
- https://www.asterisk.org/the-pjsip-outbound-registration-line-option/
- https://www.rfc-editor.org/rfc/rfc5626.html

Executed so far:

- Software suite: 1926 tests and 162 subtests passed (4 deselected).
- HA suite: 330 passed before the final legacy-policy migration changes.
- Migration/configuration/HA regression subset after that change: 54 passed;
  persistence and endpoint registry: 36 passed with 13 subtests.
- Real UDP SIP/RTP Automation suite: 24 passed, including forwarding, immediate
  and delayed answer, exact announcement PCM and BYE after the audio tail.
- Frontend runtime and documentation subset: 12 passed.
- The isolated softphone suite initially failed because its partial fake module
  graph did not contain the newly used resolver. The fixture now models that
  boundary; policy behavior is tested separately against production modules.
- A standalone endpoint-enum test loader also needed a proper package context
  for the new policy dependency. No production fallback imports were added.
- Legacy-policy regression initially reproduced the missing normalization; the
  fix moves conversion to persistence loading and removes the parallel routing
  fallback. Card runtime assertions compare serialized commands across JS VM
  realms instead of comparing objects with different prototypes.

Before promotion require public-action tests, real SIP/RTP calls with known
audio, cleanup, repeated calls, simultaneous calls, answer/timeout races,
automation overrides, codec transitions and retained community regressions.
Never treat these initial policy tests as hardware qualification.

## References

- https://developers.home-assistant.io/docs/config_entries_index/
- https://www.rfc-editor.org/rfc/rfc3842.html
- https://www.rfc-editor.org/rfc/rfc6665.html
- https://www.rfc-editor.org/rfc/rfc5359.html
- https://www.rfc-editor.org/rfc/rfc4235.html
- https://docs.asterisk.org/Configuration/Applications/Voicemail/Message-Waiting-Indication/
- https://developer.signalwire.com/freeswitch/applications/utilities/
