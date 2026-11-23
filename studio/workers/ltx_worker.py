"""One LTX invocation per special note, within the supervised worker group."""

import math
import os
from pathlib import Path
import subprocess
import sys
import wave

from protocol import output_channel, serve


def main():
    output = output_channel()
    ltx, model, gemma, ffmpeg = sys.argv[1:]
    os.environ["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + os.environ.get("PATH", "")

    def render(request):
        audio = request["audio"]
        condition = Path(request["output"]).with_name("condition.wav")
        with wave.open(audio) as sample:
            duration = sample.getnframes() / sample.getframerate()
        frames = 8 * math.ceil((duration * 24 - 1) / 8) + 1
        subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-n", "-i", audio,
                        "-af", "apad=pad_dur=0.5", str(condition)], check=True)
        subprocess.run([
            ltx, "a2v", "--distilled", "--low-ram", "--prompt",
            "Locked camera, realistic head-and-shoulders video note. Speak naturally to the "
            "camera. Keep the same face, clothing and background as in the reference.",
            "--image", request["portrait"], "0", "1.0", "--audio", str(condition),
            "--model", model, "--gemma", gemma, "--width", "512", "--height", "512",
            "--frames", str(frames), "--frame-rate", "24", "--seed", str(request["seed"]),
            "--output", request["output"],
        ], check=True)

    serve(output, render)


if __name__ == "__main__":
    main()
