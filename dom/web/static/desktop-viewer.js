import RFB from "/static/novnc/core/rfb.js";
import { initLogging } from "/static/novnc/core/util/logging.js";

initLogging("warn"); // noVNC logs each keysym at "info": typed passwords must never reach the console

const body = document.body;
const query = body.dataset.query || (
    new URLSearchParams(window.location.search).get("container")
        ? `?${new URLSearchParams({ container: new URLSearchParams(window.location.search).get("container") })}`
        : ""
);
const scheme = window.location.protocol === "https:" ? "wss" : "ws";
const wsUrl = `${scheme}://${window.location.host}/desktop/vnc/websockify${query}`;
const isEmbedded = window.self !== window.top;

const stateEl = document.getElementById("state");
const stateDot = document.getElementById("state-dot");
const liveHostEl = document.getElementById("live-host");
const countdownEl = document.getElementById("countdown");
const doneBtn = document.getElementById("done-btn");
const fullscreenBtn = document.getElementById("fullscreen-btn");
const backLink = document.getElementById("back-link");
const openFullLink = document.getElementById("open-full");
const handoffBanner = document.getElementById("handoff-banner");
const handoffReasonEl = document.getElementById("handoff-reason");
const viewport = document.getElementById("viewport");
const screen = document.getElementById("screen");
const reconnectBar = document.getElementById("reconnect-bar");
const reconnectMsg = document.getElementById("reconnect-msg");
const reconnectBtn = document.getElementById("reconnect-btn");
const toastEl = document.getElementById("toast");

const modeScrollBtn = document.getElementById("mode-scroll");
const modePanBtn = document.getElementById("mode-pan");
const zoomOutBtn = document.getElementById("zoom-out");
const zoomResetBtn = document.getElementById("zoom-reset");
const zoomInBtn = document.getElementById("zoom-in");
const phoneModeBtn = document.getElementById("phone-mode-btn");
const copyBtn = document.getElementById("copy-btn");
const pasteBtn = document.getElementById("paste-btn");
const keysToggleBtn = document.getElementById("keys-toggle");
const keybar = document.getElementById("keybar");
const ctrlToggleBtn = document.getElementById("ctrl-toggle");

const form = document.getElementById("type");
const field = document.getElementById("type-text");
const secret = document.getElementById("type-secret");

if (isEmbedded) {
    backLink.hidden = true;
    openFullLink.hidden = false;
}

// ---- X11 keysyms ----
const KEYS = {
    Enter: [0xff0d, "Enter"],
    Tab: [0xff09, "Tab"],
    BackSpace: [0xff08, "Backspace"],
    Escape: [0xff1b, "Escape"],
    ArrowLeft: [0xff51, "ArrowLeft"],
    ArrowUp: [0xff52, "ArrowUp"],
    ArrowRight: [0xff53, "ArrowRight"],
    ArrowDown: [0xff54, "ArrowDown"],
    ControlLeft: [0xffe3, "ControlLeft"],
};

const charKeysym = (char) => {
    const point = char.codePointAt(0);
    if (char === "\n") return KEYS.Enter[0];
    return (point >= 0x20 && point <= 0x7e) || (point >= 0xa0 && point <= 0xff) ? point : 0x01000000 + point;
};

// ---- Toast feedback ----
let toastTimer = null;
function showToast(text, ms = 2400) {
    if (!text) return;
    toastEl.textContent = text;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, ms);
}

// ---- Dynamic viewport for on-screen keyboard ----
function syncVisualViewport() {
    const vv = window.visualViewport;
    if (vv) {
        document.documentElement.style.setProperty("--vvh", `${Math.round(vv.height)}px`);
        const kbdOpen = window.innerHeight - vv.height > 120;
        body.dataset.kbdOpen = kbdOpen ? "true" : "false";
    } else {
        body.dataset.kbdOpen = "false";
    }
    applyZoomPan();
}

if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", syncVisualViewport);
    window.visualViewport.addEventListener("scroll", syncVisualViewport);
}
window.addEventListener("resize", syncVisualViewport);

