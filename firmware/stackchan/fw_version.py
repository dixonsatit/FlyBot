# Writes src/fw_version.h with the commit the firmware is built from, so the self-test says which
# firmware runs (after an update over WiFi there is no other way to tell). Rewritten only when it
# changes, so an unchanged tree doesn't recompile.
import os
import subprocess

Import("env")  # noqa: F821

src = os.path.join(env.subst("$PROJECT_DIR"), "src")  # noqa: F821
try:
    v = subprocess.check_output(["git", "describe", "--always", "--dirty", "--exclude", "*"], cwd=src, text=True).strip()
except (OSError, subprocess.CalledProcessError):
    v = "unknown"
path = os.path.join(src, "fw_version.h")
text = f'#pragma once\n#define FW_VERSION "{v}"\n'
if not os.path.exists(path) or open(path).read() != text:
    with open(path, "w") as f:
        f.write(text)
