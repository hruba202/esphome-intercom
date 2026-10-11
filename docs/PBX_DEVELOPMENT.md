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
- [x] Mailboxes, recording application, card playback/delete, SIP access menu and
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

### Mailbox implementation checkpoint

The branch now has a call-owned mailbox deposit application. It reuses the
local application media preparation used by Automation, including answer
commit, negotiated receive mappings, re-INVITE, browser-source ownership and
session cleanup. `send_to_voicemail` transfers the current Automation execution;
completion of that old execution does not end the mailbox recording.

Mailbox configuration is private phone-subentry data, or the existing parent
entry's ESP settings keyed by stable endpoint identity. A phone rename preserves
this data. Enabling a mailbox does not enable forwarding. Explicit
`voicemail.<phone>` destinations and no-answer mailbox outcomes reach the same
deposit application. SIP feature codes, namespace validation on every editing
surface, mailbox access menus and MWI are still being implemented.

The archive writes native decoded PCM outside the event loop through a bounded
queue. Different negotiated formats become lossless WAV segments. Storage quota
includes active writes; completed messages use private files and atomic
metadata. Interrupted recordings recover from their private pending journals.
No second RTP receiver, decoder, periodic disk poll or public `/www` file is
introduced. The read/delete UI and authenticated playback are not yet present.

Executable evidence includes:

- A real SIP call originated by an HA script, TTS followed by mailbox deposit,
  exact received PCM on disk, remote hangup and recording time-limit cleanup.
- A real audio re-INVITE during recording, preserving both native sample rates.
- Browser-originated SIP ringing followed by a configured no-answer mailbox:
  CANCEL reaches the former destination, the caller keeps its session identity,
  and its audio reaches the selected mailbox.
- Direct incoming SIP deposit through the actual HA listener, without browser
  playback resources or a second outgoing SIP call.
- Archive crash recovery, disk failures, cancelled creation, concurrent quota
  accounting, slow writers, cross-mailbox access rejection and clean retries.
- Native HA phone reconfiguration preserving mailbox identity and settings.

These are automated socket and HA-runtime tests. They do not constitute a test
of a physical SIP handset, its voicemail button, MWI indicator or audio output.


### Clarified mailbox playback contract

Selecting a message in the existing phone card starts an ordinary call from that
phone to its mailbox, without a PIN for its own mailbox. The mailbox application
plays the selected message over the existing call media path. A conventional
SIP phone reaches the same application and selects messages through its menu.
The card only retrieves message metadata and sends commands; there is no
separate HTTP audio player or public/signed audio-download endpoint. Any selected
message reference belongs to that exact call, never a global next-call setting.

Own-mailbox access uses the existing PBX endpoint identification for registered
SIP phones and trusted, configured ESP endpoints. No new mandatory PIN is added
to normal own-phone access. Home Assistant card controls reuse the existing
phone-control authorization boundary.

### Mailbox ownership and permanent endpoint removal

A mailbox belongs to the stable phone endpoint. Permanently deleting an HA
softphone or removing an ESP endpoint from VoIP Stack must remove its voicemail
messages, mailbox PIN and mailbox settings through the same backend lifecycle.
Temporary unavailability, ESP reboot, SIP registration expiry and integration
reload do not delete a mailbox. Recreating a phone with the same display name
must not recover the deleted endpoint's mailbox. Qualification must cover both
permanent removal and the temporary-disconnection cases.

### Telephone mailbox qualification

The same mailbox application now serves `*97`, `*98`, named access and deposit
routes. Its configurable HA TTS voice is required globally. Keypad input
interrupts the prompt and keeps the digit; the menu's message numbering remains
fixed for that call. Other-mailbox access requires the mailbox's configured PIN.
PIN digits are not published as ordinary call DTMF events or logged.

