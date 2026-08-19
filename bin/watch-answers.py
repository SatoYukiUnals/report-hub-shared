#!/usr/bin/env python3
"""レポートの回答を待ち受ける。回答が入るたびに 1 行出す。

AI（Claude Code）が Monitor から呼ぶ前提。標準出力の 1 行が 1 通知になり、
回答が来た時点で AI が起こされる。全設問に回答が入ったら終了する。

  python3 bin/watch-answers.py <プロジェクト> <名前> [--done] [--interval 秒]

出力（1 行 1 件）:
  [回答] qa-1 → 取消 ／ 対象画面が消えたため
  [完了] 6 件すべてに回答が入った
  [エラー] レポートがない: reports/wbs_site/xxx.html

終了コード: 0 全問回答, 1 レポートがない。
時間切れは呼び出し側（Monitor の timeout）が打ち切る。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = BASE_DIR / "reports"
QA_ID = re.compile(r"""data-qa-id\s*=\s*["']([^"']+)["']""")
SAMPLE = re.compile(r"<!--.*?-->|<pre\b.*?</pre>|<code\b.*?</code>", re.S | re.I)


def question_ids(html_path: Path) -> list[str]:
    """設問 ID。server.py と同じ規則（コメント・pre・code の中は数えない）。"""
    text = SAMPLE.sub(" ", html_path.read_text("utf-8"))
    return list(dict.fromkeys(QA_ID.findall(text)))


def read_answers(path: Path) -> dict[str, dict]:
    """qa_id → 回答。まだ無い・壊れている場合は空。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (ValueError, OSError):
        return {}
    return {a["qa_id"]: a for a in data if isinstance(a, dict) and a.get("qa_id")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("name")
    parser.add_argument("--done", action="store_true", help="完了へ移したレポートを見る")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    folder = REPORTS_DIR / args.project / ("done" if args.done else "")
    html_path = folder / f"{args.name}.html"
    answers_path = folder / f"{args.name}.answers.json"
    if not html_path.is_file():
        print(f"[エラー] レポートがない: {html_path.relative_to(BASE_DIR)}")
        return 1

    wanted = question_ids(html_path)
    if not wanted:
        print(f"[完了] 設問がない: {args.name}")
        return 0

    # 起動時点の回答は「既に答えたもの」として扱い、以後の変化だけを出す
    seen = {qa_id: a.get("answered_at") for qa_id, a in read_answers(answers_path).items()}
    left = [q for q in wanted if q not in seen]
    print(f"[待受] {args.name}：{len(wanted)} 問中 {len(left)} 問が未回答")
    if not left:
        print(f"[完了] {len(wanted)} 件すべてに回答が入っている")
        return 0

    while True:
        time.sleep(args.interval)
        current = read_answers(answers_path)
        for qa_id in wanted:
            entry = current.get(qa_id)
            if entry is None or seen.get(qa_id) == entry.get("answered_at"):
                continue
            seen[qa_id] = entry.get("answered_at")
            choice = entry.get("choice") or "(選択なし)"
            note = f"　／　{entry['note']}" if entry.get("note") else ""
            print(f"[回答] {qa_id} → {choice}{note}")
        if all(q in seen for q in wanted):
            print(f"[完了] {len(wanted)} 件すべてに回答が入った")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
