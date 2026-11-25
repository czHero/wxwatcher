"""部署件（deploy/*.service、DEPLOY.md、README.md）文件内容回归测试。

机械断言，最小防回归：不启 systemd、不读真实 ~/.groupwatch、不联网、不起子进程。
覆盖：两 unit 文件的必备键与 GUI 会话依赖、DEPLOY.md 的四条已授权原文命令与两行
enable、README.md 的前提清单与退出码表（含 2b 挂起禁重发字样），以及 ExecStart
里的路径真实存在于磁盘（钉死绝对路径的存活校验）。

路径推导：本文件在 tools/groupwatch/tests/ 下，parents[1] 即模块目录。
"""
import re
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]      # tools/groupwatch
DEPLOY_DIR = MODULE_DIR / "deploy"
UNIT_MONITOR = DEPLOY_DIR / "wechat-monitor.service"
UNIT_DAEMON = DEPLOY_DIR / "groupwatchd.service"
DEPLOY_MD = DEPLOY_DIR / "DEPLOY.md"
README_MD = MODULE_DIR / "README.md"

# 本机事实（设计 §5.6 / 部署拍板）：解释器与仓库都用绝对路径钉死。
PYTHON = "/home/dgkj/.pyenv/versions/3.13.12/bin/python3"
REPO = "/home/dgkj/文档/wechat2"

# 四条已授权原文命令 + 两行 enable（与 DEPLOY.md 必须逐字一致）。
LINGER_CMD = "loginctl enable-linger"
GSETTINGS_CMDS = [
    "gsettings set org.gnome.desktop.session idle-delay 0",
    "gsettings set org.gnome.desktop.screensaver lock-enabled false",
    "gsettings set org.gnome.settings-daemon.plugins.power "
    "sleep-inactive-ac-type 'nothing'",
]
ENABLE_CMDS = [
    "systemctl --user enable --now wechat-monitor.service",
    "systemctl --user enable --now groupwatchd.service",
]

# DEPLOY.md 步骤顺序标记：取各步骤标题原文（各取首次出现，须严格递增）。
STEP_MARKERS = ["第 1 步：init", "第 2 步：填 API key", "第 3 步：test-model",
                "第 4 步：groups", "第 5 步：编辑 groups.json",
                "第 6 步：enroll", "第 7 步：enable --now"]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class UnitFileTests(unittest.TestCase):
    def test_files_exist(self):
        for p in (UNIT_MONITOR, UNIT_DAEMON, DEPLOY_MD, README_MD):
            self.assertTrue(p.is_file(), f"缺少交付文件 {p}")

    def test_both_units_required_keys(self):
        for p in (UNIT_MONITOR, UNIT_DAEMON):
            t = _read(p)
            self.assertIn("[Service]", t, p.name)
            self.assertIn(f"ExecStart={PYTHON} ", t, p.name)
            self.assertIn("Restart=on-failure", t, p.name)
            # GUI 自动化必须等图形会话（lingering 开机自启但不早于登录会话）。
            self.assertIn("After=graphical-session.target", t, p.name)
            self.assertIn("BindsTo=graphical-session.target", t, p.name)
            self.assertIn(f"WorkingDirectory={REPO}", t, p.name)
            # README 前提清单声称"两个单元均已设"——两 unit 都钉同一行（M2 回归钉）。
            self.assertIn("Environment=GROUPWATCH_HOME=%h/.groupwatch", t, p.name)

    def test_both_units_install_section(self):
        for p in (UNIT_MONITOR, UNIT_DAEMON):
            t = _read(p)
            self.assertIn("[Install]", t, p.name)
            self.assertIn("WantedBy=default.target", t, p.name)

    def test_monitor_unit_execstart_default_params(self):
        t = _read(UNIT_MONITOR)
        self.assertIn("monitor.py", t)
        self.assertIn("--interval 30", t)
        self.assertIn(f"{REPO}/tools/wechat-monitor/monitor.py", t)
        # 保持通用产物：不加 --groups-only（设计 §5.6）。
        self.assertNotIn("--groups-only", t)

    def test_daemon_unit_dependency_and_home(self):
        t = _read(UNIT_DAEMON)
        self.assertIn("groupwatch.py run", t)
        self.assertIn(f"{REPO}/tools/groupwatch/groupwatch.py", t)
        self.assertIn("Requires=wechat-monitor.service", t)
        # Environment= 行已由 test_both_units_required_keys 对两 unit 共同钉住。

    def test_execstart_paths_exist_on_disk(self):
        """ExecStart 的每个绝对路径参数（解释器+脚本）必须在磁盘上真实存在。"""
        for p in (UNIT_MONITOR, UNIT_DAEMON):
            m = re.search(r"^ExecStart=(.+)$", _read(p), re.MULTILINE)
            self.assertIsNotNone(m, p.name)
            for tok in m.group(1).split():
                if tok.startswith("/"):
                    self.assertTrue(Path(tok).exists(), f"{p.name}: {tok} 不存在")


