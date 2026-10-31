// Схеми для звітів ЛР2 і ЛР3: Times New Roman, прості прямокутники та явні стрілки (стиль переглянутих рисунків ЛР1).
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire('/Users/iamtoyvovich/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/package.json');
const { chromium } = require('playwright');
const work = path.dirname(fileURLToPath(import.meta.url));
const out = path.join(work, 'figures');
await mkdir(out, { recursive: true });

const W = 1800;
const FONT = 50;
const escape = v => String(v).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
let parts = [];

function text(x, y, value, { size = FONT, anchor = 'middle', bold = false, leading = size * 1.18, centered = false, italic = false, halo = true } = {}) {
  const lines = String(value).split('\n');
  const first = centered ? y + size * 0.34 - (lines.length - 1) * leading / 2 : y;
  parts.push(`<text x="${x}" y="${first}" font-size="${size}" text-anchor="${anchor}"${bold ? ' font-weight="bold"' : ''}${italic ? ' font-style="italic"' : ''}${halo ? ' paint-order="stroke" stroke="white" stroke-width="12" stroke-linejoin="round"' : ''}>${lines.map((line, i) => `<tspan x="${x}" dy="${i ? leading : 0}">${escape(line)}</tspan>`).join('')}</text>`);
}
function rect(x, y, w, h, value, { fill = 'white', dashed = false, size = FONT, bold = false } = {}) {
  parts.push(`<g class="node" data-bounds="${x},${y},${w},${h}"><rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${fill}" stroke="#333" stroke-width="2.5"${dashed ? ' stroke-dasharray="13 9"' : ''}/>`);
  if (value) text(x + w / 2, y + h / 2, value, { size, centered: true, bold, halo: false });
  parts.push('</g>');
}
function group(x, y, w, h, title) {
  parts.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="none" stroke="#333" stroke-width="2.5" stroke-dasharray="13 9"/>`);
  text(x + 25, y + 62, title, { anchor: 'start' });
}
function arrow(points, { dashed = false, both = false } = {}) {
  parts.push(`<polyline points="${points.map(p => p.join(',')).join(' ')}" fill="none" stroke="#333" stroke-width="3" marker-end="url(#arrow)"${both ? ' marker-start="url(#start-arrow)"' : ''}${dashed ? ' stroke-dasharray="12 8"' : ''}/>`);
}
function line(points, { dashed = true } = {}) {
  parts.push(`<polyline points="${points.map(p => p.join(',')).join(' ')}" fill="none" stroke="#333" stroke-width="3"${dashed ? ' stroke-dasharray="12 8"' : ''}/>`);
}
function database(x, y, w, h, value) {
  const r = 22;
  parts.push(`<g class="node" data-bounds="${x},${y},${w},${h}"><path d="M ${x} ${y + r} V ${y + h - r} C ${x} ${y + h + r / 2}, ${x + w} ${y + h + r / 2}, ${x + w} ${y + h - r} V ${y + r}" fill="white" stroke="#333" stroke-width="2.5"/><ellipse cx="${x + w / 2}" cy="${y + r}" rx="${w / 2}" ry="${r}" fill="white" stroke="#333" stroke-width="2.5"/>`);
  text(x + w / 2, y + h / 2 + 14, value, { centered: true, halo: false });
  parts.push('</g>');
}

// Діаграма послідовності: учасники в колонках, кроки пронумеровано, деталі — у тексті звіту.
function sequence(names, steps, { size = 48, top = 20 } = {}) {
  const colW = W / names.length;
  const cx = i => colW * i + colW / 2;
  const headH = Math.max(...names.map(n => n.split('\n').length)) * 62 + 50;
  names.forEach((name, i) => rect(cx(i) - colW / 2 + 18, top, colW - 36, headH, name, { fill: '#F2F2F2', size }));
  let y = top + headH + 40;
  const body = [];
  let n = 0;
  for (const step of steps) {
    n += 1;
    const label = `${n}. ${step.label}`;
    const lines = label.split('\n').length;
    if (step.self !== undefined) {
      const h = lines * size * 1.18 + 40;
      const yy = y;
      body.push(() => rect(cx(step.self) - colW / 2 + 12, yy, colW - 24, h, label, { size, fill: 'white' }));
      y += h + 35;
    } else {
      const labelH = lines * size * 1.18;
      const ay = y + labelH + 12;
      const [a, b] = [cx(step.from), cx(step.to)];
      const mid = (a + b) / 2;
      const yy = y;
      body.push(() => {
        text(mid, yy + size * 0.85, label, { size });
        arrow([[a + (b > a ? 8 : -8), ay], [b + (b > a ? -10 : 10), ay]], { dashed: !!step.reply });
      });
      y = ay + 45;
    }
  }
  names.forEach((_, i) => line([[cx(i), top + headH], [cx(i), y]], { dashed: true }));
  body.forEach(draw => draw());
  return Math.ceil(y + 20);
}

function svg(h, draw) {
  parts = [];
  const height = draw() || h;
  return { height, content: `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${height}" viewBox="0 0 ${W} ${height}" style="font-family:'Times New Roman',serif;background:white"><defs><marker id="arrow" viewBox="0 0 12 10" refX="10" refY="5" markerWidth="14" markerHeight="12" orient="auto" markerUnits="userSpaceOnUse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#333"/></marker><marker id="start-arrow" viewBox="0 0 12 10" refX="2" refY="5" markerWidth="14" markerHeight="12" orient="auto" markerUnits="userSpaceOnUse"><path d="M 12 0 L 2 5 L 12 10 z" fill="#333"/></marker></defs><rect width="${W}" height="${height}" fill="white"/>${parts.join('')}</svg>` };
}

