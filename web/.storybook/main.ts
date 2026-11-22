import type { StorybookConfig } from '@storybook/svelte-vite';

const config: StorybookConfig = {
  framework: '@storybook/svelte-vite',
  stories: ['../src/**/*.stories.svelte'],
  addons: ['@storybook/addon-svelte-csf', '@storybook/addon-a11y'],
  core: { disableTelemetry: true },
};

export default config;
