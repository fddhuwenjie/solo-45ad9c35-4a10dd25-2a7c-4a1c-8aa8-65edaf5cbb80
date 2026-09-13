#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""短射平衡试验 HTTP 服务（仅标准库 wsgiref / json / sqlite3）。

接收多腔注塑短射试验数据：流道树、型腔容积、分级螺杆行程、压力曲线、
逐腔重量、秤校验值与失衡限值；还原各级物料去向，核对物料账，输出每腔
充填比例、进料滞后、级间增量、最早分流异常点，并区分连续扩大偏差与
孤立称量噪声。支持人工剔除称量（须附理由）与登记浇口修整另起方案。
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
        """
    )
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 校验

def _err(stage, cavity, code, message):
    return {"stage": stage, "cavity": cavity, "code": code, "message": message}


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

    # 流道树引用的型腔必须存在
    runner = p.get("runner_tree") or {}
    for leaf in runner.get("feeds", []):
        cid = leaf.get("cavity_id")
        if cid and cid not in seen:
            errors.append(_err(None, cid, "UNKNOWN_CAVITY",
                               "流道树指向未登记的型腔：%s" % cid))

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


# ---------------------------------------------------------------- 分析引擎

def analyze(p, exclusions):
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

    return {
        "stage_axis": [s["stage"] for s in stages],
        "fill_ratio": fill_ratio,
        "feed_lag_stages": feed_lag,
        "stage_increments_g": increments,
        "earliest_flow_imbalance": anomaly,
        "deviation_assessment": deviation_type,
        "material_balance": material,
        "imbalance_limit": limit,
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

    def get_report(self, batch_id, environ, start_response):
        row, resp = self._batch_or_404(batch_id, start_response)
        if resp:
            return resp
        conn = get_db()
        excl = [dict(r) for r in conn.execute(
            "SELECT stage, cavity_id, reason, created_at FROM exclusions "
            "WHERE batch_id=? ORDER BY id", (batch_id,))]
        plans = [dict(r) for r in conn.execute(
            "SELECT id, note, gate_changes, created_at FROM plans "
            "WHERE batch_id=? ORDER BY id", (batch_id,))]
        conn.close()
        for pl in plans:
            pl["gate_changes"] = json.loads(pl["gate_changes"])

        payload = json.loads(row["payload"])
        metrics = analyze(payload, excl)
        return self._json(start_response, 200, {
            "batch_id": batch_id,
            "created_at": row["created_at"],
            "metrics": metrics,
            "manual_handling": {
                "exclusions": excl,          # 采用记录：被剔除的称量及理由
                "follow_up_plans": plans,    # 浇口修整后另起的方案
            },
        })

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

    def diff(self, batch_id, environ, start_response):
        """批次差异：?other=<batch_id>，对比两批次的末级充填与异常点。"""
        from urllib.parse import parse_qs
        other = (parse_qs(environ.get("QUERY_STRING", ""))
                 .get("other", [None])[0])
        if not other:
            return self._json(start_response, 400,
                              {"error": "缺少查询参数 other=<batch_id>"})
        rows = {}
        for bid in (batch_id, other):
            row, resp = self._batch_or_404(bid, start_response)
            if resp:
                return resp
            rows[bid] = row
        out = {"batches": [batch_id, other], "fill_ratio_delta": {},
               "earliest_flow_imbalance": {}}
        fills = {}
        for bid, row in rows.items():
            conn = get_db()
            excl = [dict(r) for r in conn.execute(
                "SELECT stage, cavity_id, reason, created_at FROM exclusions"
                " WHERE batch_id=?", (bid,))]
            conn.close()
            m = analyze(json.loads(row["payload"]), excl)
            fills[bid] = m["fill_ratio"]
            out["earliest_flow_imbalance"][bid] = m["earliest_flow_imbalance"]
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
