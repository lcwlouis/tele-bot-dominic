import json
from pathlib import Path
import sys

import pytest

from studio_service.config import Config
from studio_service.portraits import approve, generate, main

PNG = b"\x89PNG\r\n\x1a\n"
WORKER = Path(__file__).with_name("portrait_worker.py")


@pytest.fixture
def config(tmp_path):
    def outfit(name, **extra):
        return {"portrait": tmp_path / f"{name}.png", "turnaround": tmp_path / f"{name}-turn.png", **extra}

    (tmp_path / "classic.png").write_bytes(PNG + b"classic")
    (tmp_path / "classic-turn.png").write_bytes(PNG + b"turn")
    (tmp_path / "casual.png").write_bytes(PNG + b"old casual")
    command = {"command": [sys.executable, str(WORKER)]}
    return Config.model_validate({
        "bind_host": "192.0.2.1", "port": 8080, "token_file": tmp_path / "token", "allowed_sources": ["192.0.2.2"],
        "ffmpeg": tmp_path / "ffmpeg", "default_outfit": "classic",
        "outfits": {
            "classic": outfit("classic"), "casual": outfit("casual", description="a plain grey hoodie"),
            "formal-wear": outfit("formal-wear"), "broken": outfit("broken", description="broken"),
        },
        "workers": {kind: command for kind in ("voice", "image", "video", "special")},
    })


async def test_a_candidate_for_each_other_outfit_and_nothing_replaced(config, tmp_path):
    made = await generate(config, "classic", [], seed=7)
    assert sorted(path.parent.name for path in made) == ["casual", "formal-wear"]  # the broken one is skipped
    assert all(path.read_bytes().startswith(PNG) and path.name.endswith("-7.png") for path in made)
    assert (tmp_path / "casual.png").read_bytes() == PNG + b"old casual"  # a candidate replaces nothing
    assert not list(tmp_path.glob("candidates/*/.part-*"))
    requests = [json.loads(line) for line in (tmp_path / "candidates/casual/requests.jsonl").read_text().splitlines()]
    assert requests[0]["portrait"] == str(tmp_path / "classic.png") and requests[0]["turnaround"].endswith("classic-turn.png")
    assert "a plain grey hoodie" in requests[0]["prompt"] and requests[0]["size"] == 768
    assert b"formal wear outfit" in made[1].read_bytes()  # no description: made from the name


async def test_only_the_named_outfits(config):
    made = await generate(config, "classic", ["casual"], seed=1)
    assert [path.parent.name for path in made] == ["casual"]


async def test_the_reference_must_exist(config, tmp_path):
    (tmp_path / "classic.png").unlink()
    with pytest.raises(ValueError, match="missing"):
        await generate(config, "classic", [])
    with pytest.raises(ValueError, match="unknown"):
        await generate(config, "nobody", [])


async def test_approve_swaps_one_candidate_in_and_keeps_the_old_portrait(config, tmp_path):
    [candidate] = await generate(config, "classic", ["casual"], seed=3)
    portrait = approve(config, "classic", "casual", candidate)
    assert portrait.read_bytes() == candidate.read_bytes()
    assert (tmp_path / "casual.png.previous").read_bytes() == PNG + b"old casual"


async def test_approve_takes_only_a_png_candidate_for_that_outfit(config, tmp_path):
    [casual] = await generate(config, "classic", ["casual"], seed=3)
    [formal] = await generate(config, "classic", ["formal-wear"], seed=3)
    with pytest.raises(ValueError, match="must be a file in"):
        approve(config, "classic", "casual", formal)  # another outfit's candidate
    with pytest.raises(ValueError, match="must be a file in"):
        approve(config, "classic", "casual", tmp_path / "classic.png")  # not a candidate at all
    casual.write_bytes(b"not a png")
    with pytest.raises(ValueError, match="not a PNG"):
        approve(config, "classic", "casual", casual)
    with pytest.raises(ValueError, match="other than the reference"):
        approve(config, "classic", "classic", casual)
    assert (tmp_path / "casual.png").read_bytes() == PNG + b"old casual"


def test_the_command_line(config, tmp_path, monkeypatch, capsys):
    toml = tmp_path / "studio.toml"
    toml.write_text(
        'bind_host = "192.0.2.1"\nport = 8080\ntoken_file = "t"\nallowed_sources = ["192.0.2.2"]\nffmpeg = "f"\n'
        'default_outfit = "classic"\n'
        f'[outfits.classic]\nportrait = "{tmp_path}/classic.png"\nturnaround = "{tmp_path}/classic-turn.png"\n'
        f'[outfits.casual]\nportrait = "{tmp_path}/casual.png"\nturnaround = "{tmp_path}/casual-turn.png"\n'
        + "".join(f'[workers.{kind}]\ncommand = ["{sys.executable}", "{WORKER}"]\n' for kind in ("voice", "image", "video", "special")),
    )
    assert main(["generate", str(toml), "--seed", "5"]) == 0
    [candidate] = (tmp_path / "candidates/casual").glob("*-5.png")
    assert main(["approve", str(toml), "casual", str(candidate)]) == 0
    assert "portrait replaced" in capsys.readouterr().out
    assert main(["approve", str(toml), "casual", str(tmp_path / "nope.png")]) == 1
