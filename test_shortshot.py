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

print()
if failures:
    print("失败：", failures)
    sys.exit(1)
print("全部通过")
