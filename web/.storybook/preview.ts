import type { Preview } from '@storybook/svelte-vite';
import '../src/lib/styles/base.css';

const preview: Preview = {
  parameters: {
    layout: 'padded',
    viewport: {
      options: {
        phone: { name: 'Phone (390 px)', styles: { width: '390px', height: '844px' }, type: 'mobile' },
        desktop: { name: 'Desktop (1280 px)', styles: { width: '1280px', height: '800px' }, type: 'desktop' },
      },
    },
    backgrounds: { disable: true },
  },
  globalTypes: {
    theme: {
      description: 'Colour theme',
      toolbar: {
        title: 'Theme',
        icon: 'mirror',
        items: [
          { value: 'system', title: 'Follow the system' },
          { value: 'light', title: 'Light' },
          { value: 'dark', title: 'Dark' },
        ],
        dynamicTitle: true,
      },
    },
  },
  initialGlobals: { theme: 'system' },
  decorators: [
    (story, context) => {
      const theme = context.globals.theme as string;
      if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
      else delete document.documentElement.dataset.theme;
      return story();
    },
  ],
};

export default preview;
