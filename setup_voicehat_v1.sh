#!/bin/sh
# One-time audio setup for the AIY Voice Kit v1 (Voice HAT). Reboot afterwards.
#  - enables the Voice HAT driver (dtoverlay=googlevoicehat-soundcard)
#  - turns off the Pi's onboard audio so it doesn't become the default card
#  - installs asound.conf.voicehat-v1 as /etc/asound.conf (old one backed up)
set -e
cd "$(dirname "$0")"

CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt
echo "boot config: $CONFIG"
sudo cp "$CONFIG" "$CONFIG.bak.aiy"

if ! grep -q '^dtoverlay=googlevoicehat-soundcard' "$CONFIG"; then
    echo 'dtoverlay=googlevoicehat-soundcard' | sudo tee -a "$CONFIG" >/dev/null
    echo "  + dtoverlay=googlevoicehat-soundcard"
fi
if grep -q '^dtparam=audio=on' "$CONFIG"; then
    sudo sed -i 's/^dtparam=audio=on/#dtparam=audio=on  # off for AIY Voice HAT/' "$CONFIG"
    echo "  - dtparam=audio=on (onboard audio disabled)"
fi

if [ -f /etc/asound.conf ]; then
    sudo cp /etc/asound.conf /etc/asound.conf.bak.aiy
fi
sudo cp asound.conf.voicehat-v1 /etc/asound.conf
echo "installed /etc/asound.conf"

echo
echo "Done. Reboot, then check:"
echo "  aplay -l          # should list: sndrpigooglevoi [snd_rpi_googlevoicehat_soundcard]"
echo "  speaker-test -t wav -c 2 -l 1"
echo "  arecord -f S16_LE -r 16000 -c 1 -d 3 /tmp/r.wav && aplay /tmp/r.wav"
