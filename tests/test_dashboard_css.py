"""Every usage kind the dashboard draws has its own bar colour."""

from pathlib import Path

from dom.web.pages import KINDS

CSS = (Path(__file__).parents[1] / "dom/web/static/dashboard.css").read_text()


def test_every_usage_kind_has_a_bar_colour():
    assert [kind for kind in KINDS if f".bar-{kind} {{ fill:" not in CSS] == []
