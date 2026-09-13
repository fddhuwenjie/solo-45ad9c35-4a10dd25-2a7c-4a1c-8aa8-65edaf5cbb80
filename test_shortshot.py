#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shortshot_server 冒烟测试：直接调用 WSGI 应用，不占用端口。"""

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shortshot_server as srv

srv.DB_PATH = "test_shortshot.db"
if os.path.exists(srv.DB_PATH):
    os.remove(srv.DB_PATH)
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


def make_batch(batch_id, stages, **over):
    p = {
        "batch_id": batch_id,
        "melt_density": DENSITY,
        "imbalance_limit": 0.05,
        "cavities": [
            {"cavity_id": "C1", "volume": 40.0},
            {"cavity_id": "C2", "volume": 40.0},
            {"cavity_id": "C3", "volume": 40.0},
            {"cavity_id": "C4", "volume": 40.0},
        ],
        "runner_tree": {
            "feeds": [{"cavity_id": c} for c in ("C1", "C2", "C3", "C4")]
        },
        "scale_check": {"before": 100.000, "after": 100.004, "tolerance": 0.01},
        "stages": stages,
    }
    p.update(over)
    return p


def stage(no, travel, shot, weights, runner):
    return {
        "stage": no, "screw_travel": travel, "shot_volume": shot,
        "runner_weight": runner,
        "pressure_curve": [[0.0, 20.0], [0.4, 55.0], [0.8, 38.0]],
        "cavity_weights": weights,
    }


# 每腔容量 40*0.75=30 g；每级射出增量 40cm3*0.75=30 g，
# 故每级「四腔增量合计 + 流道料增量」须为 30 g。C4 进料滞后且偏差逐级扩大。
good_stages = [
    stage(1, 10, 40, {"C1": 6, "C2": 6, "C3": 6, "C4": 0}, 12.0),  # 18+12=30
    stage(2, 20, 80, {"C1": 12, "C2": 12, "C3": 11, "C4": 4}, 21.0),
    stage(3, 30, 120, {"C1": 18, "C2": 18, "C3": 17, "C4": 7}, 30.0),
    stage(4, 40, 160, {"C1": 25, "C2": 24, "C3": 24, "C4": 8}, 39.0),
]

failures = []

def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)


# 1. 正常批次入库
code, r = call("POST", "/batches", make_batch("B001", good_stages))
check("创建批次", code == 201 and r.get("accepted"), str(r)[:120])

# 2. 报告含指标
code, r = call("GET", "/batches/B001/report")
m = r["metrics"]
check("报告 200", code == 200)
check("充填比例 C4 最低", m["fill_ratio"]["C4"] < m["fill_ratio"]["C1"],
      str(m["fill_ratio"]))
check("C4 进料滞后 1 级", m["feed_lag_stages"]["C4"] == 1,
      str(m["feed_lag_stages"]))
check("最早分流异常点在第1级",
      m["earliest_flow_imbalance"]["stage"] == 1
      and m["earliest_flow_imbalance"]["slowest"] == "C4",
      str(m["earliest_flow_imbalance"]))
check("扁平树失衡分支定位到 root",
      m["earliest_branch_imbalance"]["node_id"] == "root"
      and m["earliest_branch_imbalance"]["stage"] == 1
      and m["earliest_branch_imbalance"]["slowest_branch"] == "C4",
      str(m["earliest_branch_imbalance"]))
check("C4 判为连续扩大",
      m["deviation_assessment"]["C4"]["kind"] == "continuous_divergence",
      str(m["deviation_assessment"]["C4"]))
check("物料账汇总", abs(m["material_balance"]["unaccounted_g"]) < 1e-6,
      str(m["material_balance"]))

# 3. 剔除一次称量（附理由），报告采用记录
code, r = call("POST", "/batches/B001/exclusions",
               {"stage": 3, "cavity_id": "C2", "reason": "称量时制件沾模未取净"})
check("剔除称量", code == 201, str(r))
code, r = call("GET", "/batches/B001/report")
check("报告带采用记录",
      r["manual_handling"]["exclusions"][0]["reason"] == "称量时制件沾模未取净")
code, r = call("POST", "/batches/B001/exclusions",
               {"stage": 3, "cavity_id": "C2"})
check("无理由剔除被拒", code == 400)

# 4. 登记浇口修整另起方案
code, r = call("POST", "/batches/B001/plans",
               {"note": "扩大 C4 浇口", "gate_changes":
                [{"cavity_id": "C4", "gate_reduction_mm": -0.15}]})
check("登记修整方案", code == 201 and r.get("plan_id"), str(r))

