# User guide

This guide describes the normal VoIP Stack workflow from installation to the
first local, SIP and trunk calls. Protocol and developer details are linked at
the end.

## Install Home Assistant VoIP Stack

1. Open HACS and download **VoIP Stack**.
2. Restart Home Assistant.
3. Open **Settings > Devices & services > Add integration**.
4. Select **VoIP Stack**.
5. Keep SIP port `5060` and RTP base port `40000` unless another service uses
   them.
6. Set **Advertise host** only when automatic detection does not produce an
   address reachable by every phone.

The first setup creates one ordinary Home Assistant browser phone. Its default
name comes from the Home Assistant location name, for example `Casa`. It is a
real logical phone, not a temporary setup object, and can receive calls even
before more phones are added.

## Add the dashboard phone

1. Add the **VoIP Stack card** to a dashboard.
2. Select **Home Assistant phone** mode.
3. Choose the phone created during setup from the visual device picker,
   for example `Casa`. Select it by name; you do not need to look up its ID.
4. Save the card.
5. Grant microphone permission when the browser asks. Grant camera permission
   only when this phone must transmit video.

The editor saves your selection automatically. Incoming calls for that phone
move every connected card bound to the same Device to `ringing`. The browser
that answers becomes the media owner.

<details>
<summary>What the card editor saves in YAML</summary>

`device_id` is Home Assistant's identifier for the phone you selected. The
editor fills it in for you. This is an illustration of the saved configuration,
not an ID to copy:

```yaml
type: custom:voip-stack-card
mode: ha_softphone
device_id: 0123456789abcdef0123456789abcdef
```

</details>

## Choose the calling phone in the editor

In Home Assistant's automation editor or **Developer tools > Actions**, the
**Calling phone** field in the Call action is a device picker: search for the
phone's name and select it.
You do not need to type or copy a long ID. Home Assistant writes `device_id`
automatically when it saves that selection.

**Calling phone** chooses who places the call. **Destination** chooses who receives it.
For example, select `Reception` in **Calling phone** and enter `Kitchen` in
**Destination**: Reception calls Kitchen. Selecting Kitchen as the calling
phone would reverse their roles.

This is a Device selection, rather than an entity ID such as `sensor.*`.
The picker presents the phone's readable name, just like other Home Assistant
selectors.

### What `device_id` and `endpoint_id` mean

If you inspect the saved YAML, these fields have different jobs:

```text
device_id   = the local phone performing the action
destination = the remote party to call
```

The same action works whether the local phone is a Home Assistant browser phone
or a compatible ESPHome phone.

<details>
<summary>Example YAML generated after selecting the calling phone</summary>

The long value below represents the selection saved by Home Assistant. Use the
**Calling phone** picker to populate your own value.

```yaml
action: voip_stack.call
data:
  device_id: 0123456789abcdef0123456789abcdef
  destination: Waveshare S3 Audio
```

</details>

`endpoint_id` remains an internal stable identity used to correlate a logical
phone, SIP session, media owner and WebSocket snapshot. It is useful to the
backend because a Device can be temporarily unavailable while its logical
endpoint and active session still exist. Users do not save it in card YAML and
do not pass it to phone actions.

If `device_id` is omitted, VoIP Stack uses the explicitly preferred phone. If
no preferred phone is configured but exactly one compatible phone exists, that
phone is used. Otherwise the action asks for a phone selection instead of
guessing.

Choose the preferred phone from **Settings > Devices & services > VoIP Stack >
Configure > Home Assistant phones**. Removing or renaming the original `Casa`
phone is allowed after another preferred phone has been selected.

## Add an ESPHome phone

1. Start from a maintained profile under [`yamls/`](../yamls/).
2. Change only the documented substitutions, network secrets and hardware pins.
3. Compile and flash the device.
4. Add it through the normal ESPHome integration.
5. Wait for VoIP Stack to discover its endpoint and publish it in the phonebook.

Custom profiles use ESPHome 2026.9.0 or newer and enable native API actions:

```yaml
api:
  custom_services: true
```

The `voip_stack` component supplies discovery, phonebook reception and these
call-control actions automatically, without a VoIP HA package:

```text
start_call
answer_call
decline_call
hangup_call
```

An ESP mirror card selects the existing ESPHome Device. VoIP Stack does not
create or own a duplicate Device.

## Make local calls

The destination may be a phonebook name, extension, group, SIP URI or public
number.

