/* global console, document */
// Manual end-to-end check through the real UI: types into the chat composer, clicks Send,
// and reports what the browser actually rendered plus the wall-clock time to each answer.
// Run from frontend/ so `playwright` resolves from node_modules.
import { chromium } from "playwright";

const BASE = "http://127.0.0.1:5173";
const QUESTIONS = [
  "What is the price of Mamlakati?",
  "How many Mansam boutiques are there in Saudi Arabia?",
];

const api = [];
// Use the installed Chrome rather than Playwright's bundled build: the local package expects a
// browser revision that is not in the cache, and the real browser is also what the user sees.
const browser = await chromium.launch({ channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage();

page.on("console", (message) => console.log(`[browser ${message.type()}] ${message.text()}`));
page.on("pageerror", (error) => console.log(`[page error] ${error.message}`));
page.on("request", (request) => {
  if (request.url().includes("/api/v1/")) {
    api.push({ url: request.url().replace(BASE, ""), method: request.method(), started: Date.now() });
  }
});
page.on("requestfinished", async (request) => {
  if (!request.url().includes("/api/v1/")) return;
  const entry = api.find((item) => item.url === request.url().replace(BASE, "") && !item.finished);
  if (entry) entry.finished = Date.now();
});
page.on("response", (response) => {
  if (response.url().includes("/api/v1/")) {
    console.log(`[api] ${response.request().method()} ${response.url().split("/api/v1")[1]} -> ${response.status()}`);
  }
});

await page.goto(BASE, { waitUntil: "domcontentloaded" });
await page.waitForSelector('textarea[aria-label="Message"]', { timeout: 30000 });
console.log(`page title: ${await page.title()}`);

for (const [index, question] of QUESTIONS.entries()) {
  const assistantBefore = await page.locator("article.message.assistant").count();
  await page.fill('textarea[aria-label="Message"]', question);
  console.log(`\n=== question ${index + 1}: ${question}`);
  const started = Date.now();
  await page.click('button[type="submit"]');

  // Read the status line while the answer is still in flight: this is the feedback that used
  // to be a single frozen label.
  await page.waitForTimeout(4000);
  console.log(`in-flight footer: "${(await page.locator("footer").innerText()).trim()}"`);

  await page.waitForFunction(
    (before) => document.querySelectorAll("article.message.assistant").length > before,
    assistantBefore,
    { timeout: 240000 },
  );
  const seconds = ((Date.now() - started) / 1000).toFixed(1);

  const answer = (await page.locator("article.message.assistant > p").last().innerText()).trim();
  const meta = (await page.locator("article.message.assistant").last().innerText()).replace(/\s+/g, " ");
  const sources = meta.match(/Sources \(\d+\)/)?.[0] ?? "no sources block";
  const footer = (await page.locator("footer").innerText()).trim();
  console.log(`answer in ${seconds}s | ${sources} | footer="${footer}"`);
  console.log(`answer text: ${answer}`);
  console.log(`rendererd block: ${meta.slice(0, 320)}`);
  await page.screenshot({ path: `../tmp/frontend-chat-${index + 1}.png`, fullPage: true });
}

console.log("\n=== api calls the browser made ===");
for (const entry of api) {
  const ms = entry.finished ? `${entry.finished - entry.started} ms` : "still streaming";
  console.log(`${entry.method} ${entry.url} -> ${entry.finished ? "completed" : "open"} (${ms})`);
}

await browser.close();
