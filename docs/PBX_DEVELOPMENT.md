# HA PBX branch: implementation and qualification

The work is isolated on `feature/ha-pbx`, based on
`16714a9b5000126278ad90d87bff1ec9cd1b6c2a`. It is not a published release.
Asterisk and FreeSWITCH are protocol references and test peers, not dependencies.

## Ownership and public behavior

The existing EndpointCallSession owns the call, generation, participants,
controller, timers and cleanup. Card controls, HA actions and SIP addresses
enter that same backend. Settings belong to a phone, never to a browser tab.

Optional phone policies cover unconditional forwarding, busy/unavailable
fallback and no-answer behavior. An unset ringing timeout imposes no PBX
application deadline; SIP transaction timers remain active. Accepted automation
commands override defaults for their own call. Notification-only automations
leave the default action intact. Group branches do not individually answer via
personal forwarding rules while other members are still ringing.

Multiple named trunks have independent transports and registration state.
Outgoing selection is explicit action, then phone preference, then default.
A failed selected trunk does not silently switch providers. ESP action overrides
use a session-owned request correlated by Call-ID, endpoint and destination;
there is no global next-call setting.

Voicemail uses the existing local call application, native media storage,
interruptible TTS/DTMF and RFC 3842 message-summary subscriptions. Own-mailbox
access uses the identified endpoint; other-mailbox access requires its PIN.
Removing a phone removes its mailbox and recordings. Card playback/deletion
uses the same call/IVR path as a handset. STT remains an optional HA action.

Parking retains the caller's real SIP dialog or existing browser participant.
The lot owns slot claims and return deadlines; another phone retrieves the
same call. Directed pickup targets an individual ringing phone. RFC 4235
subscriptions report phone and slot state. Conversation recording observes the
existing media path and writes through the bounded shared archive. It does not
create a second call or RTP receiver. Disk failure stops recording, not audio.

## References and implementation checks

- RFC 3261 and RFC 3264 define dialogs, cancellation and negotiated media.
- RFC 5359 supplies interoperable call-flow examples, including parking.
- RFC 3842/RFC 6665 cover message waiting; RFC 4235 covers dialog subscriptions.
- Asterisk Dial retains the caller while destination attempts fail or time out.
  Its pickup and parking applications are references for feature semantics;
  feature-code digits remain local dial-plan configuration.
- Asterisk MixMonitor and FreeSWITCH recording hooks illustrate observing an
  existing media path rather than creating another telephone participant.

The tests exercise production SIP/RTP code with independent baresip processes,
real UDP peers, decoded audio and two Chromium phones. They distinguish final
SIP answer from provisional media, and inspect released resources after hangup.
Failure injection covers allocation/startup, cancellation, disk errors, quota,
stale ownership, concurrent actions and retry. Native phone policy tests cover
forwarding, mailbox takeover, immediate/late busy and unavailable responses,
explicit decline, and caller hangup during destination retirement.

## Qualification boundaries

Hardware tests performed before this branch remain historical evidence only.
The candidate ESP component passes its host suite and the maintained Spotpear
FULL profile compiles. The compile fixture uses dummy Wi-Fi and must never be
flashed. No physical ESP validation is claimed for these new native API actions.

Conversation recording supports two-party audio, not conferences. Parking and
directed pickup reject active video before changing ownership. Directed pickup
targets individual phones, not branches inside a ringing group. Direct endpoint
calls that bypass HA cannot use backend routing, voicemail or recording.

Home HA remains on the separately qualified voicemail revision. The development
lab is the deployment target for the complete candidate. Nothing in this branch
has been pushed, published or promoted to dev/main as part of this qualification.

User-facing instructions are in the user guide, voicemail, parking, recording
and SIP trunk guides. The static documentation build reads those same Markdown
files. Detailed local runs and failure witnesses are kept in gitignored task
captures rather than being mixed into installation instructions.

## Final qualification record

- Software suite: 1,983 tests and 181 subtests passed, four explicitly deselected.
- Final HA suite: 569 tests passed with an independently built, pinned
  baresip/libre pair, including MWI, decoded media and retained-caller scenarios.
- Two Chromium phones recorded both synthetic voices, parked and retrieved from
  each participant, and hung up. The final diagnostic resource counters were all
  zero. The additional retained-caller tests cover parking return pickup and
  destination-specific no-answer behavior.
- ESP VoIP component: 160 tests passed. Spotpear FULL compiled with the candidate
  native API actions. Adding the compile-only standard speaker/announcement
  witnesses produced a 7,238,887-byte image and 182,431 bytes of static RAM.
- Documentation: strict MkDocs build and Chromium rendering passed. The source
  Markdown remains the only authoring copy. No site was published.

Both full suites passed after the final retained-caller changes. The final
Chromium run used the same production component tree and returned all 21
call-resource counters to zero. Documentation/schema checks passed 42 tests.
Qualification records remain under the private, gitignored task captures;
public hardware claims require separate device tests.
