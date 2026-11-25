"""groupwatch 摄取层：JSONL 尾读（残行按住）+ 过滤去重 + 攒批。

monitor 每批结束才 fsync，"末行无换行"是写入中的正常中间态：`tail_jsonl`
只消费到最后一个 `\\n`（含），残行按住不推进游标，下轮补齐后重读——宁可短暂
停滞，不永久漏报。"有换行但 JSON/必备字段非法"才计 skipped 并越过游标。

全链路统一用 epoch：inbox/返回记录的 `ts`/`epoch` 取 monitor 的
`create_time`（int），monitor 的 `ts` 是 ISO 串，仅作展示不参与时序。
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

# tail 阶段仅校验"能不能安全消费这一行"，故只查结构性必备字段（create_time
# 在内——缺它落库会得 epoch 0，污染 recent 时序并立即可被 prune）；
# kind/订阅过滤属 ingest_records 职责。
_REQUIRED_FIELDS = ("kind", "db", "table", "local_id", "conversation_id", "text",
                    "create_time")


@dataclass
class TailResult:
    records: list[dict]
    new_offset: int
    skipped: int


def tail_jsonl(path: Path, offset: int) -> TailResult:
    """从 `offset` 读 jsonl 增量，只消费完整行（以 `\\n` 结尾）。"""
    p = Path(path)
    if not p.exists():
        return TailResult([], 0, 0)
    with open(p, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    cut = chunk.rfind(b"\n")
    if cut < 0:
        # 整段都是残行（或空）：不消费，游标原地。
        return TailResult([], offset, 0)
    consumed = chunk[:cut + 1]
    records: list[dict] = []
    skipped = 0
    for line in consumed.decode("utf-8", errors="replace").split("\n"):
        if not line:
            continue  # 消费段必以 \n 结尾，末项为空串非坏行
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        # 键存在还不够：create_time 必须是真 int。null/"数字串"/bool 都能过
        # 键校验，却会在 ingest_records 的 int(create_time) 处 TypeError——
        # 一行毒丸卡死整轮 tick。类型不符按坏行 skipped 越过，游标照常推进。
        ct = obj.get("create_time") if isinstance(obj, dict) else None
        if (not isinstance(obj, dict)
                or any(k not in obj for k in _REQUIRED_FIELDS)
                or isinstance(ct, bool) or not isinstance(ct, int)):
            skipped += 1
            continue
        records.append(obj)
    return TailResult(records, offset + cut + 1, skipped)


def ingest_records(store, groups: frozenset[str], records: list[dict]) -> list[dict]:
    """过滤（群聊 ∧ 订阅 ∧ 未见过）后落 inbox，返回带 source_key/epoch/rowid
    的接受记录。落库走 `store.accept_message`（seen+inbox 同一事务，终审 C2）；
    is_seen 预过滤只是快路径，真去重由该事务的 OR IGNORE 兜底。rowid 供
    watchd 在批审计尝试后推进 audited 水位。"""
    accepted: list[dict] = []
    for rec in records:
        if rec.get("kind") != "group":
            continue
        if rec.get("conversation_id") not in groups:
            continue
        key = (rec["db"], rec["table"], rec["local_id"])
        if store.is_seen(*key):
            continue
        epoch = int(rec["create_time"])
        rowid = store.accept_message({
            "db": rec["db"], "table": rec["table"], "local_id": rec["local_id"],
            "ts": epoch, "group": rec["conversation_id"],
            "sender_name": rec.get("sender_name", ""), "text": rec["text"],
        })
        accepted.append(dict(rec, source_key=f"{rec['db']}/{rec['table']}/{rec['local_id']}",
                             epoch=epoch, rowid=rowid))
    return accepted


@dataclass
class Batch:
    group: str
    msgs: list[dict]


@dataclass
class BatchBuffer:
    """按群隔离的攒批器：凑满 max_msgs 即出（一次可出多批），残余等 flush_stale。"""

    max_msgs: int
    max_wait_s: int
    _bufs: dict[str, list[dict]] = field(default_factory=dict)
    _first_ts: dict[str, float] = field(default_factory=dict)  # 各群残余的最老入队时刻

    def add(self, group: str, recs: list[dict], now: float) -> list[Batch]:
        out: list[Batch] = []
        if not recs:
            return out
        buf = self._bufs.setdefault(group, [])
        if not buf:
            self._first_ts[group] = now
        buf.extend(recs)
        while len(buf) >= self.max_msgs:
            out.append(Batch(group, buf[:self.max_msgs]))
            del buf[:self.max_msgs]
        if not buf:
            self._bufs.pop(group, None)
            self._first_ts.pop(group, None)
        elif out:
            # 仅真出了批才对残余重新计时；未出批不得重置——否则持续小流量
            # 每 tick 都把等待钟拨回，max_wait_s 永不触发。
            self._first_ts[group] = now
        return out

    def flush_stale(self, now: float) -> list[Batch]:
        out: list[Batch] = []
        for group in list(self._bufs):
            if now - self._first_ts[group] >= self.max_wait_s:
                out.append(Batch(group, self._bufs.pop(group)))
                self._first_ts.pop(group, None)
        return out

    def prime(self, group: str, recs: list[dict],
              oldest_ts: float) -> None:
        """重启恢复注入（终审 C2）：从 inbox 找回的未审计消息直接进缓冲，
        记录保留**原始 epoch**；stale 计时从 oldest_ts 起算（与现有残余取
        更早者），重启后第一个 tick 即可把过期残余审掉，不再空等一个
        max_wait_s。调用方负责不重复落库——这里只是内存态。"""
        if not recs:
            return
        buf = self._bufs.setdefault(group, [])
        buf.extend(recs)
        if group in self._first_ts:
            self._first_ts[group] = min(self._first_ts[group], oldest_ts)
        else:
            self._first_ts[group] = oldest_ts

    def drain_all(self) -> list[Batch]:
        """无视尺寸与等待，把每个群的全部残余各整批倒出（once 的 flush_all
        语义，终审 C2）。清群即清计时，不留下次的幽灵 first_ts。"""
        out: list[Batch] = []
        for group in list(self._bufs):
            if self._bufs[group]:
                out.append(Batch(group, self._bufs.pop(group)))
            self._first_ts.pop(group, None)
        return out

    def pending_count(self) -> int:
        return sum(len(b) for b in self._bufs.values())
