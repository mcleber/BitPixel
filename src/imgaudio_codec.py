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

# Number of trailing preamble bits included in the frame search pattern.
PREAMBLE_TAIL_BITS = 16
# Minimum normalized correlation accepted as a valid frame detection.
MIN_FRAME_CONFIDENCE = 0.35


def set_baud(new_baud: int) -> None:
    """Set the module-level default baud rate."""
    global BAUD, SAMPLES_PER_BIT
    if new_baud <= 0:
        raise ValueError("baud must be positive")
    BAUD = int(new_baud)
    SAMPLES_PER_BIT = max(1, int(SAMPLE_RATE / BAUD))


def set_tones(freq_0: int, freq_1: int) -> None:
    """Set the module-level default FSK tone pair in Hz."""
    global FREQ_0, FREQ_1
    FREQ_0 = int(freq_0)
    FREQ_1 = int(freq_1)


# Speed presets: label -> (baud, freq_0, freq_1).
SPEED_PRESETS = {
    "Normal (300 baud)": (300, 1200, 2200),
    "Fast (600 baud)": (600, 1000, 3000),
}


class OperationCancelled(Exception):
    """Raised when an encode or decode operation is cancelled."""


def _check_cancel(cancel_event) -> None:
    """Raise OperationCancelled if the cancel event is set."""
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled()


# ---------------------------------------------------------------------------
# Bit utilities
# ---------------------------------------------------------------------------
def byte_to_bits(value: int) -> list[int]:
    """Convert a byte to a list of 8 bits, MSB first."""
    return [(value >> i) & 1 for i in range(7, -1, -1)]


def bytes_to_bits(data: bytes) -> list[int]:
    """Convert a byte sequence to a flat list of bits, MSB first."""
    bits: list[int] = []
    for byte in data:
        bits.extend(byte_to_bits(byte))
    return bits


def bits_to_bytes(bits: list[int]) -> bytes:
    """Pack a list of bits, MSB first, into bytes. Incomplete trailing bytes are dropped."""
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        value = 0
        for j in range(8):
            value = (value << 1) | int(bits[i + j])
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
    """Result of an encode operation."""
    audio: np.ndarray
    width: int
    height: int
    payload_bytes: int


def image_to_payload(image_path: str, max_size: int = 48, mode: int = MODE_GRAYSCALE) -> tuple[bytes, int, int]:
    """Load and resize an image and build the payload with header and CRC32."""
    if mode == MODE_COLOR:
        img = Image.open(image_path).convert("RGB")
    else:
        img = Image.open(image_path).convert("L")
    if max_size and max_size > 0:
        img.thumbnail((max_size, max_size))
    width, height = img.size
    pixels = img.tobytes()

    body = MAGIC + bytes([mode]) + struct.pack(">HH", width, height) + pixels
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + struct.pack(">I", crc), width, height


def frame_payload(payload: bytes) -> bytearray:
    """Prepend the start marker and insert a sync byte before every payload block."""
    framed = bytearray()
    framed += bytes([START_MARKER]) * START_MARKER_REPEATS
    for i in range(0, len(payload), RESYNC_INTERVAL):
        framed += bytes([SYNC_BYTE])
        framed += payload[i:i + RESYNC_INTERVAL]
    return framed


def bits_to_audio(bits: list[int], cancel_event=None, samples_per_bit: int = None,
                   freq_0: int = None, freq_1: int = None) -> np.ndarray:
    """Modulate a bit list into a phase-continuous FSK signal."""
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
    """Encode an image file into an FSK audio signal.

    baud, freq_0 and freq_1 override the module defaults for this call only.
    """
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
    """Save a float audio array as a 16-bit mono WAV file."""
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
def load_wav_mono(path: str) -> np.ndarray:
    """Load a WAV file as a mono float array resampled to SAMPLE_RATE."""
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

    return data.astype(np.float32)


