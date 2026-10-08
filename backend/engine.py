# -*- coding: utf-8 -*-
"""
工业智算网 · 通算协同调度仿真引擎（服务端权威实现）

本文件是前端 `工业智算网-通算协同调度仿真平台.html` 中内嵌 JS 仿真内核的
1:1 移植版本，保持：

  * 相同的随机数发生器（mulberry32，32 位无符号整数运算，逐位对齐）
  * 相同的任务类型 / 算力节点 / 通信链路 / 场景参数
  * 相同的到达过程、调度代价函数、带宽分配、算力分配与统计口径

因此后端与前端在相同种子、相同子步长下会得到完全一致的仿真轨迹
（见 tests/test_parity.py，用 Node 跑原始 JS 内核做交叉验证）。

单位约定（与前端一致）：
    work  TFLOP      cap  TFLOPS      data  MB
    链路带宽 Gbps，1 Gbps = 0.125 MB/ms；时间统一为 ms（仿真时钟）

仅依赖 Python 标准库。
"""

from __future__ import annotations

import math

from . import models

# =========================================================================
# 基础工具
# =========================================================================

MASK32 = 0xFFFFFFFF

TSCALE = 0.10          # 1ms 真实时间 = 0.10ms 仿真时间
SAMPLE_MS = 250        # 采样间隔（仿真时间 ms）
STEP_MS = 2.0          # 规范子步长：实时循环与批量实验统一按此切片
SEED = 20261006
RT_PRIO = 5            # 实时控制任务优先级阈值（占用现场预留算力）


def clamp(v, a, b):
    return a if v < a else (b if v > b else v)


def lerp(a, b, t):
    return a + (b - a) * t


def _imul32(a, b):
    """等价 JS Math.imul：32 位整数乘法，保留低 32 位。"""
    return (a * b) & MASK32


def mulberry32(seed):
    """与前端 `mulberry32(a)` 逐位一致的 PRNG。"""
    a = seed & MASK32

    def rnd():
        nonlocal a
        a = (a + 0x6D2B79F5) & MASK32
        t = a
        t = _imul32(t ^ (t >> 15), 1 | t)
        # JS: t = t + Math.imul(t ^ t>>>7, 61|t) ^ t    （+ 优先于 ^）
        t = (((t + _imul32(t ^ (t >> 7), 61 | t)) & MASK32) ^ t) & MASK32
        t = (t ^ (t >> 14)) & MASK32
        return t / 4294967296.0

    return rnd


# =========================================================================
# 任务类型
# =========================================================================

TYPES = {
    "vision": {"name": "机器视觉检测", "short": "视觉", "icon": "📷", "dataIn": 12, "dataOut": 0.2,
               "work": 0.25, "deadline": 200, "prio": 3, "color": "#0B63F6", "dev": "cam"},
    "robot": {"name": "机器人控制", "short": "机器人", "icon": "🦾", "dataIn": 0.05, "dataOut": 0.05,
              "work": 0.015, "deadline": 20, "prio": 5, "color": "#DC2626", "dev": "rb"},
    "agv": {"name": "AGV 调度", "short": "AGV", "icon": "🚚", "dataIn": 1.0, "dataOut": 0.2,
            "work": 0.5, "deadline": 120, "prio": 4, "color": "#E08A00", "dev": "agv"},
    "llm": {"name": "大模型推理", "short": "大模型", "icon": "🧠", "dataIn": 0.8, "dataOut": 1.6,
            "work": 24, "deadline": 1100, "prio": 2, "color": "#7C3AED", "dev": "llm"},
    "maint": {"name": "预测性维护", "short": "维护", "icon": "📡", "dataIn": 1.5, "dataOut": 0.1,
              "work": 0.6, "deadline": 900, "prio": 2, "color": "#0891B2", "dev": "sen"},
}
TYPE_ORDER = ["vision", "robot", "agv", "llm", "maint"]

# =========================================================================
# 现场设备
# =========================================================================

DEVICES = [
    {"id": "cam", "name": "视觉相机阵列", "icon": "📷", "type": "vision", "x": 62, "y": 62},
    {"id": "rb", "name": "机器人控制柜", "icon": "🦾", "type": "robot", "x": 62, "y": 150},
    {"id": "agv", "name": "AGV 车队", "icon": "🚚", "type": "agv", "x": 62, "y": 238},
    {"id": "llm", "name": "工控大模型终端", "icon": "🧠", "type": "llm", "x": 62, "y": 326},
    {"id": "sen", "name": "设备传感网络", "icon": "📡", "type": "maint", "x": 62, "y": 414},
]
DEV_BY_ID = {d["id"]: d for d in DEVICES}

# =========================================================================
# 算力节点 / 通信链路
# =========================================================================

NODE_DEFS = [
    {"id": "local", "name": "现场算力一体机", "tag": "LOCAL", "x": 258, "y": 96, "w": 106, "h": 58,
     "cap": 2.5, "lat": 0.8, "ept": 0.90, "reserve": 0.60, "color": "#0B63F6"},
    {"id": "edgeA", "name": "边缘智算节点 A", "tag": "EDGE-A", "x": 432, "y": 170, "w": 114, "h": 60,
     "cap": 6, "lat": 6, "ept": 0.55, "reserve": 0, "color": "#7C3AED"},
    {"id": "edgeB", "name": "边缘智算节点 B", "tag": "EDGE-B", "x": 432, "y": 332, "w": 114, "h": 60,
     "cap": 6, "lat": 6, "ept": 0.55, "reserve": 0, "color": "#7C3AED"},
    {"id": "cloud", "name": "云端算力中心", "tag": "CLOUD", "x": 790, "y": 250, "w": 140, "h": 74,
     "cap": 120, "lat": 65, "ept": 9.00, "reserve": 0, "color": "#0891B2"},
]

LINK_DEFS = [
    {"id": "field", "name": "现场总线 / TSN", "cap": 1.2, "color": "#0B63F6"},
    {"id": "access", "name": "边缘接入网", "cap": 4.0, "color": "#7C3AED"},
    {"id": "backhaul", "name": "5G / 专线回传", "cap": 2.5, "color": "#0891B2"},
]

NODE_LINKS = {
    "local": ["field"],
    "edgeA": ["field", "access"],
    "edgeB": ["field", "access"],
    "cloud": ["field", "access", "backhaul"],
}

# =========================================================================
# 场景
# =========================================================================

