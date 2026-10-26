/** Resolve integration entities once per connection or registry change. */
export class VoipEntityIds {
  constructor() {
    this.phonebook = null;
    this._connection = null;
    this._registry = null;
    this._pending = null;
    this._wasPresent = false;
  }

  update(hass, changed) {
    this._latestHass = hass;
    const connection = hass?.connection;
    if (!connection?.sendMessagePromise) return;
    const present = !!hass.states?.[this.phonebook];
    const refresh = connection !== this._connection || hass.entities !== this._registry ||
      (this._wasPresent && !present);
    this._wasPresent = present;
    if (!refresh || this._pending) return;
    this._connection = connection;
    this._registry = hass.entities;
    const registry = hass.entities;
    this._pending = connection.sendMessagePromise({ type: "voip_stack/list_devices" })
      .then((result) => {
        if (this._latestHass?.connection !== connection) return;
        // Older integration versions do not advertise stable entity identities.
        this.phonebook = result?.entity_ids
          ? result.entity_ids.phonebook || null : "sensor.voip_phonebook";
        changed();
      })
      .catch(() => { this._connection = null; })
      .finally(() => {
        this._pending = null;
        if (this._latestHass?.connection !== connection || this._latestHass?.entities !== registry) {
          this.update(this._latestHass, changed);
        }
      });
  }
}
