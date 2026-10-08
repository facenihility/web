/**
 * 前端集成验证（无浏览器）：把「后端驱动版」页面放进一个最小 DOM 垫片里真跑一遍。
 *
 *   node page_harness.js <baseUrl> <htmlPath>
 *
 * 页面里的两个 <script> 会被合并到同一个脚本作用域执行（等价浏览器中的全局词法环境），
 * 因此测试驱动代码可以直接读写 simAI / step / params 等页面内部绑定。
 *
 * 验证内容：
 *   1. 接入层加载、探测到后端并建立 SSE
 *   2. 状态快照水合进 simAI / simBL（节点、链路、任务、趋势、统计）
 *   3. 在线时本地 step() 被短路（后端是唯一权威时钟）
 *   4. 原有渲染代码（drawTopo / drawChart / updateUI）能消费后端数据
 *   5. 控制指令双写：改动 UI 控件会同步到后端
 *   6. 断开后自动回退到本地内置引擎，页面继续工作
 */
"use strict";

const fs = require("fs");
const http = require("http");
const vm = require("vm");

const BASE = process.argv[2];
const HTML_PATH = process.argv[3];

if (!BASE || !HTML_PATH) {
  console.error("用法: node page_harness.js <baseUrl> <htmlPath>");
  process.exit(2);
}

/* ------------------------------------------------------------------ DOM 垫片 */
const ctxStub = new Proxy({}, {
  get(t, k) {
    if (k === "measureText") return () => ({ width: 40 });
    if (k === "createLinearGradient") return () => ({ addColorStop() {} });
    if (k === "canvas") return { width: 800, height: 400 };
    return () => {};
  },
  set() { return true; }
});

function El(tag, id) {
  this.tagName = String(tag || "div").toUpperCase();
  this.id = id || "";
  this.childNodes = [];
  this.style = {};
  this.dataset = {};
  this._html = "";
  this._q = {};
  this._ev = {};
  this.textContent = "";
  this.value = "";
  this.scrollTop = 0;
  this.scrollHeight = 0;
  this.clientWidth = 800;
  this.clientHeight = 400;
  this.width = 1000;
  this.height = 470;
  this.className = "";
  const set = new Set();
  this.classList = {
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    contains: (c) => set.has(c),
    toggle: (c, on) => {
      if (on === undefined) { set.has(c) ? set.delete(c) : set.add(c); }
      else if (on) set.add(c); else set.delete(c);
    },
    _set: set
  };
}
El.prototype.getContext = function () { return ctxStub; };
El.prototype.setAttribute = function () {};
El.prototype.appendChild = function (n) { this.childNodes.push(n); if (n) n.parentNode = this; return n; };
El.prototype.insertBefore = function (n, ref) {
  const i = ref ? this.childNodes.indexOf(ref) : -1;
  if (i < 0) this.childNodes.push(n); else this.childNodes.splice(i, 0, n);
  if (n) n.parentNode = this;
  return n;
};
El.prototype.removeChild = function (n) {
  const i = this.childNodes.indexOf(n);
  if (i >= 0) this.childNodes.splice(i, 1);
  if (n) n.parentNode = null;
  return n;
};
El.prototype.remove = function () { if (this.parentNode) this.parentNode.removeChild(this); };
El.prototype.addEventListener = function (t, f) { (this._ev[t] = this._ev[t] || []).push(f); };
El.prototype.dispatch = function (t, ev) { (this._ev[t] || []).forEach((f) => f(ev || { target: this })); };
Object.defineProperty(El.prototype, "children", { get() { return this.childNodes; } });
Object.defineProperty(El.prototype, "firstChild", { get() { return this.childNodes[0] || null; } });
Object.defineProperty(El.prototype, "lastChild", { get() { return this.childNodes[this.childNodes.length - 1] || null; } });
Object.defineProperty(El.prototype, "innerHTML", {
  get() { return this._html; },
  set(v) { this._html = String(v); this._parse(); }
});
El.prototype._parse = function () {
  const h = this._html;
  this._gpuI = (h.match(/<i><\/i>/g) || []).length;
  if (/data-[nl]="/.test(h)) {
    this.childNodes = [];
    const re = /data-([nl])="([^"]+)"/g;
    let m;
    while ((m = re.exec(h))) {
      const c = new El("div");
      c.dataset[m[1]] = m[2];
      this.childNodes.push(c);
    }
  }
};
El.prototype.querySelector = function (sel) {
  if (!this._q[sel]) this._q[sel] = new El("div");
  return this._q[sel];
};
El.prototype.querySelectorAll = function (sel) {
  if (sel === ".gpu i") {
    const n = this._gpuI || 12;
    return Array.from({ length: n }, () => new El("i"));
  }
  return this.childNodes.filter((c) =>
    sel === ".node" ? c.dataset.n !== undefined :
    sel === ".lk" ? c.dataset.l !== undefined : false);
};

