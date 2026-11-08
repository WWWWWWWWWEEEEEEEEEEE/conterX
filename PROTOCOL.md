# conterX 协议规范（conterx/1）

本文件是这套软件的核心。中转站不实现任何游戏规则，它只保证一件事：

> **上游游戏产出的数据，按规范裁剪后送给正确的下游程序；下游程序给出的动作，按规范校验后送回游戏。**

只要你的程序和下面这套消息格式对得上，就能直接接进来，语言不限（Python / C++ / JS / Rust / 任何能读写 stdin 的程序）。

---

## 1. 架构

```
上游游戏  ──init / run / actions──►            ──init / turn / over──►  玩家 1
         ◄──ready / ask / log / over──  conterX  ◄──ready / action / log──  玩家 2
                                         中转站  ◄──────────────────────  玩家 3…
```

- **游戏是主动方**：它自己维护状态、自己决定什么时候问谁、什么时候结束。
- **玩家是被动方**：收到 `turn` 就给 `action`，不需要知道别人在做什么。
- **中转站是唯一中间人**：玩家之间不直接通信，也拿不到别人的 `private` 数据。

中转站做四件事：

| 职责 | 说明 |
|---|---|
| 路由 | 把每个 `request` 精确投递给对应玩家，把动作汇总回游戏 |
| 隔离 | 每个玩家只收到游戏给它拼的那份 `view`，互相看不见 |
| 判罚 | 超时、崩溃、格式错误 → 记违规，累计到上限判负 |
| 记录 | 每回合的请求、响应、耗时、游戏播报全部写进日志 |

## 2. 传输层约定

| 项目 | 约定 |
|---|---|
| 编码 | UTF-8 |
| 格式 | **NDJSON**：一行一个 JSON 对象，以 `\n` 结尾 |
| stdout | **只能**输出协议消息，不能打印调试信息 |
| stderr | 随便打，中转站会收集并在出错时展示 |
| 启动 | 中转站以子进程方式拉起，工作目录 = 程序所在目录 |
| 结束 | 收到 `over` 或 `shutdown` 后自行退出；中转站也会兜底 kill |
| 版本 | 每条消息带 `"v": 1`；缺省视为 1，`"1"` / `1.0` 也认 |
| 严格性 | 必须是**严格 JSON**：不能出现 `NaN` / `Infinity`（其他语言的解析器会直接报错，中转站会按违规处理） |

Python 程序务必用 `-u`（无缓冲）或每次 `flush()`，否则消息会卡在缓冲区里。

---

## 3. 游戏协议

### 3.1 时序

```
中转站                                       游戏
   │──── init（玩家名单、种子、配置）─────────►│
   │◄──────────────────────────── ready ──────│
   │──── run ────────────────────────────────►│
   │◄───────────────────────────── ask ───────│   ← 可以连续 ask 多轮
   │──── actions（本轮所有人动作）───────────►│
   │◄───────────────────────────── ask ───────│
   │              ……                          │
   │◄──────────────────────────── over ───────│
```

### 3.2 消息定义

#### `init`（中转站 → 游戏）

```json
{
  "v": 1, "type": "init",
  "game_id": "g1712345678",
  "seed": 42,
  "players": [{"id": "p1", "name": "玩家1"}, {"id": "p2", "name": "玩家2"}],
  "config": {"rounds": 8}
}
```

`config` 是界面上「游戏配置」里填的 JSON，原样透传，用来调回合数、难度等。

#### `ready`（游戏 → 中转站）

```json
{"v": 1, "type": "ready", "name": "暗标竞拍",
 "players": {"min": 2, "max": 8}, "meta": {}}
```

#### `run`（中转站 → 游戏）

```json
{"v": 1, "type": "run"}
```

收到后开始跑第一回合。

#### `ask`（游戏 → 中转站）★ 核心