SCENES = {
    "normal": {"rate": 11, "w": {"vision": .30, "robot": .20, "agv": .20, "llm": .15, "maint": .15},
               "bw": {"field": 1, "access": 1, "backhaul": 1}},
    "vision": {"rate": 18, "w": {"vision": .55, "robot": .13, "agv": .10, "llm": .09, "maint": .13},
               "bw": {"field": 1, "access": 1, "backhaul": 1}},
    "agv": {"rate": 26, "w": {"vision": .16, "robot": .10, "agv": .52, "llm": .10, "maint": .12},
            "bw": {"field": 1, "access": 1, "backhaul": 1}},
    "llm": {"rate": 10, "w": {"vision": .16, "robot": .14, "agv": .12, "llm": .46, "maint": .12},
            "bw": {"field": 1, "access": 1, "backhaul": 1}},
    "degrade": {"rate": 18, "w": {"vision": .30, "robot": .18, "agv": .20, "llm": .17, "maint": .15},
                "bw": {"field": 1, "access": 1, "backhaul": 0.45}},
    "stress": {"rate": 24, "w": {"vision": .34, "robot": .16, "agv": .22, "llm": .14, "maint": .14},
               "bw": {"field": 0.85, "access": 0.9, "backhaul": 0.8}},
}
SCENE_LABELS = {
    "normal": "常规混线生产",
    "vision": "视觉质检高峰",
    "agv": "AGV 潮汐调度",
    "llm": "大模型突发推理",
    "degrade": "回传网络降级",
    "stress": "极限压力测试",
}


def clone_scene(name):
    sc = SCENES[name]
    return {"rate": sc["rate"], "weights": dict(sc["w"]), "bwScale": dict(sc["bw"])}


# =========================================================================
# 仿真对象
# =========================================================================

class Task(object):
    __slots__ = ("id", "type", "T", "devId", "devY", "bytes", "bytesLeft", "work", "workLeft",
                 "deadline", "prio", "spawnT", "nodeId", "state", "bwEff", "_w", "_share",
                 "est", "path", "visT", "visDur", "want", "arrived", "compressed",
                 "source", "extId")


class Node(object):
    __slots__ = ("id", "name", "tag", "x", "y", "w", "h", "cap", "lat", "ept", "reserve", "color",
                 "running", "backlog", "used", "util", "utilS", "done", "latSum", "workSum",
                 "pending", "rtLoad", "loLoad", "hwOverride", "hwSrc",
                 # 节点权重与动态分配（AI 轨专用）
                 "prior", "wDyn", "wQuality", "wEff", "wTgt", "wI", "devS", "missEMA", "locked",
                 "_accDone", "_accMiss", "_accT", "_lDone", "_lMiss", "_lLat", "_lDl", "press")

    def __init__(self, d):
        for k in ("id", "name", "tag", "x", "y", "w", "h", "cap", "lat", "ept", "reserve", "color"):
            setattr(self, k, d[k])
        self.running = []
        self.backlog = 0.0
        self.used = 0.0
        self.util = 0.0
        self.utilS = 0.0
        self.done = 0
        self.latSum = 0.0
        self.workSum = 0.0
        self.pending = 0
        self.rtLoad = 0
        self.loLoad = 0
        self.hwOverride = False
        self.hwSrc = ""
        self.prior = 1.0           # 运维先验偏置（可选，默认 1.0 = 完全交给算法）
        self.wDyn = 1.0            # 算法动态权重（主体，每周期重算）
        self.wQuality = 1.0        # 质量学习乘子（慢回路）
        self.wEff = 1.0            # 生效权重 = 先验 × 动态
        self.wTgt = 0.0            # 展示用：全网容量加权平均利用率
        self.wI = 0.0              # 相对负载偏差的积分项
        self.devS = 0.0            # 平滑后的相对负载偏差
        self.press = 0.0            # 瞬时压力（利用率 + 归一化积压）
        self.missEMA = 0.0         # 违约率滑动估计
        self.locked = False        # 锁定：该节点的权重不接受动态分配
        self._accDone = 0
        self._accMiss = 0
        self._accT = 0.0
        self._lDone = 0            # 慢回路（权重学习）统计窗口
        self._lMiss = 0
        self._lLat = 0.0
        self._lDl = 0.0


class Link(object):
    __slots__ = ("id", "name", "cap", "color", "capS", "demand", "act", "load", "util", "utilS",
                 "hwOverride", "hwSrc")

    def __init__(self, d):
        for k in ("id", "name", "cap", "color"):
            setattr(self, k, d[k])
        self.capS = d["cap"]
        self.demand = 0.0
        self.act = 0.0
        self.load = 0.0
        self.util = 0.0
        self.utilS = 0.0
        self.hwOverride = False
        self.hwSrc = ""


class Sim(object):
    """单个仿真体（AI 协同调度 或 静态规则基线）。"""

    def __init__(self, mode, seed=SEED, model=None):
        self.mode = mode
        self.model = model if model is not None else models.MODELS[models.DEFAULT]
        self.model_id = self.model["id"]
        self.flags = self.model["flags"]
        self.mparams = self.model.get("params") or {}
        self.rng = mulberry32(seed)
        self.t = 0.0
        self.acc = 0.0
        self.nextId = 1
        self.tasks = []
        self.limitHit = 0
        self.nodes = [Node(d) for d in NODE_DEFS]
        self.links = [Link(d) for d in LINK_DEFS]
        self.table = []
        self.log = []
        self.typeStat = {}
        self.stat = {"n": 0, "latSum": 0.0, "lats": [], "miss": 0, "energy": 0.0, "work": 0.0,
                     "bytes": 0.0, "cloud": 0, "edge": 0, "local": 0, "active": 0,
                     "compressed": 0, "compressSavedMB": 0.0, "compressExtraTFLOP": 0.0}
        self.hist = {"lat": [], "p95": [], "bw": [], "gpu": []}
        self.lastSample = -1.0
        self.lastBacklog = -1.0
        self.backlogTrace = []
        self.throughput = 0.0
        self.tpWindow = []
        self.lastUid = 0
        self.rr = 0
        self._learnT = 0.0
        self.nodeById = {n.id: n for n in self.nodes}
        self.linkById = {l.id: l for l in self.links}

    # ---- 便捷查询 ----
    def avg_lat(self):
        return self.stat["latSum"] / self.stat["n"] if self.stat["n"] else 0.0

    def p95(self):
        lats = self.stat["lats"]
        if not lats:
            return 0.0
        a = sorted(lats)
        return a[min(len(a) - 1, int(math.floor(len(a) * 0.95)))]

    def energy_per_task(self):
        return self.stat["energy"] / self.stat["n"] if self.stat["n"] else 0.0

    def miss_rate(self):
        return self.stat["miss"] / self.stat["n"] * 100 if self.stat["n"] else 0.0

    def gpu_util(self):
        return sum(clamp(n.util, 0, 1) for n in self.nodes) / len(self.nodes) * 100

    def peak_bw(self):
        return max(clamp(l.util, 0, 1) for l in self.links) * 100


