// docs/ stays the source of truth (code, CI and the README point at it), so
// the site is built from a generated copy with frontmatter and rewritten links.
import { cpSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const out = join(root, 'site', 'src', 'content', 'docs');
const repo = 'https://github.com/karanshukla/vinoWhisper/blob/main';

const pages = readdirSync(join(root, 'docs')).filter((f) => f.endsWith('.md'));
const known = new Set(pages.map((f) => f.replace(/\.md$/, '')));

function rewrite(target) {
  if (/^([a-z]+:|#|\/)/i.test(target)) return target;
  const [path, hash = ''] = target.split('#');
  const suffix = hash ? `#${hash}` : '';
  const page = path.replace(/\.md$/, '');
  if (known.has(page)) return `/${page}/${suffix}`;
  const resolved = join('docs', path).replace(/^docs\/\.\.\//, '');
  return `${repo}/${resolved}${suffix}`;
}

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });

for (const file of pages) {
  const lines = readFileSync(join(root, 'docs', file), 'utf8').split('\n');
  const h1 = lines.findIndex((l) => l.startsWith('# '));
  if (h1 === -1) throw new Error(`docs/${file} has no top-level heading`);
  const title = lines[h1].slice(2).trim();
  const body = lines
    .filter((_, i) => i !== h1)
    .join('\n')
    .replace(/\]\(([^)\s]+)\)/g, (_, t) => `](${rewrite(t)})`);
  writeFileSync(
    join(out, file),
    `---\ntitle: ${JSON.stringify(title)}\n---\n${body}`,
  );
}

cpSync(join(root, 'site', 'index.mdx'), join(out, 'index.mdx'));
cpSync(join(root, 'docs', 'assets', 'vinowhisper.svg'), join(root, 'site', 'src', 'assets', 'vinowhisper.svg'));
cpSync(join(root, 'docs', 'assets', 'vinowhisper.svg'), join(root, 'site', 'public', 'favicon.svg'));