# 5. 第二批（修模后改善），做批次差异
better = [
    stage(1, 10, 40, {"C1": 6, "C2": 6, "C3": 6, "C4": 5}, 7.0),
    stage(2, 20, 80, {"C1": 12, "C2": 12, "C3": 11, "C4": 9}, 16.0),
    stage(3, 30, 120, {"C1": 17, "C2": 17, "C3": 16, "C4": 15}, 25.0),
    stage(4, 40, 160, {"C1": 23, "C2": 22, "C3": 22, "C4": 19}, 34.0),
]
call("POST", "/batches", make_batch("B002", better))
code, r = call("GET", "/batches/B002/diff?other=B001")
check("批次差异", code == 200 and r["fill_ratio_delta"]["C4"] > 0.2,
      str(r["fill_ratio_delta"]))

# 6. 各类校验失败，须点出级次/型腔
bad = make_batch("E1", [stage(2, 10, 40, {"C1": 6}, 9.0),
                        stage(1, 20, 80, {"C1": 13}, 18.0)])
code, r = call("POST", "/batches", bad)
codes = {e["code"] for e in r["errors"]}
check("级次不递增", code == 400 and "STAGE_NOT_INCREASING" in codes, str(codes))

bad = make_batch("E2", [stage(1, 10, 40, {"C1": 6}, 9.0),
                        stage(2, 5, 80, {"C1": 13}, 18.0)])
code, r = call("POST", "/batches", bad)
codes = {e["code"] for e in r["errors"]}
check("行程不递增", "TRAVEL_NOT_INCREASING" in codes, str(codes))

bad = make_batch("E3", good_stages,
                 cavities=[{"cavity_id": "C1", "volume": 40},
                           {"cavity_id": "C1", "volume": 40}])
code, r = call("POST", "/batches", bad)
check("型腔重号", any(e["code"] == "DUPLICATE_CAVITY" and e["cavity"] == "C1"
                      for e in r["errors"]))

bad = make_batch("E4", good_stages,
                 scale_check={"before": 100.0, "after": 100.5,
                              "tolerance": 0.01})
code, r = call("POST", "/batches", bad)
check("秤失准", any(e["code"] == "SCALE_DRIFT" for e in r["errors"]))

bad = make_batch("E5", [stage(1, 10, 40, {"C1": 1, "C2": 1, "C3": 1, "C4": 1}, 1.0),
                        stage(2, 20, 80, {"C1": 2, "C2": 2, "C3": 2, "C4": 2}, 2.0),
                        stage(3, 30, 120, {"C1": 3, "C2": 3, "C3": 3, "C4": 3}, 3.0)])
code, r = call("POST", "/batches", bad)
check("物料账不闭合", any(e["code"] == "BALANCE_NOT_CLOSED" and e["stage"] == 1
                          for e in r["errors"]))

bad = make_batch("E6", good_stages)
bad["stages"][1]["pressure_curve"] = []
code, r = call("POST", "/batches", bad)
check("压力缺段", any(e["code"] == "PRESSURE_GAP" and e["stage"] == 2
                      for e in r["errors"]))

bad = make_batch("E7", good_stages[:2])
code, r = call("POST", "/batches", bad)
check("有效级数不足", any(e["code"] == "TOO_FEW_STAGES" for e in r["errors"]))

# 7. 孤立噪声判定：C3 中间一级偶然偏低
noisy = [
    stage(1, 10, 40, {"C1": 6, "C2": 6, "C3": 6, "C4": 6}, 6.0),
    stage(2, 20, 80, {"C1": 13, "C2": 13, "C3": 8, "C4": 11}, 15.0),
    stage(3, 30, 120, {"C1": 17, "C2": 17, "C3": 16, "C4": 16}, 24.0),
    stage(4, 40, 160, {"C1": 22, "C2": 22, "C3": 22, "C4": 21}, 33.0),
]
call("POST", "/batches", make_batch("B003", noisy))
code, r = call("GET", "/batches/B003/report")
check("孤立称量噪声",
      r["metrics"]["deviation_assessment"]["C3"]["kind"] == "isolated_noise",
      str(r["metrics"]["deviation_assessment"]["C3"]))

