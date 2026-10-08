# 工业智算网 · 通算协同调度平台 —— 后端服务

面向智能制造的「通信资源 + 计算资源」联合优化仿真平台 —— **服务端实现 + 硬件在环接入**。
纯 **Python 标准库**，零第三方依赖（不需要 pip install、不需要 Node）。

前端 `工业智算网-通算协同调度仿真平台.html` 是一个纯浏览器端 Demo；
本后端把其中的仿真内核完整移植到服务端，使其成为权威计算源，并在此之上提供三件事：

| 能力 | 说明 |
| --- | --- |
| **① 前端界面优化** | 状态条、KPI 迷你走势、硬件在环面板、模型与实验面板、**节点权重面板**、图表双轨对照、模型说明动态化 |
| **② 硬件在环（HIL）接口** | 真实边缘盒子 / 相机 / AGV / 交换机可接入：HTTP + UDP + TCP 遥测上行，调度指令下行，三种在环模式，掉线自动失效 |
| **③ 调度模型优化** | v2 模型：结构性的排队/占用/违约口径修正 + **通算权衡（拥塞时降采样上传，用算力换带宽）** + **节点权重与智能分配** |

---

## 1. 快速开始

```bat
:: 方式一：双击启动脚本（已默认开启硬件在环监听）
启动后端.cmd

:: 方式二：命令行
cd C:\Users\h'b'y\WorkBuddy\2026-10-06-23-05-36
python -m backend.run                                   :: http://127.0.0.1:8787
python -m backend.run --port 9000 --scene stress --speed 2.0
python -m backend.run --hw-mode hil                     :: 启动即进入硬件在环
python -m backend.run --udp-port 8790 --tcp-port 8791    :: 指定遥测端口（默认即此）
python -m backend.run --udp-port 0 --tcp-port 0          :: 只留 HTTP 遥测
```

浏览器打开 <http://127.0.0.1:8787/>。顶部状态条会显示当前引擎、模型、在环模式与设备数；
右下角徽标显示推流频率，点击可手动切换「后端引擎 / 本地引擎」。

| 地址 | 说明 |
| --- | --- |
| `/` | 后端驱动版页面（自动接入后端，探测不到则回退本地引擎） |
| `/classic` | 原始纯前端页面（未注入接入层，离线也能跑） |
| `/api/hw/schema` | **硬件接入接口契约**（字段、单位、示例，给硬件方看） |

### 没有硬件时先跑通全链路

```bat
:: 另开一个终端（后端已在 8787 运行）
python -m backend.tools.hw_device_sim --duration 30 --verbose
python -m backend.tools.hw_device_sim --transport udp --duration 20
python -m backend.tools.hw_device_sim --transport tcp --duration 20
```

模拟 7 台设备（2 台边缘盒子、2 台交换机、相机 / AGV / 机器人控制器），
周期上报遥测与真实任务、长轮询领取调度指令并确认 —— 这个脚本同时就是**接入模板**。

---

## 2. 架构

```
                       ┌──────────────────── 浏览器 ────────────────────┐
                       │  工业智算网-…-后端版.html（单文件、零依赖）      │
                       │   ├─ 仿真/渲染/面板代码（HTML 内联，离线可用）   │
                       │   └─ 接入层 adapter.js（注入）                  │
                       │        · 覆盖 step()/p95()：在线时后端是权威时钟 │
                       │        · SSE 快照 → 原地水合 simAI / simBL      │
                       │        · 面板数据 → UI.renderHil / renderModels │
                       │        · 控件指令双写（本地生效 + POST 后端）    │
                       └──────┬──────────────────────────────▲───────────┘
                    REST 控制  │                              │ SSE 10Hz + 面板轮询
              ┌───────────────▼──────────────────────────────┴───────────┐
              │ backend/server.py   HTTP（标准库 http.server）            │
              │  · 静态托管 /  /classic  /static/*                        │
              │  · 仿真控制 /api/control /api/config /api/reset /api/model│
              │  · 查询 /api/state /api/metrics /api/tasks /api/logs      │
              │  · 实验 /api/experiment /api/report /api/runs             │
              │  · 硬件 /api/hw/{register,telemetry,commands,ack,mode,…}  │
              ├──────────────────────────────────────────────────────────┤
              │ backend/hub.py      运行时中枢（线程安全）                 │
              │  · 双轨仿真（AI / 静态基线）同种子同任务流并行推进          │
              │  · 每步之后应用 HIL 实测覆盖（仅 hil 模式、仅新鲜数据）     │
              │  · 真实任务注入同一调度管线 → 决策回传设备                 │
              ├──────────────────────────────────────────────────────────┤
              │ backend/hardware.py 硬件在环网关                          │
              │  · 设备注册/心跳/看门狗/事件日志                           │
              │  · HTTP + UDP + TCP 三通道遥测摄入（同一套校验）            │
              │  · 指令队列：长轮询 + 至少一次投递 + 确认                  │
              ├──────────────────────────────────────────────────────────┤
              │ backend/engine.py   仿真引擎（前端 JS 内核 1:1 移植）      │
              │ backend/models.py   模型注册表（v1 标定版 / v2 优化版）     │
              │ backend/model_lab.py 模型实验台（对照 / 消融 / 搜索 / 门禁）│
              ├──────────────────────────────────────────────────────────┤
              │ backend/experiment.py 批量实验与报告（与 .test 同格式）     │
              └──────────────────────────────────────────────────────────┘
                        │                              │
        backend/data/   └─ report.txt / runs/*.json    └─ UDP:8790 · TCP:8791
                          model-ablation.txt              （硬件遥测入口）
```