The automated baresip test registers a real baresip process, subscribes to
message-summary, calls `*97`, receives recorded audio, selects and deletes the
message with RTP telephone events, observes the updated MWI counts and hangs
up. A real Chromium card in the HA lab has also completed this telephone path
using its existing keypad and the configured local xTTS provider. Captured
browser audio contains the deterministic 550 Hz message recorded from baresip.
This qualifies the existing keypad path, not the unfinished message-list UI.

The real MWI test exposed a missing Contact in successful SUBSCRIBE responses.
The common SIP response builder now supplies Contact and the dialog route set
for SUBSCRIBE/NOTIFY, following RFC 6665. Removing a mailbox sends a final
`Subscription-State: terminated;reason=noresource` and releases the existing
subscription timer. Removing a phone during a deposit terminates its call,
discards the mailbox files and suppresses the new-message event.

Archive read-state and deletion operations complete their disk and in-memory
updates before propagating cancellation, using the existing cleanup primitive.
A regression cancels each operation immediately after the real disk write and
verifies that counts and quota agree before and after reloading the archive.

The dedicated card list now calls that same application to listen and delete.
Its read-only metadata query returns the current call's frozen DTMF selection
for a stable message ID. Deletion waits for the application's confirmed event
before hanging up. The browser test clicks the actual list buttons, detects the
recorded 550 Hz signal in received audio, deletes the message and verifies idle
with the list still open. Runtime tests also cover an unrelated active call,
an idle phone retaining its previous Call-ID, and the public schema-2 call
response arriving before a separate phone-state event.

### One stored message and optional HA processing

Messages are now recorded directly in HA's media directory. A saved message has
one canonical WAV. Format-change fragments are finalized before publishing the
received event, then removed. The highest recorded rate, depth and channel count
are retained. Interrupted finalization keeps recoverable source material; the
archive checks quota for the finalization work and recovers one file on reload.

There is no export-copy action. `get_voicemail` and the native
`voicemail_received` trigger return the existing `file_path` and
`media_source_id`. The normal HA media resolver opens that same file. Telephone
playback, STT and automation attachments share it; deleting the message removes
it. This supersedes the earlier private-storage foundation described above.

`transcribe_voicemail` uses an existing HA STT entity, without a separate Whisper
client. It is optional and runs only when invoked. Triggered executions bind
their saved message independently, so concurrent messages cannot substitute for
one another. Tests exercise the actual HA entity/service interfaces, streamed
PCM, provider errors, timeout, cancellation, retries and unchanged unread state.

The HA laboratory has also run a native automation through its normal config
API: a baresip call records spoken test audio, `voicemail_received` selects the
saved message implicitly, `get_voicemail` returns the existing media file,
Wyoming Whisper transcribes it, and the automation creates an HA persistent
notification. The temporary automation and notification are removed after the
test. The saved audio contains the phrase "The package has arrived", also
present in the provider's returned transcription. No external email or mobile
notification is sent by this qualification.

The remaining PBX checkpoints above still need completion. Nothing in this
branch has been published or deployed to the user's home HA.

### Pending automation forwarding

Pending outgoing announcements now carry the selected trunk and failure policy
through the existing route decision. Failed forwarding uses the common
`RouteAbortContext`: resume redials the prior destination, busy records a 486
termination, and terminate closes the session. One install path owns replacement
legs and their RTP reservations. The source application, generation and public
call reference survive a successful forward or resume. The original cumulative
answer deadline remains in force.

Socket tests cover a busy or unresolved replacement with each policy, explicit
forwarding from one registered trunk to another with actual announcement RTP,
late answers to the cancelled original leg, and cancellation during retargeting.
The registration flows remain ready after the calls have released their dialogs.

### Public mailbox configuration and no-answer routing

The native phone config flow and card Options can enable a mailbox. The existing
`set_phone_settings` action accepts the voicemail no-answer outcome, with the
phone's own enabled mailbox as the empty-destination default. The same controls
remain backend-owned for ESP mirror cards. Disabled or missing mailbox targets
are rejected before persisting the timeout.

