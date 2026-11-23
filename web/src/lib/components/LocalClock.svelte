<script lang="ts">
  import { onMount } from "svelte";

  // Dom's local time, ticking each second. `iso` is Dom's "now" with its UTC offset, as the API sent it; the clock
  // runs on from there, so it shows Dom's time zone whatever this device's zone is.
  let { iso, class: className = "" }: { iso: string; class?: string } = $props();

  const offsetMinutes = (stamp: string) => {
    const match = /([+-])(\d\d):(\d\d)$/.exec(stamp);
    return match ? (match[1] === "-" ? -1 : 1) * (Number(match[2]) * 60 + Number(match[3])) : 0;
  };
  let elapsed = $state(0);
  let since = performance.now();
  $effect(() => {
    void iso; // a fresh answer restarts the count from its own "now"
    since = performance.now();
    elapsed = 0;
  });
  onMount(() => {
    const timer = setInterval(() => (elapsed = performance.now() - since), 1000);
    return () => clearInterval(timer);
  });

  const text = $derived.by(() => {
    const local = new Date(Date.parse(iso) + elapsed + offsetMinutes(iso) * 60_000);
    return [local.getUTCHours(), local.getUTCMinutes(), local.getUTCSeconds()].map((part) => String(part).padStart(2, "0")).join(":");
  });
</script>

<time datetime={iso} data-clock class={className}>{text}</time>
