"""read_page returns a long page in parts, so the worker can reach text past the first few thousand characters.

Live run 2026-11-22: a Wikipedia article's Singapore section sat past the 6000-character result limit, so every
read_page showed only the top of the page and scrolling did not help."""

import json

import pytest

from dom.desktop import tools


class Page:
    def __init__(self, text):
        self.text, self.calls = text, 0

    async def markdown(self):
        self.calls += 1
        return {"markdown": self.text}


def read(args):
    return tools.arguments({"name": "read_page", "arguments": json.dumps(args)})


LONG = "".join(f"[{n:05d}]" + "x" * 993 for n in range(12))  # 12,000 characters, a marker every 1000


async def test_a_long_page_comes_in_parts_that_cover_all_of_it():
    page = Page(LONG)
    first = await tools.invoke(page, *read({}))
    assert first.startswith("Part 1 of 3.") and "[00000]" in first and "[05000]" not in first
    parts = [await tools.invoke(page, *read({"part": n})) for n in (1, 2, 3)]
    body = "".join(part.split("\n", 1)[1] for part in parts)
    assert body == LONG
    assert all(len(tools.untrusted(part)) < tools.RESULT_LIMIT for part in parts)  # nothing cut by the result limit


async def test_a_short_page_is_one_part_with_no_header():
    assert await tools.invoke(Page("short page"), *read({})) == "short page"


async def test_a_part_past_the_end_says_so():
    result = await tools.invoke(Page(LONG), *read({"part": 9}))
    assert result == "There are only 3 parts."


@pytest.mark.parametrize("args", [{"part": 0}, {"part": "2"}, {"part": True}, {"page": 2}])
def test_bad_part_arguments_are_refused(args):
    with pytest.raises(ValueError):
        read(args)


def test_the_tool_says_how_to_read_further():
    description = tools._TOOLS["read_page"][0]
    assert "part" in description
    assert tools._TOOLS["read_page"][1]["part"]["type"] == "integer"


def test_part_is_optional_in_the_schema_the_model_sees():
    [schema] = [s for s in tools.schemas() if s["function"]["name"] == "read_page"]
    assert schema["function"]["parameters"]["required"] == []