### 为什么前端不用重写

接入层只做三件事，**原页面的渲染与交互代码一行未改**：

1. 覆盖两个全局入口：`step()`（在线时短路，后端才是权威时钟）、`p95()`（直接用后端算好的分位值）；
2. 把 SSE 快照**原地水合**进 `simAI` / `simBL`（属性级写入，保持引用语义），
   原有的 `drawTopo()` / `drawChart()` / `updateUI()` 零改动地渲染后端数据；
3. 把面板数据交给页面里已实现的 `UI.renderHil / renderModels / renderExperiment`（纯 DOM，不发请求）。

因此「后端权威仿真」与「本地离线引擎」共存：后端在则用它，后端不在则自动回退，页面不报错。

---

## 3. 硬件在环（HIL）接入指南

### 3.1 三种模式（建议按顺序推进）

| 模式 | 行为 | 用途 |
| --- | --- | --- |
| `off` | 纯仿真，硬件只登记 | 默认；不接硬件时 |
| `shadow` | 硬件照常上报，但**不改仿真**，只做「实测 vs 仿真」对照 | **接真实设备时先跑这个**：验证链路、量纲、字段映射 |
| `hil` | 实测的 GPU 利用率 / 链路速率 / 时延**覆盖**仿真内部状态，调度器基于真实世界决策 | 半实物联调、硬件在环测试 |

切换：页面「硬件在环」卡片的下拉框，或 `POST /api/hw/mode {"mode":"hil"}`，或启动参数 `--hw-mode hil`。

### 3.2 遥测上行（设备 → 后端）

三种通道等价，按设备能力选：

| 通道 | 地址 | 特点 |
| --- | --- | --- |
| HTTP | `POST /api/hw/telemetry` | 单条或数组批量；有应答，便于调试 |
| UDP | `8790` | 每个数据报一个 JSON（或 `\n` 分隔多个）；无应答，适合高频上报 |
| TCP | `8791` | 按行 JSON，服务端逐条回一行 JSON；适合长连接设备 |

```bash
# 注册（可选；直接上报也会自动登记）
curl -X POST http://127.0.0.1:8787/api/hw/register -H 'Content-Type: application/json' \
     -d '{"deviceId":"edge-a-01","kind":"edge","nodeId":"edgeA","name":"边缘智算节点 A（实物）"}'

# 遥测：节点 + 链路（利用率支持 0-1 与 0-100 两种口径）
curl -X POST http://127.0.0.1:8787/api/hw/telemetry -H 'Content-Type: application/json' \
     -d '{"deviceId":"edge-a-01","node":{"id":"edgeA","gpuUtil":0.62,"tflops":5.1,"queueDepth":3,"latMs":7.4},
          "links":[{"id":"access","txGbps":2.1,"rxGbps":1.4,"rttMs":7.3}]}'

# 真实任务上报（进入与仿真任务完全相同的调度管线）
curl -X POST http://127.0.0.1:8787/api/hw/telemetry -H 'Content-Type: application/json' \
     -d '{"deviceId":"cam-01","tasks":[{"id":"cam-9001","type":"vision","bytes":12.2,"work":0.25,
          "deadlineMs":200,"prio":3}]}'

# UDP 单发
echo '{"deviceId":"edge-b-01","node":{"id":"edgeB","gpuUtil":33}}' | nc -u -w1 127.0.0.1 8790
```

