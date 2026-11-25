"""groupwatch 运行时配置加载与校验。

约定：运行时根 `home`（env `GROUPWATCH_HOME` 覆盖默认 `~/.groupwatch`），目录 0700；
API key 只存 `home` 下的 `*.key` 文件（0600，由 `init` 写），其内容永不进 git/日志/
异常文本。本模块只做配置文件的读取与校验，不碰网络/子进程。

config.json 的 `limits.context_msgs` 缺省为 20（提取前文窗，见设计 §5.3）；
`limits.judge_threshold` 缺省 0.5（systemone 判定的 noul 命中阈值）；
`models.<role>.api` 缺省 "chat"，可设 "systemone"（百炼决策模型 /systemone
协议，judge 角色用）；`push.digest`
在文件里是嵌套对象 `{enabled, times}`，加载时摊平成 `digest_enabled` / `digest_times`；
`push.wxsend_py` 缺省推导到仓库内 `<repo>/tools/wechat-send/wxsend.py`；
`push.require_enroll` 缺省 true（未跑 enroll 无预检基线时拒绝盲发，见
dispatch.push_texts；设 false 恢复旧"放行带警告"）。
"""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("GROUPWATCH_HOME", "~/.groupwatch")).expanduser()

# config.py 位于 <repo>/tools/groupwatch/，parents[2] 即仓库根。
WXSEND_DEFAULT = (Path(__file__).resolve().parents[2]
                  / "tools" / "wechat-send" / "wxsend.py")

_CONTEXT_MSGS_DEFAULT = 20
_JUDGE_THRESHOLD_DEFAULT = 0.5
_MODEL_APIS = ("chat", "systemone")
_DIGEST_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(Exception):
    """配置读取/校验失败；消息为中文且具体到出错字段。"""


@dataclass(frozen=True)
class ModelRole:
    base_url: str
    model: str
    # I1（Task 1 台账裁决）：repr=False——调试打印/异常上下文带出 ModelRole
    # 实例时不得泄 key；等值比较与 dataclasses.replace 不受影响。
    api_key: str = field(repr=False)
    # systemone 适配：调用协议。"chat"= /chat/completions（OpenAI 兼容），
    # "systemone"= 百炼决策模型 /systemone（state+questions→noul 概率）。
    # 必须保持**末位带默认**——既有三元位置构造（测试/旧调用）不破。
    api: str = "chat"


@dataclass(frozen=True)
class IngestCfg:
    jsonl: Path
    poll_interval_s: int
    batch_max_msgs: int
    batch_max_wait_s: int


@dataclass(frozen=True)
class PushCfg:
    target_roster_key: str
    realtime: bool
    digest_enabled: bool
    digest_times: tuple[str, ...]
    wxsend_py: Path
    # accept-fix 2：未 enroll（无预检基线）拒绝盲发。True=baseline 缺失按
    # pre_fail 处理、零 wxsend；False=旧"放行带警告"。必须保持**末位带默认**
    # ——既有多元位置构造（测试/装配）不破。
    require_enroll: bool = True


@dataclass(frozen=True)
class Limits:
    max_retries: int
    push_max_per_tick: int
    inbox_retention_days: int
    context_msgs: int
    # systemone 判定：noul 概率 ≥ 阈值算命中（边界含等号），默认 0.5。
    judge_threshold: float = 0.5


@dataclass(frozen=True)
class Config:
    home: Path
    models: dict[str, ModelRole]
    ingest: IngestCfg
    push: PushCfg
    limits: Limits
    prompts_dir: Path


@dataclass(frozen=True)
class WatchItem:
    id: str
    name: str
    desc: str


def _read_json(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"配置文件不存在: {Path(path).name}")
    except OSError as e:
        raise ConfigError(f"无法读取配置文件 {Path(path).name}: {e.strerror}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{Path(path).name} 不是合法 JSON: {e.msg}（第 {e.lineno} 行）")


def _require_str(obj, key, where):
    if key not in obj:
        raise ConfigError(f"{where} 缺少 '{key}'")
    val = obj[key]
    if not isinstance(val, str) or not val:
        raise ConfigError(f"{where} 的 '{key}' 必须是非空字符串")
    return val


def _require_int(obj, key, where):
    if key not in obj:
        raise ConfigError(f"{where} 缺少 '{key}'")
    val = obj[key]
    if isinstance(val, bool) or not isinstance(val, int):
        raise ConfigError(f"{where} 的 '{key}' 必须是整数")
    return val


def _opt_int(obj, key, where, default):
    if key not in obj:
        return default
    return _require_int(obj, key, where)


def _opt_float_0_1(obj, key, where, default):
    """可选数值键：int/float（bool 拒——True 是 int 实例的惯犯）且落在 [0,1]。"""
    if key not in obj:
        return default
    val = obj[key]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ConfigError(f"{where} 的 '{key}' 必须是 0~1 之间的数值，"
                          f"收到 {val!r}")
    if not 0 <= val <= 1:
        raise ConfigError(f"{where} 的 '{key}' 必须在 0~1 之间，收到 {val!r}")
    return float(val)


def _load_api(entry, where):
    """models.<role>.api：缺省 "chat"，只允许恰 "chat"/"systemone"。"""
    api = entry.get("api", "chat")
    if api not in _MODEL_APIS:
        raise ConfigError(f"{where} 的 'api' 必须是 chat 或 systemone，"
                          f"收到 {api!r}")
    return api


def _require_bool(obj, key, where):
    if key not in obj:
        raise ConfigError(f"{where} 缺少 '{key}'")
    val = obj[key]
    if not isinstance(val, bool):
        raise ConfigError(f"{where} 的 '{key}' 必须是布尔值")
    return val


