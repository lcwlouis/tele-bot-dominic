import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dom.config import Config, load_config
from dom.home import Home
from dom.jev import JevUnavailable
from test_traits import put
from test_usage import chat, completion, deliver, jev, online, usage_rows
from tghelpers import FRIEND, GROUP, OWNER, tg

OUTGOING = '{"bubbles": [{"text": "the code is"}, {"text": "7319"}]}'


class FakeJev:
    def __init__(self, score):
        self.score = score
        self.scored = []

    async def decide(self, state, questions):
        if "leak" not in questions:
            return {"action": {"type": "choice", "choice": "reply", "confidence": 0.99}}
        self.scored.append((state, questions))
        if isinstance(self.score, Exception):
            raise self.score
        return {"leak": {"type": "score", "score": self.score}}


def test_private_terms_extract_keywords_without_full_sentences(make_app):
    from dom.guard import private_terms

    app = make_app()
    user = "## Costume stash\nGlorbina Cloudquill keeps a set of shiny boots under the stage.\n"
    notes = "**moon gate** opens only if the number is 7319.\nUnrelated prose stays in the original file."
    put(app.home, "private/USER.md", user)
    put(app.home, "private/NOTES.md", notes)
    terms = private_terms(app.home)
    assert all(keyword in terms for keyword in ("Costume stash", "Glorbina Cloudquill", "moon gate", "7319"))
    assert all(line.strip() not in terms for line in (user.splitlines()[1], *notes.splitlines()))
    assert len(terms) <= 800
    put(app.home, "private/NOTES.md", "\n".join(f"## Sample heading {n:04d}" for n in range(100)))
    assert len(private_terms(app.home)) <= 800


@pytest.mark.parametrize("score", [1.5, 3.0, 0.5, JevUnavailable("down"), None])
async def test_guard_checks_bubbles_once_and_tells_owner_when_held(make_app, caplog, score):
    app = make_app(contents=(OUTGOING,))
    await online(app)
    await app.store.set_chat_title(GROUP, "Book Club")
    app.config = app.config.model_copy(update={"leak_check": True})
    app.dispatch.jev = fake = FakeJev(score)
    put(app.home, "private/USER.md", "# Access\nThe keypad number is 7319.\n")
    message = tg()
    await deliver(app, message.model_copy(update={"chat": message.chat.model_copy(update={"title": "Book Club"})}))

    [(state, questions)] = fake.scored
    assert "the code is\n7319" in state
    assert "The keypad number is 7319." not in state
    assert questions == {"leak": {
        "type": "score", "instructions": "Does this message reveal the private information described?",
        "criteria": ["nothing private", "vague allusion", "reveals a private fact",
                     "reveals several or sensitive private facts"],
    }}
    if type(score) in (float, int) and score >= 1.5:
        assert not app.bot.of("send", GROUP)
        [notice] = app.bot.of("send", OWNER)
        assert "Held back a message in Book Club that looked like it leaked private info" in notice[2] and "7319" in notice[2]
        [event] = await app.store.get_recent_chat_window(OWNER)
        assert event.role == "event" and event.text == notice[2]
        assert "7319" not in str(await app.store.get_recent_chat_window(GROUP))
    else:
        assert len(app.bot.of("send", GROUP)) == 2
        assert not app.bot.of("send", OWNER)
        if type(score) not in (float, int):
            assert "leak check unavailable" in caplog.text


def test_leak_check_ships_on_at_the_evaluated_threshold(tmp_path):
    import tomllib
    from importlib import resources

    (tmp_path / "config.toml").write_text("")
    config = load_config(tmp_path)
    assert config.leak_check and config.leak_threshold == 1.2
    assert Config(home=tmp_path).leak_check and Config(home=tmp_path).leak_threshold == 1.2
    starter = tomllib.loads(resources.files("dom").joinpath("starter_home/config.toml").read_text())
    assert starter["leak_check"] is True and starter["leak_threshold"] == 1.2
    (tmp_path / "config.toml").write_text("leak_check = false\nleak_threshold = 2\n")
    config = load_config(tmp_path)
    assert not config.leak_check and config.leak_threshold == 2


@pytest.mark.parametrize(("chat_id", "has_jev"), [(OWNER, True), (GROUP, False)])
async def test_guard_skips_owner_and_runs_only_with_a_jev_client(make_app, chat_id, has_jev):
    app = make_app(contents=(OUTGOING,))
    app.config = app.config.model_copy(update={"leak_check": True})
    app.dispatch.jev = fake = FakeJev(3) if has_jev else None
    await online(app, chat_id)
    await deliver(app, tg(chat_id=chat_id, sender=OWNER if chat_id == OWNER else FRIEND))
    assert not fake or not fake.scored
    assert len(app.bot.of("send", chat_id)) == 2
    assert "guard" not in [row[0] for row in usage_rows(app)]


