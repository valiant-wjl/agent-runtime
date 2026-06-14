#!/bin/bash
# Push an OAuth / interactive-auth request to the user's feishu DM via bot.
#
# When an agent (claude-cli setup-token, lark-cli auth login
# auth login, etc.) needs interactive OAuth, this script delivers the
# URL + action description to the user's feishu inbox. User opens link
# on mobile/desktop, authorizes, and replies in the active chat session.
#
# Usage:
#   scripts/push_auth_request.sh "title" "url" ["action description"]
#
# Examples:
#   scripts/push_auth_request.sh \
#     "claude-cli token rotation" \
#     "https://claude.com/cai/oauth/authorize?..." \
#     "点 Authorize，回调 code 贴回 claude 对话"
#
#   scripts/push_auth_request.sh \
#     "lark-cli scope grant" \
#     "https://accounts.feishu.cn/oauth/v1/device/verify?flow_id=...&user_code=XXX-YYY" \
#     "device flow，user_code 已嵌 URL，浏览器点 Authorize 即可"

set -e

TITLE="${1:?title required}"
URL="${2:?url required}"
ACTION="${3:-请打开链接完成授权后回复}"

OPEN_ID="${LARK_SELF_OPEN_ID:-ou_REPLACE_ME}"

MSG="🔐 ${TITLE}

${ACTION}

${URL}

授权后回到当前 claude 会话中告诉我即可。"

env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY NO_PROXY='*' \
  lark-cli im +messages-send \
  --user-id "${OPEN_ID}" \
  --as bot \
  --text "${MSG}" 2>&1 | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    if d.get('ok'):
        print('OK push msg_id=' + d['data']['message_id'])
    else:
        err = d.get('error', {}).get('message', 'unknown')
        print(f'FAIL: {err}')
        sys.exit(1)
except Exception as e:
    print(f'parse err: {e}')
    sys.exit(2)
"
