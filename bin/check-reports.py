#!/usr/bin/env python3
"""レポート HTML の書き方を検査する。

CSS が文書順で位置を決めている箇所は、順番を間違えるとそのまま画面が崩れる。
書いた本人は気づきにくいので、出す前にここで弾く。

    python3 bin/check-reports.py                     # reports/ 配下を全部見る
    python3 bin/check-reports.py reports/x/y.html    # ファイルを指定して見る

問題が 1 つでもあれば終了コード 1 で終わる。長文の警告（check_slide_prose_length）は
既定では終了コードに影響しない。件数と中身を画面に出すだけで、既存レポートを
一斉に落とすことはしない。長文の警告を当てるのは、ファイル名の日付が
PROSE_CHECK_SINCE 以降のレポートと templates/ の雛形だけ（過去のレポートは作り直さない決め）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"

# div.change の中に置く span の並び。CSS が「1 列目＝ラベル・2 列目＝本文」を
# 文書順で割り当てるため、この順を崩すと本文がラベル欄に入って崩れる。
CHANGE_ORDER = ("tag", "from", "arrow", "tag to", "to")


class ChangeCardParser(HTMLParser):
    """div.change の中に現れる span の class を、出てきた順に集める。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[tuple[int, list[str]]] = []
        self._depth = 0
        self._current: list[str] | None = None
        self._line = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class") or ""

        if self._current is not None:
            if tag == "div":
                self._depth += 1
            elif tag == "span":
                self._current.append(" ".join(classes.split()))
            return

        if tag == "div" and "change" in classes.split():
            self._current = []
            self._depth = 1
            self._line = self.getpos()[0]

    def handle_endtag(self, tag: str) -> None:
        if self._current is None or tag != "div":
            return
        self._depth -= 1
        if self._depth == 0:
            self.cards.append((self._line, self._current))
            self._current = None


def check_change_cards(text: str) -> list[str]:
    """「いま」→「こうした」のカードの並びを見る。"""
    parser = ChangeCardParser()
    parser.feed(text)

    problems = []
    for line, spans in parser.cards:
        if not spans:
            continue
        expected = [c for c in CHANGE_ORDER if c in spans]
        if spans != expected:
            problems.append(
                f"{line} 行目: change カードの並びが違う。"
                f"いま [{' / '.join(spans)}] → 正しくは [{' / '.join(expected)}]"
            )
    return problems


def check_qa_ids(text: str) -> list[str]:
    """設問の id が重複していないか見る（重複すると回答が上書きされる）。"""
    ids = re.findall(r'data-qa-id="([^"]+)"', text)
    problems = []
    for qa_id in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"data-qa-id が重複している: {qa_id}")
    return problems


def check_submit_button(text: str) -> list[str]:
    """送信ボタンを自分で書いていないか見る（answers.js が 1 つだけ差し込む）。"""
    if re.search(r'<button[^>]*>\s*回答(する)?\s*</button>', text):
        return ["送信ボタンを自分で書いている。answers.js が差し込むので書かない"]
    return []


def check_media_paths(text: str) -> list[str]:
    """画面写真・動画の参照が絶対パスか見る（完了へ移すとリンクが切れるため）。"""
    problems = []
    for src in re.findall(r'<(?:img|video)[^>]*\ssrc="([^"]+)"', text):
        if src.startswith(("/r/", "http://", "https://", "data:")):
            continue
        problems.append(f"画面・動画の参照が絶対パスでない: {src}")
    return problems


def check_slide_default(text: str) -> list[str]:
    """様式はスライドだけ。縦スクロールで書いていたら弾く。

    レポートは slide.html を複製して起こす。例外は無い（表が何本あろうと、
    設問が何問あろうとスライドで出す）。理由を書けば通る抜け道があると、
    レビューのように設問や表が増えるレポートが軒並みそちらへ流れてしまう。
    """
    if "/assets/slide.css" in text:
        return []
    return [
        "縦スクロール様式になっている。レポートはすべてスライド（templates/slide.html）で出す。"
        "例外は無い（表の本数・設問の数を理由にできない）"
    ]


