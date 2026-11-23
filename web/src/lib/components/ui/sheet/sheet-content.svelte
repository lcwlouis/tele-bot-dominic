<script lang="ts">
	import { Dialog as SheetPrimitive } from "bits-ui";
	import type { Snippet } from "svelte";
	import { cn, type WithoutChildrenOrChild } from "$lib/utils.js";

	// A dialog that slides in from the right edge: the shadcn-svelte Sheet, kept to the one side Dom uses.
	// A swipe towards the edge closes it, like the outside tap and Escape the dialog already handles.
	let {
		ref = $bindable(null),
		class: className,
		children,
		onswipe,
		...restProps
	}: WithoutChildrenOrChild<SheetPrimitive.ContentProps> & { children?: Snippet; onswipe?: () => void } = $props();

	let start: { x: number; y: number } | null = null;
	const down = (event: PointerEvent) => (start = event.pointerType === "mouse" ? null : { x: event.clientX, y: event.clientY });
	function up(event: PointerEvent) {
		if (start && event.clientX - start.x > 60 && Math.abs(event.clientY - start.y) < 50) onswipe?.();
		start = null;
	}
</script>

<SheetPrimitive.Portal>
	<SheetPrimitive.Overlay
		data-slot="sheet-overlay"
		class="data-open:animate-in data-closed:animate-out data-closed:fade-out-0 data-open:fade-in-0 fixed inset-0 z-50 bg-canvas/75 duration-150 supports-backdrop-filter:backdrop-blur-xs"
	/>
	<SheetPrimitive.Content
		bind:ref
		data-slot="sheet-content"
		onpointerdown={down}
		onpointerup={up}
		onpointercancel={() => (start = null)}
		class={cn(
			"data-open:animate-in data-closed:animate-out data-closed:slide-out-to-right data-open:slide-in-from-right fixed inset-y-0 right-0 z-50 flex h-dvh w-[min(20rem,86vw)] flex-col border-l bg-rail text-foreground shadow-xl duration-200 outline-none motion-reduce:animate-none",
			className,
		)}
		{...restProps}
	>
		{@render children?.()}
	</SheetPrimitive.Content>
</SheetPrimitive.Portal>
