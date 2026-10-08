/**
 * 一致性验证：直接从原始前端 HTML 中抽取 JS 仿真内核并运行。
 *
 *   node parity_harness.js <html路径> [dt_ms] [duration_ms]
 *
 * 输出 JSON，供 tests/test_parity.py 与 Python 移植版逐项比对。
 */
"use strict";
const fs = require("fs");
const path = require("path");

const htmlPath = process.argv[2] || path.join(__dirname, "..", "..", "工业智算网-通算协同调度仿真平台.html");
const DT = Number(process.argv[3] || 2);
const DUR = Number(process.argv[4] || 120000);

const html = fs.readFileSync(htmlPath, "utf8");

/* 取出 <script> 主体 */
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error("未找到 script 块"); process.exit(2); }
let src = m[1];

/* 只保留「仿真内核」：截到「运行状态」章节之前（其后的代码依赖 DOM） */
const cut = src.indexOf("   运行状态");
if (cut < 0) { console.error("未找到运行状态分节标记"); process.exit(2); }
src = src.slice(0, src.lastIndexOf("/*", cut));

const driver = `
const out={};
for(const scene of Object.keys(SCENES)){
  const p=cloneScene(scene);
  const ai=makeSim("ai",20261006), bl=makeSim("baseline",20261006);
  rrCounter=0;
  const dt=${DT}, dur=${DUR}, n=Math.round(dur/dt);
  let t=0;
  for(let i=0;i<n;i++){ t=(i+1)*dt; step(ai,dt,p,t); step(bl,dt,p,t); }
  out[scene]={ai:summ(ai),bl:summ(bl)};
}
return JSON.stringify(out);
function summ(s){
  const d={local:0,edgeA:0,edgeB:0,cloud:0};
  for(const k in s.typeStat) for(const n in s.typeStat[k].node) d[n]=(d[n]||0)+s.typeStat[k].node[n];
  return {
    n:s.stat.n, t:s.t, latSum:s.stat.latSum, p95:p95(s), miss:s.stat.miss,
    energy:s.stat.energy, throughput:s.throughput, backlog:s.tasks.length,
    arrivals:s.nextId-1, backlogTrace:s.backlogTrace,
    nodes:s.nodes.map(x=>({id:x.id,done:x.done,util:x.util,utilS:x.utilS,backlog:x.backlog,pending:x.pending})),
    links:s.links.map(x=>({id:x.id,util:x.util,utilS:x.utilS,load:x.load})),
    dist:d, stat:{local:s.stat.local,edge:s.stat.edge,cloud:s.stat.cloud},
    typeStat:s.typeStat, hist:{lat:s.hist.lat.length,p95:s.hist.p95.length,bw:s.hist.bw.length,gpu:s.hist.gpu.length}
  };
}
`;

const fn = new Function(src + "\n" + driver);
process.stdout.write(fn());
