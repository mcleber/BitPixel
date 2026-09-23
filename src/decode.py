"""
decode.py - reconstroi a imagem a partir de um audio (.wav) gravado, via
linha de comando.

Uso:
    python decode.py gravacao.wav resultado.png
"""

import sys

from imgaudio_codec import decode_audio


def main():
    """Decode the WAV file given on the command line and save the recovered image."""
    if len(sys.argv) < 3:
        print("uso: python decode.py gravacao.wav resultado.png")
        sys.exit(1)

    result = decode_audio(sys.argv[1])
    for line in result.log:
        print(line)

    if result.image is None:
        print("falha na decodificacao.")
        sys.exit(1)

    result.image.save(sys.argv[2])
    print(f"imagem salva em {sys.argv[2]}")


if __name__ == "__main__":
    main()
