import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://docs.vinowhisper.com',
  integrations: [
    starlight({
      title: 'vinoWhisper',
      description:
        'Live captions and dictation on the Intel NPU, running locally on Linux.',
      logo: { src: './src/assets/vinowhisper.svg', alt: '' },
      favicon: '/favicon.svg',
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/karanshukla/vinoWhisper',
        },
      ],
      editLink: {
        baseUrl: 'https://github.com/karanshukla/vinoWhisper/edit/main/docs/',
      },
      sidebar: [
        {
          label: 'Use it',
          items: [
            { slug: 'install' },
            { slug: 'terminal' },
            { slug: 'gui' },
          ],
        },
        {
          label: 'Your machine',
          items: [{ slug: 'hardware' }, { slug: 'audio' }],
        },
        {
          label: 'How it works',
          items: [
            { slug: 'latency' },
            { slug: 'architecture' },
            { slug: 'debugging' },
          ],
        },
        { label: 'Contribute', items: [{ slug: 'development' }] },
      ],
    }),
  ],
});