# =========================================================================
# 路径几何（与前端一致，供粒子动画使用）
# =========================================================================

def build_path(node_id, dev_y):
    d = dev_y
    if node_id == "local":
        return [[86, d], [150, d], [150, 96], [205, 96]]
    if node_id == "edgeA":
        return [[86, d], [150, d], [150, 250], [340, 250], [375, 170]]
    if node_id == "edgeB":
        return [[86, d], [150, d], [150, 250], [340, 250], [375, 332]]
    return [[86, d], [150, d], [150, 250], [718, 250]]


# =========================================================================
# 调度评估
# =========================================================================

def link_bw_est(s, links):
    """链接可用带宽与拥塞度估计。"""
    bw = 1e9
    u = 0.0
    for lid in links:
        l = s.linkById[lid]
        bw = min(bw, max(l.capS * (1 - 0.82 * l.utilS), l.capS * 0.06))
        u = max(u, l.utilS)
    return bw, u


def evaluate(s, task, node, bytes_=None, work_=None):
    """AI 成本评估（按模型分派）。v1 路径与前端 JS 逐位一致，勿动。"""
    if s.model_id == "v1":
        return evaluate_v1(s, task, node)
    return evaluate_v2(s, task, node, bytes_=bytes_, work_=work_)


def evaluate_v1(s, task, node):
    """AI 成本评估：时延(含抖动余量) + 能耗 + 负载 + 算力占用 + 违约放大。"""
    links = NODE_LINKS[node.id]
    bw, u = link_bw_est(s, links)
    jitter = node.lat * 0.2 if node.lat < 2 else node.lat * (0.25 + 1.2 * u)
    path_lat = node.lat + jitter
    is_rt = task.prio >= RT_PRIO
    res = node.reserve or 0
    usable_cap = node.cap * (1 if is_rt else (1 - res))
    same = 0
    for x in node.running:
        if (x.prio >= RT_PRIO) == is_rt:
            same += 1
    conc = same + 1
    share_cap = max(usable_cap / conc, usable_cap * 0.04)
    est_transfer = task.bytesLeft / max(bw * 0.125, 0.02)
    est_compute = task.workLeft / share_cap * 1000
    est_queue = node.backlog * 0.20 / max(node.cap, 0.1) * 1000
    est_lat = path_lat + est_transfer + est_queue + est_compute
    ratio = est_lat / task.deadline
    feasible = ratio <= 1.02
    e_norm = node.ept / 9.0
    occupancy = task.workLeft / node.cap * 10
    over = max(0.0, ratio - 1)
    return {
        "estLat": est_lat, "estTransfer": est_transfer, "estQueue": est_queue,
        "estCompute": est_compute, "pathLat": path_lat, "jitter": jitter, "bwUtil": u,
        "feasible": feasible, "eNorm": e_norm, "occupancy": occupancy,
        "cost": 1.00 * ratio + 0.70 * e_norm + 0.30 * node.utilS + 0.35 * occupancy + 3.0 * over,
    }


def _concurrent_flows(s, links, task):
    """与本任务共用同一批链路的在途传输流数量（公平共享近似），用于修正传输时延。"""
    n = 1
    for tk in s.tasks:
        if tk is task or tk.state != "transfer":
            continue
        for lid in NODE_LINKS[tk.nodeId]:
            if lid in links:
                n += 1
                break
    return n


def _node_state(s, task, node, work_=None):
    """节点侧状态：可用算力、竞争并发度、本任务可得分片。

    v1：按「同类并发任务数」等分（conc = same + 1）。
    v2（wfsShare）：按**加权公平份额**估算，与 allocNode/shareGroup 的真实分配
    一致 —— 分片 = 容量 × w_self / (w_self + Σ 竞争权重)，其中竞争权重按
    「对方剩余工作量 / 我的工作量」折算（比我早结束的任务对我稀释更少），
    在途任务全额计入。这样既不需要拍脑袋的排队系数，也能正确反映
    「高优先级任务进入低优先级积压节点时不会被整段积压拖住」。
    """
    is_rt = task.prio >= RT_PRIO
    res = node.reserve or 0
    if s.flags.get("wfsShare"):
        usable = (node.cap * res) if (is_rt and res > 0) else \
                 (node.cap if is_rt else node.cap * (1 - res))
        w_self = math.pow(task.prio, 1.35)
        if work_ is None:
            work_ = task.workLeft
        sw = 0.0
        for x in node.running:
            if (x.prio >= RT_PRIO) != is_rt:
                continue
            frac = clamp(x.workLeft / max(work_, 1e-9), 0.0, 1.0)
            sw += math.pow(x.prio, 1.35) * frac
        for x in s.tasks:
            if x is task or x.nodeId != node.id or x.state != "transfer":
                continue
            if (x.prio >= RT_PRIO) != is_rt:
                continue
            sw += math.pow(x.prio, 1.35)
        share_cap = max(usable * w_self / (w_self + sw), usable * 0.04)
        return is_rt, usable, share_cap

    usable_cap = node.cap * (1 if is_rt else (1 - res))
    same = 0
    for x in node.running:
        if (x.prio >= RT_PRIO) == is_rt:
            same += 1
    conc = same + 1
    share_cap = max(usable_cap / conc, usable_cap * 0.04)
    return is_rt, usable_cap, share_cap


def _option_v2(s, task, node, links, bw, flow, path_lat, share_cap, bytes_, work_):
    """给定 (数据量, 计算量) 形态下的时延分解。"""
    mp = s.mparams
    if s.flags.get("allocShare"):
        # 与带宽分配器一致的传输估计：按链路当前需求做工作保持的按比例共享
        # （eff = capS × 我的权重 / 链路总需求），不再叠乘拥塞折减——那样会把
        # 拥塞惩罚算两遍，导致传输时延被系统性高估、实时任务被误判为不可行。
        eff = 1e9
        for lid in links:
            l = s.linkById[lid]
            dem = l.demand if l.demand > 1e-9 else 1.0
            eff = min(eff, l.capS * 0.125 / dem)
        est_transfer = bytes_ / max(eff, 0.02)
    else:
        est_transfer = bytes_ / max(bw * 0.125 * flow, 0.02)
    est_compute = work_ / share_cap * 1000
    if s.flags.get("wfsShare"):
        # 份额估计已反映竞争；积压只作为「活跃集会持续多久」的弱信号
        qf = mp.get("queueFactorWFS", 0.15)
    elif s.flags.get("fluidQueue"):
        # 处理器共享的流体排空估计：积压 / 本任务可得分片
        qf = mp.get("queueFactor", 0.5)
    else:
        qf = 0.20                       # v1 的粗估口径（backlog×0.2/cap）
    est_queue = node.backlog / max(share_cap, 1e-6) * 1000 * qf
    est_lat = path_lat + est_transfer + est_queue + est_compute
    ratio = est_lat / task.deadline
    over = max(0.0, ratio - 1)
    return est_transfer, est_compute, est_queue, est_lat, ratio, over


