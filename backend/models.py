# -*- coding: utf-8 -*-
"""
调度模型注册表。

设计原则（重要，改动前先读）：
  * `v1` 是与前端 HTML 内嵌引擎**逐位一致**的标定版模型，用于 JS↔Python 一致性
    回归（tests/test_parity.py）。**任何情况下不得修改 v1 的算法路径。**
  * `v2` 是服务端优化版，所有改动都挂在 flags 上，可单开关消融，便于用
    experiment.run_matrix() 做「替换-消融」式的量化选型（见 data/model-ablation.txt）。
  * 带宽权重策略（95% 门限 + prio^1.3 轻度倾斜）是前端历史标定的结论，
    两个版本**共用同一实现**，不做改动；v2 的增益来自择点与算力分配。
"""

from __future__ import annotations

MODELS = {}
ORDER = []


def register(m):
    MODELS[m["id"]] = m
    ORDER.append(m["id"])
    return m


# =========================================================================
# v1 · 经典代价模型（与前端内嵌引擎同源）
# =========================================================================
register({
    "id": "v1",
    "name": "经典代价模型（前端同源·标定版）",
    "short": "v1 标定版",
    "desc": "与前端 HTML 内嵌引擎逐位一致的模型：线性代价 + 在途连接数平准项。"
            "作为一致性基准与消融对照组保留。",
    "formula": "cost = 1.00·(L_est/D) + 0.70·E_node + 0.30·U_node + 0.35·O_work + 3.0·max(0, L_est/D − 1)"
               "  ；node* = argmin[ cost + 0.20 × 在途连接数 ]",
    "flags": {},
    "params": {},
    "cards": [
        {"t": "择点代价",
         "b": "L_est = 路径时延 + 传输时延 + 排队时延 + 计算时延；队列项按 <code>0.20 × 积压/cap</code> 粗估；"
              "违约惩罚 3.0×线性放大；平准项 <code>0.20 × 在途连接数</code> 保证对称边缘节点交替承接。"},
        {"t": "带宽与算力",
         "b": "利用率 ≤95% 等权公平共享，越过后线性渐入 <code>prio^1.30</code> 轻度倾斜；"
              "算力按 <code>优先级^1.35 × 截止紧迫度</code> 加权，现场节点预留 60% 供实时控制。"},
    ],
    "default": False,
})


