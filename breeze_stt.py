"""Minimal, dependency-free client for the Breeze-ASR-25 Hugging Face Space.

The Space is a Gradio app, so we talk to it with Gradio's plain HTTP API
(the same thing the Space's agents.md / "Use via API" page describes):

  1. POST {space}/gradio_api/upload            (multipart, field "files")  -> ["/tmp/gradio/.../rec.wav"]
  2. POST {space}/gradio_api/call/{api_name}   {"data": [<FileData>, ...]} -> {"event_id": "..."}
  3. GET  {space}/gradio_api/call/{api_name}/{event_id}  (server-sent events) -> "event: complete\\ndata: [...]"

The endpoint name and its parameters are discovered at runtime from
{space}/gradio_api/info, so this keeps working if the Space renames things.
How the Breeze Space works (checked against the live Space, Gradio 5.34):
  /process_audio(audio, model)  -> [<gr.State>, "轉錄完成"]   transcript kept in the State
  /apply (State .change)        -> @gr.render: one textbox per sentence + export button
  /update_export_areas(State)   -> [SRT text, plain text]   (exists only after /apply)
So we read {space}/config and run that chain through the queue API
(/queue/join + /queue/data, as the Space's agents.md describes) in one
session_hash so the State carries over. /call is only used without /config.
Only the Python standard library is used, so it runs on an old 32-bit Pi.

Run `python3 breeze_stt.py --info` to print the Space's API,
`python3 breeze_stt.py --raw some.wav` to see every output the Space returns,
`python3 breeze_stt.py --diagnose some.wav` to also dump the Space's step graph, or
`python3 breeze_stt.py some.wav` to transcribe a file.
"""

import json
import mimetypes
import os
import re
import sys
import uuid
import urllib.error
import urllib.request

DEFAULT_SPACE_URL = "https://wizardforest-breeze-asr-25-gui.hf.space"

# Gradio 5 serves the API under /gradio_api, Gradio 4 at the root.
_PREFIXES = ("/gradio_api", "")


class STTError(RuntimeError):
    pass


class NoSpeech(STTError):
    """The Space ran fine but recognized nothing (silent or very quiet audio)."""


