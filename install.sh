#!/bin/sh
# One-time setup on the Raspberry Pi.
set -e
cd "$(dirname "$0")"

sudo apt-get update
sudo apt-get install -y alsa-utils mpg123 python3-pip python3-gpiozero

pip3 install --user edge-tts

[ -f config.env ] || cp config.env.example config.env

echo
echo "Sound cards found:"
arecord -l || true
echo
echo "Next: edit config.env (PICOCLAW_BIN), then run:  python3 voice_chat.py"