class DeployMdTests(unittest.TestCase):
    def test_pinned_commands_verbatim(self):
        t = _read(DEPLOY_MD)
        self.assertIn(LINGER_CMD, t)
        for cmd in GSETTINGS_CMDS + ENABLE_CMDS:
            self.assertIn(cmd, t, f"DEPLOY.md 缺原文命令: {cmd}")

    def test_install_location_and_reload(self):
        t = _read(DEPLOY_MD)
        self.assertIn("~/.config/systemd/user", t)
        self.assertIn("systemctl --user daemon-reload", t)

    def test_step_order(self):
        t = _read(DEPLOY_MD)
        idx = [t.find(m) for m in STEP_MARKERS]
        for i, (mark, at) in enumerate(zip(STEP_MARKERS, idx)):
            self.assertGreaterEqual(at, 0, f"DEPLOY.md 缺步骤标记: {mark}")
            if i:
                self.assertGreater(at, idx[i - 1],
                                  f"步骤顺序错误: {mark} 未排在 {STEP_MARKERS[i-1]} 之后")

    def test_no_real_secrets(self):
        # 示例 key 一律占位符形态，不得出现真实密钥样式。
        t = _read(DEPLOY_MD) + _read(README_MD)
        self.assertIn("FILL_ME", t)
        self.assertNotIn("sk-", t)


class ReadmeTests(unittest.TestCase):
    def test_prereq_checklist(self):
        t = _read(README_MD)
        self.assertIn("前提清单", t)
        for kw in ("最大化", "不锁屏", "monitor", "roster", "轮换",
                   "GROUPWATCH_HOME", "ydotool", "NOPASSWD", "拼音"):
            self.assertIn(kw, t, f"README 前提清单缺: {kw}")

    def test_exit_code_table(self):
        t = _read(README_MD)
        self.assertIn("退出码", t)
        for kw in ("2a", "2b", "挂起禁重发", "让路", "pending",
                   "NEEDS_CHECK", "REFUSE: aborted at step"):
            self.assertIn(kw, t, f"README 退出码表缺: {kw}")

    def test_readme_first_run_recipe_and_known_limitation(self):
        # M4：首跑跳过历史食谱（sqlite3 用 groupwatch.db + INSERT OR REPLACE）
        # 与回填 jsonl 的内存尖峰告诫；M3 parked 的已知限制一行须在文。
        t = _read(README_MD)
        for kw in ("首跑：跳过历史", "INSERT OR REPLACE INTO cursor",
                   "groupwatch.db", "stat -c %s", "单次 tail",
                   "提醒时间显示为审计时刻，跨午夜消息可能进次日汇总"):
            self.assertIn(kw, t, f"README 缺: {kw}")

    def test_links_to_upstream_docs(self):
        t = _read(README_MD)
        self.assertIn("tools/wechat-monitor/README.md", t)
        self.assertIn("tools/wechat-send/README.md", t)


if __name__ == "__main__":
    unittest.main()
