import pytest
from aiogram.types import Document

from dom.desktop.client import SandboxError
from dom.desktop.files import download_results, safe_filename, stage_inbox, upload_inbox
from dom.desktop.worker import DesktopWorker
from dom.home import Home
from dom.telegram.handlers import handle_message
from test_desktop_worker import Sandbox, call, wait_task, worker_for
from test_usage import online
from tghelpers import FRIEND, GROUP, OWNER, drain_dispatch, tg


@pytest.mark.parametrize(("raw", "expected"), [
    ("../../report.pdf", "report.pdf"),
    (r"..\..\Quarterly report.pdf", "Quarterly_report.pdf"),
    ("...", "document"),
    (None, "document"),
    ("résumé / data<1>.csv", "data_1_.csv"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


async def test_limits_for_result_files(monkeypatch):
    class Client:
        async def download_file(self, path, *, max_bytes):
            assert max_bytes == 3
            raise SandboxError("file exceeds 3 bytes")

    monkeypatch.setattr("dom.desktop.files.MAX_FILE_BYTES", 3)
    with pytest.raises(ValueError, match="5 files"):
        await download_results(Client(), ["/tmp/example"] * 6)
    with pytest.raises(SandboxError, match="exceeds"):
        await download_results(Client(), ["/tmp/example"])


async def test_staged_files_upload_only_at_task_start(make_app):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    worker = await worker_for(app, call("finish", result="Done"), {"content": "invalid"})
    uploads = []

    async def upload(path, data):
        uploads.append((path, data))
        return {"success": True}

    worker.sandbox.upload_file = upload
    assert stage_inbox(app.home.root, "report.pdf", b"hello") == "report.pdf"
    assert stage_inbox(app.home.root, "report.pdf", b"world") == "report-2.pdf"
    directory = app.home.root.resolve().parent / "desktop-inbox"
    assert uploads == []
    await worker.enqueue(OWNER, "Read the report")
    await wait_task(worker)
    assert uploads == [
        ("/home/gem/inbox/report-2.pdf", b"world"), ("/home/gem/inbox/report.pdf", b"hello"),
    ]
    assert list(directory.iterdir()) == []


async def test_failed_upload_is_kept(make_app):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    assert stage_inbox(app.home.root, "keep.txt", b"hello") == "keep.txt"

    class Client:
        async def upload_file(self, path, data):
            raise SandboxError("upload unavailable")

    with pytest.raises(SandboxError):
        await upload_inbox(app.home.root, Client())
    assert (app.home.root.resolve().parent / "desktop-inbox" / "keep.txt").read_bytes() == b"hello"


async def test_documents_are_sent_to_owner_after_result(make_app, monkeypatch):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    worker = await worker_for(
        app, call("finish", result="Report is ready", files=["/tmp/report.txt"]),
        {"content": '{"bubbles":[{"text":"Here you go"}]}'},
    )

    async def download(path, *, max_bytes):
        assert path == "/tmp/report.txt"
        assert max_bytes == 20_000_000
        return b"report"

    async def send_document(chat_id, document):
        app.bot.calls.append(("document", chat_id, document.filename, document.data))

    monkeypatch.setattr(worker.sandbox, "download_file", download, raising=False)
    monkeypatch.setattr(app.bot, "send_document", send_document, raising=False)
    await worker.enqueue(OWNER, "Write a report")
    await wait_task(worker)
    assert [item[:3] for item in app.bot.calls if item[0] in ("send", "document")] == [
        ("send", OWNER, "Here you go"), ("document", OWNER, "report.txt"),
    ]


async def test_only_owner_dm_documents_are_staged_and_noted(make_app, monkeypatch):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    app.sandbox = Sandbox()
    app.desktop_worker = DesktopWorker(app, app.sandbox)
    app.config = app.config.model_copy(update={"desktop_mode": "remote"})
    downloaded = []

    async def download(document, *, destination):
        downloaded.append(document.file_id)
        destination.write(b"data")
        destination.seek(0)
        return destination

    monkeypatch.setattr(app.bot, "download", download, raising=False)
    document = Document(file_id="file", file_unique_id="unique", file_name="../sample.pdf", file_size=4)
    await handle_message(app, tg(GROUP, OWNER, text=None, document=document))
    await handle_message(app, tg(FRIEND, FRIEND, text=None, document=document))
    assert downloaded == []
    await online(app, OWNER)
    await handle_message(app, tg(OWNER, OWNER, text=None, document=document))
    assert downloaded == ["file"]
    await drain_dispatch(app.dispatch)
    assert any(
        "file received: inbox/sample.pdf" in str(request["messages"])
        for request in app.script.requests
    )


async def test_intake_checks_declared_and_actual_size(make_app, monkeypatch):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    app.sandbox = Sandbox()
    app.desktop_worker = DesktopWorker(app, app.sandbox)
    app.config = app.config.model_copy(update={"desktop_mode": "local"})
    monkeypatch.setattr("dom.desktop.files.MAX_FILE_BYTES", 3)
    downloads = []

    async def download(document, *, destination):
        downloads.append(document.file_id)
        destination.write(b"1234")
        return destination

    monkeypatch.setattr(app.bot, "download", download, raising=False)
    for i, size in enumerate((4, None), 1):
        await handle_message(app, tg(OWNER, OWNER, text=None, msg_id=i, document=Document(
            file_id="file", file_unique_id="unique", file_name="large.pdf", file_size=size,
        )))
    assert downloads == ["file"]
    assert all("20 MB" in call[2] for call in app.bot.of("send", OWNER))
    directory = app.home.root.resolve().parent / "desktop-inbox"
    assert not directory.exists()