```json
{
  "v": 1, "type": "ask", "turn": 1,
  "requests": [
    {"player": "p1", "view": {"public": {...}, "private": {...}}, "deadline_ms": 2000},
    {"player": "p2", "view": {"public": {...}, "private": {...}}}
  ]
}
```

- `requests` 里放几个人，就是**同时**问几个人（中转站会并发下发，整回合耗时 ≈ 最慢那个人的耗时）；只放一个就是**顺序**制（回合制轮流）。
- 同一个 `player` 在一份 `requests` 里出现两次会被去重，只问一次。
- `view` 是**发给这一位玩家**的完整视野，中转站只做透传，不会把 p1 的 `view` 给 p2。
- `deadline_ms`：单回合思考时间（毫秒）。
  - 省略或传 `0` → 用界面上的「超时」设置；
  - 席位面板里每人的「单回合超时」是**上限**：最终时限 = min(上面算出的值, 席位超时)。
    所以席位超时能把游戏给的时限压短，但**不会**把它延长；
  - **必须是正数**，传负数会直接判为协议错误并终止对局（负数会被算成 50ms，让跑得飞快的玩家集体判负）。

> **私有视野怎么保证不泄露？**
> 因为每个玩家的 `view` 是游戏自己拼的，中转站按 `player` 字段逐个投递。
> 你只要不把别人的私密数据写进这一份 `view`，信息就不可能泄露。

#### `actions`（中转站 → 游戏）

```json
{
  "v": 1, "type": "actions", "turn": 1,
  "actions": {
    "p1": {"ok": true,  "action": {"bid": 30}, "elapsed_ms": 4},
    "p2": {"ok": false, "error": "timeout", "elapsed_ms": 2000, "violations": 1}
  }
}
```

`ok: false` 的几种 `error`：

| error | 含义 | 该玩家后续 |
|---|---|---|
| `timeout` | 决策超时 | 进程已重启，下回合继续 |
| `crash` | 进程崩溃 / 非 JSON 输出 | 进程已重启，下回合继续 |
| `protocol` | 格式错误（缺字段、动作无法序列化） | 视配置决定是否重试 |
| `disqualified` | 累计违规达上限，已判负 | 本局不再询问，恒定返回此值 |

**游戏必须能处理 `ok: false`**：通常视为该玩家本回合弃权 / 跳过。

#### `log`（游戏 → 中转站，可选）

```json
{"v": 1, "type": "log", "text": "第 1 回合 · 断齿齿轮：玩家1 以 89 拍得", "level": "info"}
```

随便发多少条，会实时出现在界面的「回合日志」里，也写进日志文件。**不影响时序**，中转站在等待 `ask` 时会自动跳过。

#### `over`（游戏 → 中转站）

```json
{
  "v": 1, "type": "over",
  "result": {
    "ranking": ["p2", "p1", "p3"],
    "scores": {"p1": 22, "p2": 25, "p3": 7},
    "winner": "p2",
    "reason": "8 回合结束",
    "extra": {"budget": {"p1": 12}}
  }
}
```

- `ranking` 从第一名到最后一名；省略时按 `scores` 降序推断。
- `scores` 用于批量排行榜的「平均得分」。
- 也可以把 `ranking` / `scores` 直接放在消息顶层，两种写法都认。

#### `error`（游戏 → 中转站，可选）

```json
{"v": 1, "type": "error", "message": "玩家数不足"}
```

中转站立刻终止本局，并在界面和日志里标红。

### 3.3 一个完整的游戏侧伪代码

```python
init = recv()                      # 拿玩家名单、种子、配置
send({"v":1,"type":"ready","name":"我的游戏"})
recv()                             # run

for turn in range(1, rounds + 1):
    send({"v":1,"type":"ask","turn":turn,"requests":[
        {"player": p, "view": {"public": 公共局面, "private": 你的私密信息}}
        for p in players
    ]})
    actions = recv()["actions"]    # {pid: {ok, action, ...}}
    # ……结算，更新局面……
    send({"v":1,"type":"log","text":f"第 {turn} 回合结束"})

send({"v":1,"type":"over","result":{"ranking":排序, "scores":得分}})
```

