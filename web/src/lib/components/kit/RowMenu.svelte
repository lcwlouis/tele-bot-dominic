<script lang="ts">
  import Ellipsis from "@lucide/svelte/icons/ellipsis";
  import type { Snippet } from "svelte";
  import { buttonVariants } from "$lib/components/ui/button";
  import * as DropdownMenu from "$lib/components/ui/dropdown-menu";
  import * as Tooltip from "$lib/components/ui/tooltip";
  import { cn, tipTrigger } from "$lib/utils";

  // A row's less common actions (delete, archive) behind one glyph, so a list isn't a column of bins. The label
  // names the row ("Actions for task 12"): it is the button's accessible name and its tooltip. Fill it with
  // DropdownMenu.Item children.
  let { label, disabled = false, children }: { label: string; disabled?: boolean; children: Snippet } = $props();
  let open = $state(false);
</script>

<DropdownMenu.Root bind:open>
  <Tooltip.Root disabled={open}>
    <Tooltip.Trigger>
      {#snippet child({ props: tip })}
        <DropdownMenu.Trigger {disabled}>
          {#snippet child({ props })}
            <button
              {...tipTrigger(tip, props)}
              type="button"
              aria-label={label}
              class={cn(buttonVariants({ variant: "ghost", size: "icon-sm" }), "bg-transparent text-muted-foreground hover:text-foreground")}
            >
              <Ellipsis aria-hidden="true" />
            </button>
          {/snippet}
        </DropdownMenu.Trigger>
      {/snippet}
    </Tooltip.Trigger>
    <Tooltip.Content>{label}</Tooltip.Content>
  </Tooltip.Root>
  <DropdownMenu.Content align="end" class="bg-overlay">
    {@render children()}
  </DropdownMenu.Content>
</DropdownMenu.Root>
