# groupwatch 部署指南（专用值守机 · systemd 用户单元）

从零到开机自启，按顺序照抄执行即可（编号步骤第 0–7 步）。
运行时根 `~/.groupwatch`（两个单元已设 `Environment=GROUPWATCH_HOME=%h/.groupwatch`，
命令行可用 `--home` 覆盖）。运行前提与退出码语义见 `tools/groupwatch/README.md`；
上游文档：`tools/wechat-monitor/README.md`、`tools/wechat-send/README.md`。

下文所有 groupwatch 命令都通过变量 `GW`（解释器绝对路径 + 入口脚本）调用，
zsh/bash 均可直接复制：

```bash
export GW="/home/dgkj/.pyenv/versions/3.13.12/bin/python3 /home/dgkj/文档/wechat2/tools/groupwatch/groupwatch.py"
```

## 第 0 步：值守机准备（一次性；桌面三项已获授权）

开机自启（linger 让两个用户单元在登录后自动拉起，且不早于图形会话）：

```bash
loginctl enable-linger
```

关闭自动黑屏与锁屏、禁止插电待机——值守推送的时窗正是无人时段，锁屏/遮挡
会让预检不过、整队停在 pending（零 GUI 命令）。三条命令（恢复原值即可回退）：

```bash
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.desktop.screensaver lock-enabled false
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
```

微信侧人工准备：窗口最大化、前台、不遮挡（`enroll` 的预检点位按全屏标定）；
收件人（默认文件传输助手）在会话列表置顶。wxsend 的两项系统前提须已就绪：
sudo 免密 ydotool（NOPASSWD）、拼音输入法已关闭（拼音吞键，见
`tools/wechat-send/README.md` 运行前提第 4/5 条）。

## 第 1 步：init 建立运行环境

```bash
$GW init
```

在 `~/.groupwatch/`（0700）生成带占位符的 `config.json`（base_url 为
`<在此填百炼 base_url>`）、内置 homework 示例的 `watchitems.json`、
`groups.json`（`{"subscribe": []}`）、内容即占位符 `FILL_ME_在此粘贴百炼APIKey`
的 `judge.key` / `extract.key`（0600）、`prompts/` 三件套。重跑安全：目标文件
已存在则逐个拒绝覆盖并退 2。

## 第 2 步：填 API key 与模型端点（亲笔，不经对话/日志/git）

- 百炼控制台取 API key，**亲手**写入 `~/.groupwatch/judge.key` 与
  `~/.groupwatch/extract.key`（整行替换占位符，各一个），然后
  `chmod 600 ~/.groupwatch/judge.key ~/.groupwatch/extract.key`；
- 编辑 `~/.groupwatch/config.json`：把两处 `<在此填百炼 base_url>` 换成真实
  `base_url`；如端点模型名与模板不同，一并改 `model` 值。

## 第 3 步：test-model 链路探针

```bash
$GW test-model judge
$GW test-model extract
```

两条都退 0 且打印响应文本才算通过；退 2 = key / base_url / 网络没配对，回第 2 步。

## 第 4 步：groups 查看可订阅群

```bash
$GW groups
```

打印 `[订阅|未订阅] 群名 — id` 表。`groups` 复用监听端的库函数直接读微信本地
数据库，**不要求 monitor 进程在跑**；前提是微信在跑、数据库 key 有效。上游缺失
（key 失效、微信没跑等）时中文指引 + 退 2，按 README 前提清单排障后再来。

可选：与 groups 无关，若想让积压的历史消息提前进入 JSONL（例如你想先攒一批
再开推送），可手动拉起监听端；第二个实例会在 `~/.wechat-monitor/monitor.lock`
上直接退出，所以第 7 步 enable 单元之前务必先杀掉这个手动进程：

```bash
nohup /home/dgkj/.pyenv/versions/3.13.12/bin/python3 /home/dgkj/文档/wechat2/tools/wechat-monitor/monitor.py --interval 30 >>~/.wechat-monitor/manual.log 2>&1 &
```

## 第 5 步：编辑 groups.json 选群

把第 4 步表里你订阅的群 id（`xxx@chatroom` 形态，逐字复制）写进 `~/.groupwatch/groups.json`
的 `"subscribe": []` 数组。改完重启守护进程生效（配置在启动时载入，运行中
不热加载）：已装单元用 `systemctl --user restart groupwatchd.service`，
没装时重跑 `$GW run` 即可。

## 第 6 步：enroll 前台预检点位

微信最大化前台、无遮挡、无锁屏时执行：

```bash
$GW enroll
```

内部走 `gnome-screenshot` 截屏 + 四个点位像素采样，写 `probe_points.json`。
交互终端会先确认「微信在前台且无遮挡」再截图；非 tty 环境（如脚本里跑）
加 `--yes`。桌面布局/分辨率变化后重跑本步。

## 第 7 步：enable --now（安装两个用户单元）

把仓库内两个单元复制进 `~/.config/systemd/user/`，重载后启用：

```bash
pkill -f "monitor.py --interval 30" || true   # 仅用于清理第 4 步的可选手动进程；单元托管的进程别用 pkill 管——monitor 收到 SIGTERM 会以退出码 0 干净收尾，Restart=on-failure 不重启干净退出的进程，误杀等于摄入断流直到手动 start
install -Dm644 /home/dgkj/文档/wechat2/tools/groupwatch/deploy/wechat-monitor.service ~/.config/systemd/user/wechat-monitor.service
install -Dm644 /home/dgkj/文档/wechat2/tools/groupwatch/deploy/groupwatchd.service ~/.config/systemd/user/groupwatchd.service
systemctl --user daemon-reload
systemctl --user enable --now wechat-monitor.service
systemctl --user enable --now groupwatchd.service
```

改过单元文件后重新生效的配方：重拷两行 `install` → `systemctl --user daemon-reload`
→ `systemctl --user restart wechat-monitor.service groupwatchd.service`。
不要用 `enable --now` 再跑一遍来"重启"——对已 active 的单元它是静默 no-op。

开机自启的实际语义：linger（第 0 步）让 user 服务管理器开机即活，但两单元
BindsTo 图形会话，**要等登录后才拉起**；无人值守重启即恢复需开启 GDM 自动登录
（可选，用户级决定，现状可查 `grep -H AutomaticLogin /etc/gdm3/daemon.conf`，
本文不代作开启决定）。不开自动登录时，登录后手动
`systemctl --user start wechat-monitor.service groupwatchd.service` 或直接等
会话把已 enable 的单元带活。

## 验收与日常运维

```bash
systemctl --user status wechat-monitor.service groupwatchd.service
$GW status                      # 计数/游标/jsonl 新鲜度/推送流水/待推明细
journalctl --user -u groupwatchd.service -n 50
```

- 冒烟：往订阅群发一条作业消息 → 三分钟内文件传输助手应收到【作业 · 群名】提醒。
- 停止：`systemctl --user stop groupwatchd.service wechat-monitor.service`
  （顺序无所谓——monitor 被停时 groupwatchd 会随 `Requires=` 一并停，反之不会）。
- 微信重新登录会轮换数据库 key：按 `tools/wechat-monitor/README.md`「Linux 版」
  重新捕获后重启 monitor，再 `$GW status` 确认摄入恢复；期间积压条目走 pending 自动续流。
- 退出码语义与「绝不自动重发」红线见 `tools/groupwatch/README.md`；
  出现 hold（`status` 显示"搁置"）一律人工核对后再处置。