---

## 4. 玩家协议

### 4.1 时序

```
中转站                                       玩家
   │──── init（我是谁、什么游戏、我的配置）──►│
   │◄──────────────────────────── ready ──────│
   │──── turn（你的视野，deadline）──────────►│
   │◄─────────────────────────── action ──────│
   │              ……（重复 N 次）             │
   │──── over（最终排名 + 你的成绩）─────────►│
```

### 4.2 消息定义

#### `init`（中转站 → 玩家）

```json
{
  "v": 1, "type": "init",
  "player_id": "p1", "name": "玩家1",
  "game": {"name": "暗标竞拍"},
  "config": {},
  "persistent": true
}
```

`config` 来自席位配置（目前固定为空对象，可用于传算法参数）。
`persistent: true` 表示本进程会跨回合存活，可以放心在内存里攒状态（搜索树、历史统计）。

#### `ready`（玩家 → 中转站）

```json
{"v": 1, "type": "ready"}
```

#### `turn`（中转站 → 玩家）

```json
{
  "v": 1, "type": "turn", "turn": 3, "deadline_ms": 2000,
  "view": {
    "public": {"round": 3, "item": "青玉算盘", "scores": {"p1": 17}},
    "private": {"budget": 43, "valuation": 59}
  }
}
```

`view` 的结构完全由游戏定义，玩家按游戏的文档读即可。约定俗成：`public` 人人可见，`private` 只有你可见。

#### `action`（玩家 → 中转站）

```json
{"v": 1, "type": "action", "turn": 3, "action": {"bid": 43}}
```

`action` 的具体格式也由游戏定义（这里是 `{"bid": 43}`）。返回 `null` 会被判为违规。

**`turn` 必须原样回带当前回合号。** 中转站会校验：如果收到的 `turn` 和当前回合不一致，
说明你多吐了一条动作、队列已经错位，这一回合会按协议违规处理并重启你的进程——
不要省略 `turn`，也不要用上一次的回合号。

#### `log`（玩家 → 中转站，可选）

```json
{"v": 1, "type": "log", "text": "本轮估值 59，出价 43"}
```

调试神器：玩家的思考过程会实时出现在回合日志里，但**不会**被别的玩家看到。

#### `over`（中转站 → 玩家）

```json
{
  "v": 1, "type": "over",
  "result": {"ranking": ["p2","p1","p3"], "scores": {...}, "winner": "p2"},
  "you": {"rank": 2, "score": 22, "violations": 0, "disqualified": false}
}
```

收到后可以打印总结、存模型、然后退出。

> **非常驻模式（`persistent: false`）下也一定会收到 `over`**：中转站在下一回合开始前才关掉旧进程，
> 所以最后一回合那个进程还活着，收得到结算。别在 `over` 之前就 `sys.exit()`。

---

## 5. 违规与判负

| 情况 | 判定 | 处理 |
|---|---|---|
| 超过 `deadline_ms` 没给动作 | `timeout` | 违规 +1，杀进程；常驻模式下下回合自动重启 |
| 进程崩溃 / 提前退出 | `crash` | 违规 +1，同上 |
| stdout 出现非 JSON | `crash` | 违规 +1，同上 |
| `action` 为 `null` 或不可序列化 | `protocol` | 违规 +1（可配置重试一次） |
| 累计违规 ≥ 上限（默认 3） | — | **判负**：本局不再询问，游戏收到的永远是 `ok:false, error:"disqualified"` |

- 违规上限可在界面「违规上限」里调，设成 0 表示**一次即判负**。
- 语义层面的非法（比如出价超过预算、下了不合法的棋）**由游戏自己判定**——中转站不懂规则，它只保证格式、时限和进程健康。

