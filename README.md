# aiy-picoclaw

Push-button voice chat for an old **Google AIY Voice Kit v1** (Voice HAT + Raspberry Pi 3), with
[picoclaw](https://github.com/sipeed/picoclaw) as the agent.

Press the button on the box and one round runs:

```
bot   greeting                  edge-tts (zh-CN-YunxiaNeural)  → speaker
user  speak, press button again arecord 16 kHz mono            → Breeze-ASR-25 (HF Space)
bot   answer                    picoclaw agent -m "…"          → edge-tts → speaker
```

The arcade button's LED shows the state: blinking = bot is talking, on = listening,
pulsing = thinking.

Everything is free: STT is the [Breeze-ASR-25 Space](https://huggingface.co/spaces/WizardForest/Breeze-ASR-25-gui)
called through its Gradio HTTP API, TTS is `edge-tts`, and the LLM is whatever
your `~/.picoclaw/config.json` already points to (e.g. `nemotron-3-super` on ollama.com).
The Python code only uses the standard library, so it runs on a 32-bit Pi.

## Setup on the Pi

```sh
git clone https://github.com/x1001000/aiy-picoclaw.git
cd aiy-picoclaw
./install.sh                 # apt: alsa-utils mpg123 gpiozero; pip: edge-tts
./setup_voicehat_v1.sh       # Voice HAT driver + /etc/asound.conf, then reboot
nano config.env              # set PICOCLAW_BIN to where your picoclaw binary is
```

`setup_voicehat_v1.sh` enables `dtoverlay=googlevoicehat-soundcard`, turns off the
Pi's onboard audio, and installs `asound.conf.voicehat-v1` as `/etc/asound.conf`.
That config makes the HAT the default device and converts formats for it (the HAT
only does stereo 48 kHz), with the same mic boost Google's v1 image used. It backs
up what it changes (`*.bak.aiy`). Skip it if `arecord`/`aplay` already work.

Check each piece on its own first:

```sh
~/picoclaw agent -m "hi"                                  # LLM (you already did this)
edge-tts --voice zh-CN-YunxiaNeural --text "你好" --write-media /tmp/t.mp3 && mpg123 /tmp/t.mp3   # TTS + speaker
arecord -f S16_LE -r 16000 -c 1 -d 3 /tmp/r.wav && aplay /tmp/r.wav                              # mic
python3 breeze_stt.py --info                              # STT: lists the Space's API endpoints
python3 breeze_stt.py /tmp/r.wav                          # STT: transcribe your recording
python3 breeze_stt.py --raw /tmp/r.wav                    # STT: show every step and output the Space returns
```

Then run the demo:

```sh
python3 voice_chat.py              # button on the box
python3 voice_chat.py --keyboard   # use Enter as the button (e.g. over SSH)
```

To start it at boot, see the comments at the top of `aiy-picoclaw.service`.

## Configuration

All settings live in `config.env` (copied from `config.env.example`); environment
variables override them. The useful ones:

| key | default | |
|---|---|---|
| `PICOCLAW_BIN` | `~/picoclaw` | path to the binary |
| `PICOCLAW_SESSION` | `aiy:voice` | picoclaw keeps history per session; change it to reset |
| `GREETING_MODE` | `fixed` | `fixed` speaks `GREETING` (cached, instant); `llm` lets picoclaw greet |
| `TTS_VOICE` | `zh-CN-YunxiaNeural` | any `edge-tts --list-voices` voice |
| `MAX_RECORD_SEC` | `15` | recording stops at this limit or on the next button press |
| `STT_API_NAME` | auto | Space endpoint; auto-detected from `/gradio_api/info` |
| `STT_OUTPUT_INDEX` | auto | which Space output is the transcript (see `--raw`); auto skips status text like 轉錄完成 |
| `HF_TOKEN` | – | optional, helps when the free Space quota runs out |
| `ARECORD_DEVICE` | `default` | e.g. `plughw:0,0` if `default` is not the kit's mic |

## Troubleshooting

- **No sound card / `arecord -l` is empty.** Run `./setup_voicehat_v1.sh` and reboot.
  `aplay -l` should then list `sndrpigooglevoi`.
- **`arecord: ... Invalid argument` or sound from the wrong output.** `/etc/asound.conf`
  is missing or points at another card; the setup script installs the right one.
- **Recordings too quiet or distorted.** Adjust the `30.0` gain in `/etc/asound.conf`
  (`micboost` section). Speaker volume: `amixer set Master 80%`.
- **Button does nothing.** Without the `aiy` library the script uses `gpiozero`:
  button on BCM 23, button LED on BCM 25 (the v1 wiring). The startup log says which
  input it picked.
- **STT prints 轉錄完成 (or another status) instead of your words.** Run
  `python3 breeze_stt.py --raw /tmp/r.wav`, find the transcript's `[index]`, and set
  `STT_OUTPUT_INDEX` to it in `config.env`.
- **STT errors / slow first request.** Free Spaces go to sleep; the first call can
  take a minute to wake it. `python3 breeze_stt.py --info` shows whether it is up.
- **`edge-tts` not found.** It installs to `~/.local/bin`; add that to `PATH`, or the
  script falls back to `python3 -m edge_tts`. Recent `edge-tts` needs Python ≥ 3.8.