const byId = {};
function configure(el) {
  if (el.id === "scene") {
    el.value = "normal";
    el.selectedIndex = 0;
    el.options = [{ textContent: "常规混线生产" }, { textContent: "视觉质检高峰" },
                  { textContent: "AGV 潮汐调度" }, { textContent: "大模型突发推理" },
                  { textContent: "回传网络降级" }, { textContent: "极限压力测试" }];
    el.selectedOptions = [el.options[0]];
  }
  if (el.id === "rate") el.value = 9;
  if (el.id === "speed") el.value = 15;
}
const document = {
  body: new El("body"),
  documentElement: new El("html"),
  getElementById(id) {
    if (!byId[id]) { byId[id] = new El("div", id); configure(byId[id]); }
    return byId[id];
  },
  createElement(tag) { return new El(tag); },
  addEventListener() {},
  querySelector() { return new El("div"); }
};

let frames = 0;
globalThis.document = document;
globalThis.window = globalThis;
globalThis.devicePixelRatio = 1;
globalThis.location = { origin: BASE, href: BASE + "/" };
globalThis.requestAnimationFrame = (fn) => {
  if (frames++ < 300) setTimeout(() => fn(performance.now()), 40);
  return frames;
};
globalThis.__DSH_BACKEND_BASES__ = [BASE];

/* -------------------------------------------------------------- EventSource */
class EventSourceShim {
  constructor(url) {
    this.url = url;
    this._l = {};
    this._closed = false;
    const req = http.get(url, (res) => {
      let buf = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => {
        buf += chunk;
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let ev = "message";
          let data = "";
          raw.split("\n").forEach((line) => {
            if (line.startsWith("event: ")) ev = line.slice(7).trim();
            else if (line.startsWith("data: ")) data += line.slice(6);
          });
          if (data) this._emit(ev, data);
        }
      });
    });
    this._req = req;
    req.on("error", () => { if (!this._closed) this._emit("error", ""); });
  }
  _emit(t, data) { (this._l[t] || []).forEach((f) => f({ data: data })); }
  addEventListener(t, f) { (this._l[t] = this._l[t] || []).push(f); }
  close() { this._closed = true; try { this._req.destroy(); } catch (e) {} }
}
globalThis.EventSource = EventSourceShim;

/* ------------------------------------------------------------------ 页面加载 */
const html = fs.readFileSync(HTML_PATH, "utf8");
const scripts = Array.from(html.matchAll(/<script>([\s\S]*?)<\/script>/g)).map((m) => m[1]);
if (scripts.length < 2) {
  console.error("页面里没有找到两个 <script>（原始脚本 + 接入层），实际: " + scripts.length);
  process.exit(2);
}

