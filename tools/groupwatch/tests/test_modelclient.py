import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

import modelclient
from config import ModelRole

# 假 key：含小写 "secret"，任何异常/日志文本里出现它都算泄漏。
DECOY_KEY = "sk-DECOY-supersecret-000"
ROLE = ModelRole("https://dashscope.example/v1", "qwen-max", DECOY_KEY)


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _ok(content="模型回复"):
    return _Resp(200, {"choices": [{"message": {"content": content}}]})


class _Post:
    """假 requests.post：按序回放 responses；条目是异常则 raise。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def _sleep(self, secs):
        self.sleeps.append(secs)

    def test_chat_success_returns_content_and_payload_shape(self):
        post = _Post(_ok("好的"))
        content = modelclient.chat(ROLE, "你是法官", "请判断",
                                   sleep=self._sleep, post=post)
        self.assertEqual(content, "好的")
        self.assertEqual(len(post.calls), 1)
        url, kwargs = post.calls[0]
        self.assertEqual(url, "https://dashscope.example/v1/chat/completions")
        self.assertEqual(kwargs["headers"],
                         {"Authorization": f"Bearer {DECOY_KEY}"})
        self.assertEqual(kwargs["json"], {
            "model": "qwen-max",
            "messages": [{"role": "system", "content": "你是法官"},
                         {"role": "user", "content": "请判断"}],
            "temperature": 0,
        })
        self.assertEqual(kwargs["timeout"], 60)
        self.assertEqual(self.sleeps, [])

    def test_chat_retries_on_timeout_then_ok(self):
        post = _Post(requests.exceptions.Timeout("read timed out"),
                     requests.exceptions.ConnectionError("reset by peer"),
                     _ok("第三跳成功"))
        content = modelclient.chat(ROLE, "s", "u",
                                   sleep=self._sleep, post=post)
        self.assertEqual(content, "第三跳成功")
        self.assertEqual(len(post.calls), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_chat_gives_up_after_retries_raises_not_retryable(self):
        post = _Post(*[requests.exceptions.Timeout("t")] * 3)
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.chat(ROLE, "s", "u", retries=2,
                             sleep=self._sleep, post=post)
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(len(post.calls), 3)  # 首次 + retries 次
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_chat_retries_on_429_and_5xx_then_ok(self):
        post = _Post(_Resp(status_code=429), _Resp(status_code=500),
                     _ok("限流后成功"))
        content = modelclient.chat(ROLE, "s", "u",
                                   sleep=self._sleep, post=post)
        self.assertEqual(content, "限流后成功")
        self.assertEqual(len(post.calls), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_chat_other_4xx_immediate_fail(self):
        for status in (400, 401, 404, 422):
            with self.subTest(status=status):
                post = _Post(_Resp(status_code=status))
                with self.assertRaises(modelclient.ModelError) as cm:
                    modelclient.chat(ROLE, "s", "u",
                                     sleep=self._sleep, post=post)
                self.assertFalse(cm.exception.retryable)
                self.assertEqual(len(post.calls), 1)
                self.assertEqual(self.sleeps, [])

    def test_chat_400_immediate_fail(self):
        post = _Post(_Resp(status_code=400))
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.chat(ROLE, "s", "u", sleep=self._sleep, post=post)
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_chat_error_message_never_contains_key(self):
        post = _Post(_Resp(status_code=400))
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.chat(ROLE, "s", "u", sleep=self._sleep, post=post)
        self.assertNotIn("secret", str(cm.exception))
        self.assertNotIn("secret", repr(cm.exception))

        post = _Post(requests.exceptions.ConnectionError(
            f"failed posting to {ROLE.base_url} with key {DECOY_KEY}"))
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.chat(ROLE, "s", "u", retries=0,
                             sleep=self._sleep, post=post)
        self.assertNotIn("secret", str(cm.exception))
        self.assertNotIn("secret", repr(cm.exception))
        self.assertNotIn(DECOY_KEY, str(cm.exception))

    def test_chat_retries_on_chunked_encoding_error_then_ok(self):
        # I2：漏斗改捕 RequestException 基类——ChunkedEncodingError（响应流中
        # 途断，既非 Timeout 也非 ConnectionError）此前裸穿 chat()，现在必须
        # 走同一条退避重试路，成功后返回。
        post = _Post(
            requests.exceptions.ChunkedEncodingError("连接中断于响应流"),
            _ok("断流后重试成功"))
        content = modelclient.chat(ROLE, "s", "u",
                                   sleep=self._sleep, post=post)
        self.assertEqual(content, "断流后重试成功")
        self.assertEqual(len(post.calls), 2)
        self.assertEqual(self.sleeps, [1.0])

    def test_chat_generic_request_exception_exhausts_without_key_leak(self):
        # I2：重试耗尽 → 不可重试 ModelError；底层异常原文（可能回显含 key
        # 的 URL/头上下文）一律不转述。
        post = _Post(*[requests.exceptions.ChunkedEncodingError(
            f"boom url={ROLE.base_url} auth=Bearer {DECOY_KEY}")] * 3)
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.chat(ROLE, "s", "u", retries=2,
                             sleep=self._sleep, post=post)
        self.assertFalse(cm.exception.retryable)
        for text in (str(cm.exception), repr(cm.exception)):
            self.assertNotIn("DECOY", text)
            self.assertNotIn("secret", text)
            self.assertNotIn("boom", text)

    def test_chat_missing_structure_raises_retryable(self):
        post = _Post(_Resp(200, {"choices": []}))
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.chat(ROLE, "s", "u", sleep=self._sleep, post=post)
        self.assertTrue(cm.exception.retryable)
        self.assertNotIn("secret", str(cm.exception))
        self.assertEqual(len(post.calls), 1)


ROLE_SYSONE = ModelRole("https://dashscope.example/v1",
                        "decision-model-preview", DECOY_KEY, api="systemone")

_QUESTION = {"type": "noul", "instructions": "是否命中作业？",
             "criteria": {"true": "是作业", "false": "不是作业"}}


class SystemOneTest(unittest.TestCase):
    """决策模型 /systemone 协议：state+questions→answers，传输语义与 chat 同源。"""

    def setUp(self):
        self.sleeps = []

    def _sleep(self, secs):
        self.sleeps.append(secs)

    def test_systemone_url_payload_and_answers_extraction(self):
        post = _Post(_Resp(200, {"model": "decision-model-preview",
                                 "answers": {"q1": {"type": "noul",
                                                    "noul": 0.97}}}))
        answers = modelclient.systemone(ROLE_SYSONE, "明天带剪刀",
                                        {"q1": _QUESTION},
                                        sleep=self._sleep, post=post)
        self.assertEqual(answers, {"q1": {"type": "noul", "noul": 0.97}})
        self.assertEqual(len(post.calls), 1)
        url, kwargs = post.calls[0]
        self.assertEqual(url, "https://dashscope.example/v1/systemone")
        self.assertEqual(kwargs["headers"],
                         {"Authorization": f"Bearer {DECOY_KEY}"})
        # 协议体精确：model/state/questions 三键，questions 原样嵌套，无 temperature。
        self.assertEqual(kwargs["json"], {
            "model": "decision-model-preview",
            "state": "明天带剪刀",
            "questions": {"q1": _QUESTION},
        })
        self.assertEqual(kwargs["timeout"], 60)
        self.assertEqual(self.sleeps, [])

    def test_systemone_state_passthrough_object(self):
        # state 是 Any（String|Object|Array）：结构原样进 payload。
        post = _Post(_Resp(200, {"answers": {}}))
        modelclient.systemone(ROLE_SYSONE, {"text": "带剪刀", "who": "老师"},
                              {}, sleep=self._sleep, post=post)
        self.assertEqual(post.calls[0][1]["json"]["state"],
                         {"text": "带剪刀", "who": "老师"})

    def test_systemone_retries_on_429_then_ok(self):
        post = _Post(_Resp(status_code=429),
                     _Resp(200, {"answers": {"q1": {"noul": 0.1}}}))
        answers = modelclient.systemone(ROLE_SYSONE, "s", {"q1": _QUESTION},
                                        sleep=self._sleep, post=post)
        self.assertEqual(answers, {"q1": {"noul": 0.1}})
        self.assertEqual(len(post.calls), 2)
        self.assertEqual(self.sleeps, [1.0])

    def test_systemone_other_4xx_immediate_fail(self):
        post = _Post(_Resp(status_code=400))
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.systemone(ROLE_SYSONE, "s", {"q1": _QUESTION},
                                  sleep=self._sleep, post=post)
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_systemone_missing_answers_retryable_single_call(self):
        # 与 chat 的"结构缺失"同口径：可重试 ModelError，但不在本层重投。
        post = _Post(_Resp(200, {"model": "decision-model-preview"}))
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.systemone(ROLE_SYSONE, "s", {},
                                  sleep=self._sleep, post=post)
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(post.calls), 1)

    def test_systemone_answers_not_dict_retryable(self):
        post = _Post(_Resp(200, {"answers": ["noul"]}))
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.systemone(ROLE_SYSONE, "s", {},
                                  sleep=self._sleep, post=post)
        self.assertTrue(cm.exception.retryable)

    def test_systemone_transport_exhaustion_not_retryable(self):
        post = _Post(*[requests.exceptions.Timeout("t")] * 3)
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.systemone(ROLE_SYSONE, "s", {"q1": _QUESTION},
                                  retries=2, sleep=self._sleep, post=post)
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(len(post.calls), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_systemone_error_never_contains_key_planted_in_body(self):
        # key 红线与 chat 同纪律：错误响应体里回显 key 也不转述。
        post = _Post(_Resp(400, {"error": {"message":
                          f"invalid api key {DECOY_KEY}"}}))
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.systemone(ROLE_SYSONE, "s", {"q1": _QUESTION},
                                  sleep=self._sleep, post=post)
        for text in (str(cm.exception), repr(cm.exception)):
            self.assertNotIn("DECOY", text)
            self.assertNotIn("secret", text)
        # 结构坏 + body 内埋 key：自造消息同样干净。
        post = _Post(_Resp(200, {"answers": DECOY_KEY}))
        with self.assertRaises(modelclient.ModelError) as cm:
            modelclient.systemone(ROLE_SYSONE, "s", {},
                                  sleep=self._sleep, post=post)
        self.assertNotIn("DECOY", str(cm.exception))


class ExtractJsonTest(unittest.TestCase):
    def test_extract_json_plain(self):
        self.assertEqual(modelclient.extract_json('{"a": 1, "b": [1, 2]}'),
                         {"a": 1, "b": [1, 2]})

    def test_extract_json_fenced(self):
        self.assertEqual(
            modelclient.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(
            modelclient.extract_json('```\n{"a": [2]}\n```'), {"a": [2]})

    def test_extract_json_trailing_prose(self):
        self.assertEqual(
            modelclient.extract_json('{"a": 1} 以上即为我的判断，谢谢。'),
            {"a": 1})

    def test_extract_json_nested(self):
        text = '结论：{"verdict": {"risk": {"level": "高"}}, "items": [{"id": 1}, {"id": 2}]} 完毕'
        self.assertEqual(modelclient.extract_json(text), {
            "verdict": {"risk": {"level": "高"}},
            "items": [{"id": 1}, {"id": 2}],
        })

    def test_extract_json_raises_on_garbage(self):
        with self.assertRaises(ValueError):
            modelclient.extract_json("对不起，没有可解析的内容")
        with self.assertRaises(ValueError):
            modelclient.extract_json('{"broken": ')


if __name__ == "__main__":
    unittest.main()
