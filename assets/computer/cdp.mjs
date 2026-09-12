#!/usr/bin/env node
// cdp.mjs — тонкий CDP-хелпер для браузерного контура Spine (ADR-041).
//
// Одна команда за вызов: `node cdp.mjs '<json>'`, ровно одна JSON-строка в
// stdout: {"ok":true,…} либо {"ok":false,"error":"…"}.
//
// Почему Node, а не Rust-крейт websocket: у харнесса намеренно узкий набор
// зависимостей (docs/supply-chain.md, SBOM, cargo audit в CI), а Node 22 даёт
// глобальные fetch и WebSocket — ноль npm-пакетов. Тот же приём уже принят
// для Archify (bin/archify.mjs + [archify].node_bin).
//
// Безопасность: хелпер соединяется ТОЛЬКО с loopback-портом, переданным
// харнессом, и не читает профиль пользователя — харнесс поднимает браузер с
// выделенным --user-data-dir.

import fs from "node:fs";

/** Аргументы: первый — JSON-полезная нагрузка, дальше — флаги --k=v. */
function parseArgs(argv) {
  const payloadRaw = argv[0];
  if (!payloadRaw) {
    throw new Error("нет полезной нагрузки: node cdp.mjs '<json>' [--port=N] [--timeout-ms=N]");
  }
  const payload = JSON.parse(payloadRaw);
  const flags = { port: 9222, timeoutMs: 30000 };
  for (const arg of argv.slice(1)) {
    const m = /^--([a-z-]+)=(.*)$/.exec(arg);
    if (!m) continue;
    if (m[1] === "port") flags.port = Number(m[2]);
    if (m[1] === "timeout-ms") flags.timeoutMs = Number(m[2]);
  }
  return { payload, flags };
}

/** Минимальный CDP-клиент поверх глобального WebSocket. */
class Cdp {
  constructor(url, timeoutMs) {
    this.url = url;
    this.timeoutMs = timeoutMs;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
  }

  connect() {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error(`таймаут подключения к ${this.url}`)),
        this.timeoutMs,
      );
      this.ws = new WebSocket(this.url);
      this.ws.addEventListener("open", () => {
        clearTimeout(timer);
        resolve();
      });
      this.ws.addEventListener("error", (event) => {
        clearTimeout(timer);
        reject(new Error(`WebSocket: ${event.message || "ошибка соединения"}`));
      });
      this.ws.addEventListener("message", (event) => this.#onMessage(event.data));
      this.ws.addEventListener("close", () => {
        for (const { reject: rej } of this.pending.values()) {
          rej(new Error("соединение закрыто"));
        }
        this.pending.clear();
      });
    });
  }

  #onMessage(raw) {
    let msg;
    try {
      msg = JSON.parse(typeof raw === "string" ? raw : raw.toString());
    } catch {
      return;
    }
    if (msg.id && this.pending.has(msg.id)) {
      const { resolve, reject } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) reject(new Error(`${msg.error.message || "ошибка CDP"}`));
      else resolve(msg.result);
      return;
    }
    const waiters = this.listeners.get(msg.method);
    if (waiters && waiters.length > 0) {
      waiters.shift()(msg.params);
    }
  }

  send(method, params = {}, timeoutMs = this.timeoutMs) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`таймаут CDP ${method}`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(timer);
          reject(e);
        },
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  /** Дождаться события (или не дождаться — вызывающий решает по таймауту). */
  once(method, timeoutMs) {
    return new Promise((resolve) => {
      const list = this.listeners.get(method) || [];
      list.push(resolve);
      this.listeners.set(method, list);
      setTimeout(() => {
        const still = this.listeners.get(method) || [];
        const idx = still.indexOf(resolve);
        if (idx >= 0) still.splice(idx, 1);
        resolve(null);
      }, timeoutMs);
    });
  }

  close() {
    try {
      this.ws.close();
    } catch {
      // Соединение уже закрыто — гасить нечего.
    }
  }
}

/** Найти WebSocket-эндпоинт страницы через HTTP-эндпоинты DevTools. */
async function discover(port, timeoutMs) {
  const base = `http://127.0.0.1:${port}`;
  const ctrl = AbortSignal.timeout(timeoutMs);
  const version = await (await fetch(`${base}/json/version`, { signal: ctrl })).json();
  if (!version.webSocketDebuggerUrl) {
    throw new Error("браузер не отдал webSocketDebuggerUrl");
  }
  let targets = await (await fetch(`${base}/json/list`, { signal: ctrl })).json();
  let page = targets.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
  if (!page) {
    // Пустой список — открываем вкладку (Chrome 111+ требует PUT).
    await fetch(`${base}/json/new?about:blank`, { method: "PUT", signal: ctrl }).catch(() => {});
    targets = await (await fetch(`${base}/json/list`, { signal: ctrl })).json();
    page = targets.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
  }
  if (!page) {
    throw new Error("не найдено ни одной страницы (target type=page)");
  }
  return { page: page.webSocketDebuggerUrl, browser: version };
}

