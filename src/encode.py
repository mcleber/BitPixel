"""
encode.py - converte uma imagem em audio FSK (.wav), via linha de comando.

Uso:
    python encode.py imagem.jpg audio.wav [tamanho_max_px] [--color]
"""

import sys
import argparse

from imgaudio_codec import encode_image, save_wav, MODE_COLOR


def main():
    parser = argparse.ArgumentParser(description="Encode image to audio FSK")
    parser.add_argument("image", help="image file path")
    parser.add_argument("out", help="output audio file path")
    parser.add_argument("max_size", nargs="?", type=int, default=48,
                        help="max size in pixels (default: 48)")
    parser.add_argument("--color", action="store_true", help="encode in color (RGB)")
    args = parser.parse_args()

    mode = MODE_COLOR if args.color else 0
    max_size = args.max_size if args.max_size > 0 else None

    result = encode_image(args.image, max_size=max_size, mode=mode)
    save_wav(args.out, result.audio)

    duration = len(result.audio) / 44100
    mode_name = "Color" if mode == MODE_COLOR else "Grayscale"
    print(f"Imagem reduzida para {result.width}x{result.height} px ({mode_name}, {result.payload_bytes} bytes de dados)")
    print(f"Audio gerado: {args.out} ({duration:.1f} segundos)")


if __name__ == "__main__":
    main()
