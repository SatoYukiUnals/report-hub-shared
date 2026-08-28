#!/usr/bin/env python3
"""チャットに書いた長い成果物を、HTML レポートに出し直すよう促す。

Stop フックとして登録する。運用ルールは「調査結果・作業計画・実施結果・レビューは
report-hub の HTML で出す。チャット本文への箇条書きだけで済ませない」と決めているが、
文章で書いてあるだけなので読み落とす。そこを機械的に拾う。

止めるのは次のすべてに当てはまるときだけ。
  1. 最後の返答が長い（既定 1200 文字以上）
  2. 成果物の形をしている（見出し・箇条書き・表が一定数ある）
  3. そのターンで report-hub の reports/ 配下へ書き出していない
  4. 返答の中に report-hub の URL が出てこない

2 回目は素通しする（stop_hook_active）。判断そのものは AI に任せ、
このフックは「検討したか」を問うだけに留める。出し直すかどうかは内容次第で、
会話の続き・短い確認・コードの説明はそのままでよい。
"""
import json
import re
import sys

# 判定のしきい値。厳しめにして、普通のやり取りでは止まらないようにする
MIN_CHARS = 1200
MIN_STRUCTURE = 6  # 見出し・箇条書き・表の行数の合計

REPORT_DIR = "report-hub/reports/"
REPORT_URL = "localhost:5180/r/"


def read_transcript(path):
    """トランスクリプト（jsonl）を読む。読めなければ空で返す。"""
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []


def text_of(entry):
    """1 エントリのテキスト部分をつなげて返す。"""
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def last_turn(entries):
    """直近のユーザー発言より後ろ（＝このターン）を切り出す。"""
    for i in range(len(entries) - 1, -1, -1):
        if entries[i].get("type") == "user":
            return entries[i + 1 :]
    return entries


def wrote_report(turn):
    """このターンで report-hub のレポートを書き出したか。"""
    for entry in turn:
        message = entry.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            payload = json.dumps(block.get("input", {}), ensure_ascii=False)
            if name in ("Write", "Edit", "NotebookEdit") and REPORT_DIR in payload:
                return True
            # シェル経由で書くこともある（heredoc・python -c・cp）
            if name == "Bash" and REPORT_DIR in payload:
                return True
    return False


def structure_score(text):
    """成果物らしさ。見出し・箇条書き・表の行数を数える。"""
    score = 0
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^#{1,6}\s", stripped):
            score += 2
        elif re.match(r"^[-*]\s+\S", stripped):
            score += 1
        elif re.match(r"^\d+\.\s+\S", stripped):
            score += 1
        elif stripped.startswith("|") and stripped.endswith("|"):
            score += 1
        elif re.match(r"^\*\*[^*]+\*\*", stripped):
            score += 2
    return score


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        sys.exit(0)

    # 2 周目は素通し（同じことを繰り返し聞かない）
    if payload.get("stop_hook_active"):
        sys.exit(0)

    entries = read_transcript(payload.get("transcript_path", ""))
    if not entries:
        sys.exit(0)

    turn = last_turn(entries)
    if wrote_report(turn):
        sys.exit(0)

    replies = [text_of(e) for e in turn if e.get("type") == "assistant"]
    reply = "\n".join(t for t in replies if t).strip()
    if len(reply) < MIN_CHARS:
        sys.exit(0)
    if REPORT_URL in reply:
        sys.exit(0)
    if structure_score(reply) < MIN_STRUCTURE:
        sys.exit(0)

    print(
        "いまの返答は、チャット本文に成果物をそのまま書いている可能性がある。\n"
        "運用ルールでは、調査結果・作業計画・実施結果・レビューは report-hub の HTML レポートで出し、\n"
        "チャットには結論と URL だけを短く書くことになっている。\n"
        "\n"
        "この返答がその 4 つのどれかに当たるなら、レポートに出し直してから答え直すこと。\n"
        "  - テンプレートを複製して起こす（survey / plan / result / review / slide）\n"
        "  - 判断を仰ぐ箇所には設問を、その論点を説明した位置に置く\n"
        "  - チャットは結論を数行 ＋ 最後の行に URL\n"
        "\n"
        "当たらないなら（会話の続き・短い確認・コードや仕組みの説明・ユーザーの質問への直接の回答）、\n"
        "そのまま同じ内容で答え直してよい。この確認は 1 ターンにつき 1 回だけ出る。",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
