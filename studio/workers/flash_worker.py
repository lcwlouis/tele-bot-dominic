"""Keep the FlashHead Lite MPS pipeline resident."""

import gc
import math
import os
import subprocess
import sys

from protocol import output_channel, serve

FACE_RATIO = 2.5  # the crop's width over the face's: 2 is a tight headshot, 3 takes in the shoulders


def main():
    output = output_channel()
    source, model, audio_model, ffmpeg = sys.argv[1:]
    sys.path.insert(0, source)
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    import librosa
    import mps_adapter  # noqa: F401  (installs the pinned port's MPS shims)
    import numpy as np
    import soundfile as sf
    import torch
    from flash_head.inference import (
        get_pipeline, get_base_data, get_infer_params, get_audio_embedding, run_pipeline,
    )
    from flash_head.src.pipeline import flash_head_pipeline
    from flash_head.utils.cpu_face_handler import CPUFaceHandler
    from flash_head.utils.facecrop import get_scaled_bbox
    from PIL import Image

    if not torch.backends.mps.is_available():
        raise RuntimeError("FlashHead requires MPS")
    pipeline = get_pipeline(world_size=1, ckpt_dir=model, model_type="lite", wav2vec_dir=audio_model)
    params = get_infer_params()
    # A round video note is a talking head: crop each still to the face with head and shoulders around it, so the mouth
    # and eyes fill enough of the frame to move. The detector is built once (its files are fetched by
    # prepare-workers.sh). Only a still with exactly one face is cropped: none, or several (the crop could land on
    # someone else), and the pipeline animates the whole still, as it does whenever this raises.
    detector = CPUFaceHandler()
    size = (params["width"], params["height"])

    def face_crop(path):
        image = Image.open(path).convert("RGB")
        boxes, _ = detector(np.array(image))
        if len(boxes) != 1:
            raise ValueError(f"{len(boxes)} faces found")
        width, height = image.size
        x1, y1, x2, y2 = boxes[0]
        box = [x1 * width, y1 * height, x2 * width, y2 * height]
        return get_scaled_bbox(box, width, height, FACE_RATIO, image).resize(size)

    flash_head_pipeline.process_image = face_crop
    torch.mps.synchronize()
    torch.mps.empty_cache()

    @torch.inference_mode()
    def render(request):
        get_base_data(pipeline, cond_image_path_or_dir=request["portrait"], base_seed=request["seed"],
                      use_face_crop=True)
        sample_rate = params["sample_rate"]
        fps = params["tgt_fps"]
        frame_num = params["frame_num"]
        motion_frames = params["motion_frames_num"]
        stride = frame_num - motion_frames
        signal, source_rate = sf.read(request["audio"], dtype="float32")
        if signal.ndim == 2:
            signal = signal.mean(axis=1)
        duration = len(signal) / source_rate
        signal = librosa.resample(signal, orig_sr=source_rate, target_sr=sample_rate)
        # Include the final window even when it is only partially filled.
        needed_frames = math.ceil(duration * fps)
        chunks = 1 + max(0, math.ceil((needed_frames - frame_num) / stride))
        padded_samples = (frame_num + (chunks - 1) * stride) * sample_rate // fps
        signal = np.pad(signal[:padded_samples], (0, max(0, padded_samples - len(signal))))
        embedding = get_audio_embedding(pipeline, signal)
        if embedding.shape[1] < frame_num + (chunks - 1) * stride:
            raise RuntimeError(f"short embedding {embedding.shape} for {chunks} windows")

        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-n",
               "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{params['width']}x{params['height']}", "-r", str(fps), "-i", "-",
               "-i", request["audio"], "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
               "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
               "-ac", "1", "-movflags", "+faststart", "-shortest", request["output"]]
        frames_written = 0
        encoder = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        try:
            for index in range(chunks):
                start_frame = index * stride
                condition = embedding[:, start_frame:start_frame + frame_num].contiguous()
                frames = run_pipeline(pipeline, condition)
                if index:
                    frames = frames[motion_frames:]
                frames = frames[:needed_frames - frames_written]
                pixels = frames.to(torch.uint8).cpu().numpy()
                encoder.stdin.write(pixels.tobytes())
                frames_written += len(pixels)
                del pixels, frames, condition
        finally:
            encoder.stdin.close()
            code = encoder.wait()
        if code or frames_written != needed_frames:
            raise RuntimeError(f"encode status {code}, frames {frames_written}/{needed_frames}")
        del embedding
        gc.collect()
        torch.mps.synchronize()
        torch.mps.empty_cache()

    serve(output, render)


if __name__ == "__main__":
    main()
