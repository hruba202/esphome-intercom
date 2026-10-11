# Call parking

This guide describes the implementation on `feature/ha-pbx`. It is not part of
the published release yet. Browser-to-browser parking and dashboard controls
are still being completed.

Parking leaves the other party connected to Home Assistant while you put down
your phone. Another phone retrieves that conversation by calling its parking
slot. The caller does not have to redial.

## Enable and configure

Open the integration's main configuration and enable call parking. By default,
parking is disabled and does not reserve any numbers in the dial plan.

The initial settings are:

| Setting | Default | Purpose |
| --- | --- | --- |
| Access number | `700` | Transfer a call here to use the first free slot. |
| Slots | `701` through `720` | Call the occupied slot to retrieve its caller. |
| Parking timeout | 180 seconds | How long the caller can remain parked. |
| Return ringing timeout | 30 seconds | How long the return destination may ring. |
| Return destination | The phone that parked the call | Optional alternate contact. |
| Hold audio | None | Optional media selected through Home Assistant. |

Configured slot numbers must not conflict with existing phones, contacts or
groups. If hold audio cannot be read, the caller remains connected in silence;
the ordinary return timeout still applies.

## From a SIP phone

While speaking to someone, transfer the call to `700`. The PBX retains the
other party and releases your phone. Dial the occupied slot, for example `701`,
from another phone to retrieve the conversation. A transfer to a particular
configured slot parks there; an ordinary call to that same slot retrieves it.
The explicit aliases `park.701` and `pickup.701` distinguish these operations.

Only one phone can retrieve a slot. A failed pickup leaves it available for
another attempt. If the caller hangs up, the slot becomes free immediately.
Registered SIP phones can subscribe to the slot using the SIP dialog event
package (RFC 4235) for a busy-lamp indication.

## From a Home Assistant action

Select **VoIP Stack: Park call** and choose the phone currently participating
in the conversation. Leave the slot empty to select the first free slot.
The optional response contains `slot`, `call_id` and `generation`.

```yaml
action: voip_stack.park
data:
  device_id: YOUR_PHONE_DEVICE_ID
response_variable: parked
```

The UI supplies the device selector. The example identifier represents that
selection, not a phone name or an extension. A native VoIP call trigger can
supply the current call and phone. An explicit `call_id` must belong to the
selected phone; `expected_generation` can reject a stale command.

To retrieve a slot, use the existing call action or dial it from the phone:

```yaml
action: voip_stack.call
data:
  device_id: YOUR_OTHER_PHONE_DEVICE_ID
  destination: "701"
```

## Current verification boundary

Real baresip calls cover parking from either SIP participant, pickup, timeout
return, repeated parking, hold/resume and hangup from either side. Audio tests
inspect decoded WAV samples and released RTP reservations. Browser transport
fixtures also cover pickup, parking again and subsequent SIP pickup.

These tests do not qualify every physical handset's transfer button or BLF
configuration. Active video calls are rejected before parking rather than
silently losing their video session. Calls made directly between devices,
without passing through Home Assistant, are outside this parking service.