字段与单位（完整契约见 `GET /api/hw/schema`）：

| 字段 | 单位 | 说明 |
| --- | --- | --- |
| `node.gpuUtil` / `node.util` | 0-1 或 0-100 | 自动归一化 |
| `node.tflops` / `queueDepth` / `memPct` / `powerW` / `tempC` | TFLOPS / 个 / 0-1 / W / ℃ | 状态展示与对照 |
| `node.latMs` | ms | 实测时延；**HIL 下会覆盖节点时延，直接影响择点** |
| `links[].txGbps/rxGbps/rttMs/lossPct/jitterMs` | Gbps / ms / % | 缺 `util` 时按 (tx+rx)/链路容量 估算 |
| `tasks[].type/bytes/work/deadlineMs/prio` | — / MB / TFLOP / ms | 缺省时用该任务类型的标准值 |
| `deviceId` | — | 必填；`seq`、`ts`、`heartbeat` 可选 |

### 3.3 决策下行（后端 → 设备）

```bash
# 长轮询领取指令（最多等 wait 秒；未确认会在重投窗口后重发，至少一次语义）
curl 'http://127.0.0.1:8787/api/hw/commands?deviceId=cam-01&wait=25'

# 确认执行结果（真实设备应回填实测时延）
curl -X POST http://127.0.0.1:8787/api/hw/ack -H 'Content-Type: application/json' \
     -d '{"deviceId":"cam-01","commandId":"c12","ok":true,"result":{"latencyMs":118}}'
```

指令类型：`dispatch`（任务 → 算力节点，含预估时延与是否降采样）、`throttle`（带宽策略建议）、
`config`（调整上报周期）、`ping`。设备侧只需实现「轮询 → 执行 → 确认」三步，
参考 `backend/tools/hw_device_sim.py` 的 `command_loop()`。

### 3.4 安全设计（关键，已测）

* **遥测保鲜**：超过 `fresh_ms`（默认 2s）的数据不再参与覆盖，避免用陈旧值决策。
* **设备离线判定**：超过 `offline_ms`（默认 3s）无心跳即判定离线，其覆盖**自动失效**，
  仿真回落到内部模型 —— 硬件掉线不会让仿真停摆，也不会出现「僵尸数据决策」。
  离线/上线都会写入事件日志并在页面 HIL 面板显示。
* **覆盖范围受控**：只覆盖 `util/utilS/lat/load` 等**可测量**，
  节点算力 `cap`、链路容量 `cap` 等物理参数仍由场景定义，误配置改不坏模型。
* **增量合并**：设备分频上报（例如 100ms 报利用率、1s 报时延）时缺失字段沿用上次值，不会互相清空。

### 3.5 硬件接口一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/hw/schema` | 接口契约 + 可复制示例 |
| POST | `/api/hw/register` | 注册/更新设备，返回 token、推荐上报周期、合法 nodeId/linkId |
| POST | `/api/hw/telemetry` | 遥测（单条或数组） |
| GET | `/api/hw/devices` | 设备清单 + 实测/仿真对照 + 事件日志 + 健康统计 |
| GET | `/api/hw/health` | 在环模式、在线设备数、遥测/指令计数、覆盖数 |
| GET | `/api/hw/commands` | 长轮询领取指令 |
| POST | `/api/hw/ack` | 确认指令（可回填执行结果） |
| POST | `/api/hw/mode` | 切换 `off`/`shadow`/`hil` |
| POST | `/api/hw/command` | 手动向某设备下发指令（页面上也可用） |
| POST | `/api/hw/broadcast` | 按角色广播指令（如统一调上报周期） |
| POST | `/api/hw/reset` | 清空设备注册表（联调重置用） |
| UDP / TCP | `8790` / `8791` | 遥测上行（`--udp-port 0` / `--tcp-port 0` 可关闭） |

---

## 4. 调度模型优化（v2）

### 4.1 优化了什么

