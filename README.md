# 星载推进剂隔离装置 · 联锁捕获复核服务

在隔离装置切换前，复核员录入带时钟抖动的联锁捕获。复核器以**精确有理数**
（`fractions.Fraction`）区域传播枚举**全部可能时刻**，仅当每条可能轨迹在
每个事件下**恰有一条迁移**可触发、且最终全部抵达终态时，才允许“冻结通过”。

## 复核规则

- 位置 ≤ 8，时钟 ≤ 4，迁移 ≤ 16，捕获事件 ≤ 32，按捕获顺序到达。
- 每条迁移携带**闭区间守卫**（每时钟至多一个 `[lower, upper]`）与**复位集合**。
- 每个事件携带相对前一事件的**相对时刻闭区间**（首事件相对 0 时刻）。
- 同位置、同事件的迁移守卫为闭盒；其闭区间**不得重叠**（边界点相触也算
  重叠），非法模型精确拒绝并给出可代入的重叠时钟取值。
- 每事件依次：① 在闭窗口内推进时间 ② 按守卫切分区域 ③ 唯一迁移并复位。
- 若存在未覆盖时刻、无迁移、轨迹非终态，结论稳定指出**最早事件下标**，
  并给出一组可代入核对的**有理数时钟取值**与**阻断守卫**。
- 以稳定审计标识 `audit_id` 留存证据：
  - 同标识**语义等价重传**（规范化 JSON 指纹相同）→ 回放原结论；
  - 同标识**内容不同** → 返回 `409 conflict`，**保留原证据**并报告冲突。

### 精确性实现要点

- 区域为带严格位的**差分边界矩阵（DBM）**，Floyd–Warshall 规范化。
- 闭窗口时间推进用一次性辅助时钟精确成像后投影消去。
- 覆盖判定沿所有守卫盒边界把时钟空间切成“边界点 + 开区间单元”，逐单元做
  DBM 可行性判定（增量 DFS 剪枝），故闭区间边界、缺口内点均精确区分，
  无浮点误差。
- `tests/test_invariants.py` 用独立的标量分数仿真对 60+ 随机场景做差分
  校验，并把拒绝见证代回引擎输出的差分约束逐一验证。

## 目录

```
app/engine.py    精确区域复核引擎（模型解析、校验、区域传播、见证）
app/storage.py   审计标识证据留存（规范化指纹、重放、冲突）
app/main.py      FastAPI：/api/reviews、/api/reviews/{id}、/health、页面
app/static/      复核页面（结论 + 逐事件区域证据）
tests/           31 项规则/API/差分不变量测试
verify/          verify 容器入口脚本与 HTTP 冒烟
Dockerfile, docker-compose.yml
```

## 运行（Docker Compose）

```bash
# 宿主机端口可配置（默认 8080）
HOST_PORT=9090 docker compose up --build reviewer

# 健康检查响应文本可配置
HEALTH_ACK=alive HOST_PORT=9090 docker compose up --build reviewer
curl -s http://127.0.0.1:9090/health   # {"status":"alive"}
```

打开 `http://127.0.0.1:<HOST_PORT>/` 录入并提交，页面展示冻结/拒绝结论、
最早事件、时钟见证、阻断守卫与逐事件区域证据。

### verify 容器

verify 容器等待 reviewer 健康后，执行**规则测试、构建检查（字节编译）、
对真实服务的 HTTP 冒烟**，随后退出并以退出码报告结果：

```bash
docker compose up --build --exit-code-from verify verify
# 控制台末尾打印 VERIFY: OK / VERIFY: FAIL，退出码 0/非 0
```

冒烟覆盖：合法重叠抖动时窗冻结、冷却区间缺口（见证 `9/2`、阻断守卫）、
非法重叠模型（422）、语义等价重传回放、同标识改内容冲突（409 且原证据保留）。

## 本地运行（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest                                   # 规则/API/差分测试
EVIDENCE_STORE=/tmp/evidence.json \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
python verify/smoke_http.py              # 对本地服务 HTTP 冒烟
verify/run_tests.sh                      # 与 verify 容器相同的三步流程
```

## API

`POST /api/reviews`

```json
{
  "model": {
    "audit_id": "ISO-0001",
    "locations": ["armed", "cooling", "open", "done"],
    "clocks": ["x", "y"],
    "initial_location": "armed",
    "final_locations": ["done"],
    "transitions": [
      {"id": "t_cooldown", "source": "armed", "target": "cooling",
       "event": "cmd",
       "guards": [{"clock": "x", "lower": 0, "upper": 4}],
       "resets": ["y"]},
      {"id": "t_open", "source": "armed", "target": "open",
       "event": "cmd",
       "guards": [{"clock": "x", "lower": 5, "upper": 10}],
       "resets": ["y"]},
      {"id": "t_ack_c", "source": "cooling", "target": "done",
       "event": "ack",
       "guards": [{"clock": "y", "lower": 1, "upper": 3}], "resets": []},
      {"id": "t_ack_o", "source": "open", "target": "done",
       "event": "ack",
       "guards": [{"clock": "y", "lower": 1, "upper": 3}], "resets": []}
    ]
  },
  "events": [
    {"event": "cmd", "relative_lower": 4, "relative_upper": 6},
    {"event": "ack", "relative_lower": 2, "relative_upper": 2}
  ]
}
```

下界/上界接受整数、小数或分数字符串（如 `"9/2"`）。响应：

- `200 {"status":"frozen", ...}`：全部可能轨迹唯一迁移且终态；
- `200 {"status":"rejected","earliest_event_index":0,"failure":{...},
  "steps":[...]}`：最早阻断事件、时钟见证、阻断守卫、逐事件区域；
- `422 {"status":"invalid_model","error":{"details":{...}}}`：非法模型
  （含重叠守卫的精确见证）；
- `409 {"status":"conflict","conflict":{"original_evidence":{...}}}`：
  同标识内容冲突，原证据保留；
- 语义等价重传在原结论中附 `replay.semantically_equivalent_retransmission=true`。

`GET /api/reviews/{audit_id}` 取回留存的原始结论与证据。
