#!/bin/bash
# Copies Yoklama to root-owned system paths. Enables nothing and starts nothing.
# The service runs as root, so it must never run code from a user-writable place.
# Run it again after changing the code, then: systemctl restart yoklama
#
# DESTDIR=<dir> installs under <dir> without root, for checking the result.
set -euo pipefail

src="$(dirname "$(realpath "$0")")"
dest="${DESTDIR:-}"

if [ -n "$dest" ]; then
    owner=()
elif [ "$(id -u)" -eq 0 ]; then
    owner=(-o root -g root)
else
    echo "install.sh: run as root" >&2
    exit 1
fi

lib="$dest/usr/local/lib/yoklama"

install -d -m 755 "${owner[@]}" "$lib" "$lib/bin"
# Drop modules from an earlier version. Nothing else lives in this directory.
rm -rf -- "$lib/yoklama"
install -d -m 755 "${owner[@]}" "$lib/yoklama"

install -m 755 "${owner[@]}" "$src/bin/yoklama" "$lib/bin/yoklama"
install -m 644 "${owner[@]}" "$src"/yoklama/*.py "$lib/yoklama/"
install -m 644 "${owner[@]}" "$src/ignore_procs.txt" "$src/ignore_paths.txt" \
    "$src/protected.txt" "$lib/"

install -d -m 755 "${owner[@]}" "$dest/usr/local/bin"
ln -sfn /usr/local/lib/yoklama/bin/yoklama "$dest/usr/local/bin/yoklama"

install -D -m 644 "${owner[@]}" "$src/systemd/yoklama.service" \
    "$dest/etc/systemd/system/yoklama.service"
install -D -m 644 "${owner[@]}" "$src/pacman/yoklama.hook" \
    "$dest/etc/pacman.d/hooks/yoklama.hook"

if [ -z "$dest" ]; then
    systemctl daemon-reload
fi

echo "Installed to $lib"
