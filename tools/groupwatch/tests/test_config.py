import dataclasses
import importlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config

# 一律用 config.X 属性访问：test_default_home_env_override 会 importlib.reload
# 换掉模块命名空间里的类对象，若把 ModelRole/ConfigError 等 `from config import`
# 成局部名，reload 后新旧类不同源会导致 == 与 assertRaises 失效。


def _good_cfg():
    return {
        "models": {
            "judge": {"base_url": "https://x/v1",
                      "model": "decision-model-preview",
                      "api_key_file": "judge.key"},
            "extract": {"base_url": "https://e/v1", "model": "qwen-max",
                        "api_key_file": "extract.key"},
        },
        "ingest": {"jsonl": "~/.wechat-monitor/messages.jsonl",
                   "poll_interval_s": 5, "batch_max_msgs": 40,
                   "batch_max_wait_s": 60},
        "push": {"target_roster_key": "filehelper", "realtime": True,
                 "digest": {"enabled": True, "times": ["08:00", "20:30"]}},
        "limits": {"max_retries": 3, "push_max_per_tick": 10,
                   "inbox_retention_days": 14},
    }


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def write_config(self, data):
        (self.home / "config.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def write_keys(self, files):
        for name, content in files.items():
            (self.home / name).write_text(content, encoding="utf-8")

    def test_load_config_ok(self):
        self.write_config(_good_cfg())
        self.write_keys({"judge.key": "secret", "extract.key": "extractsecret"})
        cfg = config.load_config(self.home)
        self.assertEqual(
            cfg.models["judge"],
            config.ModelRole("https://x/v1", "decision-model-preview", "secret"))
        self.assertEqual(
            cfg.models["extract"],
            config.ModelRole("https://e/v1", "qwen-max", "extractsecret"))
        self.assertEqual(
            cfg.ingest,
            config.IngestCfg(
                Path("~/.wechat-monitor/messages.jsonl").expanduser(),
                5, 40, 60))
        self.assertEqual(cfg.push.target_roster_key, "filehelper")
        self.assertTrue(cfg.push.realtime)
        self.assertTrue(cfg.push.digest_enabled)
        self.assertEqual(cfg.push.digest_times, ("08:00", "20:30"))
        # wxsend_py 默认：由 config.py 的 parents[2] 推导到仓库内真实文件
        self.assertEqual(cfg.push.wxsend_py.name, "wxsend.py")
        self.assertEqual(cfg.push.wxsend_py.parent.name, "wechat-send")
        self.assertTrue(cfg.push.wxsend_py.is_file())
        # context_msgs 未在 config.json 出现 → 默认 20
        self.assertEqual(cfg.limits, config.Limits(3, 10, 14, 20))
        self.assertEqual(cfg.home, self.home)
        self.assertEqual(cfg.prompts_dir, self.home / "prompts")

    def test_load_config_missing_key_file(self):
        data = _good_cfg()
        data["models"]["extract"]["api_key_file"] = "nope.key"
        self.write_config(data)
        # judge.key 存在且含明文密钥，若错误信息串了内容就会泄露
        self.write_keys({"judge.key": "SUPERSECRET"})
        with self.assertRaises(config.ConfigError) as cm:
            config.load_config(self.home)
        msg = str(cm.exception)
        self.assertIn("nope.key", msg)
        self.assertNotIn("SUPERSECRET", msg)

    def test_load_config_bad_digest_time(self):
        for bad in ("25:70", "7:5"):
            with self.subTest(t=bad):
                data = _good_cfg()
                data["push"]["digest"]["times"] = [bad]
                self.write_config(data)
                self.write_keys({"judge.key": "s", "extract.key": "s"})
                with self.assertRaises(config.ConfigError):
                    config.load_config(self.home)

    def test_load_config_bad_json_shape(self):
        data = _good_cfg()
        del data["models"]
        self.write_config(data)
        self.write_keys({"judge.key": "s", "extract.key": "s"})
        with self.assertRaises(config.ConfigError):
            config.load_config(self.home)
        (self.home / "config.json").write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load_config(self.home)

    def test_load_watchitems_ok(self):
        items = [{"id": "a", "name": "甲", "desc": "d1"},
                 {"id": "b", "name": "乙", "desc": "d2"}]
        (self.home / "watchitems.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8")
        got = config.load_watchitems(self.home)
        self.assertEqual(
            got,
            (config.WatchItem("a", "甲", "d1"),
             config.WatchItem("b", "乙", "d2")))

    def test_load_watchitems_dup_id(self):
        items = [{"id": "a", "name": "甲", "desc": "d1"},
                 {"id": "a", "name": "再", "desc": "d2"}]
        (self.home / "watchitems.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load_watchitems(self.home)

    def test_load_groups(self):
        (self.home / "groups.json").write_text(
            json.dumps({"subscribe": ["a@chatroom"]}), encoding="utf-8")
        self.assertEqual(config.load_groups(self.home),
                         frozenset({"a@chatroom"}))

    def test_load_groups_missing_subscribe_is_empty(self):
        (self.home / "groups.json").write_text("{}", encoding="utf-8")
        self.assertEqual(config.load_groups(self.home), frozenset())

    def test_default_home_env_override(self):
        old = os.environ.get("GROUPWATCH_HOME")
        os.environ["GROUPWATCH_HOME"] = "/tmp/xx"
        try:
            importlib.reload(config)
            self.assertEqual(config.DEFAULT_HOME, Path("/tmp/xx"))
        finally:
            if old is None:
                os.environ.pop("GROUPWATCH_HOME", None)
            else:
                os.environ["GROUPWATCH_HOME"] = old
            importlib.reload(config)

    def test_model_role_repr_hides_api_key(self):
        # I1（Task 1 台账裁决，终审落地）：repr 绝不带出密钥；dataclasses
        # .replace 与等值比较不受 repr=False 影响。
        r = repr(config.ModelRole("https://x/v1", "m", "SECRETKW"))
        self.assertNotIn("SECRETKW", r)
        self.assertIn("https://x/v1", r)
        role = config.ModelRole("u", "m", "k")
        self.assertEqual(role, config.ModelRole("u", "m", "k"))
        self.assertEqual(dataclasses.replace(role, model="m2"),
                         config.ModelRole("u", "m2", "k"))
        self.assertEqual(role.api_key, "k")  # 值仍在，只是不进 repr

    def test_ensure_home_perms(self):
        target = self.home / "sub" / "home"
        config.ensure_home(target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        # parents=True：中间层 sub 也被创建
        self.assertTrue((self.home / "sub").is_dir())


class _HomeFixture:
    """tmp home + config/key 写入原语（与 ConfigTest 同款，但不继承 ConfigTest
    ——继承会把既有用例重复跑一遍，虚增计数）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def write_config(self, data):
        (self.home / "config.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def write_keys(self, files):
        for name, content in files.items():
            (self.home / name).write_text(content, encoding="utf-8")


class ModelApiFieldTests(_HomeFixture, unittest.TestCase):
    """systemone 适配：models.<role>.api（"chat"|"systemone"，默认 chat）。"""

    def test_model_role_api_defaults_to_chat_last_field(self):
        # api 必须是**末位带默认**字段：既有三元位置构造（测试与旧代码）不破。
        role = config.ModelRole("u", "m", "k")
        self.assertEqual(role.api, "chat")
        self.assertEqual(role, config.ModelRole("u", "m", "k"))
        self.assertEqual(dataclasses.replace(role, api="systemone").api,
                         "systemone")

    def test_load_config_api_default_and_explicit(self):
        data = _good_cfg()
        data["models"]["judge"]["api"] = "systemone"
        self.write_config(data)
        self.write_keys({"judge.key": "s", "extract.key": "s"})
        cfg = config.load_config(self.home)
        self.assertEqual(cfg.models["judge"].api, "systemone")
        # 未写 api 的角色 → "chat"（旧配置零改动继续可用）
        self.assertEqual(cfg.models["extract"].api, "chat")

    def test_load_config_bad_api_rejected(self):
        # 只允许恰 "chat"/"systemone"；错误点名角色与允许值。
        for bad in ("responses", "CHAT", "", "systemOne", 5):
            with self.subTest(bad=bad):
                data = _good_cfg()
                data["models"]["judge"]["api"] = bad
                self.write_config(data)
                self.write_keys({"judge.key": "s", "extract.key": "s"})
                with self.assertRaises(config.ConfigError) as cm:
                    config.load_config(self.home)
                msg = str(cm.exception)
                self.assertIn("models.judge", msg)
                self.assertIn("api", msg)
                self.assertIn("chat", msg)
                self.assertIn("systemone", msg)


class JudgeThresholdTests(_HomeFixture, unittest.TestCase):
    """systemone 适配：limits.judge_threshold（默认 0.5，[0,1] 数值，bool 拒）。"""

    def _load_limits(self, limits_patch):
        data = _good_cfg()
        data["limits"].update(limits_patch)
        self.write_config(data)
        self.write_keys({"judge.key": "s", "extract.key": "s"})
        return config.load_config(self.home).limits

    def test_judge_threshold_dataclass_default(self):
        self.assertEqual(config.Limits(3, 10, 14, 20).judge_threshold, 0.5)

    def test_judge_threshold_default_when_absent(self):
        self.assertEqual(self._load_limits({}).judge_threshold, 0.5)

    def test_judge_threshold_accepts_bounds(self):
        for good in (0, 1, 0.0, 0.5, 0.99):
            with self.subTest(good=good):
                self.assertEqual(self._load_limits(
                    {"judge_threshold": good}).judge_threshold, float(good))

    def test_judge_threshold_rejects_out_of_range_and_types(self):
        # bool 特意列入：True 是 int 实例，必须点名拒绝（同 _require_int 纪律）。
        for bad in (-0.1, 1.5, 2, "0.5", True, False, None, []):
            with self.subTest(bad=bad):
                with self.assertRaises(config.ConfigError) as cm:
                    self._load_limits({"judge_threshold": bad})
                self.assertIn("judge_threshold", str(cm.exception))


class PushRequireEnrollTests(_HomeFixture, unittest.TestCase):
    """accept-fix 2：push.require_enroll（缺省 true——未 enroll 拒绝盲发）。"""

    def _load_push(self, push_patch):
        data = _good_cfg()
        data["push"].update(push_patch)
        self.write_config(data)
        self.write_keys({"judge.key": "s", "extract.key": "s"})
        return config.load_config(self.home).push

    def test_pushcfg_require_enroll_default_true_last_field(self):
        # 末位带默认：既有五元位置构造（dispatch/watchd 测试与旧调用）不破；
        # 新字段等值比较照常参与。
        p = config.PushCfg("k", True, False, (), Path("w.py"))
        self.assertTrue(p.require_enroll)
        self.assertEqual(p, config.PushCfg("k", True, False, (), Path("w.py"),
                                           True))
        self.assertFalse(config.PushCfg("k", True, False, (), Path("w.py"),
                                        False).require_enroll)

    def test_load_config_require_enroll_default_and_explicit(self):
        self.assertTrue(self._load_push({}).require_enroll)  # 旧配置文件零改动
        self.assertFalse(
            self._load_push({"require_enroll": False}).require_enroll)
        self.assertTrue(
            self._load_push({"require_enroll": True}).require_enroll)

    def test_load_config_require_enroll_rejects_non_bool(self):
        # 严格布尔：字符串/数字/null 一律 ConfigError 点名键（bool 是 int
        # 实例的坑由 isinstance(val, bool) 正面挡掉）。
        for bad in ("true", "True", 1, 0, None, []):
            with self.subTest(bad=bad):
                with self.assertRaises(config.ConfigError) as cm:
                    self._load_push({"require_enroll": bad})
                self.assertIn("require_enroll", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