const drawings = {
  // ---------------- ЛР2 ----------------
  'lab2-sync-architecture': () => {
    rect(20, 20, 400, 130, 'Клієнт');
    arrow([[220, 150], [220, 260]]);
    text(240, 225, 'HTTP :8080', { anchor: 'start' });
    rect(20, 260, 400, 740, 'API-шлюз\nTraefik');
    const rows = [[260, 'calendar-service ×N\n/api/calendar', 'calendar-db'],
                  [530, 'participation-service ×N\n/api/participation', 'participation-db'],
                  [800, 'notification-service\n/api/notification', 'notification-db']];
    for (const [y, name, db] of rows) {
      rect(570, y, 640, 170, name);
      arrow([[420, y + 85], [570, y + 85]]);
      database(1360, y, 420, 170, db);
      arrow([[1210, y + 85], [1360, y + 85]]);
      text(1285, y + 65, 'SQL');
      line([[570, y + 150], [500, y + 150], [500, 1100]]);
    }
    for (const y of [430, 700]) {
      arrow([[890, y], [890, y + 100]]);
      text(915, y + 68, 'REST, адресу знайдено в Consul', { anchor: 'start', size: 44 });
    }
    line([[220, 1000], [220, 1100]]);
    rect(20, 1100, 1760, 150, 'Consul: реєстр екземплярів, перевірка GET /health кожні 5 с');
    text(20, 1335, 'Суцільні стрілки — HTTP і SQL; штрихові лінії — реєстрація', { anchor: 'start', size: 46 });
    text(20, 1395, 'екземплярів і читання каталогу Consul шлюзом та сервісами.', { anchor: 'start', size: 46 });
    return 1430;
  },
  'lab2-async-topology': () => {
    rect(20, 20, 800, 170, 'calendar-service\n(продюсер)');
    rect(980, 20, 800, 170, 'participation-service\n(продюсер)');
    arrow([[420, 190], [420, 330]]);
    text(445, 275, 'calendar.event.*', { anchor: 'start' });
    arrow([[1380, 190], [1380, 330]]);
    text(1355, 275, 'participation.invitation.*', { anchor: 'end' });
    rect(200, 330, 1400, 130, 'обмінник calendar.events (topic)');
    arrow([[420, 460], [420, 600]]);
    arrow([[1380, 460], [1380, 600]]);
    rect(20, 600, 820, 210, 'черга participation.\ncalendar-events (quorum)');
    rect(960, 600, 820, 210, 'черга notification.\ndomain-events (quorum)');
    arrow([[420, 810], [420, 940]]);
    arrow([[1380, 810], [1380, 940]]);
    text(445, 892, 'доставка, ack', { anchor: 'start' });
    text(1405, 892, 'доставка, ack', { anchor: 'start' });
    rect(20, 940, 820, 150, 'participation-service ×N');
    rect(960, 940, 820, 150, 'notification-service');
    arrow([[840, 705], [880, 705], [880, 1230]], { dashed: true });
    arrow([[960, 705], [920, 705], [920, 1230]], { dashed: true });
    rect(200, 1230, 1400, 130, 'обмінник calendar.dlx (direct)');
    arrow([[420, 1360], [420, 1480]]);
    arrow([[1380, 1360], [1380, 1480]]);
    rect(20, 1480, 820, 170, 'participation.\ncalendar-events.dlq');
    rect(960, 1480, 820, 170, 'notification.\ndomain-events.dlq');
    text(20, 1735, 'Штрихові стрілки — повідомлення після 4 невдалих доставок', { anchor: 'start', size: 46 });
    text(20, 1795, '(x-delivery-limit = 3): обробник тричі повертав nack.', { anchor: 'start', size: 46 });
    return 1830;
  },
  'lab2-chain-sequence': () => sequence(
    ['Клієнт', 'Traefik', 'calendar', 'participation', 'notification'],
    [
      { from: 0, to: 1, label: 'GET …/chain' },
      { from: 1, to: 2, label: 'маршрут\n/api/calendar' },
      { self: 2, label: 'пошук\nу Consul' },
      { from: 2, to: 3, label: 'POST /chain' },
      { self: 3, label: 'пошук\nу Consul' },
      { from: 3, to: 4, label: 'POST /chain' },
      { self: 4, label: 'дописати\nфрагмент' },
      { from: 4, to: 3, label: '200 OK', reply: true },
      { from: 3, to: 2, label: '200 OK', reply: true },
      { from: 2, to: 0, label: '200 OK: message, hops', reply: true },
    ]),
  'lab2-choreography-sequence': () => sequence(
    ['Організатор', 'calendar', 'RabbitMQ', 'participation', 'notification'],
    [
      { from: 0, to: 1, label: 'POST /events' },
      { self: 1, label: 'подія v1\nу БД' },
      { from: 1, to: 2, label: 'EventCreated' },
      { from: 1, to: 0, label: '201 Created', reply: true },
      { from: 2, to: 3, label: 'EventCreated' },
      { from: 2, to: 4, label: 'EventCreated' },
      { self: 3, label: 'запрошення\nPending' },
      { self: 4, label: 'нагадування\nорганізатору' },
      { from: 3, to: 2, label: 'InvitationCreated' },
      { from: 2, to: 4, label: 'InvitationCreated' },
      { self: 4, label: 'лист\nадресату' },
    ]),
  'lab2-orchestration-sequence': () => sequence(
    ['Організатор', 'calendar\n(координатор)', 'participation', 'notification'],
    [
      { from: 0, to: 1, label: 'PATCH /events/{id}\nIf-Match: "1"' },
      { self: 1, label: 'новий час,\nверсія 2' },
      { from: 1, to: 2, label: 'POST /internal/\n…/reschedule' },
      { self: 2, label: 'скинути\nвідповіді' },
      { from: 2, to: 1, label: '200 DONE', reply: true },
      { from: 1, to: 3, label: 'POST /internal/…/reschedule' },
      { self: 3, label: 'перенести\nнагадування' },
      { from: 3, to: 1, label: '200 DONE', reply: true },
      { from: 1, to: 0, label: '200: звіт\nкроків', reply: true },
    ]),

  // ---------------- ЛР3 ----------------
  'lab3-outbox-inbox': () => {
    arrow([[900, 20], [900, 130]]);
    text(925, 95, 'POST /events', { anchor: 'start' });
    group(20, 130, 1760, 360, 'calendar-service: одна транзакція БД календаря');
    rect(80, 240, 760, 190, 'events:\nподія Pending');
    rect(960, 240, 760, 190, 'outbox:\nEventCreated.v1');
    arrow([[1340, 430], [1340, 580]]);
    rect(960, 580, 760, 190, 'ретранслятор outbox\n(advisory lock)');
    arrow([[1340, 770], [1340, 900]]);
    text(1315, 855, 'publish, confirm', { anchor: 'end' });
    rect(960, 900, 760, 140, 'RabbitMQ');
    arrow([[1340, 1040], [1340, 1170]]);
    group(20, 1170, 1760, 360, 'participation-service: одна транзакція БД участі');
    rect(70, 1280, 520, 190, 'inbox:\nmessageId');
    rect(640, 1280, 520, 190, 'invitations');
    rect(1210, 1280, 520, 190, 'outbox:\nInvitations-\nCreated.v1');
    text(20, 1610, 'Якщо messageId уже є в inbox, транзакція не змінює даних,', { anchor: 'start', size: 46 });
    text(20, 1670, 'а повідомлення лише підтверджується (ack).', { anchor: 'start', size: 46 });
    return 1700;
  },
  'lab3-saga-success': () => sequence(
    ['Організатор', 'calendar\n(координатор)', 'RabbitMQ', 'participation', 'notification'],
    [
      { from: 0, to: 1, label: 'POST /events' },
      { self: 1, label: 'Pending,\nsaga, outbox' },
      { from: 1, to: 0, label: '202 Accepted', reply: true },
      { from: 1, to: 2, label: 'EventCreated' },
      { from: 2, to: 3, label: 'EventCreated' },
      { self: 3, label: 'inbox,\nзапрошення,\noutbox' },
      { from: 3, to: 2, label: 'Invitations-\nCreated' },
      { from: 2, to: 1, label: 'Invitations-\nCreated' },
      { self: 1, label: 'Scheduled,\nCompleted' },
      { from: 1, to: 2, label: 'EventConfirmed' },
      { from: 2, to: 4, label: 'EventConfirmed' },
      { self: 4, label: 'листи,\nнагадування' },
    ]),
  'lab3-saga-compensation': () => sequence(
    ['calendar\n(координатор)', 'RabbitMQ', 'DLQ', 'participation'],
    [
      { from: 0, to: 1, label: 'EventCreated' },
      { from: 1, to: 3, label: 'доставка, спроби 1–4' },
      { self: 3, label: 'виняток,\nвідкат транзакції' },
      { from: 3, to: 1, label: 'nack', reply: true },
      { from: 1, to: 2, label: 'delivery_limit' },
      { from: 2, to: 0, label: 'EventCreated (dead letter)' },
      { self: 0, label: 'компенсація:\nCancelled' },
      { from: 0, to: 1, label: 'EventCancelled' },
      { from: 1, to: 3, label: 'EventCancelled' },
      { self: 3, label: 'відкликати\nзапрошення' },
    ]),
  'lab3-event-states': () => {
    parts.push('<circle cx="310" cy="45" r="22" fill="#333"/>');
    arrow([[310, 67], [310, 170]]);
    text(335, 135, 'POST /events', { anchor: 'start' });
    rect(60, 170, 500, 190, 'Pending\n(SAGA створення)');
    rect(1240, 170, 500, 190, 'Cancelled');
    rect(650, 560, 500, 170, 'Scheduled');
    rect(650, 1090, 500, 230, 'Scheduled,\nSAGA перенесення,\nзміни заблоковано');
    arrow([[560, 265], [1240, 265]]);
    text(900, 240, 'DLQ або таймаут', {});
    arrow([[310, 360], [310, 645], [650, 645]]);
    text(335, 470, 'Invitations-\nCreated', { anchor: 'start' });
    arrow([[1150, 645], [1490, 645], [1490, 360]]);
    text(1465, 470, 'скасування\nорганізатором', { anchor: 'end' });
    arrow([[800, 730], [800, 1090]]);
    text(775, 880, 'PATCH:\nновий час', { anchor: 'end' });
    arrow([[1000, 1090], [1000, 730]]);
    text(1025, 830, 'Invitations-\nRescheduled або\nкомпенсація:\nпопередній час', { anchor: 'start' });
    return 1350;
  },
};