# =========================================================================
# v2 · 服务端优化版
# =========================================================================
register({
    "id": "v2",
    "name": "服务端优化版：流体排队修正 + 通算权衡（降采样换带宽）",
    "short": "v2 优化版",
    "desc": "在 v1 之上做两项结构性优化：① 排队时延改为处理器共享的流体排空估计"
            "（积压 / 本任务可得分片），并把算力占用归一化为「相当于几个截止窗口」；"
            "② 违约惩罚饱和化，避免数值巨大项淹没候选差异。"
            "③ 新增「通算权衡」能力：当某个任务若不降采样就必然违约时，评估"
            "「现场降采样后上传」（数据量 ×0.35、计算量 ×1.45）能否把它拉回可行——"
            "用算力换带宽，直接缓解现场总线这一全系统共享瓶颈。",
    "formula": "cost = 1.00·(L_est/D) + 0.70·E_node + 0.30·U_node + 0.35·O_norm"
               " + 3.0·over/(1+over) + price_n；node* = argmin[ cost + 0.20 × 在途连接数 ]；"
               " 数据形态 ∈ {原始, 降采样 0.35×/1.45×} 一并择优；"
               " price_n = clip(kp·(U_n − u*·w_n) + I_n + km·missEMA_n, 0, 1.5)",
    # 该开关组合由 model_lab 的组合搜索（64 组）+ 双种子留出集验证选出，
    # 详见 backend/data/model-ablation.txt 与 README「模型优化」一节。
    "flags": {
        "fluidQueue": True,      # 排队时延：backlog / shareCap（处理器共享流体排空）
        "wfsShare": False,       # （消融未通过）加权公平份额估计，会高估拥塞、压低长任务
        "slackEnergy": False,    # （消融未通过）能耗项随松弛度衰减
        "satViolation": True,    # 违约惩罚饱和化 over/(1+over)
        "normOccupancy": True,   # 算力占用归一化为「相当于几个截止窗口」
        "allocShare": False,     # （消融未通过）按分配器份额估传输
        "flowShare": False,      # （消融未通过）按并发流折减传输（与拥塞折减重复计算）
        "liveUrgency": False,    # （消融未通过）实时剩余工作量驱动算力权重
        "leastHarm": False,      # （消融未通过）全不可行时最小化绝对时延
        "rtExternality": True,   # 实时任务挤占无预留节点普通任务的外部性定价
        "compress": True,        # 通算权衡：必要时降采样/压缩大带宽任务
        "nodeWeight": True,      # 节点权重由算法动态分配（压力比 → 权重，主体）
        "weightQuality": True,   # 叠加质量学习乘子（慢回路，按实测结果长期偏好）
        "weightIntegral": False,  # 积分项：本场景矩阵上实测贡献 ≈0.05（噪声级），默认关
        "weightMiss": False,     # 相对违约率反馈：实测贡献 ≈0.05，默认关
    },
    "params": {
        "queueFactor": 0.5,        # 流体排队系数
        "queueFactorWFS": 0.15,    # wfsShare 开启时的弱积压信号系数
        "rtWanPenalty": 8.0,       # 实时控制任务跨广域网的附加代价
        "rtSpoilWeight": 0.6,      # 实时任务挤占普通任务的外部性权重
        "violationWeight": 3.0,
        "compressMinBytes": 4.0,   # 触发压缩的数据量门限（MB）
        "compressMinU": 0.0,       # 链路利用率门限（0=完全交给代价函数判断）
        "compressRatio": 0.35,     # 降采样后数据量比例（ROI 裁剪 + 降分辨率 + 帧间编码）
        "compressWorkGain": 1.45,  # 降采样带来的计算量增益
        "compressPenalty": 0.0,    # 压缩的附加代价（可用于抑制过度压缩）
        "urgencyCap": 4.0,
        # —— 节点权重的动态分配控制器 ——（权重完全由算法算，无需人工输入）
        # 增益由 model_lab params 在双种子×120s 上比选
        "dynGamma": 0.5,           # 压力比 → 权重 的指数（主体强度，越小越温和）
        "dynQueueW": 0.15,         # 积压（backlog/cap）折算进压力的权重
        "dynKi": 0.05,             # I 增益（默认关，见 flags.weightIntegral）
        "dynLeak": 0.5,            # 积分泄漏（防饱和钉在上下限）
        "dynIMax": 0.35,           # 积分项限幅
        "dynKm": 0.5,              # M 增益（默认关，见 flags.weightMiss）
        "dynWMin": 0.6,            # 动态权重下限
        "dynWMax": 1.8,            # 动态权重上限
        "dynTauMs": 800.0,         # 压力比平滑时间常数
        "missTau": 2000.0,         # 违约率平滑时间常数（ms）
        "missWindowMs": 500.0,     # 违约率统计窗口（ms）
        # —— 质量学习（慢回路）——
        "learnTauMs": 4000.0,      # 学习周期（仿真时间 ms）
        "learnRate": 0.15,         # 乘性更新步长（Hedge 型）
        "qualityMin": 0.6,         # 质量乘子下限
        "qualityMax": 1.6,         # 质量乘子上限
        "learnMinSamples": 3,      # 一个周期内至少完成的样本数才参与学习
        "wEffMin": 0.25,           # 生效权重的折扣下限（柔性项最大放大 4 倍）
        "wEffMax": 4.0,            # 生效权重的折扣上限（柔性项最大压到 1/4）
    },
    "cards": [
        {"t": "① 排队时延：从粗估到流体排空",
         "b": "v1 用 <code>0.20 × 积压 / 整机算力</code> 粗估排队；v2 改为 "
              "<code>积压 / 本任务可得分片</code>——分母是任务真正能拿到的算力，"
              "因此能区分「节点很忙但我的份额大」与「节点很忙且我会被挤扁」两种情况。"},
        {"t": "② 算力占用归一化 + 违约惩罚饱和化",
         "b": "占用度改为 <code>work / (cap × 截止时间)</code>，即「相当于占用几个截止窗口」，"
              "量纲与其它项可比；违约惩罚改为 <code>3.0·over/(1+over)</code> 饱和形式——"
              "原线性惩罚在过载时数值巨大且在各候选间近乎相等，会把负载信号淹没。"},
        {"t": "③ 通算权衡：用算力换带宽（本次核心增益）",
         "b": "现场总线是全系统共享瓶颈。调度器现在会在候选节点上同时评估两种数据形态："
              "<b>原样上传</b> 与 <b>现场降采样后上传</b>（数据量 ×0.35、计算量 ×1.45），"
              "只有当原样上传「无论选哪个节点都注定违约」而降采样能把它拉回可行时才会采用。"
              "实测：视觉质检高峰场景超时率 50.8% → 0.0%，平均时延 291ms → 107ms，"
              "代价是整体算力 +1.2%。"},
        {"t": "④ 节点权重与智能分配（本次新增）",
         "b": "每个算力节点可设<b>权重</b>（默认 1.0，运维可在线调整），权重表达「这个节点应当承担多少负载」："
              "目标利用率 <code>u*×w</code>（夹在 25%~95%）。调度器用<b>对偶上升 PI 控制器</b>比较实测利用率与目标，"
              "把偏差转成该节点的<b>动态价格</b>并计入择点代价——于是流量会自动从超载节点流向空闲节点，"
              "运维只需调权重，不必改代价函数。价格上还叠加<b>违约率反馈</b>：最近频繁违约的节点被临时加价规避，"
              "让闭环直接作用于「降低超时」这个真正的目标，而不只是把利用率拉平。"
              "价格与积分项都被夹在 <code>[0, 1.5]</code> 内，保证不发散。"
              "调高某节点权重即可让它承接更多任务；调低则自动卸载。"},
        {"t": "⑤ 保持不变的标定结论",
         "b": "带宽策略仍是「非必要不干预」：利用率 ≤95% 等权公平共享，越过后按 "
              "<code>prio^1.30</code> 轻度倾斜（历史标定：更强倾斜会在饱和期推高平均时延）；"
              "现场节点仍为实时控制预留 60% 算力。"},
        {"t": "⑥ 选型方法（可复现）",
         "b": "上述开关组合由 <code>python -m backend.model_lab search</code> 在 64 组开关组合上"
              "穷举选出，再用<b>不同随机种子 × 不同时长</b>的留出集复测（见 "
              "<code>backend/data/model-ablation.txt</code>）。被消融淘汰的开关已在 flags 中标注，"
              "保留以便复现实验。"},
    ],
    "default": True,
})

