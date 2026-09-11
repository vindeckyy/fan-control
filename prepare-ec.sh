#!/bin/bash
set -euo pipefail

# Ensure tuxedo modules are loaded
modprobe -q tuxedo_keyboard 2>/dev/null || true
modprobe -q tuxedo_io 2>/dev/null || true
modprobe -q uniwill_wmi 2>/dev/null || true

# Rebind WMI interface to uniwill_wmi if held by uniwill-wmi
DEV="ABBC0F72-8EA1-11D1-00A0-C90629100000-8"
if [ -e "/sys/bus/wmi/drivers/uniwill-wmi/$DEV" ]; then
    echo "$DEV" > "/sys/bus/wmi/drivers/uniwill-wmi/unbind" 2>/dev/null || true
fi
if [ -d "/sys/bus/wmi/drivers/uniwill_wmi" ] && [ ! -e "/sys/bus/wmi/drivers/uniwill_wmi/$DEV" ]; then
    echo "$DEV" > "/sys/bus/wmi/drivers/uniwill_wmi/bind" 2>/dev/null || true
fi
