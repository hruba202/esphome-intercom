# Upstream alignment

Source baseline: ESPHome `dev` commit `2d0e3faa3d1282d1ec6928d4e76b62db3d37434c`.

Upstream lazy block erase, partition bounds, update exclusion and backend
contracts replace the older copied implementation. The old whole-partition
erase budget is no longer needed. The retained local adaptation bounds the
blocking image-validation phases and feeds the watchdog between actual phases,
restoring its ordinary budget on every exit. Native update-lock ownership and
signature/downgrade checks remain intact.

On older ESPHome, ESP32 and host builds explicitly select the atomic update
lock path because the newer core's thread-model defines are absent. Log tag
spelling remains compatible with ESPHome 2026.9.1.

Local validation covers the ESP-IDF firmware builds and host contract tests.
It does not claim new physical OTA, signed-update or non-ESP32 qualification.
