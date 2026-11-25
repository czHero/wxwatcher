"""groupwatch 模型客户端（百炼双协议）：chat/completions + 决策模型 /systemone。

`chat()` 只发非流式 chat/completions 请求；`systemone()` 发百炼决策模型的
{base}/systemone 请求（state+questions → answers.noul 概率）；两者 429/5xx/
超时/连接错误按指数退避重试，其余 4xx 立即失败——分类与退避共用 _retry_send，
语义不分叉。安全红线：api_key 只进 Authorization 头，任何自造异常文本都不
引用底层异常原文/响应体（其可能回显 URL/头），因此 key 绝不泄漏到异常/日志。
`extract_json()` 从模型自由文本里剥围栏并容错截取第一个 JSON 值。
"""
import json
import re
import time
from typing import Any, Callable

import requests

from config import ModelRole


class ModelError(Exception):
    """模型调用失败；retryable=True 表示上层（如 limits.max_retries）可再试。"""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# 围栏内容：``` 或 ```json 开头、``` 结尾，取中间部分。
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _request_once(url: str, payload: dict, headers: dict, timeout_s: int,
                  post: Callable[..., Any]) -> tuple[str, Any]:
    """单次 POST + 状态分类：("ok", resp) 交给调用方解析；("retry", err)
    传输异常或 429/5xx，调用方退避；非 429 的 4xx 立即抛不可重试 ModelError。

    不转述底层异常原文/响应体：它们可能回显含敏感头的请求上下文（key 红线）。
    chat()/systemone() 共用本分类，语义不得分叉（accept-fix 契约 §2）。
    """
    try:
        resp = post(url, json=payload, headers=headers, timeout=timeout_s)
    except requests.exceptions.RequestException:
        # I2：漏斗收在 RequestException 基类——Timeout/ConnectionError
        # 语义原样（同一可重试分支），并把 ChunkedEncodingError 这类此前
        # 裸穿的流错误纳入退避重试。
        return "retry", "请求超时或连接失败"
    status = resp.status_code
    if status == 429 or 500 <= status < 600:
        return "retry", f"模型服务端返回 HTTP {status}"
    if 400 <= status < 500:
        raise ModelError(f"模型请求被拒绝 (HTTP {status})，不可重试",
                         retryable=False)
    return "ok", resp


def _retry_send(url: str, payload: dict, headers: dict, *, timeout_s: int,
                retries: int, backoff_base_s: float,
                sleep: Callable[[float], None],
                post: Callable[..., Any],
                extract: Callable[[Any], Any]) -> Any:
    """退避重试驱动：共尝试 1+retries 次；extract(resp) 成功即返回，其抛出的
    ModelError（结构缺失类，retryable=True）原样穿透、不在本层重投（与既有
    chat() 口径一致）。全部尝试以 ("retry", err) 收场则抛不可重试 ModelError，
    消息只带分类短语、不含 key。"""
    last_err = "未知错误"
    for attempt in range(retries + 1):
        kind, val = _request_once(url, payload, headers, timeout_s, post)
        if kind == "ok":
            return extract(val)
        last_err = val
        if attempt < retries:
            sleep(backoff_base_s * 2 ** attempt)
    raise ModelError(
        f"模型请求重试 {retries} 次后仍失败（最后错误: {last_err}），不再重试",
        retryable=False)


def chat(role: ModelRole, system: str, user: str, *,
         timeout_s: int = 60, retries: int = 3, backoff_base_s: float = 1.0,
         sleep: Callable[[float], None] = time.sleep,
         post: Callable[..., Any] = requests.post) -> str:
    """POST {base_url}/chat/completions，返回 choices[0].message.content。

    共尝试 1+retries 次；第 attempt 次失败后若还可重试则睡
    backoff_base_s * 2**attempt 秒。4xx（除 429）立即不可重试失败；
    重试耗尽抛不可重试 ModelError（消息不含 api_key）。
    """
    url = f"{role.base_url}/chat/completions"
    payload = {
        "model": role.model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {role.api_key}"}

    def _extract(resp):
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelError(
                "模型响应缺少 choices[0].message.content 结构",
                retryable=True) from None

    return _retry_send(url, payload, headers, timeout_s=timeout_s,
                       retries=retries, backoff_base_s=backoff_base_s,
                       sleep=sleep, post=post, extract=_extract)


def systemone(role: ModelRole, state: Any, questions: dict, *,
              timeout_s: int = 60, retries: int = 3,
              backoff_base_s: float = 1.0,
              sleep: Callable[[float], None] = time.sleep,
              post: Callable[..., Any] = requests.post) -> dict:
    """POST {base_url}/systemone——百炼决策模型协议（非 chat）。

    body = {"model", "state", "questions"}（无 temperature）；成功返回
    data["answers"]（qid → {"type","noul"}，noul=命中"true"的概率 0..1）。
    传输/退避/4xx 分类与 key 纪律和 chat() 完全同源（共用 _retry_send）。
    role.api 不在此检查——协议路由是调用方（audit.judge_batch）的职责。
    """
    url = f"{role.base_url}/systemone"
    payload = {"model": role.model, "state": state, "questions": questions}
    headers = {"Authorization": f"Bearer {role.api_key}"}

    def _extract(resp):
        try:
            answers = resp.json()["answers"]
        except (ValueError, KeyError, TypeError):
            raise ModelError("决策模型响应缺少 'answers' 结构",
                             retryable=True) from None
        if not isinstance(answers, dict):
            raise ModelError("决策模型响应的 'answers' 必须是对象",
                             retryable=True) from None
        return answers

    return _retry_send(url, payload, headers, timeout_s=timeout_s,
                       retries=retries, backoff_base_s=backoff_base_s,
                       sleep=sleep, post=post, extract=_extract)


def extract_json(text: str) -> Any:
    """从模型输出提取 JSON：剥 ```/```json 围栏，容忍前后闲话。

    从首个 '{' 或 '['（取较靠前者）起用 raw_decode 解析，允许尾缀闲话；
    找不到起始符或解析失败抛 ValueError（消息不回显全文）。
    """
    m = _FENCE_RE.search(text)
    body = m.group(1) if m else text
    starts = [i for i in (body.find("{"), body.find("[")) if i != -1]
    if not starts:
        raise ValueError("文本中未找到 JSON 起始符 '{' 或 '['")
    try:
        value, _ = json.JSONDecoder().raw_decode(body[min(starts):])
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e.msg}") from None
    return value
