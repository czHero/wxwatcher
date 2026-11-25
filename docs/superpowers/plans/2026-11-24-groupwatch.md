# groupwatch 群消息审计推送管道 Implementation Plan

> **执行状态（2026-11-25 完成）：** Task 1-11 全部完成。逐任务评审 + 全分支终审（opus "With fixes" → 单修复波复审就绪）+ 验收期三处新增改动（双协议/群名解析/precision+gate）均独立复审放行。**Task 11 联机验收端到端通过并经用户手机侧确认真实送达**（真作业混排句→判定 0.84→提取 relevant→去重合并 1 条→filehelper 收到；"测试"噪声 0.42 被挡）。当前树 groupwatch 232/232 绿、wechat-send 69/69 未受影响、只读边界多次 git diff 证实零改动。详见 `2026-11-25-groupwatch-acceptance.md`。验收期暴露并根治：decision 模型实为 `/systemone` 非 chat；noul 需"部分相关"措辞否则漏报混排真消息；预检门改 fail-closed（require_enroll）。计划勘误见文末。
>
> **执行状态（2026-11-24 阶段）：** Task 1-10 完成，终审修复波 4 提交（715ecc0/401a610/048b6cf/df21365）。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans — this plan is designed for subagent-driven execution. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在第一阶段落地单机常驻群消息审计管道：tail monitor JSONL → 百炼判定模型过滤 → 提取模型归纳 → SQLite 情报库 → wxsend 实时推送 + 每日汇总（收件人=roster 固定 key）。

**Architecture:** 方案 A（设计文档已批准）：一个 Python 守护进程 `groupwatchd` 串起 摄取/审计/推送 三段，配置与状态全在 `~/.groupwatch/`；监听端以 `~/.wechat-monitor/messages.jsonl` 追加流为唯一摄入源（不重复解密）；发送端以子进程调 `tools/wechat-send/wxsend.py` 并严守其退出码契约（wxsend **只读，一行不改**）。

**Tech Stack:** Python 3.13（pyenv）· 标准库 sqlite3/argparse/fcntl · `requests` 2.32.5（OpenAI 兼容 chat/completions）· `PIL`(pillow 11.3，像素预检) · `gnome-screenshot`（已装）· unittest（**无 pytest**，沿 `tools/wechat-send` 扁平模块 + 全注入假件风格）。

**Spec:** `docs/superpowers/specs/2026-11-24-groupwatch-design.md`（已批准；执行者必读，本计划从其 §5 逐节展开，§8 裁决 15 条为绑定决定）

## Global Constraints

- **wxsend/monitor 只读**：`tools/wechat-send/**` 与 `tools/wechat-monitor/**` 一个字节都不改；复用仅通过 CLI 子进程（发送）与 sys.path 导入（groups 列表）。锁等待 120s 是 wxsend 硬编码事实，不得假装可配。
- **安全协议继承**（wxsend 设计 §3）：exit 3 与 GUI 部分执行后的 exit 2（stderr/stdout 含 `REFUSE: aborted at step`）**永不自动重发**；预检不过 = 零 GUI 命令；测试发送仅限 `filehelper`。
- 运行时根 `~/.groupwatch/`（env `GROUPWATCH_HOME` 覆盖），目录 0700、文件 0600；API key 只存 `*.key` 文件，永不进 git/日志/异常文本。
- 模型调用仅走 OpenAI 兼容 `POST {base_url}/chat/completions`；base_url/model/key 按角色（judge/extract）可配。judge 默认 `decision-model-preview` @ `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。
- 摄取残行语义：monitor 每批才 fsync——**无换行残行不消费、游标按住**；有换行但 JSON/字段非法才跳过计数。
- 消息去重键 `(db, table, local_id)`；情报去重 `source_hash = sha1(sorted(source_keys).join("\n"))` 唯一约束。
- 所有测试无网络/无子进程/无截图/无 GUI：HTTP、subprocess、screenshot、时钟、sleep 全部注入。测试目录 `tools/groupwatch/tests/`（含 `__init__.py`），跑法 `cd tools/groupwatch && python3 -m unittest discover -s tests -v`。
- 文本产物（README、提示词、推送文案、CLI 帮助）一律中文；提交信息英文 conventional commits。
- 时间一律本机本地时区；`HH:MM` 字符串解析。

## Review Focus

设计隐含、逐任务测试最可能漏的行为，每条在标注任务里有钉死测试：

1. **半写残行**（monitor 正在写这批 JSONL）→ 游标按住等补全，既不跳过丢消息也不报错（Task 3）。
2. **窗口被遮挡/锁屏** → 预检失败：本 tick 一条 wxsend 都不调，pending 全部保留，恢复后自动续发（Task 7）。
3. **monitor 重放旧消息**（同 db/table/local_id，如回退 watermark）→ 不再进 inbox、不再送审计、不会二次提醒（Task 3）。
4. **模型输出不合规**（坏 JSON / msg_ids 越界 / 未知 item_id）→ 批级重试 ≤max_retries 后记 `audit_failed`，游标照常推进，坏批永不阻塞管道（Task 5、8）。
5. **exit 2 二义性** → `REFUSE: aborted at step` 出现 = GUI 已动（可能残留草稿）→ 比照 3 挂起禁自动重试；没出现 = 纯前置拒发 → 消耗重试额度（Task 7）。

---

## File Structure

```
tools/groupwatch/
  config.py        # home/路径、config.json+watchitems.json+groups.json 加载校验
  store.py         # SQLite schema 与全部 CRUD（cursor/seen/inbox/intelligence/dispatches/audit_log/digest_fired）
  ingest.py        # tail JSONL（残行语义）、订阅过滤+去重入库、攒批缓冲
  modelclient.py   # chat/completions 封装（重试/退避/可注入 post）、extract_json
  audit.py         # judge_batch / extract_intel / source_hash（提示词渲染+响应校验）
  render.py        # 情报/汇总 → 纯文本消息
  dispatch.py      # 前台像素预检门、classify_wxsend、dispatch_pending 状态机
  watchd.py        # one_tick 管道、digest 定时触发、flock、run_forever、dry-run 装配
  cli.py           # init/groups/enroll/status/test-model/once/run 子命令
  groupwatch.py    # 入口（python3 tools/groupwatch/groupwatch.py <cmd>）
  prompts/         # judge.md extract.md digest.md 默认模板（init 拷贝给用户副本）
  deploy/          # wechat-monitor.service groupwatchd.service + DEPLOY.md
  tests/           # __init__.py + test_config/test_store/test_ingest/test_modelclient/test_audit/test_render/test_dispatch/test_watchd/test_cli
  README.md
