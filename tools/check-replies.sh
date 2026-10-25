#!/usr/bin/env bash
# 检查 bh2-auto 仓库有没有新回复：issue、PR、评论、通知。
# 用法: bash tools/check-replies.sh [owner/repo]
# 状态存在 .state/replies.json，只报上次检查之后的新东西。
set -euo pipefail

REPO="${1:-yuesq3016/bh2-auto}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$ROOT/.state"
STATE="$STATE_DIR/replies.json"
mkdir -p "$STATE_DIR"

since="1970-01-01T00:00:00Z"
[[ -f "$STATE" ]] && since=$(grep -oE '"last_check"\s*:\s*"[^"]+"' "$STATE" | sed -E 's/.*"([^"]+)"$/\1/')

now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
found=0

echo "== $REPO 自 $since 起的新动静 =="

# 1) 新的 / 有更新的 issue 和 PR
issues=$(gh api "repos/$REPO/issues?state=all&sort=updated&direction=desc&per_page=50" \
  --jq ".[] | select(.updated_at > \"$since\") | \"#\(.number) [\(.state)] \(.title) — 最后更新 \(.updated_at) by \(.user.login)\"" 2>/dev/null || true)
if [[ -n "$issues" ]]; then
  echo; echo "[Issue / PR]"; echo "$issues"; found=1
fi

# 2) 新评论（issue 与 PR 共用 comments 接口）
comments=$(gh api "repos/$REPO/issues/comments?since=$since&per_page=50" \
  --jq ".[] | \"#\(.issue_url | split(\"/\") | last) \(.user.login): \(.body | gsub(\"\\n\"; \" \") | .[0:120]) ( \(.created_at) )\"" 2>/dev/null || true)
if [[ -n "$comments" ]]; then
  echo; echo "[评论]"; echo "$comments"; found=1
fi

# 3) PR 代码评审意见
reviews=$(gh api "repos/$REPO/pulls?state=all&sort=updated&direction=desc&per_page=20" \
  --jq ".[] | select(.updated_at > \"$since\") | \"PR #\(.number) \(.title) — \(.state) \(.updated_at)\"" 2>/dev/null || true)
if [[ -n "$reviews" ]]; then
  echo; echo "[PR]"; echo "$reviews"; found=1
fi

# 4) 星标数（顺手看一眼）
stars=$(gh api "repos/$REPO" --jq '.stargazers_count' 2>/dev/null || echo '?')
echo; echo "star: $stars"

(( found == 0 )) && echo && echo "（没有新回复）"

printf '{"last_check": "%s", "repo": "%s"}\n' "$now" "$REPO" > "$STATE"
