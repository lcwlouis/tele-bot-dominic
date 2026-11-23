"""Keep the pinned Klein 4B snapshot loaded and quantize on load."""

from pathlib import Path
import sys

from protocol import output_channel, serve


def main():
    output = output_channel()
    import mlx.core as mx
    from mflux.models.common.config.model_config import ModelConfig
    from mflux.models.flux2.variants import Flux2KleinEdit

    mx.set_cache_limit(0)
    mx.set_memory_limit(29 * 1024**3)
    model = Flux2KleinEdit(
        model_config=ModelConfig.flux2_klein_4b(), quantize=4,
        model_path=sys.argv[1], compute_precision=mx.float16,
    )
    mx.eval(model.parameters())
    mx.clear_cache()

    def render(request):
        size = request["size"]
        image = model.generate_image(
            seed=request["seed"], prompt=request["prompt"], width=size, height=size,
            image_paths=[Path(request["turnaround"]), Path(request["portrait"])],
        )
        image.save(path=request["output"])
        mx.synchronize()
        del image
        mx.clear_cache()

    serve(output, render)


if __name__ == "__main__":
    main()
