"""Message -> project routing. Four strategies, in order:

0. mention matches bot_mention_key AND there is exactly one project
   (single-bot deployment) -> direct route, ignore chat_ids whitelist
1. mention matches bot_mention_key AND chat_id in project.chat_ids
2. fallback: chat_id in any project.chat_ids
3. fallback: keyword (case-insensitive substring) in text

Strategy 0 rationale: in a single-project deployment, the user expects
'@bot anywhere' to work without needing chat_ids whitelist updates every
time the bot is added to a new group. Constraining S0 to len(projects)==1
avoids cross-project ambiguity in multi-tenant deployments.

Fall-through semantics: if Strategy 1 finds a mention match but no project
has a matching chat_id, routing continues to Strategy 2 (and then 3). This
is intentional: a config lag (bot joined group but chat_ids not yet updated)
should still get best-effort routing rather than silent drop.

When multiple projects match the same strategy, returns the first by
insertion order (config.yaml top-to-bottom).

Substring matching (Strategy 3) is character-level; Chinese keywords like
'计费' will match '计费用品'. Prefer unique identifiers in routing_keywords.
"""

import logging

from agent_runtime.channels import ParsedMsg

log = logging.getLogger(__name__)


def route(
    parsed: ParsedMsg,
    projects: dict,
    *,
    bot_mention_key: str | None = None,
) -> tuple[str, dict] | None:
    """Return (project_name, project_config) or None if no match."""
    # DEBUG-level routing trace: surfaces the exact inputs a route decision was
    # made on (sender_type / mentions / chat are the usual culprits when a
    # message routes to the wrong project or silently drops). Off by default;
    # enable by setting this logger to DEBUG. Kept terse to avoid per-message
    # INFO spam (the scheduler already logs the dispatched decision at INFO).
    log.debug(
        "route_attempt msg=%s chat=%s chat_type=%s sender_type=%r mentions=%r text_head=%r",
        parsed.message_id, parsed.chat_id, parsed.chat_type,
        parsed.sender_type, parsed.mentions, (parsed.text or "")[:60],
    )
    # Strategy 0: single-project deployment direct routing.
    # Triggers when *either* condition holds + exactly one project exists:
    #   (a) bot is @mentioned (group with explicit summon)
    #   (b) chat is p2p (DM — by definition unambiguous, mention impossible)
    # Without (b), DM users have to type a routing keyword every message,
    # which violates the natural "1:1 chat = direct conversation" expectation.
    if len(projects) == 1:
        is_mentioned = bool(
            bot_mention_key and bot_mention_key in parsed.mentions
        )
        is_p2p = parsed.chat_type == "p2p"
        if is_mentioned or is_p2p:
            return next(iter(projects.items()))

    # Strategy 1: mention + chat_id
    if bot_mention_key and bot_mention_key in parsed.mentions:
        for name, cfg in projects.items():
            if parsed.chat_id in cfg.get("chat_ids", []):
                return name, cfg

    # Strategy 2: chat_id only — only EXPLICIT bot-like senders route via
    # chat_id whitelist. Confirmed human messages (sender_type='user') are
    # skipped (S2-fix-1, 2026-05-11). Now also: sender_type==None / unknown
    # is treated as human (was legacy permissive, but caused thread/topic
    # replies in alert chats to trigger deep-branch claude — 2026-05-19
    # production case: 吴骁恺 @ 唐文博 in LBP 计费告警群 thread, bot
    # replied because sender_type=None leaked through).
    BOT_SENDER_TYPES = {"app", "bot", "webhook"}
    if parsed.sender_type in BOT_SENDER_TYPES:
        for name, cfg in projects.items():
            if parsed.chat_id in cfg.get("chat_ids", []):
                return name, cfg

    # Strategy 3: keyword in text.
    # In group chats keyword is ONLY a disambiguator for an explicit @mention,
    # never a standalone trigger — otherwise the bot answers every human
    # message that merely contains a domain term (production case 2026-06-01:
    # single-project LBP 计费群, keywords ['lbp','计费','额度','billing',...]
    # matched nearly every line of human chatter, so the bot "took over the
    # whole group"). Contract: group chats respond only to an explicit summon
    # (@mention via S0a/S1) or a bot-sender whitelist (S2). p2p / unknown
    # chat_type keep the legacy free keyword routing — p2p can't @ anyone, and
    # None preserves backward-compat for callers that don't pass chat_type.
    is_group = parsed.chat_type == "group"
    is_mentioned = bool(bot_mention_key and bot_mention_key in parsed.mentions)
    if not is_group or is_mentioned:
        lower = parsed.text.lower()
        for name, cfg in projects.items():
            for kw in cfg.get("routing_keywords", []):
                if kw.lower() in lower:
                    return name, cfg

    return None
