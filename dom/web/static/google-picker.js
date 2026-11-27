// The Google Picker page (v0.8.6 W4). It gets a drive.file-only token from Google Identity Services in this browser,
// opens Google's file picker, and reports the pick to Dom's server, which asks Google whether Dom can now see the file.
const SCOPE = "https://www.googleapis.com/auth/drive.file";
const { clientId, apiKey, project, file } = document.body.dataset;
const button = document.getElementById("choose");
const status = document.getElementById("status");
const handed = document.getElementById("handed");

function load(src) {
    return new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = src;
        script.onload = resolve;
        script.onerror = () => reject(new Error("Could not load Google's scripts."));
        document.head.append(script);
    });
}

function say(text) {
    status.textContent = text;
}

async function report(ids) {
    say("Telling Dom…");
    const response = await fetch("/api/integrations/google/files/picked", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        say(typeof data.detail === "string" ? data.detail : "Dom could not check that.");
        return;
    }
    const files = Array.isArray(data.files) ? data.files : [];
    for (const item of files) {
        const row = document.createElement("li");
        row.textContent = `${item.name} (${item.kind})`;
        handed.append(row);
    }
    say(files.length ? "Done. Dom can read these now." : "Google did not give Dom that file. Try again.");
}

function openPicker(token) {
    const picker = google.picker;
    const builder = new picker.PickerBuilder()
        .setOAuthToken(token)
        .setDeveloperKey(apiKey)
        .setAppId(project)
        .setCallback((data) => {
            if (data[picker.Response.ACTION] === picker.Action.PICKED) {
                const ids = (data[picker.Response.DOCUMENTS] || []).map((doc) => doc[picker.Document.ID]);
                if (ids.length) void report(ids).catch(() => say("Dom could not check that."));
            }
        });
    // The linked file first, when there is one; then the owner's Drive, for any other.
    if (file) builder.addView(new picker.DocsView(picker.ViewId.DOCS).setMode(picker.DocsViewMode.LIST).setFileIds(file));
    builder.addView(new picker.DocsView(picker.ViewId.DOCS).setMode(picker.DocsViewMode.LIST));
    builder.build().setVisible(true);
}

async function start() {
    if (!button) return;
    try {
        await Promise.all([load("https://apis.google.com/js/api.js"), load("https://accounts.google.com/gsi/client")]);
        await new Promise((resolve) => gapi.load("picker", { callback: resolve }));
    } catch (error) {
        say(error.message);
        button.textContent = "Google did not load";
        return;
    }
    const client = google.accounts.oauth2.initTokenClient({
        client_id: clientId,
        scope: SCOPE,
        callback: (response) => {
            if (response.error || !response.access_token) {
                say("Google did not give access. Try again.");
                return;
            }
            say("");
            openPicker(response.access_token);
        },
        error_callback: () => say("The Google window was closed or blocked."),
    });
    button.disabled = false;
    button.textContent = file ? "Choose the file in Google Drive" : "Choose a file in Google Drive";
    // The popup must open from this click, which is why everything above is loaded first.
    button.addEventListener("click", () => client.requestAccessToken({ prompt: "" }));
}

void start();
