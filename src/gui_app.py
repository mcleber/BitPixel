"""
gui_app.py - graphical interface (Tkinter) for the image <-> audio FSK codec.

Features:
  - Encode image to audio (color/grayscale, original size)
  - Decode audio to image (file or microphone)
  - PLAY, PAUSE, STOP with live volume control and click-to-seek
  - Waveform playback position indicator
  - Cancel button for active conversions
  - Auto-creates output_sound/ and output_image_recovered/ next to src/
  - English only
  - EXIT, Help, About buttons

Usage:
    python gui_app.py
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from PIL import Image, ImageTk

import imgaudio_codec as codec
from imgaudio_codec import MODE_GRAYSCALE, MODE_COLOR, OperationCancelled

try:
    import sounddevice as sd
    HAS_MIC = True
    HAS_SOUNDDEVICE = True
except Exception:
    HAS_MIC = False
    HAS_SOUNDDEVICE = False

BG = "#1e1f26"
BG_PANEL = "#262832"
FG = "#e7e7ec"
FG_MUTED = "#9a9ba8"
ACCENT = "#5ad1c6"
ACCENT_DIM = "#3a7a74"
DANGER = "#e07a7a"
WARNING = "#e0c37a"
FONT_FAMILY = "Helvetica"

SAMPLE_RATE = codec.SAMPLE_RATE

# The project root is one directory above src/, so output folders are
# always created next to src/ - never inside it - no matter where the
# app is launched from.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Themed message dialog (replaces tk.messagebox: no OS emoji/icons, dark
# background, light text, OK button in the app's accent color).
# ---------------------------------------------------------------------------
def show_message(parent, title: str, message: str, kind: str = "info"):
    accent = {"info": ACCENT, "warning": WARNING, "error": DANGER}.get(kind, ACCENT)

    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.configure(bg=BG_PANEL)
    dialog.transient(parent)
    dialog.resizable(False, False)

    frame = ttk.Frame(dialog, style="Panel.TFrame", padding=(24, 20))
    frame.pack(fill="both", expand=True)

    header = tk.Label(frame, text=title, bg=BG_PANEL, fg=accent,
                       font=(FONT_FAMILY, 12, "bold"), justify="left")
    header.pack(anchor="w")

    body = tk.Label(frame, text=message, bg=BG_PANEL, fg=FG, font=(FONT_FAMILY, 10),
                     justify="left", wraplength=360)
    body.pack(anchor="w", pady=(10, 20))

    btn_row = ttk.Frame(frame, style="Panel.TFrame")
    btn_row.pack(anchor="e")
    ttk.Button(btn_row, text="OK", command=dialog.destroy,
               style="Accent.TButton").pack()

    dialog.update_idletasks()
    width = max(320, dialog.winfo_reqwidth())
    height = max(140, dialog.winfo_reqheight())
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        x = px + max(0, (pw - width) // 2)
        y = py + max(0, (ph - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
    except Exception:
        dialog.geometry(f"{width}x{height}")

    dialog.grab_set()
    dialog.focus_set()
    dialog.wait_window()


class PlaybackState:
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


class WaveformPanel(ttk.Frame):
    """Displays a waveform. If on_seek is given, clicking the waveform
    reports the clicked time (in seconds) so playback can jump there."""

    def __init__(self, parent, on_seek=None):
        super().__init__(parent, style="Panel.TFrame")
        self.on_seek = on_seek
        self._duration = 0.0
        self.figure = Figure(figsize=(5, 1.8), dpi=100, facecolor=BG_PANEL)
        self.axes = self.figure.add_subplot(111)
        self._style_axes()
        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        if self.on_seek is not None:
            self.canvas.mpl_connect("button_press_event", self._on_click)

    def _on_click(self, event):
        if event.xdata is None or self._duration <= 0:
            return
        t = max(0.0, min(float(event.xdata), self._duration))
        self.on_seek(t)

    def _style_axes(self):
        self.axes.clear()
        self.axes.set_facecolor(BG_PANEL)
        for spine in self.axes.spines.values():
            spine.set_color("#3a3c48")
        self.axes.tick_params(colors=FG_MUTED, labelsize=8)
        self.axes.set_xlabel("seconds", color=FG_MUTED, fontsize=8)

    def plot(self, audio: np.ndarray, sample_rate: int, color: str = ACCENT, play_pos: float = -1):
        self._style_axes()
        if audio is not None and len(audio) > 0:
            self._duration = len(audio) / sample_rate
            max_points = 6000
            step = max(1, len(audio) // max_points)
            samples = audio[::step]
            t = np.arange(len(samples)) * step / sample_rate
            self.axes.plot(t, samples, color=color, linewidth=0.6)
            self.axes.set_xlim(0, self._duration)
            self.axes.set_ylim(-1.05, 1.05)
            if play_pos >= 0:
                play_time = play_pos / sample_rate
                if play_time <= self.axes.get_xlim()[1]:
                    self.axes.axvline(x=play_time, color="#ff6b6b", linewidth=1.5, linestyle="--")
        else:
            self._duration = 0.0
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def clear(self):
        self._duration = 0.0
        self._style_axes()
        self.figure.tight_layout()
        self.canvas.draw_idle()


class _PlayHandle:
    """Audio playback engine built on a sounddevice.OutputStream callback.

    Using a live callback (instead of one-shot sd.play() calls) is what
    makes pause/resume continue from the exact sample instead of
    restarting, lets the volume slider affect sound that is already
    playing, and lets seeking jump to an arbitrary position.
    """

    def __init__(self):
        self.audio: np.ndarray | None = None
        self.volume = 1.0
        self.position = 0
        self.state = PlaybackState.IDLE
        self._lock = threading.Lock()
        self._stream = None

    def load(self, audio: np.ndarray):
        self.stop()
        with self._lock:
            self.audio = audio
            self.position = 0
            self.state = PlaybackState.IDLE

    def _callback(self, outdata, frames, time_info, status):
        with self._lock:
            audio = self.audio
            start = self.position
            vol = self.volume
        if audio is None:
            outdata[:] = 0
            raise sd.CallbackStop()
        end = start + frames
        chunk = audio[start:end]
        n = len(chunk)
        if n < frames:
            outdata[:n, 0] = chunk * vol
            outdata[n:, 0] = 0
            with self._lock:
                self.position = len(audio)
                self.state = PlaybackState.STOPPED
            raise sd.CallbackStop()
        outdata[:, 0] = chunk * vol
        with self._lock:
            self.position = end

    def _open_stream(self):
        self._close_stream()
        self._stream = sd.OutputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def play(self):
        """Start (or resume) playback from the current position."""
        with self._lock:
            if self.audio is None or len(self.audio) == 0:
                return
            if self.position >= len(self.audio):
                self.position = 0
            self.state = PlaybackState.PLAYING
        self._open_stream()

    def pause(self):
        with self._lock:
            if self.state != PlaybackState.PLAYING:
                return
            self.state = PlaybackState.PAUSED
        self._close_stream()

    def resume(self):
        if self.state == PlaybackState.PAUSED:
            self.play()

    def stop(self):
        self._close_stream()
        with self._lock:
            self.position = 0
            self.state = PlaybackState.STOPPED if self.audio is not None else PlaybackState.IDLE

    def seek(self, position: int):
        with self._lock:
            if self.audio is None:
                return
            position = max(0, min(int(position), len(self.audio)))
            self.position = position
            was_playing = self.state == PlaybackState.PLAYING
        if was_playing:
            self._open_stream()

    def set_volume(self, volume: float):
        with self._lock:
            self.volume = max(0.0, min(volume, 2.0))

    @property
    def elapsed(self):
        with self._lock:
            return self.position


_play_handle = _PlayHandle()


def _play_audio_array(audio: np.ndarray):
    _play_handle.load(audio)


def _play_play():
    _play_handle.play()


def _play_pause():
    if _play_handle.state == PlaybackState.PLAYING:
        _play_handle.pause()
    elif _play_handle.state == PlaybackState.PAUSED:
        _play_handle.resume()


def _play_stop():
    _play_handle.stop()


def _play_seek(position: int):
    _play_handle.seek(position)


def _set_volume(volume: float):
    _play_handle.set_volume(volume)


class EncodeTab(ttk.Frame):
    def __init__(self, parent, S: dict):
        super().__init__(parent, style="Panel.TFrame", padding=16)
        self.S = S
        self.speed_var = tk.StringVar(value="Normal (300 baud)")
        self.image_path: str | None = None
        self.audio_result: codec.EncodedResult | None = None
        self.wav_path: str | None = None
        self.mode = MODE_GRAYSCALE
        self._cancel_event = threading.Event()
        self._encode_thread: threading.Thread | None = None
        self._build_layout()

    def _build_layout(self):
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        left = ttk.Frame(self, style="Panel.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = ttk.Frame(self, style="Panel.TFrame")
        right.grid(row=0, column=1, sticky="nsew")

        ttk.Label(left, text=self.S["choose_image"], style="Heading.TLabel").pack(anchor="w")
        self.path_label = ttk.Label(left, text=self.S["no_image"], style="Muted.TLabel")
        self.path_label.pack(anchor="w", pady=(2, 8))
        ttk.Button(left, text=self.S["select_image"], command=self._pick_image,
                    style="Accent.TButton").pack(anchor="w")

        self.preview_label = tk.Label(left, bg=BG_PANEL, bd=0)
        self.preview_label.pack(anchor="w", pady=12)

        ttk.Label(left, text=self.S["max_size_label"], style="Heading.TLabel").pack(
            anchor="w", pady=(8, 2)
        )
        self.size_var = tk.IntVar(value=48)
        self.size_var.trace_add("write", lambda *_: self._sync_size_label())
        size_row = ttk.Frame(left, style="Panel.TFrame")
        size_row.pack(anchor="w", fill="x")
        self.size_scale = ttk.Scale(size_row, from_=0, to=96, variable=self.size_var,
                                     orient="horizontal", length=220,
                                     command=lambda _v: self._sync_size_label())
        self.size_scale.pack(side="left")
        self.size_value_label = ttk.Label(size_row, text="48 px", style="Muted.TLabel")
        self.size_value_label.pack(side="left", padx=8)

        ttk.Button(left, text=self.S["original_size"], command=self._original_size,
                    style="Secondary.TButton").pack(anchor="w", pady=(4, 8))

        ttk.Label(left, text=self.S["color_mode"], style="Heading.TLabel").pack(anchor="w", pady=(8, 2))
        mode_frame = ttk.Frame(left, style="Panel.TFrame")
        mode_frame.pack(anchor="w")
        self.mode_var = tk.StringVar(value="grayscale")
        ttk.Radiobutton(mode_frame, text=self.S["grayscale"], variable=self.mode_var,
                         value="grayscale", command=self._on_mode_change).pack(side="left", padx=(0, 8))
        ttk.Radiobutton(mode_frame, text=self.S["color"], variable=self.mode_var,
                         value="color", command=self._on_mode_change).pack(side="left")

        ttk.Label(left, text=self.S["speed_label"], style="Heading.TLabel").pack(anchor="w", pady=(8, 2))
        speed_row = ttk.Frame(left, style="Panel.TFrame")
        speed_row.pack(anchor="w")
        speed_combo = ttk.Combobox(
            speed_row, textvariable=self.speed_var, state="readonly", width=18,
            values=list(codec.SPEED_PRESETS.keys()),
        )
        speed_combo.pack(side="left")
        ttk.Label(left, text=self.S["speed_hint"], style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

        ttk.Label(
            left, text=self.S["info_text"], style="Muted.TLabel", justify="left",
        ).pack(anchor="w", pady=(8, 8))

        self.generate_btn = ttk.Button(left, text=self.S["generate_audio"], command=self._generate,
                                        style="Accent.TButton")
        self.generate_btn.pack(anchor="w")

        self.cancel_btn = ttk.Button(left, text=self.S["cancel"], command=self._cancel,
                                      style="Secondary.TButton", state="disabled")
        self.cancel_btn.pack(anchor="w", pady=(4, 0))

        self.status_label = ttk.Label(left, text="", style="Muted.TLabel")
        self.status_label.pack(anchor="w", pady=(8, 0))

        self.info_label = ttk.Label(left, text="", style="Muted.TLabel", justify="left")
        self.info_label.pack(anchor="w", pady=(4, 0))

        ttk.Label(right, text=self.S["waveform_label_decode"], style="Heading.TLabel").pack(anchor="w")
        self.waveform = WaveformPanel(right, on_seek=self._on_waveform_seek)
        self.waveform.pack(fill="both", expand=True, pady=(4, 12))

        action_row = ttk.Frame(right, style="Panel.TFrame")
        action_row.pack(anchor="w")
        self.play_btn = ttk.Button(action_row, text=self.S["play"], command=self._play_play,
                                       style="Accent.TButton", state="disabled")
        self.play_btn.pack(side="left")
        self.pause_btn = ttk.Button(action_row, text=self.S["pause"], command=self._play_pause_gui,
                                       style="Secondary.TButton", state="disabled")
        self.pause_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(action_row, text=self.S["stop"], command=self._play_stop_gui,
                                       style="Secondary.TButton", state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        vol_row = ttk.Frame(right, style="Panel.TFrame")
        vol_row.pack(anchor="w", pady=(8, 0))
        ttk.Label(vol_row, text=self.S["volume"], style="Muted.TLabel").pack(side="left")
        self.volume_var = tk.DoubleVar(value=1.0)
        self.volume_var.trace_add("write", lambda *_: self._on_volume_change())
        self.vol_scale = ttk.Scale(vol_row, from_=0.0, to=2.0, variable=self.volume_var,
                                    orient="horizontal", length=150)
        self.vol_scale.pack(side="left", padx=8)
        self.vol_label = ttk.Label(vol_row, text="100%", style="Muted.TLabel")
        self.vol_label.pack(side="left")

        self.save_button = ttk.Button(action_row, text=self.S["save_wav"], command=self._save_as,
                                       style="Secondary.TButton", state="disabled")
        self.save_button.pack(side="left", padx=(8, 0))

    def _sync_size_label(self):
        self.size_value_label.config(text=f"{int(self.size_var.get())} px")

    def _original_size(self):
        self.size_var.set(0)
        self._sync_size_label()

    def _on_mode_change(self):
        self.mode = MODE_COLOR if self.mode_var.get() == "color" else MODE_GRAYSCALE

    def _on_volume_change(self):
        vol = self.volume_var.get()
        _set_volume(vol)
        self.vol_label.config(text=f"{int(vol*100)}%")

    def _pick_image(self):
        path = filedialog.askopenfilename(
            title="Choose an image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif"), ("All files", "*.*")],
        )
        if not path:
            return
        self.image_path = path
        self.path_label.config(text=os.path.basename(path))
        img = Image.open(path).convert("RGB")
        img.thumbnail((160, 160))
        photo = ImageTk.PhotoImage(img)
        self.preview_label.configure(image=photo)
        self.preview_label.image = photo

    def _generate(self):
        if not self.image_path:
            show_message(self.winfo_toplevel(), "No image", "Select an image first.", kind="warning")
            return
        _play_stop()
        self.play_btn.config(state="disabled")
        self.pause_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.save_button.config(state="disabled")
        self._cancel_event.clear()
        self.generate_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.status_label.config(text="Generating audio...", foreground=FG_MUTED)
        self.update_idletasks()

        def worker():
            try:
                size_val = self.size_var.get()
                max_size = size_val if size_val > 0 else None
                baud, freq0, freq1 = codec.SPEED_PRESETS[self.speed_var.get()]
                result = codec.encode_image(
                    self.image_path, max_size=max_size, mode=self.mode,
                    cancel_event=self._cancel_event,
                    baud=baud, freq_0=freq0, freq_1=freq1,
                )
                self.audio_result = result
                self.after(0, self._on_generate_done, result)
            except OperationCancelled:
                self.after(0, self._on_generate_cancelled)
            except Exception as exc:
                self.after(0, self._on_generate_error, exc)

        self._encode_thread = threading.Thread(target=worker, daemon=True)
        self._encode_thread.start()

    def _on_generate_done(self, result: codec.EncodedResult):
        self.generate_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.status_label.config(text="Audio generation complete!", foreground=ACCENT)
        self.waveform.plot(result.audio, codec.SAMPLE_RATE, color=ACCENT)
        duration = len(result.audio) / codec.SAMPLE_RATE
        mode_name = "Color" if self.mode == MODE_COLOR else "Grayscale"
        self.info_label.config(
            text=f"Image: {result.width}x{result.height}px | {mode_name} | "
                 f"{result.payload_bytes} bytes | {duration:.1f}s audio"
        )
        _play_audio_array(result.audio)
        self.play_btn.config(state="normal")
        self.pause_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.save_button.config(state="normal")
        self.wav_path = None
        codec.get_output_dirs(PROJECT_ROOT)
        show_message(self.winfo_toplevel(), "Complete", "Audio generation finished!", kind="info")

    def _on_generate_error(self, exc: Exception):
        self.generate_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.status_label.config(text="Error generating audio", foreground=DANGER)
        show_message(self.winfo_toplevel(), "Error", str(exc), kind="error")

    def _on_generate_cancelled(self):
        self.generate_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.status_label.config(text="Generation cancelled", foreground=WARNING)

    def _cancel(self):
        self._cancel_event.set()
        self.status_label.config(text="Cancelling...", foreground=WARNING)

    def _play_play(self):
        if not self.audio_result:
            return
        _play_play()
        self.play_btn.config(state="disabled")
        self.pause_btn.config(state="normal")
        self.stop_btn.config(state="normal")
        self._update_playback_position()

    def _play_pause_gui(self):
        _play_pause()
        if _play_handle.state == PlaybackState.PAUSED:
            self.play_btn.config(state="normal")
            self.pause_btn.config(state="disabled")
        elif _play_handle.state == PlaybackState.PLAYING:
            self.play_btn.config(state="disabled")
            self.pause_btn.config(state="normal")
            self._update_playback_position()

    def _play_stop_gui(self):
        _play_stop()
        self.play_btn.config(state="normal")
        self.pause_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        if self.audio_result:
            self.waveform.plot(self.audio_result.audio, codec.SAMPLE_RATE, color=ACCENT)

    def _on_waveform_seek(self, t_seconds: float):
        if not self.audio_result:
            return
        pos = int(t_seconds * codec.SAMPLE_RATE)
        _play_seek(pos)
        if _play_handle.state == PlaybackState.PLAYING:
            self.play_btn.config(state="disabled")
            self.pause_btn.config(state="normal")
            self.stop_btn.config(state="normal")
            self._update_playback_position()
        else:
            self.stop_btn.config(state="normal")
            self.waveform.plot(self.audio_result.audio, codec.SAMPLE_RATE,
                                color=ACCENT, play_pos=pos)

    def _update_playback_position(self):
        if not self.audio_result:
            return
        pos = _play_handle.elapsed
        self.waveform.plot(self.audio_result.audio, codec.SAMPLE_RATE,
                           color=ACCENT, play_pos=pos)
        if _play_handle.state == PlaybackState.PLAYING:
            self.after(100, self._update_playback_position)
        elif _play_handle.state == PlaybackState.STOPPED:
            self.play_btn.config(state="normal")
            self.pause_btn.config(state="disabled")
            self.stop_btn.config(state="disabled")

    def _save_as(self):
        if not self.audio_result:
            return
        sound_dir, _ = codec.get_output_dirs(PROJECT_ROOT)
        path = filedialog.asksaveasfilename(
            initialdir=sound_dir,
            title="Save audio as", defaultextension=".wav",
            filetypes=[("Audio WAV", "*.wav")],
        )
        if not path:
            return
        codec.save_wav(path, self.audio_result.audio)
        show_message(self.winfo_toplevel(), "Saved", f"Audio saved at:\n{path}", kind="info")


class DecodeTab(ttk.Frame):
    def __init__(self, parent, S: dict):
        super().__init__(parent, style="Panel.TFrame", padding=16)
        self.S = S
        self.speed_var = tk.StringVar(value="Normal (300 baud)")
        self.wav_path: str | None = None
        self.decoded_image: Image.Image | None = None
        self._is_recording = False
        self._cancel_event = threading.Event()
        self._decode_thread: threading.Thread | None = None
        self._build_layout()

    def _build_layout(self):
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        left = ttk.Frame(self, style="Panel.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = ttk.Frame(self, style="Panel.TFrame")
        right.grid(row=0, column=1, sticky="nsew")

        ttk.Label(left, text=self.S["choose_or_record"], style="Heading.TLabel").pack(anchor="w")
        self.path_label = ttk.Label(left, text=self.S["no_file"], style="Muted.TLabel")
        self.path_label.pack(anchor="w", pady=(2, 8))

        ttk.Label(left, text=self.S["speed_label"], style="Heading.TLabel").pack(anchor="w", pady=(0, 2))
        speed_row = ttk.Frame(left, style="Panel.TFrame")
        speed_row.pack(anchor="w")
        speed_combo = ttk.Combobox(
            speed_row, textvariable=self.speed_var, state="readonly", width=18,
            values=list(codec.SPEED_PRESETS.keys()),
        )
        speed_combo.pack(side="left")
        ttk.Label(left, text=self.S["speed_hint_decode"], style="Muted.TLabel").pack(anchor="w", pady=(2, 8))

        file_row = ttk.Frame(left, style="Panel.TFrame")
        file_row.pack(anchor="w")
        ttk.Button(file_row, text=self.S["select_wav"], command=self._pick_audio,
                    style="Accent.TButton").pack(side="left")

        if HAS_MIC:
            mic_row = ttk.Frame(left, style="Panel.TFrame")
            mic_row.pack(anchor="w", pady=(8, 0))
            self.record_button = ttk.Button(mic_row, text=self.S["record_mic"],
                                                command=self._toggle_recording, style="Secondary.TButton")
            self.record_button.pack(side="left")
            ttk.Label(mic_row, text=self.S["duration_label"], style="Muted.TLabel").pack(side="left", padx=(10, 4))
            self.record_seconds = tk.IntVar(value=35)
            ttk.Spinbox(mic_row, from_=5, to=180, increment=5, width=5,
                        textvariable=self.record_seconds).pack(side="left")
            self.record_status = ttk.Label(left, text="", style="Muted.TLabel")
            self.record_status.pack(anchor="w", pady=(4, 0))
        else:
            ttk.Label(
                left,
                text="Microphone recording unavailable (install 'sounddevice').",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(8, 0))

        ttk.Label(left, text=self.S["waveform_label_record"], style="Heading.TLabel").pack(anchor="w", pady=(16, 2))
        self.waveform = WaveformPanel(left)
        self.waveform.pack(fill="both", expand=True)

        self.decode_btn = ttk.Button(left, text=self.S["decode"], command=self._decode,
                                      style="Accent.TButton")
        self.decode_btn.pack(anchor="w", pady=(12, 0))

        self.cancel_btn = ttk.Button(left, text=self.S["cancel"], command=self._cancel_decode,
                                      style="Secondary.TButton", state="disabled")
        self.cancel_btn.pack(anchor="w", pady=(4, 0))

        ttk.Label(right, text=self.S["result_label"], style="Heading.TLabel").pack(anchor="w")
        self.result_label = tk.Label(right, bg=BG_PANEL, bd=0)
        self.result_label.pack(anchor="w", pady=(8, 8))

        self.status_label = ttk.Label(right, text="", style="Muted.TLabel")
        self.status_label.pack(anchor="w")

        log_header = ttk.Frame(right, style="Panel.TFrame")
        log_header.pack(fill="x", pady=(12, 2))
        ttk.Label(log_header, text=self.S["log_label"], style="Heading.TLabel").pack(side="left")
        self.clear_log_btn = ttk.Button(log_header, text=self.S["clear_log"], command=self._clear_log,
                                         style="Secondary.TButton")
        self.clear_log_btn.pack(side="right")
        log_frame = ttk.Frame(right, style="Panel.TFrame")
        log_frame.pack(fill="both", expand=True)
        self.log_box = tk.Text(log_frame, height=8, bg="#15161b", fg=FG_MUTED,
                                insertbackground=FG, relief="flat", wrap="word")
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

        self.save_button = ttk.Button(right, text=self.S["save_image"], command=self._save_image,
                                       style="Secondary.TButton", state="disabled")
        self.save_button.pack(anchor="w", pady=(10, 0))

    def _pick_audio(self):
        path = filedialog.askopenfilename(
            title="Choose recording", filetypes=[("Audio WAV", "*.wav"), ("All files", "*.*")]
        )
        if not path:
            return
        self.wav_path = path
        self.path_label.config(text=os.path.basename(path))
        try:
            audio = codec.load_wav_mono(path)
            self.waveform.plot(audio, codec.SAMPLE_RATE, color="#f2b866")
        except Exception as exc:
            show_message(self.winfo_toplevel(), "Error opening audio", str(exc), kind="error")

    def _toggle_recording(self):
        if self._is_recording:
            return
        self._is_recording = True
        duration = int(self.record_seconds.get())
        self.record_button.config(state="disabled")
        self.record_status.config(text=self.S["recording"].format(duration))
        self.update_idletasks()

        def worker():
            try:
                audio = sd.rec(int(duration * codec.SAMPLE_RATE), samplerate=codec.SAMPLE_RATE,
                                channels=1, dtype="float32")
                sd.wait()
                audio = audio.reshape(-1)
            except Exception as exc:
                self.after(0, lambda: self._on_record_error(exc))
                return
            self.after(0, lambda: self._on_record_done(audio))

        threading.Thread(target=worker, daemon=True).start()

    def _on_record_error(self, exc: Exception):
        self._is_recording = False
        self.record_button.config(state="normal")
        self.record_status.config(text="")
        show_message(self.winfo_toplevel(), "Recording error", str(exc), kind="error")

    def _on_record_done(self, audio: np.ndarray):
        self._is_recording = False
        self.record_button.config(state="normal")
        self.record_status.config(text=self.S["recorded"])

        sound_dir, _ = codec.get_output_dirs(PROJECT_ROOT)
        tmp_path = os.path.join(sound_dir, "_mic_recording.wav")
        codec.save_wav(tmp_path, audio)
        self.wav_path = tmp_path
        self.path_label.config(text="microphone recording")
        self.waveform.plot(audio, codec.SAMPLE_RATE, color="#f2b866")
        self._decode()

    def _append_log(self, lines: list[str]):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        for line in lines:
            self.log_box.insert("end", f"- {line}\n")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _decode(self):
        if not self.wav_path:
            show_message(self.winfo_toplevel(), "No audio", "Select a .wav file first.", kind="warning")
            return

        self._cancel_event.clear()
        self.decode_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.status_label.config(text="Decoding...", foreground=FG_MUTED)
        self.update_idletasks()

        def worker():
            try:
                baud, freq0, freq1 = codec.SPEED_PRESETS[self.speed_var.get()]
                result = codec.decode_audio(self.wav_path, cancel_event=self._cancel_event,
                                             baud=baud, freq_0=freq0, freq_1=freq1)
                self.after(0, lambda: self._on_decode_done(result))
            except OperationCancelled:
                self.after(0, self._on_decode_cancelled)
            except Exception as exc:
                self.after(0, lambda: self._on_decode_error(exc))

        self._decode_thread = threading.Thread(target=worker, daemon=True)
        self._decode_thread.start()

    def _on_decode_error(self, exc: Exception):
        self.decode_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if HAS_MIC:
            self.record_status.config(text="")
        self.status_label.config(text="Decode error", foreground=DANGER)
        show_message(self.winfo_toplevel(), "Decode error", str(exc), kind="error")

    def _on_decode_done(self, result: codec.DecodedResult):
        self.decode_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if HAS_MIC:
            self.record_status.config(text="")
        self._append_log(result.log)
        if result.image is None:
            self.status_label.config(text="Decode failed", foreground=DANGER)
            self.save_button.config(state="disabled")
            return

        self.decoded_image = result.image
        preview = result.image.convert("RGB")
        scale = min(600 / max(preview.size), 1.0)
        new_size = (max(1, int(preview.width * scale)), max(1, int(preview.height * scale)))
        preview_display = preview.resize(new_size, Image.LANCZOS)
        photo = ImageTk.PhotoImage(preview_display)
        self.result_label.configure(image=photo)
        self.result_label.image = photo

        status = f"{result.width}x{result.height}px | CRC: {'OK' if result.crc_ok else 'failed'}"
        self.status_label.config(text=status, foreground=(ACCENT if result.crc_ok else DANGER))
        self.save_button.config(state="normal")

    def _on_decode_cancelled(self):
        self.decode_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if HAS_MIC:
            self.record_status.config(text="")
        self.status_label.config(text="Decode cancelled", foreground=WARNING)

    def _cancel_decode(self):
        self._cancel_event.set()
        self.status_label.config(text="Cancelling...", foreground=WARNING)

    def _save_image(self):
        if not self.decoded_image:
            return
        _, image_dir = codec.get_output_dirs(PROJECT_ROOT)
        path = filedialog.asksaveasfilename(
            initialdir=image_dir,
            title="Save image as", defaultextension=".png",
            filetypes=[("PNG", "*.png")],
        )
        if not path:
            return
        self.decoded_image.save(path)
        show_message(self.winfo_toplevel(), "Saved", f"Image saved at:\n{path}", kind="info")


STRINGS = {
    "EN": {
        "title": "BitPixel",
        "encode_tab": "  Image -> Audio  ",
        "decode_tab": "  Audio -> Image  ",
        "choose_image": "1. Choose image",
        "no_image": "no image selected",
        "select_image": "Select image...",
        "max_size_label": "2. Max size (px, longest side)",
        "original_size": "Original size (no resize)",
        "info_text": "Larger images = longer audio.\n48px grayscale at 300 baud ~ 65s of audio.",
        "generate_audio": "Generate audio",
        "waveform_label_decode": "Waveform of generated audio",
        "play": "Play",
        "pause": "Pause",
        "stop": "Stop",
        "save_wav": "Save .wav as...",
        "choose_or_record": "1. Choose or record audio",
        "no_file": "no file selected",
        "select_wav": "Select .wav file...",
        "record_mic": "Record from microphone",
        "duration_label": "duration (s):",
        "recording": "Recording... speak now ({0}s)",
        "recorded": "Recording done, decoding...",
        "waveform_label_record": "Waveform of recording",
        "decode": "Decode image",
        "result_label": "2. Result",
        "log_label": "Log",
        "clear_log": "Clear log",
        "save_image": "Save image as...",
        "color_mode": "Color mode:",
        "grayscale": "Grayscale",
        "color": "Color",
        "speed_label": "Speed:",
        "speed_hint": "Fast halves the audio length; Normal is more reliable on noisy recordings.",
        "speed_hint_decode": "Set this to whatever speed the audio was encoded with (shown separately from the Encode tab's speed).",
        "volume": "Volume",
        "cancel": "Cancel",
    },
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.S = STRINGS["EN"]
        self._setup_ui()

    def _setup_ui(self):
        self.title(self.S["title"])
        self.geometry("1360x900")
        self.minsize(1300, 860)
        self.resizable(True, True)
        self.configure(bg=BG)
        self._setup_style()
        self._build_layout()
        codec.get_output_dirs(PROJECT_ROOT)

    def _setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=BG_PANEL)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG, foreground=FG_MUTED,
                         padding=(18, 10), font=(FONT_FAMILY, 10, "bold"))
        style.map("TNotebook.Tab",
                  background=[("selected", BG_PANEL)],
                  foreground=[("selected", ACCENT)])

        style.configure("Heading.TLabel", background=BG_PANEL, foreground=FG,
                         font=(FONT_FAMILY, 11, "bold"))
        style.configure("Muted.TLabel", background=BG_PANEL, foreground=FG_MUTED,
                         font=(FONT_FAMILY, 9))
        style.configure("Title.TLabel", background=BG, foreground=FG,
                         font=(FONT_FAMILY, 16, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=FG_MUTED,
                         font=(FONT_FAMILY, 10))

        style.configure("Accent.TButton", background=ACCENT, foreground="#0c1210",
                         font=(FONT_FAMILY, 10, "bold"), padding=(14, 8), borderwidth=0)
        style.map("Accent.TButton", background=[("active", ACCENT_DIM), ("disabled", "#3a3c48")],
                  foreground=[("disabled", "#77798a")])

        style.configure("Secondary.TButton", background="#33353f", foreground=FG,
                         font=(FONT_FAMILY, 10), padding=(12, 7), borderwidth=0)
        style.map("Secondary.TButton", background=[("active", "#434652"), ("disabled", "#2a2c34")],
                  foreground=[("disabled", "#6d6f7d")])

        style.configure("Horizontal.TScale", background=BG_PANEL)

    def _build_layout(self):
        header = ttk.Frame(self, style="TFrame", padding=(20, 18, 20, 8))
        header.pack(fill="x")
        ttk.Label(header, text="Image \u2194 Audio", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Encode an image into an audio signal (FSK) and decode back.",
            style="Subtitle.TLabel",
        ).pack(anchor="w")

        # Each tab owns its own independent Speed selector - so you can
        # encode a new image at Fast while decoding an older Normal-speed
        # recording at the same time, without them interfering.
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=16, pady=16)

        encode_tab = EncodeTab(notebook, self.S)
        decode_tab = DecodeTab(notebook, self.S)
        notebook.add(encode_tab, text=self.S["encode_tab"])
        notebook.add(decode_tab, text=self.S["decode_tab"])

        btn_frame = ttk.Frame(self, style="TFrame")
        btn_frame.pack(fill="x", padx=16, pady=(0, 8))
        btn_frame.columnconfigure(0, weight=1)

        left_btns = ttk.Frame(btn_frame, style="TFrame")
        left_btns.pack(side="left")

        ttk.Button(left_btns, text="Help", command=self._show_help,
                    style="Secondary.TButton").pack(side="left", padx=4)
        ttk.Button(left_btns, text="About", command=self._show_about,
                    style="Secondary.TButton").pack(side="left", padx=4)

        right_btns = ttk.Frame(btn_frame, style="TFrame")
        right_btns.pack(side="right")

        ttk.Button(right_btns, text="EXIT", command=self.quit,
                    style="Accent.TButton").pack(side="left", padx=4)

    def _show_help(self):
        help_text = (
            "BitPixel - How to Use\n\n"
            "1. IMAGE TO AUDIO:\n"
            "   - Select an image file.\n"
            "   - Set max size (or choose 'Original size' for no resize).\n"
            "   - Choose Grayscale or Color mode.\n"
            "   - Click 'Generate audio'.\n"
            "   - Use 'Play', 'Pause', 'Stop' to control audio playback.\n"
            "   - Click anywhere on the waveform to jump to that point.\n"
            "   - Adjust volume with the slider, even while playing.\n"
            "   - Click 'Cancel' to stop the conversion.\n"
            "   - Save the .wav file if needed.\n\n"
            "2. AUDIO TO IMAGE:\n"
            "   - Load a .wav file or record from microphone.\n"
            "   - Click 'Decode image'.\n"
            "   - The recovered image appears on the right.\n"
            "   - Click 'Cancel' to stop decoding.\n"
            "   - Save the image if decode succeeds.\n\n"
            "3. PLAYBACK CONTROLS:\n"
            "   - Play: Start playback, or resume after Pause.\n"
            "   - Pause: Stop playback without losing your place.\n"
            "   - Stop: Stop and reset playback to the beginning.\n"
            "   - Volume slider: Control playback volume in real time.\n"
            "   - Red line on waveform shows current position; click to seek.\n\n"
            "4. SPEED:\n"
            "   - Each tab has its own Speed dropdown (Normal / Fast).\n"
            "   - Fast halves the audio duration but is less tolerant\n"
            "     of noise; Normal (300 baud) is the safest default.\n"
            "   - When decoding a file you encoded yourself, set the\n"
            "     Decode tab's Speed to match what you used to encode it.\n\n"
            "5. TIPS:\n"
            "   - Larger images = longer audio.\n"
            "   - 48px grayscale at Normal speed ~ 65 seconds; color takes ~3x longer.\n"
            "   - For better recordings: quiet environment,\n"
            "     louder volume, closer devices.\n"
            "   - If CRC fails, try Normal speed instead of Fast.\n"
            "   - Output folders (output_sound/, output_image_recovered/)\n"
            "     are created next to src/ automatically on startup."
        )
        self._show_dialog("Help - BitPixel", help_text, min_width=560, max_height=560)

    def _show_about(self):
        about_text = (
            "BitPixel\n\n"
            "BitPixel is a FSK (Frequency Shift Keying) based system\n"
            "for encoding images as audio signals and decoding them back.\n\n"
            "Version 1.5\n\n"
            "Author: Cleber Moretti"
        )
        self._show_dialog("About - BitPixel", about_text, min_width=420, max_height=320)

    def _show_dialog(self, title: str, text: str, min_width: int = 480, max_height: int = 560):
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.configure(bg=BG)
        dialog.transient(self)
        dialog.resizable(True, True)

        container = ttk.Frame(dialog, style="Panel.TFrame")
        container.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        # Size the text box to the actual content (clamped) instead of a
        # fixed guess, so the OK button below it is never pushed off-screen.
        n_lines = text.count("\n") + 1
        text_height = min(max(n_lines + 1, 6), 28)

        text_widget = tk.Text(container, bg=BG_PANEL, fg=FG, insertbackground=FG,
                              relief="flat", wrap="word", font=(FONT_FAMILY, 10),
                              padx=10, pady=10, width=66, height=text_height)
        text_widget.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=text_widget.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text_widget.configure(yscrollcommand=scrollbar.set)
        text_widget.insert("1.0", text)
        text_widget.configure(state="disabled")

        btn_frame = ttk.Frame(dialog, style="TFrame")
        btn_frame.pack(fill="x", padx=16, pady=(0, 16), side="bottom")
        ttk.Button(btn_frame, text="OK", command=dialog.destroy,
                    style="Accent.TButton").pack(side="right", padx=4)

        # Ask Tk for the size these widgets actually need (OK button
        # included) and only then decide the window's geometry - a fixed
        # guessed size was the reason the OK button could end up clipped.
        dialog.update_idletasks()
        width = max(min_width, dialog.winfo_reqwidth())
        height = min(max_height, dialog.winfo_reqheight())
        dialog.minsize(360, 200)

        px, py = self.winfo_rootx(), self.winfo_rooty()
        pw, ph = self.winfo_width(), self.winfo_height()
        x = px + max(0, (pw - width) // 2)
        y = py + max(0, (ph - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")

        dialog.grab_set()
        dialog.focus_set()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
