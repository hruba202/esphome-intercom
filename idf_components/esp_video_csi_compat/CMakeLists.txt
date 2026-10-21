# ESP-IDF 5.5.6 backported the CSI format contract used by IDF 6. On pre-v3
# P4, esp_video 2.4.1 still gives CSI the sensor format instead of ISP output.
# Keep the one upstream pipeline and correct only that version boundary.
idf_component_register(REQUIRES espressif__esp_video)

if(CMAKE_BUILD_EARLY_EXPANSION OR NOT CONFIG_IDF_TARGET_ESP32P4 OR
   NOT CONFIG_ESP_VIDEO_ENABLE_MIPI_CSI_VIDEO_DEVICE OR
   CONFIG_ESP32P4_REV_MIN_FULL GREATER_EQUAL 300)
    return()
endif()
if(IDF_VERSION_MAJOR GREATER_EQUAL 6 OR IDF_VERSION_MAJOR LESS 5 OR
   (IDF_VERSION_MAJOR EQUAL 5 AND IDF_VERSION_MINOR LESS 5) OR
   (IDF_VERSION_MAJOR EQUAL 5 AND IDF_VERSION_MINOR EQUAL 5 AND IDF_VERSION_PATCH LESS 6))
    return()
endif()

idf_component_get_property(video_dir espressif__esp_video COMPONENT_DIR)
idf_component_get_property(video_lib espressif__esp_video COMPONENT_LIB)
file(READ "${video_dir}/src/device/esp_video_csi_format.c" format_source)
set(old_boundary "#if ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(6, 0, 0)\n        /* Old IDF version: use sensor format as CSI input */")
set(new_boundary "#if ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(5, 5, 6)\n        /* IDF before the CSI format-conversion backport uses sensor input. */")
string(FIND "${format_source}" "${old_boundary}" location)
if(location EQUAL -1)
    message(FATAL_ERROR "esp_video CSI contract changed; review the pre-v3 P4 compatibility adapter")
endif()
string(REPLACE "${old_boundary}" "${new_boundary}" format_source "${format_source}")
set(patched "${CMAKE_CURRENT_BINARY_DIR}/esp_video_csi_format.c")
file(WRITE "${patched}" "${format_source}")
get_target_property(sources ${video_lib} SOURCES)
list(FILTER sources EXCLUDE REGEX "(^|/)esp_video_csi_format\\.c$")
list(APPEND sources "${patched}")
set_property(TARGET ${video_lib} PROPERTY SOURCES "${sources}")
