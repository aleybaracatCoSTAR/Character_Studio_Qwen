"""Standalone streamed TTS test, Python standard library only. No model loading.

Run on your Mac through an SSH tunnel, or on the GPU host to save WAVs there.
--play optionally pipes arriving PCM to a locally installed ffplay player.
"""
import argparse
import base64
from html import escape
import json
from pathlib import Path
import shutil
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import wave

RATE = 44100
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MODEL = "fishaudio/drama-3"
SAMPLES = [
    ("01-anna-calm", "I am Anna, the washing machine. The housekeeper understands me. The rest of the family are still looking for the on button."),
    ("02-anna-frustrated", "Six years of delicate cycles, and they still call me complicated. Honestly, [sighs] I have three buttons."),
    ("03-anna-hopeful", "You want to learn? [excited] Finally, a little ambition in the utility room. Let us start with the jumper."),
]


def build_payload(text, max_tokens=300, reference=None, transcript=None):
    if not text.strip(): raise ValueError("Enter some text to speak.")
    if bool(reference) != bool(transcript):
        raise ValueError("A reference WAV and its exact transcript must be supplied together.")
    data = {"model": MODEL, "input": text, "voice": "default", "stream": True,
            "stream_format": "audio", "response_format": "pcm", "sample_rate": RATE,
            "max_new_tokens": max_tokens}
    if reference:
        audio = Path(reference).read_bytes()
        if len(audio) > 8 * 1024 * 1024: raise ValueError("Use a short reference WAV under 8 MB.")
        data["ref_audio"] = "data:audio/wav;base64," + base64.b64encode(audio).decode()
        data["ref_text"] = transcript
    return data


def stream_pcm(response, output, started, player=None, clock=time.perf_counter):
    """read1 avoids waiting for an arbitrary large read buffer to fill."""
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
    if content_type not in ("audio/pcm", "audio/raw", "audio/L16", "application/octet-stream"):
        raise ValueError(f"Expected raw PCM, got {content_type or 'no content type'}. Check the runtime version.")
    output = Path(output); temporary = output.with_suffix(".partial.wav")
    first = None; total = 0; remainder = b""
    try:
        with wave.open(str(temporary), "wb") as writer:
            writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(RATE)
            while True:
                chunk = response.read1(65536)
                if not chunk: break
                if first is None: first = clock() - started
                total += len(chunk)
                if total > MAX_AUDIO_BYTES: raise ValueError("Audio exceeded the test's 20 MB limit.")
                if total == len(chunk) and chunk.startswith(b"RIFF"):
                    raise ValueError("Received a WAV header where raw PCM was expected.")
                merged = remainder + chunk; aligned = len(merged) // 2 * 2
                pcm, remainder = merged[:aligned], merged[aligned:]
                writer.writeframesraw(pcm)
                if player and pcm: player.stdin.write(pcm); player.stdin.flush()
            if not total or remainder: raise ValueError("The audio stream was empty or incomplete.")
        elapsed = clock() - started
        temporary.replace(output)
        duration = total / (2 * RATE)
        return {"first_audio_seconds": round(first, 3), "total_seconds": round(elapsed, 3),
                "audio_seconds": round(duration, 3), "realtime_factor": round(elapsed / duration, 3),
                "bytes": total}
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def generate(base_url, payload, output, play=False):
    player = None
    if play:
        executable = shutil.which("ffplay")
        if not executable: raise ValueError("ffplay is not installed. Omit --play to save WAVs and listen afterwards.")
        player = subprocess.Popen([executable, "-nodisp", "-autoexit", "-loglevel", "error",
                                   "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0"],
                                  stdin=subprocess.PIPE)
    started = time.perf_counter()
    request = Request(base_url.rstrip("/") + "/v1/audio/speech", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=180) as response:
            return stream_pcm(response, output, started, player)
    finally:
        if player:
            try: player.stdin.close()
            except BrokenPipeError: pass
            try: player.wait(timeout=60)
            except subprocess.TimeoutExpired: player.kill(); player.wait()


def listening_page(results, directory):
    cards = []
    for item in results:
        label, text, path, metrics = (item[key] for key in ("label", "text", "file", "metrics"))
        cards.append(f'<article><h2>{escape(label)}</h2><p>{escape(text)}</p>'
                     f'<audio controls src="{escape(path, quote=True)}"></audio>'
                     f'<p class="meta">First audio: {metrics["first_audio_seconds"]}s · '
                     f'Total: {metrics["total_seconds"]}s · Audio: {metrics["audio_seconds"]}s</p></article>')
    document = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
    document += '<title>Anna · Drama 3 voice test</title><style>body{font:16px/1.6 system-ui;max-width:780px;margin:50px auto;padding:0 24px;background:#faf9f6;color:#242321}h1{font-family:Georgia,serif;font-weight:400}article{padding:24px 0;border-top:1px solid #ddd}h2{font-size:17px}audio{width:100%}.meta,footer{font-size:12px;color:#777}</style>'
    document += '<h1>Anna · Drama 3 voice test</h1><p>Scripted lines spoken by a TTS model. This is not a conversation or an intelligence test.</p>'
    document += ''.join(cards) + '<footer>Built with Fish Audio · Times measured by the client; include transport and reference processing. First request may compile kernels.</footer>'
    (directory / "listen.html").write_text(document)


def main():
    parser = argparse.ArgumentParser(description="Test Drama 3's voice and streamed audio latency.")
    parser.add_argument("--url", default="http://127.0.0.1:18093")
    parser.add_argument("--text", help="Speak your own line instead of the three Anna examples.")
    parser.add_argument("--ref-audio", type=Path, help="Optional local WAV, sent to your model server.")
    parser.add_argument("--ref-text", help="Exact transcript of the optional reference.")
    parser.add_argument("--max-tokens", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("outputs/drama3-voice-test"))
    parser.add_argument("--play", action="store_true", help="Play incoming PCM locally using an existing ffplay installation.")
    args = parser.parse_args()
    if not 1 <= args.max_tokens <= 1000 or not 0 <= args.warmup <= 3:
        parser.error("Use max-tokens 1–1000 and warmup 0–3.")
    samples = [("custom", args.text)] if args.text is not None else SAMPLES
    try:
        payloads = [build_payload(text, args.max_tokens, args.ref_audio, args.ref_text) for _, text in samples]
        args.output.mkdir(parents=True, exist_ok=True)
        if args.play and not shutil.which("ffplay"): raise ValueError("ffplay is missing. Omit --play to save playable WAV files.")
        for number in range(args.warmup):
            print(f"Warmup {number+1}/{args.warmup} (not part of the measurements)…", flush=True)
            path = args.output / "warmup.wav"
            generate(args.url, payloads[0], path)
            path.unlink(missing_ok=True)
        results = []
        for (label, text), payload in zip(samples, payloads):
            print(f"Generating {label}…", flush=True)
            output = args.output / (label + ".wav")
            metrics = generate(args.url, payload, output, args.play)
            results.append({"label": label, "text": text, "file": output.name, "metrics": metrics})
            print(json.dumps(metrics), flush=True)
            (args.output / "results.json").write_text(json.dumps({"model": MODEL, "samples": results}, indent=2) + "\n")
            listening_page(results, args.output)
        print(f"Listen: {(args.output / 'listen.html').resolve()}")
    except (HTTPError, URLError, ValueError, OSError) as exc:
        parser.exit(1, f"Voice test failed: {exc}\nCheck the server terminal and /health. No GPU test result is implied by this client.\n")


if __name__ == "__main__": main()
