#!/usr/bin/env python3
"""Markdown を HTML に変換する小さな変換器（標準ライブラリのみ）。

report-hub の「成果物ビューア」（/d/…）で、リポジトリ内の設計ドキュメントを
そのままブラウザで読めるようにするために置いている。汎用の Markdown 実装ではなく、
本プロジェクトの設計ドキュメントで実際に使っている記法だけを対象にする。

対応する記法：
  見出し（# 〜 #####）・表・箇条書き（- / 1.）・チェックボックス（- [ ] / - [x]）
  引用（>）・コードブロック（``` / インデント無し）・水平線（---）
  強調（**）・コード（`）・リンク（[text](url)）・自動リンク（<http://…>）

対応しないもの（設計ドキュメントで使っていないため）：
  画像・脚注・定義リスト・入れ子の引用・HTML の生書き
"""

from __future__ import annotations

import html
import re
from pathlib import PurePosixPath

# 見出し。`### ■ タイトル` のようにマークが付くが、マークも本文として出す
HEADING = re.compile(r"^(#{1,5})\s+(.*)$")
# 表の区切り行（`| :--- | ---: |`）。これがあるかどうかで表とみなす
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# 箇条書き
BULLET = re.compile(r"^(\s*)[-*]\s+(.*)$")
ORDERED = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
# チェックボックス（箇条書きの中身の先頭）
CHECKBOX = re.compile(r"^\[( |x|X)\]\s*(.*)$")
# コードブロックの囲み
FENCE = re.compile(r"^\s*```+\s*([A-Za-z0-9_+-]*)\s*$")
# 水平線
RULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
# 引用
QUOTE = re.compile(r"^>\s?(.*)$")

# インライン
INLINE_CODE = re.compile(r"`([^`]+)`")
BOLD = re.compile(r"\*\*(.+?)\*\*")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
AUTOLINK = re.compile(r"&lt;(https?://[^&\s]+)&gt;")
BARE_URL = re.compile(r"(?<![\"'=(])\b(https?://[^\s<>\)）」』]+)")


def _link_href(href: str, base: str, prefix: str) -> str:
    """相対リンクを、ビューアで開ける URL に置き換える。

    同じリポジトリ内の別ファイルを指しているリンク（`docs/xxx.md` など）は、
    ビューアの URL（`<prefix>/<解決したパス>`）に直す。外部 URL・ページ内
    リンク（#…）はそのまま通す。
    """
    if not href or href.startswith(("http://", "https://", "mailto:", "#", "/")):
        return href
    target, _, anchor = href.partition("#")
    if not target:
        return href
    # base（いま見ているファイルのパス）からの相対で解決する
    resolved = PurePosixPath(PurePosixPath(base).parent / target)
    parts: list[str] = []
    for part in resolved.parts:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    url = f"{prefix}/{'/'.join(parts)}"
    return f"{url}#{anchor}" if anchor else url


def _inline(text: str, base: str, prefix: str) -> str:
    """1 行ぶんのインライン記法を HTML にする。

    コードの中身は他の記法として解釈しないため、先にコードを取り分けてから
    残りを処理する。
    """
    out: list[str] = []
    pos = 0
    for m in INLINE_CODE.finditer(text):
        out.append(_inline_plain(text[pos : m.start()], base, prefix))
        out.append(f"<code>{html.escape(m.group(1))}</code>")
        pos = m.end()
    out.append(_inline_plain(text[pos:], base, prefix))
    return "".join(out)


def _inline_plain(text: str, base: str, prefix: str) -> str:
    """コード以外の部分。エスケープしてから記法を当てる。"""
    if not text:
        return ""
    out = html.escape(text)
    out = AUTOLINK.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', out)

    def link(m: re.Match[str]) -> str:
        href = html.unescape(m.group(2))
        label = m.group(1) or href
        return f'<a href="{html.escape(_link_href(href, base, prefix), quote=True)}">{label}</a>'

    out = LINK.sub(link, out)
    out = BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", out)
    # まだリンクになっていない裸の URL（<a> の中は既に置換済みなので触らない）
    if "<a " not in out:
        out = BARE_URL.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', out)
    return out


def _cells(row: str) -> list[str]:
    """表の 1 行をセルに割る。両端の | は飾りなので落とす。"""
    line = row.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _aligns(sep: str) -> list[str]:
    """区切り行から各列の寄せを読む。"""
    result = []
    for cell in _cells(sep):
        left, right = cell.startswith(":"), cell.endswith(":")
        if left and right:
            result.append("center")
        elif right:
            result.append("right")
        else:
            result.append("left")
    return result


