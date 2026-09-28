"""Minimal, dependency-free client for the Breeze-ASR-25 Hugging Face Space.

The Space is a Gradio app, so we talk to it with Gradio's plain HTTP API
(the same thing the Space's agents.md / "Use via API" page describes):

  1. POST {space}/gradio_api/upload            (multipart, field "files")  -> ["/tmp/gradio/.../rec.wav"]
  2. POST {space}/gradio_api/call/{api_name}   {"data": [<FileData>, ...]} -> {"event_id": "..."}
  3. GET  {space}/gradio_api/call/{api_name}/{event_id}  (server-sent events) -> "event: complete\\ndata: [...]"

The endpoint name and its parameters are discovered at runtime from
{space}/gradio_api/info, so this keeps working if the Space renames things.
Only the Python standard library is used, so it runs on an old 32-bit Pi.

Run `python3 breeze_stt.py --info` to print the Space's API, or
`python3 breeze_stt.py some.wav` to transcribe a file.
"""

import json
import mimetypes
import os
import sys
import uuid
import urllib.error
import urllib.request

DEFAULT_SPACE_URL = "https://wizardforest-breeze-asr-25-gui.hf.space"

# Gradio 5 serves the API under /gradio_api, Gradio 4 at the root.
_PREFIXES = ("/gradio_api", "")


class STTError(RuntimeError):
    pass


class BreezeSTT:
    def __init__(self, space_url=DEFAULT_SPACE_URL, api_name=None, hf_token=None, timeout=180):
        self.space_url = space_url.rstrip("/")
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

        event = self._json("POST", "%s/call/%s" % (self._prefix, name), {"data": args})
        event_id = event.get("event_id")
        if not event_id:
            raise STTError("no event_id in response: %r" % (event,))

        result = self._read_sse("%s/call/%s/%s" % (self._prefix, name, event_id))
        text = _first_string(result)
        if text is None:
            raise STTError("no text in result: %r" % (result,))
        return text.strip()

    def _read_sse(self, path):
        event = None
        with self._request("GET", path, headers={"Accept": "text/event-stream"}) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event == "complete":
                        return json.loads(data)
                    if event == "error":
                        raise STTError("space returned error: %s" % data)
        raise STTError("stream ended without a result (Space asleep or out of quota?)")


def _is_file_param(p):
    comp = (p.get("component") or "").lower()
    if comp in ("audio", "file", "microphone", "uploadbutton"):
        return True
    ptype = json.dumps(p.get("python_type", {})).lower()
    return "filepath" in ptype or "filedata" in ptype


def _first_string(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        for v in value:
            s = _first_string(v)
            if s is not None:
                return s
    if isinstance(value, dict):
        for key in ("text", "value", "transcription"):
            if isinstance(value.get(key), str):
                return value[key]
    return None


def main(argv):
    stt = BreezeSTT(
        os.environ.get("STT_SPACE_URL", DEFAULT_SPACE_URL),
        os.environ.get("STT_API_NAME") or None,
        os.environ.get("HF_TOKEN") or None,
    )
    if len(argv) < 2 or argv[1] == "--info":
        print(json.dumps(stt.info(), ensure_ascii=False, indent=2))
        name, _ = stt._pick_endpoint()
        print("\n=> would use endpoint /%s" % name, file=sys.stderr)
        return 0
    print(stt.transcribe(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
