"use strict";

function providerFields(row) {
  return {
    base_url: row.querySelector("[name=base_url]").value,
    model: row.querySelector("[name=model]").value,
    api_key_env: row.querySelector("[name=api_key_env]").value || null,
    reasoning_effort: row.querySelector("[name=reasoning_effort]").value || null,
  };
}

async function sendModelSettings(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.status === 403) throw new Error("Dashboard access expired. Reload to sign in.");
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.detail || `Request failed (${response.status}).`);
    if (Array.isArray(result.errors)) error.slotErrors = result.errors;
    throw error;
  }
  return result;
}

function clearSlotErrors(form) {
  for (const node of form.querySelectorAll("[data-slot-error]")) node.remove();
}

function showSlotErrors(form, slotErrors) {
  const rows = form.querySelectorAll("[data-provider]");
  for (const item of slotErrors) {
    const row = rows[item.index];
    if (!row) continue;
    const message = document.createElement("p");
    message.className = "error";
    message.dataset.slotError = "";
    message.setAttribute("role", "alert");
    message.textContent = item.error;
    row.append(message);
  }
}

function editModelSettings(form) {
  form.dataset.dirty = "true";
  clearSlotErrors(form);
  const providers = [...form.querySelectorAll("[data-provider]")];
  providers.forEach((row, index) => {
    row.querySelector("legend").textContent = index === 0 ? "Primary" : `Fallback ${index}`;
    row.querySelector("[data-remove]").hidden = index === 0;
    row.querySelector("[data-move-up]").hidden = index < 2;
  });
  form.querySelector("[data-add]").textContent = providers.length ? "Add fallback" : "Set primary";
  const clear = form.querySelector("[data-clear]");
  if (clear) clear.hidden = providers.length === 0;
  for (const button of form.querySelectorAll("[data-test]")) button.disabled = true;
}

document.addEventListener("input", (event) => {
  const form = event.target.closest("#model-settings form");
  if (form) editModelSettings(form);
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("#model-settings button[type=button]");
  if (!button) return;
  const form = button.form;
  const providers = form.querySelector("[data-providers]");
  const row = button.closest("[data-provider]");
  if (button.hasAttribute("data-add")) {
    providers.append(document.querySelector("#provider-fields").content.cloneNode(true));
    providers.lastElementChild.querySelector("input").focus();
  } else if (button.hasAttribute("data-remove")) {
    row.remove();
  } else if (button.hasAttribute("data-move-up")) {
    providers.insertBefore(row, row.previousElementSibling);
    if (!button.hidden) button.focus();
  } else if (button.hasAttribute("data-clear")) {
    providers.replaceChildren();
  } else if (button.hasAttribute("data-test")) {
    const output = form.querySelector("output");
    button.disabled = true;
    output.textContent = "Testing…";
    try {
      const { ok, latency_ms, reply, error } = await sendModelSettings("/api/models/test", {
        revision: form.closest("#model-settings").dataset.revision,
        role: form.dataset.role,
        index: Number(button.dataset.test),
      });
      output.textContent = `${latency_ms} ms · ${ok ? reply : error}`;
    } catch (error) {
      output.textContent = error.message;
    } finally {
      if (!form.dataset.dirty) button.disabled = false;
    }
    return;
  }
  editModelSettings(form);
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("#model-settings form");
  if (!form) return;
  event.preventDefault();
  const panel = form.closest("#model-settings");
  const output = form.querySelector("output");
  const [primary = null, ...fallbacks] = [...form.querySelectorAll("[data-provider]")].map(providerFields);
  const submit = form.querySelector("[type=submit]");
  submit.disabled = true;
  clearSlotErrors(form);
  output.textContent = "Testing and saving…";
  try {
    const settings = await sendModelSettings("/api/models", {
      revision: panel.dataset.revision,
      role: form.dataset.role,
      primary,
      fallbacks,
    });
    panel.dataset.revision = settings.revision;
    // Keep unsaved edits in other roles. Refresh saved fields and test targets in this form only.
    await htmx.ajax("GET", "/settings", {
      target: form,
      select: `form[data-role="${form.dataset.role}"]`,
      swap: "outerHTML",
    });
    const saved = panel.querySelector(`form[data-role="${form.dataset.role}"] output`);
    saved.textContent = settings.restart_required ? "Saved. Restart Dom to apply." : "Saved.";
  } catch (error) {
    if (error.slotErrors?.length) {
      showSlotErrors(form, error.slotErrors);
      output.textContent = "Could not save; fix the provider error(s) above.";
    } else {
      output.textContent = error.message;
    }
  } finally {
    submit.disabled = false;
  }
});