| 项 | v1（前端同源·标定版） | v2（服务端优化版） |
| --- | --- | --- |
| 排队时延 | `0.20 × 积压 / 整机算力` 粗估 | `积压 / 本任务可得分片` —— 处理器共享的流体排空估计 |
| 算力占用项 | `work/cap × 10`（量纲任意） | `work / (cap × 截止时间)` —— 「相当于占用几个截止窗口」 |
| 违约惩罚 | `3.0 × over` 线性 | `3.0 × over/(1+over)` 饱和 —— 避免过载时巨大且近乎相等的惩罚淹没负载信号 |
| **通算权衡** | 无（只做择点） | **必要时降采样上传**（数据量 ×0.35、计算量 ×1.45），用算力换现场总线带宽 |
| 实时任务外部性 | 无 | 实时任务挤占无预留节点普通任务时计入代价 |

**通算权衡的判定规则**（`engine.choose_node_ai`）：候选按「可用性等级」排序，
等级 0（预估不违约）永远优于等级 1（注定违约）；只有当**原样上传无论选哪个节点都注定违约**、
而降采样能把它拉回等级 0 时才降采样 —— 因此不会无谓地用算力换带宽。

### 4.2 效果（6 场景 × 120s 仿真，AI 协同调度 vs 静态规则基线）

| 场景 | v1 Δ均时延 | v1 Δ超时率 | **v2 Δ均时延** | **v2 Δ超时率** |
| --- | --- | --- | --- | --- |
| 常规混线 normal | +0.5% | ±0.0pt | **−10.1%** | ±0.0pt |
| 视觉质检高峰 vision | −1.7% | −1.8pt | **−60.5%** | **−51.5pt** |
| AGV 潮汐调度 agv | −20.7% | −30.0pt | **−32.4%** | **−42.5pt** |
| 大模型突发推理 llm | −0.5% | −1.1pt | **−48.8%** | **−15.2pt** |
| 回传网络降级 degrade | −0.4% | +0.1pt | **−17.0%** | **−12.2pt** |
| 极限压力测试 stress | −0.0% | −3.1pt | **−76.9%** | **−58.2pt** |

综合得分（越小越好，负值=优于基线）：**v1 −17.0 → v2 −119.4**。
关键类别红线（机器人控制 / AGV / 机器视觉）全部通过，**6 场景无一劣于基线**；
换用另一个随机种子（77777）复测结论一致。

> 视觉场景超时率 50.8% → 0.0%：现场总线是全系统共享瓶颈，降采样把 12.2MB 的图像压到 4.3MB，
> 代价是整体算力 +1.2%。这是真正的「通信 + 计算联合优化」，而不是只换执行位置。

### 4.3 节点权重：由算法动态分配（本轮新增）

节点权重**不是人工设定的常数**，而是算法每个控制周期根据系统实时状态重算：
不需要任何输入，接入即生效。

**权重公式**（`engine._update_dynamic_weights`）

```
压力     p_n = utilS_n + qw · clamp(backlog_n / cap_n, 0, 2)      # 无量纲，节点间可比
参考     p̄   = mean(p_n)   ← 节点间**简单平均**（不用容量加权）
压力比   ratio_n = p̄ / p_n
权重     log w_n = γ · log(ratio_n) + I_n − km·(miss_n − miss̄) + log(q_n)
         w_n   = exp(log w_n − mean(log w))        # 均值归一：只改份额不改尺度
生效     wEff_n = prior_n × w_n      （prior 默认 1.0 = 完全交给算法）
代价     cost = 1.00·(L_est/D) + [0.70E + 0.30U + 0.35O] / wEff + 罚项
```

| 信号 | 作用 | 时间尺度 | 实测贡献 |
| --- | --- | --- | --- |
| **压力比 P**（主体） | 压力高于全网的节点降权、低于的提权 | 每步（τ=800ms 平滑） | **+0.78** |
| **质量学习 Q** | Hedge 型乘性更新，按实测服务质量（相对违约率 + 按截止归一化时延）给长期偏好 | 4s | **+0.32** |
| 积分 I（默认关） | 消除长期偏载（带泄漏，防饱和钉边界） | 每步 | +0.05 |
| 相对违约 M（默认关） | 避开近期频繁违约的节点 | 500ms 窗口 | +0.05 |

> 后两项在本场景矩阵上是噪声级（±0.05），故默认关闭但保留开关，便于复现与再验证。