def _tone_discriminator(audio: np.ndarray, samples_per_bit: int, freq_0: float, freq_1: float,
                         cancel_event=None, block: int = 1 << 20) -> np.ndarray:
    """Return a soft bit value in [-1, 1] for a one-bit window starting at each sample.

    Positive values indicate freq_1 and negative values indicate freq_0. The value
    is normalized by the total tone energy, so it does not depend on signal level.
    """
    x = np.asarray(audio, dtype=np.float64)
    x = x - np.mean(x)
    n_out = len(x) - samples_per_bit + 1
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)

    out = np.empty(n_out, dtype=np.float32)
    w0 = 2 * np.pi * freq_0 / SAMPLE_RATE
    w1 = 2 * np.pi * freq_1 / SAMPLE_RATE
    floor = 1e-9 * samples_per_bit

    for start in range(0, n_out, block):
        _check_cancel(cancel_event)
        stop = min(start + block, n_out)
        seg = x[start:stop + samples_per_bit - 1]
        k = np.arange(start, stop + samples_per_bit - 1)
        powers = []
        for w in (w0, w1):
            acc = np.concatenate(([0j], np.cumsum(seg * np.exp(-1j * w * k))))
            window_sum = acc[samples_per_bit:] - acc[:-samples_per_bit]
            powers.append(window_sum.real ** 2 + window_sum.imag ** 2)
        p0, p1 = powers
        out[start:stop] = (p1 - p0) / (p1 + p0 + floor)
    return out


def _pattern_score(soft: np.ndarray, positions: np.ndarray, pattern: np.ndarray,
                    samples_per_bit: int) -> np.ndarray:
    """Correlate a +/-1 bit pattern with the soft bit stream at the given positions."""
    score = np.zeros(len(positions), dtype=np.float32)
    for i, expected in enumerate(pattern):
        score += expected * soft[positions + i * samples_per_bit]
    return score


