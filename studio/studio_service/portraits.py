"""Owner-run wardrobe portraits: one candidate portrait per outfit, made from the reference outfit's portrait.

    python -m studio_service.portraits generate CONFIG [--reference NAME] [--outfit NAME ...] [--seed N]
    python -m studio_service.portraits approve CONFIG OUTFIT CANDIDATE

`generate` runs the image worker once per outfit (the reference outfit, `default_outfit` unless named, is the identity
reference) and saves each result as a candidate under `candidates/<outfit>/` beside the reference portrait. It never
touches a configured portrait. `approve` copies one chosen candidate over its outfit's portrait, keeping the old one as
`<portrait>.previous`. Run it with the studio service stopped: the image worker needs the memory the service holds.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile

from .config import Config, load
from .process import Worker, WorkerError

PNG = b"\x89PNG\r\n\x1a\n"
SIZE = 768
PROMPT = (
    "Portrait of the same person as in the reference images: keep the face, hair, skin tone and build exactly. "
    "Dressed in {outfit}. Head and shoulders, facing the camera, relaxed, plain neutral background, even light."
)


def describe(name: str, description: str) -> str:
    return description.strip() or f"a {name.replace('-', ' ').replace('_', ' ')} outfit"


def candidates_dir(config: Config, reference: str, outfit: str) -> Path:
    return config.outfits[reference].portrait.parent / "candidates" / outfit


def check_reference(config: Config, reference: str) -> None:
    if reference not in config.outfits:
        raise ValueError(f"unknown reference outfit {reference!r}")
    for path in (config.outfits[reference].portrait, config.outfits[reference].turnaround):
        if not path.is_file():
            raise ValueError(f"the reference outfit's {path.name} is missing")


async def generate(config: Config, reference: str, names: list[str], seed: int | None = None) -> list[Path]:
    """Make one candidate for each named outfit (every outfit but the reference when none are named); returns their
    paths. A failed outfit is reported and skipped, so one bad render doesn't lose the others."""
    check_reference(config, reference)
    unknown = [name for name in names if name not in config.outfits]
    if unknown:
        raise ValueError(f"unknown outfit {unknown[0]!r}")
    targets = [name for name in (names or list(config.outfits)) if name != reference]
    source = config.outfits[reference]
    worker, made = Worker(config.workers.image), []
    try:
        for name in targets:
            run_seed = secrets.randbits(32) if seed is None else seed
            folder = candidates_dir(config, reference, name)
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target = folder / f"{stamp}-{run_seed}.png"
            handle, temp = tempfile.mkstemp(dir=folder, prefix=".part-", suffix=".png")
            os.close(handle)
            try:
                await worker.run({
                    "prompt": PROMPT.format(outfit=describe(name, config.outfits[name].description)),
                    "portrait": str(source.portrait), "turnaround": str(source.turnaround),
                    "size": SIZE, "seed": run_seed, "output": temp,
                })
                if not Path(temp).read_bytes().startswith(PNG):
                    raise WorkerError("not a PNG")
                os.replace(temp, target)  # a candidate appears whole or not at all
            except WorkerError:
                print(f"{name}: generation failed", file=sys.stderr)
                worker.retry_at = 0.0  # one bad render shouldn't hold back the next outfit
                continue
            finally:
                Path(temp).unlink(missing_ok=True)
            made.append(target)
            print(f"{name}: {target}")
    finally:
        await worker.stop()
    return made


def approve(config: Config, reference: str, outfit: str, candidate: Path) -> Path:
    """Put a candidate in place as the outfit's portrait. The old portrait is kept as `<portrait>.previous`."""
    if outfit not in config.outfits or outfit == reference:
        raise ValueError("name an outfit other than the reference")
    folder = candidates_dir(config, reference, outfit).resolve()
    chosen = candidate.resolve()
    if chosen.parent != folder or not chosen.is_file():
        raise ValueError(f"the candidate must be a file in {folder}")
    data = chosen.read_bytes()
    if not data.startswith(PNG):
        raise ValueError("the candidate is not a PNG")
    portrait = config.outfits[outfit].portrait
    if portrait.is_file():
        shutil.copy2(portrait, portrait.with_name(portrait.name + ".previous"))
    handle, temp = tempfile.mkstemp(dir=portrait.parent, prefix=".part-")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, portrait)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return portrait


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("generate", help="make candidate portraits")
    make.add_argument("config", type=Path)
    make.add_argument("--reference", help="the outfit whose portrait is the identity reference (default_outfit)")
    make.add_argument("--outfit", action="append", default=[], help="only this outfit (repeat for more)")
    make.add_argument("--seed", type=int, help="a fixed seed, to repeat a render")
    use = commands.add_parser("approve", help="use a candidate as an outfit's portrait")
    use.add_argument("config", type=Path)
    use.add_argument("outfit")
    use.add_argument("candidate", type=Path)
    use.add_argument("--reference")
    args = parser.parse_args(argv)
    config = load(args.config)
    reference = args.reference or config.default_outfit
    try:
        if args.command == "generate":
            if not asyncio.run(generate(config, reference, args.outfit, args.seed)):
                return 1
        else:
            print(f"{args.outfit}: portrait replaced ({approve(config, reference, args.outfit, args.candidate)})")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