**效果（6 场景 × 120s，AI 协同调度 vs 静态规则基线）**

| 种子 | 有动态权重 | 无动态权重 | v1 |
| --- | --- | --- | --- |
| 20261006 | **−120.56** | −119.44 | −17.02 |
| 77777 | **−97.37** | −96.72 | −16.38 |

两个种子上都优于"不加权重"，且**六个场景无一劣于基线**（normal 超时率持平，其余全部显著更优）。
控制权限实测：把某个节点持续压热（利用率 97%、积压 3TFLOP），它的权重会降到下限 0.60，
同时空闲节点升到 1.53（热/冷比值 1.01 → 0.39）——**权重真的在跟着负载走**。

**三条关键设计（都是踩坑换来的）**

1. **必须用简单平均而不是容量加权平均**：云节点 120 TFLOPS、现场只有 2.5，
   容量加权后基准等于被云节点主导，小节点会被永久判成"欠载"而顶到上限，
   权重钉死在边界、完全丧失调节能力（第一版就是这样，轨迹里 local 恒为 1.80）。
2. **必须用乘性/对数形式 + 均值归一**：线性 PI 的 `1 − kp·dev − I` 会让积分饱和，
   退化成 bang-bang（同样钉边界）；而且绝对价格在"全网都低于目标"时会被归一成常数、
   相互抵消。改成 `w ∝ (p̄/p_n)^γ` 后：**压力相等时权重精确为 1**，
   只在失衡时发力，全网均衡即退化为不加权重的标定版（测试已断言这一点）。
3. **违约率要用相对值**：绝对违约率会让"只跑大模型推理"的节点被永久歧视
   （那类任务本来就难），质量评分也必须做**按任务截止归一化**，否则只跑简单任务的节点永远得分最高。

**接口**（`prior` 与 `locks` 都是可选的；不调用任何接口时算法照样在分配权重）

```bash
# 查看算法当前分配的权重、压力、质量乘子、有效权重
curl http://127.0.0.1:8787/api/nodes/weights

# 可选：给某节点一个先验偏置（1.0 = 不干预算法），并锁定另一节点（权重固定 1.0）
curl -X POST http://127.0.0.1:8787/api/nodes/weights -H 'Content-Type: application/json' \
     -d '{"weights":{"edgeA":1.5},"locks":["edgeB"]}'

# 清除偏置与锁定（回到完全由算法分配）
curl -X POST http://127.0.0.1:8787/api/nodes/weights -H 'Content-Type: application/json' \
     -d '{"reset":true}'
```

接口返回 `owner: "algorithm"`，并同时给出 `dynamic`（算法权重）、`quality`（质量乘子）、
`effective`（= 先验 × 算法权重）、`pressure`/`loadDev`/`missEMA`（控制器内部量），便于观测与调参。
权重只作用于 **AI 轨**；基线是静态规则映射，保持权重无关（测试已断言其分布不随权重变化）。

界面上：`算力节点负载` 卡片每个节点显示`算法权重`与`压力`（绿=被提权、红=被降权）；
`模型与实验` 卡片底部有四个先验偏置输入框 + 应用/清除（默认全 1.0，可留空不用）。

### 4.4 选型方法（可复现，避免过拟合）

```bat
python -m backend.model_lab matrix  --models v1,v2 --duration 120000   :: 对照
python -m backend.model_lab ablate  --base v2 --duration 120000        :: 单开关敏感性
python -m backend.model_lab search  --base v2                          :: 64 组开关穷举 + 留出集验证
python -m backend.model_lab params  --base v2 --sweep compressRatio=0.2,0.35,0.5
python -m backend.model_lab matrix  --weights edgeA=1.6,edgeB=0.6       :: 带权重意图跑矩阵
python -m backend.model_lab params  --weights edgeA=1.6,edgeB=0.6 --sweep learnRate=0.05,0.1,0.3
python -m backend.model_lab serve-ready                               :: 模型门禁（改动后必跑）
```

节点权重控制器的参数（`uStar` / `priceKp` / `priceKi` / `priceMax` / `learnRate` /
`learnWMin` / `learnWMax` …）同样可以用 `params` 子命令扫描。本轮已扫过
`uStar ∈ {0.55,0.7,0.85}`、`priceKp ∈ {0.6,1.2,2.2}`、`missGain ∈ {0,1.5,3}`、
`learnRate ∈ {0.05,0.1,0.3}`、`learnWMin ∈ {0.6,0.8}`，**当前默认值在留出场景上最优**。

