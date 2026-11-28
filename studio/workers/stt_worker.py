"""Keep an MLX Whisper model loaded and transcribe 16 kHz mono WAV files."""

from pathlib import Path
import sys

from protocol import output_channel, serve


def main():
    output = output_channel()
    import mlx.core as mx
    import mlx_whisper

    model_path = sys.argv[1]
    mx.set_cache_limit(0)
    # Warm the model into MLX's ModelHolder before reporting ready.
    mlx_whisper.load_models.load_model(model_path)
    mx.clear_cache()

    def transcribe(request):
        language = (request.get("language") or "").strip() or None
        result = mlx_whisper.transcribe(
            request["audio"],
            path_or_hf_repo=model_path,
            language=language,
        )
        text = (result.get("text") or "").strip()
        Path(request["output"]).write_text(text, encoding="utf-8")
        mx.clear_cache()

    serve(output, transcribe)


if __name__ == "__main__":
    main()