// ---- Local zoom & pan (sizes #screen directly so noVNC maps coordinates accurately) ----
const MIN_ZOOM = 1;
const MAX_ZOOM = 4;
let zoom = 1;
let panX = 0;
let panY = 0;
let inputMode = "tap"; // "tap" | "scroll" | "pan"

function clampPan() {
    const vw = viewport.clientWidth || 1;
    const vh = viewport.clientHeight || 1;
    const maxX = Math.max(0, Math.round(vw * zoom) - vw);
    const maxY = Math.max(0, Math.round(vh * zoom) - vh);
    panX = Math.max(0, Math.min(maxX, panX));
    panY = Math.max(0, Math.min(maxY, panY));
}

function applyZoomPan() {
    const vw = viewport.clientWidth;
    const vh = viewport.clientHeight;
    if (!vw || !vh) return;
    if (zoom <= 1.01) {
        zoom = 1;
        panX = 0;
        panY = 0;
        screen.style.width = "";
        screen.style.height = "";
        screen.style.transform = "";
        modePanBtn.hidden = true;
        if (inputMode === "pan") setInputMode("tap");
    } else {
        clampPan();
        screen.style.width = `${Math.round(vw * zoom)}px`;
        screen.style.height = `${Math.round(vh * zoom)}px`;
        screen.style.transform = `translate(${-Math.round(panX)}px, ${-Math.round(panY)}px)`;
        modePanBtn.hidden = false;
    }
    zoomResetBtn.textContent = `${Math.round(zoom * 100)}%`;
}

function setZoom(nextZoom, anchorX, anchorY) {
    const clamped = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, nextZoom));
    const vw = viewport.clientWidth || 1;
    const vh = viewport.clientHeight || 1;
    const ax = anchorX ?? vw / 2;
    const ay = anchorY ?? vh / 2;
    const ratio = clamped / zoom;
    panX = (panX + ax) * ratio - ax;
    panY = (panY + ay) * ratio - ay;
    zoom = clamped;
    applyZoomPan();
}

function setInputMode(mode) {
    inputMode = mode;
    modeScrollBtn.setAttribute("aria-pressed", mode === "scroll" ? "true" : "false");
    modePanBtn.setAttribute("aria-pressed", mode === "pan" ? "true" : "false");
}

modeScrollBtn.addEventListener("click", () => setInputMode(inputMode === "scroll" ? "tap" : "scroll"));
modePanBtn.addEventListener("click", () => setInputMode(inputMode === "pan" ? "tap" : "pan"));
zoomInBtn.addEventListener("click", () => setZoom(zoom + 0.5));
zoomOutBtn.addEventListener("click", () => setZoom(zoom - 0.5));
zoomResetBtn.addEventListener("click", () => setZoom(1));

// ---- Capture-phase touch handling: local pinch-zoom/pan + one-finger scroll ----
let pinchState = null; // { dist, midX, midY }
let singleTouch = null; // { x, y, startX, startY, moved, mode }

function vncCanvas() {
    return screen.querySelector("canvas");
}

function dispatchRemoteWheel(clientX, clientY, deltaX, deltaY) {
    const canvas = vncCanvas();
    if (!canvas) return;
    canvas.dispatchEvent(new WheelEvent("wheel", {
        clientX,
        clientY,
        deltaX,
        deltaY,
        deltaMode: 0,
        bubbles: true,
        cancelable: true,
    }));
}

function dispatchRemoteClick(clientX, clientY) {
    const canvas = vncCanvas();
    if (!canvas) return;
    canvas.dispatchEvent(new MouseEvent("mousedown", { clientX, clientY, buttons: 1, button: 0, bubbles: true, cancelable: true }));
    canvas.dispatchEvent(new MouseEvent("mouseup", { clientX, clientY, buttons: 0, button: 0, bubbles: true, cancelable: true }));
}