class BreezeSTT:
    def __init__(self, space_url=DEFAULT_SPACE_URL, api_name=None, hf_token=None, timeout=180,
                 output_index=None):
        self.space_url = space_url.rstrip("/")
        self.output_index = output_index
        self.api_name = api_name.lstrip("/") if api_name else None
        self.hf_token = hf_token or None
        self.timeout = timeout
        self._prefix = None
        self._info = None

    # ---------------------------------------------------------------- http
    def _request(self, method, path, data=None, headers=None, timeout=None):
        url = self.space_url + path
        hdrs = {"User-Agent": "aiy-picoclaw/1.0"}
        if self.hf_token:
            hdrs["Authorization"] = "Bearer " + self.hf_token
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        return urllib.request.urlopen(req, timeout=timeout or self.timeout)

    def _json(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        with self._request(method, path, data, headers) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---------------------------------------------------------------- api info
    def info(self):
        if self._info is not None:
            return self._info
        last_err = None
        for prefix in _PREFIXES:
            try:
                self._info = self._json("GET", prefix + "/info")
                self._prefix = prefix
                return self._info
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code != 404:
                    break
            except urllib.error.URLError as e:
                last_err = e
                break
        raise STTError("cannot read Gradio API info from %s: %s" % (self.space_url, last_err))

    def _pick_endpoint(self):
        endpoints = self.info().get("named_endpoints", {})
        if self.api_name:
            ep = endpoints.get("/" + self.api_name)
            if ep is None:
                raise STTError("endpoint /%s not found; available: %s"
                               % (self.api_name, ", ".join(sorted(endpoints))))
            return self.api_name, ep

        def has_audio(ep):
            return any(_is_file_param(p) for p in ep.get("parameters", []))

        candidates = [(name, ep) for name, ep in endpoints.items() if has_audio(ep)]
        if not candidates:
            raise STTError("no endpoint with an audio/file input; available: %s"
                           % ", ".join(sorted(endpoints)))
        # Prefer something that looks like transcription.
        candidates.sort(key=lambda c: (not any(k in c[0].lower() for k in ("transcri", "asr", "stt", "predict")), c[0]))
        name, ep = candidates[0]
        return name.lstrip("/"), ep

    # ---------------------------------------------------------------- calls
    def _upload(self, wav_path):
        boundary = "----aiy" + uuid.uuid4().hex
        filename = os.path.basename(wav_path)
        ctype = mimetypes.guess_type(filename)[0] or "audio/wav"
        with open(wav_path, "rb") as f:
            content = f.read()
        body = b"".join([
            ("--%s\r\n" % boundary).encode(),
            ('Content-Disposition: form-data; name="files"; filename="%s"\r\n' % filename).encode(),
            ("Content-Type: %s\r\n\r\n" % ctype).encode(),
            content,
            ("\r\n--%s--\r\n" % boundary).encode(),
        ])
        headers = {"Content-Type": "multipart/form-data; boundary=" + boundary}
        with self._request("POST", self._prefix + "/upload", body, headers) as resp:
            paths = json.loads(resp.read().decode("utf-8"))
        if not paths:
            raise STTError("upload returned no path")
        return paths[0], filename, len(content), ctype

    def transcribe(self, wav_path):
        steps = self.transcribe_raw(wav_path)
        # The transcript can arrive in streamed "generating" updates, or only in a
        # follow-up step (the Breeze Space returns [<State>, "轉錄完成"] and a hidden
        # .then() step shows the text), so search everything, newest first.
        for _, _, labels, outputs in reversed(steps):
            text = self._pick_output(outputs, labels)
            if text:
                # The plain-text export puts one sentence per line.
                return " ".join(line.strip() for line in text.splitlines() if line.strip())
        if any(event == "rendered" for _, event, _, _ in steps):
            raise NoSpeech("no speech recognized: is the recording silent? (check with: aplay %s)" % wav_path)
        raise STTError("no transcript in result: %r" % (steps[-1][3] if steps else None,))

    def transcribe_raw(self, wav_path):
        """Run the endpoint plus the follow-up steps the web page would run.

        Returns [(step name, event, output labels, outputs), ...].
        """
        name, ep = self._pick_endpoint()
        server_path, filename, size, ctype = self._upload(wav_path)
        file_data = {
            "path": server_path,
            "orig_name": filename,
            "size": size,
            "mime_type": ctype,
            "meta": {"_type": "gradio.FileData"},
        }

        args = []
        for p in ep.get("parameters", []):
            if _is_file_param(p):
                args.append(file_data)
            elif p.get("parameter_has_default"):
                args.append(p.get("parameter_default"))
            else:
                args.append(None)

        # One session for the whole chain, so gr.State values carry over.
        session = uuid.uuid4().hex[:11]
        config = self._config()
        dep = _find_dep(config, name)
        steps = []
        if dep is None:  # no /config: just the documented endpoint
            labels = [r.get("label") for r in ep.get("returns", [])]
            for event, outputs in self._call_api(name, args):
                steps.append(("/" + name, event, labels, outputs))
            return steps

        comps = {c["id"]: c for c in config.get("components", [])}
        values = {}  # component id -> latest value we know
        queue = [(dep, args)]
        done = set()
        while queue and len(done) < 12:
            d, d_args = queue.pop(0)
            did = _dep_id(config, d)
            done.add(did)
            step = ("/" + d["api_name"]) if d.get("api_name") else "fn %d" % did
            labels = [(comps.get(o, {}).get("props") or {}).get("label") for o in d.get("outputs", [])]
            last = None
            render = {}
            for event, outputs in self._call_queue(did, d_args, session, render):
                steps.append((step, event, labels, outputs))
                last = outputs
            if last is not None:
                for cid, v in zip(d.get("outputs", []), last):
                    if not (isinstance(v, dict) and v.get("__type__") == "update"):
                        values[cid] = v

            # A @gr.render step (the Breeze Space's "apply") builds new components
            # on the fly: one textbox per sentence, plus an export button whose
            # step turns the State into SRT and plain text.
            rendered = render.get("components") or []
            for c in rendered:
                comps[c["id"]] = c
            segments = [(c.get("props") or {}).get("value") for c in rendered if c.get("type") == "textbox"]
            segments = [v.strip() for v in segments if isinstance(v, str) and v.strip()]
            if render:
                steps.append((step, "rendered", ["轉錄結果"], [" ".join(segments)]))
            for rd in render.get("dependencies") or []:
                ins = rd.get("inputs", [])
                if (rd.get("backend_fn", True) and rd.get("outputs") and ins
                        and all(comps.get(i, {}).get("type") == "state" for i in ins)
                        and _dep_id(config, rd) not in done):
                    queue.append((rd, [None] * len(ins)))

            for nxt in _followups(config, d):
                if _dep_id(config, nxt) in done or not nxt.get("backend_fn", True):
                    continue
                nxt_args = []
                for cid in nxt.get("inputs", []):
                    c = comps.get(cid, {})
                    if c.get("type") == "state":
                        nxt_args.append(None)  # server uses the session's stored value
                    elif cid in values:
                        nxt_args.append(values[cid])
                    else:
                        nxt_args.append((c.get("props") or {}).get("value"))
                queue.append((nxt, nxt_args))
        return steps

    def _config(self):
        for path in ("/config", self._prefix + "/config"):
            try:
                return self._json("GET", path)
            except Exception:
                continue
        return {}

    def _call_api(self, api_name, args):
        # No session_hash here: Gradio 5 files /call results under the event id,
        # and a custom session_hash makes the result lookup fail with
        # "404: Session not found."
        event = self._json("POST", "%s/call/%s" % (self._prefix, api_name), {"data": args})
        event_id = event.get("event_id")
        if not event_id:
            raise STTError("no event_id in response: %r" % (event,))
        return self._read_sse("%s/call/%s/%s" % (self._prefix, api_name, event_id))

    def _call_queue(self, fn_index, args, session, render=None):
        """Gradio's queue API (what the web page uses). Unlike /call it lets several
        steps share one session_hash, so gr.State carries over between them."""
        event = self._json("POST", self._prefix + "/queue/join",
                           {"data": args, "fn_index": fn_index, "session_hash": session,
                            "event_data": None, "trigger_id": None})
        event_id = event.get("event_id")
        events = []
        current = None
        path = "%s/queue/data?session_hash=%s" % (self._prefix, session)
        with self._request("GET", path, headers={"Accept": "text/event-stream"}) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                msg = json.loads(line[5:].strip())
                if msg.get("event_id") not in (None, event_id):
                    continue
                kind = msg.get("msg")
                output = msg.get("output") or {}
                if kind == "process_generating" and isinstance(output.get("data"), list):
                    # First update has full values, later ones are diffs against it.
                    if current is None:
                        current = list(output["data"])
                    else:
                        current = [_apply_diff(c, d) for c, d in zip(current, output["data"])]
                    events.append(("generating", list(current)))
                elif kind == "process_completed":
                    if not msg.get("success", True):
                        raise STTError("space step %s failed: %s" % (fn_index, output.get("error") or msg))
                    if render is not None and output.get("render_config"):
                        render.update(output["render_config"])
                    events.append(("complete", output.get("data") or []))
                    return events
        return events

    def _pick_output(self, result, labels):
        """The Space may return several outputs (e.g. a status line such as
        "轉錄完成" plus the transcript), so pick the one that is the transcript."""
        if self.output_index is not None:
            if self.output_index >= len(result):
                return None
            return self._output_text(result[self.output_index])

        candidates = []
        for i, value in enumerate(result):
            text = self._output_text(value)
            if not text or not text.strip():
                continue
            label = (labels[i] if i < len(labels) else None) or ""
            if _STATUS_LABEL.search(label) or _STATUS_TEXT.match(text.strip()):
                continue
            candidates.append((1 if _TEXT_LABEL.search(label) else 0, len(text), text))
        if not candidates:
            return None
        return max(candidates)[2]

    def _output_text(self, value):
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("text", "value", "transcription"):
                if isinstance(value.get(key), str):
                    return value[key]
            # A downloadable transcript file (.txt / .srt / .vtt).
            url = value.get("url") or ""
            if url and re.search(r"\.(txt|srt|vtt)$", url, re.I):
                path = url[len(self.space_url):] if url.startswith(self.space_url) else None
                if path is None:
                    return None
                with self._request("GET", path) as resp:
                    return _strip_subtitles(resp.read().decode("utf-8", "replace"))
        return None

    def _read_sse(self, path):
        """Collect every "generating" update and the final "complete" result."""
        events = []
        event = None
        with self._request("GET", path, headers={"Accept": "text/event-stream"}) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event in ("generating", "complete"):
                        outputs = json.loads(data)
                        if outputs is not None:
                            events.append((event, outputs if isinstance(outputs, list) else [outputs]))
                        if event == "complete":
                            return events
                    elif event == "error":
                        raise STTError("space returned error: %s" % data)
        if events:
            return events
        raise STTError("stream ended without a result (Space asleep or out of quota?)")


def _apply_diff(value, diff):
    """Apply a Gradio streaming diff: [[action, path, value], ...]."""
    import copy

    value = copy.deepcopy(value)
    for action, path, v in diff:
        if not path:
            value = v if action == "replace" else value + v
            continue
        target = value
        for key in path[:-1]:
            target = target[key]
        key = path[-1]
        if action == "replace":
            target[key] = v
        elif action == "append":
            target[key] += v
        elif action == "add":
            if isinstance(target, list):
                target.insert(int(key), v)
            else:
                target[key] = v
        elif action == "delete":
            del target[int(key) if isinstance(target, list) else key]
    return value


def _dep_id(config, dep):
    if "id" in dep:
        return dep["id"]
    return config["dependencies"].index(dep)


def _find_dep(config, api_name):
    for d in config.get("dependencies", []):
        if d.get("api_name") == api_name:
            return d
    return None


def _followups(config, dep):
    """Steps the browser runs after dep: .then()/.success() chains, and
    .change() listeners on the components dep writes to."""
    did = _dep_id(config, dep)
    outs = set(dep.get("outputs", []))
    found = []
    for d in config.get("dependencies", []):
        if d is dep:
            continue
        if d.get("trigger_after") == did:
            found.append(d)
        elif any(t and t[0] in outs and t[1] == "change" for t in d.get("targets", [])):
            found.append(d)
    return found


def _is_file_param(p):
    comp = (p.get("component") or "").lower()
    if comp in ("audio", "file", "microphone", "uploadbutton"):
        return True
    ptype = json.dumps(p.get("python_type", {})).lower()
    return "filepath" in ptype or "filedata" in ptype


_STATUS_LABEL = re.compile(r"status|狀態|state|message|訊息|log|info|進度|progress", re.I)
_TEXT_LABEL = re.compile(r"轉錄|轉寫|文字|結果|text|transcri|result|output", re.I)
_STATUS_TEXT = re.compile(r"^[^\n]{0,20}(完成|成功|失敗|錯誤|處理中|done|success|complete[d]?|error|failed)[!！。.]?$", re.I)


def _strip_subtitles(text):
    """Turn SRT/VTT into plain text; plain text passes through unchanged."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or line.isdigit() or "-->" in line:
            continue
        lines.append(line)
    return " ".join(lines)


def main(argv):
    stt = BreezeSTT(
        os.environ.get("STT_SPACE_URL", DEFAULT_SPACE_URL),
        os.environ.get("STT_API_NAME") or None,
        os.environ.get("HF_TOKEN") or None,
        output_index=int(os.environ["STT_OUTPUT_INDEX"]) if os.environ.get("STT_OUTPUT_INDEX") else None,
    )
    if len(argv) >= 2 and argv[1] == "--diagnose":
        # Everything needed to see where a Space puts its result, in one paste.
        config = stt._config() if stt.info() is not None else {}
        print("gradio", config.get("version"), "| endpoint picked: /%s" % stt._pick_endpoint()[0])
        comps = {c["id"]: c for c in config.get("components", [])}

        def desc(cid):
            c = comps.get(cid, {})
            props = c.get("props") or {}
            extra = "" if props.get("visible", True) else ",hidden"
            return "%s#%s%s(%s)" % (c.get("type"), cid, extra, props.get("label") or "")

        for i, d in enumerate(config.get("dependencies", [])):
            print("dep %s api=%s trig=%s after=%s backend=%s js=%s\n    in : %s\n    out: %s" % (
                d.get("id", i), d.get("api_name"), d.get("targets"), d.get("trigger_after"),
                d.get("backend_fn"), bool(d.get("js")),
                ", ".join(desc(x) for x in d.get("inputs", [])),
                ", ".join(desc(x) for x in d.get("outputs", []))))
        if len(argv) < 3:
            return 0
        argv = [argv[0], "--raw", argv[2]]
    if len(argv) >= 3 and argv[1] == "--raw":
        for step, event, labels, outputs in stt.transcribe_raw(argv[2]):
            print("--- %s %s" % (step, event))
            for i, value in enumerate(outputs):
                label = labels[i] if i < len(labels) else None
                print("[%d] %s: %s" % (i, label, json.dumps(value, ensure_ascii=False)))
        return 0
    if len(argv) < 2 or argv[1] == "--info":
        print(json.dumps(stt.info(), ensure_ascii=False, indent=2))
        name, _ = stt._pick_endpoint()
        print("\n=> would use endpoint /%s" % name, file=sys.stderr)
        return 0
    print(stt.transcribe(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
