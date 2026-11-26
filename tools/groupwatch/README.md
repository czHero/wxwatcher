# groupwatch — 微信群情报审计推送

单台专用值守机上的常驻守护：只读监听班级群消息，两级模型（判定→提取）审计
关注事项（默认：作业），实时把【作业 · 群名】提醒推送到文件传输助手，到点再推
当日汇总。同一条消息只报一次（游标 + 去重）。

本文档面向第一次上手的操作者；部署步骤见 [`deploy/DEPLOY.md`](deploy/DEPLOY.md)，
内部机制设计见 `docs/superpowers/specs/2026-11-24-groupwatch-design.md`。

## 它是怎么跑起来的

```
微信(Linux) ── monitor.py 只读轮询 ──> ~/.wechat-monitor/messages.jsonl
                                              │ tail + 字节游标（重启续跑）
                                              v
                            groupwatchd run：攒批 → 判定模型 → 提取模型 → 情报入库
                                              │
                          实时推送（≤3 分钟）  ├── 每日汇总（digest.times 到点）
                                              v
                            tools/wechat-send/wxsend.py（GUI 自动化，只读复用）
                                              v
                                      文件传输助手（默认收件人）
```

三个进程各司其职：**监听端**读库出 JSONL，**本守护**理解内容，**发送端**动 GUI。
本守护自身不碰微信进程、不直接注入键鼠——一切外发都委托 wxsend 并遵守其
退出码契约（见下表）。

## 前提清单（逐项核对，缺一项就有一环断链）

1. **微信窗口最大化、前台、不遮挡、不锁屏**——推送时段多在无人值守的夜晚。
   已授权以桌面设置关闭息屏/锁屏/待机，见 `deploy/DEPLOY.md` 第 0 步；
   预检门仍保留兜底（被遮挡时整队 pending，绝不瞎发）。
2. **监听端在跑**：`systemctl --user status wechat-monitor.service` 应为 active；
   `groupwatch status` 里 jsonl 新鲜度超 5 分钟会报警。
3. **收件人在 roster 且有行坐标**：默认收件人 = 文件传输助手。roster/calib 的
   维护流程见 `tools/wechat-send/README.md`（本文不重复其校准细节）。
4. **数据库 keys 有效**：微信重新登录后 keys 轮换，监听端会读不到库——须按
   `tools/wechat-monitor/README.md`「Linux 版」流程重新捕获并重启 monitor。
5. **ydotool 注入可用**：wxsend 的点击/键入全走 `sudo ydotool`（本机为
   NOPASSWD sudoers 条目 + uinput 直连模式，无需 ydotoold 守护）；未配好时
   wxsend 会卡在要密码——前提与维护见 `tools/wechat-send/README.md` 运行前提第 5 条。
6. **拼音输入法已关闭**：GUI 注入期间拼音会吞键事件、打乱输入（`tools/wechat-send/README.md`
   运行前提第 4 条）。
7. **模型 keys 已填**：`~/.groupwatch/judge.key` / `extract.key` 内容仍是 init
   占位符（`FILL_ME_在此粘贴百炼APIKey`）时，`test-model` 会失败退 2——语义正确。
8. **`GROUPWATCH_HOME` 指向运行根**：两个 systemd 单元均已设
   `Environment=GROUPWATCH_HOME=%h/.groupwatch`；手动跑 CLI 不设置则默认同一位置。
9. **重启后自动恢复的前提 = 自动登录（可选）**：linger 只让 user 服务管理器
   开机即活，两单元仍要等图形会话才拉起——要求真·无人值守重启恢复需开 GDM
   自动登录（用户级决定）；否则登录后手动 `systemctl --user start wechat-monitor
   groupwatchd` 或等已 enable 的单元随会话自起。
10. **决策模型走 `{base_url}/systemone`**：`models.judge.api="systemone"` 时
    判定请求发往 /systemone（百炼决策模型协议，与 chat 端点同一把 key、同
    base_url）；`decision-model-preview` 只支持该协议——把它配成 chat 模型
    会在 /chat/completions 报 url error。`extract` 角色仍走 /chat/completions。
11. **群显示名来自 contact.db**：groups 列表与推送里的群名优先取
    contact.db `contact` 表的 `nick_name`（`contacts.py` 直查，守护启动时
    预载）。monitor 的 `load_maps` 有 name2id 先占坑缺陷（群名恒空），
    groupwatch 侧直接查 nick_name 绕开——若两者都取不到才回落裸
    `xxx@chatroom`。contact.db 或 keys 不可用时静默降级，不影响运行。
12. **正式使用前必须 `groupwatch enroll`**：首跑未 enroll 时预检基线缺失，
    默认 `push.require_enroll=true` 会把所有推送按 pre_fail 挡下（拒绝盲发，
    零 GUI 调用，整队留在 pending）；这是安全默认而非故障。

## config.json 关键字段（模型协议）

- `models.<role>.api`：`"chat"`（缺省，/chat/completions）| `"systemone"`
  （百炼决策模型 /systemone）。judge 用 `"systemone"` 时**逐条消息**构造
  noul 问题判定命中（`prompts/judge.md` 模板不参与该路径）；extract 恒用
  chat（qwen/deepseek 等常规模型）。坏值加载即报错点名角色。
