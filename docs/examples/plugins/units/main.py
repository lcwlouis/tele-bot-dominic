"""Starter plugin: one tool, one slash command and one dashboard panel. See docs/plugins.md."""

from _dom import command, panel, run, tool


@tool
def length(value, unit):
    return f"{value * 1.609:.2f} km" if unit == "mi" else f"{value / 1.609:.2f} mi"


@command
def km(text, scope):
    try:
        miles = float(text)
    except ValueError:
        return "Usage: /km <miles>"
    return f"{miles} mi is {miles * 1.609:.2f} km"


@panel
def summary():
    return {
        "title": "Common distances",
        "paragraphs": ["Rounded to two decimals."],
        "items": [["5 km", "3.11 mi"], ["10 km", "6.21 mi"]],
        "table": {"columns": ["Race", "km", "mi"], "rows": [["Half", 21.1, 13.1], ["Full", 42.2, 26.2]]},
    }


run()
