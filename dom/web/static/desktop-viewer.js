import RFB from "/static/novnc/core/rfb.js";

const status = document.getElementById("status");
const scheme = window.location.protocol === "https:" ? "wss" : "ws";
const rfb = new RFB(
    document.getElementById("screen"),
    `${scheme}://${window.location.host}/desktop/vnc/websockify`,
);
rfb.scaleViewport = true;
rfb.addEventListener("connect", () => { status.textContent = "Connected"; });
rfb.addEventListener("disconnect", (event) => {
    status.textContent = event.detail.clean ? "Disconnected" : "Connection closed";
});
rfb.addEventListener("credentialsrequired", () => { status.textContent = "The desktop asked for a password"; });
