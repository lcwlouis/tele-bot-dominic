document.addEventListener("htmx:beforeSwap", (event) => {
    const { xhr, requestConfig } = event.detail;
    if (xhr.getResponseHeader("X-Dom-Decided") !== "true" ||
        window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        return;
    }
    const card = requestConfig.elt.closest(".proposal");
    if (!card) {
        return;
    }
    event.detail.swapOverride = "outerHTML swap:180ms";
    card.animate(
        [{ height: `${card.offsetHeight}px`, opacity: 1 },
         { height: 0, opacity: 0, paddingTop: 0, paddingBottom: 0, overflow: "hidden" }],
        { duration: 180, easing: "ease-in", fill: "forwards" },
    );
});

document.addEventListener("input", (event) => {
    if (event.target.id !== "memory-text") {
        return;
    }
    const counter = event.target.form.querySelector(".fill");
    const length = Array.from(event.target.value).length;
    counter.querySelector("[data-count]").textContent = length.toLocaleString("en-US");
    const limit = Number(counter.dataset.limit);
    if (limit) {
        const ring = counter.querySelector(".fill-value");
        ring.setAttribute("stroke-dashoffset", Math.max(0, 1 - length / limit));
        ring.classList.toggle("over", length > limit);
    }
});

document.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target.id !== "desktop-state") {
        return;
    }
    const state = document.getElementById("desktop-state");
    const live = document.getElementById("desktop-live");
    const ready = state.dataset.status === "ready";
    live.hidden = !ready;
    if (ready && !live.hasAttribute("src")) {
        live.setAttribute("src", live.dataset.src);
    } else if (!ready && live.hasAttribute("src")) {
        live.removeAttribute("src");
    }
});

document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy-brief]");
    if (!button) {
        return;
    }
    const card = button.closest("details");
    const source = card.querySelector("textarea");
    const status = card.querySelector("[role=status]");
    try {
        await navigator.clipboard.writeText(source.value);
        status.textContent = "Copied.";
    } catch {
        source.hidden = false;
        source.focus();
        source.select();
        status.textContent = "Copy the selected Markdown.";
    }
});

document.querySelector(".rail [aria-current]")?.scrollIntoView({ inline: "center", block: "nearest" });
