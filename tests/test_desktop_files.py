from types import SimpleNamespace

import pytest
from aiogram.types import Document

from dom.desktop.client import SandboxError
from dom.desktop.files import bind_inbox, download_results, safe_filename, stage_inbox, upload_inbox
from dom.desktop.worker import DesktopWorker
from dom.home import Home
from dom.telegram.handlers import handle_message
from test_desktop_worker import Sandbox, adopt, call, task_row, wait_task, worker_for
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
    task_id = await worker.enqueue(OWNER, "Read the report")
    await wait_task(worker)
    assert uploads == [
        (f"/home/gem/inbox-{task_id}/report-2.pdf", b"world"), (f"/home/gem/inbox-{task_id}/report.pdf", b"hello"),
    ]
    assert list(directory.iterdir()) == []
    assert f"/home/gem/inbox-{task_id}/" in app.client.requests[0][0][1]["content"]
    assert "report.pdf" in app.client.requests[0][0][1]["content"]


async def test_files_are_bound_to_the_next_task_when_it_is_enqueued(make_app):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    worker = await worker_for(app, call("finish", result="Done"), {"content": "invalid"})
    uploads = []

    async def upload(path, data):
        uploads.append(path)
        return {"success": True}

    worker.sandbox.upload_file = upload
    directory = app.home.root.resolve().parent / "desktop-inbox"
    stage_inbox(app.home.root, "before.txt", b"1")
    task_id = await worker.enqueue(OWNER, "Read it")
    assert [path.name for path in directory.iterdir()] == [f"task-{task_id}"]  # bound before the task ran
    assert (directory / f"task-{task_id}" / "before.txt").read_bytes() == b"1"
    stage_inbox(app.home.root, "after.txt", b"2")  # belongs to the next task
    await wait_task(worker)
    assert uploads == [f"/home/gem/inbox-{task_id}/before.txt"]
    assert [path.name for path in directory.iterdir()] == ["after.txt"]


async def test_a_refused_enqueue_leaves_staged_files_for_the_next_task(make_app):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    worker = await worker_for(app, {"content": "invalid"})
    stage_inbox(app.home.root, "keep.txt", b"1")
    with pytest.raises(ValueError):
        await worker.enqueue(OWNER, "")
    assert [path.name for path in (app.home.root.resolve().parent / "desktop-inbox").iterdir()] == ["keep.txt"]


async def test_stale_task_directories_are_dropped_on_recover(make_app):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    worker = await worker_for(app)
    directory = app.home.root.resolve().parent / "desktop-inbox"
    (directory / "task-99").mkdir(parents=True)
    (directory / "task-99" / "old.txt").write_text("private")
    stage_inbox(app.home.root, "pending.txt", b"1")
    await worker.recover()
    assert [path.name for path in directory.iterdir()] == ["pending.txt"]


async def test_failed_upload_drops_the_files_and_the_report_says_so(make_app):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    assert stage_inbox(app.home.root, "drop.txt", b"hello") == "drop.txt"
    bind_inbox(app.home.root, 7)

    class Client:
        async def upload_file(self, path, data):
            raise SandboxError("upload unavailable")

    with pytest.raises(SandboxError, match="dropped"):
        await upload_inbox(app.home.root, Client(), 7)
    assert list((app.home.root.resolve().parent / "desktop-inbox").iterdir()) == []

    worker = await worker_for(app, call("finish", result="never"), {"content": "invalid"})

    async def upload(path, data):
        raise SandboxError("upload unavailable")

    worker.sandbox.upload_file = upload
    stage_inbox(app.home.root, "again.txt", b"hello")
    task_id = await worker.enqueue(OWNER, "Read it")
    await wait_task(worker)
    row = await task_row(app, task_id)
    assert row["status"] == "failed" and "dropped" in row["error"]
    assert list((app.home.root.resolve().parent / "desktop-inbox").iterdir()) == []
    assert "dropped" in str(app.client.requests[-1][0])  # the report model is told


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
    adopt(app, DesktopWorker(app, app.sandbox))
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
    adopt(app, DesktopWorker(app, app.sandbox))
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


@pytest.mark.parametrize("caption", [
    None, "", "please read this", "read /desktop", "/desktopx", "/desktop2 go", "/desktops", "/desktop@otherbot go",
    "/desktop@dombotx", "/desktop:", "/desktop_now", " /desktop",
])
async def test_documents_without_a_desktop_caption_are_just_chat(make_app, monkeypatch, caption):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    app.sandbox = Sandbox()
    adopt(app, DesktopWorker(app, app.sandbox))
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


@pytest.mark.parametrize("caption", ["/desktop", "/desktop read it", "/Desktop\nread it", "/desktop@dombot read", "/desktop@DomBot"])
async def test_the_exact_desktop_command_stages_the_document(make_app, monkeypatch, caption):
    app = make_app()
    app.home = Home(app.home.root / 'home')
    app.sandbox = Sandbox()
    adopt(app, DesktopWorker(app, app.sandbox))
    app.config = app.config.model_copy(update={"desktop_mode": "local"})
    downloads = []

    async def download(document, *, destination):
        downloads.append(document.file_id)
        destination.write(b"data")
        return destination

    monkeypatch.setattr(app.bot, "download", download, raising=False)
    document = Document(file_id="file", file_unique_id="unique", file_name="a.pdf", file_size=4)
    await handle_message(app, tg(OWNER, OWNER, text=None, caption=caption, document=document))
    assert downloads == ["file"]


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
    adopt(app, DesktopWorker(app, app.sandbox))
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
    assert commands == [f"rm -rf /home/gem/inbox-{task_id}"]
    assert (await task_row(app, task_id))["status"] == ("done" if finish else "failed")


@pytest.mark.parametrize(("result", "logged"), [
    ({"exit_code": 0}, False), ({"exit_code": 1}, True), ({}, True), (None, True), ("odd", True),
])
async def test_inbox_removal_exit_code_is_verified(make_app, caplog, result, logged):
    app = make_app()
    worker = await worker_for(app, call("finish", result="Done"), {"content": "invalid"})

    async def bash(command):
        return result

    worker.sandbox.client = SimpleNamespace(bash=bash)
    await worker.enqueue(OWNER, "Example")
    await wait_task(worker)
    assert ("could not remove the desktop inbox" in caplog.text) is logged


async def test_images_go_as_photos_and_fall_back_to_files(make_app):
    # The owner had to tap Download to see a page screenshot; a photo opens inline.
    from aiogram.exceptions import TelegramBadRequest

    from dom.actions import SendFile
    from dom.telegram import executor

    app = make_app()
    sent = []

    class Bot:
        async def send_photo(self, chat_id, photo):
            if photo.filename == "long-page.png":
                raise TelegramBadRequest(method=None, message="PHOTO_INVALID_DIMENSIONS")
            sent.append(("photo", photo.filename))

        async def send_document(self, chat_id, document):
            sent.append(("document", document.filename))

    files = [
        SendFile("page.png", b"png"), SendFile("long-page.png", b"png"), SendFile("notes.txt", b"txt"),
        SendFile("huge.jpg", b"x" * (executor.PHOTO_MAX_BYTES + 1)),
    ]
    await executor.send_files(Bot(), app.store, OWNER, files)
    assert sent == [("photo", "page.png"), ("document", "long-page.png"), ("document", "notes.txt"), ("document", "huge.jpg")]