`search` 先在 60s 矩阵上穷举 2⁶ 组开关，再把前几名放到**不同时长 + 不同随机种子**的留出集上复测，
只有同时战胜 v1 才采用 —— 避免在单一矩阵上过拟合。结果落盘
`backend/data/model-ablation.txt` 与 `model-matrix.json`；v2 中被淘汰的开关在 `models.py`
中保留并标注「消融未通过」，以便复现实验。

---

## 5. API 一览（完整）

所有接口返回 JSON，统一带 CORS 头，因此从 `file://` 直接打开页面也能连上后端。

### 查询

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 服务与仿真状态（场景、强度、速度、暂停、时钟、在线客户端、在环模式、实验进度） |
| GET | `/api/catalog` | 场景 / 任务类型 / 节点 / 链路 / 设备 / 模型定义 |
| GET | `/api/models` · `/api/model` | 模型清单 / 当前模型（含公式与说明卡片） |
| GET | `/api/state` | 完整双轨快照（前端渲染所需一切；`?lite=1` 只返回运行状态） |
| GET | `/api/metrics` | KPI 聚合 + AI 相对基线的差值（含「变好/变差」判定） |
| GET | `/api/tasks` · `/api/logs` | 最近任务流水 / AI 决策日志（`?mode=ai|bl`） |
| GET | `/api/stream` | **SSE 实时推流**：`hello` + 10Hz `state`；`?once=1` 退化为单次快照 |

### 控制

| 方法 | 路径 | 载荷 |
| --- | --- | --- |
| POST | `/api/control` | `{"action":"scene","value":"stress"}` — action ∈ `scene` `rate` `speed` `pause` `reset` `view` `seed` `model` `hwMode` |
| POST | `/api/config` | `{"scene":"agv","rate":18,"speed":2.0,"paused":false}` |
| POST | `/api/model` | `{"id":"v2"}` 切换模型（会重置双轨仿真） |
| POST | `/api/nodes/weights` | `{"weights":{"edgeA":1.8},"locks":["edgeB"]}` 或 `{"reset":true}`（立即生效，不重置仿真） |
| GET | `/api/nodes/weights` | 权重 / 锁定 / 目标与实际利用率 / 动态价格 / 学习乘子 / 控制器参数 |
| POST | `/api/reset` | 重置双轨仿真 |

