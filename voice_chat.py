#!/usr/bin/env python3
"""AIY Voice Kit + picoclaw push-button voice chat demo.

Press the button on the box and one chat round runs:

    bot  : speaks a greeting                     (edge-tts)
    user : talks; press the button again to stop (arecord, or MAX_RECORD_SEC)
    bot  : answers                               (Breeze-ASR-25 -> picoclaw -> edge-tts)

Settings come from config.env next to this file (see config.env.example);
environment variables override it.
"""

import hashlib
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from breeze_stt import BreezeSTT, DEFAULT_SPACE_URL

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------- config
def load_config():
    cfg = {
        "PICOCLAW_BIN": os.path.expanduser("~/picoclaw"),
        "PICOCLAW_SESSION": "aiy:voice",
        "TTS_VOICE": "zh-CN-YunxiaNeural",
        "TTS_RATE": "+0%",
        "STT_SPACE_URL": DEFAULT_SPACE_URL,
        "STT_API_NAME": "",
        "HF_TOKEN": "",
        "GREETING": "你好，我是小龍蝦。請在嗶聲後說話，說完再按一次按鈕。",
        "GREETING_MODE": "fixed",  # "fixed" or "llm"
        "GREETING_PROMPT": "有人按下了語音盒的按鈕。請用一句簡短、口語的繁體中文打招呼，並邀請對方說話。",
        "REPLY_HINT": "（這是語音對話：請用繁體中文口語、簡短地回答，兩三句話以內，不要使用 markdown、列表或表情符號。）",
        "ARECORD_DEVICE": "default",
        "MAX_RECORD_SEC": "15",
        "BUTTON_GPIO": "23",
        "BUTTON_LED_GPIO": "25",
        "BEEP": "1",
    }
    path = os.path.join(HERE, "config.env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                cfg[k.strip()] = v
    for k in list(cfg):
        if k in os.environ:
            cfg[k] = os.environ[k]
    cfg["PICOCLAW_BIN"] = os.path.expanduser(cfg["PICOCLAW_BIN"])
    return cfg


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------- button / led
class KeyboardButton:
    """Fallback for testing without the kit: Enter acts as the button."""

    name = "keyboard (Enter)"

    def wait_for_press(self, timeout=None):
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            if not sys.stdin.readline():
                raise KeyboardInterrupt  # stdin closed
            return True
        return False

    def led(self, state):
        pass


class GpiozeroButton:
    name = "gpiozero"

    def __init__(self, pin, led_pin=None):
        from gpiozero import Button, PWMLED

        self._b = Button(pin, pull_up=True, bounce_time=0.05)
        self._led = PWMLED(led_pin) if led_pin is not None else None

    def wait_for_press(self, timeout=None):
        # Wait for release first so a single long press isn't counted twice.
        self._b.wait_for_release()
        return bool(self._b.wait_for_press(timeout))

    def led(self, state):
        if self._led is None:
            return
        if state == "on":
            self._led.on()
        elif state == "blink":
            self._led.blink(on_time=0.25, off_time=0.25)
        elif state == "pulse":
            self._led.pulse(fade_in_time=0.5, fade_out_time=0.5)
        else:
            self._led.off()


class AiyBoard:
    """Uses Google's aiy library if installed (button + LED on the box)."""

    name = "aiy.board"

    def __init__(self):
        from aiy.board import Board, Led

        self._board = Board()
        self._Led = Led
        self._pressed = threading.Event()
        self._board.button.when_pressed = self._pressed.set

    def wait_for_press(self, timeout=None):
        self._pressed.clear()
        return self._pressed.wait(timeout)

    def led(self, state):
        states = {"off": self._Led.OFF, "on": self._Led.ON,
                  "blink": self._Led.BLINK, "pulse": self._Led.PULSE_QUICK}
        try:
            self._board.led.state = states[state]
        except Exception:
            pass


def make_button(cfg):
    if "--keyboard" in sys.argv:
        return KeyboardButton()
    try:
        return AiyBoard()
    except Exception as e:
        log("aiy library not usable (%s); trying gpiozero" % e)
    try:
        led_pin = cfg["BUTTON_LED_GPIO"]
        return GpiozeroButton(int(cfg["BUTTON_GPIO"]), int(led_pin) if led_pin else None)
    except Exception as e:
        log("gpiozero not usable (%s); falling back to keyboard" % e)
    return KeyboardButton()


# --------------------------------------------------------------------- audio
def which_or_die(name, hint):
    path = shutil.which(name)
    if not path:
        sys.exit("missing '%s' — %s" % (name, hint))
    return path


def tts_to_file(cfg, text, out_path):
    exe = shutil.which("edge-tts")
    cmd = [exe] if exe else [sys.executable, "-m", "edge_tts"]
    cmd += ["--voice", cfg["TTS_VOICE"], "--rate=" + cfg["TTS_RATE"],
            "--text=" + text, "--write-media", out_path]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def play(path):
    if path.endswith(".wav"):
        subprocess.run(["aplay", "-q", path])
    else:
        subprocess.run(["mpg123", "-q", path])


def cached_tts(cfg, text):
    """Render text once and keep the mp3, so repeated phrases play instantly."""
    cache_dir = os.path.join(HERE, ".tts_cache")
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.sha1(("%s|%s|%s" % (cfg["TTS_VOICE"], cfg["TTS_RATE"], text)).encode()).hexdigest()
    path = os.path.join(cache_dir, key + ".mp3")
    if not os.path.exists(path):
        tts_to_file(cfg, text, path + ".tmp")
        os.rename(path + ".tmp", path)
    return path


def say(cfg, text, cache=False):
    text = clean_for_speech(text)
    if not text:
        return
    if cache:
        play(cached_tts(cfg, text))
        return
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "say.mp3")
        tts_to_file(cfg, text, path)
        play(path)


