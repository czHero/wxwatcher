# wxsend 使用说明

一条命令安全地给花名册目标发微信消息，并用数据库回执确认落库。
错发是唯一不可接受的失败，所以整套编排是「校验 → 锁 → GUI → 回执」，顺序本身即安全设计。
（2026-11-21 联机验收通过；本文所有行为均来自实测台账，非设计稿。）

## 用法

```bash
python3 tools/wechat-send/wxsend.py --to <roster_key> --text "报告摘要…链接"
python3 tools/wechat-send/wxsend.py --to <roster_key> --file report.txt
```

- `--to`：roster.json 中的目标 key；`--text` / `--file` 二选一（互斥、必选其一）。
- 可选 `--roster` / `--calib` 覆盖默认路径（默认 `~/.wechat-monitor/roster.json`、`~/.wechat-monitor/calib.json`）。
- `--dry-run`：只打印完整计划（目标、row、消息表、阶段与 argv 序列），**零 GUI 注入命令、零加锁**
  （非 `--force` 时仍会执行只读查询定表）；但拒发条件照常判定（未知目标、空文本、缺 row 仍是退出码 2）。
- `--force`：跳过前置/后置校验（误发保护禁用），GUI 序列照常执行，前后打印显式 WARNING，
  不承诺任何回执。注意：缺 row / row 越界的拒发**先于** `--force`——不带坐标的 GUI 序列绝不允许跑。
- `--nowait`：锁被占用时立即失败（退出码 4），不自旋等待；默认最多等 120 秒。

### 退出码

| 码 | 含义 | 之后做什么 |
|---|---|---|
| 0 | 成功：RECEIPT 已在目标表确认落库（local_id 较发送前基线增长） | 无需动作 |
| 2 | 拒发：前置校验/参数/环境不满足（零 GUI 命令执行）；**也可能**是 GUI 中途失败于发送之前——此时会话可能已被点开、输入框可能残留草稿，但发送未执行、消息未发出 | 前置拒发：修配置后重试；中途失败：清理输入框残留草稿后可安全重试 |
| 3 | NEEDS_CHECK：GUI 已开始但回执未确认（轮询超时、后置解密失败、send 阶段命令 rc≠0、或 GUI 中途意外异常） | **绝不自动重发**，人工核对微信窗口 |
| 4 | 锁占用（`--nowait` 或等待超时） | 稍后重试 |

退出码 3 的语义是「送达状态未知」：消息可能已发出，重发即二次发送，因此设计上禁止任何自动化对 3 的盲目重试。

剪贴板卫生：GUI 序列一旦开始，**任何**退出路径（含成功）都会尽力清空剪贴板
（`wl-copy --clear`，报告文本不留在剪贴板）；清理本身失败不改变退出码。
中止的运行可能在输入框残留草稿——按提示清理草稿后再人工发送或重试。

## roster.json 条目形态

```json
{"targets": {
  "filehelper": {"kind": "user", "ident": "文件传输助手", "wxid": "filehelper", "row": 141},
  "team":       {"kind": "group", "ident": "123@chatroom", "row": 220}
}}
```

- `kind` ∈ `user` | `group`；`ident`：user 为备注或显示名，group 为 chatroom id（其表标识即 id 本身）。
- `row`：该会话**置顶后**在会话列表中的行 y 坐标（按校准屏幕逻辑像素）。缺失即拒发（2）。
- `wxid`（可选）：直连消息表 `Msg_<md5(wxid)>`，给出则完全跳过 contact.db 备注查询（6b 校准产物形状）。
- 无 `wxid` 的 user：走备注唯一性前置——在 contact.db 按 `remark` 精确查 username，
  0 命中或多义命中均拒发（建议用 `RPT-` 前缀专属备注保证唯一）。
- `row` 越出 calib 屏幕高度同样拒发（行点击必然打偏）。

## 运行前提（逐条满足，否则打偏或校验失败）

1. **微信窗口保持打开、最大化、处于前台且不被遮挡**。坐标按 1440×900 逻辑空间标定，
   gnome-screenshot 截图与 ydotool 注入在同一逻辑空间 1:1；focus 阶段是字面空操作
   （`/usr/bin/true`），不能唤起最小化窗口、也不能把微信抢到前台——点击/粘贴只会命中
   坐标处当前的窗口。发送期间微信必须可见且在最上层，任何弹窗遮挡都会打偏。
   1:1 的前提是鼠标加速配置文件为 flat（6b 实测：adaptive 缩放下小位移会打偏；代码不锁定此项，须自查）：

   ```bash
   gsettings get org.gnome.desktop.peripherals.mouse accel-profile   # 期望 'flat'
   gsettings set org.gnome.desktop.peripherals.mouse accel-profile flat   # 不是则设置
   ```
2. **目标会话必须置顶（微信里右键 → 置顶）**。置顶块行位稳定；普通列表随每条新消息重排，
   row 坐标会点到错误的会话行。
3. **发送期间不得人工操作鼠标键盘**。flock 只串行化 wxsend 与 wxsend 之间，不防人——
   人工移动指针会直接打断注入序列。
4. **输入法必须处于非拼音状态**。拼音会吞键事件；当前系统 input-sources 已仅保留 `xkb:cn`。
   若需恢复手工中文输入，用拼音前不要跑 wxsend（自动化期间拼音必须关闭）。
