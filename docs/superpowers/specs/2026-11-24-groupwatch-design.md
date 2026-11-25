# groupwatch：群消息审计推送管道 设计文档

日期：2026-11-24 · 状态：已批准（2026-11-24，用户批准全文并确认开机自启 linger=开） · 上游：无（新子系统；复用 wechat-monitor 与 wechat-send 成果）

## 1. 意图与背景

老师把作业、通知散落在班级群的流水话里，家长容易漏看。本系统用**一个微信账号**驻留在群中，按运营者配置的"关注内容"（如"老师布置的作业""调课/缴费通知"）**逐项审计群聊天记录**：先由廉价判定模型过滤无关消息，命中后才交给提取模型归纳、结构化，再以纯文本摘要推送给订阅者。

分两步走（用户决定）：

- **第一阶段（本文档范围）**：自己用。单台 Ubuntu、单微信账号、单收件人（默认发文件传输助手）。
- **第二阶段（本文档只留形状，不建）**：开放 SaaS——Web 管理端、用户添加机器人账号并拉群、订阅映射、动态收件人。

成功标准（第一阶段）：一条"明天数学第 5 页做完拍照"进群后，**默认配置下 ≤3 分钟**我的文件传输助手收到结构化提醒（预算 = monitor 轮询 30s + 攒批等待 60s + 两级模型 + 串行发送）；每晚再收到一份当日汇总；同一条作业永远只报一次。

## 2. 目标 / 非目标 / 反目标

**目标**

- 增量、断点续跑、幂等的消息审计管道（重启不重报、不漏报）。
- 两级模型：判定（默认百炼 decision-model-preview）→ 提取（默认常规大模型），base_url / model / key 全部可配置。
- 关注项、订阅群、推送节奏均为数据驱动的配置（JSON + 提示词模板文件），改配置不改代码。
- 复用现有资产：监听端以 monitor 的 JSONL 产物为输入；发送端以 wxsend CLI 的退出码契约为出口。

**非目标（第一阶段）**

- Web 界面、账号体系、多租户、计费、动态收件人——全部留给第二阶段。
- 主动加群/接受好友请求等新增 GUI 自动化（风险未评估，明确不做；机器人靠人工拉群）。
- 图片/文件/语音内容理解（监听端现只产出纯文本消息）。
- 多微信账号并发。

**反目标**（技术上成功但项目失败的形态）

- 把每条群消息都烧进提取模型的成本失控。
- 误报轰炸：同一条作业因重试/重启被推 N 遍。
- 发送环节出现任何 wxsend 明令禁止的行为（歧义状态自动重发、Enter 键路径、协议/hook 发送）。

## 3. 约束（沿袭既有安全协议，全部继承自 wxsend 设计）

1. 发送只走 wxsend GUI 自动化：不 hook 微信二进制、不用第三方协议；**exit 3（NEEDS_CHECK）永不自动重发**，人工处置。
2. wxsend 前提：微信窗口最大化、前台、目标会话在置顶区有固定行坐标（roster row）；第一阶段收件人固定为 roster 中已有条目（默认 `filehelper`），满足"固定小圈子"约束。
3. 群消息的取得是**只读解密本地数据库**（既有监听端能力），不产生任何可被风控的账号行为。
4. 百炼 API key 与提取原文出境到第三方模型：用户已知情同意（2026-11-24 对话）。key 存本机 0600 文件/环境变量，不进 git，日志不落 key。
5. 数据库加密 key 在微信重登后轮换（既有事实），管道对"读不到库"要安全退化而不是崩溃重报。
6. **wxsend 是只读复用边界**：本系统不修改 wxsend 源码（它是已评审、安全契约钉死的关键组件；改动要另立评审）。其锁等待硬编码 120s、无参数可配——groupwatch 不假装能配它，超时按 exit 4 状态机走。

## 4. 总体架构（方案 A：单机常驻守护进程）