def make_beep(path, freq=880, dur=0.15, rate=16000):
    import math
    import struct
    import wave

    n = int(rate * dur)
    frames = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
                      for i in range(n))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


def record(cfg, button, wav_path):
    """Record 16 kHz mono until the button is pressed again or the time limit hits."""
    max_sec = float(cfg["MAX_RECORD_SEC"])
    cmd = ["arecord", "-q", "-D", cfg["ARECORD_DEVICE"], "-f", "S16_LE", "-r", "16000",
           "-c", "1", "-t", "wav", "-d", str(int(max_sec)), wav_path]
    proc = subprocess.Popen(cmd)
    start = time.time()
    try:
        while proc.poll() is None:
            remaining = max_sec - (time.time() - start)
            if remaining <= 0:
                break
            if button.wait_for_press(timeout=min(0.5, remaining)):
                break
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)  # arecord finalizes the WAV header on SIGINT
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    return time.time() - start


# --------------------------------------------------------------------- picoclaw
def ask_picoclaw(cfg, message):
    cmd = [cfg["PICOCLAW_BIN"], "agent", "-s", cfg["PICOCLAW_SESSION"], "-m", message]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=os.path.dirname(cfg["PICOCLAW_BIN"]) or None)
    out = res.stdout.decode("utf-8", "replace")
    if res.returncode != 0:
        raise RuntimeError("picoclaw failed (%d): %s" % (res.returncode,
                           (res.stderr.decode("utf-8", "replace") or out).strip()[-500:]))
    # picoclaw prints its logs first, then "\n🦞 <response>\n".
    marker = "\n\U0001F99E "
    idx = out.rfind(marker)
    return (out[idx + len(marker):] if idx >= 0 else out).strip()


_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def clean_for_speech(text):
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)      # [label](url) -> label
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^\s*(#+|[-*+]|\d+\.)\s+", "", text, flags=re.M)
    text = re.sub(r"[*_`>|~]", "", text)
    text = _EMOJI.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------- main loop
def chat_round(cfg, button, stt, beep_path):
    # 1. bot
    button.led("blink")
    if cfg["GREETING_MODE"] == "llm":
        greeting = ask_picoclaw(cfg, cfg["GREETING_PROMPT"])
        log("bot : " + greeting)
        say(cfg, greeting)
    else:
        log("bot : " + cfg["GREETING"])
        say(cfg, cfg["GREETING"], cache=True)

    # 2. user
    if beep_path:
        play(beep_path)
    button.led("on")
    log("listening... (press the button to stop)")
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "rec.wav")
        dur = record(cfg, button, wav)
        button.led("pulse")
        if dur < 0.7 or not os.path.exists(wav) or os.path.getsize(wav) < 16000:
            log("recording too short, skipped")
            say(cfg, "我沒有聽到聲音喔。", cache=True)
            return
        t = time.time()
        user_text = stt.transcribe(wav)
        log("user: %s   (STT %.1fs)" % (user_text, time.time() - t))
    if not user_text:
        say(cfg, "抱歉，我沒聽清楚。", cache=True)
        return

    # 3. bot
    t = time.time()
    reply = ask_picoclaw(cfg, user_text + "\n\n" + cfg["REPLY_HINT"])
    log("bot : %s   (LLM %.1fs)" % (reply, time.time() - t))
    say(cfg, reply)


def main():
    cfg = load_config()
    which_or_die("arecord", "sudo apt install alsa-utils")
    which_or_die("mpg123", "sudo apt install mpg123")
    if not shutil.which("edge-tts"):
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            sys.exit("missing edge-tts — pip3 install edge-tts")
    if not os.access(cfg["PICOCLAW_BIN"], os.X_OK):
        sys.exit("picoclaw not found at %s — set PICOCLAW_BIN in config.env" % cfg["PICOCLAW_BIN"])

    stt = BreezeSTT(cfg["STT_SPACE_URL"], cfg["STT_API_NAME"] or None, cfg["HF_TOKEN"] or None)
    button = make_button(cfg)

    beep_path = None
    if cfg["BEEP"] == "1":
        beep_path = os.path.join(tempfile.gettempdir(), "aiy_picoclaw_beep.wav")
        make_beep(beep_path)

    # Warm up: discover the STT endpoint and pre-render the greeting.
    try:
        log("STT endpoint: /%s" % stt._pick_endpoint()[0])
    except Exception as e:
        log("warning: STT space not reachable yet (%s)" % e)
    if cfg["GREETING_MODE"] != "llm":
        try:
            cached_tts(cfg, clean_for_speech(cfg["GREETING"]))
        except Exception as e:
            log("warning: could not pre-render greeting (%s)" % e)

    log("ready — press the button (%s) to chat. Ctrl+C to quit." % button.name)
    while True:
        button.led("off")
        button.wait_for_press()
        try:
            chat_round(cfg, button, stt, beep_path)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log("error: %s" % e)
            try:
                say(cfg, "抱歉，出了一點問題。", cache=True)
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