def evaluate_v2(s, task, node, bytes_=None, work_=None):
    """服务端优化版成本评估（结构性修正，见 models.py 的说明）。"""
    mp = s.mparams
    fl = s.flags
    links = NODE_LINKS[node.id]
    bw, u = link_bw_est(s, links)
    flow = _concurrent_flows(s, links, task) if fl.get("flowShare") else 1
    jitter = node.lat * 0.2 if node.lat < 2 else node.lat * (0.25 + 1.2 * u)
    path_lat = node.lat + jitter
    is_rt, usable_cap, share_cap = _node_state(s, task, node, work_)

    b = task.bytesLeft if bytes_ is None else bytes_
    w = task.workLeft if work_ is None else work_
    est_transfer, est_compute, est_queue, est_lat, ratio, over = _option_v2(
        s, task, node, links, bw, flow, path_lat, share_cap, b, w)
    feasible = ratio <= 1.02
    e_norm = node.ept / 9.0

    # 能耗项随剩余松弛度衰减：越逼近截止，越只认时延
    slack_w = clamp(1.0 - ratio, 0.0, 1.0) if fl.get("slackEnergy") else 1.0
    # 算力占用归一化：相当于占用「几个截止窗口」的整机算力
    if fl.get("normOccupancy"):
        occupancy = w / max(node.cap, 1e-6) / max(task.deadline / 1000.0, 1e-6)
    else:
        occupancy = w / node.cap * 10
    if fl.get("satViolation"):
        pen = mp.get("violationWeight", 3.0) * (over / (1.0 + over))
    else:
        pen = mp.get("violationWeight", 3.0) * over

    # 柔性偏好项（能耗 / 负载 / 占用）：受**算法动态分配**的节点权重调节，
    # 权重越高 ⇒ 这些偏好项被压得越低 ⇒ 该节点更有吸引力。
    soft = 0.70 * e_norm * slack_w + 0.30 * node.utilS + 0.35 * occupancy
    if fl.get("nodeWeight"):
        soft /= clamp(node.wEff, mp.get("wEffMin", 0.25), mp.get("wEffMax", 4.0))
    cost = 1.00 * ratio + soft + pen
    # 时延可行性（ratio）与违约惩罚**不打折**：权重再高也不能让任务变得可行。
    if is_rt and node.lat > 25:
        cost += mp.get("rtWanPenalty", 8.0)
    # RT 任务的「外部性定价」：现场节点有预留分区，实时任务不会挤占普通任务；
    # 没有预留分区的节点（边缘）上，实时任务会抢占普通任务的分片。把这份外部性
    # 计入代价，实时控制回路才会优先留在现场一体机，只在预留算力不够时才溢出。
    extern = 0.0
    if is_rt and fl.get("rtExternality") and (node.reserve or 0) <= 0:
        lo_n = 0
        for x in node.running:
            if x.prio < RT_PRIO:
                lo_n += 1
        if lo_n:
            extern = mp.get("rtSpoilWeight", 0.6) * clamp(lo_n * 0.25, 0.0, 1.0)
            cost += extern
    return {
        "estLat": est_lat, "estTransfer": est_transfer, "estQueue": est_queue,
        "estCompute": est_compute, "pathLat": path_lat, "jitter": jitter, "bwUtil": u,
        "feasible": feasible, "eNorm": e_norm, "occupancy": occupancy,
        "flow": flow, "slackW": slack_w, "extern": extern, "cost": cost,
    }


STATIC_RULE = {"robot": "local", "vision": None, "agv": None, "llm": "cloud", "maint": None}


def choose_node_ai(s, tk):
    """AI 派发决策：择点 + （v2）通算权衡。

    返回 (node, estimate, compressed)。

    v1：最小代价择点（与前端逐位一致）。
    v2：两级择点 + 数据形态择优。
      * 候选按「可用性等级」排序：等级 0 = 预估不违约，等级 1 = 注定违约。
        等级 0 永远优于等级 1 —— 只要存在可行候选就不做压缩，避免无谓地用算力换带宽。
      * 全部注定违约时，在等级 1 内最小化**绝对预估时延**（最小伤害）：
        违约惩罚在各候选间数值巨大且彼此接近，会淹没选择信号（历史已知缺陷）。
      * 在同一批候选上评估「原样上传」与「现场降采样上传」两种数据形态：
        只要能因此把任务从等级 1 拉回等级 0，这次降采样就是值得的。
    """
    if s.model_id == "v1":
        best = None
        for n in s.nodes:
            e = evaluate_v1(s, tk, n)
            c = e["cost"] + 0.20 * n.pending
            if best is None or c < best[2]:
                best = (n, e, c, False)
        return best[0], best[1], False

    mp = s.mparams
    pick = _v2_pick(s, tk, tk.bytesLeft, tk.workLeft)
    if s.flags.get("compress") and tk.bytesLeft >= mp.get("compressMinBytes", 4.0):
        max_u = 0.0
        for l in s.links:
            if l.utilS > max_u:
                max_u = l.utilS
        if max_u >= mp.get("compressMinU", 0.0):
            b2 = tk.bytesLeft * mp.get("compressRatio", 0.35)
            w2 = tk.workLeft * mp.get("compressWorkGain", 1.45)
            alt = _v2_pick(s, tk, b2, w2, extra=mp.get("compressPenalty", 0.0))
            if alt[2] < pick[2]:
                return alt[0], alt[1], True
    return pick[0], pick[1], False


def _v2_pick(s, tk, bytes_, work_, extra=0.0):
    """v2 候选评估：返回 (node, estimate, 比较键)。

    比较键 = (等级, 数值)，等级 0 = 可行、1 = 全不可行。两种数据形态（原始/压缩）
    的比较因此始终自洽：
      * 等级内可行   → 数值 = cost + 平准项（+ 压缩附加代价）；
      * 等级内不可行 → 数值 = 绝对预估时延（最小伤害），附加代价按截止时延折算为 ms。
    """
    cands = []
    for n in s.nodes:
        e = evaluate_v2(s, tk, n, bytes_=bytes_, work_=work_)
        e["_pend"] = n.pending
        e["_node"] = n
        cands.append(e)

    if s.flags.get("leastHarm"):
        feasible = [c for c in cands if c["feasible"]]
        if feasible:
            best = min(feasible, key=lambda c: c["cost"] + 0.20 * c["_pend"])
            return best["_node"], best, (0, best["cost"] + 0.20 * best["_pend"] + extra)
        best = min(cands, key=lambda c: (c["estLat"] + (extra + c.get("extern", 0.0)) * tk.deadline,
                                         c["cost"] + 0.20 * c["_pend"]))
        return best["_node"], best, (1, best["estLat"] + (extra + best.get("extern", 0.0)) * tk.deadline)

    best = min(cands, key=lambda c: c["cost"] + 0.20 * c["_pend"])
    return best["_node"], best, (0, best["cost"] + 0.20 * best["_pend"] + extra)


