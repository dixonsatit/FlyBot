// See quiet_camera.py: the camera driver's prints, renamed to these, do nothing.
// IRAM: the driver calls them from its interrupt, which may run while the flash cache is off.
#include "esp_attr.h"

IRAM_ATTR int cam_quiet_ets_printf(const char* fmt, ...) { (void)fmt; return 0; }
IRAM_ATTR int cam_quiet_esp_rom_printf(const char* fmt, ...) { (void)fmt; return 0; }
IRAM_ATTR int cam_quiet_printf(const char* fmt, ...) { (void)fmt; return 0; }
