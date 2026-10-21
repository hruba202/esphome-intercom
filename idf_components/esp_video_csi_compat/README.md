# Pre-v3 P4 CSI format contract

ESP-IDF 5.5.6 backported the CSI format-conversion contract from IDF 6.
For pre-v3 P4 silicon, esp_video 2.4.1 still selects the sensor's RAW format
as CSI input until IDF 6, although ISP already outputs the requested RGB/YUV.
The new CSI driver rejects that apparent conversion on this silicon, so
`VIDIOC_STREAMON` fails before any camera frame is delivered.

This video-only build adaptation corrects that one version boundary in a
build-directory copy of the upstream translation unit. It preserves the
downloaded library and external camera component. Exactly one implementation
is compiled. No task, capture pipeline, buffer or runtime wrapper is added.
Audio-only profiles, production P4 silicon, older IDF and IDF 6 are untouched.
A changed upstream boundary stops the build and requires review.

The related upstream discussions are
[ESP Video #98](https://github.com/espressif/esp-video-components/issues/98) and
[ESP-IDF #18405](https://github.com/espressif/esp-idf/issues/18405).
Remove this adaptation once the selected esp_video release handles the
5.5.6 contract on pre-v3 P4. Do not enable hardware format conversion on
unsupported silicon or falsify the IDF version to bypass this check.
