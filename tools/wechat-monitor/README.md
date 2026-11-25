# Linux 监听端（微信 for Linux 4.1.13）

只读解密微信本地消息库并增量输出 JSONL。**纯观察，零注入**（发送侧能力见 `tools/wechat-send/`）。

## 运行前提

- 数据目录：`~/文档/xwechat_files/<wxid>/db_storage`（`paths.py` 按 XDG 自动解析）。
- 库密钥：`~/.wechat-monitor/db_keys.json`（0600，键=DB 文件名，值=64 位 hex raw key）。
  密钥捕获工具链不在本仓库发布范围，由运维侧流程生成；**微信重新登录后 key 会变，需重新生成**。
- 依赖：python3（hashlib/hmac 标准库即可）、`zstandard`（消息压缩帧）、`cryptography`（AES，测试与页校验）。

## 用法

```bash
# 单轮验证（首次建议重置水位到当前，只采新消息）
python3 tools/wechat-monitor/monitor.py --once --since-now

# 常驻增量（每 30s 轮询，追加写 ~/.wechat-monitor/messages.jsonl）
python3 tools/wechat-monitor/monitor.py --interval 30

# 常用开关
#   --backfill N     首跑每表回补最近 N 条
#   --groups-only    仅群聊
#   --group 关键词    会话 id/群名子串过滤
#   --echo / --json  同时打印到 stdout
```

## 记录格式（messages.jsonl 每行一条）

`ts`(ISO) `db` `table`(Msg_<md5>) `local_id` `server_id` `create_time`(epoch)
`kind`(group/single) `conversation_id` `conversation_name` `sender_id` `sender_name`
`type` `text` `decompressed` —— 仅纯文本消息（local_type==1），压缩帧自动解压。

## 设计要点

- 每 tick 自动发现新会话表（`refresh_tables`），新开的聊天不漏采。
- watermark 依赖 `local_id` 单调；检测到异常回退会 reset + warn。
- flock 单实例；JSONL O_APPEND 追加 + fsync（消费端按"残行不消费"协议 tail，见 groupwatch）。

## 测试

`cd tools/wechat-monitor && python3 -m unittest discover -s tests`

已知历史（macOS 版逆向工程记录）不在本仓库发布范围。