viewport.addEventListener("touchstart", (event) => {
    if (event.touches.length >= 2) {
        // Intercept pinch/2-finger drag before noVNC so the remote browser never zooms.
        event.preventDefault();
        event.stopImmediatePropagation();
        singleTouch = null;
        const [a, b] = event.touches;
        const rect = viewport.getBoundingClientRect();
        pinchState = {
            dist: Math.hypot(b.clientX - a.clientX, b.clientY - a.clientY) || 1,
            midX: (a.clientX + b.clientX) / 2 - rect.left,
            midY: (a.clientY + b.clientY) / 2 - rect.top,
        };
        return;
    }
    if (pinchState) {
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
    }
    if (event.touches.length === 1 && (inputMode === "scroll" || (inputMode === "pan" && zoom > 1))) {
        event.preventDefault();
        event.stopImmediatePropagation();
        const t = event.touches[0];
        singleTouch = { x: t.clientX, y: t.clientY, startX: t.clientX, startY: t.clientY, moved: false, mode: inputMode };
    }
}, { capture: true, passive: false });

viewport.addEventListener("touchmove", (event) => {
    if (pinchState && event.touches.length >= 2) {
        event.preventDefault();
        event.stopImmediatePropagation();
        const [a, b] = event.touches;
        const rect = viewport.getBoundingClientRect();
        const dist = Math.hypot(b.clientX - a.clientX, b.clientY - a.clientY) || 1;
        const midX = (a.clientX + b.clientX) / 2 - rect.left;
        const midY = (a.clientY + b.clientY) / 2 - rect.top;
        panX -= midX - pinchState.midX;
        panY -= midY - pinchState.midY;
        const factor = dist / pinchState.dist;
        pinchState = { dist, midX, midY };
        setZoom(zoom * factor, midX, midY);
        return;
    }
    if (pinchState) {
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
    }
    if (singleTouch && event.touches.length === 1) {
        event.preventDefault();
        event.stopImmediatePropagation();
        const t = event.touches[0];
        const dx = t.clientX - singleTouch.x;
        const dy = t.clientY - singleTouch.y;
        if (!singleTouch.moved && Math.hypot(t.clientX - singleTouch.startX, t.clientY - singleTouch.startY) >= 10) {
            singleTouch.moved = true;
        }
        singleTouch.x = t.clientX;
        singleTouch.y = t.clientY;
        if (singleTouch.mode === "pan") {
            panX -= dx;
            panY -= dy;
            applyZoomPan();
        } else if (singleTouch.mode === "scroll" && singleTouch.moved) {
            dispatchRemoteWheel(singleTouch.startX, singleTouch.startY, -dx * 1.4, -dy * 1.4);
        }
    }
}, { capture: true, passive: false });

function endTouch(event) {
    if (pinchState) {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (event.touches.length === 0) pinchState = null;
        return;
    }
    if (singleTouch) {
        event.preventDefault();
        event.stopImmediatePropagation();
        const finished = singleTouch;
        if (event.touches.length === 0) singleTouch = null;
        if (!finished.moved && finished.mode === "scroll") {
            dispatchRemoteClick(finished.startX, finished.startY);
            schedulePasswordCheck();
        }
        return;
    }
    schedulePasswordCheck();
}

viewport.addEventListener("touchend", endTouch, { capture: true, passive: false });
viewport.addEventListener("touchcancel", endTouch, { capture: true, passive: false });
viewport.addEventListener("mouseup", () => schedulePasswordCheck());

// ---- noVNC connection with auto-reconnect ----
let rfb = null;
let connected = false;
let reconnectTimer = null;
let reconnectDelay = 1000;
let remoteClipboard = "";

function setConnState(state, label) {
    stateDot.dataset.state = state;
    stateEl.textContent = label;
}

function clearReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
}

function scheduleReconnect(reason) {
    clearReconnect();
    const seconds = Math.round(reconnectDelay / 1000);
    setConnState("disconnected", `Reconnecting (${seconds}s)`);
    reconnectMsg.textContent = `${reason}. Reconnecting in ${seconds}s…`;
    reconnectBar.hidden = false;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectVnc();
    }, reconnectDelay);
    reconnectDelay = Math.min(10000, reconnectDelay * 2);
}