def check_scripts(text: str) -> list[str]:
    """雛形の <script> を落としていないか見る。

    レポートを雛形から複製せずに書き起こすと、末尾の 2 行を落としやすい。
    落ちると回答ボタンが出ず（answers.js が差し込む）、スライドも 1 枚ずつ送れない。
    画面を開いた人にしか分からない壊れ方なので、ここで弾く。
    """
    problems = []
    if 'data-qa-id="' in text and "/assets/answers.js" not in text:
        problems.append(
            "設問があるのに /assets/answers.js を読み込んでいない。回答ボタンが出ない。"
            '末尾に <script src="/assets/answers.js"></script> を置く'
        )
    if "/assets/slide.css" in text and "/assets/slide.js" not in text:
        problems.append(
            "スライド様式なのに /assets/slide.js を読み込んでいない。1 枚ずつ送れない。"
            '末尾に <script src="/assets/slide.js"></script> を置く'
        )
    return problems


def check_qa_answers_dropped(text: str, path: Path) -> list[str]:
    """回答済みの設問が HTML から消えていないか見る。

    設問を書き直す・枚を整理するときに data-qa-id ごと削ってしまうと、
    <名前>.answers.json に残った過去の回答が行き場を失う（迷子になる）。
    """
    answers_path = path.with_name(f"{path.stem}.answers.json")
    if not answers_path.exists():
        return []

    try:
        answers = json.loads(answers_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"{answers_path.name} を読めない（壊れている可能性がある）"]

    ids_in_html = set(re.findall(r'data-qa-id="([^"]+)"', text))
    problems = []
    for qa_id in sorted({a.get("qa_id") for a in answers if isinstance(a, dict) and a.get("qa_id")}):
        if qa_id not in ids_in_html:
            problems.append(
                f"回答済みの設問 {qa_id} が HTML から消えている（answers.json の回答が迷子になっている）"
            )
    return problems


