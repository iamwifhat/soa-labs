// Знімки інтерфейсів Consul, Traefik і RabbitMQ запущеного стеку ЛР2/ЛР3.
// Запуск: node capture_ui.mjs ../../lab2/docs/screenshots [consul traefik rabbit]
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const require = createRequire('/Users/iamtoyvovich/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/package.json');
const { chromium } = require('playwright');
const out = path.resolve(process.argv[2] || path.join(path.dirname(fileURLToPath(import.meta.url)), 'build', 'ui'));
const only = process.argv.slice(3);
const browser = await chromium.launch({ headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2, locale: 'uk-UA' });
const page = await context.newPage();
const shots = {
  consul: async () => {
    await page.goto('http://localhost:8500/ui/dc1/services/participation-service/instances');
    await page.waitForTimeout(2500);
    await page.screenshot({ path: `${out}/consul-instances.png` });
    await page.goto('http://localhost:8500/ui/dc1/services');
    await page.waitForTimeout(2500);
    await page.screenshot({ path: `${out}/consul-services.png` });
  },
  traefik: async () => {
    await page.goto('http://localhost:8081/dashboard/#/http/services/calendar-service@consulcatalog');
    await page.waitForTimeout(3000);
    await page.screenshot({ path: `${out}/traefik-service.png` });
    await page.goto('http://localhost:8081/dashboard/#/http/routers');
    await page.waitForTimeout(3000);
    await page.screenshot({ path: `${out}/traefik-routers.png` });
  },
  rabbit: async () => {
    await page.goto('http://localhost:15672/');
    await page.fill('input[name="username"]', 'calendar');
    await page.fill('input[name="password"]', 'calendar');
    await page.click('input[type="submit"]');
    await page.waitForTimeout(2000);
    await page.goto('http://localhost:15672/#/queues');
    await page.waitForTimeout(3500);
    await page.screenshot({ path: `${out}/rabbitmq-queues.png` });
  },
};
for (const [name, shot] of Object.entries(shots)) if (!only.length || only.includes(name)) await shot();
await browser.close();