A real Chromium card configured this rule in the HA lab, then a baresip caller
left a recorded 550 Hz message after the unanswered call. The card called its
own mailbox, selected and deleted that message, and the original media file was
removed. With the lab's optional routing automations temporarily disabled, the
configured two-second timeout produced an answer after 2.059 seconds. Their
previous enabled states and the phone's previous settings were restored.

Mailbox removal also waits for deposits whose remote party has already hung
up. All affected applications are marked before awaiting their existing cleanup
barriers, so a concurrent save cannot announce a new message for the removed
phone. Cancelling the removal caller waits for the owned deletion to finish.
The regressions hold both disk finalization and the final archive lookup open,
then verify no message event, no WAV, no active writer and no cleanup errors.

### SIP consultation and shared REFER policy

Three registered baresip processes reproduced an occupied-phone rejection when
the middle phone held its established call and called a third phone. SIP source
consultations now acquire another claim in the existing session registry. The
phone remains busy to unrelated incoming calls until all its owned calls finish.
Closing either call first and late releases cannot clear the remaining claim.
Browser and ESP phones retain their existing single-call behavior.

REFER requests from outbound dialogs now use the same routing and external-call
permissions as those received by the inbound listener. The previous callback
could only find another outbound client and rejected an ordinary inbound leg.
The shared handler delegates to the actual UDP/TCP dialog owner instead.

Qualification: 106 registry tests and 27 subtests, 33 transfer-policy tests, and
458 HA tests passed. A permanent real-baresip test covers consultation, returning
to the original call and cleanup. These results do not qualify completed attended
transfer: its final INVITE with Replaces still receives 486 in the three-phone
witness. That dialog replacement is the next required step.

Protocol references: RFC 3891 and Asterisk's res_pjsip remote-attended-transfer
documentation. Asterisk distinguishes replacement of locally owned dialogs
from delegation to another server; the replacement must retain existing media
and ownership rather than ring the busy destination again.

The next increment now receives INVITE/Replaces and matches Call-ID plus both
dialog tags. A live REFER grants only its opposite identified endpoint access
to the consultation. The same identified user can also replace its own dialog.
Wrong tags, missing authorization, early-only against a confirmed dialog and
concurrent replacement attempts receive distinct SIP failures. The grant belongs
to the existing session and is released after the REFER or call terminates.

Media preparation uses the existing re-INVITE code. After the successful SIP
response, the established destination client, relay and endpoint claim move to
the new source session together. Destination re-offers, DTMF, BYE watching and
cleanup are rebound to that session. No second destination INVITE or parallel
media path is created for the transfer.

Real baresip tests verify both received audio tones, hangup from either remaining
phone, immediate redial and rejection of unauthorized replacement requests. Other
tests hold a prepared replacement while the destination hangs up, or cancel it
before commit, and verify that the original call remains independently usable.
Video uses the shared media preparation but still needs a dedicated transfer
witness before being called qualified.

This test series also exposed a cancellation bug in the existing SIP dialog
reader. Cancellation during its child-task cleanup could bypass lock release,
leaving BYE blocked without an active reader. A deterministic regression fails
on that exact schedule before the fix. Lock release is now in its own finally
block; no timeout or retry policy changes are involved.

The resulting HA suite passes 463 tests. The six real-baresip scenarios include
both hangup sides, immediate redial and unauthorized Replaces on the wire.
Recorded audio is inspected after the independent peer finalizes its WAV files;
an HA idle state alone is not a file-flush barrier for the peer's capture filter.

### Dialog notifications for BLF

The existing SIP application subscription owner now serves RFC 4235 `dialog`
notifications alongside presence and MWI. Full documents use the call registry's
actual claims, optional SIP identities and a per-subscription version. Changes
are event-driven; identical state does not enqueue another notification. Native
HA softphone and configured ESP endpoints can be monitored for HA-owned calls.
Firmware-side subscriptions on ESP remain outside this backend increment.