# 8. 多层流道树：失衡须定位到分支节点 R 而非根
multi_tree = {
    "root": "sprue",
    "nodes": [
        {"node_id": "sprue", "children": ["L", "R"]},
        {"node_id": "L", "feeds": ["C1", "C2"]},
        {"node_id": "R", "children": ["R1", "R2"]},
        {"node_id": "R1", "feeds": ["C3"]},
        {"node_id": "R2", "feeds": ["C4"]},
    ],
}
# 每级型腔增量合计 24 g + 流道料 6 g = 30 g；C3 快、C4 慢但两支均值相等，
# 根节点两支平衡，失衡只发生在 R 节点内部。
multi = [
    stage(1, 10, 40, {"C1": 6, "C2": 6, "C3": 9, "C4": 3}, 6.0),
    stage(2, 20, 80, {"C1": 12, "C2": 12, "C3": 15, "C4": 9}, 12.0),
    stage(3, 30, 120, {"C1": 18, "C2": 18, "C3": 20, "C4": 16}, 18.0),
    stage(4, 40, 160, {"C1": 24, "C2": 24, "C3": 25, "C4": 23}, 24.0),
]
code, r = call("POST", "/batches",
               make_batch("B004", multi, runner_tree=multi_tree))
check("多层树批次入库", code == 201, str(r)[:160])
code, r = call("GET", "/batches/B004/report")
bi = r["metrics"]["earliest_branch_imbalance"]
check("失衡定位到分支节点 R",
      bi["stage"] == 1 and bi["node_id"] == "R"
      and bi["fastest_branch"] == "R1" and bi["slowest_branch"] == "R2"
      and bi["cavities"] == ["C3", "C4"],
      str(bi))
check("分支明细带原始级次与型腔",
      {b["branch"] for b in bi["branches"]} == {"R1", "R2"}
      and bi["branches"][0]["mean_fill"] > 0,
      str(bi["branches"]))

# 9. 空树明确拒绝
for label, empty in [("缺 runner_tree 字段", None),
                     ("空对象", {}),
                     ("feeds 为空", {"feeds": []}),
                     ("nodes 为空", {"nodes": []})]:
    bad = make_batch("E9", good_stages)
    if empty is None:
        del bad["runner_tree"]
    else:
        bad["runner_tree"] = empty
    code, r = call("POST", "/batches", bad)
    check("空树拒绝：%s" % label,
          code == 400 and any(e["code"] == "EMPTY_RUNNER_TREE"
                              for e in r["errors"]),
          str(r.get("errors"))[:120])

# 10. 树结构非法与接入核对
bad = make_batch("E10", good_stages, runner_tree={
    "nodes": [{"node_id": "a", "children": ["ghost"],
               "feeds": ["C1", "C2", "C3", "C4"]}]})
code, r = call("POST", "/batches", bad)
check("悬空子节点", any(e["code"] == "RUNNER_TREE_INVALID"
                        for e in r["errors"]), str(r["errors"])[:120])

bad = make_batch("E11", good_stages, runner_tree={
    "feeds": [{"cavity_id": c} for c in ("C1", "C2", "C3")]})
code, r = call("POST", "/batches", bad)
check("型腔未接入流道树",
      any(e["code"] == "CAVITY_NOT_FED" and e["cavity"] == "C4"
          for e in r["errors"]), str(r["errors"])[:120])

bad = make_batch("E12", good_stages, runner_tree={
    "nodes": [{"node_id": "root", "children": ["a", "b"]},
              {"node_id": "a", "feeds": ["C1", "C2"]},
              {"node_id": "b", "feeds": ["C1", "C3", "C4"]}]})
code, r = call("POST", "/batches", bad)
check("型腔重复进料",
      any(e["code"] == "CAVITY_FED_TWICE" and e["cavity"] == "C1"
          for e in r["errors"]), str(r["errors"])[:120])

# 11. 报告快照可独立追溯：复算输入 + 采用记录 + 指标 + 人工处理
code, r = call("GET", "/batches/B001/report")
check("快照四要素齐全",
      all(k in r for k in ("recompute_inputs", "adopted_records",
                           "metrics", "manual_handling")))
check("复算输入即原始提交",
      r["recompute_inputs"]["batch_id"] == "B001"
      and len(r["recompute_inputs"]["stages"]) == 4
      and r["recompute_inputs"]["imbalance_limit"] == 0.05)
check("采用记录含剔除与浇口方案",
      r["adopted_records"]["exclusions"] == [{"stage": 3, "cavity_id": "C2"}]
      and r["adopted_records"]["gate_plans"][0]["gate_changes"][0]
      ["cavity_id"] == "C4",
      str(r["adopted_records"]))
check("人工处理带理由",
      r["manual_handling"]["exclusions"][0]["reason"] == "称量时制件沾模未取净"
      and r["manual_handling"]["follow_up_plans"][0]["note"] == "扩大 C4 浇口")