function connectVnc() {
    clearReconnect();
    if (rfb) {
        try { rfb.disconnect(); } catch { /* ignore */ }
        rfb = null;
    }
    screen.replaceChildren();
    connected = false;
    setConnState("connecting", "Connecting");
    reconnectBar.hidden = true;

    const instance = new RFB(screen, wsUrl);
    instance.scaleViewport = true;
    instance.resizeSession = false;
    instance.clipViewport = false;
    rfb = instance;

    instance.addEventListener("connect", () => {
        if (rfb !== instance) return;
        connected = true;
        reconnectDelay = 1000;
        setConnState("connected", "Connected");
        reconnectBar.hidden = true;
        applyZoomPan();
        checkPasswordFocus();
    });

    instance.addEventListener("disconnect", (event) => {
        if (rfb !== instance) return;
        connected = false;
        const reason = event.detail.clean ? "Disconnected" : "Connection closed";
        scheduleReconnect(reason);
    });

    instance.addEventListener("credentialsrequired", () => {
        if (rfb !== instance) return;
        setConnState("disconnected", "Password required");
    });

    // Store remote clipboard in memory only — never write automatically to the device clipboard.
    instance.addEventListener("clipboard", (event) => {
        if (rfb !== instance) return;
        remoteClipboard = typeof event.detail?.text === "string" ? event.detail.text : "";
        copyBtn.dataset.hasText = remoteClipboard ? "true" : "false";
    });
}

reconnectBtn.addEventListener("click", () => {
    reconnectDelay = 1000;
    connectVnc();
});

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
        pollStatus();
        if (!connected) {
            reconnectDelay = 1000;
            connectVnc();
        } else {
            checkPasswordFocus();
        }
    }
});

window.addEventListener("online", () => {
    if (!connected) {
        reconnectDelay = 1000;
        connectVnc();
    }
});

// ---- Key bar & Ctrl combos ----
let ctrlLatched = false;

function setCtrlLatched(on) {
    ctrlLatched = on;
    ctrlToggleBtn.setAttribute("aria-pressed", on ? "true" : "false");
}

function sendRemoteKey(keysym, code) {
    if (!rfb || !connected) return;
    if (ctrlLatched) {
        rfb.sendKey(KEYS.ControlLeft[0], KEYS.ControlLeft[1], true);
        rfb.sendKey(keysym, code, true);
        rfb.sendKey(keysym, code, false);
        rfb.sendKey(KEYS.ControlLeft[0], KEYS.ControlLeft[1], false);
        setCtrlLatched(false);
    } else {
        rfb.sendKey(keysym, code);
    }
}

function sendCtrlCombo(letter) {
    if (!rfb || !connected) return;
    const code = `Key${letter.toUpperCase()}`;
    const sym = letter.toLowerCase().codePointAt(0);
    rfb.sendKey(KEYS.ControlLeft[0], KEYS.ControlLeft[1], true);
    rfb.sendKey(sym, code, true);
    rfb.sendKey(sym, code, false);
    rfb.sendKey(KEYS.ControlLeft[0], KEYS.ControlLeft[1], false);
    setCtrlLatched(false);
}

ctrlToggleBtn.addEventListener("click", () => setCtrlLatched(!ctrlLatched));

for (const button of keybar.querySelectorAll("button[data-key]")) {
    button.addEventListener("click", () => {
        const entry = KEYS[button.dataset.key];
        if (!entry) return;
        sendRemoteKey(entry[0], entry[1]);
        if (button.dataset.key === "Tab" || button.dataset.key === "Enter") {
            schedulePasswordCheck();
        }
    });
}

for (const button of keybar.querySelectorAll("button[data-combo]")) {
    button.addEventListener("click", () => sendCtrlCombo(button.dataset.combo));
}

keysToggleBtn.addEventListener("click", () => {
    const next = keybar.hidden;
    keybar.hidden = !next;
    keysToggleBtn.setAttribute("aria-expanded", next ? "true" : "false");
    applyZoomPan();
});

// ---- Explicit Copy / Paste buttons ----
copyBtn.addEventListener("click", async () => {
    if (!remoteClipboard) {
        showToast("Remote clipboard is empty — use Ctrl+C on the desktop first.");
        return;
    }
    try {
        await navigator.clipboard.writeText(remoteClipboard);
        showToast("Copied from desktop.");
    } catch {
        showToast("Browser blocked clipboard access.");
    }
});

