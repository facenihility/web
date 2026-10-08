const fs = require("fs");
const path = require("path");
/* 路径基于脚本自身位置推导：整个项目目录可以任意复制/改名 */
const TEST_DIR = __dirname;
const ROOT = path.dirname(TEST_DIR);
const DIR = TEST_DIR + path.sep;
const OUT = [];
const flush = () => fs.writeFileSync(DIR + "report.txt", OUT.join("\n"), "utf8");
const log = (...a) => { OUT.push(a.join(" ")); flush(); };
console.log = log;
const html = fs.readFileSync(path.join(ROOT, "工业智算网-通算协同调度仿真平台.html"), "utf8");
let src = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/requestAnimationFrame\(frame\);\s*$/, "");
const noop = () => {};
function makeCtx(){const t={measureText:()=>({width:40}),createLinearGradient:()=>({addColorStop:noop}),canvas:{}};return new Proxy(t,{get(o,k){return k in o?o[k]:noop;},set(o,k,v){o[k]=v;return true;}});}
const mkEl=(id)=>({id,innerHTML:"",textContent:"",value:"9",clientWidth:1000,style:{},children:[],dataset:{},selectedOptions:[{textContent:"x"}],classList:{add:noop,remove:noop,toggle:()=>true},addEventListener:noop,removeChild:noop,appendChild:noop,insertBefore:noop,querySelector:()=>null,querySelectorAll:()=>[],getContext:makeCtx});
const els={};
global.document={getElementById:(id)=>(els[id]=els[id]||mkEl(id)),createElement:mkEl};
global.window={devicePixelRatio:1,addEventListener:noop,clientWidth:1000};
global.requestAnimationFrame=noop; global.performance={now:()=>Date.now()};

const tests = `
function run(sceneName, seconds, dt){
  params = cloneScene(sceneName); reset();
  const steps = Math.round(seconds*1000/dt), trace=[];
  for(let i=0;i<steps;i++){
    const tt=i*dt;
    step(simAI, dt, params, tt); step(simBL, dt, params, tt);
    if(i % Math.round(10000/dt) === 0) trace.push(simAI.tasks.length);
  }
  const f=(s)=>({n:s.stat.n, lat:avgLat(s), p95:p95(s),
    miss:s.stat.n?s.stat.miss/s.stat.n*100:0,
    gpu:s.nodes.reduce((a,n)=>a+clamp(n.util,0,1),0)/s.nodes.length*100,
    bw:Math.max(...s.links.map(l=>clamp(l.util,0,1)))*100,
    e:energyPerTask(s), lec:s.stat.local+"|"+s.stat.edge+"|"+s.stat.cloud,
    active:s.tasks.length, tp:s.throughput,
    nu:s.nodes.map(n=>n.id.slice(0,5)+":"+(n.util*100).toFixed(0)).join(" "),
    lu:s.links.map(l=>l.id.slice(0,4)+":"+(l.util*100).toFixed(0)).join(" "),
    ts:s.typeStat});
  return {ai:f(simAI), bl:f(simBL), trace};
}
function show(name,r){
  log("");
  log("===== "+name+" =====");
  log("        n     均时延   P95   超时%  GPU%  峰值BW%  单位能耗   本地|边缘|云端  积压 到达/吞吐");
  for(const k of ["ai","bl"]){ const v=r[k];
    log((k==="ai"?"AI  :":"基线:").padEnd(6)+String(v.n).padStart(5)+" "+v.lat.toFixed(1).padStart(8)+" "+
      v.p95.toFixed(0).padStart(5)+" "+v.miss.toFixed(1).padStart(6)+" "+v.gpu.toFixed(0).padStart(5)+" "+
      v.bw.toFixed(0).padStart(8)+" "+v.e.toFixed(4).padStart(8)+"   "+v.lec.padStart(13)+" "+
      String(v.active).padStart(4)+" "+params.rate+"/"+v.tp.toFixed(0));
  }
  log("节点 AI: "+r.ai.nu+"   链路: "+r.ai.lu);
  log("节点 基: "+r.bl.nu+"   链路: "+r.bl.lu);
  for(const k of ["vision","robot","agv","llm","maint"]){
    const a=r.ai.ts[k], b=r.bl.ts[k];
    if(!a&&!b) continue;
    const f=(t)=>t?("n="+String(t.n).padStart(4)+" 时延"+String((t.lat/t.n).toFixed(0)).padStart(6)+" 超时"+(t.miss/t.n*100).toFixed(0).padStart(3)+"% "+JSON.stringify(t.node)):"—";
    log("  "+k.padEnd(7)+"AI "+f(a));
    log("  "+"".padEnd(7)+"基 "+f(b));
  }
  log("积压轨迹: "+r.trace.join(" "));
}
const scenes=["normal","vision","agv","llm","degrade","stress"];
for(const sc of scenes) show(sc, run(sc, 120, 2));
`;
try { new Function("global","log",src+tests)(global,log); log(""); log("DONE"); }
catch(e){ log("RUN ERROR: "+(e&&e.stack||e)); }
flush();