已比较并否决：B 现在按 SaaS 骨架建（发送端本就全局锁串行，运维先行、两周不见产出）；C cron 胶水（状态续跑/重试/锁全靠互踩）。A 的核心逻辑是纯数据函数（输入消息批→输出结构化情报），第二阶段套 Web 壳是加法。

```
[微信Linux] --(已布防)--> monitor.py 常驻 ──追加──> ~/.wechat-monitor/messages.jsonl
                                                        │ tail+游标
                                                        v
                                            groupwatchd（本系统守护进程）
                              ┌────────────────────────────────────────┐
                              │ 摄取 → 按群过滤/攒批 → 判定模型(百炼)     │
                              │        → 命中批走提取模型 → 情报入库      │
                              │        → 实时推送 / 定时每日汇总          │
                              └────────────────────────────────────────┘
   ~/.groupwatch/{config.json, watchitems.json, groups.json, groupwatch.db, prompts/}
        │ 推送 = subprocess: wxsend.py --to <roster_key> --file <渲染文本>
        v
[文件传输助手 / 第二阶段:订阅用户]
```

## 5. 组件设计

### 5.1 摄取（ingest）

- 输入 = `~/.wechat-monitor/messages.jsonl` 追加流（monitor 已带 flock 常驻，记录含 `kind/conversation_id/conversation_name/sender_id/sender_name/text/ts/db/table/local_id`）。**不重复实现解密查询**。
- 自管游标 = 文件字节偏移，持久化于 groupwatch.db。**坏行语义**：monitor 每批结束才 fsync，"无换行结尾的残行"是写入中的正常中间态——游标按住不推进、下轮重读（宁可短暂停滞，不永久漏报）；仅"有换行但 JSON/必备字段非法"才跳过并计数（status 可见）。
- 只处理 `kind == "group"` 且 `conversation_id ∈ 订阅集` 的记录；其余丢弃前移动游标。
- 去重键 = `(db, table, local_id)`，入库时唯一约束兜底（monitor 重启回退重放也不会重复审计）。

### 5.2 配置布局（`~/.groupwatch/`，目录 0700，文件 0600）

- `config.json`：模型与节律。形状（示意）：

```json
{
  "models": {
    "judge":   {"base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                 "model": "decision-model-preview", "api_key_file": "judge.key"},
    "extract": {"base_url": "<百炼兼容端点>", "model": "<qwen 等常规模型>", "api_key_file": "extract.key"}
  },
  "ingest": {"jsonl": "~/.wechat-monitor/messages.jsonl", "poll_interval_s": 5,
              "batch_max_msgs": 40, "batch_max_wait_s": 60},
  "push": {"target_roster_key": "filehelper",
            "realtime": true,
            "digest": {"enabled": true, "times": ["20:30"]}},
  "limits": {"max_retries": 3, "push_max_per_tick": 10, "inbox_retention_days": 14}
}
```

- `watchitems.json`：`[{"id": "homework", "name": "老师布置的作业", "desc": "含科目、要求、截止时间；含"明天带剪刀"这类口头布置"}, ...]`——整表注入判定提示词；这也是第二阶段"管理端配置的正是这张表"的落点。
- `groups.json`：`{"subscribe": ["xxx@chatroom", ...]}`，群 id 由 `groupwatch groups` 列出来自手工挑选（第一阶段无 UI）。
- `prompts/judge.md`、`prompts/extract.md`、`prompts/digest.md`：提示词模板（`{watchitems}`、`{batch}`、`{hits}` 等占位符），外置可调。
- 判定/提取响应要求 JSON 输出；解析失败重试（计入 max_retries），最终失败记 `audit_failed` 状态并继续（坏批不阻塞游标，情报库留痕）。

### 5.3 审计引擎

