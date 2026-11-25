"""chatroom 显示名解析：直接查 contact.db 的 contact 表 nick_name 列。

为什么存在：monitor.load_maps → probe_maps 按表序 first-wins 建群名表，
`name2id` 表先到手却没有任何显示名列，于是每个 @chatroom 的 name 恒为
None——contact 表里真实的 nick_name（如 `19927187460@chatroom => family`）
永远覆写不上。monitor.py 是只读上游，不得修改；这里以库复用（sys.path
import 后调用其 run_sqlcipher/quote_ident 原语）绕开该缺陷，只读查询、
零写入。若本模块与 monitor 都取不到名字，调用方回落裸 @chatroom id。

best-effort 契约：任何失败（contact.db 缺失、无 key、解密失败、sqlcipher
不可用）一律返回 {}，绝不上抛——群名解析不值得拖垮 groups 列表或守护。
"""
import sys
from pathlib import Path


def chatroom_names(monitor, db_storage, keys) -> dict[str, str]:
    """返回 {chatroom_username: nick_name}；失败返回 {}（NEVER raise）。

    monitor：注入的 monitor 模块（提供 run_sqlcipher，quote_ident 可选）；
    db_storage：微信 db_storage 根 Path；keys：load_key_material +
    load_aux_keys 合并后的 {str(db路径): key}，key 允许 bytes 或 hex 字符串
    （归一方式与 monitor 相同：有 .hex() 用 .hex()，否则 str()）。
    """
    try:
        db = Path(db_storage) / "contact" / "contact.db"
        key = keys.get(str(db))
        if key is None:
            return {}
        key_hex = key.hex() if hasattr(key, "hex") else str(key)
        qi = getattr(monitor, "quote_ident", None)

        def ident(name):
            return qi(name) if callable(qi) else name

        # 表/列名由已验证的 contact.db schema 固定：contact(username, nick_name)
        sql = (f"SELECT {ident('username')}, {ident('nick_name')} "
               f"FROM {ident('contact')} "
               f"WHERE {ident('username')} LIKE '%@chatroom'")
        rows = monitor.run_sqlcipher(db, key_hex, sql) or []
        out: dict[str, str] = {}
        for row in rows:
            gid = row.get("username")
            name = row.get("nick_name")
            if not gid or not name:
                continue
            name = str(name).strip()
            if name:
                out[str(gid)] = name
        return out
    except Exception as e:
        # 一行中文提示，只带异常类型，绝不外泄 key/路径内容
        print(f"群名解析跳过：{type(e).__name__}", file=sys.stderr)
        return {}
