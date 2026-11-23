<script lang="ts" module>
	import { type VariantProps, tv } from "tailwind-variants";
	import { cn, type WithElementRef } from "$lib/utils.js";
	import type { HTMLAnchorAttributes, HTMLButtonAttributes } from "svelte/elements";

	export const buttonVariants = tv({
		// Quiet Orbit: secondary and ghost stay visible at rest; destructive is a calm coral, solid only for
		// the final confirmation.
		base: "focus-visible:ring-ring/40 aria-invalid:border-destructive rounded-[0.65rem] border border-transparent bg-clip-padding text-sm font-medium focus-visible:ring-3 active:not-aria-[haspopup]:translate-y-px [&_svg:not([class*='size-'])]:size-4 group/button inline-flex shrink-0 items-center justify-center whitespace-nowrap transition-[color,background-color,border-color,box-shadow,transform] duration-150 outline-none select-none disabled:pointer-events-none disabled:opacity-45 disabled:saturate-50 [&_svg]:pointer-events-none [&_svg]:shrink-0",
		variants: {
			variant: {
				default: "bg-primary text-primary-foreground shadow-[0_0_0_1px_var(--primary),0_6px_18px_-10px_var(--signal)] hover:bg-signal-strong active:bg-[color-mix(in_oklab,var(--primary),var(--canvas)_18%)]",
				secondary: "border-signal/55 bg-secondary text-secondary-foreground hover:border-signal hover:bg-[color-mix(in_oklab,var(--tint),var(--signal)_14%)] active:bg-[color-mix(in_oklab,var(--tint),var(--canvas)_30%)] aria-expanded:border-signal",
				outline: "border-border bg-transparent text-foreground hover:border-signal/60 hover:bg-raised aria-expanded:bg-raised",
				ghost: "bg-signal/8 text-signal-strong hover:bg-tint active:bg-[color-mix(in_oklab,var(--tint),var(--canvas)_30%)] aria-expanded:bg-tint",
				destructive: "border-coral/55 bg-coral-tint text-coral hover:border-coral hover:bg-[color-mix(in_oklab,var(--coral-tint),var(--coral)_14%)] focus-visible:ring-coral/40",
				"destructive-solid": "bg-coral-fill text-on-coral hover:bg-[color-mix(in_oklab,var(--coral-fill),var(--text)_12%)] focus-visible:ring-coral/40",
				link: "text-signal-strong underline-offset-4 hover:underline",
			},
			size: {
				default: "h-9 gap-1.5 px-3.5 has-data-[icon=inline-end]:pr-2.5 has-data-[icon=inline-start]:pl-2.5",
				xs: "h-6 gap-1 rounded-md px-2 text-xs [&_svg:not([class*='size-'])]:size-3",
				sm: "h-8 gap-1 rounded-[0.55rem] px-3 text-[0.8rem] [&_svg:not([class*='size-'])]:size-3.5",
				lg: "h-10 gap-2 px-4",
				icon: "size-9",
				"icon-xs": "size-6 rounded-md [&_svg:not([class*='size-'])]:size-3",
				"icon-sm": "size-8 rounded-[0.55rem]",
				"icon-lg": "size-10",
			},
		},
		defaultVariants: {
			variant: "default",
			size: "default",
		},
	});

	export type ButtonVariant = VariantProps<typeof buttonVariants>["variant"];
	export type ButtonSize = VariantProps<typeof buttonVariants>["size"];

	export type ButtonProps = WithElementRef<HTMLButtonAttributes> &
		WithElementRef<HTMLAnchorAttributes> & {
			variant?: ButtonVariant;
			size?: ButtonSize;
		};
</script>

<script lang="ts">
	let {
		class: className,
		variant = "default",
		size = "default",
		ref = $bindable(null),
		href = undefined,
		type = "button",
		disabled,
		children,
		...restProps
	}: ButtonProps = $props();
</script>

{#if href}
	<a
		bind:this={ref}
		data-slot="button"
		class={cn(buttonVariants({ variant, size }), className)}
		href={disabled ? undefined : href}
		aria-disabled={disabled}
		role={disabled ? "link" : undefined}
		tabindex={disabled ? -1 : undefined}
		{...restProps}
	>
		{@render children?.()}
	</a>
{:else}
	<button
		bind:this={ref}
		data-slot="button"
		class={cn(buttonVariants({ variant, size }), className)}
		{type}
		{disabled}
		{...restProps}
	>
		{@render children?.()}
	</button>
{/if}