def _node_score(n, miss_ref=0.0):
    """节点近期服务质量得分（0~1，越大越好）——用于权重在线学习的慢回路。

    两个修正让节点之间可比，避免"只跑简单任务的节点永远得分最高"的偏差：
      · 时延按**任务自身截止时延**归一化（lat/deadline），跨任务类型可比；
      · 违约率取**相对值**（自己 − 全网同窗口均值），
        只跑本来就难的任务的节点不会因为"这类任务普遍超时"而被永久歧视。
    """
    if n._lDone <= 0:
        return None
    miss_rel = max(0.0, n._lMiss / n._lDone - miss_ref)
    lat_norm = (n._lLat / n._lDl) if n._lDl > 0 else 0.0
    return (1.0 / (1.0 + 6.0 * miss_rel)) * clamp(
        1.0 / (1.0 + max(0.0, lat_norm - 0.5) * 2.0), 0.2, 1.0)


def _learn_quality(s):
    """慢回路：按实测服务质量调整「质量乘子」（乘性权重更新 / Hedge 型）。

    这是在快回路（负载偏差 PI + 违约反馈）之上的一层长期记忆：
    它回答的是「哪个节点在**结果上**持续更好」，而快回路只管「当下谁更闲/更不稳」。
    只调整相对份额：每轮乘性更新后归一化到均值 1，并夹在 [qMin, qMax] 内。
    """
    mp = s.mparams
    rate = mp.get("learnRate", 0.15)
    qmin = mp.get("qualityMin", 0.6)
    qmax = mp.get("qualityMax", 1.6)
    scores = {}
    rates = {}
    for n in s.nodes:
        if n.locked:
            continue
        if n._lDone > 0:
            rates[n.id] = n._lMiss / n._lDone
    miss_ref = (sum(rates.values()) / len(rates)) if rates else 0.0
    for n in s.nodes:
        if n.locked:
            continue
        sc = _node_score(n, miss_ref)
        if sc is not None and n._lDone >= mp.get("learnMinSamples", 3):
            scores[n.id] = sc
    if len(scores) >= 1:
        mean = sum(scores.values()) / len(scores)
        if mean > 1e-9:
            for n in s.nodes:
                sc = scores.get(n.id)
                if sc is None:
                    continue
                n.wQuality = clamp(n.wQuality * (1.0 + rate * (sc / mean - 1.0)), qmin, qmax)
    vals = [n.wQuality for n in s.nodes if not n.locked]
    if vals:
        m = sum(vals) / len(vals)
        if m > 1e-9:
            for n in s.nodes:
                if not n.locked:
                    n.wQuality = clamp(n.wQuality / m, qmin, qmax)
    for n in s.nodes:
        n._lDone = 0
        n._lMiss = 0
        n._lLat = 0.0
        n._lDl = 0.0


def _update_dynamic_weights(s, dt):
    """**算法动态分配节点权重**（本项目权重机制的主体，无需任何人工输入）。

    权重不是常数，而是每个控制周期按四路信号重算：

      1. **压力比（P 项，主体）**：先算每个节点的瞬时压力
         `p_n = utilS_n + qw·clamp(backlog_n/cap_n, 0, 2)`（无量纲、节点间可比），
         再取**节点间简单平均** `p̄`（不用容量加权——否则大节点会长期主导基准，
         把小节点永久判成"欠载"而顶到上限），令 `w ∝ (p̄/p_n)^γ`。
         压力相等时权重**恰好为 1**，压力大者自动降权、压力小者自动提权。
         用乘性/对数形式而不是线性偏差，是为了让调整平滑、对称、天然有界。
      2. **积分（I 项，可选、带泄漏）**：消除长期偏载。泄漏项保证它不会饱和到
         把权重永久钉在上下限（线性 PI 直接用 `1−kp·dev−I` 会退化成 bang-bang）。
      3. **相对违约率（M 项，可选）**：用自己减全网均值的违约率。
         相对比较是为了公平——只跑大模型推理的节点不该因为"这类任务本来就难"
         被永久歧视。
      4. **质量学习乘子（Q 项，可选，慢回路）**：Hedge 型乘性更新，
         按实测服务质量（违约率 + 按任务截止归一化的时延）给出长期偏好。

    最后做**均值归一**：只改变"份额怎么分"，不改变整体尺度；全网均衡时权重回到 1.0。
    """
    mp = s.mparams
    fl = s.flags
    gamma = mp.get("dynGamma", 0.5)
    qw = mp.get("dynQueueW", 0.15)
    ki = mp.get("dynKi", 0.05)
    leak = mp.get("dynLeak", 0.5)
    i_max = mp.get("dynIMax", 0.35)
    km = mp.get("dynKm", 0.5)
    w_min = mp.get("dynWMin", 0.6)
    w_max = mp.get("dynWMax", 1.8)
    tau = max(mp.get("dynTauMs", 800.0), 1.0)
    win = max(mp.get("missWindowMs", 500.0), 1.0)
    miss_tau = max(mp.get("missTau", 2000.0), 1.0)
    alpha = min(1.0, dt / tau)

    # ---- 1) 压力（利用率 + 归一化积压） ----
    for n in s.nodes:
        q = clamp(n.backlog / max(n.cap, 1e-6), 0.0, 2.0)
        n.press = n.utilS + qw * q
    p_bar = sum(n.press for n in s.nodes) / len(s.nodes) if s.nodes else 0.0

    # ---- 2) 违约率窗口（相对比较） ----
    for n in s.nodes:
        n._accT += dt
        if n._accT >= win:
            rate = n._accMiss / n._accDone if n._accDone else 0.0
            n.missEMA += (rate - n.missEMA) * min(1.0, win / miss_tau)
            n._accT = 0.0
            n._accDone = 0
            n._accMiss = 0
    miss_bar = sum(n.missEMA for n in s.nodes) / len(s.nodes) if s.nodes else 0.0

    lo, hi = math.log(w_min), math.log(w_max)
    raw = []
    for n in s.nodes:
        if n.locked:
            n.devS = 0.0
            n.wI = 0.0
            raw.append(1.0)
            continue
        ratio = clamp(p_bar / max(n.press, 1e-3), 0.2, 5.0)
        dev = math.log(ratio)                     # >0 = 比全网轻松 → 应提权
        n.devS += (dev - n.devS) * alpha
        if fl.get("weightIntegral"):
            n.wI = clamp(n.wI + ki * n.devS * (dt / 1000.0) - leak * n.wI * (dt / 1000.0),
                         -i_max, i_max)
        else:
            n.wI = 0.0
        logw = gamma * n.devS + n.wI
        if fl.get("weightMiss"):
            logw -= km * (n.missEMA - miss_bar)
        if fl.get("weightQuality"):
            logw += math.log(max(n.wQuality, 1e-3))
        raw.append(clamp(logw, lo, hi))

    # ---- 3) 均值归一：只改份额不改尺度 ----
    mean_log = sum(raw) / len(raw) if raw else 0.0
    for n, lw in zip(s.nodes, raw):
        n.wDyn = 1.0 if n.locked else clamp(math.exp(lw - mean_log), w_min, w_max)
        n.wEff = clamp(n.prior * n.wDyn, 0.15, 4.0)
        n.wTgt = p_bar                            # 展示用：全网平均压力

    # ---- 4) 慢回路：质量学习 ----
    if fl.get("weightQuality"):
        s._learnT += dt
        if s._learnT >= mp.get("learnTauMs", 4000.0):
            s._learnT = 0.0
            _learn_quality(s)


