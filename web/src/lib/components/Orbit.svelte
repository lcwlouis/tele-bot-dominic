<script lang="ts" module>
  export type Activity = "idle" | "replying" | "desktop" | "asleep";

  export const ACTIVITY_LABELS: Record<Activity, string> = {
    idle: "Idle",
    replying: "Thinking or replying",
    desktop: "Working on the desktop",
    asleep: "Asleep",
  };
</script>

<script lang="ts">
  import { cn } from "$lib/utils";

  // The gyroscope: three gimbal rings precess around a glowing core. Its pace and colour follow what Dom is
  // doing. All motion is CSS animation on classes (no inline styles, so Dom's CSP holds), and it stops under
  // prefers-reduced-motion, where colour and brightness alone tell the states apart.
  let { activity = "idle", size = "lg", class: className }: { activity?: Activity; size?: "sm" | "md" | "lg"; class?: string } = $props();
  const uid = $props.id();
</script>

<svg
  viewBox="-100 -100 200 200"
  role="img"
  aria-label={`Dom: ${ACTIVITY_LABELS[activity].toLowerCase()}`}
  data-activity={activity}
  class={cn("orbit", size === "sm" && "size-10", size === "md" && "size-28", size === "lg" && "size-44 sm:size-52", className)}
>
  <defs>
    <radialGradient id="{uid}-core" cx="0" cy="0" r="1" gradientUnits="objectBoundingBox" fx="0.4" fy="0.38">
      <stop offset="0" class="stop-bright" />
      <stop offset="0.55" class="stop-mid" />
      <stop offset="1" class="stop-edge" />
    </radialGradient>
    <radialGradient id="{uid}-glow">
      <stop offset="0" class="stop-glow" />
      <stop offset="1" class="stop-clear" />
    </radialGradient>
  </defs>

  <g class="dial">
    <circle r="94" class="ticks" />
    <circle r="86" class="hairline" />
  </g>
  <circle r="46" class="glow" fill="url(#{uid}-glow)" />

  <g class="gimbal gimbal-a"><ellipse rx="74" ry="74" class="ring" /><circle cx="74" cy="0" r="4.2" class="satellite" /></g>
  <g class="gimbal gimbal-b"><ellipse rx="62" ry="22" class="ring ring-tilted" /></g>
  <g class="gimbal gimbal-c"><ellipse rx="52" ry="52" class="ring ring-inner" /><circle cx="0" cy="-52" r="2.6" class="satellite satellite-small" /></g>

  <circle r="16" class="core" fill="url(#{uid}-core)" />
</svg>

<style>
  .orbit {
    --ring: var(--signal);
    --core: var(--signal-strong);
    --pace: 1;
    overflow: visible;
    flex: none;
  }
  .orbit[data-activity="replying"] {
    --pace: 0.28;
  }
  .orbit[data-activity="desktop"] {
    --ring: var(--amber);
    --pace: 0.6;
  }
  .orbit[data-activity="asleep"] {
    --ring: var(--text-muted);
    --core: var(--text-muted);
    --pace: 3.2;
    opacity: 0.62;
  }

  .stop-bright { stop-color: var(--core); stop-opacity: 1; }
  .stop-mid { stop-color: var(--ring); stop-opacity: 0.65; }
  .stop-edge { stop-color: var(--ring); stop-opacity: 0.08; }
  .stop-glow { stop-color: var(--ring); stop-opacity: 0.32; }
  .stop-clear { stop-color: var(--ring); stop-opacity: 0; }

  .ticks {
    fill: none;
    stroke: var(--ring);
    stroke-opacity: 0.35;
    stroke-width: 1.2;
    stroke-dasharray: 1.2 8.6;
  }
  .hairline { fill: none; stroke: var(--ring); stroke-opacity: 0.16; stroke-width: 0.8; }
  .ring { fill: none; stroke: var(--ring); stroke-width: 1.4; stroke-opacity: 0.85; }
  .ring-tilted { stroke-opacity: 0.55; }
  .ring-inner { stroke-opacity: 0.4; stroke-dasharray: 3 5; }
  .gimbal-b { transform: rotate(-24deg); }
  .satellite { fill: var(--core); }
  .satellite-small { fill: var(--ring); }

  /* Every rotating group turns about the SVG's centre (0,0). */
  .gimbal,
  .dial,
  .glow,
  .core {
    transform-box: view-box;
    transform-origin: 0 0;
  }

  @media (prefers-reduced-motion: no-preference) {
    .gimbal-a { animation: spin calc(26s * var(--pace)) linear infinite; }
    .gimbal-b { animation: precess calc(18s * var(--pace)) ease-in-out infinite; }
    .gimbal-c { animation: spin calc(40s * var(--pace)) linear infinite reverse; }
    .dial { animation: spin calc(120s * var(--pace)) linear infinite; }
    .glow { animation: breathe calc(6s * var(--pace)) ease-in-out infinite; }
    .core { animation: pulse calc(6s * var(--pace)) ease-in-out infinite; }
    .orbit[data-activity="replying"] .glow { animation-duration: 1.6s; }
    .orbit[data-activity="replying"] .core { animation-duration: 1.6s; }
    .orbit[data-activity="desktop"] .ticks { stroke-opacity: 0.6; }
  }

  .orbit[data-activity="replying"] .glow { opacity: 1; }
  .orbit[data-activity="asleep"] .glow { opacity: 0.35; }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }
  /* The tilted ring swings through its plane, which reads as a gimbal turning in 3D. */
  @keyframes precess {
    0% { transform: rotate(-24deg) scaleY(1); }
    50% { transform: rotate(156deg) scaleY(-0.35); }
    100% { transform: rotate(336deg) scaleY(1); }
  }
  @keyframes breathe {
    0%, 100% { transform: scale(0.88); opacity: 0.7; }
    50% { transform: scale(1.08); opacity: 1; }
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1); }
    50% { transform: scale(1.12); }
  }
</style>
