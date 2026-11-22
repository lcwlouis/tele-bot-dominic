import { createRequire } from 'node:module';
import { dirname } from 'node:path';
import type { StorybookConfig } from '@storybook/svelte-vite';

const require = createRequire(import.meta.url);

// Framework and addons by their resolved folders, Storybook's recommended form.
const config: StorybookConfig = {
  framework: dirname(require.resolve('@storybook/svelte-vite/package.json')),
  stories: ['../src/**/*.stories.svelte'],
  addons: [
    dirname(require.resolve('@storybook/addon-svelte-csf/package.json')),
    dirname(require.resolve('@storybook/addon-a11y/package.json')),
  ],
  core: { disableTelemetry: true },
};

export default config;