5. **ydotool 需 sudo**（本机已配 NOPASSWD）：Debian sid 1.0.4 客户端、无 ydotoold，
   走 uinput 直连模式。其键名表**没有 Enter/Return 变体**（未知键名会被静默当首字母打出——
   这就是早期消息出现乱字符污染的原因），故发送走发送按钮点击，不用 Enter。
6. **db_keys.json 与登录态绑定**。微信重新登录后 key 会变，须按
   `tools/wechat-monitor/README.md`「Linux 版」节的按键流程重新捕获。密钥验证全部前置：
   每种目标（含 wxid 直连与 group）发送前都会只读探测消息库取 local_id 基线，
   解密失败即拒发（2，零 GUI 命令）；contact.db 备注查询解密失败同样拒发（2）。
   只有后置校验阶段才暴露的解密失败是 NEEDS_CHECK（3）。

### 会话 toggle 与收尾关闭点击

微信原生行为：**点击当前已打开会话的列表行会关闭该会话**（toggle，联机复现两次）。
因此 open_search 的行点击只在会话处于关闭态时可靠打开它。为保证连续发送同一目标
不踩 toggle，send 阶段在发送按钮点击之后追加一次同一置顶行的点击（`{IDENT}`=row）：
发送时会话必然处于打开+选中态，该行点击确定性地关闭会话——**每次成功发送结束时
会话为关闭态**，下一次发送的 open_search 又从关闭态开始。

中止的运行（exit 2/3）可能把会话留在打开态：下一次对同一目标的运行会安全失败一次
（exit 3，什么都没发出去——行点击先把会话关了，正文粘贴到空处），之后状态自愈。
收到 exit 3 先人工核对，不要盲目重发。

### 管道上下文与 wl-copy

`wl-copy` 会 fork 一个继承调用方 stdout/stderr 的守护进程；在管道/捕获输出上下文
（CI、harness）里不重定向会导致调用结束后下游等不到 EOF 而挂起。所以 calib 的 paste
模板必须用重定向包装：`["sh","-c","wl-copy \"$1\" >/dev/null 2>&1","_","{TEXT}"]`——
`{TEXT}` 仍是完整 argv 元素，正文作为位置参数 `$1` 传入，绝不插进脚本文本（注入安全）。
剪贴板清理同理：`["sh","-c","wl-copy --clear >/dev/null 2>&1"]`。

## 校准维护

- 文件：`~/.wechat-monitor/calib.json`（0600）。schema 两部分：
  - `screen`: `[W, H]` 逻辑屏幕尺寸（本机 `[1440, 900]`）；
  - `steps`: 五阶段 `focus / open_search / paste / select_first / send`，每阶段是 argv 模板列表
    （外部命令语法是数据不是代码），`{IDENT}` / `{TEXT}` 必须独立成元素。
- row 模式下 `{IDENT}` 承载的是**置顶行 y 坐标字符串**，`open_search` 与 `send` 两相都代入
  `str(row)`：open_search 点行进入会话（输入框自动聚焦），send 末尾的点行收尾关闭会话
  （见上文 toggle 节）；紧随 open_search 的 `paste(text="")` 是钉死 paste 模板的无害空跑。
- Return/enter 键**仅 send 阶段允许**（`load_calib` 加载期硬性校验）；当前 send 为发送按钮
  点击 + 收尾行点击（纯鼠标），全表无任何 return——这是防「选中前误发」的机制锁。
- 当前实测坐标：行点击 x=190、输入框 (600,805)、发送按钮 (1391,866)。
- `select_first` 的 repeat-backspace 次数以运行期 calib 为准（清空残留草稿是有界操作，
  次数不足会留草稿尾巴——拒发/中止提示语不引用具体次数）。
- 布局变化（改窗口尺寸、换主题、升级改按钮位）后重标：

```bash
python3 tools/wechat-send/calibrate.py
```

需人在场逐阶段试打命令；保存前经 `load_calib` 校验，不合格直接拒绝且保留旧文件。

## 安全设计

- **三重校验**：roster 白名单（不在册即拒）→ 目标定位唯一（备注→唯一 wxid，或 wxid 直连；
  多义即拒，拒绝猜测）→ 数据库回执（发送前只读探测目标表 local_id 基线——顺带对每种目标
  前置验证消息密钥；发送后轮询最新行，前缀比对**且 local_id 必须较基线增长**，
  同前缀旧行不得冒充回执）。
- **串行锁**：`~/.wechat-monitor/wxsend.lock` 上非阻塞 flock 自旋，任何时刻至多一条发送在途。
- **空文本拒发**：strip 后无内容不执行任何东西。
- **退出 3 禁止自动重发**：回执超时只报 NEEDS_CHECK，把「是否重发」留给人工判断，防错发二次伤害。
- **--force 显式降级**：跳过前后校验时 WARNING 明说「误发保护已禁用」「不承诺回执」，
  且带坐标缺失类配置错误的目标仍先被拒发——force 不是绕开物理打偏的后门。
- 多行正文整块粘贴、一次按钮点击作为**一条**消息发出（换行与链接保留，已联机验证）。
