// Знімки HTML-сторінок терміналу у PNG (елемент #term).
import { createRequire } from 'node:module';
import { readdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire('/Users/iamtoyvovich/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/package.json');
const { chromium } = require('playwright');
const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), 'build', 'terminals');
const browser = await chromium.launch({ headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
const page = await browser.newPage({ viewport: { width: 1000, height: 800 }, deviceScaleFactor: 2.5 });
for (const file of (await readdir(dir)).filter(f => f.endsWith('.html')).sort()) {
  await page.goto('file://' + path.join(dir, file));
  const box = await page.locator('#term').boundingBox();
  await page.locator('#term').screenshot({ path: path.join(dir, file.replace('.html', '.png')) });
  console.log(file, Math.round(box.width), Math.round(box.height));
}
await browser.close();
