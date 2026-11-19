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