```

模块间用扁平导入（`import store`），入口/测试以 `sys.path.insert(0, dirname)` 自举（沿 wechat-send 先例）。

---

### Task 1: config.py

**Files:**
- Create: `tools/groupwatch/config.py`
- Test: `tools/groupwatch/tests/test_config.py`

**Interfaces:**
- Consumes: 无（首任务）
- Produces: `DEFAULT_HOME: Path`；`ConfigError(Exception)`；dataclass `ModelRole(base_url: str, model: str, api_key: str)`、`IngestCfg(jsonl: Path, poll_interval_s: int, batch_max_msgs: int, batch_max_wait_s: int)`、`PushCfg(target_roster_key: str, realtime: bool, digest_enabled: bool, digest_times: tuple[str, ...], wxsend_py: Path)`、`Limits(max_retries: int, push_max_per_tick: int, inbox_retention_days: int, context_msgs: int)`、`Config(home: Path, models: dict[str, ModelRole], ingest: IngestCfg, push: PushCfg, limits: Limits, prompts_dir: Path)`；`WatchItem(id: str, name: str, desc: str)`；`load_config(home) -> Config`、`load_watchitems(home) -> tuple[WatchItem, ...]`、`load_groups(home) -> frozenset[str]`、`ensure_home(home) -> None`（mkdir 0700 parents）。`digest_times` 已按 `"HH:MM"` 升序校验；`wxsend_py` 默认 `<repo>/tools/wechat-send/wxsend.py`（config.py 内用 `Path(__file__).resolve().parents[2]` 推导）。

- [ ] **Step 1: 写失败测试** `tests/test_config.py`：
  - `test_load_config_ok`：tmp home 写全合法 config.json + `judge.key`（"secret"），断言 `cfg.models["judge"] == ModelRole("https://x/v1", "decision-model-preview", "secret")` 且 ingest/push/limits 各默认与显式值正确。
  - `test_load_config_missing_key_file`：`api_key_file` 指向不存在 → `ConfigError`，消息含 key 文件名、**不含**任何 key 内容。
  - `test_load_config_bad_digest_time`：`"25:70"` → ConfigError；`"7:5"` 也拒（必须两位）。
  - `test_load_config_bad_json_shape`（缺 models / 非 dict）→ ConfigError。
  - `test_load_watchitems_dup_id` → ConfigError；`test_load_watchitems_ok` 返回按文件序 tuple；`test_load_groups` 读 `{"subscribe": ["a@chatroom"]}` → frozenset；缺 `subscribe` 键 → 空集（未订阅是合法态，不是错误）。
  - `test_default_home_env_override`：`GROUPWATCH_HOME=/tmp/xx`（monkeypatch os.environ）→ `DEFAULT_HOME == Path("/tmp/xx")`。
  - `test_ensure_home_perms`：新建 home mode == 0o700。
- [ ] **Step 2: 跑测试确认失败**：`cd tools/groupwatch && python3 -m unittest tests.test_config -v` → ModuleNotFoundError/AttributeError。
- [ ] **Step 3: 实现 config.py**：dataclass 全部 frozen；api_key 读文件 `.strip()`，空文件视为缺失；`api_key_file` 相对路径以 home 为基。校验错误消息必须具体到字段名。
- [ ] **Step 4: 跑测试确认通过**（同上命令，全绿）。
- [ ] **Step 5: Commit** `git add tools/groupwatch && git commit -m "feat(groupwatch): config/watchitems/groups loaders"`

### Task 2: store.py

**Files:**
- Create: `tools/groupwatch/store.py`
- Test: `tools/groupwatch/tests/test_store.py`

**Interfaces:**
- Consumes: Task 1（仅 `Path` 约定，无代码依赖）
- Produces: `open_store(path: Path) -> Store`（幂等建表，`sqlite3.Row` row factory，dict 返回）；`Store` 方法：
  `cursor_get(key: str) -> int`（缺 0）/ `cursor_set(key: str, offset: int)`；
  `is_seen(db: str, table: str, local_id: int) -> bool` / `mark_seen(...)`；
  `insert_inbox(msg: dict)`（msg 键：db/table/local_id/ts:int/group/sender_name/text）；
  `recent(group: str, before_ts: int, limit: int) -> list[dict]`（同群 `ts < before_ts` 取最近 limit 条，**返回按 ts 升序**）；
  `insert_intelligence(row: dict) -> int | None`（row 键：group/item_id/title/detail/deadline/action/source_keys:list[str]→JSON 列/raw_json；`source_hash` UNIQUE 冲突 → None 且不写）；
  `pending(limit: int) -> list[dict]`（pushed='pending' 按 id）；
  `mark_status(intel_id: int, status: str)`（sent/hold/suppressed）；`bump_retry(intel_id: int) -> int`（retries+=1 并返回新值）；
  `day_intels(day: str) -> list[dict]`（date 本地 `YYYY-MM-DD` 匹配 ts）；
  `try_fire_digest(day: str, hhmm: str) -> bool`（INSERT OR IGNORE，真=首次）；
  `log_dispatch(kind: str, status: str, detail: str)`；`log_audit(group: str, n_msgs: int, hits: int, status: str)`；
  `recent_dispatches(n: int) -> list[dict]`；`counts() -> dict[str, int]`（键：inbox/intelligence/pending/hold/suppressed/dispatches）；
  `prune_inbox(now_ts: int, retention_days: int)`（删 inbox+messages_seen 中 `ts < now - retention*86400` 的行；intelligence 不动）。

- [ ] **Step 1: 写失败测试** `tests/test_store.py`（每方法 1-3 测，tmp sqlite 文件）：
  - `test_cursor_roundtrip`：set 42 → get 42；未设 get == 0。
  - `test_seen_roundtrip_and_inbox_idempotent`：同 (db,table,local_id) `insert_inbox` 两次 → inbox 恰 1 行（INSERT OR IGNORE）。
  - `test_insert_intelligence_unique_source_hash`：同 source_keys 集合（顺序不同）两次 → 第二次 None，表 1 行。
  - `test_recent_order_ascending_and_group_isolated`：插 a 群 3 条、b 群 2 条，`recent("a", before_ts=…, limit=2)` 返回 a 群最近 2 条且升序。
  - `test_pending_bump_retry_mark_status`；`test_try_fire_digest_once`；`test_counts_shape`；`test_prune_inbox_keeps_recent_and_intelligence`；`test_day_intels_filters_by_local_date`。
- [ ] **Step 2: 跑测试确认失败**（同 Task 1 模式）。
- [ ] **Step 3: 实现 store.py**：schema 常量 `SCHEMA_SQL` 含 7 张表：`cursor(key TEXT PRIMARY KEY, offset INTEGER)`、`messages_seen(db,table,local_id, PRIMARY KEY 三列)`、`inbox(db,table,local_id PRIMARY KEY, ts INTEGER, group TEXT, sender_name TEXT, text TEXT)`、`intelligence(id INTEGER PK AUTOINCREMENT, ts INTEGER, group TEXT, item_id TEXT, title TEXT, detail TEXT, deadline TEXT, action TEXT, source_keys TEXT, source_hash TEXT UNIQUE, raw_json TEXT, pushed TEXT DEFAULT 'pending', retries INTEGER DEFAULT 0)`、`dispatches(id PK, ts INTEGER, kind TEXT, status TEXT, detail TEXT)`、`audit_log(id PK, ts INTEGER, group TEXT, n_msgs INT, hits INT, status TEXT)`、`digest_fired(day TEXT, hhmm TEXT, PRIMARY KEY(day,hhmm))`。
- [ ] **Step 4: 跑测试确认通过**；再跑 `python3 -m unittest discover -s tests` 全绿。
- [ ] **Step 5: Commit** `"feat(groupwatch): sqlite state store"`

### Task 3: ingest.py

**Files:**
- Create: `tools/groupwatch/ingest.py`
- Test: `tools/groupwatch/tests/test_ingest.py`

**Interfaces:**
- Consumes: Task 2 `Store`（is_seen/mark_seen/insert_inbox）
- Produces:
  `TailResult(records: list[dict], new_offset: int, skipped: int)`；
  `tail_jsonl(path: Path, offset: int) -> TailResult` — 文件不存在 → `TailResult([], 0, 0)`；从 offset 读全部，只消费到最后一个 `\n`（含），残行**不消费**；坏 JSON 行与缺必备字段（`kind,db,table,local_id,conversation_id,text` 任一缺）→ skipped+1 且游标越过；
  `ingest_records(store: Store, groups: frozenset[str], records: list[dict]) -> list[dict]` — 只留 `kind=="group"` 且 `conversation_id in groups` 且 `not is_seen`；对每条接受者 `mark_seen` + `insert_inbox`（inbox `ts` 取 `int(rec["create_time"])`——monitor 的 `ts` 是 ISO 串、`create_time` 才是 epoch int，本计划全链路统一用 epoch）；返回接受记录（原 dict + 派生键 `source_key` = `f"{db}/{table}/{local_id}"`、`epoch` = 同上 int）。
  `Batch(group: str, msgs: list[dict])`；
  `class BatchBuffer(max_msgs: int, max_wait_s: int)`：`add(group: str, recs: list[dict], now: float) -> list[Batch]`（凑满即出，可出多批）、`flush_stale(now: float) -> list[Batch]`（等待 ≥ max_wait_s 的残余出批并清）；`pending_count() -> int`。

- [ ] **Step 1: 写失败测试** `tests/test_ingest.py`（tmp 文件 + tmp store）：
  - `test_tail_full_records_and_offset_advances`；
  - `test_tail_partial_last_line_is_held`（Review Focus 1）：文件尾无换行的 JSON 前缀 → 该行**不**进 records，`new_offset` 停在最后换行处；追加剩余后二次 tail 完整收到该记录且 skipped==0；
  - `test_tail_bad_line_skipped_cursor_moves`：中间一行 `not-json\n` → skipped==1，其余照常；
  - `test_tail_missing_file`；
  - `test_ingest_records_filter_dedupe_and_inbox`（Review Focus 3）：非订阅群/单聊/重复 (db,table,local_id) 各一条 + 合法一条 → 只返回 1 条；再喂同记录 → 返回 []；store.counts()["inbox"]==1；
  - `test_buffer_flush_by_size`、`test_buffer_flush_stale_by_time`（假 now 步进）、`test_buffer_groups_isolated`。
- [ ] **Step 2: 跑测试确认失败** → **Step 3: 实现**（tail 用 `open(path,'rb')`+seek+decode utf-8 errors=replace 逐行 json.loads）→ **Step 4: 通过** → **Step 5: Commit** `"feat(groupwatch): jsonl tail ingest with partial-line hold and batching"`

### Task 4: modelclient.py

**Files:**
- Create: `tools/groupwatch/modelclient.py`
- Test: `tools/groupwatch/tests/test_modelclient.py`

**Interfaces:**
- Consumes: Task 1 `ModelRole`
- Produces: `ModelError(Exception)`（属性 `retryable: bool`）；
  `chat(role: ModelRole, system: str, user: str, *, timeout_s: int = 60, retries: int = 3, backoff_base_s: float = 1.0, sleep: Callable[[float], None] = time.sleep, post: Callable[..., Any] = requests.post) -> str` — POST `{base_url}/chat/completions`，json=`{"model": role.model, "messages": [{"role":"system","content":system},{"role":"user","content":user}], "temperature": 0}`，header `Authorization: Bearer {api_key}`；成功取 `data["choices"][0]["message"]["content"]`（缺结构 → retryable ModelError）；429/5xx/超时/连接错误重试（退避 `backoff_base_s * 2**attempt`，retries 次后 raise 不可重试版）；4xx（除 429）立即 `ModelError(retryable=False)`；任何异常/日志文本**不得包含 api_key**；返回 content。
  `extract_json(text: str) -> Any` — 剥 ```json/``` 围栏；从首个 `{` 或 `[` 起 `json.JSONDecoder().raw_decode`（容忍尾缀闲话）；失败 `raise ValueError`。

- [ ] **Step 1: 写失败测试** `tests/test_modelclient.py`（post 注入假函数，记录调用序列）：
  - `test_chat_success_returns_content_and_payload_shape`（断言 URL、Bearer 头、messages、temperature 0）；
  - `test_chat_retries_on_timeout_then_ok`（假 sleep 记录 [1.0, 2.0]）；
  - `test_chat_gives_up_after_retries_raises_not_retryable`；
  - `test_chat_400_immediate_fail`（假 post 返回 status_code=400，只调 1 次）；
  - `test_chat_error_message_never_contains_key`（异常 str 不含 "secret"）；
  - `test_extract_json_plain/fenced/trailing_prose/nested/raises_on_garbage`。
- [ ] **Step 2 失败 → Step 3 实现 → Step 4 通过 → Step 5 Commit** `"feat(groupwatch): openai-compatible model client with json extraction"`

### Task 5: audit.py

**Files:**
- Create: `tools/groupwatch/audit.py`, `tools/groupwatch/prompts/judge.md`, `tools/groupwatch/prompts/extract.md`
- Test: `tests/test_audit.py`

**Interfaces:**
- Consumes: Task 1（`ModelRole`/`WatchItem`）、Task 3（`Batch`）、Task 4（`chat`/`extract_json`/`ModelError`）
- Produces: `AuditError(Exception)`；`Hit(item_id: str, msg_ids: tuple[int, ...], reason: str)`；`Intel(item_id: str, title: str, detail: str, deadline: str | None, action: str | None, excerpts: tuple[str, ...], source_keys: tuple[str, ...])`；
  `source_hash(keys: Sequence[str]) -> str`（sha1 hex of `"\n".join(sorted(keys))`）；
  `render_judge_prompt(tpl: str, items: Sequence[WatchItem], batch: Batch) -> str`（占位符 `{watchitems}` `{group}` `{batch}`；batch 行格式 `[#i][ts] sender_name: text`，i 从 0）；
  `render_extract_prompt(tpl, items, batch: Batch, hits: Sequence[Hit], context: Sequence[dict]) -> str`（追加 `{hits}` 与 `{context}` 块）；
  `split_prompt(tpl: str) -> tuple[str, str]`（按 `\n===\n` 拆 system/user 两段；缺分隔符 → ConfigError 转 AuditError）；
  `judge_batch(role, tpl, items, batch, *, chat=chat, retries=3) -> list[Hit]`；
  `extract_intel(role, tpl, items, batch, hits, context: Sequence[dict], *, chat=chat, retries=3) -> list[Intel]`；
  `retryable_audit(fn: Callable[[], Any], *, retries: int, sleep=time.sleep) -> Any`——对 `ModelError|AuditError|ValueError` 重试 retries 次，耗尽抛 `AuditFailed(Exception)`（watchd 捕获记 audit_failed）。

- [ ] **Step 1: 写失败测试** `tests/test_audit.py`（chat 注入假函数返回固定字符串）：
  - `test_judge_parses_valid_hits_and_sorts_dedupes_msg_ids`；
  - `test_judge_rejects_unknown_item_id`（AuditError）/ `test_judge_rejects_out_of_range_msg_id`（Review Focus 4）/ `test_judge_rejects_non_json`；
  - `test_retryable_audit_gives_up_after_n`（假 sleep，调用计数 == retries+1）；
  - `test_extract_maps_source_keys_from_hit_msg_ids`（hit 的 msg_ids → batch 内记录的 `source_key`）；
  - `test_extract_rejects_empty_title_or_detail`；
  - `test_source_hash_order_insensitive`。
- [ ] **Step 2 失败** → **Step 3 实现 + 写默认模板**：
  `prompts/judge.md`（全文即成品，中文，system/user 两段以 `\n===\n` 分隔，audit.py 的 `split_prompt(tpl)` 拆）：
  ```
  你是消息过滤器。给定「关注项列表」和一批群消息，逐条判断哪些消息命中哪些关注项。只依据消息原文，不臆测。输出纯 JSON，无其他文字。
  ===
  关注项列表：
  {watchitems}
  群：{group}
  消息批次：
  {batch}
  输出 JSON：{"hits": [{"item_id": "<关注项id>", "msg_ids": [<整数>], "reason": "<一句话>"}]}。没有命中就 {"hits": []}。
  ```
  `prompts/extract.md` 同风格：输入含 {context}（前文参考）、{batch}、{hits}（已命中项+msg_ids），要求输出 `{"items": [{"item_id","title","detail","deadline","action","excerpts":[…]}]}`，deadline 为原文中的时间描述或 null，excerpts 必须逐字摘自原文。
- [ ] **Step 4 通过 → Step 5 Commit** `"feat(groupwatch): judge/extract audit stage with default prompts"`

### Task 6: render.py

**Files:**
- Create: `tools/groupwatch/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: store 的 intelligence dict（键见 Task 2）
- Produces:
  `render_intel(row: dict, item_name: str, group_name: str) -> str`；
  `render_digest(day: str, rows: list[dict], item_names: dict[str, str], group_names: dict[str, str]) -> str | None`（空 rows → None）。

- [ ] **Step 1: 写失败测试**：
  - `test_render_intel_realtime_shape`：断言输出恰为三行式（首行 `【{item_name} · {group_name}】{title}`；正文 detail；`deadline` 非空才出 `截止：…` 行；末行 `来源：{sender 或群} · {HH:MM}`——ts→`%H:%M` 本机时区）；无 deadline 时无该行；
  - `test_render_intel_multiline_detail_preserved`；
  - `test_render_digest_groups_by_item_then_time`（两个 item、三条情报 → 分组标题按 item_name，条目序按 ts）；`test_render_digest_empty_none`；`test_render_digest_header_has_date`（首行含 `{day} 汇总`）。
- [ ] **Step 2 失败 → Step 3 实现（纯字符串，无逻辑分支藏身）→ Step 4 通过 → Step 5 Commit** `"feat(groupwatch): plain-text renderers for realtime and digest"`

### Task 7: dispatch.py

**Files:**
- Create: `tools/groupwatch/dispatch.py`
- Test: `tests/test_dispatch.py`

**Interfaces:**
- Consumes: Task 1（`Config`/`PushCfg`/`Limits`）、Task 2（`Store`）、Task 6（renderers）
- Produces:
  `PROBE_POINTS: tuple[dict, ...]` = `({"name": t, "x": X, "y": Y} for t in 标题带(60,40)/侧栏(60,500)/输入区(700,880)/发送钮(1391,866))`（1440×900 全屏微信坐标系）；
  `pixel_probe(png: Path, x: int, y: int) -> tuple[int, int, int]`（PIL open→convert("RGB")→getpixel）；
  `load_probe_baseline(home: Path) -> list[dict] | None`（读 `probe_points.json`：`[{"name","x","y","r","g","b","tol"}]`；文件缺失 → None）；
  `foreground_ok(home, *, shot: Callable[[Path], None], now: float = time.time) -> tuple[bool, str]`——baseline None → `(True, "预检未配置，跳过（建议跑 groupwatch enroll）")`；否则 gnome-screenshot 到 `home/probe.png`（shot 默认 `subprocess.run(["gnome-screenshot","-f",str(png)], capture, timeout=15)`，截图失败/超时 → `(False, "截图失败")`），逐点 `abs(cur-cached) <= tol` 全真才 `(True, "ok")`，否则 `(False, f"点位 {name} 不匹配")`；
  `classify_wxsend(rc: int, output: str) -> str`——0→`sent`；2 且含 `"REFUSE: aborted at step"`→`gui_partial`；2→`pre_refuse`；3→`needs_check`；4→`lock_busy`；其余→`crash`；
  `push_texts(cfg: Config, store: Store, jobs: list[tuple[str, str]], *, kind: str, shot=default_shot, run=subprocess.run, now=time.time) -> str`——jobs 为 `[(intel_id|None 用 kind=digest 时忽略, text)]`，返回本批结束态 `ok|pre_fail|stopped:<status>`；行为：一次 `foreground_ok`（不过→`log_dispatch(kind,"pre_fail",原因)`→返回 pre_fail，零 wxsend）；每条 job 写 `home/out/<n>.txt`（0600，`os.open O_WRONLY|O_CREAT|O_TRUNC` 后 chmod）→ `argv=[sys.executable, str(cfg.push.wxsend_py), "--to", cfg.push.target_roster_key, "--file", <path>]` → `run(argv, capture_output=True, text=True, timeout=420)` → classify 迁移：
  - sent → `mark_status(id,"sent")` + log_dispatch("ok")
  - pre_refuse → `bump_retry`：新值 ≥ `limits.max_retries` → suppressed+log，否则保持 pending；**中止本批**（环境坏了别连撞）返回 stopped:pre_refuse
  - gui_partial / needs_check → `mark_status(id,"hold")` + log；中止本批（Review Focus 5：绝不自动重发）
  - lock_busy → 保持 pending，**不 bump_retry**，中止本批
  - crash → 同 pre_refuse 计额度
  `run` 返回对象 rc 属性缺失/抛 OSError → 按 crash。

- [ ] **Step 1: 写失败测试** `tests/test_dispatch.py`（tmp home；假 shot 写死一个 1×1 png 文件或返回布尔的替身——`foreground_ok` 的 shot 注入口径以测试为准：测试直接 monkeypatch `dispatch.pixel_probe` 返回值与 `load_probe_baseline`）：
  - `test_classify_wxsend_six_way`（含 "REFUSE: aborted at step" 的 rc2 → gui_partial；普通 REFUSE rc2 → pre_refuse）；
  - `test_push_texts_zero_wxsend_calls_on_precheck_fail`（Review Focus 2：假 run 调用计数 ==0，pending 原样，dispatches 有 pre_fail 行）；
  - `test_push_texts_success_marks_sent_and_tmp_0600`（os.stat mode & 0o077 == 0o600）；
  - `test_push_texts_needs_check_holds_and_stops_batch`；
  - `test_push_texts_gui_partial_holds_not_retried`；
  - `test_push_texts_pre_refuse_bumps_then_suppresses_at_cap`；
  - `test_push_texts_lock_busy_keeps_pending_without_retry_bump`；
  - `test_push_texts_respects_push_max_per_tick`（11 pending → 单批 ≤ cfg 上限）；
  - `test_foreground_ok_baseline_missing_passthrough_with_warning`；`test_foreground_ok_tolerance_math`（含边界 tol；注意实现比较用逐通道 abs≤tol）。
- [ ] **Step 2 失败 → Step 3 实现 → Step 4 通过 → Step 5 Commit** `"feat(groupwatch): dispatch gate, wxsend exit-code state machine, batch policy"`

### Task 8: watchd.py

**Files:**
- Create: `tools/groupwatch/watchd.py`
- Test: `tests/test_watchd.py`

**Interfaces:**
- Consumes: Task 1-7 全部
- Produces:
  `Deps(cfg: Config, store: Store, items: tuple[WatchItem, ...], groups: frozenset[str], buf: BatchBuffer, judge_tpl: str, extract_tpl: str, chat: Callable, push: Callable[[list[tuple], str], str], now: Callable[[], float], group_names: dict[str, str])`（digest 渲染为纯代码 render_digest，无 digest 模板字段）；
  `build_deps(home: Path, *, dry_run: bool = False, chat=None, push=None, now=time.time) -> Deps`——dry_run：chat 返回 `{"hits": []}` 恒空、push 只 log_dispatch("dry")；非 dry_run 用 modelclient.chat 与 dispatch.push_texts 真装配（group_names 由 store 内 inbox group 字段回退 group id；enroll 无关系）；
  `one_tick(deps: Deps) -> dict`——`tail_jsonl → ingest_records → buf.add → 对每个就绪 Batch:` `context = store.recent(batch.group, before_ts=batch 首条 epoch, limit=cfg.limits.context_msgs)` → `retryable_audit(judge_batch)`（AuditFailed → log_audit(status="audit_failed") 继续）；有 hits → `retryable_audit(lambda: extract_intel(..., context))`（失败同理 status="extract_failed"）；每 Intel `store.insert_intelligence`（None=重复 → 计 dup）；成功后 log_audit(hits=n)；tail 的 `skipped` 累进 `cursor_set("bad_lines", cursor_get("bad_lines")+skipped)`（坏行计数持久可见，Review Focus 配套）；新 intel 且 cfg.push.realtime → 取 `store.pending(cfg.limits.push_max_per_tick)` 渲染 jobs → `deps.push(jobs, "realtime")`；digest 检查：`now` 的本地日期 day，对每个 `t in digest_times`：`now >= 当日t 且 try_fire_digest(day,t) 且 store.day_intels(day)` 非空 → render_digest → push(kind="digest")；返回 `{"ingested":N,"batches":N,"hits":N,"intel_new":N,"push":<状态>,"pre":<bool 或 None>}`；
  `acquire_lock(home: Path)`——flock LOCK_EX|LOCK_NB on `home/groupwatchd.lock`，失败 `SystemExit("另一 groupwatchd 在跑")`；
  `run_forever(deps, *, sleep=time.sleep, ticks: int | None = None)`——ticks 限定轮数（测试用），每 tick 间隔 `cfg.ingest.poll_interval_s`，KeyboardInterrupt 干净退出 0；每 tick 异常兜底打印继续下轮（单 tick 崩不杀守护）。

- [ ] **Step 1: 写失败测试** `tests/test_watchd.py`（真 store(tmp)+真 ingest；chat 假、push 假、时钟假）：
  - `test_one_tick_end_to_end_dry`：jsonl 灌 3 条（1 订阅群含作业句）→ 假 judge 返回该消息命中、假 extract 返回 1 条情报 → 断言 `result == {"ingested":3,"batches":1,"hits":1,"intel_new":1,...}` 且 pending==1、push 假件收到 1 job kind=realtime；
  - `test_replay_after_crash_no_duplicate_intel`（Review Focus：kill 场景 = 游标重置回 0 重放同一 jsonl + 另一进程重启后 tick）：第二次 `intel_new==0`，push 无新 job；
  - `test_audit_failed_does_not_block_cursor`（Review Focus 4：chat 一直抛异常 → audit_failed，游标推进，下批继续）；
  - `test_digest_fires_once_per_day_per_time`（假 now 越过 20:30 两次 tick → 只推一次 digest；day_intels 空那天不推不占 fire）；
  - `test_realtime_disabled_still_stores`（cfg.push.realtime=false → 入库零推送）；
  - `test_lock_second_instance_exits`。
- [ ] **Step 2 失败 → Step 3 实现 → Step 4 通过（含全套 discover）→ Step 5 Commit** `"feat(groupwatch): tick pipeline, digest scheduling, lock"`

### Task 9: cli.py + 入口 + prompts/digest 默认

**Files:**
- Create: `tools/groupwatch/cli.py`, `tools/groupwatch/groupwatch.py`, `tools/groupwatch/prompts/digest.md`
- Modify: 无
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1-8
- Produces: `main(argv: list[str] | None = None) -> int`；子命令：
  - `init`：建 home 0700 + 写**带占位符模板**的 config.json（`<在此填百炼 base_url>` 等；`api_key_file: "judge.key"`）、watchitems.json（内置 homework 一条示例）、groups.json `{"subscribe": []}`、`judge.key`/`extract.key` 占位内容 `FILL_ME_在此粘贴百炼APIKey`（0600——保证 init 产物立即可过 load_config 校验；test-model 对占位 key 真调用会失败退 2，语义正确）、`prompts/` 三件套拷贝（repo 默认→home）；目标文件已存在 → 逐个拒绝覆盖并 exit 2。
  - `groups`：`sys.path.insert(0, <repo>/tools/wechat-monitor)` 后 import monitor 的 `choose_db_storage/discover_message_dbs/load_key_material/load_maps`，打印 `[订阅|未订阅] name — id` 表；任何上游缺失（目录/keys/monitor 不在跑导致锁不到）→ 中文指引错误 exit 2，**exit 1 永不出现**。
  - `enroll`：`gnome-screenshot -f <home>/enroll.png`（子进程注入可测）→ 逐 `PROBE_POINTS` pixel_probe → 写 `probe_points.json`（tol 默认 28，字段 r/g/b/tol）；先询问确认微信在前台无遮挡（非 `--yes` 且 stdin 非 tty → exit 2 提示加 --yes）。
  - `status`：print counts、cursor、`bad_lines` 计数、jsonl 新鲜度（`mtime` 距今 >5 分钟 → 中文警告"监听端疑似停跑"）、`recent_dispatches(8)`、pending/hold/suppressed 明细行、config 是否可加载（坏则打 ConfigError 消息）。
  - `test-model judge|extract`：对真实配置发一条 `"回复 OK"` 探针，stdout 打印响应前 200 字符；`ModelError` → exit 2。
  - `once [--dry-run]` / `run [--dry-run]`：build_deps→one_tick / run_forever；输出 one_tick 摘要 JSON 行。
  `groupwatch.py` 入口：`sys.path` 自举 + `sys.exit(main())`。
- [ ] **Step 1: 写失败测试** `tests/test_cli.py`（全部子命令走 tmp home；groups/enroll/test-model/run 的上游全部 monkeypatch 模块属性：`cli._list_groups_impl`、`dispatch.pixel_probe`、`modelclient.chat`、`watchd.build_deps`）：
  - `test_init_writes_placeholders_and_chmods_0600_refuses_overwrite`；
  - `test_init_created_config_loads_and_reports_placeholder`（load_config 应成功——占位符只在值里，不在结构上；base_url 占位符允许任何非空字符串）；
  - `test_groups_prints_subscription_markers`（假 _list_groups_impl 返回 2 群，groups.json 订了 1）；
  - `test_groups_failure_maps_to_exit2_with_hint`（impl 抛 OSError → exit 2，输出含"监听端/keys"字样）；
  - `test_enroll_writes_baseline_points_file`（假 shot+假 pixel_probe 固定色 → json 含 4 点 + tol）；
  - `test_status_shape_after_seed`（灌 store 再 status，捕获 stdout 含 counts 行）；
  - `test_test_model_ok_and_fail_paths`。
- [ ] **Step 2 失败 → Step 3 实现（digest.md 默认模板：`你是汇总员。把当日情报条目写成一条晚间汇总…===\n{date}\n{items}`——digest 实际渲染由 render_digest 做，模板仅在"LLM 润色版汇总"未来启用，故 digest.md 只入 init 拷贝清单，不被运行时调用——**若嫌 YAGNI 可整删 digest.md**，本任务默认保留占位三件套与 §5.2 一致）→ Step 4 通过 → Step 5 Commit** `"feat(groupwatch): cli surface (init/groups/enroll/status/test-model/run)"`

### Task 10: 部署件 + README

**Files:**
- Create: `tools/groupwatch/deploy/wechat-monitor.service`、`tools/groupwatch/deploy/groupwatchd.service`、`tools/groupwatch/deploy/DEPLOY.md`、`tools/groupwatch/README.md`
- Test: `tests/test_deploy_files.py`

**Interfaces:**
- Consumes: Task 1-9 成品路径
- Produces: 无代码接口；交付文件本身。

- [ ] **Step 1: 写失败测试** `tests/test_deploy_files.py`（读文件文本断言，最小机械防回归）：
  - 两 unit 文件均含 `[Service]`、绝对 `ExecStart=`（含 `groupwatch.py run` / `monitor.py` 字样）、`Restart=on-failure`、`After=graphical-session.target`+`BindsTo=graphical-session.target`（GUI 自动化必须等图形会话——lingering 开机自启但不早于登录会话）；groupwatchd 含 `Requires=wechat-monitor.service`；
  - DEPLOY.md 含四条命令原文：`loginctl enable-linger` 与三条已授权 `gsettings set ...`（idle-delay 0 / lock-enabled false / sleep-inactive-ac-type nothing）+ `systemctl --user enable --now` 两行；
  - README.md 含「前提清单」（微信最大化前台不锁屏、monitor 在跑、roster row、keys 轮换重捕、GROUPWATCH_HOME）与退出码表（0/2/3/4→本系统状态映射，含 2b 挂起禁重发字样）。
- [ ] **Step 2 失败 → Step 3: 写四文件**（unit 里 `Environment=GROUPWATCH_HOME=%h/.groupwatch`、WorkingDirectory 绝对 repo 路径；monitor 单元 ExecStart 用默认参数 `python3 …/monitor.py --interval 30`；DEPLOY.md 步骤有序：init → 填 key → test-model → groups → 编辑 groups.json → enroll → enable units）→ **Step 4 通过** → **Step 5 Commit** `"feat(groupwatch): systemd user units, deploy guide, readme"`

### Task 11: 联机验收（用户在场，最后执行）

**Files:** 无代码；产物 = `docs/superpowers/plans/2026-11-24-groupwatch-acceptance.md`（验收记录，勾选清单）。

**Interfaces:** Consumes: 全部。

- [ ] **Step 1:** `groupwatch init` 落真配置：judge/extract base_url+model 填百炼 token-plan 端点；用户把两个 API key 亲笔写入 `~/.groupwatch/*.key`（**不经对话/日志**），`chmod 600`。
- [ ] **Step 2:** `test-model judge` 与 `test-model extract` 各一次 → 均 exit 0 且有响应文本。
- [ ] **Step 3:** `groupwatch groups` 列出真实群；用户点名 1 个测试群写入 groups.json；确认 monitor 已在跑（`systemctl --user start wechat-monitor` 或 nohup）。
- [ ] **Step 4:** `enroll` 建预检基线；随即人为遮挡微信（开一个全窗口盖住）跑 `once` → dispatches 出现 `pre_fail`、零 wxsend 调用（日志佐证）。撤遮挡再跑 → 正常。
- [ ] **Step 5:** 用户（或假手）在测试群发一句含作业语义的中文消息；≤3 分钟内文件传输助手收到【作业 · 群名】实时提醒；`status` 显示 intel 1 条 sent、audit_log 1 批 hits=1。
- [ ] **Step 6:** 把 digest.times 临时设为 5 分钟后的 HH:MM → 等到点后收到当日汇总一条；恢复 20:30。
- [ ] **Step 7:** 重启 `groupwatchd`（systemctl --user restart）→ 重放零新情报、零重复推送（游标+seen 生效实证）。
- [ ] **Step 8:** 验收记录文件落盘（每步命令+观察结果），commit `"docs(groupwatch): live acceptance record"`。

**联机红线（全程有效）：** 测试发送仅限 filehelper；任何 NEEDS_CHECK/挂起 → 停下人工核对，禁止自动重发；出现 phantom 字符/误发迹象立即停止并报告。

---

## 完成定义

`cd tools/groupwatch && python3 -m unittest discover -s tests` 全绿（实际 180 测试）+ Task 11 验收清单逐项有记录 + `git log` 每任务独立提交。

## 计划勘误（终审后补，2026-11-24）

全分支终审（opus）证明本计划三处自身缺陷，实现已由 fix wave（623a2d0..df21365）纠正，记录于此免得后来者重蹈：

1. **Task 8 one_tick 步骤序缺陷**：计划把「游标持久化」排在「摄取」之后、「审计」之前，且 BatchBuffer 设计为纯内存——两者叠加 = `once` 必漏审不足批量的消息、每次重启静默丢失最多 batch_max_wait_s 缓冲。纠正为：per-group `audited:{group}` 水位（audit 尝试完成即推进）+ build_deps 从 inbox 水位后重建缓冲 + `one_tick(flush_all=)`。
2. **崩溃模型测试缺陷**：计划给出的 `test_replay_after_crash` 以「游标重置为 0」建模崩溃——而真实崩溃中游标是审计前已持久化的，该假态使缺陷 (1) 十次任务评审全部失明。重写为双 Deps 复用同一 home 的真模型。
3. **digest 触发序计划沉默**：计划只写「try_fire_digest 防重」，未规定推送失败时槽位归属——首实现按 fire-before-push 不回滚，一次遮挡即永久烧掉当晚汇总。纠正：非 "ok" 结果 `digest_unfire` 归还槽位；realtime 本 tick 停止时 digest 不尝试。
4. 附带：`ingest_records` 的 mark_seen/insert_inbox 两段提交 → `store.accept_message` 单事务；out/ 文件按批次序号命名 → 按情报 id 命名（否则 hold 人工补发规程可能拿错文本）。