def _load_dataroom_sources() -> dict[str, Path]:
    """docker-compose.yml の volumes から、/d/<プロジェクト> が指す実体パスを読む。

    `~/…:/srv/sources/<名前>:ro` の形の行だけを拾う。<名前> が URL の
    /d/<プロジェクト> にそのまま対応する。
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    sources: dict[str, Path] = {}
    for host, name in re.findall(r"-\s*(\S+):/srv/sources/([^:\s]+):ro", compose):
        sources[name] = Path(os.path.expanduser(host))
    return sources


_DATAROOM_SOURCES: dict[str, Path] | None = None


def check_dataroom_links(text: str) -> list[str]:
    """成果物ビューア（/d/<プロジェクト>/<パス>）のリンク先が実在するか見る。

    docs の再編でファイルが移動・改名されると、レポートに書いたリンクだけが
    取り残されて気づかれない。ここで docker-compose.yml のマウント元と突き合わせる。
    """
    global _DATAROOM_SOURCES
    if _DATAROOM_SOURCES is None:
        _DATAROOM_SOURCES = _load_dataroom_sources()

    problems = []
    for href in re.findall(r'href="(/d/[^"]+)"', text):
        path_part = href.split("?", 1)[0].split("#", 1)[0]
        parts = path_part.split("/", 3)  # ["", "d", "<プロジェクト>", "<残り>"]
        if len(parts) < 3 or not parts[2]:
            continue  # /d/ 直下（プロジェクト一覧そのもの）は対象外

        project = unquote(parts[2])
        base = _DATAROOM_SOURCES.get(project)
        if base is None:
            continue  # docker-compose.yml に無いプロジェクトは判定できないので見ない

        rest = unquote(parts[3]) if len(parts) > 3 else ""
        target = (base / rest) if rest else base
        if not target.exists():
            problems.append(f"成果物ビューアのリンク先が無い: {href}")
    return problems


def check_ui_change_meta(text: str, path: Path, *, require_meta: bool) -> list[str]:
    """実施結果に画面・動画を貼っているかを、自己申告の meta で見る。

    「画面に触ったかどうか」は機械的には判定がぶれるので、書いた本人に
    <meta name="ui-change" content="yes|no"> で申告させ、yes のときだけ
    figure.shot か video が本文にあるかを機械で確かめる。
    meta が無い＝申告していない状態は、require_meta のときだけ指摘する
    （既存レポートを一括で赤くしないため。新しく書くときだけ効く）。
    """
    eyebrow = re.search(r'<p class="eyebrow">([^<]*)</p>', text)
    if not eyebrow or "実施結果" not in eyebrow.group(1):
        return []

    meta = re.search(r'<meta\s+name="ui-change"\s+content="([^"]*)"', text)
    if meta is None:
        if require_meta:
            return [
                '実施結果なのに <meta name="ui-change" content="yes|no"> が無い。'
                "画面に触ったかどうかを申告する"
            ]
        return []

    content = meta.group(1)
    if content not in ("yes", "no"):
        return [f'<meta name="ui-change"> の値がおかしい: "{content}"（yes か no にする）']

    has_media = bool(re.search(r'class="[^"]*\bshot\b[^"]*"', text)) or "<video" in text
    if content == "yes" and not has_media:
        return ['<meta name="ui-change" content="yes"> にしているのに、figure.shot か video が本文に無い']

    return []


SLIDE_TEXT_TAGS = ("p", "li", "dd", "td")
SLIDE_TEXT_EXCLUDE_TAGS = ("details", "pre", "code", "textarea")
SLIDE_TEXT_MAX_LENGTH = 60
# 「。」の個数ではなく、「。」の後ろに（空白を除いて）文字が続くかで数える。
# 末尾の 1 個だけの「。」は 1 文として通す。「」や（）の中の「。」も区別せず
# 同じ扱いにする（取りこぼし側として tests/test_check_reports_length.py に明記）。
SLIDE_TEXT_SENTENCE_BREAK = re.compile("。.")
# <br> は見た目の改行なので、行の区切りとして扱う。字数も文の数も行ごとに見る
SLIDE_TEXT_LINE_BREAK = "\x00"


class SlideProseParser(HTMLParser):
    """.slide 内の p・li・dd・td のテキストを、枚（section.slide）ごとに集める。

    details・pre・code・textarea の中は除外する（根拠は畳んでよい・コードは
    対象外・div.qa の補足欄は任意の長さの自由記述のため）。

    入れ子（li の中に li がある等）は、内側のタグが開いている間の文字を
    内側だけのものとして数える。外側の直接のテキストには内側の文字を
    合算しない。そのため「外側と内側それぞれは 60 字以内だが、合わせると
    超える」書き方はここでは拾わない（意図した割り切り。
    tests/test_check_reports_length.py に明記する）。

    2 文以上の判定は「。」の個数ではなく、「。」の後ろに（空白を除いて）
    文字が続くかで数える。「」や（）の中の「。」も区別せず同じ扱いにする
    （括弧の中で完結していても、外の「。」と見分けない割り切り）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.warnings: list[tuple[int, int, str, str]] = []
        self._slide_no = 0
        self._slide_title = ""
        self._exclude_depth = 0
        self._frames: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        classes = (attrs_d.get("class") or "").split()

        if tag == "section" and "slide" in classes:
            self._slide_no += 1
            self._slide_title = attrs_d.get("data-title") or ""

        if tag in SLIDE_TEXT_EXCLUDE_TAGS:
            self._exclude_depth += 1
            return

        if self._exclude_depth:
            return

        if tag == "br" and self._frames:
            self._frames[-1]["text"].append(SLIDE_TEXT_LINE_BREAK)
            return

        if tag in SLIDE_TEXT_TAGS:
            self._frames.append({"tag": tag, "line": self.getpos()[0], "text": []})

    def handle_data(self, data: str) -> None:
        if self._exclude_depth or not self._frames:
            return
        self._frames[-1]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in SLIDE_TEXT_EXCLUDE_TAGS:
            if self._exclude_depth:
                self._exclude_depth -= 1
            return

        if self._exclude_depth:
            return

        if tag in SLIDE_TEXT_TAGS and self._frames and self._frames[-1]["tag"] == tag:
            frame = self._frames.pop()
            body = re.sub(r"\s+", "", "".join(frame["text"])).strip()
            if not body:
                return

            lines = [ln for ln in body.split(SLIDE_TEXT_LINE_BREAK) if ln]
            if not lines:
                return
            longest = max(len(ln) for ln in lines)
            reasons = []
            if longest > SLIDE_TEXT_MAX_LENGTH:
                reasons.append(f"{longest} 字（{SLIDE_TEXT_MAX_LENGTH} 字超）")
            if any(SLIDE_TEXT_SENTENCE_BREAK.search(ln) for ln in lines):
                reasons.append("「。」の後ろに文字が続く（1 要素に 2 文以上）")
            if reasons:
                head = lines[0][:20]
                self.warnings.append(
                    (
                        frame["line"],
                        self._slide_no,
                        self._slide_title,
                        f"<{tag}> 「{head}」… {' / '.join(reasons)}",
                    )
                )