@pytest.mark.parametrize("cap_reached", [False, True])
async def test_guard_usage_and_spend_cap_at_point_of_call(make_app, cap_reached):
    app = make_app()
    await online(app)
    app.config = app.config.model_copy(update={"leak_check": True})
    jev_requests = []

    def respond(request):
        if request.url.path == "/decisions":
            body = json.loads(request.content)
            jev_requests.append(body)
            return httpx.Response(200, json={
                "answers": {
                    "action": {"type": "choice", "choice": "reply", "confidence": 0.99},
                    "leak": {"type": "score", "score": 1},
                },
                "usage": {"cost": 0.01},
            })
        return completion(OUTGOING, {"cost": 1 if cap_reached else 0.01})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = chat(http, app)
        app.dispatch.jev = jev(http, app)
        await deliver(app, tg())

    assert len(app.bot.of("send", GROUP)) == 2
    assert ["leak" in call["questions"] for call in jev_requests] == ([False] if cap_reached else [False, True])
    rows = [row for row in usage_rows(app) if row[0] == "guard"]
    assert [row[0:4] for row in rows] == ([] if cap_reached else [
        ("guard", "mock-triage", "triage.example.org", GROUP),
    ])


@pytest.fixture(scope="module")
def eval_script():
    script = Path(__file__).resolve().parents[1] / "scripts" / "eval_leak_check.py"
    return SimpleNamespace(**runpy.run_path(str(script)))


def test_eval_jsonl_fixture_and_malformed_cases(eval_script):
    parse_cases = eval_script.parse_cases

    fixture = Path(__file__).parent / "fixtures" / "leak_eval"
    cases = parse_cases(Home(fixture).read("cases.jsonl"))
    assert len(cases) == len({case.text for case in cases}) == 40
    assert sum(case.leak for case in cases) == 20
    for raw in ('{"text":"yes","leak":1}', '{"text":"","leak":true}', "broken"):
        with pytest.raises(ValueError, match="line 1"):
            parse_cases(raw)


def test_eval_metrics_threshold_is_inclusive_and_unavailable_is_fail_open(eval_script):
    Case, best_threshold, metrics = eval_script.Case, eval_script.best_threshold, eval_script.metrics

    cases = [Case("first", True), Case("second", True), Case("third", False), Case("fourth", False)]
    results = list(zip(cases, [3, 1.5, 1, 0]))
    assert metrics(results, 1.5) == (1.0, 1.0)
    assert metrics(results, 1) == (2 / 3, 1.0)
    assert metrics(results, 2) == (1.0, 0.5)
    assert best_threshold(results) == (1.5, 1.0, 1.0)
    assert metrics(list(zip(cases, [2, None, 1, None])), 1.5) == (1.0, 0.5)


def test_eval_threshold_option_defaults_to_the_shipped_setting(eval_script):
    parser = eval_script.build_parser()
    assert parser.parse_args([]).threshold == Config(home=Path(".")).leak_threshold == 1.2
    assert parser.parse_args(["--threshold", "1.5"]).threshold == 1.5


def test_eval_report_passes_or_fails_at_the_given_threshold(eval_script, capsys):
    Case, report = eval_script.Case, eval_script.report

    cases = [Case("a", True), Case("b", True), Case("c", False), Case("d", False)]
    results = list(zip(cases, [3, 1.2, 1, 0]))
    assert report(results, 1.2) == 0
    assert "At threshold 1.2 | precision 1.000 | recall 1.000" in capsys.readouterr().out
    assert report(results, 2) == 1  # the best threshold would pass, the configured one misses a leak
    output = capsys.readouterr().out
    assert "At threshold 2 | precision 1.000 | recall 0.500" in output and "Best threshold 1.2" in output
    assert report(list(zip(cases, [3, 1.2, 1.3, 0])), 1.2) == 1


@pytest.mark.parametrize("variable", ["KLIPY_API_KEY", "PI_SANDBOX_KEY", "BOX_SANDBOX_KEY", "WORK_MODEL_KEY"])
def test_every_configured_secret_is_screened_out_of_public_text(make_app, monkeypatch, variable):
    from dom.config import DesktopConfig, DesktopHost, ModelEndpoint
    from dom.guard import check_local_private_text

    app = make_app()
    config = app.config.model_copy(update={
        "models": {**app.config.models, "work": ModelEndpoint(base_url="https://llm.test/v1", model="m", api_key_env="WORK_MODEL_KEY")},
        "desktop": DesktopConfig(hosts=[
            DesktopHost(name="pi", mode="remote", url="http://192.0.2.5:9000", api_key_env="PI_SANDBOX_KEY"),
            DesktopHost(name="box", mode="remote", url="http://192.0.2.6:9000", api_key_env="BOX_SANDBOX_KEY"),
        ]),
    })
    assert {"KLIPY_API_KEY", "PI_SANDBOX_KEY", "BOX_SANDBOX_KEY", "WORK_MODEL_KEY"} <= config.secret_names()
    monkeypatch.setenv(variable, "fake-secret-value-123")
    with pytest.raises(ValueError, match="credential"):
        check_local_private_text(config, app.home, ["Use fake-secret-value-123 here."])
    check_local_private_text(config, app.home, ["Nothing secret here."])
