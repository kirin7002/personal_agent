"""Local prerecorded PCM fixtures and real-time ingress; no audio API mocking."""
import asyncio
import base64
from pathlib import Path
import subprocess
import wave


def read_pcm(path):
    with wave.open(str(path)) as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
            raise ValueError("Audio fixture must be mono PCM16 at 16kHz")
        pcm = wav.readframes(wav.getnframes())
    if len(pcm) < 3200:
        raise ValueError("Audio fixture is too short")
    return pcm


def prepare(root, folder):
    voices = subprocess.run(["/usr/bin/say", "-v", "?"], capture_output=True, text=True, timeout=10)
    if voices.returncode or not any(line.startswith("Tingting") for line in voices.stdout.splitlines()):
        raise RuntimeError("Local Tingting voice is unavailable; no voice will be installed")
    aiff, wav = folder / "correction-21.aiff", folder / "correction-21.wav"
    subprocess.run(["/usr/bin/say", "-v", "Tingting", "-o", str(aiff), "改成二十一度"],
                   check=True, capture_output=True, timeout=20)
    subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
                   check=True, capture_output=True, timeout=15)
    return read_pcm(Path(root) / "test-results/voice-command.wav"), read_pcm(wav)


async def send_pcm(browser, pcm):
    # 20-ms frames with lead/trailing silence exercise server VAD and ASR.
    data = b"\0" * 9600 + pcm + b"\0" * 48000
    for offset in range(0, len(data), 640):
        await browser.incoming.put({"type": "audio", "audio": base64.b64encode(data[offset:offset+640]).decode()})
        await asyncio.sleep(.02)