- **判定调用**：一批（≤batch_max_msgs 条，按群分组攒批）消息编号列表 + 全部关注项 → 返回命中项 `[{item_id, msg_ids[], reason}]`。msg_ids 越界/未知 → 该批视为失败重试。
- **提取调用**：命中批中相关消息原文 + 同群前文若干条（上下文窗，默认 20 条）→ 每条情报 `{item_id, group, title, detail, deadline, action, excerpts[], source_keys[]}`（deadline 可空；excerpts 为原文摘录）。
- **去重**：情报按 `source_keys` 集合哈希入库唯一；同批重放不产生第二条。跨批的"老师补发同一条作业"不追求语义去重（第二阶段可加），接受小概率重复并在汇总里合并。
- 两模型均走 OpenAI 兼容 `POST {base_url}/chat/completions`（httpx，超时 + max_retries 指数退避；判定失败该批整体稍后重投，提取失败仅该命中项重投）。

### 5.4 情报库（`groupwatch.db`，SQLite）

表：`cursor(uniq key, offset)`、`messages_seen(db,table,local_id 主键)`、**`inbox(db,table,local_id 主键,ts,group,sender_id,sender_name,text)`（正文仓**——提取的"前文窗"与 excerpts 回溯的唯一来源；不靠回翻 messages.jsonl，上游文件不受本系统保留策略管辖）、`intelligence(id,ts,group,item_id,title,detail,deadline,action,source_hash 唯一,source_keys_json,raw_json,pushed:pending|sent|suppressed|hold)`、`dispatches(id,ts,kind:realtime|digest,status:ok|pre_fail|retry|exit2a|exit2b|exit3|exit4|error,detail)`、`audit_log(批级流水：ts,group,n_msgs,hits,status)`。
保留策略：`inbox`/`messages_seen` 按 `inbox_retention_days`（默认 14 天）删旧，游标未越过的不删；intelligence/dispatches/audit_log 不清（量级小）。正文仓仍是本机 0600，出境范围 = 提取那一批的原文，与约束 4 的同意一致。

### 5.5 推送（dispatch）

- 渲染纯文本（示意）：

```
【作业 · 三(2)班群】数学
明天完成课本第5页，拍照上传班级圈
截止：明天(11/25)晚上 · 来源：王老师 18:42
```

- **前台性预检门（发送前，只读观测）**：wxsend 的 focus 阶段是空操作（calib 里就是 `/usr/bin/true`），窗口被遮挡/锁屏时注入命令照样 rc=0，最后以 exit 3 收场——无人值守时段（正是 20:30 汇总的时间）这会持续制造挂起项。故 dispatch 每次调 wxsend 前，用现成截图能力核验微信前台可见性（对已知坐标点做像素断言，如发送按钮区的形态）；**预检不过 → 整条推送队列保持 pending（记 dispatch `pre_fail`），一条 GUI 命令都不发**，条件恢复后 pending 自动续流（pending 重推安全：此前零执行，不违反"歧义状态永不重发"）。预检本身是纯观测，不属于"新增 GUI 自动化"非目标。
- 实时推送 = `subprocess` 调 `tools/wechat-send/wxsend.py --to <key> --file <tmp>`，尊重其全局锁，**退出码状态机分型**：
  - `0` → sent。
  - `2` 且 stderr 无 GUI 步骤失败证据（前置拒发：row 缺失/微信未运行/密钥失效等，一条命令都没执行）→ `exit2a`，回 pending 下轮重试，计入 `max_retries`；耗尽 → suppressed（status 报警，汇总里带一句"有 N 条被抑制"）。
  - `2` 且已到 GUI 步骤（"step N failed" 类证据，输入框可能残留草稿）→ `exit2b`，**比照 exit 3 挂起**（标 hold + 草稿警告），绝不自动重进 GUI。
  - `3` → NEEDS_CHECK 挂起（`hold`），写"绝不自动重发，去手机上看微信"（同 wxsend 安全协议）。
  - `4`（锁等待 120s 超时，未执行）→ 等同一轮末尾重试，不计入 max_retries 消耗（锁竞争不是本条的错）。
