#!/usr/bin/env python3
"""bin/check-reports.py の長文検査（check_slide_prose_length）の判定表。

Python 標準の unittest だけで走る。Docker も依存パッケージも要らない。

    python3 tests/test_check_reports_length.py

1 ケース 1 行の表（CASES）にしてあり、次の 3 方向を必ず含む。

  - 肯定側 …… 通るべきもの（短い p・1 文の li・details/pre/code の中の長文）が
    警告なしで通る。
  - 否定側 …… 弾かれるべきもの（61 字の p・2 文の dd・長い td）が警告になる。
  - 取りこぼし側 …… この検査が「何を見ていないか」を先に書き出し、それを
    実際に確かめる。ここに挙げたものは検査の対象外だと知ったうえで残す
    割り切りであり、拾えるようにする改善ではない。

判定が見ていない場所（取りこぼし側として洗い出したもの）:
  - p・li・dd・td 以外のタグ（span・h3 など）に書いた長文はそもそも対象タグでは
    ないので見ない。
  - 文の区切りを「。」の有無でしか数えていない。<br> で見た目だけ改行した文・
    全角の「．」「！」で終わる文は、「。」を含まなければ数えられない。
  - 入れ子（li の中の li 等）は内側だけを個別に数え、外側の直接テキストと
    合算しない。外側と内側それぞれは 60 字以内でも、合わせて 60 字を超える
    書き方は拾わない。
  - 「」や（）の中の「。」も区別しない。括弧の中で文が終わっていても、外の
    文が続いていても、同じ「後ろに文字が続く「。」」として扱う（内と外を
    見分けない割り切り）。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER_PATH = ROOT / "bin" / "check-reports.py"

# ファイル名にハイフンが入っているので、ふつうの import ではなく
# importlib でモジュールとして読み込む。
_spec = importlib.util.spec_from_file_location("check_reports", CHECKER_PATH)
check_reports = importlib.util.module_from_spec(_spec)
sys.modules["check_reports"] = check_reports
assert _spec.loader is not None
_spec.loader.exec_module(check_reports)

check_slide_prose_length = check_reports.check_slide_prose_length


def wrap(body: str, title: str = "テスト枚") -> str:
    """検査対象の本文を、スライド様式の最小限の HTML に包む。"""
    return (
        '<!doctype html><html><head>'
        '<link rel="stylesheet" href="/assets/slide.css">'
        "</head><body>\n"
        f'<section class="slide" data-title="{title}">\n'
        f"{body}\n"
        "</section>\n"
        "</body></html>"
    )


# 60 字ちょうど（1 文）／61 字（1 文）を作るための材料。
J60 = "あ" * 59 + "。"  # 60 字・「。」1 個
J61 = "あ" * 60 + "。"  # 61 字・「。」1 個
LONG_NO_PERIOD = "あ" * 70  # 70 字・句点なし
TWO_SENTENCES_SHORT = "文一終わり。文二終わり。"  # 12 字・「。」2 個
TRAILING_PERIOD_ONLY = "短い一文で終わる。"  # 末尾だけ「。」・1 文
NO_TRAILING_PERIOD = "句点で終わらない一文"  # 「。」なし・1 文
NO_PERIOD_AT_END_TWO_SENTENCES = "一文目だ。二文目は句点なし"  # 「。」の後ろに文字が続く

# (id, 方向, 本文 HTML, 警告が出るべきか, 説明)
CASES: list[tuple[str, str, str, bool, str]] = [
    # --- 肯定側：通るべきものが警告なしで通る ---
    (
        "ok-br-splits-sentences",
        "肯定側",
        "<td>一文目で終わる。<br>二文目も終わる。</td>",
        False,
        "<br> で行を分けた 2 文は、見た目が 1 文ずつの改行なので警告なし",
    ),
    (
        "ok-br-each-line-short",
        "肯定側",
        f"<p>{J60}<br/>{J60}</p>",
        False,
        "<br/> で分けた各行が 60 字以内なら、合計が 60 字を超えても警告なし",
    ),
    (
        "ng-br-one-line-long",
        "否定側",
        f"<td>{J61}<br>短い</td>",
        True,
        "<br> で分けても、1 行が 60 字を超えれば警告あり",
    ),
    (
        "ng-br-line-has-two-sentences",
        "否定側",
        "<td>一文目。二文目。<br>三文目</td>",
        True,
        "<br> で分けた 1 行の中に 2 文あれば警告あり",
    ),
    (
        "ok-p-short",
        "肯定側",
        f"<p>{J60}</p>",
        False,
        "60 字ちょうど・1 文の p は警告なし",
    ),
    (
        "ok-li-one-sentence",
        "肯定側",
        f"<li>{J60}</li>",
        False,
        "1 文だけの短い li は警告なし",
    ),
    (
        "ok-trailing-period-only",
        "肯定側",
        f"<p>{TRAILING_PERIOD_ONLY}</p>",
        False,
        "「。」が末尾に 1 個だけ（後ろに文字が続かない）なら 1 文として警告なし",
    ),
    (
        "ok-no-period",
        "肯定側",
        f"<p>{NO_TRAILING_PERIOD}</p>",
        False,
        "「。」が無い短文はそもそも 1 文なので警告なし",
    ),
    (
        "ok-trailing-space-after-period",
        "肯定側",
        f"<p>{TRAILING_PERIOD_ONLY} </p>",
        False,
        "末尾の「。」の後ろに空白だけが続く場合は、空白を除くので 1 文のまま警告なし",
    ),
    (
        "ok-details-long",
        "肯定側",
        f"<details><summary>根拠を見る</summary><p>{TWO_SENTENCES_SHORT}{LONG_NO_PERIOD}</p></details>",
        False,
        "details の中の長文・複数文は対象外（根拠は畳んでよい）",
    ),
    (
        "ok-code-long",
        "肯定側",
        f"<pre><code>{LONG_NO_PERIOD}{TWO_SENTENCES_SHORT}</code></pre>",
        False,
        "pre・code の中の長文は対象外（コードは検査しない）",
    ),
    (
        "ok-qa-textarea-long",
        "肯定側",
        (
            '<div class="qa" data-qa-id="q1" data-question="質問">'
            f"<textarea>{LONG_NO_PERIOD}{TWO_SENTENCES_SHORT}</textarea>"
            "</div>"
        ),
        False,
        "div.qa の中の textarea（補足欄）の長文は対象外",
    ),
    # --- 否定側：弾かれるべきものが警告になる ---
    (
        "ng-p-61",
        "否定側",
        f"<p>{J61}</p>",
        True,
        "61 字の p は 60 字超で警告",
    ),
    (
        "ng-dd-two-sentences",
        "否定側",
        f"<dd>{TWO_SENTENCES_SHORT}</dd>",
        True,
        "1 要素に 2 文（「。」2 個）の dd は警告",
    ),
    (
        "ng-td-long",
        "否定側",
        f"<td>{LONG_NO_PERIOD}</td>",
        True,
        "60 字超の td は警告",
    ),
    (
        "ng-no-trailing-period-two-sentences",
        "否定側",
        f"<p>{NO_PERIOD_AT_END_TWO_SENTENCES}</p>",
        True,
        "「A。B」のように最後の文に「。」が無い 2 文は、「。」の後ろに文字が"
        "続くので警告（旧ロジックは「。」が 1 個しかないので見逃していた）",
    ),
    # --- 取りこぼし側：判定が見ていない場所を、先に書き出したうえで確かめる ---
    (
        "gap-span-only",
        "取りこぼし側",
        f"<div><span>{LONG_NO_PERIOD}</span></div>",
        False,
        "span だけで書いた長文は対象タグ（p/li/dd/td）に入っていないので拾わない",
    ),
    (
        "gap-h3-long",
        "取りこぼし側",
        f"<h3>{LONG_NO_PERIOD}</h3>",
        False,
        "h3 の長文も対象タグではないので拾わない",
    ),
    (
        "gap-br-two-lines-no-period",
        "取りこぼし側",
        "<p>一行目はここまで<br>二行目もここまで</p>",
        False,
        "br で見た目だけ 2 行に分けても「。」が無ければ 2 文と数えない"
        "（文字数も 60 字未満なので長さでも拾わない）",
    ),
    (
        "gap-fullwidth-period-and-bang",
        "取りこぼし側",
        "<p>一文目だ．二文目だ！</p>",
        False,
        "全角の「．」「！」で終わる文は「。」の個数でしか数えていないため"
        "2 文とは判定しない",
    ),
    (
        "gap-nested-li-combined-over-60",
        "取りこぼし側",
        (
            "<li>"
            + "あ" * 35
            + "<ul><li>" + "い" * 35 + "</li></ul>"
            + "う" * 5
            + "</li>"
        ),
        False,
        "入れ子の li は内側だけを個別に数える。外側の直接テキスト（35+5=40 字）と"
        "内側（35 字）はどちらも 60 字以内なので、合わせて 75 字あっても拾わない",
    ),
    (
        "gap-period-inside-quote",
        "取りこぼし側",
        "<p>『済。』と書く運用にした</p>",
        True,
        "括弧の中の「。」（『済。』）も外の「。」と区別せず、後ろに文字が続く"
        "「。」として同じように警告にする（内と外を見分けない割り切り）",
    ),
]


class CheckSlidePrewLengthTest(unittest.TestCase):
    pass


def _make_test(case_id: str, direction: str, body: str, expect_warning: bool, note: str):
    def test(self: unittest.TestCase) -> None:
        warnings = check_slide_prose_length(wrap(body))
        if expect_warning:
            self.assertTrue(
                warnings,
                f"[{direction}/{case_id}] 警告が出るべきだが出なかった。{note}",
            )
        else:
            self.assertEqual(
                warnings,
                [],
                f"[{direction}/{case_id}] 警告が出ないべきだが出た: {warnings}\n{note}",
            )

    return test


for _case_id, _direction, _body, _expect, _note in CASES:
    setattr(
        CheckSlidePrewLengthTest,
        f"test_{_case_id.replace('-', '_')}",
        _make_test(_case_id, _direction, _body, _expect, _note),
    )


class NonSlideReportIsSkippedTest(unittest.TestCase):
    """/assets/slide.css を読まない（＝スライド様式でない）ものは検査しない。"""

    def test_non_slide_report_not_checked(self) -> None:
        html = (
            "<!doctype html><html><head>"
            '<link rel="stylesheet" href="/assets/report.css">'
            "</head><body>"
            f"<p>{LONG_NO_PERIOD}</p>"
            "</body></html>"
        )
        self.assertEqual(check_slide_prose_length(html), [])



# 長文の検査を当てる範囲（ファイル名の日付で決める）の判定表。
# (パス, 当てるか, 説明)
SCOPE_CASES: list[tuple[str, bool, str]] = [
    ("reports/example/2026-09-28_x.html", True, "肯定側：始めた日のレポートは当てる"),
    ("reports/example/2026-10-01_x.html", True, "肯定側：それより後のレポートは当てる"),
    ("templates/slide.html", True, "肯定側：雛形は日付が無くても当てる"),
    ("reports/example/2026-09-27_x.html", False, "否定側：前日のレポートは当てない"),
    ("reports/example/done/2026-09-07_x.html", False, "否定側：done/ の過去分も当てない"),
    ("reports/example/notes.html", False, "取りこぼし側：日付で始まらない名前は当てない"),
    ("reports/templates/2026-01-01_x.html", True, "取りこぼし側：templates という名のフォルダなら日付に関係なく当たる"),
]


class ProseCheckScopeTest(unittest.TestCase):
    def test_scope_table(self) -> None:
        for path, expected, desc in SCOPE_CASES:
            with self.subTest(path=path, desc=desc):
                self.assertEqual(check_reports.prose_check_applies(Path(path)), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
