# Links a copy of the framework's esp32-camera library whose prints go nowhere.
#
# The driver prints "cam_hal: EV-VSYNC-OVF" from its interrupt, and other lines from cam_task
# (2 KB stack), whenever WiFi keeps core 0 busy. Every panic logged on 2026-10-16 (heap and
# FreeRTOS list corruption, broken semaphores) came right after a burst of those lines.
# esp_log_level_set can't silence them: they bypass ESP_LOG. The calls are renamed in the copy to
# cam_quiet_<name> (src/quiet_camera.c); nothing else in the firmware changes.
import os
import subprocess

Import("env")  # noqa: F821

fw = env.PioPlatform().get_package_dir("framework-arduinoespressif32")  # noqa: F821
src = os.path.join(fw, "tools", "sdk", "esp32s3", "lib", "libesp32-camera.a")
out_dir = os.path.join(env.subst("$BUILD_DIR"), "quiet_camera")  # noqa: F821
dst = os.path.join(out_dir, "libesp32-camera.a")
if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
    os.makedirs(out_dir, exist_ok=True)
    tc = env.PioPlatform().get_package_dir("toolchain-xtensa-esp32s3")  # noqa: F821
    objcopy = os.path.join(tc, "bin", "xtensa-esp32s3-elf-objcopy")
    syms = []
    for s in ("ets_printf", "esp_rom_printf", "printf"):  # objcopy wants one new name each
        syms += ["--redefine-sym", f"{s}=cam_quiet_{s}"]
    subprocess.check_call([objcopy, *syms, src, dst])
env.Prepend(LIBPATH=[out_dir])  # noqa: F821