- 每日汇总 = 到 `digest.times` 时刻，取当日全部情报按关注项分组渲染成一条消息，走同一 wxsend 通道；推送成功后把当日 realtime 条目在汇总中标注（汇总允许与实时重复，是有意冗余，不做抑制）。
- 多情报积压时实时推送逐条串行（wxsend 本就串行锁）；单轮最多推 N 条（默认 10）防轰炸，其余留到下轮。

### 5.6 进程与运维

- `groupwatchd run`：主循环（poll_interval_s tick），flock 单实例（`~/.groupwatch/groupwatchd.lock`）；`--once` 单轮（测试用）；`--dry-run` 全链路跑通但不调模型不发送（打印计划）。
- `groupwatch groups|init|status|run|once|test-model <judge|extract>`：CLI 子命令；`groups` 复用 monitor 的 `load_maps` 直接读 session/contact 库列群清单。
- 部署形态：**monitor 目前没有 systemd 单元、也不在跑**——monitor 与 groupwatchd 两个 user service 单元都是本项目的交付物（monitor 启动参数采默认 interval=30、不加 --groups-only（保持通用产物）、首跑是否 backfill 由 init 命令询问式文档指导）。`groupwatchd.service` 在 unit 层 Requires monitor 单元；开发期 `nohup` 亦可。
- **运营承诺（用户拍板：专用机模式）**：这台 Ubuntu 作为值守机，会话常开不锁屏、微信保持最大化前台。交付含部署步骤（README + 可重放的 setup 清单）：`loginctl enable-linger` 开机自启；关闭自动黑屏与锁屏——`gsettings set org.gnome.desktop.session idle-delay 0`、`gsettings set org.gnome.desktop.screensaver lock-enabled false`、`gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'`（用户已授权改这三项桌面设置）。5.5 预检门**保留**作兜底：人为临时遮挡/异常时走 pending 补发，不瞎发。

## 6. 第二阶段预留（只定形状，不实现）

- 数据模型已含 group/订阅维度：把 `groups.json` 换成 `subscriptions(user_id, group_id, item_ids)` 表即订阅映射。
- 发送通道是接口（现在实现 = wxsend 子进程）：SaaS 动态用户需换发送实现（候选：企业微信应用消息/公众号模板消息，绕开个人号 GUI），判定/提取/情报库原样复用。
- 管理端 = 对 `watchitems.json` + 订阅表的 CRUD 壳。
- 本文档不承诺上述任何选型，只保证核心逻辑不阻碍。

## 7. 测试与验收

- 单测（无网络、无 GUI）：游标推进/**残行按住**/非法行跳过计数/回退重放幂等；攒批切分；判定响应解析（越界 msg_ids、坏 JSON 重试路径）；去重唯一约束；渲染快照；**wxsend 退出码状态机全型**（2a 重试/2b 挂起/3 挂起/4 让路重试/预检不过整队 pending）；inbox 回溯与保留删除；digest 分组与时刻触发（假时钟）。
- 集成（假模型 HTTP + 假 wxsend runner，沿 wxsend 测试既有模式）：端到端一轮"消息进 JSONL → 库出情报 → 推送计划生成"。
- 联机验收（用户在场，规则沿袭 wxsend）：真实百炼 key 各调一次判定/提取（`test-model`）；真实群一条含作业语句的消息 → 文件传输助手收到实时提醒 + `groupwatch status` 显示 sent；重复重启进程不重报；晚间汇总到达。**测试发送仅限文件传输助手。**

## 8. 控制器已做的裁决（用户可逐条推翻）

