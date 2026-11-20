import RFB from "/static/novnc/core/rfb.js";

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
