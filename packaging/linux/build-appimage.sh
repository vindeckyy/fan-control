#!/usr/bin/env bash
# Build a self-contained Fan Control AppImage for Linux x86_64.
#
# Output: dist/fan-control-<version>-<arch>.AppImage
#
# The image bundles the GTK4 + WebKitGTK 6 dashboard stack: CPython, PyGObject,
# the GI typelibs, GTK 4, WebKitGTK 6.0 and its helper processes, and the
# built React UI. Build it on the oldest distro you want to support (CI uses
# Ubuntu 24.04, the first with webkitgtk-6.0); the glibc floor is the build
# host's.
#
# WebKitGTK 6.0 hardcodes the path to its helper processes and has no
# environment override, so libwebkitgtk is binary-patched to a short /tmp path
# and AppRun links that to the bundled helpers.
#
# The build script and the WebKit helper-path patch follow the approach used by
# the Lantern project (github.com/ursa-nz/Lantern).
#
# Usage: packaging/linux/build-appimage.sh [version]

set -euo pipefail

APP_NAME="Fan Control"
APP_ID="org.community.FanControl"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
cd "${ROOT}"

log() { printf '==> %s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

ARCH="$(uname -m)"
case "${ARCH}" in
    x86_64) MULTIARCH="x86_64-linux-gnu" ;;
    aarch64) MULTIARCH="aarch64-linux-gnu" ;;
    *) err "unsupported arch: ${ARCH}" ;;
esac
if have gcc; then MULTIARCH="$(gcc -dumpmachine 2>/dev/null || echo "${MULTIARCH}")"; fi
LIBDIR="/usr/lib/${MULTIARCH}"

