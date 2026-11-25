<script lang="ts">
  import type { Component, Snippet } from "svelte";
  import { Button, type ButtonProps, type ButtonSize, type ButtonVariant } from "$lib/components/ui/button";
  import * as Tooltip from "$lib/components/ui/tooltip";
  import { tipTrigger } from "$lib/utils";

  // Every icon-only button: the label is required, and it is both the accessible name and the tooltip shown on
  // hover and keyboard focus (E78). A shortcut, when there is one, shows in the tooltip and is announced.
  let {
    label,
    icon: Icon,
    variant = "ghost",
    size = "icon-sm",
    side = "top",
    shortcut,
    tip,
    class: className,
    ref = $bindable(null),
    ...rest
  }: Omit<ButtonProps, "aria-label" | "children" | "variant" | "size" | "ref"> & {
    label: string;
    icon: Component;
    variant?: ButtonVariant;
    size?: ButtonSize;
    side?: "top" | "right" | "bottom" | "left";
    shortcut?: string;
    /** Richer tooltip content; the label stays the accessible name. */
    tip?: Snippet;
    ref?: HTMLElement | null;
  } = $props();
</script>

<Tooltip.Root>
  <Tooltip.Trigger>
    {#snippet child({ props })}
      <Button {...tipTrigger(props, rest, !tip)} bind:ref {variant} {size} class={className} aria-label={label} aria-keyshortcuts={shortcut}>
        <Icon aria-hidden="true" />
      </Button>
    {/snippet}
  </Tooltip.Trigger>
  <Tooltip.Content {side}>
    {#if tip}{@render tip()}{:else}{label}{/if}
    {#if shortcut}<kbd class="ml-1 font-mono opacity-70">{shortcut}</kbd>{/if}
  </Tooltip.Content>
</Tooltip.Root>
