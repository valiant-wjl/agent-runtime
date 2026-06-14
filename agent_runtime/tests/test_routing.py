"""Tests for runtime/routing.py — migrated from feishu-agent-gateway."""

from agent_runtime.channels import ParsedMsg
from agent_runtime import routing

PROJECTS = {
    "billing": {
        "work_dir": "/tmp/billing",
        "display_name": "BillingBot",
        "routing_keywords": ["billing", "计费"],
        "chat_ids": ["oc_billing"],
    },
    "platform": {
        "work_dir": "/tmp/platform",
        "display_name": "PlatformBot",
        "routing_keywords": ["platform"],
        "chat_ids": ["oc_platform"],
    },
}
BOT_MENTION = "ou_bot_xxx"


def _msg(**kwargs) -> ParsedMsg:
    defaults = dict(
        channel="feishu",
        message_id="m1",
        thread_root_id="t1",
        chat_id="oc_billing",
        sender_id="ou_user",
        sender_name="user",
        text="",
        mentions=[],
    )
    defaults.update(kwargs)
    return ParsedMsg(**defaults)


def test_strategy2_skips_human_messages_in_whitelisted_chat():
    """A confirmed human (sender_type='user') in a whitelisted chat with no
    mention and no keyword match must NOT be routed. Pre-fix this returned
    ('billing', cfg) and burned a deep Claude turn on idle group chatter
    (production case: Aily 告警群 with humans typing amongst themselves).
    """
    msg = _msg(
        chat_id="oc_billing", mentions=[], sender_type="user", text="一件件来",
    )
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    assert result is None


def test_strategy2_still_routes_app_messages_in_whitelisted_chat():
    """Bot/webhook messages (sender_type='app') must still match S2 — that's
    how alert webhooks get dispatched without anyone @-ing the bot."""
    msg = _msg(
        chat_id="oc_billing", mentions=[], sender_type="app", text="alert payload",
    )
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    assert result is not None
    name, _cfg = result
    assert name == "billing"


def test_strategy1_unchanged_for_human_with_mention():
    """Human user who @-mentions the bot in a whitelisted chat still routes
    via S1, regardless of the S2 tightening."""
    msg = _msg(
        chat_id="oc_billing", mentions=[BOT_MENTION], sender_type="user",
        text="hey bot",
    )
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    assert result is not None
    name, _cfg = result
    assert name == "billing"


def test_route_by_mention():
    """mention 含 BOT_MENTION 且 chat_id='oc_billing' → 返回 ('billing', cfg)."""
    msg = _msg(mentions=[BOT_MENTION], chat_id="oc_billing")
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    assert result is not None
    name, cfg = result
    assert name == "billing"
    assert cfg["display_name"] == "BillingBot"


def test_route_by_chat_id_only():
    """bot 发送者(sender_type='app') + chat_id='oc_platform' → S2 返回 ('platform', cfg).

    S2 (chat_id-only) 自 2026-05-11/05-19 起仅对 bot 类发送者生效（修复告警群
    人类 thread 回复误触发）；故必须显式 sender_type='app' 才走 chat_id 路由。
    """
    msg = _msg(mentions=[], chat_id="oc_platform", sender_type="app")
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    assert result is not None
    name, cfg = result
    assert name == "platform"
    assert cfg["display_name"] == "PlatformBot"


def test_route_no_match():
    """mention 空 + chat_id='oc_unknown' + text 不含 keyword → 返回 None."""
    msg = _msg(mentions=[], chat_id="oc_unknown", text="hello world")
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    assert result is None


def test_strategy_3_keyword_matches():
    """Keyword substring match（no mention, no chat_id match）."""
    msg = _msg(chat_id="oc_unknown", text="帮我查下 billing 配置", mentions=[])
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    assert result is not None
    assert result[0] == "billing"


def test_mention_match_but_no_chat_id_falls_through_to_keyword():
    """mention 命中 bot 但 chat_id 无 project, 应 fall-through 到 keyword."""
    msg = _msg(mentions=[BOT_MENTION], chat_id="oc_unknown", text="查 platform 状态")
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    assert result is not None
    assert result[0] == "platform"


def test_multiple_projects_same_chat_id_returns_first():
    """多 project chat_ids 交集时, 返回 dict 插入顺序第一个."""
    projs = {
        "p_first": {"chat_ids": ["oc_shared"], "routing_keywords": []},
        "p_second": {"chat_ids": ["oc_shared"], "routing_keywords": []},
    }
    msg = _msg(chat_id="oc_shared", mentions=[], sender_type="app")
    result = routing.route(msg, projs, bot_mention_key=BOT_MENTION)
    assert result is not None
    assert result[0] == "p_first"


def test_single_project_mention_routes_without_chat_id_whitelist():
    """Strategy 0: 单 project 部署 + bot 被 @ → 直接路由, 无视 chat_ids 白名单.

    Why: 让 'bot 被拉进新群 + 立刻可用' 成为默认体验, 避免每加一个客户群
    都要改 config.chat_ids + 重启。多 project 部署仍走 S1/S2/S3（保留歧义解析）。
    """
    single = {
        "spring_billing": {
            "chat_ids": ["oc_already_listed"],   # 白名单里已有别的群
            "routing_keywords": ["计费"],
            "display_name": "lbp-growth-agent",
        }
    }
    # 关键: chat_id 不在白名单, 文本也不含 keyword, 但 bot 被 @ → 仍应命中
    msg = _msg(mentions=[BOT_MENTION], chat_id="oc_brand_new_group", text="你好")
    result = routing.route(msg, single, bot_mention_key=BOT_MENTION)
    assert result is not None
    assert result[0] == "spring_billing"


