# wxsend — 微信自动发送（文本报告投递）设计

日期：2026-11-20 · 状态：已实现（2026-11-21 联机验收通过；搜索路线实测出局，改置顶行点击+发送按钮，见 plans 台账 Task 6b/6c） · 平台：Ubuntu 24.04 Wayland / 微信 Linux 4.1.13

## 意图

把 AI 生成的报告（纯文本，摘要+链接）从本机自动投递到指定微信联系人/群，
并保证「发到且只发到目标会话」——错发是本功能唯一不可接受的失败。

## 目标

- `wxsend.py --to <roster_key> --text "..."`（或 `--file report.txt`）一条命令完成发送
- 发送前三重校验 + 发送后数据库回执确认（复用现成监听端 sqlcipher 链路）
- 串行队列：文件锁保证与人工使用、并发任务互斥
- 校准可重放：窗口布局变化后 `calibrate.py` 十分钟重标

## 非目标

- 实时应答机器人、图片/文件消息、群发营销、多账号
- 不修改微信进程/二进制（hook 方案 B 明确出局）

## 约束

- Wayland：输入注入只有 ydotool(uinput)+wl-clipboard 可行；坐标假定微信窗口最大化
- 中文与长文本一律剪贴板粘贴（Ctrl+V），不用键盘模拟
- key 轮换问题不变：db_keys.json 失效时，校验环节自动降级为"仅发送不验货"并显著警告

## 方案（已选 A）

GUI 操作序列：`Super 聚焦微信 → 点搜索框 → 粘贴目标 → Enter 进会话 →
点输入框 → 粘贴正文 → Enter`，随后查库确认。

> 实现时机制调整（2026-11-21，Task 6b/6c 联机实测）：搜索路线出局——搜索胶囊位于标题拖拽带内点不中，且 ydotool 键名表无 Enter；改为「点击置顶会话行（roster `row` 坐标，`{IDENT}` 承载之）→ 粘贴正文 → 点击发送按钮」，全序列无 Return 键，目标必须置顶。

### 目标定位与三重校验

1. **roster.yaml**：`roster_key → {kind: user|group, target}`。
   user 用**专属备注**（建议 `RPT-` 前缀）作 target；group 用 `xxx@chatroom`。
2. **发送前（库校验）**：在 contact.db（已有 K1）按备注查唯一匹配 → 得 wxid →
   预期表名 `Msg_<md5(wxid)>`；备注不唯一/查不到 → 拒发。
3. **发送后（回执校验）**：轮询该表 15 秒，出现 local_id 增长且 message_content
   解压后等于发送文本（前缀匹配）→ 成功；超时 → **不自动重发**，退出码 3，
   状态 `NEEDS_CHECK`（防错发二次伤害）。

### 组件

- `wxsend.py`：CLI + 编排（校验 → 锁 → GUI 序列 → 回执）
- `gui_driver.py`：ydotool/wl-clipboard 薄封装（click/type_paste/keys），坐标读 calib.json
- `calibrate.py`：截屏（gnome-screenshot）→ 人工报点或图像辅助 → 写 calib.json
- `roster.yaml`、`~/.wechat-monitor/`（复用现有 key/state）

### 失败与告警

- 微信未运行/窗口未找到 → 拒发（退出码 2）
- key 失效 → 跳过前后校验但打印大警告，`--force` 才允许继续
- 锁被占 → 排队等待或 `--nowait` 直接失败

## 交给实现者的裁量

- Enter 与 Ctrl+Enter 发送热键差异：实现时实测一次，写死到 calib.json
- 等待时长的具体数值（会话打开、粘贴渲染）
- 回执文本比对的 zstd/前缀细节复用 content_parser 现成函数