const browser = await chromium.launch({ headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', args: ['--disable-background-networking'] });
const audits = [];
try {
  for (const [name, draw] of Object.entries(drawings)) {
    const { height, content } = svg(0, draw);
    const html = '<!doctype html><html lang="uk"><meta charset="utf-8"><title>Схема</title><style>html,body{margin:0;padding:0;background:white}svg{display:block}</style>' + content + '</html>';
    await writeFile(path.join(out, name + '.svg'), content);
    await writeFile(path.join(out, name + '.html'), html);
    const page = await browser.newPage({ viewport: { width: W, height }, deviceScaleFactor: 1.5 });
    await page.goto('file://' + path.join(out, name + '.html'));
    await page.evaluate(() => document.fonts.ready);
    const errors = await page.evaluate(() => {
      const issues = [];
      for (const g of document.querySelectorAll('g.node')) {
        const [x, y, w, h] = g.dataset.bounds.split(',').map(Number);
        const t = g.querySelector('text');
        if (!t) continue;
        const b = t.getBBox();
        if (b.x < x + 10 || b.x + b.width > x + w - 10 || b.y < y + 6 || b.y + b.height > y + h - 6) issues.push({ text: g.textContent, bounds: [x, y, w, h], textBounds: [b.x, b.y, b.width, b.height].map(Math.round) });
      }
      for (const t of document.querySelectorAll('svg text')) {
        const b = t.getBBox(); const s = t.ownerSVGElement;
        if (b.x < 0 || b.x + b.width > s.width.baseVal.value || b.y < 0 || b.y + b.height > s.height.baseVal.value) issues.push({ outsideCanvas: t.textContent });
      }
      return issues;
    });
    audits.push({ name, height, errors });
    await page.screenshot({ path: path.join(out, name + '.png') });
    await page.close();
  }
} finally { await browser.close(); }
console.log(JSON.stringify(audits, null, 1));
if (audits.some(x => x.errors.length)) process.exitCode = 1;