def render(text: str, base: str = "", prefix: str = "") -> str:
    """Markdown 本文を HTML の断片にする（<body> の中身だけを返す）。

    base   … いま変換しているファイルのパス（相対リンクの解決に使う）
    prefix … ビューアの URL の頭（例 "/d/intern"）
    """
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # コードブロック
        fence = FENCE.match(line)
        if fence:
            lang = fence.group(1)
            i += 1
            body: list[str] = []
            while i < n and not FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # 閉じの ``` を飛ばす
            cls = f' class="lang-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(body))}</code></pre>")
            continue

        # 空行
        if not line.strip():
            i += 1
            continue

        # 水平線（表の区切り行と紛れないよう、表の判定より先には置かない）
        if RULE.match(line) and not (i + 1 < n and "|" in line):
            out.append("<hr>")
            i += 1
            continue

        # 見出し
        head = HEADING.match(line)
        if head:
            level = len(head.group(1))
            out.append(f"<h{level}>{_inline(head.group(2).strip(), base, prefix)}</h{level}>")
            i += 1
            continue

        # 表（次の行が区切り行なら表とみなす）
        if "|" in line and i + 1 < n and TABLE_SEP.match(lines[i + 1]):
            heads = _cells(line)
            aligns = _aligns(lines[i + 1])
            i += 2
            rows: list[list[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            out.append('<div class="tablewrap"><table>')
            out.append("<thead><tr>")
            for idx, cell in enumerate(heads):
                align = aligns[idx] if idx < len(aligns) else "left"
                out.append(f'<th style="text-align:{align}">{_inline(cell, base, prefix)}</th>')
            out.append("</tr></thead><tbody>")
            for row in rows:
                out.append("<tr>")
                for idx in range(len(heads)):
                    cell = row[idx] if idx < len(row) else ""
                    align = aligns[idx] if idx < len(aligns) else "left"
                    out.append(f'<td style="text-align:{align}">{_inline(cell, base, prefix)}</td>')
                out.append("</tr>")
            out.append("</tbody></table></div>")
            continue

        # 引用（連続する > をまとめて 1 つの引用にする）
        if QUOTE.match(line):
            body = []
            while i < n and QUOTE.match(lines[i]):
                body.append(QUOTE.match(lines[i]).group(1))
                i += 1
            out.append(f'<blockquote>{render(chr(10).join(body), base, prefix)}</blockquote>')
            continue

        # 箇条書き（- / 1.）。入れ子は 1 段だけ見る
        if BULLET.match(line) or ORDERED.match(line):
            markup, i = _list(lines, i, base, prefix)
            out.append(markup)
            continue

        # ふつうの段落（空行までを 1 つにまとめる）
        para: list[str] = []
        while i < n and lines[i].strip() and not _starts_block(lines[i]):
            para.append(lines[i].strip())
            i += 1
        if para:
            out.append(f"<p>{_inline(' '.join(para), base, prefix)}</p>")
        else:  # 念のため（判定漏れで無限ループにしない）
            i += 1

    return "\n".join(out)


def _starts_block(line: str) -> bool:
    """段落の途中で別の書き方が始まっているか。"""
    return bool(
        HEADING.match(line)
        or BULLET.match(line)
        or ORDERED.match(line)
        or FENCE.match(line)
        or QUOTE.match(line)
        or RULE.match(line)
        or ("|" in line and line.strip().startswith("|"))
    )


def _list(lines: list[str], start: int, base: str, prefix: str) -> tuple[str, int]:
    """箇条書きを 1 つ分だけ HTML にする。戻り値は (HTML, 次に読む行)。"""
    ordered = bool(ORDERED.match(lines[start]))
    indent = len(ORDERED.match(lines[start]).group(1) if ordered else BULLET.match(lines[start]).group(1))
    items: list[str] = []
    i = start
    n = len(lines)

    while i < n:
        m_b, m_o = BULLET.match(lines[i]), ORDERED.match(lines[i])
        if not (m_b or m_o):
            break
        cur_indent = len(m_o.group(1) if m_o else m_b.group(1))
        if cur_indent < indent:
            break
        if cur_indent > indent:
            # 入れ子。中の箇条書きを直前の項目にぶら下げる
            inner, i = _list(lines, i, base, prefix)
            if items:
                items[-1] += inner
            continue
        body = m_o.group(3) if m_o else m_b.group(2)
        check = CHECKBOX.match(body)
        if check:
            mark = "checked" if check.group(1).lower() == "x" else ""
            items.append(
                f'<li class="task"><input type="checkbox" disabled {mark}>'
                f"{_inline(check.group(2), base, prefix)}"
            )
        else:
            items.append(f"<li>{_inline(body, base, prefix)}")
        i += 1

    tag = "ol" if ordered else "ul"
    return f"<{tag}>" + "".join(f"{item}</li>" for item in items) + f"</{tag}>", i