def _opt_bool(obj, key, where, default):
    """可选布尔键：缺失 → default；在场必须严格 bool（同 _require_bool 纪律）。"""
    if key not in obj:
        return default
    val = obj[key]
    if not isinstance(val, bool):
        raise ConfigError(f"{where} 的 '{key}' 必须是布尔值，收到 {val!r}")
    return val


def _load_key(home, api_key_file, where):
    p = Path(api_key_file).expanduser()
    if not p.is_absolute():
        p = home / p
    try:
        content = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise ConfigError(f"{where} 的 api_key_file 不存在: {p.name}")
    except OSError as e:
        raise ConfigError(f"{where} 的 api_key_file 无法读取 {p.name}: {e.strerror}")
    if not content:
        raise ConfigError(f"{where} 的 api_key_file 内容为空: {p.name}")
    return content


def _load_digest_times(digest, where):
    if "times" not in digest:
        raise ConfigError(f"{where} 缺少 'times'")
    times = digest["times"]
    if not isinstance(times, list):
        raise ConfigError(f"{where} 的 'times' 必须是数组")
    out = []
    for t in times:
        if not isinstance(t, str) or not _DIGEST_RE.match(t):
            raise ConfigError(f"{where} 的 'times' 每项必须是两位 'HH:MM'，收到 {t!r}")
        out.append(t)
    if any(out[i] >= out[i + 1] for i in range(len(out) - 1)):
        raise ConfigError(f"{where} 的 'times' 必须严格升序，收到 {out}")
    return tuple(out)


def load_config(home) -> Config:
    home = Path(home)
    data = _read_json(home / "config.json")
    if not isinstance(data, dict):
        raise ConfigError("config.json 顶层必须是 JSON 对象")
    for section in ("models", "ingest", "push", "limits"):
        if section not in data:
            raise ConfigError(f"config.json 缺少 '{section}' 段")

    models_raw = data["models"]
    if not isinstance(models_raw, dict) or not models_raw:
        raise ConfigError("config.json 的 'models' 必须是非空对象")
    models = {}
    for role, entry in models_raw.items():
        where = f"models.{role}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} 必须是对象")
        base_url = _require_str(entry, "base_url", where)
        model = _require_str(entry, "model", where)
        api_key_file = _require_str(entry, "api_key_file", where)
        models[role] = ModelRole(base_url, model,
                                 _load_key(home, api_key_file, where),
                                 api=_load_api(entry, where))

    ingest_raw = data["ingest"]
    if not isinstance(ingest_raw, dict):
        raise ConfigError("config.json 的 'ingest' 必须是对象")
    jsonl = Path(_require_str(ingest_raw, "jsonl", "ingest")).expanduser()
    ingest = IngestCfg(
        jsonl,
        _require_int(ingest_raw, "poll_interval_s", "ingest"),
        _require_int(ingest_raw, "batch_max_msgs", "ingest"),
        _require_int(ingest_raw, "batch_max_wait_s", "ingest"),
    )

    push_raw = data["push"]
    if not isinstance(push_raw, dict):
        raise ConfigError("config.json 的 'push' 必须是对象")
    target_roster_key = _require_str(push_raw, "target_roster_key", "push")
    realtime = _require_bool(push_raw, "realtime", "push")
    digest_raw = push_raw.get("digest")
    if not isinstance(digest_raw, dict):
        raise ConfigError("push 的 'digest' 必须是对象")
    digest_enabled = _require_bool(digest_raw, "enabled", "push.digest")
    digest_times = _load_digest_times(digest_raw, "push.digest")
    if "wxsend_py" in push_raw:
        wxsend_py = Path(_require_str(push_raw, "wxsend_py", "push")).expanduser()
    else:
        wxsend_py = WXSEND_DEFAULT
    require_enroll = _opt_bool(push_raw, "require_enroll", "push", True)
    push = PushCfg(target_roster_key, realtime, digest_enabled,
                   digest_times, wxsend_py, require_enroll)

    limits_raw = data["limits"]
    if not isinstance(limits_raw, dict):
        raise ConfigError("config.json 的 'limits' 必须是对象")
    limits = Limits(
        _require_int(limits_raw, "max_retries", "limits"),
        _require_int(limits_raw, "push_max_per_tick", "limits"),
        _require_int(limits_raw, "inbox_retention_days", "limits"),
        _opt_int(limits_raw, "context_msgs", "limits", _CONTEXT_MSGS_DEFAULT),
        _opt_float_0_1(limits_raw, "judge_threshold", "limits",
                       _JUDGE_THRESHOLD_DEFAULT),
    )

    return Config(home, models, ingest, push, limits, home / "prompts")


def load_watchitems(home) -> tuple[WatchItem, ...]:
    data = _read_json(Path(home) / "watchitems.json")
    if not isinstance(data, list):
        raise ConfigError("watchitems.json 顶层必须是数组")
    seen = set()
    out = []
    for idx, entry in enumerate(data):
        where = f"watchitems[{idx}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} 必须是对象")
        item = WatchItem(_require_str(entry, "id", where),
                         _require_str(entry, "name", where),
                         _require_str(entry, "desc", where))
        if item.id in seen:
            raise ConfigError(f"watchitems.json 存在重复 id: {item.id!r}")
        seen.add(item.id)
        out.append(item)
    return tuple(out)


def load_groups(home) -> frozenset[str]:
    data = _read_json(Path(home) / "groups.json")
    if not isinstance(data, dict):
        raise ConfigError("groups.json 顶层必须是对象")
    if "subscribe" not in data:
        return frozenset()
    sub = data["subscribe"]
    if not isinstance(sub, list) or any(not isinstance(x, str) for x in sub):
        raise ConfigError("groups.json 的 'subscribe' 必须是字符串数组")
    return frozenset(sub)


def ensure_home(home) -> None:
    p = Path(home)
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
