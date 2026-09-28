#!/bin/sh
# One-time setup on the Raspberry Pi. Installs only what is missing.
cd "$(dirname "$0")"

need=""
command -v arecord >/dev/null 2>&1 || need="$need alsa-utils"
command -v mpg123 >/dev/null 2>&1 || need="$need mpg123"
python3 -c "import gpiozero" >/dev/null 2>&1 || need="$need python3-gpiozero"
python3 -m pip --version >/dev/null 2>&1 || need="$need python3-pip"

if [ -n "$need" ]; then
    # Raspbian Buster is end-of-life: its packages moved to legacy.raspbian.org.
    if grep -q buster /etc/os-release && grep -q 'raspbian.raspberrypi.org' /etc/apt/sources.list 2>/dev/null; then
        echo "Buster detected: pointing apt at legacy.raspbian.org (backup: /etc/apt/sources.list.bak.aiy)"
        sudo cp /etc/apt/sources.list /etc/apt/sources.list.bak.aiy
        sudo sed -i 's#raspbian.raspberrypi.org#legacy.raspbian.org#g' /etc/apt/sources.list
    fi
    # Other broken repos (e.g. an expired Coral key) only cause warnings here.
    sudo apt-get update || echo "(apt-get update reported errors; trying to install anyway)"
    if ! sudo apt-get install -y $need; then
        echo "apt could not install:$need"
        exit 1
    fi
fi

python3 -m pip install --user --upgrade edge-tts || exit 1

[ -f config.env ] || cp config.env.example config.env

echo
echo "Sound cards found:"
arecord -l || true
echo
echo "Python: $(python3 --version 2>&1)"
python3 -m edge_tts --version 2>/dev/null || echo "edge-tts: installed (run with: python3 -m edge_tts)"
echo
echo "Next: edit config.env (PICOCLAW_BIN), then run:  python3 voice_chat.py"