From a card, select a contact or enter the destination and press Call.

From an automation, use the visual editor:

1. Add the **VoIP Stack: Call** action.
2. In **Calling phone**, select the phone that should place the call by its name.
3. In **Destination**, enter who to call, for example `Kitchen`.
4. Save the automation.

You can make the same selection in **Developer tools > Actions** to try it.
Leaving **Calling phone** empty uses the preferred or sole compatible phone, as
explained above.

<details>
<summary>The corresponding action in YAML</summary>

Home Assistant fills `device_id` from your Calling phone selection:

```yaml
action: voip_stack.call
data:
  device_id: 0123456789abcdef0123456789abcdef
  destination: Kitchen
```

</details>

From an ESPHome phone, choose a contact and invoke its normal call action. The
ESP calls the selected destination through the route published by Home
Assistant. Direct ESP-to-ESP SIP remains possible when the phonebook contains a
direct route.

The public actions are:

| Action | Purpose |
| --- | --- |
| `voip_stack.call` | Start a call from the selected local phone |
| `voip_stack.answer` | Answer a pending call |
| `voip_stack.decline` | Reject a pending call |
| `voip_stack.hangup` | End the selected call |
| `voip_stack.forward` | Redirect a pending or ringing HA-owned call |
| `voip_stack.transfer` | Transfer an established call with SIP REFER |
| `voip_stack.set_dnd` | Change DND on an HA phone |
| `voip_stack.set_auto_answer` | Persist Auto Answer for an HA phone |
| `voip_stack.set_send_video` | Persist default camera transmission |

## Add more Home Assistant phones

Open **Settings > Devices & services > VoIP Stack > Add phone > Home Assistant
browser phone**.

Create one phone per independent place or browser session, for example:

- `Casa`, extension `666`;
- `Test`, extension `667`;
- `Reception`, extension `200`.

Each phone receives its own Device, call state, DND, Auto Answer, extension,
groups and video settings. Bind each card to the intended Device. Two cards in
one browser tab do not create two physical microphones or cameras, so use a
separate browser or Companion session for simultaneous independent media.

Numeric extensions must be unique across HA phones, SIP accounts, ESPHome
phones, manual contacts and Assist. If discovery introduces a duplicate,
**Settings > System > Repairs** lists the extension and the conflicting
names. Calls to that ambiguous number are rejected instead of selecting a
phone or falling through to a trunk. Give each destination a different
extension; for an ESPHome phone, change its extension on the device. The
repair clears automatically after the phonebook updates. Unique names and
explicit SIP addresses remain usable while you resolve the conflict.

## Register a SIP client or IP phone

1. Enable the local registrar in **VoIP Stack > Configure**.
2. Create an account through **Add phone > SIP account**, or use the
   `voip_stack.create_account` action.
3. Configure the SIP client with the Home Assistant advertise host, SIP port,
   username and generated password.
4. Use UDP or TCP according to the account and network configuration.

The registered client appears in the central phonebook while at least one
Contact binding is active. Account passwords are returned only by the account
management action and are not placed in entity state.

## Configure groups

Use phone Device entities or phone settings to assign comma-separated groups.

- A **ring group** rings all eligible members. The first answer wins.
- A **conference group** joins participants to one HA audio mixer.
- **Ring for conference calls** controls whether a member rings when another
  participant starts that conference.

Call the group name exactly as shown in the phonebook.

## Make Assist callable

Open **VoIP Stack > Reconfigure**, enable **Include voice assistant**, choose a
pipeline and assign an extension. Assist then becomes a normal phonebook
destination.

Hanging up terminates the Assist media leg and its active pipeline work. It
does not leave a listening call session behind.

## Dahua door stations

For registered or static Dahua devices, see the [Dahua setup guide](DAHUA.md).
The contact editor exposes the SIP profile and audio compatibility choices;
you do not need to change Python code or mark a static contact as registered.

## Configure a trunk

A local-only installation needs no trunk. To connect a provider, FRITZ!Box or
another PBX:

1. Open **Settings > Devices & services > VoIP Stack**.
2. Open the integration's three-dot menu and choose **Add SIP trunk**. The
   integration page also has an **Add SIP trunk** shortcut.
3. Name the line and choose whether the peer requires REGISTER.
4. Enter its server, transport and credentials, or the allowed incoming networks
   for a static trunk. Set an outbound proxy only when required by the peer.
