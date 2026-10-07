// Drives headless Edge for front-end checks.
//   node dev/shot.mjs <steps.json>
// steps.json: { "url": "...", "width": 1440, "height": 900, "steps": [
//   {"wait": 500}, {"eval": "document.title"}, {"click": "css selector"}, {"clickText": "Barrel"},
//   {"type": "css selector", "text": "..."}, {"shot": "path.png"} ] }
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const config = JSON.parse(readFileSync(process.argv[2], "utf8"));
const port = 9300 + Math.floor(Math.random() * 500);
const profile = mkdtempSync(join(tmpdir(), "rtse-edge-"));

const proc = spawn(EDGE, [
  "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  `--window-size=${config.width || 1440},${config.height || 900}`, "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist", "--no-first-run", "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function pageTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page;
    } catch { /* browser still starting */ }
    await sleep(250);
  }
  throw new Error("Edge did not start");
}

const target = await pageTarget();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve) => ws.addEventListener("open", resolve));
let nextId = 1;
const pending = new Map();
const problems = [];
ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message);
    pending.delete(message.id);
  } else if (message.method === "Runtime.exceptionThrown") {
    problems.push(`exception: ${message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text}`);
  } else if (message.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(message.params.type)) {
    problems.push(`console.${message.params.type}: ${message.params.args.map((a) => a.value ?? a.description).join(" ")}`);
  } else if (message.method === "Network.responseReceived" && message.params.response.status >= 400) {
    problems.push(`http ${message.params.response.status}: ${message.params.response.url}`);
  }
});
const send = (method, params = {}) => new Promise((resolve) => {
  const id = nextId++;
  pending.set(id, resolve);
  ws.send(JSON.stringify({ id, method, params }));
});

await send("Page.enable");
await send("Runtime.enable");
await send("Network.enable");
await send("Emulation.setDeviceMetricsOverride", { width: config.width || 1440, height: config.height || 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: config.url });
await sleep(1500);

const evaluate = async (expression) => {
  const result = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.result.exceptionDetails) problems.push(`eval failed: ${result.result.exceptionDetails.exception?.description}`);
  return result.result.result?.value;
};

for (const step of config.steps) {
  if (step.wait) await sleep(step.wait);
  if (step.eval) console.log("eval:", JSON.stringify(await evaluate(step.eval)));
  if (step.click) console.log("click:", step.click, await evaluate(`(() => { const e = document.querySelector(${JSON.stringify(step.click)}); if (!e) return "NOT FOUND"; e.click(); return "ok"; })()`));
  if (step.clickText) {
    const needle = JSON.stringify(step.clickText);
    console.log("clickText:", step.clickText, await evaluate(`(() => { const e = [...document.querySelectorAll('.row, .leg, .card, summary, button')].find(x => x.textContent.includes(${needle})); if (!e) return "NOT FOUND"; e.click(); return "ok"; })()`));
  }
  if (step.type) {
    console.log("type:", step.type, await evaluate(`(() => { const e = document.querySelector(${JSON.stringify(step.type)}); if (!e) return "NOT FOUND"; e.focus(); e.value = ${JSON.stringify(step.text)}; e.dispatchEvent(new Event('input', {bubbles: true})); return "ok"; })()`));
  }
  if (step.mouse) {
    const [x, y] = step.mouse;
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
    console.log("mouse click at", x, y);
  }
  if (step.hover) {
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: step.hover[0], y: step.hover[1] });
    console.log("mouse hover at", step.hover);
  }
  if (step.scroll) await evaluate(`document.querySelector(${JSON.stringify(step.scroll.selector)}).scrollTop = ${step.scroll.top}`);
  if (step.save) {
    const data = await evaluate(step.save.expr);
    if (typeof data === "string" && data.includes(",")) { writeFileSync(step.save.file, Buffer.from(data.split(",")[1], "base64")); console.log("saved", step.save.file); } else console.log("save failed:", step.save.file, data);
  }
  if (step.shot) {
    const clip = step.fullPage ? undefined : undefined;
    const shot = await send("Page.captureScreenshot", { format: "png", ...(clip ? { clip } : {}) });
    writeFileSync(step.shot, Buffer.from(shot.result.data, "base64"));
    console.log("saved", step.shot);
  }
}

console.log(problems.length ? `PROBLEMS:\n${problems.join("\n")}` : "no console problems");
ws.close();
proc.kill();
process.exit(0);
