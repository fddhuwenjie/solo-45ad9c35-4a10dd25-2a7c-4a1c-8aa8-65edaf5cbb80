#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shortshot_server 压力复核回归（纯 Python，直接调 WSGI 应用，临时 SQLite）。

补充 test_shortshot.py 未锁定的边界，不改 HTTP 契约、SQLite 结构与响应字段：

  1. _window_integral：采样点分布在窗口两侧且窗内无离散点的恒压/斜坡曲线。
     旧版“窗内（含边界）无采样点即提前返回 0”的缺陷在旧用例下也能通过
     （旧用例的采样点都落在窗口边界或窗内），这里用两侧夹窗用例直接锁定；
     另测窗口部分相交、完全不相交与退化情形。
  2. clock_residual_s / max_sample_gap_s 的 1e-9 容差：构造等于、仅低于、
     仅高于限值的批次，断言问题代码、原始区间与压力结论。等于限值时浮点
     残差实际略高于限值（如 0.0050000000000007816 > 0.005），没有 1e-9
     容差就会误报，这些用例正是锁定该容差。
  3. 同采集器锚点修订：alignment 与三条冻结曲线必须采用同一锚点。
"""

import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shortshot_server as srv

# 临时 SQLite 文件：不触碰仓库中的 shortshot.db / test_shortshot.db
TMPDIR = tempfile.mkdtemp(prefix="shortshot_regression_")
srv.DB_PATH = os.path.join(TMPDIR, "regression.db")
app = srv.App()

DENSITY = 0.75  # g/cm3，例如 PP 熔体近似


def call(method, path, body=None):
    status_headers = {}

    def start_response(status, headers):
        status_headers["status"] = status

    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path.split("?")[0],
        "QUERY_STRING": path.split("?")[1] if "?" in path else "",
        "wsgi.input": io.BytesIO(json.dumps(body).encode() if body else b""),
        "CONTENT_LENGTH": str(len(json.dumps(body).encode()) if body else 0),
    }
    chunks = app(env, start_response)
    return int(status_headers["status"].split()[0]), \
        json.loads(b"".join(chunks).decode())


failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)


# ----------------------------------------------------------------
# 1. _window_integral：采样点在窗口两侧、窗内无离散点的插值面积
# ----------------------------------------------------------------
# 以下用例在旧版实现（窗内无采样点即返回 0）下必然失败
check("恒压曲线·采样点仅在窗口两侧",
      abs(srv._window_integral([[0.0, 10.0], [1.0, 10.0]], 0.25, 0.75) - 5.0)
      < 1e-9)
check("恒压曲线·窗口夹在中间采样段",
      abs(srv._window_integral(
          [[0.0, 10.0], [0.2, 10.0], [0.8, 10.0], [1.0, 10.0]],
          0.3, 0.7) - 4.0) < 1e-9)
check("斜坡曲线·采样点仅在窗口两侧",
      abs(srv._window_integral([[0.0, 0.0], [1.0, 10.0]], 0.2, 0.8) - 3.0)
      < 1e-9)
check("折线斜坡·窗内无点按端点插值",
      abs(srv._window_integral([[0.0, 0.0], [0.4, 8.0], [1.0, 10.0]],
                               0.5, 0.9) - 3.6) < 1e-9)

# 窗口部分相交：越出定义域的部分按端点值（钳制）延长
check("窗口左越界·部分相交",
      abs(srv._window_integral([[0.2, 4.0], [0.8, 10.0]], 0.0, 0.5) - 2.45)
      < 1e-9)
check("窗口右越界·部分相交",
      abs(srv._window_integral([[0.2, 4.0], [0.8, 10.0]], 0.5, 1.0) - 4.55)
      < 1e-9)

# 窗口完全不相交（含与定义域端点相切）积分为 0
check("窗口完全在定义域左侧",
      srv._window_integral([[0.5, 10.0], [1.0, 10.0]], 0.0, 0.4) == 0.0)
check("窗口完全在定义域右侧",
      srv._window_integral([[0.0, 10.0], [0.5, 10.0]], 0.6, 1.0) == 0.0)
check("窗口与定义域左端相切",
      srv._window_integral([[0.5, 10.0], [1.0, 10.0]], 0.0, 0.5) == 0.0)
check("窗口与定义域右端相切",
      srv._window_integral([[0.0, 10.0], [0.5, 10.0]], 0.5, 1.0) == 0.0)

# 退化情形
check("空曲线积分为 0", srv._window_integral([], 0.0, 1.0) == 0.0)
check("窗口宽度为 0 积分为 0",
      srv._window_integral([[0.0, 10.0], [1.0, 10.0]], 0.5, 0.5) == 0.0)
check("窗口倒置积分为 0",
      srv._window_integral([[0.0, 10.0], [1.0, 10.0]], 0.8, 0.2) == 0.0)
check("单点曲线按恒定外推积分",
      abs(srv._window_integral([[0.5, 10.0]], 0.0, 1.0) - 10.0) < 1e-9)


# ----------------------------------------------------------------
# 压力批次构造：R 支进料滞后，压力应指认 root 节点 node:R 支受限
# ----------------------------------------------------------------
PRESSURE_TREE = {
    "root": "root",
    "nodes": [
        {"node_id": "root", "children": ["L", "R"]},
        {"node_id": "L", "feeds": ["C1", "C2"]},
        {"node_id": "R", "feeds": ["C3", "C4"]},
    ],
}
PRESSURE_SENSORS = [
    {"sensor_id": "SROOT", "target_kind": "node", "target_id": "root",
     "collector": "A", "range": {"max_pressure": 100.0},
     "calibration_valid_until": "2030-01-01"},
    {"sensor_id": "SL", "target_kind": "node", "target_id": "L",
     "collector": "A", "range": {"max_pressure": 100.0},
     "calibration_valid_until": "2030-01-01"},
    {"sensor_id": "SR", "target_kind": "node", "target_id": "R",
     "collector": "A", "range": {"max_pressure": 100.0},
     "calibration_valid_until": "2030-01-01"},
]
# 采样间隔 ≤50 ms，避免触发采样断档；ROOT 0.067 到压、L 0.097、R 0.140
P_MACHINE = [[0.00, 20], [0.05, 30], [0.10, 55], [0.15, 48],
             [0.20, 42], [0.25, 40], [0.30, 38]]
P_ROOT = [[10.00, 20], [10.05, 22], [10.10, 28], [10.15, 50],
          [10.20, 60], [10.25, 50], [10.30, 42]]
P_LEFT = [[10.00, 20], [10.05, 21], [10.10, 24], [10.15, 40],
          [10.20, 52], [10.25, 58], [10.30, 44]]
P_RIGHT = [[10.00, 20], [10.05, 20], [10.10, 21], [10.15, 22],
           [10.20, 24], [10.25, 30], [10.28, 38]]


def root_curve_with_tail_gap(gap):
    """SROOT 曲线：上升沿与 P_ROOT 相同（到压时刻不变），
    尾部 10.20 之后按指定间隔展开，用于采样断档边界测试。"""
    return [[10.00, 20], [10.05, 22], [10.10, 28], [10.15, 50],
            [10.20, 60], [10.20 + gap, 50], [10.20 + gap + 0.04, 42]]


def anchors_with_stage3_delta(delta):
    """第 3 级锚点相对前两级偏移 delta，用于时钟残差边界测试。"""
    return {"1": {"A": 10.0}, "2": {"A": 10.0},
            "3": {"A": 10.0 + delta}}


def pressure_batch(batch_id, anchors=None, root_curve=None):
    stages = []
    for no in (1, 2, 3):
        stages.append({
            "stage": no, "screw_travel": no * 10, "shot_volume": no * 40,
            "runner_weight": no * 9.0,          # 制件 20 g + 流道 9 g ≈ 射出 30 g
            "pressure_curve": P_MACHINE,
            "cavity_weights": {"C1": no * 6, "C2": no * 6,
                               "C3": no * 6, "C4": no * 2},
            "mold_pressure": {"SROOT": root_curve or P_ROOT,
                              "SL": P_LEFT, "SR": P_RIGHT},
        })
    return {
        "batch_id": batch_id,
        "melt_density": DENSITY,
        "imbalance_limit": 0.05,
        "cavities": [{"cavity_id": c, "volume": 40.0}
                     for c in ("C1", "C2", "C3", "C4")],
        "runner_tree": PRESSURE_TREE,
        "scale_check": {"before": 100.000, "after": 100.004,
                        "tolerance": 0.01},
        "stages": stages,
        "sensors": PRESSURE_SENSORS,
        "trigger_anchors": anchors or {str(n): {"A": 10.0}
                                       for n in (1, 2, 3)},
    }


def post_batch(name, batch_id, **kw):
    code, r = call("POST", "/batches", pressure_batch(batch_id, **kw))
    check("%s：批次入库" % name, code == 201 and r.get("accepted"),
          str(r)[:160])


def check_clean_review(name, batch_id):
    """无质量问题且压力结论保持指认 root 节点 node:R 支受限。"""
    code, r = call("GET", "/batches/%s/report" % batch_id)
    pr = r["metrics"]["pressure_review"]
    check("%s：无质量问题，压力结论指认 R 支受限" % name,
          code == 200
          and pr["status"] == "restricted"
          and pr["quality_issues"] == []
          and pr["restriction"]["primary"]["node_id"] == "root"
          and pr["restriction"]["primary"]["slow_branch"] == "node:R"
          and pr["restriction"]["cross_check"]["verdict"] == "agrees",
          pr["status"] + " " + str(pr["quality_issues"])[:160])


# ----------------------------------------------------------------
# 2. clock_residual_s（限值 0.005 s，1e-9 容差）边界批次
# ----------------------------------------------------------------
# 等于限值时浮点残差为 0.0050000000000007816 > 0.005，
# 没有 1e-9 容差就会误报——以下三组同时锁定限值与容差
for name, delta in [("时钟残差仅低于限值", 0.0049),
                    ("时钟残差等于限值", 0.005),
                    ("时钟残差高于限值但在 1e-9 容差内", 0.0050000005)]:
    bid = "CR-" + str(delta)
    post_batch(name, bid, anchors=anchors_with_stage3_delta(delta))
    check_clean_review(name, bid)
    code, r = call("GET", "/batches/%s/report" % bid)
    res = r["metrics"]["pressure_review"]["clock_residual_s"]["A"]
    check("%s：残差记录值 %.7g 未越限" % (name, res),
          res == round((10.0 + delta) - 10.0, 6)
          and not any(i["code"] == "CLOCK_RESIDUAL"
                      for i in r["metrics"]["pressure_review"]
                      ["quality_issues"]),
          str(res))

# 仅高于限值（超出 1e-9 容差）：记 CLOCK_RESIDUAL，结论转为数据质量存疑
post_batch("时钟残差仅高于限值", "CR-ABOVE",
           anchors=anchors_with_stage3_delta(0.0051))
code, r = call("GET", "/batches/CR-ABOVE/report")
pr = r["metrics"]["pressure_review"]
clock_issues = [i for i in pr["quality_issues"] if i["code"] == "CLOCK_RESIDUAL"]
check("时钟残差仅高于限值：记 CLOCK_RESIDUAL 且压力结论存疑",
      pr["status"] == "inconclusive_data_quality"
      and len(clock_issues) == 1
      and clock_issues[0]["node"] == "A"
      and clock_issues[0]["node_kind"] == "collector"
      and clock_issues[0]["stage"] is None
      and clock_issues[0]["raw_interval"] == [10.0, 10.0051]
      and pr["clock_residual_s"]["A"] == 0.0051,
      pr["status"] + " " + str(clock_issues))
check("时钟残差越限：受限证据保留但被质量否决",
      pr["restriction"]["primary"]["node_id"] == "root"
      and pr["restriction"]["primary"]["slow_branch"] == "node:R",
      str(pr["restriction"]["primary"])[:160])


# ----------------------------------------------------------------
# 3. max_sample_gap_s（限值 0.050 s，1e-9 容差）边界批次
# ----------------------------------------------------------------
# 等于限值时浮点间隔为 0.050000000000000711 > 0.050，同样靠 1e-9 容差吸收
for name, gap in [("采样断档仅低于限值", 0.049),
                  ("采样断档等于限值", 0.05),
                  ("采样断档高于限值但在 1e-9 容差内", 0.0500000005)]:
    bid = "GAP-" + str(gap)
    post_batch(name, bid, root_curve=root_curve_with_tail_gap(gap))
    check_clean_review(name, bid)
    code, r = call("GET", "/batches/%s/report" % bid)
    check("%s：未记 SAMPLE_GAP" % name,
          not any(i["code"] == "SAMPLE_GAP"
                  for i in r["metrics"]["pressure_review"]["quality_issues"]))

# 仅高于限值（超出 1e-9 容差）：三级各记一次 SAMPLE_GAP，结论存疑
post_batch("采样断档仅高于限值", "GAP-ABOVE",
           root_curve=root_curve_with_tail_gap(0.051))
code, r = call("GET", "/batches/GAP-ABOVE/report")
pr = r["metrics"]["pressure_review"]
gaps = [i for i in pr["quality_issues"] if i["code"] == "SAMPLE_GAP"]
check("采样断档仅高于限值：三级各记 SAMPLE_GAP 且压力结论存疑",
      pr["status"] == "inconclusive_data_quality"
      and len(gaps) == 3
      and {i["stage"] for i in gaps} == {1, 2, 3}
      and all(i["node"] == "root" and i["node_kind"] == "node"
              and i["raw_interval"] == [10.2, 10.251] for i in gaps),
      pr["status"] + " " + str(gaps)[:200])
check("采样断档越限：受限证据保留但被质量否决",
      pr["restriction"]["primary"]["node_id"] == "root"
      and pr["restriction"]["primary"]["slow_branch"] == "node:R",
      str(pr["restriction"]["primary"])[:160])


# ----------------------------------------------------------------
# 4. 同采集器锚点修订：alignment 与三条冻结曲线采用同一锚点
# ----------------------------------------------------------------
post_batch("锚点修订", "P-ANCHOR")
code, r = call("GET", "/batches/P-ANCHOR/report")
pr = r["metrics"]["pressure_review"]
check("锚点修订前：alignment 为原始锚点 10.0",
      all(pr["alignment"][str(n)]["A"]["adopted_anchor"] == 10.0
          and pr["alignment"][str(n)]["A"]["revised_by"] is None
          for n in (1, 2, 3)),
      str(pr["alignment"]["1"]))
fz = pr["frozen_curves"]["sensors"]
check("锚点修订前：三条冻结曲线均按 10.0 对齐（首点 0.0）",
      all(fz[s]["1"]["adopted_anchor"] == 10.0
          and fz[s]["1"]["aligned_points"][0][0] == 0.0
          for s in ("SROOT", "SL", "SR")),
      str({s: fz[s]["1"]["aligned_points"][0]
           for s in ("SROOT", "SL", "SR")}))

# 通过 SL 提交锚点修订 10.0 -> 9.9（采集器 A 级时钟量），覆盖全部级
code, r = call("POST", "/batches/P-ANCHOR/pressure-revisions",
               {"kind": "anchor_override", "sensor_id": "SL",
                "new_anchor": 9.9,
                "reason": "采集器 A 接线复核，触发时刻整体偏移 0.1 s"})
check("锚点修订派生逐帧记录",
      code == 201 and len(r["derived_records"]) == 3
      and {x["stage"] for x in r["derived_records"]} == {1, 2, 3}
      and all(x["old_anchor"] == 10.0 and x["new_anchor"] == 9.9
              for x in r["derived_records"]), str(r))

code, r = call("GET", "/batches/P-ANCHOR/report")
pr = r["metrics"]["pressure_review"]
check("锚点修订后：alignment 三级统一为 9.9 且注明派生自 SL",
      all(pr["alignment"][str(n)]["A"]["adopted_anchor"] == 9.9
          and pr["alignment"][str(n)]["A"]["raw_anchor"] == 10.0
          and pr["alignment"][str(n)]["A"]["revised_by"] == "SL"
          for n in (1, 2, 3)),
      str(pr["alignment"]))
fz = pr["frozen_curves"]["sensors"]
# 关键回归：修复前 SROOT 仍按 10.0 对齐（首点 0.0），与 SL/SR 的 0.1 不一致
check("锚点修订后：三条冻结曲线采用同一锚点（首点同为 0.1）",
      all(fz[s]["1"]["adopted_anchor"] == 9.9
          and abs(fz[s]["1"]["aligned_points"][0][0] - 0.1) < 1e-9
          for s in ("SROOT", "SL", "SR"))
      and len({fz[s]["1"]["aligned_points"][0][0]
               for s in ("SROOT", "SL", "SR")}) == 1,
      str({s: (fz[s]["1"]["adopted_anchor"],
               fz[s]["1"]["aligned_points"][0])
           for s in ("SROOT", "SL", "SR")}))
check("锚点修订后：机台曲线锚点保持 0.0，结论不受统一平移影响",
      pr["frozen_curves"]["machine"]["1"]["anchor_s"] == 0.0
      and pr["status"] == "restricted",
      pr["status"])
check("锚点修订留痕且原始记录未被覆盖",
      len(pr["revision_history"]) == 3
      and all(x["kind"] == "anchor_override"
              and x["reason"].startswith("采集器 A")
              and x["old_anchor"] == 10.0
              for x in pr["revision_history"])
      and r["recompute_inputs"]["trigger_anchors"]["1"]["A"] == 10.0,
      str(pr["revision_history"])[:200])


print()
shutil.rmtree(TMPDIR, ignore_errors=True)
if failures:
    print("失败：", failures)
    sys.exit(1)
print("全部通过")