def check_slide_prose_length(text: str) -> list[str]:
    """スライド様式の p・li・dd・td が長すぎないか見る（既定は警告のみ）。

    「1 項目 1 行・1 文ごとに改行する」という決めを機械で気づけるようにする。
    過去のレポート（既に出したもの・雛形）を一斉に落としたくないので、
    ここで返すのは警告であって問題（終了コードに響くもの）ではない。
    呼び出し側（check_file・main）で別扱いにする。
    """
    if "/assets/slide.css" not in text:
        return []

    parser = SlideProseParser()
    parser.feed(text)
    return [
        f"{line} 行目: 枚 {slide_no}（{title or '(無題)'}）: {message}"
        for line, slide_no, title, message in parser.warnings
    ]


CHECKS = (
    check_change_cards,
    check_qa_ids,
    check_submit_button,
    check_media_paths,
    check_slide_default,
    check_scripts,
    check_qa_answers_dropped,
    check_dataroom_links,
    check_ui_change_meta,
)

# text だけでなく path も要る検査。check_file 側で個別に呼び分ける。
_NEEDS_PATH = (check_qa_answers_dropped, check_ui_change_meta)


# 長文の検査を当て始めた日。これより前の日付のレポートは対象外
PROSE_CHECK_SINCE = "2026-09-28"
_REPORT_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})_")


def prose_check_applies(path: Path) -> bool:
    """長文の検査を当てるか。雛形は常に、レポートはファイル名の日付で決める。

    日付で始まらないファイル名は、いつのものか分からないので当てない。
    """
    if path.parent.name == "templates":
        return True
    m = _REPORT_DATE.match(path.name)
    return bool(m) and m.group(1) >= PROSE_CHECK_SINCE


def check_file(path: Path, *, style: bool = True) -> tuple[list[str], list[str]]:
    """1 件を検査する。style=False のときは様式（スライドか否か）の検査を飛ばす。

    style は「これから出す（まだ git に入っていない）レポートか」も表す。
    check_ui_change_meta の meta 必須判定も、これに合わせて新規のときだけ効かせる。

    戻り値は (problems, warnings)。problems は終了コードに響く既存の検査、
    warnings は check_slide_prose_length など、既定では警告どまりの検査。
    """
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    for check in CHECKS:
        if check is check_slide_default and not style:
            continue
        if check is check_ui_change_meta:
            problems.extend(check(text, path, require_meta=style))
        elif check in _NEEDS_PATH:
            problems.extend(check(text, path))
        else:
            problems.extend(check(text))

    warnings = check_slide_prose_length(text) if prose_check_applies(path) else []
    return problems, warnings


def already_published(path: Path) -> bool:
    """git に入っている＝既に出したレポートか見る。

    過去に出した縦スクロールのレポートは作り直さない決まりなので、様式の検査から外す。
    そうしないと、完了へ移す（mv）たびに古いレポートで引っかかって作業が止まる。
    新しく書いたレポートはまだ git に入っていないので、様式の検査が効く。
    """
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--error-unmatch", str(path)],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def main(argv: list[str]) -> int:
    # 様式の検査は、これから出すレポート（まだ git に入っていない）だけを見る。
    # 全件走査では蒸し返さない。
    if argv:
        targets = [Path(a).resolve() for a in argv]
        style = True
    else:
        targets = sorted(REPORTS.rglob("*.html"))
        style = False

    total = 0
    total_warnings = 0
    for path in targets:
        problems, warnings = check_file(path, style=style and not already_published(path))
        if not problems and not warnings:
            continue
        total += len(problems)
        total_warnings += len(warnings)
        try:
            shown = path.relative_to(ROOT)
        except ValueError:
            shown = path
        print(f"\n{shown}")
        for problem in problems:
            print(f"  - {problem}")
        for warning in warnings:
            print(f"  ・（警告）{warning}")

    if total:
        print(f"\n{len(targets)} 件中、問題 {total} 件（ほかに警告 {total_warnings} 件）。")
        return 1

    if total_warnings:
        print(f"\n{len(targets)} 件、問題なし（警告 {total_warnings} 件）。")
    else:
        print(f"{len(targets)} 件、問題なし。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