def test_single_project_no_mention_no_chat_no_keyword_returns_none():
    """Strategy 0 不应在没 @ 时触发: 防止 bot 接管整个群所有消息."""
    single = {
        "only": {
            "chat_ids": [],
            "routing_keywords": ["计费"],
            "display_name": "OnlyBot",
        }
    }
    msg = _msg(mentions=[], chat_id="oc_random", text="今天天气不错")
    result = routing.route(msg, single, bot_mention_key=BOT_MENTION)
    assert result is None


def test_multi_project_mention_does_not_use_strategy_0():
    """多 project 时不走 Strategy 0 (歧义), 必须靠 S1/S2/S3 解析."""
    msg = _msg(mentions=[BOT_MENTION], chat_id="oc_unknown", text="hello")
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    # PROJECTS 有 2 个, S0 不应触发; chat_id 不在任何白名单, 文本无 keyword → None
    assert result is None


# ---------------------------------------------------------------------------
# Strategy 0 — p2p chat_type extension
# ---------------------------------------------------------------------------


def test_p2p_chat_type_routes_single_project_without_mention():
    """单 project + p2p (私聊) → 直接路由, 不要求 mention 或 keyword.

    Why: feishu 私聊里没法 @ 自己/对面, 所以 mention 永远空; 用户期望"私聊
    bot 就是和它 1:1 对话", 不应该再要求他在每条消息里带 keyword.
    """
    single = {
        "spring_billing": {
            "chat_ids": [],
            "routing_keywords": ["计费"],
            "display_name": "lbp-growth-agent",
        }
    }
    msg = _msg(
        mentions=[],
        chat_id="oc_p2p_with_bot",
        text="你好",
        chat_type="p2p",
    )
    result = routing.route(msg, single, bot_mention_key=BOT_MENTION)
    assert result is not None
    assert result[0] == "spring_billing"


def test_group_chat_type_without_mention_does_not_use_strategy_0():
    """群聊 + 无 mention + 无 keyword → 不走 S0, 返回 None.

    防止 bot 进任何群就接管所有消息的反模式 (S0 必须保守).
    """
    single = {
        "only": {
            "chat_ids": [],
            "routing_keywords": ["计费"],
            "display_name": "OnlyBot",
        }
    }
    msg = _msg(
        mentions=[],
        chat_id="oc_random_group",
        text="今天天气不错",
        chat_type="group",
    )
    result = routing.route(msg, single, bot_mention_key=BOT_MENTION)
    assert result is None


def test_p2p_chat_type_multi_project_does_not_use_strategy_0():
    """多 project 部署即使是 p2p 也不走 S0 (歧义), 仍走 S1/S2/S3."""
    msg = _msg(
        mentions=[],
        chat_id="oc_unknown",
        text="hello",
        chat_type="p2p",
    )
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    # PROJECTS 有 2 个, p2p 不能消歧; chat_id 不在白名单, 文本无 keyword → None
    assert result is None


def test_chat_type_none_falls_back_to_legacy_strategies():
    """旧调用没传 chat_type (None) → 表现与改造前完全一致 (向后兼容)."""
    single = {
        "spring_billing": {
            "chat_ids": [],
            "routing_keywords": ["计费"],
            "display_name": "B",
        }
    }
    # 无 mention, 无 keyword, chat_type=None → 应当 None (不能假设 p2p)
    msg = _msg(mentions=[], chat_id="oc_x", text="你好", chat_type=None)
    result = routing.route(msg, single, bot_mention_key=BOT_MENTION)
    assert result is None


# ---------------------------------------------------------------------------
# Strategy 3 — group chats never trigger on keyword alone
# ---------------------------------------------------------------------------


def test_group_chat_keyword_alone_does_not_route():
    """群聊 + 无 mention + 命中 routing_keyword → 仍返回 None.

    复现 bug: 单/多 project 部署里, bot 被拉进领域群 (e.g. LBP 计费群),
    群里人类互相闲聊只要含领域词 (lbp / 计费 / billing) 就被 S3 路由,
    导致 bot "接管整个群". 用户契约: 群聊只回应显式 @ 召唤. 故 S3 在群聊
    里只能作为 @ 后的 project 消歧器, 绝不单独触发.
    """
    msg = _msg(
        chat_id="oc_unknown",
        text="帮我查下 billing 配置",
        mentions=[],
        chat_type="group",
    )
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    assert result is None


def test_group_chat_keyword_with_mention_still_disambiguates():
    """群聊 + @bot + chat_id 不在白名单 + 文本含某 project keyword
    → S1 fall-through 到 S3, 用 keyword 选中对应 project.

    群聊里 keyword 仍是合法的消歧手段 —— 前提是有显式 @ 召唤
    (config lag: bot 进群但 chat_ids 还没更新, 仍应 best-effort 路由).
    """
    msg = _msg(
        mentions=[BOT_MENTION],
        chat_id="oc_unknown",
        text="查 platform 状态",
        chat_type="group",
    )
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    assert result is not None
    assert result[0] == "platform"


def test_p2p_chat_keyword_alone_still_routes_multi_project():
    """多 project p2p 私聊 (无法 @) 仍可用 keyword 消歧, S3 在 p2p 保留."""
    msg = _msg(
        chat_id="oc_unknown",
        text="查 platform 状态",
        mentions=[],
        chat_type="p2p",
    )
    result = routing.route(msg, PROJECTS, bot_mention_key=BOT_MENTION)
    assert result is not None
    assert result[0] == "platform"