# 12. 差异响应带两批次采用记录与完整快照
code, r = call("GET", "/batches/B002/diff?other=B001")
check("差异带两批采用记录",
      r["adopted_records"]["B001"]["exclusions"]
      == [{"stage": 3, "cavity_id": "C2"}]
      and r["adopted_records"]["B001"]["gate_plans"]
      and r["adopted_records"]["B002"]["exclusions"] == [],
      str(r["adopted_records"]))
check("差异带两批快照",
      r["snapshots"]["B001"]["recompute_inputs"]["batch_id"] == "B001"
      and r["snapshots"]["B002"]["metrics"]["fill_ratio"]
      and r["snapshots"]["B001"]["manual_handling"]["exclusions"])
check("差异带两批分支定位",
      r["earliest_branch_imbalance"]["B001"]["node_id"] == "root"
      and "B002" in r["earliest_branch_imbalance"],
      str(r["earliest_branch_imbalance"]))

# 13. _window_integral 窗口边界插值：窗口内无离散点也要积分
check("窗口内无采样点按端点插值积分",
      abs(srv._window_integral([[0.0, 10.0], [1.0, 10.0]], 0.0, 0.5) - 5.0)
      < 1e-9)
check("采样点全在窗口外但落在曲线定义域内仍积分",
      abs(srv._window_integral([[0.2, 10.0], [0.4, 10.0]], 0.0, 0.5) - 5.0)
      < 1e-9)
check("窗口与曲线定义域不相交积分为 0",
      srv._window_integral([[0.1, 10.0], [0.2, 10.0]], 0.4, 0.5) == 0.0)
check("斜线跨窗积分等于梯形面积",
      abs(srv._window_integral([[0.0, 0.0], [1.0, 10.0]], 0.0, 1.0) - 5.0)
      < 1e-9)

