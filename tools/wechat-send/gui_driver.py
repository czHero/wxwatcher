"""gui_driver：calib.json 驱动的 GUI 步骤模板展开与注入式执行。

设计动机：安装的 ydotool 1.0.4 没有 --absolute，且 CLI 语法跨版本漂移——
所以每条外部命令都以 argv 模板形式存放在 calib.json（语法是数据不是代码）。
本模块只把 {IDENT}/{TEXT} 占位符展开为 argv 列表并经可注入 runner 执行，
自身绝不拼接 shell 命令。
"""
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

PHASES = ("focus", "open_search", "paste", "select_first", "send")
PLACEHOLDERS = ("{IDENT}", "{TEXT}")
# FP#1：选中目标前的多余 Return 会发错对象，故 return/enter 键仅允许出现在 send 模板。
RETURN_TOKENS = ("return", "enter")


@dataclass
class Calib:
    screen: tuple[int, int]
    steps: dict[str, list[list[str]]]


def load_calib(path: Path) -> Calib:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"calib 顶层必须是 JSON 对象: {path}")
    screen = data.get("screen")
    if (
        not isinstance(screen, list)
        or len(screen) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in screen)
    ):
        raise ValueError(f"calib 缺少或非法 'screen' [W, H]: {path}")
    steps = data.get("steps")
    if not isinstance(steps, dict):
        raise ValueError(f"calib 缺少 'steps' 对象: {path}")
    missing = [p for p in PHASES if p not in steps]
    if missing:
        raise ValueError(f"calib steps 缺少阶段: {missing}")
    normalized: dict[str, list[list[str]]] = {}
    for phase in PHASES:
        value = steps[phase]
        if not isinstance(value, list) or not value:
            raise ValueError(f"calib steps[{phase!r}] 必须是非空 argv 列表")
        for argv in value:
            if not isinstance(argv, list) or not argv or not all(
                isinstance(a, str) for a in argv
            ):
                raise ValueError(f"calib steps[{phase!r}] 含非法 argv: {argv!r}")
            for elem in argv:
                # 占位符只能是完整 argv 元素，禁止子串合并（防注入/拆分）。
                if elem in PLACEHOLDERS:
                    continue
                if any(ph in elem for ph in PLACEHOLDERS):
                    raise ValueError(
                        f"calib steps[{phase!r}] 占位符必须独立成元素: {elem!r}"
                    )
            if phase != "send":
                for elem in argv:
                    tokens = {t.lower() for t in elem.split("+")}
                    if tokens & set(RETURN_TOKENS):
                        raise ValueError(
                            f"calib steps[{phase!r}] 不得含 return/enter 键（仅 send 允许）: {elem!r}"
                        )
        normalized[phase] = [list(argv) for argv in value]
    return Calib((screen[0], screen[1]), normalized)


def expand_steps(
    calib: Calib,
    phase: str,
    *,
    ident: str | None = None,
    text: str | None = None,
) -> list[list[str]]:
    """把 {IDENT}/{TEXT} 作为整元素替换进指定阶段的 argv 模板；缺失参数即报错。

    {TEXT} 只去尾随空白（rstrip）：前导空白是正文的一部分（Review-Focus #1）。
    """
    if phase not in calib.steps:
        raise ValueError(f"未知阶段: {phase!r}")
    text_value = text.rstrip() if text is not None else None
    out: list[list[str]] = []
    for argv in calib.steps[phase]:
        expanded = []
        for elem in argv:
            if elem == "{IDENT}":
                if ident is None:
                    raise ValueError("模板含 {IDENT} 但未提供 ident")
                expanded.append(ident)
            elif elem == "{TEXT}":
                if text_value is None:
                    raise ValueError("模板含 {TEXT} 但未提供 text")
                expanded.append(text_value)
            else:
                expanded.append(elem)
        out.append(expanded)
    return out


def run_steps(steps: list[list[str]], runner=subprocess.run) -> list:
    """顺序执行 argv 列表；不经 shell，argv 原样传给 runner（测试可注入）。"""
    return [runner(argv) for argv in steps]
