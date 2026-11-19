from types import SimpleNamespace

import pytest
from aiogram.types import Document

from dom.desktop.client import SandboxError
from dom.desktop.files import download_results, safe_filename, stage_inbox, upload_inbox
from dom.desktop.worker import DesktopWorker
from dom.home import Home
from dom.telegram.handlers import handle_message
from test_desktop_worker import Sandbox, call, task_row, wait_task, worker_for
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
    await handle_message(app, tg(GROUP, OWNER, text=None, document=document, caption="/desktop"))
    await handle_message(app, tg(FRIEND, FRIEND, text=None, document=document, caption="/desktop"))
    assert downloaded == []
    await online(app, OWNER)
    await handle_message(app, tg(OWNER, OWNER, text=None, document=document, caption="/Desktop read it"))
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
        await handle_message(app, tg(OWNER, OWNER, text=None, msg_id=i, caption="/desktop", document=Document(
            file_id="file", file_unique_id="unique", file_name="large.pdf", file_size=size,
        )))
    assert downloads == ["file"]
    assert all("20 MB" in call[2] for call in app.bot.of("send", OWNER))
    directory = app.home.root.resolve().parent / "desktop-inbox"
    assert not directory.exists()


@pytest.mark.parametrize("caption", [None, "", "please read this", "read /desktop"])
async def test_documents_without_a_desktop_caption_are_just_chat(make_app, monkeypatch, caption):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    app.sandbox = Sandbox()
    app.desktop_worker = DesktopWorker(app, app.sandbox)
    app.config = app.config.model_copy(update={"desktop_mode": "local"})
    downloads = []

    async def download(document, *, destination):
        downloads.append(document.file_id)
        return destination

    monkeypatch.setattr(app.bot, "download", download, raising=False)
    document = Document(file_id="file", file_unique_id="unique", file_name="a.pdf", file_size=4)
    await handle_message(app, tg(OWNER, OWNER, text=None, caption=caption, document=document))
    assert downloads == []
    assert not (app.home.root.resolve().parent / "desktop-inbox").exists()


def test_inbox_caps_files_and_total_bytes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for number in range(5):
        stage_inbox(home, f"{number}.txt", b"x")
    with pytest.raises(ValueError, match="Inbox full"):
        stage_inbox(home, "6.txt", b"x")
    assert len(list((tmp_path / "desktop-inbox").iterdir())) == 5
    monkeypatch.setattr("dom.desktop.files.MAX_INBOX_BYTES", 7)
    for path in (tmp_path / "desktop-inbox").iterdir():
        path.unlink()
    stage_inbox(home, "a.txt", b"12345")
    with pytest.raises(ValueError, match="Inbox full"):
        stage_inbox(home, "b.txt", b"123")
    stage_inbox(home, "b.txt", b"12")


async def test_full_inbox_replies_and_does_not_stage(make_app, monkeypatch):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    app.sandbox = Sandbox()
    app.desktop_worker = DesktopWorker(app, app.sandbox)
    app.config = app.config.model_copy(update={"desktop_mode": "local"})
    for number in range(5):
        stage_inbox(app.home.root, f"{number}.txt", b"x")

    async def download(document, *, destination):
        destination.write(b"data")
        return destination

    monkeypatch.setattr(app.bot, "download", download, raising=False)
    document = Document(file_id="file", file_unique_id="unique", file_name="late.pdf", file_size=4)
    await handle_message(app, tg(OWNER, OWNER, text=None, caption="/desktop", document=document))
    assert app.bot.of("send", OWNER)[-1][2] == "Inbox full"
    assert not (app.home.root.resolve().parent / "desktop-inbox" / "late.pdf").exists()


@pytest.mark.parametrize("finish", [True, False])
async def test_inbox_is_removed_from_the_sandbox_after_any_task(make_app, finish):
    app = make_app()
    worker = await worker_for(
        app, call("finish", result="Done") if finish else {"content": "stop"}, {"content": "invalid"}, max_steps=1,
    )
    commands = []

    async def bash(command):
        commands.append(command)
        raise SandboxError("not available")

    worker.sandbox.client = SimpleNamespace(bash=bash)
    task_id = await worker.enqueue(OWNER, "Example")
    await wait_task(worker)
    assert commands == ["rm -rf /home/gem/inbox/*"]
    assert (await task_row(app, task_id))["status"] == ("done" if finish else "failed")
