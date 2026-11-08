# 璀璨宝石 Splendor · 接入说明

`examples/games/splendor.py` —— conterX 的上游游戏（进程式），支持 2~5 人。

## 一、怎么开一局

1. 打开 conterX →「选择游戏…」→ 选 `examples/games/splendor.py`，接入方式选「独立进程」
2. 玩家席位里挂上算法程序（示例：`examples/players/splendor_greedy.py`、`splendor_random.py`）
3. 单回合超时建议设 **3000~5000ms**（游戏默认请求 3000ms，席位设置更小则以席位为准）
4. 点「开始对局」

游戏配置（JSON，可留空）：

| 键 | 默认 | 说明 |
|---|---|---|
| `target_points` | 15 | 目标分数 |
| `max_take_colors` | 3（5 人局自动为 2） | 每回合最多拿几种不同颜色 |
| `token_limit` | 10 | 手牌宝石上限 |
| `deadline_ms` | 3000 | 请求给玩家的思考时间 |
| `verbose` | false | 每回合额外输出一张 ASCII 盘面 |
| `max_turns` | 400 | 手数硬上限，防死循环 |

## 二、规则（与官方一致）

- 宝石：2 人 4 枚/色、3 人 5 枚、4/5 人 7 枚；黄金恒 5 枚
- 贵族：人数 + 1 张（2 人 3 张…5 人 6 张），每张 3 分
- 每级发展卡洗混后翻 4 张，被买走立刻补
- 回合四选一：拿 3 枚异色 / 拿 2 枚同色（该色剩 ≥4）/ 预约（限 3 张，拿 1 黄金）/ 购买
- 回合结束时手牌宝石 >10 要弃到 10
- 满足贵族条件立刻来访，不占行动，每回合最多 1 张
- 有人达到 15 分后，**本轮跑完**再结算；同分时发展卡少者胜

> 5 人局是官方变体：宝石数量不变，但拿取指示物只能拿 2 种不同颜色。

## 三、视野（发给玩家的 view）

```json
{
  "public": {
    "turn": 37, "round": 13, "current": "p1",
    "target_points": 15, "final_round": false,
    "board": {
      "1": [{"id": "L1-26", "tier": 1, "points": 0, "bonus": "white",
             "cost": {"black": 1, "red": 1, "green": 1, "blue": 1}}, null, ...],
      "2": [...], "3": [...]
    },
    "decks": {"1": 21, "2": 14, "3": 8},
    "tokens": {"white": 3, "black": 5, "red": 2, "green": 4, "blue": 1, "gold": 2},
    "nobles": [{"id": "N03", "points": 3, "requires": {"red": 4, "green": 4}}],
    "players": [
      {"id": "p1", "name": "玩家1", "points": 9,
       "tokens": {"white": 2, "black": 0, "red": 1, "green": 2, "blue": 0},
       "gold": 1,
       "bonuses": {"white": 3, "black": 2, "red": 0, "green": 1, "blue": 1},
       "cards": 7, "reserved_count": 2, "nobles": ["N01"]}
    ],
    "token_limit": 10, "max_take_colors": 3
  },
  "private": {
    "player_id": "p1",
    "reserved": [{"id": "L3-07", "tier": 3, "points": 4, "bonus": "red",
                  "cost": {"black": 7}}],
    "legal_actions": [ ... ]
  }
}
```

要点：

- **对手的预约牌不可见**，只给 `reserved_count`；自己的预约牌在 `private.reserved` 里
- `board` 每级固定 4 个位置，牌堆空了的空位是 `null`
- `cost` 是**未扣加成**的原始费用；实际支付要减去自己的 `bonuses`
- `legal_actions` 是当前可执行的全部动作，直接挑一个发回去就一定合法

## 四、动作格式

```json
{"action": "take",    "gems": ["white", "blue", "red"]}
{"action": "take",    "gems": ["blue", "blue"]}
{"action": "take",    "gems": ["white","blue","red"], "return": {"white": 2}}
{"action": "reserve", "card": "L2-07"}
{"action": "reserve", "tier": 3, "from": "deck"}
{"action": "buy",     "card": "L2-07"}
{"action": "pass"}
```

- `take` 的 `gems`：3 种不同颜色（5 人局 2 种），或 2 个相同颜色
- `return`：可选。手牌超过 10 枚时指定弃哪些；不写则由游戏自动从最多的颜色开始弃
- `reserve` 用 `card` 指定场上明牌，或用 `tier` + `from:"deck"` 盲抽牌堆顶
- `buy` 的 `card` 既可以是场上明牌，也可以是自己预约的牌
- `pass` 只在没有任何合法动作时允许

非法动作的处理：**本回合作废**（不扣分、不判负），游戏会记一次 `illegal` 并在局末统计里给出。所以不确定时优先照抄 `legal_actions`。

## 五、卡牌编号

`L{等级}-{序号}`，例如 `L2-17` 是二级第 17 张。卡表在 `splendor_cards.json`，每张卡包含：

```json
{"id": "L3-01", "tier": 3, "type": "73", "points": 5,
 "bonus": "white", "cost": {"black": 7, "white": 3}}
```

`type` 是费用模式的中文简写（73 = 3 本色 + 7 顺临；单7 = 7 顺临；633、5333、单6、53、单5、421、332、322、单4、311、221、2111、22、1111、单3、21）。想改某张卡直接改这个 JSON，不用动代码。

颜色环：**白 → 黑 → 红 → 绿 → 蓝 → 白**（顺临 = 顺时针下一个）。

## 六、示例 bot

| 文件 | 说明 |
|---|---|
| `splendor_random.py` | 从 `legal_actions` 随机挑，基线陪练 |
| `splendor_greedy.py` | 启发式：优先高分牌、按目标卡攒色、会预约大牌 |

两个都是**基线水平**，对局通常要 25~30 轮才结束（真人 15~20 轮）。想跑出好看的局，把这两个换掉就行。
