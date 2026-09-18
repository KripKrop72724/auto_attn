#pragma once
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
#define ZONE_LITE_FIRMWARE_FAMILY "hikvision"
#define ZONE_LITE_PROJECT_NAME "zone_lite_hikvision"
#else
#define ZONE_LITE_FIRMWARE_FAMILY "zkt"
#define ZONE_LITE_PROJECT_NAME "zone_lite"
#endif