pasteBtn.addEventListener("click", async () => {
    if (!rfb || !connected) {
        showToast("Not connected yet.");
        return;
    }
    try {
        const text = await navigator.clipboard.readText();
        if (!text) {
            showToast("Local clipboard is empty.");
            return;
        }
        rfb.clipboardPasteFrom(text);
        setTimeout(() => sendCtrlCombo("v"), 80);
        showToast("Pasted into desktop.");
    } catch {
        field.focus();
        showToast("Paste into the Type box and tap Send.");
    }
});

// ---- Type field, Password toggle & Auto password mode ----
let manualSecret = false;
let lastRemotePassword = false;
let passwordCheckInFlight = false;
let passwordCheckTimer = null;

function applyPasswordUi(enabled, { focus = false } = {}) {
    secret.checked = enabled;
    field.type = enabled ? "password" : "text";
    field.name = enabled ? "password" : "username";
    field.autocomplete = enabled ? "current-password" : "username";
    field.placeholder = enabled ? "Password (sent over VNC)" : "Type into the desktop";
    body.dataset.passwordMode = enabled ? "true" : "false";
    if (focus) field.focus();
}

secret.addEventListener("change", () => {
    manualSecret = true;
    applyPasswordUi(secret.checked, { focus: true });
});

form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!rfb || !connected) {
        showToast("Not connected yet.");
        return;
    }
    const text = field.value;
    if (ctrlLatched && text.length === 1) {
        sendCtrlCombo(text);
    } else {
        for (const char of text) rfb.sendKey(charKeysym(char));
    }
    field.value = "";
    manualSecret = false;
    if (secret.checked) {
        applyPasswordUi(false);
    }
    schedulePasswordCheck();
});

async function checkPasswordFocus() {
    if (passwordCheckInFlight || !connected || document.visibilityState !== "visible") return;
    passwordCheckInFlight = true;
    try {
        const response = await fetch(`/desktop/viewer/password-focus${query}`, { cache: "no-store" });
        if (!response.ok) return;
        const data = await response.json();
        const focused = data && data.password === true;
        if (focused && !lastRemotePassword) {
            manualSecret = false;
            if (!secret.checked) {
                applyPasswordUi(true);
                showToast("Password field focused — tap the Password box to type or autofill.");
            }
        } else if (!focused && lastRemotePassword) {
            if (!manualSecret && secret.checked && field.value === "") {
                applyPasswordUi(false);
            }
        }
        lastRemotePassword = focused;
    } catch {
        // Ignore transient network errors during polling.
    } finally {
        passwordCheckInFlight = false;
    }
}

function schedulePasswordCheck() {
    clearTimeout(passwordCheckTimer);
    passwordCheckTimer = setTimeout(checkPasswordFocus, 250);
}

