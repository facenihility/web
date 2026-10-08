const fs = require("fs");
const path = require("path");
/* 路径基于脚本自身位置推导：整个项目目录可以任意复制/改名 */
const TEST_DIR = __dirname;
const ROOT = path.dirname(TEST_DIR);
const DIR = TEST_DIR + path.sep;
const OUT = [];
const flush = () => fs.writeFileSync(DIR + "ui.txt", OUT.join("\n"), "utf8");
const log = (...a) => { OUT.push(a.join(" ")); flush(); };
const noop = () => {};
const calls = {};
function makeCtx(tag){
  const t={measureText:()=>({width:40}),
    createLinearGradient:()=>({addColorStop:noop}),
    createRadialGradient:()=>({addColorStop:noop}),
    canvas:{}};
  return new Proxy(t,{get(o,k){ if(k in o) return o[k]; calls[tag+"."+String(k)]=(calls[tag+"."+String(k)]||0)+1; return noop; },
    set(o,k,v){o[k]=v;return true;}});
}
const mkEl=(id)=>({id,innerHTML:"",textContent:"",value:"9",clientWidth:1000,height:470,style:{},
  children:[],dataset:{},selectedOptions:[{textContent:"x"}],
  classList:{add:noop,remove:noop,toggle:()=>true},
  addEventListener:noop,removeChild:noop,appendChild:noop,insertBefore:noop,
  querySelector:()=>null,querySelectorAll:()=>[],
  getContext:()=>makeCtx(id)});
const els={};
global.__els=els;
global.document={getElementById:(id)=>(els[id]=els[id]||mkEl(id)),createElement:mkEl};
global.window={devicePixelRatio:2,addEventListener:noop,clientWidth:1000};
global.requestAnimationFrame=noop; global.performance={now:()=>Date.now()};

const html = fs.readFileSync(path.join(ROOT, "工业智算网-通算协同调度仿真平台.html"), "utf8");
const RAW = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/requestAnimationFrame\(frame\);\s*$/, "");

const tests = `
const rep={};
function t(name,fn){ try{ fn(); rep[name]="OK"; }catch(e){ rep[name]="ERR: "+(e&&e.message||e); } }
/* 依次覆盖各场景，模拟真实渲染循环 */
t("reset",            ()=>reset());
t("step+draw(normal)",()=>{ params=cloneScene("normal"); for(let i=0;i<80;i++){ const tt=i*2; step(simAI,2,params,tt); step(simBL,2,params,tt); } drawTopo(simAI); });
t("updateUI(normal)", ()=>updateUI());
t("drawChart",        ()=>{ CHARTS.forEach(c=>drawChart(c,simAI)); });
t("scene=vision",     ()=>{ params=cloneScene("vision"); reset(); for(let i=0;i<80;i++){const tt=i*2; step(simAI,2,params,tt); step(simBL,2,params,tt);} drawTopo(simAI); updateUI(); });
t("scene=agv",        ()=>{ params=cloneScene("agv"); reset(); for(let i=0;i<80;i++){const tt=i*2; step(simAI,2,params,tt); step(simBL,2,params,tt);} drawTopo(simAI); updateUI(); });
t("scene=llm",        ()=>{ params=cloneScene("llm"); reset(); for(let i=0;i<80;i++){const tt=i*2; step(simAI,2,params,tt); step(simBL,2,params,tt);} drawTopo(simAI); updateUI(); });
t("scene=degrade",    ()=>{ params=cloneScene("degrade"); reset(); for(let i=0;i<80;i++){const tt=i*2; step(simAI,2,params,tt); step(simBL,2,params,tt);} drawTopo(simAI); updateUI(); });
t("scene=stress",     ()=>{ params=cloneScene("stress"); reset(); for(let i=0;i<80;i++){const tt=i*2; step(simAI,2,params,tt); step(simBL,2,params,tt);} drawTopo(simAI); updateUI(); });
t("frame()",          ()=>{ frame(1e6); });
t("视图切到基线",       ()=>{ viewMode="bl"; updateUI(); drawTopo(simBL); CHARTS.forEach(c=>drawChart(c,simBL)); });
t("基线视图-log占位",   ()=>{ const lg=global.__els["log"]; if(!lg||!/静态规则基线不做在线推理/.test(lg.innerHTML)) throw new Error("日志占位未生效: "+((lg&&lg.innerHTML)||"").slice(0,60)); });
t("切换回AI视图",       ()=>{ viewMode="ai"; updateUI(); });
t("sizeTopo/sizeCharts",()=>{ sizeTopo(); sizeCharts(); });
__OUT__(rep, global.__els);
`;

const capture = (rep, els)=>{
  log("=== 渲染路径冒烟 ===");
  for(const k of Object.keys(rep)) log((rep[k]==="OK"?"  OK   ":"  FAIL ")+k+(rep[k]==="OK"?"":"  → "+rep[k]));
  log("");
  log("=== 关键 DOM 是否被写入 ===");
  for(const id of ["kpi","cmp","nodes","links","log","tbody","topo"]){
    const e=els[id];
    log("  #"+id.padEnd(7)+(e? ("  htmlLen="+String((e.innerHTML||"").length)) : "  (未创建)"));
  }
  const bad=Object.entries(rep).filter(([k,v])=>v!=="OK");
  log(""); log(bad.length? ("!!! 失败 "+bad.length+" 项") : "全部通过");
};
try { new Function("global","log","__OUT__",RAW+tests)(global,log,capture); }
catch(e){ log("RUN ERROR: "+(e&&e.stack||e)); }
flush();
