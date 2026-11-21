import RFB from "/static/novnc/core/rfb.js";
import { initLogging } from "/static/novnc/core/util/logging.js";

initLogging("warn"); // noVNC logs each keysym at "info": typed passwords must never reach the console

const status = document.getElementById("state");
const scheme = window.location.protocol === "https:" ? "wss" : "ws";
const container = new URLSearchParams(window.location.search).get("container");
const query = container ? `?${new URLSearchParams({ container })}` : "";
const rfb = new RFB(
    document.getElementById("screen"),
    `${scheme}://${window.location.host}/desktop/vnc/websockify${query}`,
);
rfb.scaleViewport = true;
rfb.addEventListener("connect", () => { status.textContent = "Connected"; });
rfb.addEventListener("disconnect", (event) => {
    status.textContent = event.detail.clean ? "Disconnected" : "Connection closed";
});
rfb.addEventListener("credentialsrequired", () => { status.textContent = "The desktop asked for a password"; });

// X keysyms: Latin-1 characters are their own code; any other character is 0x01000000 + its code point.
const KEYS = { Enter: 0xff0d, Tab: 0xff09, BackSpace: 0xff08 };
const keysym = (char) => {
    const point = char.codePointAt(0);
    if (char === "\n") return KEYS.Enter;
    return (point >= 0x20 && point <= 0x7e) || (point >= 0xa0 && point <= 0xff) ? point : 0x01000000 + point;
};

// Typing into the desktop from a real text field: a phone shows its keyboard for it, and in password mode a
// password manager can fill it. The text goes to the desktop as keystrokes only; it is cleared straight after.
const form = document.getElementById("type");
const field = document.getElementById("type-text");
const secret = document.getElementById("type-secret");
secret.addEventListener("change", () => {
    field.type = secret.checked ? "password" : "text";
    field.name = secret.checked ? "password" : "username";
    field.autocomplete = secret.checked ? "current-password" : "username";
    field.focus();
});
form.addEventListener("submit", (event) => {
    event.preventDefault();
    for (const char of field.value) rfb.sendKey(keysym(char));
    field.value = "";
    if (secret.checked) {
        secret.checked = false;
        secret.dispatchEvent(new Event("change"));
    }
});
for (const button of form.querySelectorAll("button[data-key]")) {
    button.addEventListener("click", () => rfb.sendKey(KEYS[button.dataset.key]));
}
