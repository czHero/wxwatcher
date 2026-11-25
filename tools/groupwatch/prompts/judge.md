<!-- 仅在 models.judge.api=chat 时使用；systemone 问题构造见 audit._noul_questions（部分相关即 true 措辞，联机标定 2026-11-25） -->
你是消息过滤器。给定「关注项列表」和一批群消息，逐条判断哪些消息命中哪些关注项。只依据消息原文，不臆测。输出纯 JSON，无其他文字。
===
关注项列表：
{watchitems}
群：{group}
消息批次：
{batch}
输出 JSON：{"hits": [{"item_id": "<关注项id>", "msg_ids": [<整数>], "reason": "<一句话>"}]}。没有命中就 {"hits": []}。
