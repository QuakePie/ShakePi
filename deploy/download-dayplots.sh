#!/bin/sh
# Download all ShakePie daily plots from the recorder to this computer.
#
# Run this script from any Linux/macOS shell. You will be asked for the Pi account
# password once. Images are copied into ./dayplots and are named
# YYYYMMDD-STATION-CHn.png (CH1/CH2/CH3 = N/E/Z), ready for upload to a website.
#
# Set SHAKEPIE_REMOTE_USER and, if needed, SHAKEPIE_REMOTE_HOST before running.

set -eu

REMOTE_USER="${SHAKEPIE_REMOTE_USER:-}"
REMOTE_HOST="${SHAKEPIE_REMOTE_HOST:-raspberrypi.local}"
REMOTE_DIR="${SHAKEPIE_REMOTE_DIR:-/mnt/shakepie-data/dayplots}"
DEST="${SHAKEPIE_DAYPLOT_DEST:-$(cd "$(dirname "$0")" && pwd)/dayplots}"

if [ -z "$REMOTE_USER" ]; then
    echo "Set SHAKEPIE_REMOTE_USER to your Pi SSH username." >&2
    exit 1
fi
case "$REMOTE_USER" in
    *[!A-Za-z0-9._-]*) echo "Invalid SSH username." >&2; exit 1 ;;
esac
case "$REMOTE_HOST" in
    ""|*[!A-Za-z0-9._:-]*) echo "Invalid Pi hostname or IP address." >&2; exit 1 ;;
esac
case "$REMOTE_DIR" in
    /*) ;;
    *) echo "SHAKEPIE_REMOTE_DIR must be an absolute path." >&2; exit 1 ;;
esac
case "$REMOTE_DIR" in
    *[!A-Za-z0-9._/-]*) echo "Invalid remote directory." >&2; exit 1 ;;
esac

if ! command -v scp >/dev/null 2>&1; then
    echo "scp was not found. Install the OpenSSH client, then run this script again." >&2
    exit 1
fi

mkdir -p "$DEST"

echo "Downloading daily plots from ${REMOTE_USER}@${REMOTE_HOST} to ${DEST}"
echo "Enter the password for ${REMOTE_USER}@${REMOTE_HOST} when prompted."
scp -q -r "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/*" "$DEST/"
rm -rf "$DEST/tmp"

count=$(find "$DEST" -type f -name '*.png' | wc -l)
echo "Done. ${count} daily plot image(s) are available in ${DEST}"
echo "Each image is named YYYYMMDD-STATION-CHn.png (CH1/CH2/CH3 = N/E/Z), ready for upload."
