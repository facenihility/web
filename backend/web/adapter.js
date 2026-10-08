/* =============================================================================
 * 工业智算网 · 后端接入层（适配器）
 * -----------------------------------------------------------------------------
 * 设计要点（保持「后端权威仿真 + 本地离线引擎」两者兼顾）：
 *   1. 不修改原页面任何一行渲染/仿真代码，只作为第二个 <script> 覆盖两个入口：
 *        - step()：在线时短路，禁止浏览器重复推进仿真（后端才是权威时钟）
 *        - p95() ：在线时直接取后端算好的分位值
 *   2. 通过 SSE 接收后端 10Hz 状态快照，把 simAI / simBL 两个对象「原地水合」
 *      （属性级写入，保持对象与数组引用语义），原有的 drawTopo / drawChart /
 *      updateUI 全部零改动地渲染后端数据。
 *   3. 后端不可达时自动回退为原始纯前端引擎，页面功能完全不受影响。
 *   4. 控制指令（场景 / 强度 / 速度 / 暂停 / 重置）双写：本地生效 + 后端同步。
 * ========================================================================== */
(function () {
  "use strict";

  if (typeof simAI === "undefined" || typeof simBL === "undefined") {
    return; // 页面不完整（缺少引擎），不做任何处理
  }

  var BASES = window.__DSH_BACKEND_BASES__ || ["", "http://127.0.0.1:8787", "http://localhost:8787"];

  var S = {
    base: "",
    online: false,
    everOnline: false,
    es: null,
    poll: null,
    probe: null,
    lastMsg: 0,
    msgCount: 0,
    hz: 0,
    fails: 0,
    snap: null
  };

  var badgeEl, dotEl, txtEl;

  /* ------------------------------------------------------------------ 工具 */
  function $(id) { return document.getElementById(id); }

  function api(path, opts, timeoutMs) {
    var url = S.base + path;
    if (!timeoutMs || typeof AbortController === "undefined") return fetch(url, opts);
    var ac = new AbortController();
    var timer = setTimeout(function () { ac.abort(); }, timeoutMs);
    var o = opts || {};
    o.signal = ac.signal;
    return fetch(url, o).then(function (r) {
      clearTimeout(timer);
      return r;
    }, function (e) {
      clearTimeout(timer);
      throw e;
    });
  }

  function probeBase(base) {
    return new Promise(function (resolve) {
      var ac = typeof AbortController !== "undefined" ? new AbortController() : null;
      var timer = ac ? setTimeout(function () { ac.abort(); }, 1600) : null;
      var init = { cache: "no-store" };
      if (ac) init.signal = ac.signal;
      fetch(base + "/api/health", init).then(function (r) {
        if (timer) clearTimeout(timer);
        return r.ok ? r.json() : null;
      }).then(function (j) {
        resolve(j && j.ok ? { base: base, health: j } : null);
      }).catch(function () {
        if (timer) clearTimeout(timer);
        resolve(null);
      });
    });
  }

  /* ------------------------------------------------------- 后端状态 → UI 同步 */
  function syncFromState(st) {
    if (!st) return;
    var sel = $("scene");
    if (sel && st.scene && sel.value !== st.scene) {
      sel.value = st.scene;
      if (sel.selectedOptions && sel.selectedOptions[0] && $("topoTag")) {
        $("topoTag").textContent = sel.selectedOptions[0].textContent;
      }
    }
    try {
      params = cloneScene(st.scene);
      params.rate = st.rate;
    } catch (e) { /* 忽略 */ }

    var r = $("rate");
    if (r) { r.value = st.rate; if ($("rateV")) $("rateV").textContent = st.rate + "/s"; }

    simSpeed = st.speed;
    var sp = $("speed");
    if (sp) {
      sp.value = Math.max(5, Math.min(40, Math.round(st.speed * 10)));
      if ($("speedV")) $("speedV").textContent = st.speed.toFixed(1) + "×";
    }

    paused = !!st.paused;
    var bp = $("btnPause");
    if (bp) { bp.textContent = paused ? "继续" : "暂停"; bp.classList.toggle("on", paused); }

    viewMode = st.view === "bl" ? "bl" : "ai";
  }

  /* --------------------------------------------- 快照 → 仿真对象「原地水合」 */
  function hydrateTasks(sim, snap, seen) {
    sim._tm = sim._tm || {};
    var pool = sim._tm, list = [], i, t, o;
    for (i = 0; i < snap.tasks.length; i++) {
      t = snap.tasks[i];
      o = pool[t.id];
      if (!o) { o = { id: t.id, type: t.type, T: TYPES[t.type] }; pool[t.id] = o; }
      o.state = t.state;
      o.nodeId = t.nodeId;
      o.devId = t.devId;
      o.devY = t.devY;
      o.visT = t.visT;
      o.visDur = t.visDur;
      o.deadline = t.deadline;
      o.prio = t.prio;
      o.bwEff = t.bwEff;
      o.workLeft = t.workLeft;
      o.bytesLeft = t.bytesLeft;
      if (!o.path || o._pn !== t.nodeId || o._py !== t.devY) {
        o.path = buildPath(t.nodeId, t.devY);
        o._pn = t.nodeId;
        o._py = t.devY;
      }
      seen[t.id] = 1;
      list.push(o);
    }
    for (var k in pool) { if (!seen[k]) delete pool[k]; }
    sim.tasks = list;
  }

  function hydrate(sim, snap) {
    if (!sim || !snap) return;
    sim.t = snap.t;
    sim.throughput = snap.throughput;
    sim.mode = snap.mode;
    sim.limitHit = snap.limitHit;

    var d = snap.stat, st = sim.stat;
    st.n = d.n; st.latSum = d.latSum; st.miss = d.miss; st.energy = d.energy;
    st.work = d.work; st.bytes = d.bytes; st.cloud = d.cloud; st.edge = d.edge;
    st.local = d.local; st.active = d.active; st.p95 = d.p95;

    var seen = {};
    hydrateTasks(sim, snap, seen);

    sim._nm = sim._nm || {};
    var nPool = sim._nm, nodes = [], byId = {}, i, j, n, no, run, tk;
    for (i = 0; i < snap.nodes.length; i++) {
      n = snap.nodes[i];
      no = nPool[n.id];
      if (!no) {
        no = { id: n.id, name: n.name, tag: n.tag, color: n.color, cap: n.cap,
               x: n.x, y: n.y, w: n.w, h: n.h, lat: n.lat, reserve: n.reserve };
        nPool[n.id] = no;
      }
      no.util = n.util; no.utilS = n.utilS; no.backlog = n.backlog; no.pending = n.pending;
      no.done = n.done; no.latSum = n.latSum; no.workSum = n.workSum;
      no.rtLoad = n.rtLoad; no.loLoad = n.loLoad;
      run = [];
      for (j = 0; j < n.running.length; j++) {
        tk = sim._tm[n.running[j]];
        if (tk) run.push(tk);
      }
      no.running = run;
      nodes.push(no);
      byId[n.id] = no;
    }
    sim.nodes = nodes;
    sim.nodeById = byId;

    sim._lm = sim._lm || {};
    var lPool = sim._lm, links = [], lById = {}, l, lo;
    for (i = 0; i < snap.links.length; i++) {
      l = snap.links[i];
      lo = lPool[l.id];
      if (!lo) { lo = { id: l.id, name: l.name, cap: l.cap, color: l.color }; lPool[l.id] = lo; }
      lo.capS = l.capS; lo.demand = l.demand; lo.act = l.act; lo.load = l.load;
      lo.util = l.util; lo.utilS = l.utilS;
      links.push(lo);
      lById[l.id] = lo;
    }
    sim.links = links;
    sim.linkById = lById;

    sim.table = snap.table;
    sim.log = snap.log;
    sim.typeStat = snap.typeStat;
    sim.hist = snap.hist;
  }

  /* --------------------------------------------------------- 覆盖仿真入口点 */
  var _origStep = step;
  var _origP95 = p95;

  step = function (s, dt, p, t) {
    if (S.online) return;          // 在线：后端是唯一权威时钟，浏览器不再推进
    return _origStep(s, dt, p, t);
  };

  p95 = function (s) {
    if (S.online && s && s.stat && typeof s.stat.p95 === "number") return s.stat.p95;
    return _origP95(s);
  };

  /* ----------------------------------------------------------------- 推流 */
  function applySnapshot(snap) {
    if (!snap) return;
    S.snap = snap;
    S.lastMsg = Date.now();
    S.msgCount++;
    if (S.winStart === undefined) { S.winStart = Date.now(); S.winCount = 0; }
    S.winCount++;
    var dtWin = Date.now() - S.winStart;
    if (dtWin >= 1000) {
      S.hz = Math.round(S.winCount * 1000 / dtWin);
      S.winStart = Date.now();
      S.winCount = 0;
    }
    hydrate(simAI, snap.ai);
    hydrate(simBL, snap.bl);
    try {
      UI.net(true, snap);
      UI.pump("推流 " + (S.hz || 0) + "Hz · 仿真 T+" + (snap.tSim / 1000).toFixed(0) + "s · 客户端 " + snap.clients);
    } catch (e) { /* 忽略 */ }
    updateBadge();
  }

  function openStream() {
    if (S.es) { try { S.es.close(); } catch (e) {} S.es = null; }
    if (S.poll) { clearInterval(S.poll); S.poll = null; }
    if (typeof EventSource === "undefined") { startPolling(); return; }
    var url = S.base + "/api/stream";
    var es;
    try { es = new EventSource(url); } catch (e) { startPolling(); return; }
    S.es = es;
    es.addEventListener("state", function (ev) {
      S.fails = 0;
      try { applySnapshot(JSON.parse(ev.data)); } catch (e) { /* 忽略坏帧 */ }
    });
    es.addEventListener("hello", function () { S.fails = 0; });
    es.onerror = function () {
      S.fails++;
      if (S.fails >= 4) goOffline("推流中断");
    };
  }

  function startPolling() {
    S.poll = setInterval(function () {
      api("/api/state", { cache: "no-store" }, 2000).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (j) {
        if (j && j.snapshot) applySnapshot(j.snapshot);
      }).catch(function () {
        S.fails++;
        if (S.fails >= 6) goOffline("轮询失败");
      });
    }, 250);
  }

  /* ------------------------------------------------------------- 连接管理 */
  function connect(base, health) {
    S.base = base;
    S.online = true;
    S.everOnline = true;
    S.fails = 0;
    if (health && health.sim) syncFromState(health.sim);
    openStream();
    // 让后端对齐当前 UI 参数，避免「界面 9/s、后端 11/s」这类漂移
    post("scene", $("scene") ? $("scene").value : "normal", true);
    post("rate", params ? params.rate : 11, true);
    post("speed", simSpeed, true);
    post("pause", !!paused, true);
    updateBadge();
    startPanels();
    refreshAll();
  }

  function goOffline(reason) {
    var was = S.online;
    S.online = false;
    if (S.es) { try { S.es.close(); } catch (e) {} S.es = null; }
    if (S.poll) { clearInterval(S.poll); S.poll = null; }
    stopPanels();
    if (was) {
      // 丢弃被水合过的渲染镜像，重建干净的本地引擎状态
      try { reset(); } catch (e) { /* 忽略 */ }
      try { updateUI(); } catch (e) { /* 忽略 */ }
    }
    try {
      UI.net(false, null);
      UI.pump("本地引擎 · 无推流");
      UI.renderHil(null);
      UI.expStatus({ running: false, error: null });
      UI.hilHint("未连接后端 · 当前由页面内置引擎（v1）运行；接入硬件需先启动后端。");
    } catch (e) { /* 忽略 */ }
    updateBadge(reason);
  }

  function autoConnect() {
    if (S.probe) return;
    var i = 0;
    S.probe = (function next() {
      if (i >= BASES.length || S.online) { S.probe = null; return; }
      return probeBase(BASES[i++]).then(function (hit) {
        if (hit) {
          S.probe = null;
          connect(hit.base, hit.health);
          return null;
        }
        return next();
      });
    })();
  }

  function post(action, value, silent) {
    if (!S.online) return Promise.resolve(null);
    return api("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action, value: value })
    }, 3000).then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { if (!silent) updateBadge("指令失败"); return null; });
  }

  /* --------------------------------------------------------------- 状态徽标 */
  function updateBadge(extra) {
    if (!badgeEl) return;
    if (S.online) {
      badgeEl.style.background = "#0A2A5E";
      dotEl.style.background = S.lastMsg && Date.now() - S.lastMsg > 2000 ? "#F59E0B" : "#4ADE80";
      var label = S.snap ? "T+" + (S.snap.ai.t / 1000).toFixed(1) + "s" : "连接中";
      txtEl.textContent = "后端引擎 · " + label + (S.hz ? " · " + S.hz + "Hz" : "");
    } else {
      badgeEl.style.background = "#7A2E00";
      dotEl.style.background = "#F59E0B";
      txtEl.textContent = "本地离线引擎" + (extra ? " · " + extra : "");
    }
  }

  function buildBadge() {
    badgeEl = document.createElement("div");
    badgeEl.style.cssText =
      "position:fixed;right:14px;bottom:14px;z-index:99999;display:flex;align-items:center;" +
      "gap:8px;color:#fff;border-radius:20px;padding:6px 12px;cursor:pointer;" +
      "font:12px/1.4 'Segoe UI','Microsoft YaHei',system-ui,sans-serif;" +
      "box-shadow:0 6px 18px rgba(10,42,94,.35);transition:background .25s";
    badgeEl.title = "点击切换：后端权威引擎 / 本地内置引擎";
    dotEl = document.createElement("span");
    dotEl.style.cssText = "width:8px;height:8px;border-radius:50%;background:#F59E0B;flex:none";
    txtEl = document.createElement("span");
    txtEl.textContent = "探测后端…";
    badgeEl.appendChild(dotEl);
    badgeEl.appendChild(txtEl);
    badgeEl.addEventListener("click", function () {
      if (S.online) goOffline("已手动切换");
      else { txtEl.textContent = "重新探测后端…"; S.fails = 0; autoConnect(); }
    });
    document.body.appendChild(badgeEl);
    updateBadge();
  }

  /* --------------------------------------------------- 面板数据（HIL / 模型 / 实验） */
  function startPanels() {
    stopPanels();
    S.panelTimer = setInterval(function () {
      if (!S.online) return;
      if (S.hilBusy) return;
      refreshHil();
    }, 1500);
    S.modelTimer = setInterval(function () {
      if (!S.online) return;
      refreshModels();
      refreshExperimentState();
    }, 4000);
  }

  function stopPanels() {
    if (S.panelTimer) { clearInterval(S.panelTimer); S.panelTimer = null; }
    if (S.modelTimer) { clearInterval(S.modelTimer); S.modelTimer = null; }
    if (S.expTimer) { clearInterval(S.expTimer); S.expTimer = null; }
  }

  function refreshHil() {
    if (!S.online) return Promise.resolve(null);
    S.hilBusy = true;
    return api("/api/hw/devices", { cache: "no-store" }, 4000).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (j) {
      S.hilBusy = false;
      if (!j) return null;
      UI.renderHil(j);
      S.hil = j;
      return j;
    }).catch(function () { S.hilBusy = false; return null; });
  }

  function refreshModels() {
    if (!S.online) return Promise.resolve(null);
    return api("/api/models", { cache: "no-store" }, 4000).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (j) {
      if (!j) return null;
      return api("/api/model", { cache: "no-store" }, 4000).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (a) {
        UI.renderModels(j.models, a ? a.active : j.default, a ? a.model : null);
        S.models = j;
        return j;
      });
    }).catch(function () { return null; });
  }

  function refreshExperimentState() {
    if (!S.online) return Promise.resolve(null);
    return api("/api/experiment", { cache: "no-store" }, 4000).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (j) {
      if (!j) return null;
      var st = j.experiment;
      UI.expStatus(st);
      if (st.running) {
        if (!S.expTimer) {
          S.expTimer = setInterval(function () { checkExperimentDone(st.startedAt); }, 900);
        }
      }
      return st;
    }).catch(function () { return null; });
  }

  function checkExperimentDone(startedAt) {
    api("/api/experiment", { cache: "no-store" }, 4000).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (j) {
      if (!j) return;
      UI.expStatus(j.experiment);
      if (!j.experiment.running) {
        if (S.expTimer) { clearInterval(S.expTimer); S.expTimer = null; }
        loadLatestReport();
      }
    }).catch(function () {});
  }

  function loadLatestReport() {
    return api("/api/report?format=json", { cache: "no-store" }, 8000).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (j) {
      if (j && j.run) UI.renderExperiment(j.run);
      return j;
    }).catch(function () { return null; });
  }

  function refreshWeights() {
    if (!S.online) return Promise.resolve(null);
    return api("/api/nodes/weights", { cache: "no-store" }, 4000).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (j) {
      if (!j) return null;
      UI.renderWeights(j.state);
      S.weights = j.state;
      return j.state;
    }).catch(function () { return null; });
  }

  function refreshAll() {
    if (!S.online) { autoConnect(); return Promise.resolve(null); }
    return Promise.all([
      api("/api/health", { cache: "no-store" }, 4000).then(function (r) { return r.ok ? r.json() : null; }),
      refreshHil(), refreshModels(), refreshExperimentState(), refreshWeights()
    ]).then(function (res) {
      var h = res[0];
      if (h && h.sim) {
        UI.net(true, h.sim);
        S.sim = h.sim;
      }
      if (S.hil && S.hil.health) {
        UI.hilHint(S.hil.health.mode === "hil"
          ? '硬件在环生效中：实测的 GPU 利用率 / 链路速率 / 时延正在<b>覆盖</b>仿真内部状态，调度决策基于真实世界。'
          : (S.hil.health.mode === "shadow"
            ? '影子模式：硬件数据仅用于「实测 vs 仿真」对照，<b>不影响</b>仿真。验证链路与量纲时先用这个模式。'
            : '纯仿真模式：硬件只做登记与对照，不参与仿真；切到「硬件在环」即可让实测数据接管。'));
      }
      return loadLatestReport();
    }).catch(function () { return null; });
  }

  /* ------------------------------------------------------------ 面板操作入口 */
  window.Backend = {
    refreshAll: refreshAll,
    refreshHil: function () { refreshHil(); UI.toast("设备状态已刷新"); },
    setHilMode: function (mode) {
      if (!S.online) { UI.toast("切换在环模式需要连接后端", "warn"); return Promise.resolve(null); }
      return api("/api/hw/mode", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: mode })
      }, 5000).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (j && j.ok) { UI.toast("在环模式 → " + ({ off: "纯仿真", shadow: "影子模式", hil: "硬件在环" }[j.mode] || j.mode)); refreshAll(); }
        else UI.toast("切换失败：" + ((j && j.error) || "未知错误"), "bad");
        return j;
      }).catch(function () { UI.toast("切换失败：后端不可达", "bad"); return null; });
    },
    setModel: function (id) {
      if (!S.online) { UI.toast("切换模型需要连接后端", "warn"); return Promise.resolve(null); }
      return api("/api/model", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: id })
      }, 6000).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (j && j.ok) { UI.toast("已切换到 " + (j.model.short || id) + "（仿真已重置）"); refreshAll(); }
        else UI.toast("切换失败：" + ((j && j.error) || "未知错误"), "bad");
        return j;
      }).catch(function () { UI.toast("切换失败：后端不可达", "bad"); return null; });
    },
    runExperiment: function (opts) {
      if (!S.online) { UI.toast("批量实验需要连接后端", "warn"); return Promise.resolve(null); }
      opts = opts || {};
      var body = { async: true, durationMs: opts.durationMs || 120000 };
      if (opts.scenes && opts.scenes.length) body.scenes = opts.scenes;
      UI.expStatus({ running: true });
      UI.toast("实验已提交，正在离线跑场景矩阵…");
      return api("/api/experiment", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      }, 10000).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (!j || !j.ok) { UI.toast("提交失败：" + ((j && j.error) || "未知错误"), "bad"); UI.expStatus({ running: false }); return null; }
        refreshExperimentState();
        return j;
      }).catch(function () { UI.toast("提交失败：后端不可达", "bad"); UI.expStatus({ running: false }); return null; });
    },
    openSchema: function () {
      if (!S.online) { UI.toast("接口文档需要连接后端", "warn"); return; }
      window.open(S.base + "/api/hw/schema", "_blank");
    },
    setWeights: function (weights, locks) {
      if (!S.online) { UI.toast("设置先验偏置需要连接后端", "warn"); UI.resetWeightsUI(); return Promise.resolve(null); }
      var body = { weights: weights };
      if (locks) body.locks = locks;
      return api("/api/nodes/weights", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      }, 6000).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (j && j.ok) {
          UI.renderWeights(j.weights);
          UI.toast("先验偏置已应用（权重仍由算法动态分配）");
          refreshAll();
        } else {
          UI.toast("设置失败：" + ((j && j.error) || "未知错误"), "bad");
          refreshWeights();
        }
        return j;
      }).catch(function () { UI.toast("设置失败：后端不可达", "bad"); return null; });
    },
    resetWeights: function () {
      if (!S.online) { UI.toast("清除偏置需要连接后端", "warn"); UI.resetWeightsUI(); return Promise.resolve(null); }
      return api("/api/nodes/weights", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reset: true })
      }, 6000).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (j && j.ok) { UI.renderWeights(j.weights); UI.toast("先验偏置已清除（权重完全由算法动态分配）"); }
        return j;
      }).catch(function () { return null; });
    }
  };

  /* --------------------------------------------------- 控制指令双写（本地+后端） */
  function bindControls() {
    var el;
    if ((el = $("scene"))) {
      el.addEventListener("change", function (e) { post("scene", e.target.value); });
    }
    if ((el = $("rate"))) {
      var t1 = null;
      el.addEventListener("input", function (e) {
        clearTimeout(t1);
        var v = +e.target.value;
        t1 = setTimeout(function () { post("rate", v); }, 150);
      });
    }
    if ((el = $("speed"))) {
      var t2 = null;
      el.addEventListener("input", function () { clearTimeout(t2); t2 = setTimeout(function () { post("speed", simSpeed); }, 150); });
    }
    if ((el = $("btnPause"))) {
      el.addEventListener("click", function () { post("pause", !!paused); });
    }
    if ((el = $("btnReset"))) {
      el.addEventListener("click", function () { post("reset", 1); });
    }
    if ((el = $("btnAI"))) {
      el.addEventListener("click", function () { post("view", viewMode); });
    }
  }

  /* ------------------------------------------------------------------ 启动 */
  buildBadge();
  bindControls();
  autoConnect();

  // 离线时每 10s 重试一次（服务起来后自动接管）
  setInterval(function () {
    if (!S.online && !S.probe) autoConnect();
  }, 10000);

  // 暴露给控制台/外部脚本，便于调试与自动化
  window.WorkBuddyBackend = {
    state: S,
    connect: function (base) { return probeBase(base || "").then(function (hit) { if (hit) connect(hit.base, hit.health); return !!hit; }); },
    disconnect: function () { goOffline("手动断开"); },
    snapshot: function () { return S.snap; },
    control: post
  };
})();