def refresh_weight_state(s):
    """权重/先验变更后立即刷新派生量，使接口与界面即时一致。"""
    for n in s.nodes:
        n.wDyn = 1.0 if n.locked else clamp(n.wDyn, 0.4, 2.5)
        n.wEff = clamp(n.prior * n.wDyn, 0.15, 4.0)


def node_weight_report(s):
    """节点权重/动态项状态（供 API 与界面展示）。"""
    out = {}
    for n in s.nodes:
        out[n.id] = {
            "prior": round(n.prior, 3),          # 运维先验（可选，默认 1.0）
            "dynamic": round(n.wDyn, 3),         # 算法动态权重（主体）
            "quality": round(n.wQuality, 3),     # 质量学习乘子
            "effective": round(n.wEff, 3),       # 生效权重 = 先验 × 动态
            "utilS": round(n.utilS, 4),
            "loadDev": round(n.devS, 4),         # 相对负载偏差
            "integral": round(n.wI, 4),          # 稳态积分项
            "missEMA": round(n.missEMA, 4),      # 违约率估计
            "locked": bool(n.locked),
            "pending": n.pending, "backlog": round(n.backlog, 4), "done": n.done,
        }
    return out


# =========================================================================
# 单步推进
# =========================================================================

def pick_type(s, params):
    w = params["weights"]
    x = s.rng()
    acc = 0.0
    for k in TYPE_ORDER:
        if k not in w:
            continue
        acc += w[k]
        if x < acc:
            return k
    return "vision"


def step(s, dt, params, t):
    s.t = t

    # --- 链路实际容量（含场景劣化系数） ---
    for l in s.links:
        l.capS = l.cap * params["bwScale"][l.id]

    # --- 1. 任务到达 + 调度决策 ---
    s.acc += dt * params["rate"] / 1000.0
    guard = 0
    while s.acc >= 1 and guard < 40:
        guard += 1
        s.acc -= 1
        spawn_task(s, pick_type(s, params), t)

    _propagate(s, dt, t)


def spawn_task(s, type_key, t, dev_id=None, bytes_=None, work_=None, deadline=None,
               prio=None, source="sim", ext_id=None):
    """创建并按当前模型派发一个任务（仿真到达与硬件上报共用同一路径）。

    硬件在环上报的真实任务通过覆盖 bytes_/work_/deadline/prio 进入同一调度管线，
    因此「真实任务怎么被调度」与「仿真任务怎么被调度」口径完全一致。
    """
    T = TYPES[type_key]
    dev = DEV_BY_ID[dev_id] if dev_id in DEV_BY_ID else DEV_BY_ID[T["dev"]]
    tk = Task()
    tk.id = s.nextId
    s.nextId += 1
    tk.type = type_key
    tk.T = T
    tk.devId = dev["id"]
    tk.devY = dev["y"]
    tk.bytes = T["dataIn"] + T["dataOut"] if bytes_ is None else float(bytes_)
    tk.bytesLeft = tk.bytes
    tk.work = T["work"] if work_ is None else float(work_)
    tk.workLeft = tk.work
    tk.deadline = T["deadline"] if deadline is None else float(deadline)
    tk.prio = T["prio"] if prio is None else float(prio)
    tk.spawnT = t
    tk.nodeId = None
    tk.state = "transfer"
    tk.bwEff = 0.25
    tk._w = 1.0
    tk._share = 0.0
    tk.want = 1.0
    tk.arrived = 0.0
    tk.compressed = False
    tk.source = source
    tk.extId = ext_id

    if s.mode == "ai":
        # 综合代价择优 + 「最少在途连接数」平准项（v2 另含通算权衡）
        chosen_n, chosen_e, compressed = choose_node_ai(s, tk)
        if compressed:
            r = s.mparams.get("compressRatio", 0.35)
            k = s.mparams.get("compressWorkGain", 1.45)
            b0, w0 = tk.bytes, tk.work
            tk.bytes = b0 * r
            tk.bytesLeft = tk.bytes
            tk.work = w0 * k
            tk.workLeft = tk.work
            tk.compressed = True
            s.stat["compressed"] = s.stat.get("compressed", 0) + 1
            s.stat["compressSavedMB"] = s.stat.get("compressSavedMB", 0.0) + (b0 - tk.bytes)
            s.stat["compressExtraTFLOP"] = s.stat.get("compressExtraTFLOP", 0.0) + (tk.work - w0)
    else:
        nid = STATIC_RULE[tk.type]
        if nid is None:
            nid = "edgeA" if (s.rr % 2 == 0) else "edgeB"
            s.rr += 1
        chosen_n = s.nodeById[nid]
        chosen_e = evaluate(s, tk, chosen_n)

    tk.nodeId = chosen_n.id
    chosen_n.pending += 1
    tk.est = chosen_e
    tk.path = build_path(chosen_n.id, tk.devY)
    tk.visT = t
    tk.visDur = clamp(chosen_e["estTransfer"], 100, 1500)
    s.tasks.append(tk)
    if s.mode == "ai":
        s.log.append({
            "t": t, "type": tk.type, "node": chosen_n.tag, "lat": chosen_e["estLat"],
            "dl": tk.deadline, "ok": chosen_e["feasible"],
            "reason": ("[%s] " % tk.source if tk.source != "sim" else "") +
                      "综合代价 %.2f ｜ 预估时延 %.0fms" % (chosen_e["cost"], chosen_e["estLat"]),
        })
        if len(s.log) > 40:
            s.log.pop(0)
    return tk


