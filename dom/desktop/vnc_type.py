"""Minimal async RFB 3.8 keystroke injector over the sandbox's `/websockify` WebSocket.

Used by R5 saved-login filling so usernames and passwords NEVER travel through the sandbox HTTP API
(`/v1/browser/page/type`, `/v1/browser/page/fill`, `/v1/browser/page/evaluate`, `/v1/bash/exec`), which
traces requests. This mirrors how the live noVNC viewer already sends manual password keystrokes.
"""

from __future__ import annotations

import asyncio
import struct
from typing import TYPE_CHECKING

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from dom.desktop.client import SandboxError

if TYPE_CHECKING:
    from dom.desktop.client import SandboxClient

_KEY_ENTER = 0xFF0D
_KEY_BACKSPACE = 0xFF08
_KEY_CONTROL_L = 0xFFE3
_KEY_A = 0x0061


class _NoRedirectConnect(connect):
    def process_redirect(self, exc):
        # Upstream auth header belongs only to the configured sandbox.
        return exc


connect_vnc = _NoRedirectConnect


def char_keysym(char: str) -> int:
    """Map one character to its X11 keysym, matching dom/web/static/desktop-viewer.js."""
    if char == "\n":
        return _KEY_ENTER
    point = ord(char)
    if (0x20 <= point <= 0x7E) or (0xA0 <= point <= 0xFF):
        return point
    return 0x01000000 + point


def key_event(keysym: int, down: bool) -> bytes:
    """Build an 8-byte RFB KeyEvent message (message-type 4)."""
    return struct.pack("!BBxxI", 4, 1 if down else 0, keysym)


def key_press(keysym: int) -> bytes:
    return key_event(keysym, True) + key_event(keysym, False)


def encode_typing_frames(text: str, *, clear: bool = True, submit: bool = False) -> list[bytes]:
    """Build the ordered RFB KeyEvent payloads for optional Ctrl+A/BackSpace, typing `text`, and optional Enter."""
    frames: list[bytes] = []
    if clear:
        frames.append(
            key_event(_KEY_CONTROL_L, True)
            + key_press(_KEY_A)
            + key_event(_KEY_CONTROL_L, False)
            + key_press(_KEY_BACKSPACE)
        )
    for char in text:
        frames.append(key_press(char_keysym(char)))
    if submit:
        frames.append(key_press(_KEY_ENTER))
    return frames


class _ByteReader:
    """Read exact byte counts from a binary WebSocket stream."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._buf = bytearray()

    async def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = await self._ws.recv()
            if isinstance(chunk, str):
                chunk = chunk.encode("latin-1")
            if not chunk:
                raise SandboxError("VNC connection closed during handshake")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


async def _rfb_handshake(ws) -> None:
    """Perform an RFB 3.8 (or 3.7/3.3) shared-mode handshake using SecurityType None (1)."""
    reader = _ByteReader(ws)
    server_ver = await reader.read_exact(12)
    if not server_ver.startswith(b"RFB ") or not server_ver.endswith(b"\n"):
        raise SandboxError("invalid VNC greeting")
    minor = server_ver[8:11]
    if minor >= b"008":
        await ws.send(b"RFB 003.008\n")
        count = (await reader.read_exact(1))[0]
        if count == 0:
            raise SandboxError("VNC server rejected handshake")
        sec_types = await reader.read_exact(count)
        if 1 not in sec_types:
            raise SandboxError("VNC server does not allow bearer-authenticated None security")
        await ws.send(b"\x01")
        result = struct.unpack("!I", await reader.read_exact(4))[0]
        if result != 0:
            raise SandboxError("VNC security handshake failed")
    elif minor >= b"007":
        await ws.send(b"RFB 003.007\n")
        count = (await reader.read_exact(1))[0]
        sec_types = await reader.read_exact(count)
        if 1 not in sec_types:
            raise SandboxError("VNC server does not allow None security")
        await ws.send(b"\x01")
    else:
        await ws.send(b"RFB 003.003\n")
        sec = struct.unpack("!I", await reader.read_exact(4))[0]
        if sec != 1:
            raise SandboxError("VNC server requires unsupported security type")

    # ClientInit: shared-flag = 1 so any live viewer session stays connected.
    await ws.send(b"\x01")
    server_init = await reader.read_exact(24)
    name_len = struct.unpack("!I", server_init[20:24])[0]
    if name_len > 4096:
        raise SandboxError("VNC desktop name too long")
    if name_len:
        await reader.read_exact(name_len)


async def vnc_type_text(
    client: SandboxClient,
    text: str,
    *,
    clear: bool = True,
    submit: bool = False,
    timeout: float = 10.0,
) -> None:
    """Inject keystrokes into the sandbox browser strictly over `/websockify` RFB KeyEvents.

    Never logs `text` and sanitizes all errors so no secret byte can ever escape in an exception.
    """
    url = httpx.URL(client.base_url + "/websockify")
    ws_url = str(url.copy_with(scheme="wss" if url.scheme == "https" else "ws"))
    frames = encode_typing_frames(text, clear=clear, submit=submit)
    try:
        async with asyncio.timeout(timeout):
            async with connect_vnc(
                ws_url,
                additional_headers=client.auth_headers(),
                subprotocols=["binary"],
                proxy=None,
                open_timeout=timeout,
                close_timeout=2,
            ) as ws:
                await _rfb_handshake(ws)
                for frame in frames:
                    await ws.send(frame)
    except SandboxError:
        raise
    except (OSError, TimeoutError, WebSocketException, ValueError, RuntimeError):
        raise SandboxError("could not type saved login into the browser") from None
