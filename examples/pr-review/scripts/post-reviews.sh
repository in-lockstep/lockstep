#!/usr/bin/env bash
# Publish each aspect's findings as its own pull request review.
#
# A thin wrapper: the work is in the composite action, which is where it has to be because posting a
# review needs `gh` and the job's token. This exists so the step reads as part of the pipeline rather
# than as an overlay somebody has to go looking for.
set -euo pipefail

pr=""
reviews=""
diff=""
while [ $# -gt 0 ]; do
  case "$1" in
    --pr=*) pr="${1#*=}" ;;
    --reviews=*) reviews="${1#*=}" ;;
    --diff=*) diff="${1#*=}" ;;
  esac
  shift
done

if [ -z "$pr" ]; then
  echo "no pull request to post to" >&2
  exit 0
fi
if [ ! -d "$reviews" ] || [ -z "$(find "$reviews" -name '*.json' 2>/dev/null | head -1)" ]; then
  echo "nothing to post: no aspect needed reviewing"
  exit 0
fi

commit=""
[ -f "$diff" ] && commit=$(jq -r '.head_sha // ""' "$diff")

posted=0
for file in "$reviews"/*.json; do
  [ -e "$file" ] || continue
  aspect=$(basename "$file" .json)
  previous=$(jq -r '.previous_review_id // empty' "$file")
  marker="<!-- lockstep:review aspect=${aspect} sha=${commit} -->"

  body=$(jq -r --arg a "$aspect" --arg marker "$marker" '
    $marker + "\n## " + (.title // ($a | ascii_upcase)) + " review\n\n" +
    (.summary // "No findings.") +
    (if (.findings // [] | length) > 0 then
      "\n\n" + ([.findings[] | "- **" + (.path // "general") +
        (if .line then ":" + (.line|tostring) else "" end) + "** — " + .comment] | join("\n"))
     else "" end)
  ' "$file")

  if [ -n "$previous" ]; then
    # A submitted review's body can be updated; its inline comments cannot. The revision therefore
    # goes into the body, and the thread keeps one review per aspect however often the branch moves.
    jq -n --arg body "$body" '{body: $body}' \
      | gh api -X PUT "repos/${GITHUB_REPOSITORY}/pulls/${pr}/reviews/${previous}" --input - >/dev/null \
      && { posted=$((posted + 1)); echo "revised the $aspect review"; } \
      || echo "::warning::could not revise the $aspect review"
  else
    comments=$(jq -c '[ (.findings // [])[] | select(.path != null and .line != null)
                        | {path, line, side: "RIGHT", body: .comment} ]' "$file")
    jq -n --arg body "$body" --argjson comments "$comments" --arg sha "$commit" \
       '{body: $body, event: "COMMENT", comments: $comments}
        + (if $sha == "" then {} else {commit_id: $sha} end)' \
      | gh api -X POST "repos/${GITHUB_REPOSITORY}/pulls/${pr}/reviews" --input - >/dev/null \
      && { posted=$((posted + 1)); echo "posted the $aspect review"; } \
      || echo "::warning::could not post the $aspect review"
  fi
done

echo "posted or revised $posted review(s)"
echo "posted or revised $posted review(s)" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