- `limits.judge_threshold`：systemone 判定的命中阈值——回答概率 `noul ≥
  阈值`算命中（含等号），缺省 `0.5`；chat 判定路径忽略此键。联机实测标定
  （V2 部分相关问法，见 `audit._noul_questions`）：混杂真作业句 0.84 /
  噪声「测试」句 0.42 / 无关闲聊 0.0，推荐设 `0.7`。教训：旧「整体主题」
  式问法会把混排真消息压到 0.1 而噪声反得高分（0.72）。
- 提取相关性字段（`prompts/extract.md`）：每个命中项必须输出布尔 `relevant`，
  `false`＝原文对该项无实质可用信息，整条丢弃、不入库不推送（提取器否决权）；
  缺失或非布尔按审计失败处理——宁可漏报不误报。
- `push.require_enroll`：缺省 `true`——从未跑过 `groupwatch enroll`（无预检
  基线）时推送一律按 pre_fail 挡下、**零** wxsend 调用（拒绝盲发）；设
  `false` 恢复旧"放行带警告"行为（自担风险）。
- 其余字段（base_url/model/api_key_file、ingest/push/limits 各项）见设计
  文档 §5.2；init 模板已把上述旋钮写进产物，改后需 restart 生效。

## CLI 子命令面

全部子命令通过解释器绝对路径 + 入口脚本调用（下表 `GW` 即这条 export）：

```bash
export GW="/home/dgkj/.pyenv/versions/3.13.12/bin/python3 /home/dgkj/文档/wechat2/tools/groupwatch/groupwatch.py"
```

| 命令 | 用途 |
|---|---|
| `$GW init` | 建立 `~/.groupwatch/`（0700）带占位符的运行环境；拒绝覆盖已存在文件 |
| `$GW test-model judge` / `… extract` | 链路探针：真实配置 + 真实网络各发一条 |
| `$GW groups` | 列出全部群 `[订阅|未订阅] 群名 — id`，供编辑 groups.json |
| `$GW status` | 计数/游标/jsonl 新鲜度/推送流水/待推明细/汇总队列（待补发+待裁决）（只报告，不修复） |
| `$GW once` | 单轮 tick（`--dry-run` 不外发不联网） |
| `$GW run` | 常驻主循环（Ctrl-C 退出；生产用 systemd 单元） |
| `$GW enroll` | 截图采样写 probe_points.json（发送前预检基线） |

CLI 退出码只有两种：**0 成功/信息性报告，2 一切错误**（用法/配置/上游故障，
含未预期异常——顶层兜底成中文一行；**永不退 1**）。

## 推送状态与退出码表（本守护的处置，设计 §5.5）

每批推送开头过一次只读预检门（`probe_points.json` 像素断言），不过则**一条 GUI
命令都不发**、整队留在 pending，条件恢复后自动续流。运营前提：汇总/实时推送
时刻，微信须是最前置全屏窗口——预检采左栏图标特征色（F3-round3 实况 A/B：
纯 chrome 点位会被深色平面窗口巧合穿透），平面遮挡会被识破；聊天钮点位另
隐含微信停在聊天 tab 的约定（换 tab → pre_fail，零执行、可见、恢复后自动
重试）。wxsend 退出码的处置：

| wxsend 退出码 | 含义 | 本系统状态 | 自动重发？ |
|---|---|---|---|
| 0 | 送达，数据库回执已确认 | sent | — |
| 2a·exit 2 且前置拒发（零 GUI 命令执行） | row 缺失/微信未运行/keys 失效等 | pending 重试（计入 max_retries，耗尽转 suppressed，`status` 可查） | 是（此前零执行，安全） |
| 2b·exit 2 且含 `REFUSE: aborted at step`（GUI 已部分执行） | 会话可能已点开、输入框可能残留草稿 | **搁置**（hold：挂起禁重发，比照 exit 3） | **绝不**——人工核对 |
| 3（NEEDS_CHECK） | GUI 已执行、送达状态未知 | **搁置**（hold：挂起禁重发） | **绝不**——去手机核对 |
| 4（锁忙，120s 等待超时） | 有他人在发（或 wxsend 串行锁） | 让路，本轮停推 | 下轮重试，不消耗 max_retries |
| 被信号杀死（rc<0）或 wxsend 超时 >420s | 可能死在发送键之后、回执打印之前 | **搁置**（hold：挂起禁重发，比照 3） | 绝不 |
| 其它非 0/2/3/4（子进程没跑起来/解释器异常，零 GUI 执行） | crash，未触 GUI | pending 重试（同 2a 计额度） | 是（零执行，安全） |

> 记忆口诀：**0 过、2a 重试、2b/3 搁置、4 让路**。"搁置"是 `status` 打印的
> 状态名（代码内状态名 hold，即挂起禁重发）；搁置条目永不出现在任何自动重发
> 路径上；人工核对后可在 `home/out/realtime-<id>.txt` 找到渲染文本（文件按
> 情报行 id 身份命名）自行处置。

### 汇总（digest）推送的结果分级

