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

Remaining before completing phone policies: native TTS takeover of a phone call,
forwarding to applications and outgoing-call retargeting across every source type.
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