def _propagate(s, dt, t):
    """单步传播：带宽分配 → 算力推进 → 链路利用率 → 回收 → 采样。

    与 spawn_task 分离，使硬件在环上报的真实任务可以复用同一套派发逻辑，
    而不必重复仿真推进代码。
    """
    # --- 2. 带宽权重（拥塞自适应优先级调度） ---
    max_u = 0.0
    for l in s.links:
        if l.utilS > max_u:
            max_u = l.utilS
    press = clamp((max_u - 0.95) / 0.35, 0, 1)
    for l in s.links:
        l.demand = 0.0
    for tk in s.tasks:
        if tk.state != "transfer":
            continue
        w = 1.0
        if s.mode == "ai":
            w = 1.0 + press * (math.pow(tk.prio, 1.30) - 1)
        tk.want = w
        for lid in NODE_LINKS[tk.nodeId]:
            s.linkById[lid].demand += w

    # --- 3. 按权重比例分配带宽（工作保持，单流上限 85%）并推进传输 ---
    for l in s.links:
        l.act = 0.0
    for tk in s.tasks:
        if tk.state != "transfer":
            continue
        links = NODE_LINKS[tk.nodeId]
        eff = 1e9
        for lid in links:
            l = s.linkById[lid]
            eff = min(eff, min(l.capS * tk.want / l.demand, l.capS * 0.85))
        tk.bwEff = max(eff, 0.004)
        for lid in links:
            s.linkById[lid].act += tk.bwEff
        tk.bytesLeft -= tk.bwEff * 0.125 * dt
        if tk.bytesLeft <= 0:
            tk.bytesLeft = 0
            tk.state = "compute"
            tk.arrived = t
            n = s.nodeById[tk.nodeId]
            n.running.append(tk)
            n.backlog += tk.workLeft

    # --- 4. 算力分配 + 推进计算 ---
    for n in s.nodes:
        alloc_node(n, dt, s, t)

    # --- 5. 链路真实利用率 ---
    for l in s.links:
        l.load = l.act
        l.util = clamp(l.act / max(l.capS, 0.01), 0, 1)
        l.utilS += (l.util - l.utilS) * min(1, dt / 300)

    # --- 5b. 节点权重：算法动态分配（仅 AI 轨；基线是静态规则映射，必须权重无关） ---
    if s.mode == "ai" and s.flags.get("nodeWeight"):
        _update_dynamic_weights(s, dt)

    # --- 6. 回收已完成任务 ---
    alive = []
    for tk in s.tasks:
        if tk.state == "compute" and tk.workLeft <= 0:
            finish(s, tk, t)
        else:
            alive.append(tk)
    s.tasks = alive
    s.stat["active"] = len(s.tasks)

    # --- 7. 采样 ---
    if s.lastSample < 0 or t - s.lastSample >= SAMPLE_MS:
        s.lastSample = t
        gu = sum(n.util for n in s.nodes) / len(s.nodes)
        bu = max(clamp(l.util, 0, 1) for l in s.links)
        s.hist["lat"].append(s.avg_lat())
        s.hist["p95"].append(s.p95())
        s.hist["gpu"].append(gu)
        s.hist["bw"].append(bu)
        for k in s.hist:
            if len(s.hist[k]) > 140:
                s.hist[k] = s.hist[k][-140:]
        s.tpWindow.append({"t": t, "c": s.stat["n"]})
        while len(s.tpWindow) > 1 and s.tpWindow[0]["t"] < t - 1200:
            s.tpWindow.pop(0)
        if len(s.tpWindow) > 1:
            f = s.tpWindow[0]
            lw = s.tpWindow[-1]
            s.throughput = 1000.0 * (lw["c"] - f["c"]) / max(lw["t"] - f["t"], 1)
        if s.lastBacklog < 0 or t - s.lastBacklog >= 10000:
            s.lastBacklog = t
            s.backlogTrace.append(len(s.tasks))
            if len(s.backlogTrace) > 12:
                s.backlogTrace.pop(0)

    if len(s.tasks) > 1200:
        del s.tasks[:len(s.tasks) - 1200]
        s.limitHit += 1


def share_group(group, cap, dt, s, t):
    """组内算力共享：按 优先级 × 紧迫度 加权，未用满的算力自动回补。"""
    if not group or cap <= 0:
        return 0.0
    open_ = list(group)
    cap_left = cap
    for _ in range(4):
        if not open_:
            break
        W = 0.0
        for tk in open_:
            w = math.pow(tk.prio, 1.35)
            if s.mode == "ai":
                if s.flags.get("liveUrgency"):
                    # 用「实时剩余工作量 ÷ 剩余松弛」识别真正来不及的任务，而非派发时的静态估计
                    need_s = tk.workLeft / max(cap, 1e-6) * 1000.0
                    slack = max(tk.deadline - (t - tk.spawnT), 8)
                    w *= clamp(1 + need_s / max(slack, 8), 1,
                               s.mparams.get("urgencyCap", 4.0))
                else:
                    slack = max(tk.deadline - (t - tk.spawnT), 8)
                    w *= clamp(1 + tk.est["estCompute"] / max(slack, 8), 1, 3.2)
            tk._w = w
            W += w
        nxt = []
        used = 0.0
        for tk in open_:
            share = cap_left * tk._w / W
            need = tk.workLeft / (dt / 1000.0)
            if need < share:
                tk._share = need
                used += need
            else:
                tk._share = share
                nxt.append(tk)
        cap_left -= used
        open_ = nxt
    tot = 0.0
    for tk in group:
        tot += tk._share
    return tot


def alloc_node(n, dt, s, t):
    """节点算力分配：实时组优先使用预留分区，其余算力给普通组。"""
    lst = n.running
    keep = []
    for tk in lst:
        if tk.workLeft > 0:
            keep.append(tk)
    n.running = keep
    lst = keep
    if not lst:
        n.util = 0.0
        n.backlog = 0.0
        n.rtLoad = 0
        n.loLoad = 0
        n.utilS += (0 - n.utilS) * min(1, dt / 200.0)
        return
    for tk in lst:
        tk._share = 0.0
    hi = [tk for tk in lst if tk.prio >= RT_PRIO]
    lo = [tk for tk in lst if tk.prio < RT_PRIO]
    reserved = n.cap * (n.reserve or 0)
    used_hi = 0.0
    if hi:
        used_hi = share_group(hi, reserved if reserved > 0 else n.cap, dt, s, t)
    if lo:
        share_group(lo, max(n.cap - used_hi, 0), dt, s, t)
    total = 0.0
    for tk in lst:
        tk.workLeft -= tk._share * dt / 1000.0
        if tk.workLeft <= 1e-9:
            tk.workLeft = 0.0
        total += tk._share
    n.util = clamp(total / n.cap, 0, 1)
    n.utilS += (n.util - n.utilS) * min(1, dt / 200.0)
    n.backlog = 0.0
    for tk in lst:
        if tk.workLeft > 0:
            n.backlog += tk.workLeft
    n.rtLoad = len(hi)
    n.loLoad = len(lo)