DEFAULT = "v2"


def get(model_id):
    return MODELS.get(model_id) or MODELS[DEFAULT]


def variant(base_id="v2", name=None, **flag_overrides):
    """基于某个模型派生一个消融变体（只改 flags，不动 params）。"""
    base = get(base_id)
    flags = dict(base["flags"])
    for k, v in flag_overrides.items():
        flags[k] = v
    tag = "+".join(sorted(k for k, v in flag_overrides.items() if v))
    off = ",".join(sorted(k for k, v in flag_overrides.items() if not v))
    label = name or ("%s[%s%s%s]" % (base_id, tag or "无", "−" if off else "", off))
    return {
        "id": "%s~%s" % (base_id, ("%s|%s" % (tag, off)).strip("|") or "base"),
        "name": label,
        "short": label,
        "desc": "消融变体：%s" % (label,),
        "formula": base["formula"],
        "flags": flags,
        "params": dict(base["params"]),
        "cards": base["cards"],
        "default": False,
        "base": base_id,
        "ablation": dict(flag_overrides),
    }


def catalog():
    return [{"id": m["id"], "name": m["name"], "short": m["short"], "desc": m["desc"],
             "formula": m["formula"], "flags": m["flags"], "params": m["params"],
             "cards": m["cards"], "default": bool(m.get("default"))}
            for m in (MODELS[i] for i in ORDER) if "~" not in m["id"]]
