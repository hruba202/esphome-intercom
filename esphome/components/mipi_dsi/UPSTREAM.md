# ESPHome MIPI DSI fork upstream record

Upstream baseline: ESPHome `dev` commit
`5f6a910e2d6e41d3716668a66e5dff8cca25f2ea`, component path
`esphome/components/mipi_dsi`.

The local fork preserves the validated P4 panel models and initialization,
exposes the immutable framebuffer to the narrow video adapter, and serializes
display submissions so LVGL and direct video cannot consume each other's DMA
completion event. Optional video diagnostics remain compile-time gated.

Checked with:

```bash
git diff --no-index ../esphome-pr-work/esphome/components/mipi_dsi esphome/components/mipi_dsi
```

Re-run the comparison after every ESPHome update and keep unrelated upstream
model definitions intact.

## Outstanding retirement blockers

Checked on October 17, 2026.

The Waveshare 10.1-inch JD9365 initialization fix is still local. This driver's
extra waits are 120 ms after `SLEEP_OUT` and 20 ms after `DISPLAY_ON`, preserving
the previously validated fix for three corrupted color bands. The driver in
both ESPHome 2026.9.1 and dev `1a28a9220ba1f77f8d9576562adb0baacba3ed0c`
still waits only 10 ms after `SLEEP_OUT`. Init-sequence delay records are
additional to these driver waits and must be included in any timing comparison.

The transfer mutex, completion-token ownership and read-only framebuffer access
are separate differences. The new upstream external-drawing APIs do not by
themselves prove equivalent DMA ownership. Current upstream dev also changes
the bulk-draw signature, so the renderer must be checked with the selected
display/core version before dropping this provider. No new P4 hardware
qualification is implied by this source comparison.
