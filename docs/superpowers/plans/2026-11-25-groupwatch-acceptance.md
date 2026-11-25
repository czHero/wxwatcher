# groupwatch Task 11 联机验收记录

日期：2026-11-25 · 环境：Ubuntu 25.10 / 微信 Linux 4.1.13.26（Wayland，1440×900 最大化）/ 百炼 token-plan 网关 / 订阅群=「专用测试」(54414536868@chatroom) / 收件人=filehelper(row 141)

## 逐项结果

| # | 验收项 | 结果 | 证据 |
|---|--------|------|------|
| 1 | API key 配置 | ✅ | 用户亲手写入 judge.key/extract.key（0600，未过对话）；/models 200 |
| 2 | 双模型链路探针 | ✅ | `test-model judge`→systemone answers {"ping":{"noul":0.31}}；`test-model extract`→chat "OK" |
| 3 | 群列表与订阅 | ✅ | 发现 monitor `load_maps` name2id 取名缺陷→groupwatch 侧 contacts.py 直查 contact.nick_name（60/60 群名实机可读）；测试群经用户建群+首条消息触发注册 |
| 4 | 预检门 | ✅ | enroll 建 4 锚点基线；遮挡实验点名「点位 输入区 不匹配」→pre_fail 零 wxsend；未 enroll 时 require_enroll 拒盲发（实测复现） |
| 5 | 端到端真推送 | ✅✅ | 真作业消息（混排活动通知尾）→ 判定 0.84≥0.7 → 提取 relevant=true → **手机确认收到**；两条重复消息合并为 1 条情报（source_keys 含两 local_id）；"测试" 0.42<0.7 不命中 |
| 6 | 晚间汇总 | ⚠️ 部分 | 遮挡期汇总尝试=pre_fail+槽位归还（digest_fired 空）实证 C1 修复；撤遮挡后同槽补发未实机走完（同机制单测 test_pending_resumes_without_new_intel 绿）。今晚 20:30 自然触发可复核 |
| 7 | 重启不重报 | ⚠️ 单测 | 实机 C2 触发两次因消息时序未成立；逻辑由 test_replay_after_crash（诚实崩溃模型：游标在审前已持久化）+ hold-never-redispatch-on-restart 单测覆盖，复审核过 |
| 8 | 安全协议 | ✅ | needs_check（库 flush 延迟假阴性）→hold 不自动重发→人工裁决后改判 sent；全程仅 filehelper 收到测试文本 |

## 验收期事故与根治（均已提交评审）

1. **协议发现**：decision-model-preview 非 chat 模型——百炼决策模型走 `POST {base}/systemone`（state+questions→noul 概率）。实现双协议路由（judge.api=systemone，extract.api=chat）。
2. **判定语义陷阱**：noul 读"整条消息主题"，真作业混活动尾→0.1（漏报）。改"部分相关"措辞→0.84/0.42/0.0 三分正确，阈值 0.7。
3. **假阳性推送**："测试"0.72 曾命中且被推送。三重防线：阈值 0.7 + **提取 relevant 否决**（宁漏勿误）+ enroll 门。
4. **盲发事故**：无基线时预检 fail-open 致一次真实盲发（落在 needs_check，取证证实未发出、未误伤他聊）。根治：`push.require_enroll=true` fail-closed。

## 操作备忘（本机现场）

- 运行配置：`~/.groupwatch/config.json`（judge=systemone/decision-model-preview，extract=chat/qwen3.7-max，judge_threshold=0.7，require_enroll=true，digest 20:30）
- 开机自启：linger 已开 + 单元已 enable——**monitor 已 enable**；**groupwatchd 建议今晚复核后 enable**：`systemctl --user enable --now groupwatchd`
- 订阅编辑：`groups.json` 的 subscribe 数组（群 id 用 `groupwatch groups` 查）
- 关注项：`watchitems.json` + home 提示词副本；**部署新机务必刷新 home prompts 副本**（旧 extract.md 无 relevant 指令→全部 extract_failed，安全方向=漏报）
- needs_check 处置：看手机→已发则 `UPDATE intelligence SET pushed='sent' WHERE id=?`；未发则 `pushed='pending'` 待下 tick 自动续流

## 已知限制（PARKED，见裁决清单）

- `inbox_retention_days` 是死配置（prune 未接生产；接它需先修 rowid 复用防审计水位失明——独立小任务）
- 提醒显示审计时刻；跨午夜消息进次日汇总
- 群名解析依赖 contact.db（不可读时回落裸 id）

## 结论

原始目标「按配置关注项审计群聊、提取有用信息、汇总推送给订阅人」**第一阶段端到端达成并经手机侧确认**。测试发送全程仅限文件传输助手。