// ---- Phone mode (remote screen resize via sandbox API) ----
async function togglePhoneMode() {
    const currentlyPhone = phoneModeBtn.getAttribute("aria-pressed") === "true";
    const nextMode = currentlyPhone ? "desktop" : "phone";
    phoneModeBtn.disabled = true;
    showToast(nextMode === "phone" ? "Switching to phone mode (800×600)…" : "Restoring desktop (1280×1024)…");
    try {
        const response = await fetch(`/desktop/viewer/screen${query}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: nextMode }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            showToast(data.detail || "Could not resize screen.");
            return;
        }
        phoneModeBtn.setAttribute("aria-pressed", data.phone_mode ? "true" : "false");
        setZoom(1);
        showToast(data.phone_mode ? `Phone mode (${data.resolution})` : `Desktop (${data.resolution})`);
    } catch {
        showToast("Could not reach Dom to resize screen.");
    } finally {
        pollStatus();
    }
}

phoneModeBtn.addEventListener("click", togglePhoneMode);

// ---- Status polling, live host, countdown & Done ----
let currentHandoff = null;
let countdownSeconds = null;

if (body.dataset.handoffTask) {
    currentHandoff = {
        task_id: Number(body.dataset.handoffTask),
        steps: Number(body.dataset.handoffSteps || 0),
        reason: handoffReasonEl.textContent || "",
    };
    if (body.dataset.handoffRemaining !== undefined && body.dataset.handoffRemaining !== "") {
        countdownSeconds = Number(body.dataset.handoffRemaining);
    }
}

function renderCountdown() {
    if (currentHandoff === null || countdownSeconds === null || Number.isNaN(countdownSeconds)) {
        countdownEl.hidden = true;
        return;
    }
    const clamped = Math.max(0, Math.floor(countdownSeconds));
    const mins = Math.floor(clamped / 60);
    const secs = clamped % 60;
    countdownEl.textContent = `${mins}:${String(secs).padStart(2, "0")}`;
    countdownEl.dataset.urgent = clamped <= 120 ? "true" : "false";
    countdownEl.hidden = false;
}

function applyStatus(data) {
    if (!data || typeof data !== "object") return;
    liveHostEl.textContent = typeof data.live_host === "string" ? data.live_host : "";
    phoneModeBtn.setAttribute("aria-pressed", data.phone_mode ? "true" : "false");
    phoneModeBtn.disabled = !data.can_resize;

    if (data.handoff && typeof data.handoff === "object") {
        currentHandoff = data.handoff;
        countdownSeconds = typeof data.handoff.remaining_seconds === "number" ? data.handoff.remaining_seconds : null;
        doneBtn.hidden = false;
        doneBtn.disabled = false;
        doneBtn.textContent = "Done";
        handoffReasonEl.textContent = data.handoff.reason || "Dom needs your help here.";
        handoffBanner.hidden = false;
    } else {
        currentHandoff = null;
        countdownSeconds = null;
        doneBtn.hidden = true;
        handoffBanner.hidden = true;
    }
    renderCountdown();
}

async function pollStatus() {
    if (document.visibilityState !== "visible") return;
    try {
        const response = await fetch(`/desktop/viewer/status${query}`, { cache: "no-store" });
        if (!response.ok) return;
        applyStatus(await response.json());
    } catch {
        // Ignore transient network errors.
    }
}

doneBtn.addEventListener("click", async () => {
    if (!currentHandoff) return;
    doneBtn.disabled = true;
    doneBtn.textContent = "Resuming…";
    // Clear remote clipboard on the VNC side as well (server also clears X11 clipboard and restores 1280x1024).
    remoteClipboard = "";
    copyBtn.dataset.hasText = "false";
    if (rfb && connected) {
        try { rfb.clipboardPasteFrom(""); } catch { /* ignore */ }
    }
    try {
        const response = await fetch("/desktop/viewer/done", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ task_id: currentHandoff.task_id, steps: currentHandoff.steps }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            doneBtn.disabled = false;
            doneBtn.textContent = "Done";
            showToast(data.detail || "Could not resume task.");
            return;
        }
        showToast(data.message || "Task resumed.", 4000);
        await pollStatus();
    } catch {
        doneBtn.disabled = false;
        doneBtn.textContent = "Done";
        showToast("Could not reach Dom.");
    }
});

// ---- Fullscreen ----
const canFullscreen = Boolean(document.fullscreenEnabled || document.documentElement.webkitRequestFullscreen);
if (!canFullscreen) {
    fullscreenBtn.hidden = true;
} else {
    fullscreenBtn.addEventListener("click", async () => {
        try {
            if (document.fullscreenElement || document.webkitFullscreenElement) {
                await (document.exitFullscreen ? document.exitFullscreen() : document.webkitExitFullscreen());
            } else {
                const el = document.documentElement;
                await (el.requestFullscreen ? el.requestFullscreen() : el.webkitRequestFullscreen());
            }
        } catch {
            showToast("Fullscreen unavailable here.");
        }
    });
    document.addEventListener("fullscreenchange", () => {
        fullscreenBtn.setAttribute("aria-pressed", document.fullscreenElement ? "true" : "false");
        applyZoomPan();
    });
}

// ---- Boot ----
renderCountdown();
syncVisualViewport();
connectVnc();
pollStatus();

setInterval(() => {
    if (currentHandoff !== null && countdownSeconds !== null && countdownSeconds > 0) {
        countdownSeconds -= 1;
        renderCountdown();
    }
}, 1000);

setInterval(pollStatus, 4000);
setInterval(checkPasswordFocus, 1500);
