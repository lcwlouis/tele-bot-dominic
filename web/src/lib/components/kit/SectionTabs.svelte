<script lang="ts">
  import * as Tabs from "$lib/components/ui/tabs";

  // The sections inside one page (Settings: Models, Providers…; Access: Saved logins…), as an underlined row
  // under the page's own tabs. Goes inside a Tabs.Root; scrolls sideways on a narrow screen with no scrollbar.
  let { items }: { items: { value: string; label: string }[] } = $props();
</script>

<Tabs.List variant="line" class="no-scrollbar mb-5 h-auto! w-full justify-start gap-5 overflow-x-auto border-b pb-1">
  {#each items as item (item.value)}
    <Tabs.Trigger value={item.value} class="flex-none px-0 py-2 data-active:text-signal-strong">{item.label}</Tabs.Trigger>
  {/each}
</Tabs.List>