每日汇总走同一条安全协议的队列版（旧"失败退位重试"措辞废除——歧义态绝不
自动重发）：到点且当日有情报的槽位先把渲染文本入 `digest_pending` 队列，
每 tick 至多补发队首一条，交付结果分级：

| push 返回 | 含义 | 队列处置 | 自动重发？ |
|---|---|---|---|
| `ok` | 送达，回执确认 | 定稿：出队 + 写 `digest_fired` | —（不再发） |
| `pre_fail` | 预检门挡住，wxsend 一条命令都没跑 | 留 `pending`，下 tick 续试（跨午夜不丢） | 是（零执行，安全） |
| `stopped:lock_busy` | 锁忙让路，未触 GUI | 留 `pending`，下 tick 续试 | 是（零执行，安全） |
| `stopped:needs_check` | GUI 已执行、送达状态未知（rc 3） | **`held`** 待人工裁决 | **绝不**——去手机核对 |
| `stopped:gui_partial` | GUI 部分执行（rc 2 aborted at step） | **`held`** 待人工裁决 | **绝不** |
| `stopped:crash` | 子进程异常——保守归入歧义（可能死在粘贴之后） | **`held`** 待人工裁决 | **绝不** |

`$GW status` 分两路显示：`未送达汇总 N 条`（pending，会自动续补）与
`待人工裁决汇总 M 条`（held，永不自动重发）。held 行保留渲染文本，按手机
核对结果二选一裁决（把 `D`/`H` 换成 status 点名的 day/hhmm）：

```bash
# ① 确认**已送达**：定稿——删队列行 + 记 digest_fired（与 ok 后代码走的同一路）
sqlite3 ~/.groupwatch/groupwatch.db \
  "DELETE FROM digest_pending WHERE day='D' AND hhmm='H'; \
   INSERT OR IGNORE INTO digest_fired(day,hhmm) VALUES('D','H');"

# ② 确认**没送达**且环境已修：重入队列，下个健康 tick 自动补发
sqlite3 ~/.groupwatch/groupwatch.db \
  "UPDATE digest_pending SET state='pending' WHERE day='D' AND hhmm='H';"
```

## 日常运维

```bash
systemctl --user status wechat-monitor.service groupwatchd.service   # 都在跑吗
journalctl --user -u groupwatchd.service -f                          # 实时看守护日志
$GW status                                                           # 计数/待推/流水
systemctl --user restart groupwatchd.service                         # 改配置后生效
systemctl --user stop groupwatchd.service wechat-monitor.service     # 停机（monitor 停会连带停 groupwatchd）
```

- 改了 `config.json`/`groups.json`/prompts 模板：需 restart（配置在启动时载入）。
- 改了单元文件：`deploy/` 里的只是模板——重拷进 `~/.config/systemd/user/` 并
  `systemctl --user daemon-reload`（详见 DEPLOY.md 第 7 步）。
- 出现搁置（hold）：`$GW status` 看"搁置/今日搁置"明细，人工核对微信（有没有
  发出/有无残留草稿）。补发时按 hold 行的 id 找 `out/realtime-<id>.txt`，
  人工在微信里用该文本发送（搁置条目永不自动重进 GUI）；确认已发出则直接
  清理输入框残留即可。
- `max_retries`（默认 3）耗尽的条目转 suppressed：不再自动推送，看 `$GW status`
  的"抑制"计数与"今日抑制"明细；每日汇总尾部带一行累计抑制点名
  （"另有累计 N 条情报被抑制（重试耗尽），跑 groupwatch status 查看"，
  设计 §5.5，N=0 时不出现）。

### 首跑：跳过历史（可选食谱）

先跑了 monitor（或手动 backfill）很久才启动 groupwatch 时，`messages.jsonl`
里已堆大量历史——首跑会把**全部**历史一次摄取进 inbox 再逐批审计：单次 tail
读取会把整段增量读进内存（攒批只限流审计节奏，不限流摄取），inbox 正文仓随
历史体量涨，模型调用量也翻倍。只想从此刻起看新消息的，先把游标推到 EOF：

```bash
stat -c %s ~/.wechat-monitor/messages.jsonl   # 记下这个字节数 N
sqlite3 ~/.groupwatch/groupwatch.db \
  "INSERT OR REPLACE INTO cursor(key, offset) VALUES('jsonl', N)"
```

（`groupwatch.db` 尚不存在时，先跑一次 `$GW once --dry-run` 建库再执行上面的
sqlite3；完成后 `$GW status` 的游标偏移应等于文件大小。反之，**要补历史**就
别用本食谱——接受一次性全量审计，内存紧时先调小 `batch_max_msgs`。）

### 已知限制

- 提醒时间显示为审计时刻，跨午夜消息可能进次日汇总（情报 `ts` 记的是审计
  入库时间而非消息原始时间；第二阶段随 M3 一并处理）。

## 测试

无网络、无 GUI、无子进程的单测：

```bash
cd tools/groupwatch && python3 -m unittest discover -s tests
```

联机验收清单见计划文档 Task 11；执行前通读 `deploy/DEPLOY.md` 与本文「前提清单」。