/* 测试驱动：在页面同一作用域内执行 */
function pageDriver() {
  const results = [];
  const check = (name, ok, info) => results.push({ name: name, ok: !!ok, info: info === undefined ? "" : String(info) });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const health = () => fetch(BASE_ + "/api/health").then((r) => r.json());

  return (async () => {
    const api = window.WorkBuddyBackend;
    check("接入层已加载 (window.WorkBuddyBackend)", !!api);

    // 等后端连上
    for (let i = 0; i < 60 && !(api && api.state.online); i++) await sleep(100);
    check("SSE 已连接后端", api && api.state.online, api && api.state.online ? api.state.base : "未连接");
    check("右下角状态徽标已创建", !!document.getElementById("__wb_badge") ||
      !!Array.from(document.body.children).find((c) => c.style && c.style.position === "fixed"));

    // 让后端跑快一点，便于观察
    await api.control("pause", false);
    await api.control("speed", 4.0);
    await api.control("rate", 20);

    let maxTasks = 0;
    let sawLat = 0;
    const t1 = simAI.t;
    for (let i = 0; i < 30; i++) {
      await sleep(120);
      maxTasks = Math.max(maxTasks, simAI.tasks.length);
      sawLat = Math.max(sawLat, simAI.hist.lat.length);
    }
    check("后端正在推进仿真时钟", simAI.t > t1, t1.toFixed(1) + "ms → " + simAI.t.toFixed(1) + "ms");
    check("任务快照水合进 simAI.tasks", maxTasks > 0, "峰值在途任务 " + maxTasks);
    check("趋势序列水合", sawLat > 1, "采样点 " + sawLat);
    check("节点对象水合正确",
      simAI.nodeById.edgeA && typeof simAI.nodeById.edgeA.util === "number" &&
      simAI.nodeById.cloud.cap === 120, "cloud.cap=" + (simAI.nodeById.cloud && simAI.nodeById.cloud.cap));
    check("链路对象水合正确",
      simAI.linkById.field && typeof simAI.linkById.field.util === "number",
      "field.util=" + simAI.linkById.field.util.toFixed(4));
    check("基线轨道同样运行", simBL.mode === "baseline" && simBL.stat.n >= 0,
      "基线完成 " + simBL.stat.n + " 个任务");

    // 在线时本地 step 必须被短路
    const tBefore = simAI.t;
    step(simAI, 2, params, tBefore + 2);
    check("在线时本地 step() 被短路（无双重推进）", simAI.t === tBefore,
      "t=" + simAI.t.toFixed(3));

    // 渲染管线消费后端数据
    check("KPI 面板已渲染", document.getElementById("k1").textContent !== "" &&
      document.getElementById("k1").textContent !== "—", "#k1=" + document.getElementById("k1").textContent);
    check("节点卡片已渲染", /class="node"/.test(document.getElementById("nodes").innerHTML),
      "长度 " + document.getElementById("nodes").innerHTML.length);
    check("任务流水表已写入", document.getElementById("tbody").children.length > 0,
      "行数 " + document.getElementById("tbody").children.length);
    check("决策日志已写入", document.getElementById("log").innerHTML.length > 0);

    // ---------------- 新增面板：状态条 / KPI 走势 / HIL / 模型 / 实验 ----------------
    check("状态条：引擎与模型已渲染",
      document.getElementById("netText").textContent === "后端引擎" &&
      document.getElementById("modelText").textContent.length > 0,
      "引擎=" + document.getElementById("netText").textContent +
      " 模型=" + document.getElementById("modelText").textContent);
    check("KPI 迷你走势已绘制", document.getElementById("sp1").width === 800,
      "sp1 宽=" + document.getElementById("sp1").width);
    check("图表对照轨已接入（drawChart 双轨参数）", typeof UI === "object" && !!UI.renderHil);

    // 硬件在环面板：注册一台设备 → 刷新 → 表格出现该设备
    await fetch(BASE_ + "/api/hw/register", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deviceId: "edge-ui-01", kind: "edge", nodeId: "edgeA" })
    }).then((r) => r.json());
    await fetch(BASE_ + "/api/hw/telemetry", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deviceId: "edge-ui-01", node: { id: "edgeA", gpuUtil: 0.55, latMs: 7.2 } })
    }).then((r) => r.json());
    await window.Backend.refreshHil();
    await sleep(300);
    const hilHtml = document.getElementById("hilDevices").innerHTML;
    check("HIL 设备表渲染（含注册设备）", hilHtml.indexOf("edge-ui-01") >= 0,
      "长度 " + hilHtml.length);
    check("HIL 实测值展示", /GPU 55%/.test(hilHtml), "");

    const chipBefore = document.getElementById("hilText").textContent;
    await window.Backend.setHilMode("hil");
    await sleep(400);
    check("在环模式切换并反映到状态条",
      document.getElementById("hilText").textContent === "硬件在环",
      chipBefore + " → " + document.getElementById("hilText").textContent);

    // 模型面板：下拉由后端模型填充，说明卡片切换为后端模型
    await window.Backend.refreshAll();
    await sleep(300);
    check("模型下拉已由后端填充",
      document.getElementById("modelSel").innerHTML.indexOf("<option") >= 0,
      document.getElementById("modelSel").innerHTML.slice(0, 60));
    check("模型说明卡片切换为后端模型",
      /降采样|通算权衡|流体/.test(document.getElementById("modelCard").innerHTML),
      "卡片长度 " + document.getElementById("modelCard").innerHTML.length);

    // 实验面板：跑一个小实验并渲染结果
    await window.Backend.runExperiment({ durationMs: 6000, scenes: ["normal"] });
    let expOk = false;
    for (let i = 0; i < 40; i++) {
      await sleep(400);
      if (/exp-sum/.test(document.getElementById("expBody").innerHTML)) { expOk = true; break; }
    }
    check("实验面板渲染对照结果", expOk,
      "长度 " + document.getElementById("expBody").innerHTML.length);

    await window.Backend.setHilMode("off");
    await sleep(200);

    // 控制指令双写：改动下拉框 → 后端场景应变化
    const sel = document.getElementById("scene");
    const tBeforeSwitch = simAI.t;
    sel.value = "llm";
    sel.selectedOptions = [{ textContent: "大模型突发推理" }];
    sel.dispatch("change", { target: sel });
    await sleep(600);
    const h = await health();
    check("UI 控件改动同步到后端（场景 → llm）", h.sim.scene === "llm", "后端场景 " + h.sim.scene);
    check("后端按新场景重置时钟", h.sim.tSim < tBeforeSwitch,
      "切换前 " + tBeforeSwitch.toFixed(1) + "ms → 切换后 " + h.sim.tSim.toFixed(1) + "ms");

    // 断开 → 回退本地引擎
    api.disconnect();
    await sleep(200);
    check("断开后进入离线模式", api.state.online === false);
    const t2 = simAI.t;
    for (let i = 0; i < 200; i++) step(simAI, 5, params, simAI.t + 5);
    check("离线后本地引擎恢复推进", simAI.t > t2, t2.toFixed(1) + "ms → " + simAI.t.toFixed(1) + "ms");
    check("离线后本地引擎产生新任务流", simAI.nextId > 1,
      "累计到达 " + (simAI.nextId - 1) + " 个 / 完成 " + simAI.stat.n + " 个");

    return results;
  })();
}

globalThis.__DONE = function (results) {
  const bad = results.filter((r) => !r.ok);
  results.forEach((r) => console.log("  " + (r.ok ? "OK  " : "FAIL") + " " + r.name +
    (r.info ? "  [" + r.info + "]" : "")));
  console.log(JSON.stringify({ total: results.length, failed: bad.length }));
  process.exit(bad.length ? 1 : 0);
};

const driverSrc = "const BASE_ = " + JSON.stringify(BASE) + ";\n" +
  "(" + pageDriver.toString() + ")().then(function(r){ globalThis.__DONE(r); })" +
  ".catch(function(e){ console.log('DRIVER ERROR: ' + ((e && e.stack) || e)); process.exit(3); });";

try {
  vm.runInThisContext(scripts.join("\n;\n") + "\n;\n" + driverSrc, { filename: "page-bundle.js" });
} catch (e) {
  console.log("PAGE ERROR: " + (e && e.stack || e));
  process.exit(3);
}

setTimeout(() => { console.log("TIMEOUT: 驱动未在 60s 内结束"); process.exit(4); }, 60000);