# 14. 同采集器多传感器：改任一传感器锚点须统一作用于 alignment 与全部冻结曲线
pressure_tree = {
    "root": "root",
    "nodes": [
        {"node_id": "root", "children": ["L", "R"]},
        {"node_id": "L", "feeds": ["C1", "C2"]},
        {"node_id": "R", "feeds": ["C3", "C4"]},
    ],
}
pressure_sensors = [
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
# 采样间隔 ≤20 ms，避免采样断档；ROOT 0.20 到压、L 0.24、R 0.32（R 支滞后）
p_machine = [[0.00, 20], [0.05, 30], [0.10, 55], [0.15, 48],
             [0.20, 42], [0.25, 40], [0.30, 38]]
p_root = [[10.00, 20], [10.05, 22], [10.10, 28], [10.15, 50],
          [10.20, 60], [10.25, 50], [10.30, 42]]
p_left = [[10.00, 20], [10.05, 21], [10.10, 24], [10.15, 40],
          [10.20, 52], [10.24, 58], [10.30, 44]]
p_right = [[10.00, 20], [10.05, 20], [10.10, 21], [10.15, 22],
           [10.20, 24], [10.25, 30], [10.28, 38]]


def pstage(no):
    s = stage(no, no * 10, no * 40,
              {"C1": no * 6, "C2": no * 6, "C3": no * 6, "C4": no * 2},
              no * 9.0)        # 制件增量 20 g + 流道 9 g = 29 g ≈ 射出 30 g
    s["pressure_curve"] = p_machine
    s["mold_pressure"] = {"SROOT": p_root, "SL": p_left, "SR": p_right}
    return s


p_batch = make_batch("P001", [pstage(1), pstage(2), pstage(3)],
                     runner_tree=pressure_tree)
p_batch["sensors"] = pressure_sensors
p_batch["trigger_anchors"] = {str(n): {"A": 10.0} for n in (1, 2, 3)}
code, r = call("POST", "/batches", p_batch)
check("压力批次入库", code == 201 and r.get("accepted"), str(r)[:200])
code, r = call("GET", "/batches/P001/report")
pr = r["metrics"]["pressure_review"]
check("修订前对齐锚点为原始值",
      pr["alignment"]["1"]["A"]["adopted_anchor"] == 10.0
      and pr["alignment"]["1"]["A"]["revised_by"] is None,
      str(pr["alignment"]["1"]))
fz = pr["frozen_curves"]["sensors"]
check("修订前三传感器冻结曲线均按 10.0 对齐（首点落在 0.0）",
      all(fz[s]["1"]["aligned_points"][0][0] == 0.0 for s in
          ("SROOT", "SL", "SR")),
      str({s: fz[s]["1"]["aligned_points"][0] for s in
           ("SROOT", "SL", "SR")}))
check("修订前无质量问题且压力指认 R 支受限",
      pr["status"] == "restricted"
      and pr["restriction"]["primary"]["node_id"] == "root"
      and pr["restriction"]["primary"]["slow_branch"] == "node:R"
      and pr["restriction"]["cross_check"]["verdict"] == "agrees",
      pr["status"])

# 通过 SL 提交锚点修订 10.0 -> 9.9（采集器 A 级时钟量），覆盖全部级
code, r = call("POST", "/batches/P001/pressure-revisions",
               {"kind": "anchor_override", "sensor_id": "SL",
                "new_anchor": 9.9,
                "reason": "采集器 A 接线复核，触发时刻整体偏移 0.1 s"})
check("锚点修订派生逐帧记录",
      code == 201 and len(r["derived_records"]) == 3
      and {x["stage"] for x in r["derived_records"]} == {1, 2, 3}
      and all(x["old_anchor"] == 10.0 and x["new_anchor"] == 9.9
              for x in r["derived_records"]), str(r))
code, r = call("GET", "/batches/P001/report")
pr = r["metrics"]["pressure_review"]
check("修订后 alignment 统一为 9.9 且注明派生自 SL",
      all(pr["alignment"][str(n)]["A"]["adopted_anchor"] == 9.9
          and pr["alignment"][str(n)]["A"]["raw_anchor"] == 10.0
          and pr["alignment"][str(n)]["A"]["revised_by"] == "SL"
          for n in (1, 2, 3)),
      str(pr["alignment"]))
fz = pr["frozen_curves"]["sensors"]
check("修订后 SROOT/SL/SR 冻结曲线统一平移（首点 0.1，锚点字段 9.9）",
      all(fz[s]["1"]["adopted_anchor"] == 9.9
          and abs(fz[s]["1"]["aligned_points"][0][0] - 0.1) < 1e-9
          for s in ("SROOT", "SL", "SR")),
      str({s: (fz[s]["1"]["adopted_anchor"],
               fz[s]["1"]["aligned_points"][0])
           for s in ("SROOT", "SL", "SR")}))
# 关键回归：修复前 SROOT 仍按 10.0 对齐（首点 0.0），与 SL 的 0.1 不一致
check("修订历史注明理由且原始记录未被覆盖",
      len(pr["revision_history"]) == 3
      and all(x["kind"] == "anchor_override"
              and x["reason"].startswith("采集器 A")
              and x["old_anchor"] == 10.0
              for x in pr["revision_history"])
      and r["recompute_inputs"]["trigger_anchors"]["1"]["A"] == 10.0,
      str(pr["revision_history"]))

# 锚点修订必须附理由；坏点排除必须给级次与时标区间
code, r = call("POST", "/batches/P001/pressure-revisions",
               {"kind": "anchor_override", "sensor_id": "SL",
                "new_anchor": 9.8})
check("无理由锚点修订被拒", code == 400, str(r))
code, r = call("POST", "/batches/P001/pressure-revisions",
               {"kind": "bad_point", "sensor_id": "SR",
                "reason": "该帧受电磁干扰"})
check("坏点缺级次/区间被拒", code == 400, str(r))

# 15. 压力主流程：到压延迟、衰减、积分与批次差异中的压力字段
code, r = call("GET", "/batches/P001/report")
pr = r["metrics"]["pressure_review"]
edges = {(e["to"], e["edge_kind"]): e for e in pr["edges"]}
check("沿树父子边含传播延迟与衰减",
      edges[("node:R", "runner")]["per_stage"][0]["delay_s"] > 0.05
      and edges[("node:R", "runner")]["per_stage"][0]["peak_attenuation"]
      is not None,
      str(edges[("node:R", "runner")]["per_stage"][0]))
check("注射区间积分非零（边界插值修复后）",
      all(v["integral"] > 0 for sid in ("SROOT", "SL", "SR")
          for st, v in pr["sensor_metrics"][sid].items())
      and pr["frozen_curves"]["machine"]["1"]["points"])
# 无传感器批次复核状态为 no_sensors，差异接口不受影响
code, r = call("GET", "/batches/B002/diff?other=B001")
check("差异接口对无传感器批次跳过压力字段",
      code == 200 and r["pressure_review"] == {}, str(r.get("pressure_review")))
# 有传感器批次入差异：冻结曲线/对齐/依据随快照共享
code, r = call("GET", "/batches/P001/diff?other=B002")
check("差异带压力复核与到压时刻",
      code == 200 and r["pressure_review"]["P001"]["alignment"]["1"]["A"]
      ["adopted_anchor"] == 9.9
      and "SL" in r["pressure_arrival_delta"]
      and r["pressure_review"]["P001"]["judgment_basis"]["rule"],
      str(r.get("pressure_review", {}))[:160])

print()
if failures:
    print("失败：", failures)
    sys.exit(1)
print("全部通过")