/** Выполнить Runtime.evaluate и вернуть значение (или бросить ошибку страницы). */
async function evaluate(cdp, expression, awaitPromise = false) {
  const res = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise,
  });
  if (res.exceptionDetails) {
    const text = res.exceptionDetails.exception?.description || res.exceptionDetails.text;
    throw new Error(`ошибка на странице: ${text}`);
  }
  return res.result?.value;
}

/** Центр элемента по CSS-селектору — для координатного клика. */
async function selectorCenter(cdp, selector) {
  const box = await evaluate(
    cdp,
    `(() => { const el = document.querySelector(${JSON.stringify(selector)});
       if (!el) return null;
       const r = el.getBoundingClientRect();
       if (r.width === 0 && r.height === 0) return null;
       return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
     })()`,
  );
  if (!box) throw new Error(`элемент не найден или невидим: ${selector}`);
  return box;
}

const COMMANDS = {
  async ping() {
    return { ok: true };
  },

  async nav(cdp, payload) {
    await cdp.send("Page.enable");
    const loaded = cdp.once("Page.loadEventFired", 15000);
    const res = await cdp.send("Page.navigate", { url: payload.url });
    if (res.errorText) throw new Error(`навигация отклонена: ${res.errorText}`);
    await loaded;
    const title = await evaluate(cdp, "document.title").catch(() => "");
    const url = await evaluate(cdp, "location.href").catch(() => payload.url);
    return { ok: true, url, title };
  },

  async page(cdp, payload) {
    const url = await evaluate(cdp, "location.href").catch(() => "");
    const title = await evaluate(cdp, "document.title").catch(() => "");
    const text = await evaluate(cdp, "document.body ? document.body.innerText : ''").catch(
      () => "",
    );
    const limit = payload.maxChars || 8000;
    const clipped = typeof text === "string" ? text.slice(0, limit) : "";
    return {
      ok: true,
      url,
      title,
      text: clipped,
      truncated: typeof text === "string" && text.length > limit,
    };
  },

  async shot(cdp, payload) {
    const res = await cdp.send("Page.captureScreenshot", { format: "png" });
    if (!res.data) throw new Error("браузер не вернул данные скриншота");
    const path = payload.path;
    if (!path) throw new Error("не передан путь для скриншота");
    fs.writeFileSync(path, Buffer.from(res.data, "base64"));
    return { ok: true, path };
  },

  async query(cdp, payload) {
    const items = await evaluate(
      cdp,
      `(() => Array.from(document.querySelectorAll(${JSON.stringify(payload.selector)}))
         .slice(0, ${Number(payload.limit) || 50})
         .map((el) => { const r = el.getBoundingClientRect();
           return { tag: el.tagName.toLowerCase(), text: (el.innerText || el.value || '').slice(0, 200),
                    href: el.getAttribute('href'), x: Math.round(r.left + r.width / 2),
                    y: Math.round(r.top + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) }; }))()`,
    );
    return { ok: true, count: Array.isArray(items) ? items.length : 0, items: items || [] };
  },

  async click(cdp, payload) {
    const point = await selectorCenter(cdp, payload.selector);
    for (const type of ["mousePressed", "mouseReleased"]) {
      await cdp.send("Input.dispatchMouseEvent", {
        type,
        x: point.x,
        y: point.y,
        button: "left",
        clickCount: 1,
      });
    }
    return { ok: true, x: point.x, y: point.y };
  },

  async type(cdp, payload) {
    if (payload.selector) {
      await evaluate(
        cdp,
        `(() => { const el = document.querySelector(${JSON.stringify(payload.selector)});
           if (!el) throw new Error('элемент не найден');
           el.focus(); return true; })()`,
      );
    }
    // Input.insertText не зависит от раскладки клавиатуры — кириллица
    // вводится надёжно, в отличие от xdotool type.
    await cdp.send("Input.insertText", { text: payload.text });
    return { ok: true, chars: [...payload.text].length };
  },

  async eval(cdp, payload) {
    const value = await evaluate(cdp, payload.expression, Boolean(payload.await));
    const serialized = JSON.stringify(value);
    const limit = payload.maxChars || 8000;
    const clipped = serialized && serialized.length > limit ? serialized.slice(0, limit) : serialized;
    return { ok: true, result: clipped };
  },
};

async function main() {
  const { payload, flags } = parseArgs(process.argv.slice(2));
  const cmd = payload.cmd;
  const handler = COMMANDS[cmd];
  if (!handler) {
    throw new Error(`неизвестная команда «${cmd}» (${Object.keys(COMMANDS).join(" | ")})`);
  }
  const { page, browser } = await discover(flags.port, flags.timeoutMs);
  const cdp = new Cdp(page, flags.timeoutMs);
  await cdp.connect();
  try {
    const out = await handler(cdp, payload);
    process.stdout.write(JSON.stringify({ ...out, target: browser.Browser }) + "\n");
  } finally {
    cdp.close();
  }
}

main().catch((err) => {
  // Отказ кодируется в JSON и НЕ роняет код возврата: вызывающая сторона —
  // Rust-обёртка, которая разбирает stdout; ненулевой код она трактует как
  // сбой самого Node, а не как отказ команды.
  process.stdout.write(JSON.stringify({ ok: false, error: String(err.message || err) }) + "\n");
});
