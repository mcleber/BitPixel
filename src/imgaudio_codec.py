"""
imgaudio_codec.py - FSK modem for image <-> audio transmission.

Protocol:
  [silence] [preamble] [start marker] [mode(1B)][width(2B)][height(2B)][pixels][crc32(4B)]

  mode: 0=grayscale, 1=color(RGB)
  pixels: 1B/pixel for grayscale, 3B/pixel for color

Usage: imported by encode.py, decode.py, and gui_app.py
"""

from __future__ import annotations

import os
import struct
import wave
import zlib
from dataclasses import dataclass

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Protocol parameters
# ---------------------------------------------------------------------------
SAMPLE_RATE = 44100
BAUD = 300
FREQ_0 = 1200
FREQ_1 = 2200
AMPLITUDE = 0.6
PREAMBLE_BITS = 80
START_MARKER = 0x7E
START_MARKER_REPEATS = 4
SYNC_BYTE = 0xAA
RESYNC_INTERVAL = 16
MAGIC = b"IMAU"

MODE_GRAYSCALE = 0
MODE_COLOR = 1

SOUND_DIR_NAME = "output_sound"
IMAGE_DIR_NAME = "output_image_recovered"

SAMPLES_PER_BIT = int(SAMPLE_RATE / BAUD)


def set_baud(new_baud: int) -> None:
    """Change the module-level default modulation speed (bits per second)."""
    global BAUD, SAMPLES_PER_BIT
    if new_baud <= 0:
        raise ValueError("baud must be positive")
    BAUD = int(new_baud)
    SAMPLES_PER_BIT = max(1, int(SAMPLE_RATE / BAUD))


def set_tones(freq_0: int, freq_1: int) -> None:
    """Change the module-level default FSK tone pair (Hz). See set_baud()."""
    global FREQ_0, FREQ_1
    FREQ_0 = int(freq_0)
    FREQ_1 = int(freq_1)


# Speed presets: (label, baud, freq_0, freq_1).
SPEED_PRESETS = {
    "Normal (300 baud)": (300, 1200, 2200),
    "Fast (600 baud)": (600, 1000, 3000),
}


class OperationCancelled(Exception):
    """Raised internally when a running encode/decode is cancelled by the user."""


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled()


# ---------------------------------------------------------------------------
# Bit utilities
# ---------------------------------------------------------------------------
def byte_to_bits(value: int) -> list[int]:
    return [(value >> i) & 1 for i in range(7, -1, -1)]


def bytes_to_bits(data: bytes) -> list[int]:
    bits: list[int] = []
    for byte in data:
        bits.extend(byte_to_bits(byte))
    return bits


