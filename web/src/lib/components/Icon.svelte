<script lang="ts" module>
  // 24 px line icons drawn for this dashboard. Inline SVG: no icon font, no third-party request.
  const PATHS = {
    home: 'M4 11.5 12 5l8 6.5V20a1 1 0 0 1-1 1h-4.5v-6h-5v6H5a1 1 0 0 1-1-1z',
    chats: 'M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4h0A1.5 1.5 0 0 1 4 14.5z',
    settings:
      'M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4ZM19 12c0-.5 0-.9-.1-1.3l2-1.6-2-3.4-2.4.9a7 7 0 0 0-2.2-1.3L13.9 3h-3.8l-.4 2.3a7 7 0 0 0-2.2 1.3l-2.4-.9-2 3.4 2 1.6a7 7 0 0 0 0 2.6l-2 1.6 2 3.4 2.4-.9c.7.6 1.4 1 2.2 1.3l.4 2.3h3.8l.4-2.3a7 7 0 0 0 2.2-1.3l2.4.9 2-3.4-2-1.6c.1-.4.1-.8.1-1.3Z',
    chevron: 'm8 10 4 4 4-4',
    close: 'M6 6l12 12M18 6 6 18',
    check: 'm5 12.5 4.5 4.5L19 7.5',
    alert: 'M12 8v5m0 3.5v.01M10.3 4.2 2.9 17a2 2 0 0 0 1.7 3h14.8a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z',
    moon: 'M19.5 14.5A7.5 7.5 0 0 1 9.5 4.5a7.5 7.5 0 1 0 10 10Z',
    screen: 'M4 5h16v11H4zM9 20h6M12 16v4',
  } as const;
  export type IconName = keyof typeof PATHS;
</script>

<script lang="ts">
  interface Props {
    name: IconName;
    size?: number;
    /** Text for screen readers. Leave empty when the icon sits next to its own label. */
    label?: string;
  }

  let { name, size = 24, label = '' }: Props = $props();
</script>

<svg
  class="icon"
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.8"
  stroke-linecap="round"
  stroke-linejoin="round"
  role={label ? 'img' : undefined}
  aria-label={label || undefined}
  aria-hidden={label ? undefined : 'true'}
>
  <path d={PATHS[name]} />
</svg>

<style>
  .icon {
    flex: none;
    display: block;
  }
</style>