---

## 6. 插件式游戏（Python）

不想单独起进程时，可以写成一个 Python 文件，由中转站直接驱动：

```python
class MyGame:
    name = "我的游戏"

    def run(self, ctx):                      # 必须是一个生成器
        for turn in range(1, 4):
            actions = yield ctx.ask({        # 发出询问，收回动作
                "p1": {"public": {...}, "private": {...}},
                "p2": {"public": {...}},
            })
            bid = (actions["p1"].get("action") or {}).get("bid")
            ctx.log(f"第 {turn} 回合，p1 出价 {bid}")

        return {"ranking": ["p1", "p2"], "scores": {"p1": 3, "p2": 1}}


def create_game():
    return MyGame()
```

`ctx` 提供：

| 成员 | 说明 |
|---|---|
| `ctx.players` | `[{"id": "p1", "name": "玩家1"}, ...]` |
| `ctx.player_ids` | `["p1", "p2", ...]` |
| `ctx.name_of(pid)` | 取玩家显示名 |
| `ctx.seed` / `ctx.random` | 种子与已播种的 `random.Random` |
| `ctx.config` | 界面上的游戏配置 |
| `ctx.ask(requests, deadline_ms=None)` | 构造一次询问，`turn` 自增 |
| `ctx.log(text, level="info")` | 写回合日志 |

**view 必须是纯 JSON 数据。** 即使玩家是模块式（和中转站同进程），view 也会先经过一次 JSON 序列化校验——
传 `datetime`、`set`、自定义对象都会报「view 无法序列化为 JSON」。这是为了让同一套游戏既能挂进程玩家也能挂模块玩家。

**返回 `GameResult` 对象和返回字典等价**：两种写法都会走同一套规范化（过滤幽灵 pid、非有限分数归零）。

`ctx.ask` 的 `requests` 支持三种写法：

```python
ctx.ask({"p1": view1, "p2": view2})                  # 字典：各自不同视野
ctx.ask(["p1", "p2"])                                # 列表：同一份空视野
ctx.ask([{"player": "p1", "view": v1}])              # 完整写法，可单独设 deadline
```

---

## 7. 模块式玩家（Python）

不写进程、直接一个函数：

```python
def setup(ctx):                       # 可选
    ctx.log("准备好了")

def decide(view, turn, ctx):          # 必须
    budget = view["private"]["budget"]
    return {"bid": min(budget, 30)}

def teardown(result, you):            # 可选
    pass
```

- 跑在中转站进程内，调试方便（`print` 也行）。
- 只能 Python；超时只能"放弃等待"，**无法真正杀掉**卡死的代码，所以重算法建议用进程式。
- 一旦某回合超时，这个玩家会被标记为不可信，**本局不再询问**（避免新线程和卡死的旧线程同时改模块全局变量），
  之后每回合按违规计，累计到上限判负。

---

## 8. 写程序前的检查清单

**写游戏时**

- [ ] stdout 只输出 JSON，一行一条，带 `"v": 1` 和 `"type"`
- [ ] 收到 `init` 回 `ready`，收到 `run` 才开始
- [ ] 每个 `ask` 里的 `view` 只包含该玩家该看的东西
- [ ] 能处理 `actions` 里的 `ok: false`（视为弃权）
- [ ] 结束时发 `over`，给出 `ranking` 和 `scores`
- [ ] 玩家数量变化时不崩（`players` 是动态的）

**写玩家时**

- [ ] 收到 `init` 回 `ready`
- [ ] 收到 `turn` 后必须在 `deadline_ms` 内回 `action`
- [ ] 只从 `view` 里取信息，别去猜别人的牌
- [ ] 状态可以留在内存里（常驻模式），但不要依赖它一定存在
- [ ] 收到 `over` 后退出

---

## 9. 版本

当前版本 `conterx/1`。后续如需扩展，会在消息里加字段而不改语义，旧程序继续可用。
