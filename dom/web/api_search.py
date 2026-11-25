"""JSON API for the dashboard's search box: one query over chats, memory files, briefs, reminders, desktop tasks,
skills and plugins (dom/global_search.py). Another chat's messages arrive as one-line previews only."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Query, Request

from dom import global_search
from dom.web.shared import _title, owner_app

router = APIRouter()
GUARDED_GETS = {"/api/search"}  # same-site browsers only

PAGES = {
    "chat": "/app/chats", "memory": "/app/memory", "brief": "/app/work/briefs", "reminder": "/app/work/reminders",
    "task": "/app/work", "skill": "/app/work/own-work", "plugin": "/app/system/plugins",
}


def _href(hit: global_search.Hit) -> str:
    if hit.kind == "memory":
        return f"{PAGES['memory']}?file={quote(hit.ref, safe='')}"
    return PAGES[hit.kind]


@router.get("/api/search")
async def search(request: Request, q: str = Query("", max_length=global_search.MAX_QUERY)) -> dict:
    app = owner_app(request)
    titles = {row["chat_id"]: _title(row, app.owner_id) for row in await app.store.list_chats()}
    found = await global_search.search_all(app.store, app.home, q, titles)
    return {
        "query": " ".join(q.split()),
        "groups": [
            {
                "kind": kind, "label": global_search.LABELS[kind],
                "hits": [
                    {"title": hit.title, "text": hit.text, "href": _href(hit), "at": hit.at, "full": hit.full}
                    for hit in hits
                ],
            }
            for kind, hits in found.items() if hits
        ],
    }
