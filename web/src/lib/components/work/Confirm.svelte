<script lang="ts">
  import * as AlertDialog from "$lib/components/ui/alert-dialog";

  // The confirm dialog every destructive Work action goes through. `onconfirm` runs first, while the caller still holds what is being confirmed.
  let {
    open = $bindable(false),
    title,
    description,
    keep,
    action,
    onconfirm,
  }: { open: boolean; title: string; description: string; keep: string; action: string; onconfirm: () => void } = $props();
</script>

<AlertDialog.Root bind:open>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>{title}</AlertDialog.Title>
      <AlertDialog.Description>{description}</AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">{keep}</AlertDialog.Cancel>
      <AlertDialog.Action variant="destructive-solid" onclick={() => { onconfirm(); open = false; }}>{action}</AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
