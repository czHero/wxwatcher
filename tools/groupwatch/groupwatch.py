#!/usr/bin/env python3
"""groupwatch 入口脚本：sys.path 自举 + sys.exit(main())。

把脚本自身所在目录（`<repo>/tools/groupwatch`）插到 sys.path 首位，使 cli 及其
同级平铺模块（config/dispatch/modelclient/store/watchd）按裸名导入。注意本目录
的 `cli.py` 会遮蔽 site-packages 里可能存在的同名 `cli` 包——正因如此必须先插入
本目录再导入。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 本模块树以「本地 cli.py 先于 site-packages 同名包」为前提（sys.path[0] 顺序保证）。
# 注意 sys.modules 按名缓存：若未来任何入口先 import 了外部 `cli` 包，缓存会遮蔽
# 本地、路径技巧再靠前也无效——届时改用包名限定导入（如 `groupwatch.cli`）。
from cli import main

if __name__ == "__main__":
    sys.exit(main())
