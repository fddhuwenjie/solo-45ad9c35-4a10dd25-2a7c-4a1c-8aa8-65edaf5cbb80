#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""短射平衡试验 HTTP 服务（仅标准库 wsgiref / json / sqlite3）。

接收多腔注塑短射试验数据：流道树、型腔容积、分级螺杆行程、压力曲线、
逐腔重量、秤校验值与失衡限值；还原各级物料去向，核对物料账，输出每腔
充填比例、进料滞后、级间增量、最早分流异常点，并沿流道树定位最早发生
失衡的分支节点；区分连续扩大偏差与孤立称量噪声。支持人工剔除称量
（须附理由）与登记浇口修整另起方案。报告与批次差异均为可独立追溯的
可复算 JSON：复算输入、实际采用的记录、计算指标与人工处理同快照返回。

模内压力传播复核：批次可为流道节点与型腔挂接传感器，提交量程、校准
有效期、采集器触发锚点与各级带时标压力序列。按锚点对齐机台曲线与各
采集器模内曲线，沿流道树计算到压时刻、父子传播延迟、压力衰减与注射
区间积分，再与逐腔充填比例、进料滞后交叉判断最早受限分支（主流道 /
分支 / 浇口）。时钟残差过大、采样断档、超量程、校准失效一律列出节点
与原始区间；压力证据与称重趋势相反时不给结论。人工改锚点、排除坏点
须附理由并派生修订；报告、批次差异与复算 JSON 共用同一份冻结曲线、
对齐参数与判断依据。
"""

import json
import sqlite3
import traceback
from datetime import datetime, timezone
from wsgiref.simple_server import make_server
from wsgiref.util import setup_testing_defaults  # noqa: F401  (保留给测试)

DB_PATH = "shortshot.db"

MIN_EFFECTIVE_STAGES = 3          # 有效级数下限
DEFAULT_BALANCE_TOL = 0.08        # 物料账默认容差（相对）
FILL_START_FRACTION = 0.005       # 判定某腔开始进料的增量阈值（占其终重比例）
PRESSURE_MIN_SAMPLES = 2          # 每级压力曲线最少采样点

# ---- 模内压力传播复核 ----
DEFAULT_PRESSURE = {
    "arrival_fraction": 0.10,     # 到压判据：达到基线→峰值跃升的比例
    "delay_tolerance_s": 0.020,   # 父子传播延迟同级同类最大差（秒）
    "attenuation_tolerance": 0.10,  # 同级兄弟支峰压相对衰减差限值
    "integral_tolerance": 0.10,   # 同级兄弟支注射区间积分相对差限值
    "clock_residual_s": 0.005,    # 采集器触发锚点逐帧抖动残差限值（秒）
    "max_sample_gap_s": 0.050,    # 采样断档限值（相邻点时间间隔，秒）
}


# ---------------------------------------------------------------- 存储层

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS batches (
            batch_id   TEXT PRIMARY KEY,
            payload    TEXT NOT NULL,          -- 原始请求 JSON（可复算）
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS exclusions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id   TEXT NOT NULL REFERENCES batches(batch_id),
            stage      INTEGER NOT NULL,
            cavity_id  TEXT NOT NULL,
            reason     TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (batch_id, stage, cavity_id)
        );
        CREATE TABLE IF NOT EXISTS plans (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id    TEXT NOT NULL REFERENCES batches(batch_id),
            note        TEXT,
            gate_changes TEXT NOT NULL,        -- [{cavity_id, gate_reduction_mm}]
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pressure_revisions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id   TEXT NOT NULL REFERENCES batches(batch_id),
            kind       TEXT NOT NULL,          -- anchor_override | bad_point
            sensor_id  TEXT NOT NULL,
            stage      INTEGER,                -- bad_point 必填；锚点修订可为空=全部级
            collector  TEXT,                   -- anchor_override 时填采集器
            t_from     REAL, t_to REAL,        -- bad_point 的原始时标区间（秒）
            old_anchor REAL, new_anchor REAL,  -- anchor_override 锚点前后值
            reason     TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 校验

def _err(stage, cavity, code, message):
    return {"stage": stage, "cavity": cavity, "code": code, "message": message}


def parse_runner_tree(tree):
    """归一化流道树为节点列表 [{node_id, parent, children, feeds}]。

    支持三种写法：
      1. {"feeds": [{"cavity_id": ...}, ...]}        扁平，单根直接出各腔
      2. {"root": "...", "nodes": [{node_id, children, feeds}, ...]}  邻接表
      3. {"node_id": ..., "children": [{...}], "feeds": [...]}        嵌套
    返回 (nodes, None)；空树或结构非法返回 (None, (code, message))。
    """
    if not isinstance(tree, dict) or not tree:
        return None, ("EMPTY_RUNNER_TREE",
                      "流道树为空：短射平衡必须提供流道拓扑")

    def _norm_feeds(feeds):
        out = []
        for f in feeds or []:
            out.append(f.get("cavity_id") if isinstance(f, dict) else f)
        return [c for c in out if c]

    nodes = []
    if "nodes" in tree:
        raw = tree.get("nodes") or []
        if not raw:
            return None, ("EMPTY_RUNNER_TREE", "流道树 nodes 为空")
        by_id = {}
        for n in raw:
            nid = n.get("node_id")
            if not nid:
                return None, ("RUNNER_TREE_INVALID", "流道节点缺少 node_id")
            if nid in by_id:
                return None, ("RUNNER_TREE_INVALID",
                              "流道节点重复：%s" % nid)
            by_id[nid] = {"node_id": nid, "parent": None,
                          "children": list(n.get("children") or []),
                          "feeds": _norm_feeds(n.get("feeds"))}
        for n in by_id.values():
            for ch in n["children"]:
                if ch not in by_id:
                    return None, ("RUNNER_TREE_INVALID",
                                  "节点 %s 指向不存在的子节点 %s"
                                  % (n["node_id"], ch))
                if by_id[ch]["parent"] is not None:
                    return None, ("RUNNER_TREE_INVALID",
                                  "节点 %s 有多个父节点" % ch)
                by_id[ch]["parent"] = n["node_id"]
        declared = tree.get("root")
        roots = [nid for nid, n in by_id.items() if n["parent"] is None]
        if declared:
            if declared not in by_id:
                return None, ("RUNNER_TREE_INVALID",
                              "root 指向不存在的节点：%s" % declared)
            root = declared
        elif len(roots) == 1:
            root = roots[0]
        else:
            return None, ("RUNNER_TREE_INVALID",
                          "流道树不连通：存在 %d 个根" % len(roots))
        seen, stack = set(), [root]
        while stack:
            cur = stack.pop()
            if cur in seen:
                return None, ("RUNNER_TREE_INVALID",
                              "流道树存在环：%s" % cur)
            seen.add(cur)
            stack.extend(by_id[cur]["children"])
        if len(seen) != len(by_id):
            return None, ("RUNNER_TREE_INVALID", "流道树存在不可达节点")
        nodes = list(by_id.values())
    elif "node_id" in tree:
        def _walk(obj, parent):
            nid = obj.get("node_id")
            if not nid:
                return None
            node = {"node_id": nid, "parent": parent,
                    "children": [], "feeds": _norm_feeds(obj.get("feeds"))}
            nodes.append(node)
            for ch in obj.get("children") or []:
                child = _walk(ch, nid)
                if child is None:
                    return None
                node["children"].append(child)
            return nid

        if _walk(tree, None) is None:
            return None, ("RUNNER_TREE_INVALID",
                          "嵌套流道树存在缺 node_id 的节点")
        ids = [n["node_id"] for n in nodes]
        if len(set(ids)) != len(ids):
            return None, ("RUNNER_TREE_INVALID", "流道节点重复")
    elif "feeds" in tree:
        feeds = _norm_feeds(tree.get("feeds"))
        if not feeds:
            return None, ("EMPTY_RUNNER_TREE",
                          "流道树为空：feeds 未接任何型腔")
        nodes = [{"node_id": "root", "parent": None,
                  "children": [], "feeds": feeds}]
    else:
        return None, ("EMPTY_RUNNER_TREE",
                      "流道树为空：未提供 nodes / node_id / feeds")

    if not any(n["feeds"] for n in nodes):
        return None, ("EMPTY_RUNNER_TREE", "流道树没有任何型腔出口")
    return nodes, None


def validate_batch(p):
    """返回错误列表；非空则拒绝入库。错误均点出原始级次与型腔。"""
    errors = []

    cavities = p.get("cavities") or []
    stages = p.get("stages") or []
    scale = p.get("scale_check") or {}

    # 型腔重号
    seen, dup = set(), set()
    for c in cavities:
        cid = c.get("cavity_id")
        if cid in seen:
            dup.add(cid)
        seen.add(cid)
    for cid in sorted(dup):
        errors.append(_err(None, cid, "DUPLICATE_CAVITY",
                           "型腔编号重复：%s" % cid))
    if not cavities:
        errors.append(_err(None, None, "NO_CAVITY", "未提供型腔容积表"))

    # 流道树：空树明确拒绝；有效树须拓扑完整且每个型腔恰好接入一次
    tree_nodes, tree_err = parse_runner_tree(p.get("runner_tree"))
    if tree_err:
        errors.append(_err(None, None, tree_err[0], tree_err[1]))
    else:
        feed_count = {}
        for n in tree_nodes:
            for cid in n["feeds"]:
                if cid not in seen:
                    errors.append(_err(None, cid, "UNKNOWN_CAVITY",
                                       "流道树指向未登记的型腔：%s" % cid))
                feed_count[cid] = feed_count.get(cid, 0) + 1
        for cid in sorted(feed_count):
            if feed_count[cid] > 1:
                errors.append(_err(None, cid, "CAVITY_FED_TWICE",
                                   "型腔被流道树重复进料：%s" % cid))
        for c in cavities:
            cid = c.get("cavity_id")
            if cid not in feed_count:
                errors.append(_err(None, cid, "CAVITY_NOT_FED",
                                   "型腔未接入流道树：%s" % cid))
    # 模内压力传感器（可选数据）；树非法时节点挂接检查跳过
    validate_sensors(p, tree_nodes if not tree_err else None,
                     {c.get("cavity_id") for c in cavities}, errors)

    # 级次与螺杆行程必须严格递增
    prev_no, prev_travel = None, None
    for s in stages:
        no = s.get("stage")
        travel = s.get("screw_travel")
        if prev_no is not None:
            if not isinstance(no, int) or no <= prev_no:
                errors.append(_err(no, None, "STAGE_NOT_INCREASING",
                                   "级次不递增：第 %s 级接在第 %s 级之后"
                                   % (no, prev_no)))
            if travel is None or travel <= prev_travel:
                errors.append(_err(no, None, "TRAVEL_NOT_INCREASING",
                                   "螺杆行程不递增：第 %s 级行程 %s 不大于第 %s 级 %s"
                                   % (no, travel, prev_no, prev_travel)))
        prev_no, prev_travel = no, travel

    # 压力记录缺段
    for s in stages:
        curve = s.get("pressure_curve")
        if not curve or len(curve) < PRESSURE_MIN_SAMPLES:
            errors.append(_err(s.get("stage"), None, "PRESSURE_GAP",
                               "第 %s 级压力记录缺段（采样点不足 %d）"
                               % (s.get("stage"), PRESSURE_MIN_SAMPLES)))
        else:
            ts = [pt[0] for pt in curve]
            if any(b <= a for a, b in zip(ts, ts[1:])):
                errors.append(_err(s.get("stage"), None, "PRESSURE_GAP",
                                   "第 %s 级压力曲线时间轴不连续/不回溯"
                                   % s.get("stage")))

    # 秤在试验期间失准
    before = scale.get("before")
    after = scale.get("after")
    tol = scale.get("tolerance")
    if before is None or after is None or tol is None:
        errors.append(_err(None, None, "SCALE_CHECK_MISSING",
                           "缺少秤校验值（before/after/tolerance）"))
    elif abs(after - before) > tol:
        errors.append(_err(None, None, "SCALE_DRIFT",
                           "秤在试验期间失准：试验前 %.4f g、试验后 %.4f g，"
                           "漂移 %.4f g 超出允差 %.4f g"
                           % (before, after, abs(after - before), tol)))

    # 有效级数不足（每级至少一条逐腔称量）
    effective = [s for s in stages if s.get("cavity_weights")]
    if len(effective) < MIN_EFFECTIVE_STAGES:
        errors.append(_err(None, None, "TOO_FEW_STAGES",
                           "有效级数不足：%d 级，少于要求的 %d 级"
                           % (len(effective), MIN_EFFECTIVE_STAGES)))

    # 物料账不闭合（按级核对增量）
    density = p.get("melt_density")
    balance_tol = p.get("balance_tolerance", DEFAULT_BALANCE_TOL)
    if density:
        prev_shot = 0.0
        prev_cav = {c["cavity_id"]: 0.0 for c in cavities}
        prev_runner = 0.0
        for s in stages:
            shot = s.get("shot_volume")
            if shot is None:
                errors.append(_err(s.get("stage"), None, "SHOT_MISSING",
                                   "第 %s 级缺少射出量 shot_volume" % s.get("stage")))
                continue
            inj = (shot - prev_shot) * density
            weights = s.get("cavity_weights") or {}
            runner_w = s.get("runner_weight", 0.0) or 0.0
            measured = (runner_w - prev_runner)
            for cid in prev_cav:
                measured += weights.get(cid, 0.0) - prev_cav[cid]
            if inj > 1e-9:
                rel = abs(inj - measured) / inj
                if rel > balance_tol:
                    errors.append(_err(
                        s.get("stage"), None, "BALANCE_NOT_CLOSED",
                        "第 %s 级物料账不闭合：射出 %.3f g，制件+流道料合计 "
                        "%.3f g，偏差 %.1f%% 超出 %.1f%%"
                        % (s.get("stage"), inj, measured,
                           rel * 100, balance_tol * 100)))
            prev_shot = shot
            prev_runner = runner_w
            for cid in prev_cav:
                prev_cav[cid] = weights.get(cid, prev_cav[cid])
    else:
        errors.append(_err(None, None, "DENSITY_MISSING",
                           "缺少熔体密度 melt_density，无法核对物料账"))

    return errors


def parse_iso_date(s):
    """宽容解析 YYYY-MM-DD（可带时间）为 date；非法返回 None。"""
    if not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _valid_trace(points):
    return isinstance(points, list) and len(points) >= 2 and all(
        isinstance(pt, (list, tuple)) and len(pt) == 2
        and isinstance(pt[0], (int, float))
        and isinstance(pt[1], (int, float))
        and (j == 0 or pt[0] > points[j - 1][0])
        for j, pt in enumerate(points))


def validate_sensors(p, tree_nodes, cavity_ids, errors):
    """校验模内传感器挂接、量程、校准有效期与触发锚点/序列的结构。

    传感器为可选数据（整批复核不挂传感器时 pressure_review 记 no_sensors）。
    校准过期、超量程、断档、时钟残差等属分析期数据质量问题，不在这里拦截；
    此处只拒绝结构性错误（重复挂接、量程非法、锚点缺失、曲线非法等）。
    """
    sensors = p.get("sensors")
    if sensors is None:
        return
    if not isinstance(sensors, list) or not sensors:
        errors.append(_err(None, None, "SENSORS_INVALID",
                           "sensors 须为非空列表（无传感器时请省略该字段）"))
        return
    node_ids = {n["node_id"] for n in (tree_nodes or [])}

    seen_ids, seen_targets = set(), set()
    for s in sensors:
        sid = s.get("sensor_id")
        if not sid:
            errors.append(_err(None, None, "SENSOR_ID_MISSING",
                               "存在缺少 sensor_id 的传感器"))
            continue
        if sid in seen_ids:
            errors.append(_err(None, sid, "SENSOR_DUPLICATE",
                               "传感器编号重复：%s" % sid))
        seen_ids.add(sid)
        kind, tid = s.get("target_kind"), s.get("target_id")
        if kind not in ("node", "cavity") or not tid:
            errors.append(_err(None, sid, "SENSOR_TARGET_UNKNOWN",
                               "传感器 %s 的 target_kind/target_id 无效" % sid))
        elif (kind, tid) in seen_targets:
            errors.append(_err(None, sid, "SENSOR_DUPLICATE_TARGET",
                               "目标 %s %s 已挂有传感器" % (kind, tid)))
        else:
            seen_targets.add((kind, tid))
            if kind == "node":
                if not tree_nodes:
                    errors.append(_err(None, sid, "SENSOR_TARGET_UNKNOWN",
                                       "传感器 %s 挂接流道节点，但流道树非法"
                                       % sid))
                elif tid not in node_ids:
                    errors.append(_err(None, sid, "SENSOR_TARGET_UNKNOWN",
                                       "传感器 %s 挂接到不存在的流道节点：%s"
                                       % (sid, tid)))
            elif tid not in cavity_ids:
                errors.append(_err(None, sid, "SENSOR_TARGET_UNKNOWN",
                                   "传感器 %s 挂接到不存在的型腔：%s"
                                   % (sid, tid)))
        rng = s.get("range")
        if not isinstance(rng, dict) or not isinstance(rng.get("max_pressure"),
                                                       (int, float)) \
                or rng["max_pressure"] <= 0:
            errors.append(_err(None, sid, "SENSOR_RANGE_INVALID",
                               "传感器 %s 缺少有效量程 range.max_pressure"
                               % sid))
        if parse_iso_date(s.get("calibration_valid_until")) is None:
            errors.append(_err(None, sid, "CALIBRATION_DATE_INVALID",
                               "传感器 %s 校准有效期缺失或非 YYYY-MM-DD"
                               % sid))

    stages = p.get("stages") or []
    anchors = p.get("trigger_anchors") or {}
    if not isinstance(anchors, dict):
        errors.append(_err(None, None, "TRIGGER_ANCHORS_INVALID",
                           "trigger_anchors 须为 {stage: {collector: anchor}}"))
    else:
        for st in stages:
            no = st.get("stage")
            row = anchors.get(str(no), anchors.get(no))
            if row is not None and not isinstance(row, dict):
                errors.append(_err(no, None, "TRIGGER_ANCHORS_INVALID",
                                   "第 %s 级触发锚点不是采集器映射对象" % no))

    for st in stages:
        no = st.get("stage")
        traces = st.get("mold_pressure") or {}
        if not isinstance(traces, dict):
            errors.append(_err(no, None, "MOLD_TRACE_INVALID",
                               "第 %s 级 mold_pressure 须为传感器映射对象" % no))
            continue
        for sid, pts in traces.items():
            if sid not in seen_ids:
                errors.append(_err(no, None, "MOLD_TRACE_UNKNOWN_SENSOR",
                                   "第 %s 级出现未登记传感器的压力序列：%s"
                                   % (no, sid)))
            elif not _valid_trace(pts):
                errors.append(_err(no, None, "MOLD_TRACE_INVALID",
                                   "第 %s 级传感器 %s 的压力序列非法"
                                   "（至少 2 点且 [时标, 压力] 严格升序）"
                                   % (no, sid)))


# ---------------------------------------------------------------- 分析引擎

def branch_imbalance(tree_nodes, per_stage_fill, stage_axis, limit):
    """沿流道树定位最早发生失衡的分支节点。

    对每个分出 ≥2 支的节点（子节点或直接进料的型腔都算作一支），
    逐级比较各支下游型腔的平均充填度；返回最早越限的级次、节点、
    相关型腔与各支明细。同级多个节点越限时取最靠近根者。
    """
    by_id = {n["node_id"]: n for n in tree_nodes}

    depth, frontier, d = {}, [n["node_id"] for n in tree_nodes
                              if n["parent"] is None], 0
    while frontier:
        nxt = []
        for nid in frontier:
            depth[nid] = d
            nxt.extend(by_id[nid]["children"])
        frontier, d = nxt, d + 1

    def subtree_cavities(nid):
        node = by_id[nid]
        cavs = list(node["feeds"])
        for ch in node["children"]:
            cavs.extend(subtree_cavities(ch))
        return cavs

    candidates = []
    for n in tree_nodes:
        branches = ([("node", c) for c in n["children"]] +
                    [("cavity", c) for c in n["feeds"]])
        if len(branches) >= 2:
            candidates.append((n, branches))

    for i, stage_no in enumerate(stage_axis):
        row = per_stage_fill[i]
        viol = []
        for n, branches in candidates:
            fills = []
            for kind, bid in branches:
                cavs = [bid] if kind == "cavity" else subtree_cavities(bid)
                vals = [row[c] for c in cavs if c in row]
                if vals:
                    fills.append((bid, sum(vals) / len(vals), cavs))
            if len(fills) < 2:
                continue
            spread = max(f[1] for f in fills) - min(f[1] for f in fills)
            if spread > limit:
                viol.append((depth.get(n["node_id"], 0), n["node_id"],
                             n, fills, spread))
        if viol:
            viol.sort(key=lambda v: (v[0], v[1]))
            _, _, node, fills, spread = viol[0]
            fastest = max(fills, key=lambda f: f[1])
            slowest = min(fills, key=lambda f: f[1])
            return {
                "stage": stage_no,
                "node_id": node["node_id"],
                "spread": round(spread, 4),
                "fastest_branch": fastest[0],
                "slowest_branch": slowest[0],
                "cavities": sorted(set(fastest[2]) | set(slowest[2])),
                "branches": [{"branch": b, "mean_fill": round(f, 4),
                              "cavities": cavs}
                             for b, f, cavs in fills],
            }
    return None


def analyze(p, exclusions, pressure_revisions=None):
    """还原各级物料去向，输出充填比例、进料滞后、级间增量与异常判定。"""
    cavities = {c["cavity_id"]: c["volume"] for c in p["cavities"]}
    density = p["melt_density"]
    limit = p.get("imbalance_limit", 0.05)
    stages = sorted(p["stages"], key=lambda s: s["stage"])
    excluded = {(e["stage"], e["cavity_id"]) for e in exclusions}

    capacity = {cid: cavities[cid] * density for cid in cavities}

    # 逐腔累计重量序列（剔除点按缺失处理，用相邻线性插值补齐以便续算）
    series = {cid: [] for cid in cavities}
    for s in stages:
        w = s.get("cavity_weights") or {}
        for cid in cavities:
            key = (s["stage"], cid)
            val = None if key in excluded else w.get(cid)
            series[cid].append(val)
    for cid, vals in series.items():
        for i, v in enumerate(vals):
            if v is None:
                lo = next((vals[j] for j in range(i - 1, -1, -1)
                           if vals[j] is not None), 0.0)
                hi = next((vals[j] for j in range(i + 1, len(vals))
                           if vals[j] is not None), lo)
                vals[i] = (lo + hi) / 2.0

    # 级间增量
    increments = {}
    for cid, vals in series.items():
        increments[cid] = [round(vals[i] - (vals[i - 1] if i else 0.0), 4)
                           for i in range(len(vals))]

    # 充填比例（末级）
    fill_ratio = {cid: round(series[cid][-1] / capacity[cid], 4)
                  for cid in cavities}

    # 进料滞后：各腔首次明显进料所在级，相对最早腔的级差
    first_fill = {}
    for cid, vals in series.items():
        final_w = vals[-1] if vals[-1] else 1e-9
        thresh = max(FILL_START_FRACTION * final_w, 1e-6)
        first_fill[cid] = next(
            (i for i in range(len(vals))
             if increments[cid][i] > thresh), None)
    known = [i for i in first_fill.values() if i is not None]
    earliest = min(known) if known else 0
    feed_lag = {cid: (None if first_fill[cid] is None
                      else first_fill[cid] - earliest)
                for cid in cavities}

    # 每级各腔充填度（累计重 / 容量），求最早分流异常点
    per_stage_fill = []
    for i, s in enumerate(stages):
        row = {cid: series[cid][i] / capacity[cid] for cid in cavities}
        per_stage_fill.append(row)
    anomaly = None
    for i, s in enumerate(stages):
        row = per_stage_fill[i]
        spread = max(row.values()) - min(row.values())
        if spread > limit:
            worst = max(row, key=row.get), min(row, key=row.get)
            anomaly = {"stage": s["stage"],
                       "spread": round(spread, 4),
                       "fastest": worst[0], "slowest": worst[1]}
            break

    # 沿流道树定位最早失衡的分支节点（空树/坏树在入库前已被拦截）
    tree_nodes, _ = parse_runner_tree(p.get("runner_tree"))
    branch = branch_imbalance(tree_nodes, per_stage_fill,
                              [s["stage"] for s in stages], limit) \
        if tree_nodes else None

    # 偏差性质：连续扩大 vs 孤立称量噪声
    deviation_type = {}
    n = len(stages)
    for cid in cavities:
        devs = []
        for i in range(n):
            row = per_stage_fill[i]
            mean = sum(row.values()) / len(row)
            devs.append(row[cid] - mean)
        over = [abs(d) > limit for d in devs]
        growing = 0
        for i in range(1, n):
            if over[i] and over[i - 1] and abs(devs[i]) > abs(devs[i - 1]):
                growing += 1
        if growing >= 1 and sum(over) >= 2:
            kind = "continuous_divergence"   # 连续扩大，疑似流道/浇口/排气系统性差异
        elif sum(over) == 1:
            kind = "isolated_noise"          # 孤立越限，疑似称量噪声
        else:
            kind = "within_limit"
        deviation_type[cid] = {
            "kind": kind,
            "max_deviation": round(max(abs(d) for d in devs), 4),
            "stages_over_limit": [stages[i]["stage"]
                                  for i in range(n) if over[i]],
        }

    # 物料去向汇总（末级口径）
    total_shot = stages[-1].get("shot_volume", 0.0) * density
    parts = sum(series[cid][-1] for cid in cavities)
    runner_w = stages[-1].get("runner_weight", 0.0) or 0.0
    material = {
        "injected_g": round(total_shot, 4),
        "parts_g": round(parts, 4),
        "runner_g": round(runner_w, 4),
        "unaccounted_g": round(total_shot - parts - runner_w, 4),
    }

    metrics = {
        "stage_axis": [s["stage"] for s in stages],
        "fill_ratio": fill_ratio,
        "feed_lag_stages": feed_lag,
        "stage_increments_g": increments,
        "earliest_flow_imbalance": anomaly,
        "earliest_branch_imbalance": branch,
        "runner_node_count": len(tree_nodes or []),
        "deviation_assessment": deviation_type,
        "material_balance": material,
        "imbalance_limit": limit,
    }
    # 模内压力传播复核：冻结曲线、对齐参数、沿树指标与称重交叉判断
    metrics["pressure_review"] = pressure_review(
        p, pressure_revisions or [], metrics)
    return metrics


# ------------------------------------------------- 模内压力传播复核引擎

def _median(vals):
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _interp(points, t, t_idx=0, v_idx=1):
    """分段线性插值（越界取端点值）。"""
    if not points:
        return None
    if t <= points[0][t_idx]:
        return points[0][v_idx]
    if t >= points[-1][t_idx]:
        return points[-1][v_idx]
    for i in range(1, len(points)):
        if t <= points[i][t_idx]:
            t0, t1 = points[i - 1][t_idx], points[i][t_idx]
            v0, v1 = points[i - 1][v_idx], points[i][v_idx]
            if t1 == t0:
                return v1
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return points[-1][v_idx]


def _arrival_time(points, frac):
    """到压时刻：压力从基线跃升 frac*(峰压-基线) 的时刻（线性插值）。

    曲线持平（无跃升）时返回 None——短射前几级熔体尚未到达该传感器。
    """
    if len(points) < 2:
        return None
    baseline = points[0][1]
    peak = max(p[1] for p in points)
    if peak <= baseline:
        return None
    target = baseline + frac * (peak - baseline)
    for i in range(1, len(points)):
        if points[i][1] >= target and points[i - 1][1] < target:
            t0, t1 = points[i - 1][0], points[i][0]
            p0, p1 = points[i - 1][1], points[i][1]
            if p1 == p0:
                return t1
            return t0 + (target - p0) * (t1 - t0) / (p1 - p0)
    return None


def _window_integral(points, lo, hi):
    """[lo, hi] 区间梯形积分；区间端点按分段线性插值补点。"""
    inside = [p for p in points if lo <= p[0] <= hi]
    if not inside:
        return 0.0
    seg = [pt for pt in points if lo <= pt[0] <= hi]
    seg = [(lo, _interp(points, lo))] + seg + [(hi, _interp(points, hi))]
    # 去重并保持升序（lo/hi 恰有采样点时插值点与采样点重合）
    out = []
    for pt in seg:
        if out and pt[0] == out[-1][0]:
            out[-1] = pt
        else:
            out.append(pt)
    return sum((out[i][0] - out[i - 1][0])
               * (out[i][1] + out[i - 1][1]) / 2.0
               for i in range(1, len(out)))


def _node_depths(tree_nodes):
    by_id = {n["node_id"]: n for n in tree_nodes}
    depth, frontier, d = {}, [n["node_id"] for n in tree_nodes
                              if n["parent"] is None], 0
    while frontier:
        nxt = []
        for nid in frontier:
            depth[nid] = d
            nxt.extend(by_id[nid]["children"])
        frontier, d = nxt, d + 1
    return depth, by_id


def _subtree_cavities(by_id, nid):
    cavs = list(by_id[nid]["feeds"])
    for ch in by_id[nid]["children"]:
        cavs.extend(_subtree_cavities(by_id, ch))
    return cavs


def _nearest_sensor_key(by_id, key, sensor_at):
    """子树内最靠近上游的传感器定位（BFS，节点自身先于子节点先于直连型腔）。"""
    kind, tid = key
    if kind == "cavity":
        return key if key in sensor_at else None
    queue = [tid]
    while queue:
        cur = queue.pop(0)
        if ("node", cur) in sensor_at:
            return ("node", cur)
        node = by_id[cur]
        for cid in node["feeds"]:
            if ("cavity", cid) in sensor_at:
                return ("cavity", cid)
        queue.extend(node["children"])
    return None


def pressure_review(payload, revisions, weight_metrics):
    """模内压力传播复核。

    输入：原始批次 payload、人工修订（改锚点/排坏点，附理由）、称重分析指标。
    输出冻结曲线、对齐参数、沿树传播指标、数据质量问题、与称重交叉判断后的
    最早受限分支结论及判断依据。status 取值：
      no_sensors / restricted / no_restriction /
      inconclusive_conflict / inconclusive_data_quality /
      inconclusive_insufficient_sensors
    """
    sensors = payload.get("sensors")
    if not sensors:
        return {"status": "no_sensors"}

    cfg = dict(DEFAULT_PRESSURE)
    cfg.update(payload.get("pressure_review") or {})
    today = datetime.now(timezone.utc).date()
    stages = sorted(payload["stages"], key=lambda s: s["stage"])
    stage_axis = [s["stage"] for s in stages]

    tree_nodes, _ = parse_runner_tree(payload.get("runner_tree"))
    depth, by_id = _node_depths(tree_nodes)
    root_id = next(nid for nid, d in depth.items() if d == 0)
    parent_of = {n["node_id"]: n["parent"] for n in tree_nodes}
    sensor_at = {(s["target_kind"], s["target_id"]): s["sensor_id"]
                 for s in sensors}
    sensor_info = {s["sensor_id"]: s for s in sensors}
    anchors_raw = payload.get("trigger_anchors") or {}

    revisions = revisions or []
    anchor_rev = [r for r in revisions if r["kind"] == "anchor_override"]
    badpoint_rev = [r for r in revisions if r["kind"] == "bad_point"]

    issues = []

    def add_issue(code, node, node_kind, stage, raw_interval, message):
        issues.append({"code": code, "node": node, "node_kind": node_kind,
                       "stage": stage,
                       "raw_interval": [round(x, 6) for x in raw_interval]
                       if raw_interval is not None else None,
                       "message": message})

    def target_node(kind, tid):
        """问题归属节点：型腔传感器归到直连它的流道节点，便于沿树定位。"""
        if kind == "node":
            return tid, "node"
        for n in tree_nodes:
            if tid in n["feeds"]:
                return n["node_id"], "node"
        return tid, "cavity"

    # ---- 校准有效期（修订无法消除，只能换传感器重测）
    for s in sensors:
        valid_until = parse_iso_date(s.get("calibration_valid_until"))
        if valid_until is not None and valid_until < today:
            nid, nk = target_node(s["target_kind"], s["target_id"])
            add_issue("CALIBRATION_EXPIRED", nid, nk, None, None,
                      "传感器 %s（%s %s）校准已于 %s 失效"
                      % (s["sensor_id"], s["target_kind"], s["target_id"],
                         s.get("calibration_valid_until")))

    # ---- 锚点采纳：原始锚点 + 人工改锚点修订（注明理由、保留原值）
    def adopted_anchor(sid, collector, stage_no):
        row = anchors_raw.get(str(stage_no), anchors_raw.get(stage_no)) or {}
        raw = row.get(collector)
        chosen = None
        for r in anchor_rev:
            if r["sensor_id"] != sid:
                continue
            if r["stage"] is not None and r["stage"] != stage_no:
                continue
            chosen = r
        if chosen is not None:
            return float(chosen["new_anchor"]), raw, chosen
        return (float(raw) if raw is not None else None), raw, None

    # ---- 逐级对齐：机台曲线锚 0；各采集器锚点逐帧对齐
    collectors = sorted({s.get("collector") or "default" for s in sensors})
    # 参考采集器：挂传感器最多者（多台采集器时以其触发时钟为基准）
    use_count = {c: 0 for c in collectors}
    for s in sensors:
        use_count[s.get("collector") or "default"] += 1
    ref_collector = sorted(collectors,
                           key=lambda c: (-use_count[c], c))[0]

    alignment = {}
    anchor_series = {c: [] for c in collectors}
    clock_block = False
    for s in stages:
        no = s["stage"]
        row = anchors_raw.get(str(no), anchors_raw.get(no)) or {}
        arow = {"__machine__": {"raw_anchor": 0.0, "adopted_anchor": 0.0,
                                "revised_by": None}}
        for c in collectors:
            raw = row.get(c)
            if raw is None:
                add_issue("CLOCK_ANCHOR_MISSING", c, "collector", no, None,
                          "第 %s 级采集器 %s 缺触发锚点，无法与机台时钟对齐"
                          % (no, c))
                clock_block = True
                arow[c] = {"raw_anchor": None, "adopted_anchor": 0.0,
                           "revised_by": None}
                continue
            # 采集器锚点修订以该采集器上任一传感器的修订为准（同级一致）
            rev = next((r for r in anchor_rev
                        if (r["stage"] in (None, no))
                        and (sensor_info[r["sensor_id"]].get("collector")
                             or "default") == c), None)
            adopted = float(rev["new_anchor"]) if rev else float(raw)
            arow[c] = {"raw_anchor": float(raw), "adopted_anchor": adopted,
                       "revised_by": rev["sensor_id"] if rev else None}
            anchor_series[c].append(adopted)
        alignment[no] = arow

    clock_residual = {}
    for c in collectors:
        series = anchor_series[c]
        if len(series) >= 2:
            residual = max(series) - min(series)
            clock_residual[c] = round(residual, 6)
            if residual > cfg["clock_residual_s"]:
                add_issue("CLOCK_RESIDUAL", c, "collector", None,
                          [round(min(series), 6), round(max(series), 6)],
                          "采集器 %s 触发锚点逐帧抖动 %.1f ms，超出 %.1f ms："
                          "多采集器传播延迟可能只是时钟不齐"
                          % (c, residual * 1000,
                             cfg["clock_residual_s"] * 1000))
                clock_block = True

    # ---- 逐级曲线：机台 + 各传感器（原始序列 → 采纳曲线：对齐/排坏点/超量程）
    machine_metrics = {}
    sensor_metrics = {s["sensor_id"]: {} for s in sensors}
    frozen_machine, frozen_sensors = {}, {}

    for s in stages:
        no = s["stage"]
        mc = [[float(t), float(p)] for t, p in s["pressure_curve"]]
        win_lo, win_hi = mc[0][0], mc[-1][0]
        m_arrival = _arrival_time(mc, cfg["arrival_fraction"])
        machine_metrics[no] = {
            "arrival_s": round(m_arrival, 6) if m_arrival is not None else None,
            "peak": round(max(p for _, p in mc), 4),
            "integral": round(_window_integral(mc, win_lo, win_hi), 4),
            "window_s": [round(win_lo, 6), round(win_hi, 6)],
        }
        frozen_machine[no] = {"unit": "machine_hydraulic", "anchor_s": 0.0,
                              "window_s": [round(win_lo, 6), round(win_hi, 6)],
                              "points": [[round(t, 6), round(p, 4)]
                                         for t, p in mc]}

    for s in stages:
        no = s["stage"]
        mc = [[float(t), float(p)] for t, p in s["pressure_curve"]]
        win_lo, win_hi = mc[0][0], mc[-1][0]
        raw_traces = s.get("mold_pressure") or {}
        for sens in sensors:
            sid = sens["sensor_id"]
            kind, tid = sens["target_kind"], sens["target_id"]
            nid, nk = target_node(kind, tid)
            collector = sens.get("collector") or "default"
            anchor, _, _ = adopted_anchor(sid, collector, no)
            max_p = sens["range"]["max_pressure"]
            frozen = {"collector": collector, "node": nid,
                      "target": [kind, tid],
                      "raw_window": None, "adopted_anchor": anchor,
                      "aligned_points": [], "removed": []}

            raw = raw_traces.get(sid)
            if raw is None:
                add_issue("SENSOR_MISSING_TRACE", nid, nk, no, None,
                          "第 %s 级传感器 %s（%s %s）缺压力序列"
                          % (no, sid, kind, tid))
                frozen_sensors.setdefault(sid, {})[no] = frozen
                sensor_metrics[sid][no] = None
                continue

            raw = [[float(t), float(p)] for t, p in raw]
            frozen["raw_window"] = [round(raw[0][0], 6),
                                    round(raw[-1][0], 6)]
            bad_iv = [(float(r["t_from"]), float(r["t_to"]), r["reason"])
                      for r in badpoint_rev
                      if r["sensor_id"] == sid
                      and (r["stage"] == no)]

            adopted = []
            for t, p in raw:
                cover = next((iv for iv in bad_iv if iv[0] <= t <= iv[1]), None)
                over = p > max_p
                if cover is not None:
                    frozen["removed"].append(
                        {"raw_t": round(t, 6), "pressure": round(p, 4),
                         "reason": "manual_bad_point", "detail": cover[2]})
                    continue
                if over:
                    add_issue("SENSOR_OVERRANGE", nid, nk, no, [t, t],
                              "第 %s 级传感器 %s（节点 %s）采样 %.4f 超量程 "
                              "%.4f，原始时标 %.4f s"
                              % (no, sid, nid, p, max_p, t))
                    frozen["removed"].append(
                        {"raw_t": round(t, 6), "pressure": round(p, 4),
                         "reason": "overrange", "detail": None})
                    continue
                adopted.append([t - (anchor or 0.0), p, t])

            for i in range(1, len(adopted)):
                gap = adopted[i][0] - adopted[i - 1][0]
                if gap > cfg["max_sample_gap_s"]:
                    add_issue("SAMPLE_GAP", nid, nk, no,
                              [adopted[i - 1][2], adopted[i][2]],
                              "第 %s 级传感器 %s（节点 %s）采样断档 %.1f ms，"
                              "原始区间 %.4f–%.4f s%s"
                              % (no, sid, nid, gap * 1000,
                                 adopted[i - 1][2], adopted[i][2],
                                 "（区间含人工排除坏点）" if bad_iv else ""))

            pts = [[t, p] for t, p, _ in adopted]
            frozen["aligned_points"] = [[round(t, 6), round(p, 4)]
                                        for t, p in pts]
            frozen_sensors.setdefault(sid, {})[no] = frozen

            if len(pts) < 2:
                sensor_metrics[sid][no] = None
                continue
            arrival = _arrival_time(pts, cfg["arrival_fraction"])
            sensor_metrics[sid][no] = {
                "arrival_s": round(arrival, 6) if arrival is not None else None,
                "peak": round(max(p for _, p in pts), 4),
                "integral": round(_window_integral(pts, win_lo, win_hi), 4),
                "samples": len(pts),
            }

    # ---- 沿流道树：父子传播延迟、压力衰减、注射区间积分
    def upstream_sensor_key(kind, tid):
        """最近的上游已挂接传感器；根以上为机台。"""
        cur = parent_of[tid] if kind == "node" else next(
            n["node_id"] for n in tree_nodes if tid in n["feeds"])
        while cur is not None:
            if ("node", cur) in sensor_at:
                return ("node", cur)
            cur = parent_of[cur]
        return ("machine", None)

    edges = []
    relations = []  # (child_key, parent_key, edge_kind)
    for sens in sensors:
        kind, tid = sens["target_kind"], sens["target_id"]
        pk = upstream_sensor_key(kind, tid)
        edge_kind = "sprue" if pk == ("machine", None) and depth.get(tid) == 0 \
            else ("gate" if kind == "cavity" else "runner")
        relations.append(((kind, tid), pk, edge_kind))

    def metric_of(key, no):
        if key == ("machine", None):
            return machine_metrics[no]
        sid = sensor_at.get(key)
        return sensor_metrics[sid].get(no) if sid else None

    for ck, pk, ek in relations:
        sid = sensor_at[ck]
        per_stage = []
        for no in stage_axis:
            cm, pm = metric_of(ck, no), metric_of(pk, no)
            if not cm or not pm or cm["arrival_s"] is None \
                    or pm["arrival_s"] is None:
                per_stage.append({"stage": no, "delay_s": None,
                                  "peak_attenuation": None,
                                  "integral_ratio": None})
                continue
            delay = cm["arrival_s"] - pm["arrival_s"]
            peak_att = (pm["peak"] - cm["peak"]) / pm["peak"] \
                if pm["peak"] else None
            integ_ratio = cm["integral"] / pm["integral"] \
                if pm["integral"] else None
            per_stage.append({
                "stage": no,
                "delay_s": round(delay, 6),
                "peak_attenuation": round(peak_att, 4)
                if peak_att is not None else None,
                "integral_ratio": round(integ_ratio, 4)
                if integ_ratio is not None else None,
            })
        edges.append({
            "from": "machine" if pk == ("machine", None) else "%s:%s" % pk,
            "to": "%s:%s" % ck, "edge_kind": ek, "sensor_id": sid,
            "node_depth": depth.get(ck[1], max(depth.values()) + 1),
            "per_stage": per_stage,
        })

    # ---- 分流节点：同级兄弟支延迟差/衰减差/积分差，逐支求最早受限
    splits = []
    split_evidence = []   # (earliest_stage, node_depth, node_id, slow_cavs, detail)
    for n in tree_nodes:
        branches = ([("node", c) for c in n["children"]] +
                    [("cavity", c) for c in n["feeds"]])
        if len(branches) < 2:
            continue
        reps = []
        for bk in branches:
            rk = _nearest_sensor_key(by_id, bk, sensor_at)
            reps.append((bk, rk))
        branch_rows = []
        restricted_stages = []
        for bk, rk in reps:
            cavs = [bk[1]] if bk[0] == "cavity" \
                else _subtree_cavities(by_id, bk[1])
            rdepth = (depth.get(bk[1], 0) + 1) if bk[0] == "node" \
                else depth.get(n["node_id"], 0) + 1
            stages_row = []
            pk = upstream_sensor_key("node", n["node_id"])
            for s in stages:
                no = s["stage"]
                pm = metric_of(pk, no)
                rm = metric_of(rk, no) if rk else None
                stages_row.append({
                    "stage": no,
                    "arrival_s": rm["arrival_s"] if rm else None,
                    "integral": rm["integral"] if rm else None,
                    "peak": rm["peak"] if rm else None,
                    "parent_arrival_s": pm["arrival_s"] if pm else None,
                })
            branch_rows.append({"branch": "%s:%s" % bk,
                                "sensor_id": sensor_at.get(rk),
                                "rep_depth": rdepth, "cavities": cavs,
                                "per_stage": stages_row})

        for i, s in enumerate(stages):
            no = s["stage"]
            live = [r for r in branch_rows
                    if r["per_stage"][i]["arrival_s"] is not None
                    and r["per_stage"][i]["parent_arrival_s"] is not None]
            if len(live) < 2:
                continue
            gaps = {r["branch"]:
                    r["per_stage"][i]["arrival_s"]
                    - r["per_stage"][i]["parent_arrival_s"]
                    for r in live}
            ints = [r["per_stage"][i]["integral"] for r in live
                    if r["per_stage"][i]["integral"]]
            int_med = _median(ints)
            depths_here = {r["branch"]: r["rep_depth"] for r in live}
            equal_depth = len(set(depths_here.values())) == 1
            peaks = [r["per_stage"][i]["peak"] for r in live
                     if r["per_stage"][i]["peak"]]
            peak_max = max(peaks) if peaks else None
            slow_branch = max(live, key=lambda r: gaps[r["branch"]])
            others = [r for r in live if r is not slow_branch]
            delay_gap = gaps[slow_branch["branch"]] \
                - _median([gaps[r["branch"]] for r in others])
            int_loss = (1 - slow_branch["per_stage"][i]["integral"]
                        / int_med) if int_med else None
            att_loss = None
            if equal_depth and peak_max:
                sp = slow_branch["per_stage"][i]["peak"]
                att_loss = (peak_max - sp) / peak_max
            restricted = delay_gap > cfg["delay_tolerance_s"] and (
                (int_loss is not None and int_loss > cfg["integral_tolerance"])
                or (att_loss is not None
                    and att_loss > cfg["attenuation_tolerance"]))
            for r in branch_rows:
                r["per_stage"][i]["arrival_gap_s"] = round(
                    gaps.get(r["branch"], 0.0), 6) if r in live else None
                r["per_stage"][i]["restricted_evidence"] = bool(
                    restricted and r is slow_branch)
            if restricted:
                restricted_stages.append({
                    "stage": no,
                    "delay_gap_s": round(delay_gap, 6),
                    "integral_loss": round(int_loss, 4)
                    if int_loss is not None else None,
                    "peak_attenuation_loss": round(att_loss, 4)
                    if att_loss is not None else None,
                    "slow_branch": slow_branch["branch"],
                    "slow_kind": "gate" if slow_branch["branch"]
                    .startswith("cavity:") else "runner",
                })

        if restricted_stages:
            earliest = restricted_stages[0]["stage"]
            splits.append({"node_id": n["node_id"],
                           "node_depth": depth[n["node_id"]],
                           "restricted": True,
                           "earliest_stage": earliest,
                           "evidence": restricted_stages,
                           "branches": branch_rows})
            slow = restricted_stages[0]
            slow_row = next(r for r in branch_rows
                            if r["branch"] == slow["slow_branch"])
            split_evidence.append(
                (earliest, depth[n["node_id"]], n["node_id"],
                 list(slow_row["cavities"]), slow, slow_row))
        else:
            splits.append({"node_id": n["node_id"],
                           "node_depth": depth[n["node_id"]],
                           "restricted": False,
                           "branches": branch_rows})

    # ---- 主流道：根节点到压相对机台起射的延迟（逐级代表值取中位数）
    sprue = None
    root_key = ("node", root_id)
    if root_key in sensor_at:
        root_sid = sensor_at[root_key]
        root_delays = []
        for no in stage_axis:
            rm, mm = sensor_metrics[root_sid].get(no), machine_metrics[no]
            if rm and mm and rm["arrival_s"] is not None \
                    and mm["arrival_s"] is not None:
                root_delays.append((no, rm["arrival_s"] - mm["arrival_s"]))
        if len(root_delays) >= 2:
            med = _median([d for _, d in root_delays])
            sprue = {
                "root_node": root_id, "sensor_id": root_sid,
                "delays_s": [{"stage": no, "delay_s": round(d, 6)}
                             for no, d in root_delays],
                "median_delay_s": round(med, 6),
                "restricted": med > cfg["delay_tolerance_s"],
            }

    # ---- 证据路径上的数据质量问题：时钟问题全局一票否决；
    #      节点/型腔问题只否决使用该路径得到的结论
    def issue_nodes():
        s = set()
        for iss in issues:
            if iss["node_kind"] in ("node", "cavity"):
                s.add((iss["node_kind"], iss["node"]))
        return s

    blocked_nodes = issue_nodes()

    def path_blocked(node_id):
        """节点本身或其上游/直连型腔存在未消除的质量问题。"""
        if ("node", node_id) in blocked_nodes:
            return True
        for cid in _subtree_cavities(by_id, node_id):
            if ("cavity", cid) in blocked_nodes:
                return True
        cur = parent_of[node_id]
        while cur is not None:
            if ("node", cur) in blocked_nodes:
                return True
            cur = parent_of[cur]
        return False

    candidate = None
    blocked_by_quality = clock_block
    if sprue and sprue["restricted"]:
        if path_blocked(root_id):
            blocked_by_quality = True
        candidate = {"segment": "sprue", "node_id": root_id,
                     "edge_kind": "sprue",
                     "earliest_stage": sprue["delays_s"][0]["stage"],
                     "median_delay_s": sprue["median_delay_s"],
                     "cavities": _subtree_cavities(by_id, root_id)}
    split_evidence.sort(key=lambda e: (e[0], e[1]))
    split_candidates = []
    for est, dd, nid, slow_cavs, ev, slow_row in split_evidence:
        affected = path_blocked(nid) or any(
            ("cavity", c) in blocked_nodes for c in slow_cavs)
        if affected:
            blocked_by_quality = True
        split_candidates.append((nid, est, ev, slow_cavs, slow_row, affected))

    # ---- 与逐腔充填比例、进料滞后交叉判断
    fill = weight_metrics["fill_ratio"]
    lag = weight_metrics["feed_lag_stages"]
    limit = weight_metrics["imbalance_limit"]
    cross_check = {"verdict": "none", "details": []}

    primary = None
    if candidate:
        primary = candidate
    elif split_candidates:
        nid, est, ev, slow_cavs, slow_row, _ = split_candidates[0]
        primary = {"segment": ev["slow_kind"], "node_id": nid,
                   "edge_kind": ev["slow_kind"],
                   "earliest_stage": est,
                   "delay_gap_s": ev["delay_gap_s"],
                   "integral_loss": ev["integral_loss"],
                   "peak_attenuation_loss": ev["peak_attenuation_loss"],
                   "slow_branch": ev["slow_branch"],
                   "cavities": sorted(slow_cavs)}

    if primary:
        slow_cavs = primary["cavities"]
        all_cavs = list(fill)
        fast_cavs = [c for c in all_cavs if c not in slow_cavs]
        slow_mean = sum(fill[c] for c in slow_cavs) / len(slow_cavs)
        fast_mean = (sum(fill[c] for c in fast_cavs) / len(fast_cavs)
                     if fast_cavs else slow_mean)
        slow_lag = [lag.get(c) for c in slow_cavs]
        lag_vals = [v for v in slow_lag if v is not None]
        lag_behind = bool(lag_vals) and max(lag_vals) >= 1
        detail = {"node_id": primary["node_id"],
                  "slow_cavities": sorted(slow_cavs),
                  "slow_mean_fill": round(slow_mean, 4),
                  "other_mean_fill": round(fast_mean, 4),
                  "slow_feed_lag_stages": slow_lag,
                  "weight_fill_lower": slow_mean < fast_mean - limit,
                  "weight_lag_behind": lag_behind}
        if slow_mean > fast_mean + limit or (
                lag_vals and max(lag_vals) <= 0
                and fast_cavs and any((lag.get(c) or 0) >= 1
                                      for c in fast_cavs)):
            # 压力显示受限的分支反而充得更满/更早进料：证据相反，不给结论
            cross_check = {"verdict": "conflicts", "details": [detail]}
        elif detail["weight_fill_lower"] or lag_behind:
            cross_check = {"verdict": "agrees", "details": [detail]}
        else:
            cross_check = {"verdict": "neutral", "details": [detail]}

    # ---- 汇总结论
    active_splits = [{"node_id": nid, "earliest_stage": est,
                      "slow_branch": ev["slow_branch"],
                      "slow_kind": ev["slow_kind"],
                      "delay_gap_s": ev["delay_gap_s"],
                      "integral_loss": ev["integral_loss"],
                      "quality_blocked": aff}
                     for nid, est, ev, _, _, aff in split_candidates]

    if primary is None and sprue is None and not split_candidates:
        comparable = len(sensors) >= 2 or (sprue is not None)
        status = "inconclusive_insufficient_sensors" if not comparable \
            else "no_restriction"
        if status == "no_restriction" and issues:
            status = "no_restriction_with_quality_notes"
    elif blocked_by_quality:
        status = "inconclusive_data_quality"
    elif cross_check["verdict"] == "conflicts":
        status = "inconclusive_conflict"
    else:
        status = "restricted"

    revision_history = [{
        "id": r.get("id"), "kind": r["kind"], "sensor_id": r["sensor_id"],
        "stage": r["stage"], "collector": r.get("collector"),
        "t_from": r.get("t_from"), "t_to": r.get("t_to"),
        "old_anchor": r.get("old_anchor"), "new_anchor": r.get("new_anchor"),
        "reason": r["reason"], "created_at": r.get("created_at"),
        "derived_from": "raw_submission",
    } for r in revisions]

    judgment_basis = {
        "arrival_fraction": cfg["arrival_fraction"],
        "delay_tolerance_s": cfg["delay_tolerance_s"],
        "attenuation_tolerance": cfg["attenuation_tolerance"],
        "integral_tolerance": cfg["integral_tolerance"],
        "clock_residual_s": cfg["clock_residual_s"],
        "max_sample_gap_s": cfg["max_sample_gap_s"],
        "reference_collector": ref_collector,
        "injection_window_s": {
            no: machine_metrics[no]["window_s"] for no in stage_axis},
        "rule": "兄弟支到压延迟差超限时，还须有积分损失或峰压衰减同向佐证"
                "才判受限；主流道延迟看根节点相对机台；压力与称重相反则无结论",
    }

    return {
        "status": status,
        "config": {k: cfg[k] for k in DEFAULT_PRESSURE},
        "reference_collector": ref_collector,
        "alignment": {no: alignment[no] for no in stage_axis},
        "clock_residual_s": clock_residual,
        "quality_issues": issues,
        "frozen_curves": {"machine": frozen_machine,
                          "sensors": frozen_sensors},
        "sensor_metrics": sensor_metrics,
        "edges": edges,
        "splits": splits,
        "sprue_delay": sprue,
        "restriction": {
            "status": status,
            "primary": primary,
            "candidate_branches": active_splits,
            "cross_check": cross_check,
        },
        "revision_history": revision_history,
        "judgment_basis": judgment_basis,
    }


# ---------------------------------------------------------------- HTTP 层

class App:
    def __init__(self):
        init_db()

    # ---- 工具
    @staticmethod
    def _json(start_response, code, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        start_response("%d %s" % (code, {
            200: "OK", 201: "Created", 400: "Bad Request",
            404: "Not Found", 409: "Conflict", 500: "Internal Server Error",
        }[code]), [("Content-Type", "application/json; charset=utf-8"),
                   ("Content-Length", str(len(body)))])
        return [body]

    @staticmethod
    def _read(environ):
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            size = 0
        return environ["wsgi.input"].read(size) if size else b""

    def _batch_or_404(self, batch_id, start_response):
        conn = get_db()
        row = conn.execute("SELECT * FROM batches WHERE batch_id=?",
                           (batch_id,)).fetchone()
        conn.close()
        if not row:
            return None, self._json(start_response, 404,
                                    {"error": "批次不存在：%s" % batch_id})
        return row, None

    # ---- 路由
    def __call__(self, environ, start_response):
        try:
            method = environ["REQUEST_METHOD"]
            path = environ["PATH_INFO"].rstrip("/") or "/"
            parts = [p for p in path.split("/") if p]

            if method == "POST" and parts == ["batches"]:
                return self.create_batch(environ, start_response)
            if method == "GET" and len(parts) == 2 and parts[0] == "batches":
                return self.get_batch(parts[1], start_response)
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" \
                    and parts[2] == "report":
                return self.get_report(parts[1], environ, start_response)
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" \
                    and parts[2] == "exclusions":
                return self.add_exclusion(parts[1], environ, start_response)
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" \
                    and parts[2] == "plans":
                return self.add_plan(parts[1], environ, start_response)
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" \
                    and parts[2] == "pressure-revisions":
                return self.add_pressure_revision(parts[1], environ,
                                                  start_response)
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" \
                    and parts[2] == "diff":
                return self.diff(parts[1], environ, start_response)
            return self._json(start_response, 404, {"error": "未知路由"})
        except Exception:  # 兜底，保证服务不挂
            traceback.print_exc()
            return self._json(start_response, 500,
                              {"error": "服务器内部错误"})

    # ---- 端点
    def create_batch(self, environ, start_response):
        try:
            payload = json.loads(self._read(environ) or b"{}")
        except json.JSONDecodeError as e:
            return self._json(start_response, 400,
                              {"error": "JSON 解析失败：%s" % e})
        batch_id = payload.get("batch_id")
        if not batch_id:
            return self._json(start_response, 400,
                              {"error": "缺少 batch_id"})

        errors = validate_batch(payload)
        if errors:
            # 暂不建议改模：仅返回问题定位，不入库
            return self._json(start_response, 400, {
                "accepted": False,
                "recommendation": "数据未通过校核，请先修正试验记录，暂不建议改模",
                "errors": errors,
            })

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO batches (batch_id, payload, created_at) "
                "VALUES (?,?,?)",
                (batch_id, json.dumps(payload, ensure_ascii=False), now_iso()))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return self._json(start_response, 409,
                              {"error": "批次已存在：%s" % batch_id})
        conn.close()
        return self._json(start_response, 201,
                          {"accepted": True, "batch_id": batch_id,
                           "report_url": "/batches/%s/report" % batch_id})

    def get_batch(self, batch_id, start_response):
        row, resp = self._batch_or_404(batch_id, start_response)
        if resp:
            return resp
        return self._json(start_response, 200, json.loads(row["payload"]))

    def _snapshot(self, batch_id):
        """可复算批次快照：复算输入 + 实际采用的记录 + 计算指标 + 人工处理。"""
        conn = get_db()
        row = conn.execute("SELECT * FROM batches WHERE batch_id=?",
                           (batch_id,)).fetchone()
        if not row:
            conn.close()
            return None
        excl = [dict(r) for r in conn.execute(
            "SELECT stage, cavity_id, reason, created_at FROM exclusions "
            "WHERE batch_id=? ORDER BY id", (batch_id,))]
        plans = [dict(r) for r in conn.execute(
            "SELECT id, note, gate_changes, created_at FROM plans "
            "WHERE batch_id=? ORDER BY id", (batch_id,))]
        prev = [dict(r) for r in conn.execute(
            "SELECT id, kind, sensor_id, stage, collector, t_from, t_to, "
            "old_anchor, new_anchor, reason, created_at "
            "FROM pressure_revisions WHERE batch_id=? ORDER BY id",
            (batch_id,))]
        conn.close()
        for pl in plans:
            pl["gate_changes"] = json.loads(pl["gate_changes"])

        payload = json.loads(row["payload"])
        metrics = analyze(payload, excl, prev)
        return {
            "batch_id": batch_id,
            "created_at": row["created_at"],
            "recompute_inputs": payload,         # 原始输入，可直接复算
            "adopted_records": {                 # 本次计算实际采用的记录
                "exclusions": [{"stage": e["stage"],
                                "cavity_id": e["cavity_id"]} for e in excl],
                "gate_plans": plans,
                "pressure_revisions": [
                    {"id": r["id"], "kind": r["kind"],
                     "sensor_id": r["sensor_id"], "stage": r["stage"],
                     "reason": r["reason"]} for r in prev],
            },
            "metrics": metrics,
            "manual_handling": {                 # 人工处理流水（含理由与时间）
                "exclusions": excl,
                "follow_up_plans": plans,
                "pressure_revisions": prev,
            },
        }

    def get_report(self, batch_id, environ, start_response):
        snap = self._snapshot(batch_id)
        if snap is None:
            return self._json(start_response, 404,
                              {"error": "批次不存在：%s" % batch_id})
        return self._json(start_response, 200, snap)

    def add_exclusion(self, batch_id, environ, start_response):
        row, resp = self._batch_or_404(batch_id, start_response)
        if resp:
            return resp
        try:
            body = json.loads(self._read(environ) or b"{}")
        except json.JSONDecodeError as e:
            return self._json(start_response, 400,
                              {"error": "JSON 解析失败：%s" % e})
        stage, cid, reason = body.get("stage"), body.get("cavity_id"), \
            (body.get("reason") or "").strip()
        if stage is None or not cid or not reason:
            return self._json(start_response, 400,
                              {"error": "剔除称量必须提供 stage、cavity_id 与理由 reason"})
        payload = json.loads(row["payload"])
        known = {c["cavity_id"] for c in payload["cavities"]}
        if cid not in known:
            return self._json(start_response, 400,
                              {"error": "型腔不存在：%s" % cid})
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO exclusions (batch_id, stage, cavity_id, reason,"
                " created_at) VALUES (?,?,?,?,?)",
                (batch_id, stage, cid, reason, now_iso()))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return self._json(start_response, 409,
                              {"error": "该称量点已被剔除过"})
        conn.close()
        return self._json(start_response, 201,
                          {"excluded": True, "batch_id": batch_id,
                           "stage": stage, "cavity_id": cid})

    def add_plan(self, batch_id, environ, start_response):
        row, resp = self._batch_or_404(batch_id, start_response)
        if resp:
            return resp
        try:
            body = json.loads(self._read(environ) or b"{}")
        except json.JSONDecodeError as e:
            return self._json(start_response, 400,
                              {"error": "JSON 解析失败：%s" % e})
        changes = body.get("gate_changes") or []
        payload = json.loads(row["payload"])
        known = {c["cavity_id"] for c in payload["cavities"]}
        for ch in changes:
            if ch.get("cavity_id") not in known:
                return self._json(start_response, 400,
                                  {"error": "型腔不存在：%s" % ch.get("cavity_id")})
            if not isinstance(ch.get("gate_reduction_mm"), (int, float)):
                return self._json(start_response, 400,
                                  {"error": "gate_reduction_mm 必须为数值"})
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO plans (batch_id, note, gate_changes, created_at) "
            "VALUES (?,?,?,?)",
            (batch_id, body.get("note", ""),
             json.dumps(changes, ensure_ascii=False), now_iso()))
        conn.commit()
        plan_id = cur.lastrowid
        conn.close()
        return self._json(start_response, 201,
                          {"plan_id": plan_id, "batch_id": batch_id,
                           "gate_changes": changes})

    def add_pressure_revision(self, batch_id, environ, start_response):
        """人工修订模内压力证据：改触发锚点或排除坏点，须附理由。

        锚点修订按采集器逐帧派生（锚点是采集器级时钟量）；坏点排除按
        传感器+级次+原始时标区间。每次修订都作为独立派生记录留痕，
        复算时在原始冻结曲线上叠加修订链，原始记录永不被覆盖。
        """
        row, resp = self._batch_or_404(batch_id, start_response)
        if resp:
            return resp
        try:
            body = json.loads(self._read(environ) or b"{}")
        except json.JSONDecodeError as e:
            return self._json(start_response, 400,
                              {"error": "JSON 解析失败：%s" % e})
        kind = body.get("kind")
        sid = body.get("sensor_id")
        reason = (body.get("reason") or "").strip()
        stage = body.get("stage")
        if kind not in ("anchor_override", "bad_point") or not sid \
                or not reason:
            return self._json(start_response, 400,
                              {"error": "必须提供 kind(anchor_override/"
                                        "bad_point)、sensor_id 与理由 reason"})
        payload = json.loads(row["payload"])
        known = {s["sensor_id"]: s for s in (payload.get("sensors") or [])}
        if sid not in known:
            return self._json(start_response, 400,
                              {"error": "传感器不存在：%s" % sid})
        if stage is not None and stage not in {s["stage"]
                                               for s in payload["stages"]}:
            return self._json(start_response, 400,
                              {"error": "级次不存在：%s" % stage})
        collector = known[sid].get("collector") or "default"
        created = []
        conn = get_db()
        if kind == "anchor_override":
            new_anchor = body.get("new_anchor")
            if not isinstance(new_anchor, (int, float)):
                conn.close()
                return self._json(start_response, 400,
                                  {"error": "new_anchor 必须为数值（秒）"})
            anchors = payload.get("trigger_anchors") or {}
            target_stages = [s["stage"] for s in payload["stages"]] \
                if stage is None else [stage]
            for no in target_stages:
                arow = anchors.get(str(no), anchors.get(no)) or {}
                old_anchor = arow.get(collector)
                if old_anchor is None:
                    continue
                cur = conn.execute(
                    "INSERT INTO pressure_revisions (batch_id, kind, "
                    "sensor_id, stage, collector, t_from, t_to, old_anchor, "
                    "new_anchor, reason, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, kind, sid, no, collector, None, None,
                     float(old_anchor), float(new_anchor), reason, now_iso()))
                created.append({"id": cur.lastrowid, "stage": no,
                                "collector": collector,
                                "old_anchor": old_anchor,
                                "new_anchor": new_anchor})
        else:
            t_from, t_to = body.get("t_from"), body.get("t_to")
            if not isinstance(t_from, (int, float)) \
                    or not isinstance(t_to, (int, float)) or t_to < t_from:
                conn.close()
                return self._json(start_response, 400,
                                  {"error": "bad_point 必须提供有效原始时标"
                                            "区间 t_from <= t_to（秒）"})
            if stage is None:
                conn.close()
                return self._json(start_response, 400,
                                  {"error": "排除坏点必须指定 stage"})
            cur = conn.execute(
                "INSERT INTO pressure_revisions (batch_id, kind, sensor_id, "
                "stage, collector, t_from, t_to, old_anchor, new_anchor, "
                "reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (batch_id, kind, sid, stage, collector,
                 float(t_from), float(t_to), None, None, reason, now_iso()))
            created.append({"id": cur.lastrowid, "stage": stage,
                            "t_from": float(t_from), "t_to": float(t_to)})
        conn.commit()
        conn.close()
        return self._json(start_response, 201,
                          {"revised": True, "batch_id": batch_id, "kind": kind,
                           "sensor_id": sid, "derived_records": created})

    def diff(self, batch_id, environ, start_response):
        """批次差异：?other=<batch_id>。

        响应携带两批次的完整快照（复算输入、采用记录、指标、人工处理），
        并单列两批次各自采用的称量剔除与浇口方案，便于独立追溯。
        """
        from urllib.parse import parse_qs
        other = (parse_qs(environ.get("QUERY_STRING", ""))
                 .get("other", [None])[0])
        if not other:
            return self._json(start_response, 400,
                              {"error": "缺少查询参数 other=<batch_id>"})
        snaps = {}
        for bid in (batch_id, other):
            snap = self._snapshot(bid)
            if snap is None:
                return self._json(start_response, 404,
                                  {"error": "批次不存在：%s" % bid})
            snaps[bid] = snap

        out = {
            "batches": [batch_id, other],
            "fill_ratio_delta": {},
            "earliest_flow_imbalance": {},
            "earliest_branch_imbalance": {},
            "pressure_review": {},       # 各批压力复核（含冻结曲线/对齐/依据）
            "pressure_arrival_delta": {},
            "adopted_records": {},      # 两批次各自采用的剔除与浇口方案
            "snapshots": snaps,         # 完整可复算快照
        }
        fills = {}
        for bid, snap in snaps.items():
            m = snap["metrics"]
            fills[bid] = m["fill_ratio"]
            out["earliest_flow_imbalance"][bid] = m["earliest_flow_imbalance"]
            out["earliest_branch_imbalance"][bid] = \
                m["earliest_branch_imbalance"]
            out["adopted_records"][bid] = snap["adopted_records"]
            pr = m["pressure_review"]
            if pr.get("status") != "no_sensors":
                out["pressure_review"][bid] = {
                    "status": pr["status"],
                    "restriction": pr["restriction"],
                    "clock_residual_s": pr["clock_residual_s"],
                    "alignment": pr["alignment"],
                    "judgment_basis": pr["judgment_basis"],
                    "frozen_curves": pr["frozen_curves"],
                    "quality_issues": pr["quality_issues"],
                    "revision_history": pr["revision_history"],
                }
        # 到压时刻差异（同传感器同列两级，判断修模后受限支是否缓解）
        reviews = {bid: snaps[bid]["metrics"]["pressure_review"]
                   for bid in snaps}
        sensor_ids = set()
        for pr in reviews.values():
            sensor_ids.update(pr.get("sensor_metrics", {}))
        for sid in sorted(sensor_ids):
            row = {}
            for bid in (batch_id, other):
                sm = reviews[bid].get("sensor_metrics", {}).get(sid)
                if sm:
                    row[bid] = {no: (v["arrival_s"] if v else None)
                                for no, v in sm.items()}
            if row:
                out["pressure_arrival_delta"][sid] = row
        for cid in fills[batch_id]:
            if cid in fills[other]:
                out["fill_ratio_delta"][cid] = round(
                    fills[batch_id][cid] - fills[other][cid], 4)
        return self._json(start_response, 200, out)


def main():
    app = App()
    port = 8000
    print("短射平衡试验服务已启动：http://127.0.0.1:%d" % port)
    make_server("127.0.0.1", port, app).serve_forever()


if __name__ == "__main__":
    main()
