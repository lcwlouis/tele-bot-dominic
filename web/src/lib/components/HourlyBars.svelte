<script lang="ts">
  // Bars drawn as SVG rects: their sizes are attributes, not inline styles, so Dom's CSP allows them.
  let { counts, label }: { counts: number[]; label: string } = $props();
  const max = $derived(Math.max(1, ...counts));
  const peak = $derived(counts.indexOf(Math.max(...counts)));
  const summary = $derived(
    counts.some((count) => count > 0) ? `${label}. Busiest around ${String(peak).padStart(2, "0")}:00.` : `${label}. Not enough yet.`,
  );
</script>

<figure class="mt-5">
  <svg viewBox="0 0 240 48" preserveAspectRatio="none" class="h-14 w-full" role="img" aria-label={summary}>
    {#each counts as count, hour (hour)}
      {@const height = count ? Math.max(3, (44 * count) / max) : 1.5}
      <rect
        x={hour * 10 + 1.5}
        y={48 - height}
        width="7"
        height={height}
        rx="1.5"
        class={hour === peak && count ? "fill-signal" : "fill-signal/35"}
      />
    {/each}
  </svg>
  <figcaption class="mt-1.5 flex justify-between font-mono text-[0.68rem] tracking-[0.1em] text-muted-foreground uppercase">
    <span>00:00</span><span>{label}</span><span>23:00</span>
  </figcaption>
</figure>