| # | 裁决 | 理由 | 若错代价 |
|---|------|------|----------|
| 1 | 摄取用 tail monitor 的 JSONL，而非自写解密查询 | 零重复实现、复用已验证代码 | monitor 若停跑则数据断流；需文档+status 报警 |
| 2 | 第一阶段收件人 = roster 固定 key，默认 filehelper | 满足 wxsend"人工量坐标"前提 | 若要发给账号本人的手机微信（作好友），需为该好友加 roster 行（一次性人工操作） |
| 3 | 语义级跨批去重不做（只做 source_keys 精确去重） | 简单可靠；漏网重复在汇总中可合并 | 老师改述重发同一作业会收到两条实时提醒 |
| 4 | 汇总与实时有意冗余（同条作业最多见两次） | 汇总完整性优先 | 略唠叨 |
| 5 | systemd user service 为部署形态 | 本机已有 GUI 会话 | 未启用 lingering 时重启机器不自启；README 写明 |
| 6 | 判定模型失败=整批重投，提取失败=仅命中项重投 | 判定便宜、重投无害（幂等） | 高刷屏时段重投放大判定调用量（batch 上限压制） |
| 7 | 群订阅=CLI 列群 + 手工编辑 groups.json | 第一阶段无 UI 的合理替代 | 换群需动手改配置（低频） |
| 8 | **发送前加"前台性预检门"**（截图像素断言，纯观测；不过则整队 pending、零 GUI 命令） | wxsend focus 是空操作，遮挡/锁屏会 rc=0 假成功→exit3 挂起堆积，无人值守（恰是汇总时段）近乎不可用；预检是观测不是新自动化 | 断言点选错会误判 pending（假阴性）或放过遮挡（退回 exit3 老路）；阈值需联机调 |
| 9 | **exit 2 分型**：无 GUI 步骤证据→pending 重试（2a）；已到 GUI 步骤（可能残留草稿）→比照 exit3 挂起（2b） | 对 (b) 自动重试=每轮重复操控 GUI，违背"歧义不重发"精神 | 依赖 stderr 文案分类，wxsend 文案若演化需同步 |
| 10 | **新增 inbox 正文仓表**（本机存原文，retention 14 天） | 提取"前文 20 条"与 excerpts 回溯需要正文，但 messages.jsonl 不归本系统管、可能被清理/演化 | 本机留存更多群聊原文（磁盘/隐私面，权限 0600 与出境范围不变） |
| 11 | **残行按住不推进游标**（非残行才跳过计数） | monitor 每批才 fsync，截断是写入中间态；跳过=永久漏报，与"不漏报"冲突 | 病态残行会卡住该表摄取（需 status 报警可见） |
| 12 | **wxsend 定为只读边界，不改其源码**；`wxsend_lock_wait_s` 配置项砍掉（其锁硬编码 120s） | 它是已评审、安全契约钉死的组件；改动需另立评审，膨胀范围与风险 | 锁等待不可调（只能接受 120s 或 --nowait） |
| 13 | **monitor 与 groupwatchd 两个 systemd user 单元都是本项目交付物**（monitor 现无单元且未运行） | 摄取依赖 monitor 常驻，否则整链无源 | 新增交付面（unit 文件 + linger 文档） |
| 14 | **运营形态=专用值守机**（用户选 3）：不锁屏常开、微信最大化前台；交付含三条 gsettings（idle-delay/lock-enabled/sleep-inactive-ac）关闭自动息屏锁屏 | 无人值守推送时窗恰在人在场之外，遮挡/锁屏会让发送假成功堆积 exit3；预检门兜底但根本解是保持前台 | 改的是用户桌面设置（已授权）；预检断言点选错仍可能误判 |
| 15 | **`loginctl enable-linger` 开机自启=开**（用户 2026-11-24 明确确认"要开"） | 专用值守机重启后无人值守拉起全链 | 系统启动即开始读库+调模型+可能发送；停止需 `systemctl --user stop groupwatchd monitor` |

## 9. 交给计划阶段决定

任务拆分、模块文件布局（预计 `tools/groupwatch/`）、提示词具体文案、digest 时区细节、以及实现期任何接口微调。
