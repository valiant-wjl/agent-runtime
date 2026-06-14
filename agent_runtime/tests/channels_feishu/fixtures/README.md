# channels/feishu Test Fixtures

## sample_event.json

- **Captured**: 2026-03 (migrated from feishu-agent-gateway)
- **lark-cli version**: (不可考, 沿用 legacy repo 捕获)
- **Event type**: `im.message.receive_v1`
- **Notes**: P2P chat, text message with 1 mention (bot)
- **Key fields tested**:
  - `header.event_type`
  - `event.message.{message_id, chat_id, content, mentions}`
  - `event.sender.sender_id.{open_id, user_id}`

若 lark-cli event schema 升级导致字段漂移，**不要直接改 fixture**，而是启动 `feishu-agent-gateway` 捕获新 event 替换（保持 fixture 真实性）。