def bits_to_bytes(bits: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        value = 0
        for j in range(8):
            value = (value << 1) | bits[i + j]
        out.append(value)
    return bytes(out)


# ---------------------------------------------------------------------------
# Output directories
# ---------------------------------------------------------------------------
def get_output_dirs(base_dir: str = None) -> tuple[str, str]:
    """Return (sound_dir, image_dir), creating them if needed."""
    if base_dir is None:
        base_dir = os.getcwd()
    sound_dir = os.path.join(base_dir, SOUND_DIR_NAME)
    image_dir = os.path.join(base_dir, IMAGE_DIR_NAME)
    os.makedirs(sound_dir, exist_ok=True)
    os.makedirs(image_dir, exist_ok=True)
    return sound_dir, image_dir


# ---------------------------------------------------------------------------
# Encode: image -> bits -> audio
# ---------------------------------------------------------------------------
@dataclass
class EncodedResult:
    audio: np.ndarray
    width: int
    height: int
    payload_bytes: int


def image_to_payload(image_path: str, max_size: int = 48, mode: int = MODE_GRAYSCALE) -> tuple[bytes, int, int]:
    if mode == MODE_COLOR:
        img = Image.open(image_path).convert("RGB")
    else:
        img = Image.open(image_path).convert("L")
    if max_size and max_size > 0:
        img.thumbnail((max_size, max_size))
    width, height = img.size
    pixels = img.tobytes()

    if mode == MODE_COLOR:
        pixel_size = width * height * 3
    else:
        pixel_size = width * height

    body = MAGIC + bytes([mode]) + struct.pack(">HH", width, height) + pixels
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + struct.pack(">I", crc), width, height


def frame_payload(payload: bytes) -> bytearray:
    framed = bytearray()
    framed += bytes([START_MARKER]) * START_MARKER_REPEATS
    for i in range(0, len(payload), RESYNC_INTERVAL):
        framed += bytes([SYNC_BYTE])
        framed += payload[i:i + RESYNC_INTERVAL]
    return framed


def bits_to_audio(bits: list[int], cancel_event=None, samples_per_bit: int = None,
                   freq_0: int = None, freq_1: int = None) -> np.ndarray:
    samples_per_bit = SAMPLES_PER_BIT if samples_per_bit is None else samples_per_bit
    freq_0 = FREQ_0 if freq_0 is None else freq_0
    freq_1 = FREQ_1 if freq_1 is None else freq_1
    n = len(bits)
    audio = np.zeros(n * samples_per_bit, dtype=np.float32)
    phase = 0.0
    t_bit = np.arange(samples_per_bit) / SAMPLE_RATE
    for idx, bit in enumerate(bits):
        if idx % 2000 == 0:
            _check_cancel(cancel_event)
        freq = freq_1 if bit else freq_0
        segment = AMPLITUDE * np.sin(2 * np.pi * freq * t_bit + phase)
        audio[idx * samples_per_bit:(idx + 1) * samples_per_bit] = segment
        phase = (phase + 2 * np.pi * freq * samples_per_bit / SAMPLE_RATE) % (2 * np.pi)
    return audio


def encode_image(image_path: str, max_size: int = 48, silence_seconds: float = 0.5,
                  mode: int = MODE_GRAYSCALE, cancel_event=None,
                  baud: int = None, freq_0: int = None, freq_1: int = None) -> EncodedResult:
    """Encode an image to audio. baud/freq_0/freq_1 default to the module
    constants but can be overridden per call, this is how the GUI lets
    each tab use an independent speed without shared mutable state."""
    baud = BAUD if baud is None else baud
    freq_0 = FREQ_0 if freq_0 is None else freq_0
    freq_1 = FREQ_1 if freq_1 is None else freq_1
    samples_per_bit = max(1, int(SAMPLE_RATE / baud))

    payload, width, height = image_to_payload(image_path, max_size, mode)
    _check_cancel(cancel_event)
    framed = frame_payload(payload)
    preamble = [0, 1] * (PREAMBLE_BITS // 2)
    bits = preamble + bytes_to_bits(bytes(framed))

    audio = bits_to_audio(bits, cancel_event=cancel_event, samples_per_bit=samples_per_bit,
                           freq_0=freq_0, freq_1=freq_1)
    pad = np.zeros(int(SAMPLE_RATE * silence_seconds), dtype=np.float32)
    audio = np.concatenate([pad, audio, pad])

    return EncodedResult(audio=audio, width=width, height=height, payload_bytes=len(payload))


def save_wav(path: str, audio: np.ndarray) -> None:
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


# ---------------------------------------------------------------------------
# Decode: audio -> bits -> image
# ---------------------------------------------------------------------------
def goertzel_power(samples: np.ndarray, freq: float) -> float:
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(0.5 + n * freq / SAMPLE_RATE)
    w = 2 * np.pi * k / n
    coeff = 2 * np.cos(w)
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 ** 2 + s_prev ** 2 - coeff * s_prev * s_prev2


def bit_at(audio: np.ndarray, start_sample: int, samples_per_bit: int = None,
           freq_0: int = None, freq_1: int = None):
    samples_per_bit = SAMPLES_PER_BIT if samples_per_bit is None else samples_per_bit
    freq_0 = FREQ_0 if freq_0 is None else freq_0
    freq_1 = FREQ_1 if freq_1 is None else freq_1
    seg = audio[start_sample:start_sample + samples_per_bit]
    if len(seg) < samples_per_bit:
        return None
    p0 = goertzel_power(seg, freq_0)
    p1 = goertzel_power(seg, freq_1)
    return 1 if p1 > p0 else 0


def load_wav_mono(path: str) -> np.ndarray:
    with wave.open(path, "r") as wf:
        n_frames = wf.getnframes()
        sample_rate = wf.getframerate()
        sample_width = wf.getsampwidth()
        channels = wf.getnchannels()
        raw = wf.readframes(n_frames)

    if sample_width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise ValueError("use a WAV of 8 or 16 bits per sample")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)

    if sample_rate != SAMPLE_RATE:
        duration = len(data) / sample_rate
        new_len = int(duration * SAMPLE_RATE)
        old_idx = np.linspace(0, len(data) - 1, num=len(data))
        new_idx = np.linspace(0, len(data) - 1, num=new_len)
        data = np.interp(new_idx, old_idx, data)

    return data


def find_signal_start(audio: np.ndarray) -> int:
    window = int(SAMPLE_RATE * 0.05)
    energies = []
    for i in range(0, len(audio) - window, window):
        segment = audio[i:i + window]
        energies.append(np.sqrt(np.mean(segment ** 2)))
    energies = np.array(energies)
    if len(energies) == 0:
        return 0

    n_noise_windows = max(1, min(5, len(energies) // 4))
    noise_floor = np.median(energies[:n_noise_windows])
    threshold = max(noise_floor * 4, 0.02)

    for i, energy in enumerate(energies):
        if energy > threshold:
            return max(0, i * window - window)
    return 0


def _search_pattern(audio: np.ndarray, center: int, expected_bits: list[int], search_samples: int,
                     samples_per_bit: int = None, freq_0: int = None, freq_1: int = None):
    samples_per_bit = SAMPLES_PER_BIT if samples_per_bit is None else samples_per_bit
    step = max(1, samples_per_bit // 10)
    best_pos, best_score = center, -1
    for offset in range(-search_samples, search_samples, step):
        pos = center + offset
        if pos < 0:
            continue
        bits, p, ok = [], pos, True
        for _ in expected_bits:
            b = bit_at(audio, p, samples_per_bit=samples_per_bit, freq_0=freq_0, freq_1=freq_1)
            if b is None:
                ok = False
                break
            bits.append(b)
            p += samples_per_bit
        if not ok:
            continue
        score = sum(1 for a, e in zip(bits, expected_bits) if a == e)
        if score > best_score:
            best_score, best_pos = score, pos
    return best_pos, best_score, len(expected_bits)


@dataclass
class DecodedResult:
    image: Image.Image | None
    width: int
    height: int
    crc_ok: bool
    marker_score: tuple[int, int]
    log: list[str]


def decode_audio(path: str, cancel_event=None, baud: int = None,
                  freq_0: int = None, freq_1: int = None) -> DecodedResult:
    audio = load_wav_mono(path)
    return decode_audio_array(audio, cancel_event=cancel_event, baud=baud, freq_0=freq_0, freq_1=freq_1)


def decode_audio_array(audio: np.ndarray, cancel_event=None, baud: int = None,
                        freq_0: int = None, freq_1: int = None) -> DecodedResult:
    """Decode audio back to an image. baud/freq_0/freq_1 default to the
    module constants but can be overridden per call, must match whatever
    speed the audio was encoded with."""
    baud = BAUD if baud is None else baud
    freq_0 = FREQ_0 if freq_0 is None else freq_0
    freq_1 = FREQ_1 if freq_1 is None else freq_1
    samples_per_bit = max(1, int(SAMPLE_RATE / baud))

    log: list[str] = []
    _check_cancel(cancel_event)
    rough_start = find_signal_start(audio)

    marker_bits = byte_to_bits(START_MARKER) * START_MARKER_REPEATS
    expected_marker_pos = rough_start + PREAMBLE_BITS * samples_per_bit
    marker_pos, marker_score, marker_total = _search_pattern(
        audio, expected_marker_pos, marker_bits, samples_per_bit * 40,
        samples_per_bit=samples_per_bit, freq_0=freq_0, freq_1=freq_1,
    )
    log.append(f"start marker: {marker_score}/{marker_total} bits correct")
    if marker_score < marker_total * 0.7:
        log.append("warning: low score - signal may be weak or too noisy")

    pos = marker_pos + samples_per_bit * 8 * START_MARKER_REPEATS
    sync_bits = byte_to_bits(SYNC_BYTE)

    collected = bytearray()
    needed = None
    width = height = 0
    mode = MODE_GRAYSCALE

    while True:
        _check_cancel(cancel_event)
        pos, _score, _ = _search_pattern(audio, pos, sync_bits, samples_per_bit // 2,
                                          samples_per_bit=samples_per_bit, freq_0=freq_0, freq_1=freq_1)
        pos += samples_per_bit * 8

        chunk_bits = []
        for _ in range(8 * RESYNC_INTERVAL):
            b = bit_at(audio, pos, samples_per_bit=samples_per_bit, freq_0=freq_0, freq_1=freq_1)
            if b is None:
                break
            chunk_bits.append(b)
            pos += samples_per_bit
        chunk_bytes = bits_to_bytes(chunk_bits)
        collected += chunk_bytes

        if needed is None and len(collected) >= 9:
            mode = collected[4]
            width, height = struct.unpack(">HH", collected[5:9])
            if width == 0 or height == 0 or width * height > 2_000_000:
                log.append("invalid header - sync failed")
                return DecodedResult(None, 0, 0, False, (marker_score, marker_total), log)
            pixel_size = width * height * (3 if mode == MODE_COLOR else 1)
            needed = 9 + pixel_size + 4

        if (needed is not None and len(collected) >= needed) or len(chunk_bytes) < RESYNC_INTERVAL:
            break

    if needed is None or len(collected) < 9:
        log.append("could not decode header - signal weak or too corrupted")
        return DecodedResult(None, 0, 0, False, (marker_score, marker_total), log)

    payload = bytes(collected[:needed])
    magic = payload[0:4]
    mode = payload[4]
    width, height = struct.unpack(">HH", payload[5:9])
    pixel_size = width * height * (3 if mode == MODE_COLOR else 1)
    pixels = payload[9:9 + pixel_size]

    body_for_crc = payload[:-4]
    crc_ok = False
    if len(payload) >= 9 + pixel_size + 4:
        crc_received = struct.unpack(">I", payload[9 + pixel_size:9 + pixel_size + 4])[0]
        crc_calc = zlib.crc32(body_for_crc) & 0xFFFFFFFF
        crc_ok = crc_calc == crc_received
        log.append("CRC ok: image received without errors" if crc_ok else "warning: CRC mismatch - noise may be present")

    if magic != MAGIC:
        log.append("warning: header does not match expected")

    if len(pixels) < pixel_size:
        pixels = pixels + b"\x00" * (pixel_size - len(pixels))

    image_mode = "RGB" if mode == MODE_COLOR else "L"
    image = Image.frombytes(image_mode, (width, height), bytes(pixels))
    return DecodedResult(image, width, height, crc_ok, (marker_score, marker_total), log)


# ---------------------------------------------------------------------------
# Audio playback
# ---------------------------------------------------------------------------
def play_audio(audio: np.ndarray) -> None:
    try:
        import sounddevice as sd
    except ImportError:
        raise RuntimeError("sounddevice is not installed")
    sd.stop()
    sd.play(audio, SAMPLE_RATE)
    sd.wait()