Qualification includes a real SIPp REGISTER/Digest and SUBSCRIBE exchange while
baresip phones ring, answer and hang up. SIPp acknowledges every NOTIFY; the
captured XML verifies versions and state transitions, including final noresource
after endpoint removal. These tests do not claim a physical handset's BLF LED
was inspected.

Initial claim publication now exposes the session's claim before notifying
registry observers. A failed busy claim rolls back its staged resource and
revision. This avoids an observer seeing a busy endpoint whose authoritative
session does not yet own it.

The complete HA run at this checkpoint passed 471 tests. The final observer
cleanup passed the 15-test BLF/presence/MWI subset, including the independent
SIPp client. Dialog listeners are removed when the last dialog subscription
ends, so unused BLF does not retain call-event observers.

### Local audio transfer without peer REFER

ESP firmware currently advertises ACK, BYE, CANCEL, INVITE, OPTIONS and UPDATE,
and returns 501 for REFER. An attended transfer now has a local audio fallback
when the remaining peer returns 405/501. An existing caller with no HA endpoint
identity uses the local path directly instead of authorizing an anonymous
replacement INVITE. The referring phone must still own both matched dialogs.

The shared RTP relay can adopt two existing transports atomically. Codec
contracts, RTP sender identities and clock positions remain attached to the
remaining peers. Reservations are merged into the retained pair immediately;
unused ports become reusable and old cleanup cannot release another call's
later allocation. A failed transport handoff restores both original protocols.

Both transfer directions are covered: an inbound caller keeps its session;
two outbound peer dialogs move together to one new session while the referring
phone's old session retires after its final NOTIFY. Retired legs cannot issue
further REFER controls. A staged re-offer cannot commit into a replaced relay
even if the logical call generation is unchanged.

Independent SIPp peers return 501 and echo RTP while baresip performs transfers.
Tests cover registered callers, static callees without REGISTER, callers without
an HA phone entity and repeated transfers. The destination's captured WAV must
contain its tone returned through the other peer. Exactly two ports remain
reserved after each transfer and none after hangup. Video splicing is not
implemented; existing negotiated video is preserved by refusing that fallback.

The focused integration run passed 50 tests; primitive, port and media-update
regressions passed 86 tests and nine subtests before the full qualification run.

Full qualification of this increment passed 1,976 software tests with 178
subtests (four deselected) and 476 HA tests. Port ownership remains a single
retained pair across repeated transfers; later cleanup cannot release reused
ports belonging to another call. The newly added relay tests use real HA
imports rather than depending on the old protocol harness's import order.

### Parking ownership, implementation in progress

The slot reservation belongs to the parked call's existing session. Picking up
and returning a timed-out call compete for the same reservation. A failed pickup
releases its claim; if the parking deadline has already expired, the return
proceeds without another polling task or an additional timeout. Hangup revokes
the reservation and cancels its session-owned timer. Old cleanup cannot release
a slot already reused by another call.

Browser audio and video adapters are now indexed by call and endpoint together.
Two phones participating in the same logical call must retain independent
media updates and cleanup. A camera change on one phone cannot select the other
phone's adapter merely because their Call-ID is shared.

The reservation tests and existing application/media regressions pass. This is
internal groundwork: parking is not yet exposed as a working user feature.
The SIP pickup, audio handoff, return route and public controls still need
implementation and end-to-end qualification.

This foundation passes 1,976 software tests (178 subtests) and 480 HA tests.
The SIP endpoint manager also exposes the listener's existing awaited BYE
operation. Call cleanup can wait for that transaction instead of receiving only
confirmation that BYE was scheduled. A protocol test with two active dialogs
checks exact selection, response-before-completion and repeated cleanup. The
28-test real-call subset covers transfers and mailbox access after this change.
