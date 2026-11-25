# wxwatcher

微信群消息自动化套件（Linux 桌面端，纯 GUI 自动化 + 本地只读解析，不使用任何第三方协议接口）。

## 组件

- **tools/wechat-monitor** — 只读解析微信本地消息库，增量输出 JSONL（纯观察，零注入）。
- **tools/groupwatch** — 群消息审计管道：按关注项两级模型审计（便宜判定模型召回 + 强模型提取终审），
  结构化入库，实时提醒 + 每日汇总。`docs/superpowers/specs/2026-11-24-groupwatch-design.md` 为设计文档。
- **tools/wechat-send** — 经 GUI 自动化（ydotool + 剪贴板）安全发送消息，含数据库回执核验与
  "状态未知永不自动重发"协议。各组件 README 为权威使用文档。

## 使用前提与安全姿态

- 需本机运行微信 Linux 桌面版并登录；发送要求窗口最大化、前台、目标会话置顶。
- 全部能力构建在"你自己拥有并登录的账号"之上；请遵守微信服务条款，自行承担使用风险。
- 密钥（微信库 key、模型 API key）只应存在于本机用户目录（0600），切勿提交进仓库。
- 消息库解密参数的运行时实现在 `tools/wechat-monitor`；历史逆向过程文档不在本仓库范围。

## 快速上手

见各组件 README；群watch 一条命令完成审计+推送：

```bash
python3 tools/groupwatch/groupwatch.py init   # 首次：脚手架
python3 tools/groupwatch/groupwatch.py once --dry-run   # 全链路演练（零外呼零发送）
```