def _find_frame(soft: np.ndarray, pattern_bits: list[int], samples_per_bit: int,
                 cancel_event=None) -> tuple[int, float]:
    """Search the whole recording for the known frame pattern.

    Returns the sample position of the pattern start and its normalized
    correlation (1.0 means a perfect match).
    """
    pattern = np.array([1.0 if b else -1.0 for b in pattern_bits], dtype=np.float32)
    n_positions = len(soft) - (len(pattern) - 1) * samples_per_bit
    if n_positions <= 0:
        return 0, 0.0

    step = max(1, samples_per_bit // 8)
    coarse = np.arange(0, n_positions, step)
    best, best_score = 0, -np.inf
    chunk = 1 << 18
    for i in range(0, len(coarse), chunk):
        _check_cancel(cancel_event)
        positions = coarse[i:i + chunk]
        scores = _pattern_score(soft, positions, pattern, samples_per_bit)
        j = int(np.argmax(scores))
        if scores[j] > best_score:
            best_score, best = float(scores[j]), int(positions[j])

    fine = np.arange(max(0, best - step), min(n_positions, best + step + 1))
    scores = _pattern_score(soft, fine, pattern, samples_per_bit)
    j = int(np.argmax(scores))
    return int(fine[j]), float(scores[j]) / len(pattern)


def _align_sync(soft: np.ndarray, center: int, samples_per_bit: int) -> int:
    """Refine the sync byte position within half a bit of the expected position."""
    pattern = np.array([1.0 if b else -1.0 for b in byte_to_bits(SYNC_BYTE)], dtype=np.float32)
    last_valid = len(soft) - 1 - (len(pattern) - 1) * samples_per_bit
    radius = samples_per_bit // 2
    lo, hi = max(0, center - radius), min(last_valid, center + radius)
    if hi < lo:
        return center
    positions = np.arange(lo, hi + 1)
    scores = _pattern_score(soft, positions, pattern, samples_per_bit)
    return int(positions[int(np.argmax(scores))])


def _read_bits(soft: np.ndarray, start: int, count: int, samples_per_bit: int) -> list[int]:
    """Read hard bit decisions, stopping at the end of the recording."""
    idx = start + np.arange(count) * samples_per_bit
    idx = idx[idx < len(soft)]
    return (soft[idx] > 0).astype(np.uint8).tolist()


@dataclass
class DecodedResult:
    """Result of a decode operation."""
    image: Image.Image | None
    width: int
    height: int
    crc_ok: bool
    marker_score: tuple[int, int]
    log: list[str]


def decode_audio(path: str, cancel_event=None, baud: int = None,
                  freq_0: int = None, freq_1: int = None) -> DecodedResult:
    """Decode a WAV file back into an image."""
    audio = load_wav_mono(path)
    return decode_audio_array(audio, cancel_event=cancel_event, baud=baud, freq_0=freq_0, freq_1=freq_1)


def decode_audio_array(audio: np.ndarray, cancel_event=None, baud: int = None,
                        freq_0: int = None, freq_1: int = None) -> DecodedResult:
    """Decode an audio array back into an image.

    baud, freq_0 and freq_1 must match the values used for encoding.
    """
    baud = BAUD if baud is None else baud
    freq_0 = FREQ_0 if freq_0 is None else freq_0
    freq_1 = FREQ_1 if freq_1 is None else freq_1
    spb = max(1, int(SAMPLE_RATE / baud))

    log: list[str] = []
    _check_cancel(cancel_event)
    soft = _tone_discriminator(audio, spb, freq_0, freq_1, cancel_event=cancel_event)

    marker_bits = byte_to_bits(START_MARKER) * START_MARKER_REPEATS
    marker_total = len(marker_bits)
    preamble_tail = ([0, 1] * (PREAMBLE_BITS // 2))[-PREAMBLE_TAIL_BITS:]
    search_bits = preamble_tail + marker_bits + byte_to_bits(SYNC_BYTE) + bytes_to_bits(MAGIC)

    frame_pos, confidence = _find_frame(soft, search_bits, spb, cancel_event=cancel_event)
    marker_pos = frame_pos + PREAMBLE_TAIL_BITS * spb

    received = _read_bits(soft, marker_pos, marker_total, spb)
    marker_score = sum(1 for a, e in zip(received, marker_bits) if a == e)

    if confidence < MIN_FRAME_CONFIDENCE:
        log.append(f"no valid signal found (best match {confidence:.0%})")
        log.append("check the Speed setting, playback volume and recording length")
        return DecodedResult(None, 0, 0, False, (marker_score, marker_total), log)

    log.append(f"signal found at {marker_pos / SAMPLE_RATE:.2f}s (match {confidence:.0%})")
    log.append(f"start marker: {marker_score}/{marker_total} bits correct")
    if marker_score < marker_total * 0.7:
        log.append("warning: low score - signal may be weak or too noisy")

    pos = marker_pos + marker_total * spb
    collected = bytearray()
    needed = None
    width = height = 0

    while True:
        _check_cancel(cancel_event)
        pos = _align_sync(soft, pos, spb)
        pos += 8 * spb

        chunk_bits = _read_bits(soft, pos, 8 * RESYNC_INTERVAL, spb)
        pos += len(chunk_bits) * spb
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

    if len(collected) < needed:
        log.append(f"warning: recording ended early ({len(collected)}/{needed} bytes received)")

    payload = bytes(collected[:needed])
    magic = payload[0:4]
    mode = payload[4]
    width, height = struct.unpack(">HH", payload[5:9])
    pixel_size = width * height * (3 if mode == MODE_COLOR else 1)
    pixels = payload[9:9 + pixel_size]

    crc_ok = False
    if len(payload) >= 9 + pixel_size + 4:
        crc_received = struct.unpack(">I", payload[9 + pixel_size:9 + pixel_size + 4])[0]
        crc_calc = zlib.crc32(payload[:9 + pixel_size]) & 0xFFFFFFFF
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
    """Play an audio array and block until playback ends."""
    try:
        import sounddevice as sd
    except ImportError:
        raise RuntimeError("sounddevice is not installed")
    sd.stop()
    sd.play(audio, SAMPLE_RATE)
    sd.wait()