### 批量实验与报告

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/experiment` | `{"scenes":["normal","agv"],"durationMs":120000,"dtMs":2,"seed":20261006,"async":true}` |
| GET | `/api/experiment` | 当前/最近一次实验状态（前端据此显示进度） |
| GET | `/api/report?format=txt\|json` | 最近报告（`txt` 与 `.test/report.txt` 同格式，`&download=1` 触发下载） |
| GET | `/api/runs` · `/api/runs/{id}` | 历史实验列表 / 单次完整结果 |

---

## 6. 目录结构

```
backend/
├─ engine.py            仿真引擎（前端 JS 内核 1:1 移植；v1 路径与前端逐位一致）
├─ models.py            模型注册表：v1 标定版 / v2 优化版（开关 + 参数 + 说明卡片）
├─ hub.py               运行时中枢：双轨推进、控制、订阅、快照、硬件任务注入
├─ hardware.py          硬件在环网关：注册/遥测/覆盖/指令/UDP+TCP 传输
├─ model_lab.py         模型实验台：matrix / ablate / params / search / serve-ready
├─ experiment.py        批量实验与报告生成、落盘、历史检索
├─ server.py            HTTP 服务（静态 + REST + SSE + 硬件接口）
├─ run.py               启动入口（python -m backend.run）
├─ build_frontend.py    生成「后端驱动版」页面（注入接入层，可重复执行）
├─ web/adapter.js       前端接入层源码（被注入到生成页面）
├─ tools/hw_device_sim.py  硬件设备模拟器（同时也是接入模板）
├─ tools/sync_desktop.py   同步到桌面交付包（本项目约定：改完就同步，--check 只校验）
├─ data/                report.txt · runs/*.json · model-ablation.txt · model-matrix.json
└─ tests/
   ├─ run_all.py            一键回归（9 组，约 1 分钟）
   ├─ test_parity.py        JS 内核 ↔ Python 引擎 逐项比对
   ├─ test_api.py           REST / SSE / 实验接口（40 项）
   ├─ test_hil.py           硬件在环（46 项：三通道、三模式、指令闭环、看门狗）
   ├─ test_weights.py       节点权重动态分配（27 项：自主性/响应/有界/均衡/先验/有效性）
   ├─ test_frontend.py      页面集成（28 项，无浏览器真跑整页 + DOM 垫片 + 真 SSE）
   ├─ test_ui_static.py     前端静态校验（标签配平 / id 绑定 / 离线可用性）
   ├─ parity_harness.js     JS 内核抽取执行器
   └─ page_harness.js       无浏览器页面驱动器
```

---

## 7. 自测

```bat
python backend\tests\run_all.py            :: 全量回归（推荐）
python backend\tests\run_all.py --fast     :: 跳过耗时的模型矩阵
```

改完之后（本项目约定：**每次改完都要同步桌面交付包**）：

```bat
python backend\tools\sync_desktop.py           :: 同步到桌面
python backend\tools\sync_desktop.py --check   :: 只校验是否已同步（不改文件）
```
（也可双击根目录 `同步到桌面.cmd`。）

测试不会污染交付物：`test_api / test_hil / test_frontend` 会把 `DSH_DATA_DIR` 指向系统临时目录，
因此 `backend/data/report.txt` 与 `data/runs` 不会被测试产生的实验覆盖。

| 组 | 内容 | 当前 |
| --- | --- | --- |
| 1 | 引擎一致性（JS ↔ Python，v1 逐位） | PASS |
| 2 | 前端引擎回归（`.test/smoke.js` 与历史基线**逐字节一致**） | PASS |
| 3 | 前端渲染冒烟（`.test/uicheck.js` 14 项） | PASS |
| 4 | 后端接口（40 项） | PASS |
| 5 | 硬件在环（46 项） | PASS |
| 6 | 节点权重与智能分配（30 项） | PASS |
| 7 | 页面集成（28 项，含新面板） | PASS |
| 8 | 模型门禁（默认模型不劣于 v1） | PASS |
| 9 | 前端静态校验 | PASS |

> 前端引擎回归之所以要求「逐字节一致」：本次只优化了界面与后端，页面前端引擎必须零改动的
> 证据就是 `.test/smoke.js` 的输出与历史基线 SHA256 完全相同。

---

## 8. 参数与模型速查

| 项 | 值 |
| --- | --- |
| 时标 | `TSCALE = 0.10`（1ms 真实 = 0.10ms 仿真），子步长 2ms |
| 随机种子 | `20261006`（双轨共用，保证「同一任务流」对照） |
| 任务类型 | 机器视觉 12.2MB/0.25TFLOP/200ms · 机器人 0.1MB/0.015/20ms · AGV 1.2MB/0.5/120ms · 大模型 2.4MB/24/1100ms · 维护 1.6MB/0.6/900ms |
| 算力节点 | 现场 2.5 TFLOPS（预留 60% 给实时控制）· 边缘 A/B 各 6 · 云端 120 |
| 通信链路 | 现场总线 1.2 Gbps · 边缘接入 4.0 · 5G/专线回传 2.5 |
| 择点代价 | `1.00·(L_est/D) + [0.70·E_node + 0.30·U_node + 0.35·O + price] / wEff + 罚项`；`node* = argmin[ cost + 0.20 × 在途连接数 ]` |
| 节点权重 | 由算法动态分配：压力比为主 + 质量学习；wDyn ∈ [0.6,1.8]，均值归一；prior 为可选先验（默认 1.0） |
| 权重公式 | `log w = γ·log(p̄/p_n) + log(q)`，γ=0.5，p_n = utilS + 0.15·backlog/cap，τ=800ms |
| 带宽策略 | ≤95% 等权公平共享；越过后线性渐入 `prio^1.30` 轻度倾斜（历史标定结论，v1/v2 共用） |
| 降采样参数 | `compressRatio 0.35` · `compressWorkGain 1.45` · 触发门限 `bytes ≥ 4MB`（可扫参标定） |

参数改动请编辑 `engine.py` / `models.py` 顶部常量；`/api/catalog` 与 `/api/models` 会同步反映，
改完务必跑 `python -m backend.model_lab serve-ready` 与 `python backend\tests\run_all.py`。