VERSION="${1:-}"
if [ -z "${VERSION}" ]; then
    VERSION="$(grep -oP '^APP_VERSION = "\K[^"]+' fan_policy.py | head -1)"
fi
[ -n "${VERSION}" ] || err "could not determine version (pass it as an argument)"
log "Building ${APP_NAME} ${VERSION} AppImage for ${ARCH}"

# ---------- Build host sanity ----------
[ -f "${LIBDIR}/libwebkitgtk-6.0.so.4" ] || err \
    "libwebkitgtk-6.0 not found. Install: gir1.2-gtk-4.0 gir1.2-webkit-6.0 python3-gi libgtk-4-dev libglib2.0-bin librsvg2-common desktop-file-utils pkg-config"
have pkg-config || err "pkg-config missing"
pkg-config --exists gtk4 || err "gtk4.pc not found (install libgtk-4-dev)"
[ -f "${ROOT}/ui/dist/index.html" ] || err "ui/dist is missing; build the UI first (npm ci && npm run build)"
for t in python3 gtk-update-icon-cache glib-compile-schemas; do
    have "$t" || err "missing build tool: $t"
done

# ---------- Tools ----------
TOOLS="${HOME}/.cache/fan-control-appimage/tools"
mkdir -p "${TOOLS}"
export APPIMAGE_EXTRACT_AND_RUN=1

fetch() {
    local url="$1" dest="$2"
    [ -f "${dest}" ] && return 0
    log "Fetching $(basename "${dest}")"
    if have wget; then wget -qO "${dest}" "${url}"; else curl -fsSL -o "${dest}" "${url}"; fi
    chmod +x "${dest}"
}
fetch "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-${ARCH}.AppImage" \
      "${TOOLS}/linuxdeploy-${ARCH}.AppImage"
fetch "https://raw.githubusercontent.com/linuxdeploy/linuxdeploy-plugin-gtk/master/linuxdeploy-plugin-gtk.sh" \
      "${TOOLS}/linuxdeploy-plugin-gtk.sh"
fetch "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage" \
      "${TOOLS}/appimagetool-${ARCH}.AppImage"
LINUXDEPLOY="${TOOLS}/linuxdeploy-${ARCH}.AppImage"
APPIMAGETOOL="${TOOLS}/appimagetool-${ARCH}.AppImage"
export PATH="${TOOLS}:${PATH}"

# ---------- AppDir: application tree ----------
APPDIR="${ROOT}/build/AppDir"
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/share/fan-control" "${APPDIR}/usr/bin"

log "Staging application files"
install -m 644 "${ROOT}"/fan_*.py "${APPDIR}/usr/share/fan-control/"
install -m 755 "${ROOT}/fan-gui.py" "${ROOT}/fan-daemon.py" "${ROOT}/fan-ctl.py" "${APPDIR}/usr/share/fan-control/"
mkdir -p "${APPDIR}/usr/share/fan-control/ui"
cp -a "${ROOT}/ui/dist" "${APPDIR}/usr/share/fan-control/ui/dist"

# ---------- Bundle CPython, stdlib, and PyGObject ----------
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PYBIN="$(readlink -f "$(command -v python3)")"
log "Bundling Python ${PYVER} (${PYBIN})"
install -Dm755 "${PYBIN}" "${APPDIR}/usr/bin/python${PYVER}"
ln -sf "python${PYVER}" "${APPDIR}/usr/bin/python3"

mkdir -p "${APPDIR}/usr/lib"
cp -a "/usr/lib/python${PYVER}" "${APPDIR}/usr/lib/python${PYVER}"
( cd "${APPDIR}/usr/lib/python${PYVER}" && rm -rf \
    test tests idlelib tkinter turtledemo ensurepip lib2to3 \
    config-*/libpython*.a 2>/dev/null || true )
find "${APPDIR}/usr/lib/python${PYVER}" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

python3 - <<'PY' > "${TOOLS}/gi-path.txt"
import gi, os
print(os.path.dirname(gi.__file__))
PY
GI_SRC="$(cat "${TOOLS}/gi-path.txt")"
[ -d "${GI_SRC}" ] || err "could not locate the gi package"
GI_DEST="${APPDIR}/usr/lib/python3/dist-packages/gi"
mkdir -p "$(dirname "${GI_DEST}")"
cp -a "${GI_SRC}" "${GI_DEST}"
find "${GI_DEST}" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

log "Bundling GI typelibs"
mkdir -p "${APPDIR}/usr/lib/girepository-1.0"
cp -a "${LIBDIR}/girepository-1.0/." "${APPDIR}/usr/lib/girepository-1.0/"

log "Bundling WebKitGTK helper processes"
cp -a "${LIBDIR}/webkitgtk-6.0" "${APPDIR}/usr/lib/webkitgtk-6.0"

if [ -d "${LIBDIR}/gio/modules" ]; then
    log "Bundling GIO modules"
    mkdir -p "${APPDIR}/usr/lib/gio/modules"
    cp -a "${LIBDIR}/gio/modules/." "${APPDIR}/usr/lib/gio/modules/" 2>/dev/null || true
    rm -f "${APPDIR}/usr/lib/gio/modules/giomodule.cache"
fi

# ---------- Desktop entry + icon ----------
STAGED_DESKTOP="${APPDIR}/usr/share/applications/fan-control.desktop"
mkdir -p "$(dirname "${STAGED_DESKTOP}")"
cp "${ROOT}/packaging/fan-control.desktop" "${STAGED_DESKTOP}"
cp "${STAGED_DESKTOP}" "${APPDIR}/fan-control.desktop"
# linuxdeploy requires Exec to name a deployed ELF; restore fan-gui afterward.
sed -i "s|^Exec=.*|Exec=python${PYVER} %F|" "${STAGED_DESKTOP}"
ICON_SRC="${ROOT}/packaging/icons/fan-control.svg"

# ---------- Dependency gathering (linuxdeploy + GTK plugin) ----------
LD_LIBS=(
    "${LIBDIR}/libgtk-4.so.1"
    "${LIBDIR}/libwebkitgtk-6.0.so.4"
    "${LIBDIR}/libjavascriptcoregtk-6.0.so.1"
    "${LIBDIR}/libgirepository-1.0.so.1"
)
ld_args=()
for lib in "${LD_LIBS[@]}"; do [ -f "$lib" ] && ld_args+=(--library "$lib"); done

log "Running linuxdeploy (+gtk plugin)"
DEPLOY_GTK_VERSION=4 NO_STRIP=1 "${LINUXDEPLOY}" \
    --appdir "${APPDIR}" \
    --executable "${APPDIR}/usr/bin/python${PYVER}" \
    --desktop-file "${STAGED_DESKTOP}" \
    --icon-file "${ICON_SRC}" \
    "${ld_args[@]}" \
    --plugin gtk

sed -i "s|^Exec=.*|Exec=fan-gui %F|" "${STAGED_DESKTOP}"

# ---------- Patch WebKitGTK's hardcoded helper path ----------
patch_webkit() {
    local lib="$1"
    python3 - "$lib" <<'PY'
import re, sys
path = sys.argv[1]
data = bytearray(open(path, "rb").read())
patched = 0
for match in re.finditer(rb"/usr/lib/[a-z0-9_]+-linux-gnu/webkitgtk-6\.0\x00", data):
    start, end = match.start(), match.end() - 1
    replacement = b"/tmp/fan-control-wk"
    data[start:end] = replacement + b"\x00" * ((end - start) - len(replacement))
    patched += 1
open(path, "wb").write(data)
print(f"  patched {patched} occurrence(s) in {path}")
sys.exit(0 if patched else 3)
PY
}
WEBKIT_LIB="$(find "${APPDIR}/usr/lib" -maxdepth 1 -name 'libwebkitgtk-6.0.so.4*' -type f | head -1)"
[ -n "${WEBKIT_LIB}" ] || err "bundled libwebkitgtk not found after linuxdeploy"
log "Patching WebKitGTK helper path in $(basename "${WEBKIT_LIB}")"
patch_webkit "${WEBKIT_LIB}" || err "failed to patch WebKitGTK helper path"

# ---------- AppRun ----------
log "Writing AppRun"
cat > "${APPDIR}/AppRun" <<APPRUN
#!/bin/bash
HERE="\$(dirname "\$(readlink -f "\${0}")")"
export APPDIR="\${HERE}"

for hook in "\${APPDIR}"/apprun-hooks/*.sh; do
    [ -e "\${hook}" ] && . "\${hook}"
done

export PATH="\${APPDIR}/usr/bin:\${PATH}"
export LD_LIBRARY_PATH="\${APPDIR}/usr/lib:\${APPDIR}/usr/lib/${MULTIARCH}\${LD_LIBRARY_PATH:+:\${LD_LIBRARY_PATH}}"
export PYTHONHOME="\${APPDIR}/usr"
export PYTHONPATH="\${APPDIR}/usr/share/fan-control:\${APPDIR}/usr/lib/python3/dist-packages\${PYTHONPATH:+:\${PYTHONPATH}}"
export PYTHONDONTWRITEBYTECODE=1
export GI_TYPELIB_PATH="\${APPDIR}/usr/lib/girepository-1.0\${GI_TYPELIB_PATH:+:\${GI_TYPELIB_PATH}}"
export GIO_MODULE_DIR="\${APPDIR}/usr/lib/gio/modules"
export GSETTINGS_SCHEMA_DIR="\${APPDIR}/usr/share/glib-2.0/schemas\${GSETTINGS_SCHEMA_DIR:+:\${GSETTINGS_SCHEMA_DIR}}"
export XDG_DATA_DIRS="\${APPDIR}/usr/share:\${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"

ln -sfn "\${APPDIR}/usr/lib/webkitgtk-6.0" /tmp/fan-control-wk 2>/dev/null || true
export WEBKIT_INJECTED_BUNDLE_PATH="\${APPDIR}/usr/lib/webkitgtk-6.0/injected-bundle"
export WEBKIT_DISABLE_DMABUF_RENDERER=1

APP="\${APPDIR}/usr/share/fan-control"
case "\${1:-}" in
    daemon|fan-daemon)
        shift
        exec "\${APPDIR}/usr/bin/python${PYVER}" "\${APP}/fan-daemon.py" "\$@"
        ;;
    ctl|fan-ctl)
        shift
        exec "\${APPDIR}/usr/bin/python${PYVER}" "\${APP}/fan-ctl.py" "\$@"
        ;;
    *)
        exec "\${APPDIR}/usr/bin/python${PYVER}" "\${APP}/fan-gui.py" "\$@"
        ;;
esac
APPRUN
chmod 755 "${APPDIR}/AppRun"

# ---------- Pack ----------
mkdir -p "${ROOT}/dist"
OUT="${ROOT}/dist/fan-control-${VERSION}-${ARCH}.AppImage"
rm -f "${OUT}"
log "Packing ${OUT}"
ARCH="${ARCH}" "${APPIMAGETOOL}" --no-appstream "${APPDIR}" "${OUT}"

log "Done: ${OUT}"