5. Choose the allowed codecs and the receiving phone under **Fallback destination**.
6. Enable digit collection only if callers should choose an internal extension.

Each trunk is a separate HA entry with its own device and readiness entity.
Edit it through the configuration control beside that named trunk, not through
**Reconfigure > Trunk** on the main integration. The first line becomes the
outgoing default; additional lines can be selected per phone or per call.
Incoming destinations are configured independently of that outgoing choice.

See the illustrated [PBX walkthrough](PBX_WORKFLOWS.md), detailed
[trunk guide](SIP_TRUNK.md) and
[migration instructions](BREAKING_CHANGES.md#pbx-development-branch-separate-sip-trunk-entries).

Public numbers and service strings such as `*` or `**621` are sent to the trunk
without destructive normalization. Names and internal extensions are resolved
through the phonebook first.

Home Assistant trunk and standard SIP legs support UDP, TCP and verified TLS.
ESP endpoints intentionally remain lightweight local SIP/RTP phones and do not
terminate SIP TLS or SRTP.

## Forward or transfer a call

These actions also provide a visual **Phone** picker and a **Destination** field.
For Transfer call, the optional Phone picker is under **Advanced options**.
Select the local phone by name; Home Assistant saves its `device_id` for you.
Native VoIP call triggers supply the current call automatically. The explicit
`call_id` shown below is an advanced option for selecting a particular call,
not a value you must look up for every automation. See the
[automation cookbook](AUTOMATION_DIALPLAN.md) for examples using the current call.

<details>
<summary>Advanced YAML with an explicit phone and call</summary>

Forward a call before it is established:

```yaml
action: voip_stack.forward
data:
  device_id: 0123456789abcdef0123456789abcdef
  call_id: current-call-id
  destination: Reception
```

Transfer an established call:

```yaml
action: voip_stack.transfer
data:
  device_id: 0123456789abcdef0123456789abcdef
  call_id: current-call-id
  destination: sip:desk@pbx.example
```

</details>

Secure SIP identities such as `sips:desk@pbx.example:5061;transport=tls` are
preserved across direct routing, outbound proxies and REFER targets.

Between SIP endpoints, use the telephone's normal attended-transfer controls: hold
the caller, dial the other extension, speak to that person, then complete the
transfer. The telephone must support REFER with Replaces. VoIP Stack keeps the
destination's existing SIP dialog and RTP address, so that person does not receive
a second ringing call. If the transfer is rejected, the existing calls remain
available to the telephone's normal resume or hangup controls.

A SIP phone can originate a consultation while already in an established call.
It still appears busy to unrelated incoming calls. Browser and ESP phone controls
retain their existing single-call behavior.

For two SIP audio calls already routed through HA, a remaining phone that rejects
REFER can still be connected locally. HA retains its existing call and joins the
two media legs. This also covers static SIP endpoints without REGISTER. Video
calls continue to require the peers' REFER/Replaces support; the local fallback
does not silently remove negotiated video.

## Voicemail

Voicemail is available on the `feature/ha-pbx` development branch. Enable it in
VoIP Stack's settings and select a Home Assistant TTS voice and language for the
spoken telephone menus. Then enable the mailbox for each phone that needs one.

The mailbox belongs to that phone. Its card shows a **Voicemail** button when
messages exist. Open it to see callers and recording dates, listen to a message
or delete it. The softphone places an ordinary call to the mailbox and sends
keypad commands; the message list stays open during that call.

On a registered SIP handset, configure the voicemail key to dial `*97`. This
opens its own mailbox without a PIN. To reach another mailbox, dial `*98` and
enter its extension and PIN. VoIP Stack uses SIP MWI to update a compatible
handset's new-message indicator.

Enabling a mailbox alone leaves normal ringing unchanged. In the phone card's
**Options > Call routing**, enable a ringing timeout and choose voicemail if
unanswered calls should leave a message. Automations can also speak an
announcement and then send that particular call to a selected mailbox.

Recordings are stored once in Home Assistant Media. A **Voicemail received**
automation can use that file for a notification or optionally transcribe it
through an installed HA STT provider. See the [voicemail guide](VOICEMAIL.md)
for complete setup, keypad commands and automation examples.

## Busy lamps on SIP phones

A registered SIP phone can monitor another phone with a normal BLF key. Choose
BLF in the handset's key settings and use the monitored phone's username or
extension as the destination, through its VoIP Stack account.

VoIP Stack supports `Event: dialog` subscriptions and sends versioned
`application/dialog-info+xml` notifications. They reflect HA-owned calls:
ringing, established conversations and return to idle. A phone with two active
dialogs stays busy until both end. The state comes from the same call registry
used by the backend and card; no periodic status polling is added.

This is separate from the voicemail MWI indicator, which uses `message-summary`,
and online presence, which uses `presence`. Removing the monitored phone ends
its BLF subscription with `noresource`. Subscriptions can request up to one hour
and must refresh before expiry.

## Parking and picking up calls

Enable parking in the main integration configuration. Transfer an established
call to `700`, then dial its assigned slot (normally `701` through `720`) from
another phone. The dashboard's **Park call** button uses the same backend.

To answer an individual phone that is still ringing, dial `pickup.<phone>` or
`*8` followed by its extension. For example, `*8101` answers the ringing phone
with extension `101`. These are local PBX dial-plan conventions, not SIP
protocol requirements. See [parking and pickup](CALL_PARKING.md) for limits and
return behavior.

## Recording a conversation

Use **VoIP Stack: Start recording** with the participating phone selected, then
**Stop recording**, or enable **Show recording controls** in that phone card's editor.
A hangup also finishes the recording. The conversation continues if recording
fails or reaches its configured limit. Recordings are separate from voicemail
and do not turn on message-waiting indicators.

The [recording guide](CALL_RECORDING.md) describes file access, automation events
and supported call paths. Recording is optional and off until requested.

## Audio and video calls

Browser phones negotiate supported audio codecs and can send or receive video.
ESP32-P4 profiles provide either JPEG or H.264 SIP video according to the YAML
selected at compile time.

An audio call can add compatible video through re-INVITE. The previous media
contract remains active until both dialogs accept the change. Closing the
camera or browser track does not independently rewrite the PBX call state.

For browser calls:

- microphone permission is required before Answer or Call can start media;
- camera permission is required only to transmit video;
- receiving a remote camera does not require local camera permission;
- reset the Companion frontend cache after upgrading the bundled card.

## Health, repairs and diagnostics

Use **Settings > System > Repairs** for actionable problems such as missing
ESPHome call-control actions, incompatible firmware contracts or enabled media
capture.

Use **Settings > System > Repairs > System information** or the integration's
System Health entry to inspect aggregate listener, trunk, endpoint, active-call
and RTP-resource status. Diagnostics redact credentials, private identities and
complete Call-IDs.

If a call fails:

1. check that both phone Devices are available;
2. check DND and Auto Answer;
3. verify the destination in the phonebook;
4. verify SIP and RTP firewall rules;
5. read [`troubleshooting.md`](troubleshooting.md);
6. collect diagnostics only after reproducing the failure.

## Updating an existing installation

1. Read the target release notes and apply migration instructions relevant to
   your installed version.
2. Download the update through HACS and restart Home Assistant.
3. Reload the dashboard when the card changes. Reset the browser or Companion
   frontend cache if it still displays the previous card.
4. Rebuild ESPHome firmware when adopting firmware-side changes, using the
   component versions and build instructions specified for that release.

Use **Reconfigure**, change card bindings or edit existing automations only when
the release notes call for it or you want to change your setup. Current examples
use the visual phone picker and supported action fields; see [Services](SERVICES.md)
when adapting older YAML.

## Use ESP firmware with a classic PBX

ESP phones can use a static SIP peer configuration with a traditional PBX,
without REGISTER. See [Use a VoIP Stack firmware with a classic PBX](CLASSIC_PBX.md)
for Asterisk and FreeSWITCH static extensions, conventional SIP phones, ESP
contacts, audio formats and call tests. This is a
firmware-to-PBX path; HA can continue providing the device's Assist features.

## Detailed references

- [Automation cookbook](AUTOMATION_DIALPLAN.md)
- [What is new in 2026.9.0](WHATS_NEW_2026_9_0.md)
- [Deployment guide](DEPLOYMENT_GUIDE.md)
- [Home Assistant actions](SERVICES.md)
- [Dial plan](DIALPLAN_RESOLVER.md)
- [Groups](GROUPS.md)
- [SIP trunk](SIP_TRUNK.md)
- [SIP video](SIP_VIDEO.md)
- [Automation cookbook](AUTOMATION_DIALPLAN.md)
- [Troubleshooting](troubleshooting.md)
- [Architecture](ARCHITECTURE.md)