def finish(s, tk, t):
    n = s.nodeById[tk.nodeId]
    lat = (t - tk.spawnT) + n.lat
    miss = lat > tk.deadline
    n._accDone += 1
    if miss:
        n._accMiss += 1
    n._lDone += 1
    if miss:
        n._lMiss += 1
    n._lLat += lat / max(tk.deadline, 1e-6)      # 按任务自身截止归一化，跨类型可比
    n._lDl += 1.0
    energy = tk.work * n.ept + tk.bytes * (0.09 if n.id == "cloud" else 0.02)
    s.stat["n"] += 1
    s.stat["latSum"] += lat
    s.stat["lats"].append(lat)
    if len(s.stat["lats"]) > 400:
        s.stat["lats"].pop(0)
    if miss:
        s.stat["miss"] += 1
    s.stat["energy"] += energy
    s.stat["work"] += tk.work
    s.stat["bytes"] += tk.bytes
    if n.id == "cloud":
        s.stat["cloud"] += 1
    elif n.id == "local":
        s.stat["local"] += 1
    else:
        s.stat["edge"] += 1
    n.done += 1
    n.latSum += lat
    n.workSum += tk.work
    n.pending = max(0, n.pending - 1)
    ts = s.typeStat.get(tk.type)
    if ts is None:
        ts = {"n": 0, "lat": 0.0, "miss": 0, "node": {}}
        s.typeStat[tk.type] = ts
    ts["n"] += 1
    ts["lat"] += lat
    if miss:
        ts["miss"] += 1
    ts["node"][n.id] = ts["node"].get(n.id, 0) + 1
    s.table.append({"id": tk.id, "type": tk.type, "dev": tk.devId, "nodeId": tk.nodeId,
                    "lat": lat, "dl": tk.deadline, "miss": miss})
    if len(s.table) > 60:
        s.table.pop(0)


# =========================================================================
# 运行与统计
# =========================================================================

def run_steps(s, params, duration_ms, dt=STEP_MS, t0=None):
    """从当前时刻连续推进 duration_ms 仿真时间（按 dt 切片）。"""
    if t0 is None:
        t0 = s.t
    n = int(round(duration_ms / dt))
    t = t0
    for i in range(n):
        t = t0 + (i + 1) * dt
        step(s, dt, params, t)
    return t


def metrics(s):
    """与前端 KPI / report.txt 口径一致的聚合指标。"""
    return {
        "n": s.stat["n"],
        "t": s.t,
        "avgLat": s.avg_lat(),
        "p95": s.p95(),
        "missRate": s.miss_rate(),
        "gpu": s.gpu_util(),
        "peakBw": s.peak_bw(),
        "energyPerTask": s.energy_per_task(),
        "throughput": s.throughput,
        "backlog": len(s.tasks),
        "queue": len(s.tasks),
        "local": s.stat["local"],
        "edge": s.stat["edge"],
        "cloud": s.stat["cloud"],
        "active": s.stat["active"],
        "compressed": s.stat.get("compressed", 0),
        "workTotal": s.stat["work"],
        "bytesTotal": s.stat["bytes"],
        "dist": {"local": s.stat["local"], "edgeA": _node_dist(s, "edgeA"),
                 "edgeB": _node_dist(s, "edgeB"), "cloud": s.stat["cloud"]},
        "typeStat": s.typeStat,
    }


def _node_dist(s, nid):
    ts = s.typeStat
    tot = 0
    for k in ts:
        tot += ts[k]["node"].get(nid, 0)
    return tot


def report_block(scene, ai, bl, dur_s=None):
    """生成与 .test/report.txt 相同格式的对照文本。"""
    if dur_s is None:
        dur_s = max(ai.t, 1) / 1000.0

    def row(tag, s):
        m = metrics(s)
        arr = (s.nextId - 1) / dur_s
        return ("%-5s:%6d %7.1f %5.0f %6.1f %5.0f %7.0f %8.4f %6d|%d|%d %5d %d/%d" % (
            tag, m["n"], m["avgLat"], m["p95"], m["missRate"], m["gpu"], m["peakBw"],
            m["energyPerTask"], m["local"], m["edge"], m["cloud"], m["backlog"],
            int(round(arr)), int(round(m["throughput"]))))

    def node_line(tag, s):
        parts = " ".join("%s:%d" % (n.id, n.done) for n in s.nodes)
        links = " ".join("%s:%.0f" % (l.id, clamp(l.util, 0, 1) * 100) for l in s.links)
        return "节点 %s: %s   链路: %s" % (tag, parts, links)

    lines = []
    lines.append("")
    lines.append("===== %s =====" % scene)
    lines.append("        n     均时延   P95   超时%  GPU%  峰值BW%  单位能耗   本地|边缘|云端  积压 到达/吞吐")
    lines.append(row("AI  ", ai))
    lines.append(row("基线", bl))
    lines.append(node_line("AI", ai))
    lines.append(node_line("基", bl))
    for k in TYPE_ORDER:
        ta = ai.typeStat.get(k)
        tb = bl.typeStat.get(k)
        if ta:
            lines.append("  %-6s AI n=%4d 时延 %6.0f 超时 %2.0f%% %s" % (
                k, ta["n"], ta["lat"] / ta["n"], ta["miss"] / ta["n"] * 100, _fmt_dist(ta["node"])))
        if tb:
            lines.append("       基 n=%4d 时延 %6.0f 超时 %2.0f%% %s" % (
                tb["n"], tb["lat"] / tb["n"], tb["miss"] / tb["n"] * 100, _fmt_dist(tb["node"])))
    lines.append("积压轨迹: " + " ".join(str(x) for x in ai.backlogTrace))
    comp = ai.stat.get("compressed", 0)
    if comp:
        lines.append("通算权衡: %d 个任务降采样上传，省下 %.1f MB 现场总线流量，"
                     "代价是额外 %.2f TFLOP 算力（用算力换带宽）"
                     % (comp, ai.stat.get("compressSavedMB", 0.0),
                        ai.stat.get("compressExtraTFLOP", 0.0)))
    return "\n".join(lines)


def _fmt_dist(d):
    return "{" + ",".join('"%s":%d' % (k, d[k]) for k in d) + "}"
