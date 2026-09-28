#!/usr/bin/env python3
"""レポート閲覧・回答サーバー（ローカル専用）。

AI が出した調査結果・作業計画・実施結果の HTML をブラウザで開き、
ページ内の確認事項にその場で回答するための小さなサーバー。
回答は reports/<プロジェクト>/<名前>.answers.json に溜まり、AI はそれを読む。

置き場所はプロジェクトの外（本ディレクトリ）。複数プロジェクトを横断して使う。

  reports/
    example/2026-08-05_main-commits.html
    example/2026-08-05_main-commits.answers.json   ← 回答（このサーバーが書く）
    other_project/...

一覧では、各レポートに未回答の設問がいくつ残っているか、前回開いたあとに
更新されたか（新着）を出す。開いた時刻は reports/.read.json に記録する。

URL:
  GET  /                                  レポート一覧（答え待ちを上に集めた 1 本のリスト）
  GET  /r/<プロジェクト>/<名前>.html        レポート本体（開いた時刻を記録する）
  GET  /r/<プロジェクト>/                   一覧のそのプロジェクトの位置へ戻す（302）
  GET  /t/<名前>.html                      テンプレート（複製して使う雛形）
  GET  /r/<プロジェクト>/media/<ファイル>    レポートに貼る画面・動画
  GET  /assets/<ファイル>                   共通の css / js
  GET  /favicon.ico                        タブのアイコン（一覧と同じ favicon-reports.svg）
  GET  /api/answers/<プロジェクト>/<名前>   回答の取得（再読み込み時の復元用・AI もここから読む）
  POST /api/answers/<プロジェクト>/<名前>   回答の保存（同じ設問は上書き）

ローカル専用のため認証は持たない。待ち受けは 127.0.0.1 のみ。
"""

from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import re
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

# 成果物ビューア（mdlib.py）。同じ考え方で、無くても他の機能は動かす
try:
    import mdlib
except Exception:  # noqa: BLE001
    mdlib = None

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
ASSETS_DIR = BASE_DIR / "assets"
TEMPLATES_DIR = BASE_DIR / "templates"
# 成果物ビューア。リポジトリ側のフォルダを読み取り専用でここへマウントする
# （docker-compose.yml を参照）。sources/<プロジェクト>/… が公開範囲になる。
SOURCES_DIR = BASE_DIR / "sources"
# レポートを開いた時刻の記録。「<プロジェクト>/<名前>": epoch 秒
STATE_PATH = REPORTS_DIR / ".read.json"
HOST = os.environ.get("REPORT_HUB_HOST", "0.0.0.0")
PORT = int(os.environ.get("REPORT_HUB_PORT", "5180"))

# プロジェクト名・レポート名に許すのはこの範囲だけ（パス抜けの防止）
SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-]+$")
# 配信してよい共通ファイルの拡張子
ASSET_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
}

# レポート本体・テンプレートは AI が書いた HTML をそのまま配信するが、タブのアイコンだけは
# 配信時にメモリ上で <head> の直後へ差し込む（元ファイルは書き換えない）。
_FAVICON_LINK = '<link rel="icon" type="image/svg+xml" href="/assets/favicon-reports.svg">'
_HEAD_RE = re.compile(rb"<head[^>]*>", re.IGNORECASE)


def _inject_favicon(markup: bytes) -> bytes:
    match = _HEAD_RE.search(markup)
    if not match:
        return markup
    pos = match.end()
    return markup[:pos] + _FAVICON_LINK.encode("utf-8") + markup[pos:]
# レポート内の確認事項。form.qa の data-qa-id を拾って設問数を数える
QA_ID = re.compile(r"""data-qa-id\s*=\s*["']([^"']+)["']""")
# 設問として数えない範囲（コメント・サンプルコード）
SAMPLE = re.compile(r"<!--.*?-->|<pre\b.*?</pre>|<code\b.*?</code>", re.S | re.I)
# レポート冒頭の「種別 ／ プロジェクト ／ 日付」。一覧で種別を出すのに使う
EYEBROW = re.compile(r"""class\s*=\s*["']eyebrow["']\s*>([^<]{0,120})""", re.I)
# 「種別 ／ プロジェクト ／ 日付」の区切り。レポートによって使う記号が揺れる
KIND_SEP = re.compile(r"[／/・·|]")
# 未回答のまま何日で目立たせるか（色を変える／行の左に帯を出す）
STALE_DAYS, ROTTEN_DAYS = 3, 7
# ファイル名の頭に付く日付（<YYYY-MM-DD>_<名前>）。一覧では題名から外して下の行に回す
NAME_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)$")
# 完了したレポートの置き場（<プロジェクト>/done/）
DONE_DIR = "done"
# レポートに貼る画面・動画の置き場（<プロジェクト>/media/）と、配信してよい拡張子。
# 実施結果は画面・動画を貼る決まりのため、レポートと同じ場所に置いて /r/<プロジェクト>/media/… で出す。
# 完了へ移してもここは動かさないので、レポート側は絶対パスで参照する（リンクが切れない）。
MEDIA_DIR = "media"
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
}

# 成果物ビューアで開ける拡張子。md は HTML に直して出し、それ以外はそのまま返す。
# 実行できるもの（.html / .js）は入れない（レポート側と混ざらないようにするため）。
DOC_TYPES = {
    ".md": "markdown",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8",
    ".json": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".drawio": "text/plain; charset=utf-8",
}

def _safe_media_path(project: str, filename: str) -> Path | None:
    """レポートに貼る画面・動画の実ファイルのパスを組む。危うい名前・扱わない拡張子は None。"""
    if not SAFE_NAME.match(project) or not SAFE_NAME.match(filename):
        return None
    if Path(filename).suffix.lower() not in MEDIA_TYPES:
        return None
    return REPORTS_DIR / project / MEDIA_DIR / filename


def _safe_report_path(project: str, name: str, suffix: str, done: bool = False) -> Path | None:
    """プロジェクト名とレポート名から実ファイルのパスを組む。危うい名前は None。

    完了したレポートは <プロジェクト>/done/ に移してある（進行中と分けて置く運用）。
    """
    if not SAFE_NAME.match(project) or not SAFE_NAME.match(name):
        return None
    folder = REPORTS_DIR / project / DONE_DIR if done else REPORTS_DIR / project
    path = (folder / f"{name}{suffix}").resolve()
    # reports/ の外に出ていないことを最後に確認する
    if REPORTS_DIR.resolve() not in path.parents:
        return None
    return path


def _safe_doc_path(project: str, rel: str) -> Path | None:
    """成果物ビューアで開くファイルの実パス。sources/ の外を指すものは None。

    ファイル名に日本語を使うため、レポート名のような文字種の制限はかけられない。
    代わりに、組み立てたパスが sources/<プロジェクト>/ の下に収まることだけを見る
    （`..` やシンボリックリンクで外へ出るものはここで落ちる）。
    """
    if not SAFE_NAME.match(project):
        return None
    root = (SOURCES_DIR / project).resolve()
    if not root.is_dir():
        return None
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        return None
    return path


def _doc_projects() -> list[str]:
    """成果物ビューアで開けるプロジェクト（sources/ 直下のフォルダ）。"""
    if not SOURCES_DIR.is_dir():
        return []
    return sorted(
        p.name for p in SOURCES_DIR.iterdir() if p.is_dir() and SAFE_NAME.match(p.name)
    )


def _now() -> str:
    """回答時刻。ローカル時刻で秒まで。"""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _question_ids(text: str) -> list[str]:
    """レポートに含まれる設問 ID。HTML の data-qa-id を数える。

    コメントと <pre>・<code> の中は数えない（書き方の説明としてサンプルを
    載せているだけで、設問ではないため）。
    JavaScript で設問を組み立てるレポートには追随できない。設問は静的に書く前提。
    """
    return list(dict.fromkeys(QA_ID.findall(SAMPLE.sub(" ", text))))


def _report_kind(text: str) -> str:
    """レポートの種別（作業計画・実施結果・調査結果…）。

    雛形が先頭に置く <p class="eyebrow">種別 ／ プロジェクト ／ 日付</p> の
    最初の区切りまでを種別として拾う。雛形から外れた書き方なら空にする。
    """
    found = EYEBROW.search(text)
    if not found:
        return ""
    return KIND_SEP.split(html.unescape(found.group(1)))[0].strip()[:14]


def _age(mtime: float, today: date) -> tuple[int, str]:
    """更新からの経過日数と、その表示。日をまたいだ回数で数える。"""
    days = (today - datetime.fromtimestamp(mtime).date()).days
    if days <= 0:
        return 0, "今日"
    if days == 1:
        return 1, "昨日"
    if days < 7:
        return days, f"{days}日前"
    return days, datetime.fromtimestamp(mtime).strftime("%m/%d")


def _read_state() -> dict[str, float]:
    """レポートを開いた時刻の記録。壊れていれば空として扱う。"""
    if not STATE_PATH.is_file():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text("utf-8"))
    except (ValueError, OSError):
        return {}
    return {k: float(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _mark_read(project: str, name: str, done: bool = False) -> None:
    """レポートを開いた時刻を残す。新着の判定に使う。"""
    state = _read_state()
    key = f"{project}/{DONE_DIR}/{name}" if done else f"{project}/{name}"
    state[key] = datetime.now().timestamp()
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8")
    except OSError:
        pass  # 記録できなくても閲覧は妨げない


def _report_row(html_path: Path, project: str, done: bool, state: dict[str, float], today: date) -> dict:
    """レポート 1 件ぶんの見出し情報。"""
    name = html_path.stem
    try:
        text = html_path.read_text("utf-8")
    except OSError:
        text = ""
    questions = _question_ids(text)
    answered_ids = {
        a.get("qa_id")
        for a in Handler._read_answers(html_path.with_name(f"{name}.answers.json"))
        if isinstance(a, dict)
    }
    mtime = html_path.stat().st_mtime
    key = f"{project}/{DONE_DIR}/{name}" if done else f"{project}/{name}"
    answered = len([q for q in questions if q in answered_ids])
    age_days, age_label = _age(mtime, today)
    return {
        "name": name,
        "project": project,
        "kind": _report_kind(text),
        "done": done,
        "url": f"/r/{project}/{DONE_DIR}/{name}.html" if done else f"/r/{project}/{name}.html",
        "mtime": mtime,
        "updated": datetime.fromtimestamp(mtime).strftime("%m/%d %H:%M"),
        "age_days": age_days,
        "age": age_label,
        "questions": len(questions),
        "answered": answered,
        "open": len(questions) - answered,
        "unread": mtime > state.get(key, 0.0),
    }


def _list_reports() -> list[dict]:
    """プロジェクト別のレポート一覧。

    各レポートに「設問がいくつあり、いくつ答えたか」「前回開いたあとに更新されたか」を付ける。
    <プロジェクト>/done/ に移したものは完了として分けて数える。
    未回答が残っているプロジェクトを先に、その中では更新の新しい順に並べる
    （一覧の左に出す絞り込みの並び。レポートの並べ替えは _render_index が行う）。
    """
    if not REPORTS_DIR.is_dir():
        return []

    state = _read_state()
    today = date.today()
    projects = []
    for project_dir in sorted(p for p in REPORTS_DIR.iterdir() if p.is_dir()):
        project = project_dir.name
        rows = sorted(
            (_report_row(f, project, False, state, today) for f in project_dir.glob("*.html")),
            key=lambda r: r["mtime"],
            reverse=True,
        )
        done_rows = sorted(
            (_report_row(f, project, True, state, today) for f in (project_dir / DONE_DIR).glob("*.html")),
            key=lambda r: r["mtime"],
            reverse=True,
        )
        if not rows and not done_rows:
            continue
        projects.append(
            {
                "name": project,
                "rows": rows,
                "done_rows": done_rows,
                # 未回答・新着は進行中のぶんだけ数える（完了は片付いたもの）
                "open": sum(r["questions"] - r["answered"] for r in rows),
                "unread": sum(1 for r in rows if r["unread"]),
                "updated": max((r["mtime"] for r in rows + done_rows), default=0.0),
            }
        )

    projects.sort(key=lambda p: (p["open"] > 0, p["unread"] > 0, p["updated"]), reverse=True)
    return projects


def _signature() -> str:
    """一覧の中身を表す短い文字列。変わったら画面を作り直す合図にする。"""
    parts = []
    for project in _list_reports():
        for row in project["rows"] + project["done_rows"]:
            parts.append(
                f'{project["name"]}/{row["name"]}:{row["mtime"]:.0f}'
                f':{row["answered"]}/{row["questions"]}:{int(row["unread"])}:{int(row["done"])}'
            )
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _list_templates() -> list[str]:
    """テンプレートの名前。複製して reports/ に置いて使う雛形。"""
    if not TEMPLATES_DIR.is_dir():
        return []
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.html"))


class Handler(BaseHTTPRequestHandler):
    server_version = "ReportHub/1.0"

    # ------------------------------------------------------------------ 送信部
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, message: str) -> None:
        self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send_html(self, status: int, markup: str) -> None:
        self._send(status, markup.encode("utf-8"), "text/html; charset=utf-8")

    # -------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802（BaseHTTPRequestHandler の規約）
        path = unquote(self.path.split("?", 1)[0])

        if path in ("/", "/index.html"):
            self._send_html(200, self._render_index())
            return

        # /favicon.ico … ブラウザが自動で取りに行く分。一覧のアイコンをそのまま返す
        if path == "/favicon.ico":
            self._send(200, (ASSETS_DIR / "favicon-reports.svg").read_bytes(), "image/svg+xml")
            return

        parts = [p for p in path.split("/") if p]

        # /api/signature … 一覧の中身が変わったかを見るための短い文字列
        if len(parts) == 2 and parts[0] == "api" and parts[1] == "signature":
            self._send_json(200, {"signature": _signature()})
            return

        # /r/<project>/ … レポートの「← レポート一覧」。一覧のそのプロジェクトの位置へ戻す
        # （完了ぶんは /r/<project>/done/<名前>.html なので、そこからの "./" も同じ扱い）
        if parts[:1] == ["r"] and path.endswith("/") and len(parts) <= 3:
            project = parts[1] if len(parts) >= 2 else ""
            anchor = f"#{project}" if project else ""
            self.send_response(302)
            self.send_header("Location", f"/{anchor}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # /assets/<file>（css・js だけ）
        if len(parts) == 2 and parts[0] == "assets":
            asset = ASSETS_DIR / parts[1]
            content_type = ASSET_TYPES.get(asset.suffix)
            if not SAFE_NAME.match(parts[1]) or content_type is None or not asset.is_file():
                self._send_text(404, "ありません")
                return
            self._send(200, asset.read_bytes(), content_type)
            return

        # /r/<project>/media/<file>（レポートに貼る画面・動画）
        if parts[:1] == ["r"] and len(parts) == 4 and parts[2] == MEDIA_DIR:
            target = _safe_media_path(parts[1], parts[3])
            if target is None or not target.is_file():
                self._send_text(404, "その画面・動画はありません")
                return
            self._send(200, target.read_bytes(), MEDIA_TYPES[target.suffix.lower()])
            return

        # /r/<project>/<name>.html ・ /r/<project>/done/<name>.html（完了ぶん）
        if parts[:1] == ["r"] and parts[-1].endswith(".html") and len(parts) in (3, 4):
            done = len(parts) == 4
            if done and parts[2] != DONE_DIR:
                self._send_text(404, "レポートがありません")
                return
            name = parts[-1][: -len(".html")]
            target = _safe_report_path(parts[1], name, ".html", done)
            if target is None or not target.is_file():
                self._send_text(404, "レポートがありません")
                return
            _mark_read(parts[1], name, done)  # 一覧の新着表示に使う
            self._send(200, _inject_favicon(target.read_bytes()), "text/html; charset=utf-8")
            return

        # /d/<project>/<パス…>（成果物ビューア。md は HTML に直して出す）
        if parts[:1] == ["d"]:
            project = parts[1] if len(parts) >= 2 else ""
            self._handle_doc(project, "/".join(parts[2:]), path.endswith("/"))
            return

        # /t/<name>.html（テンプレートの下見。複製はファイルを直接コピーする）
        if len(parts) == 2 and parts[0] == "t" and parts[1].endswith(".html"):
            name = parts[1][: -len(".html")]
            target = TEMPLATES_DIR / f"{name}.html"
            if not SAFE_NAME.match(name) or not target.is_file():
                self._send_text(404, "テンプレートがありません")
                return
            self._send(200, _inject_favicon(target.read_bytes()), "text/html; charset=utf-8")
            return

        # /api/answers/<project>/<name> ・ /api/answers/<project>/done/<name>
        answers_path = self._answers_path(parts)
        if answers_path is not None:
            self._send_json(200, self._read_answers(answers_path))
            return
        if parts[:2] == ["api", "answers"]:
            self._send_json(400, {"detail": "名前が不正です"})
            return

        self._send_text(404, "見つかりません")

    # ------------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        path = unquote(self.path.split("?", 1)[0])
        parts = [p for p in path.split("/") if p]

        if parts[:2] != ["api", "answers"]:
            self._send_json(404, {"detail": "見つかりません"})
            return

        answers_path = self._answers_path(parts)
        if answers_path is None:
            self._send_json(400, {"detail": "名前が不正です"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send_json(400, {"detail": "本文が JSON ではありません"})
            return

        # 画面末尾の「回答する」でまとめて届く（{"answers":[...]}）。
        # 1 件ずつの形（{"qa_id":...}）も受ける。
        incoming = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(incoming, list):
            incoming = [payload]

        now = _now()
        saved = []
        for item in incoming:
            if not isinstance(item, dict):
                continue
            qa_id = str(item.get("qa_id") or "").strip()
            if not qa_id:
                continue
            saved.append(
                {
                    "qa_id": qa_id,
                    "question": str(item.get("question") or ""),
                    "choice": str(item.get("choice") or ""),
                    "note": str(item.get("note") or ""),
                    "answered_at": now,
                }
            )
        if not saved:
            self._send_json(400, {"detail": "qa_id が必要です"})
            return

        # 読む→混ぜる→書くの間、ファイルそのものを排他ロックする（初回は空ファイルを
        # 先に作ってからロックを取る）。同時に届いた回答が互いを踏み潰さないようにするため。
        answers_path.parent.mkdir(parents=True, exist_ok=True)
        if not answers_path.exists():
            answers_path.touch()
        with answers_path.open("r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                raw = f.read()
                try:
                    data = json.loads(raw) if raw.strip() else []
                except ValueError:
                    data = []
                answers = data if isinstance(data, list) else []
                # 同じ設問への回答は最新の 1 件だけ残す（言い直しができるように）
                replaced = {e["qa_id"] for e in saved}
                answers = [a for a in answers if a.get("qa_id") not in replaced]
                answers.extend(saved)
                f.seek(0)
                f.truncate()
                f.write(json.dumps(answers, ensure_ascii=False, indent=2) + "\n")
                # ロックを外す前に書き切る。バッファに残したまま外すと、
                # 実際の書き込みがロックの外へこぼれて互いを踏み潰す
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        self._send_json(200, {"saved": saved, "count": len(answers)})

    # -------------------------------------------------------- 成果物ビューア
    def _handle_doc(self, project: str, rel: str, is_dir_url: bool) -> None:
        """/d/… の 1 本。ファイルなら中身を、フォルダなら中の一覧を出す。"""
        if project == "" or (project and not SAFE_NAME.match(project)):
            self._send_html(200, self._doc_roots())
            return
        target = _safe_doc_path(project, rel)
        if target is None or not target.exists():
            self._send_text(404, "その成果物はありません")
            return
        if target.is_dir():
            self._send_html(200, self._doc_listing(project, rel, target))
            return

        kind = DOC_TYPES.get(target.suffix.lower())
        if kind is None:
            self._send_text(404, "この種類のファイルは開けません")
            return
        if kind == "markdown":
            if mdlib is None:
                self._send_text(503, "Markdown の表示は準備中です")
                return
            body = mdlib.render(
                target.read_text(encoding="utf-8", errors="replace"),
                base=rel,
                prefix=f"/d/{project}",
            )
            self._send_html(200, self._doc_page(project, rel, body, self._is_bare()))
            return
        self._send(200, target.read_bytes(), kind)

    def _is_bare(self) -> bool:
        """?bare=1 … 左のサイドバーを外した表示（レポートへ埋め込むときに使う）。"""
        return "bare=1" in self.path.split("?", 1)[-1] if "?" in self.path else False

    def _doc_roots(self) -> str:
        """公開しているプロジェクトの一覧（/d/ を直に開いたとき）。"""
        projects = _doc_projects()
        if not projects:
            rows = "<p class='empty'>公開している成果物はありません。</p>"
        else:
            rows = "<ul class='doclist'>" + "".join(
                f"<li><a href='/d/{html.escape(p)}/'>{html.escape(p)}</a></li>" for p in projects
            ) + "</ul>"
        return self._doc_shell("成果物", "<h1>成果物</h1>" + rows)

    def _doc_listing(self, project: str, rel: str, target: Path) -> str:
        """フォルダの中身。読めるものだけを並べる。"""
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        items = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            child = f"{rel}/{entry.name}".strip("/")
            if entry.is_dir():
                items.append(
                    f"<li class='dir'><a href='/d/{html.escape(project)}/{quote(child)}/'>"
                    f"{html.escape(entry.name)}/</a></li>"
                )
            elif entry.suffix.lower() in DOC_TYPES:
                items.append(
                    f"<li><a href='/d/{html.escape(project)}/{quote(child)}'>"
                    f"{html.escape(entry.name)}</a></li>"
                )
        listing = "<ul class='doclist'>" + "".join(items) + "</ul>" if items else "<p class='empty'>中身がありません。</p>"
        here = f"{project}/{rel}".strip("/")
        return self._doc_shell(
            here,
            f"<p class='crumb'>{self._doc_crumb(project, rel)}</p><h1>{html.escape(here)}</h1>" + listing,
        )

    def _doc_page(self, project: str, rel: str, body: str, bare: bool = False) -> str:
        """Markdown 1 枚。上に来た道を出し、レポートへ戻れるようにする。

        bare のときは道すじとサイドバーを外す（レポートの中へ埋め込むため）。
        """
        crumb = "" if bare else f"<p class='crumb'>{self._doc_crumb(project, rel)}</p>"
        return self._doc_shell(
            PurePosixPath(rel).name or project,
            crumb + f"<article class='doc'>{body}</article>",
            bare,
        )

    @staticmethod
    def _doc_crumb(project: str, rel: str) -> str:
        """`成果物 / <プロジェクト> / <フォルダ> / <ファイル>` の道。"""
        crumbs = [f"<a href='/d/'>成果物</a>", f"<a href='/d/{html.escape(project)}/'>{html.escape(project)}</a>"]
        walked: list[str] = []
        parts = [p for p in rel.split("/") if p]
        for part in parts[:-1]:
            walked.append(part)
            crumbs.append(
                f"<a href='/d/{html.escape(project)}/{quote('/'.join(walked))}/'>{html.escape(part)}</a>"
            )
        if parts:
            crumbs.append(f"<span>{html.escape(parts[-1])}</span>")
        return " / ".join(crumbs)

    @classmethod
    def _doc_shell(cls, title: str, body: str, bare: bool = False) -> str:
        return (
            "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(title)}</title>"
            "<link rel='stylesheet' href='/assets/index.css'>"
            "<link rel='stylesheet' href='/assets/nav.css'>"
            "<link rel='stylesheet' href='/assets/doc.css'>"
            + ("</head><body class='is-bare'>" if bare else "</head><body class='has-nav'>")
            + ("" if bare else cls._sidenav("docs"))
            + "<div class='wrap'>"
            + body
            + "</div></body></html>"
        )

    # ------------------------------------------------------------------ 補助
    @staticmethod
    def _answers_path(parts: list[str]) -> Path | None:
        """/api/answers/<project>/<name> と …/<project>/done/<name> を実ファイルへ。"""
        if parts[:2] != ["api", "answers"] or len(parts) not in (4, 5):
            return None
        if len(parts) == 5 and parts[3] != DONE_DIR:
            return None
        project, name, done = parts[2], parts[-1], len(parts) == 5
        return _safe_report_path(project, name, ".answers.json", done)

    @staticmethod
    def _read_answers(path: Path) -> list[dict]:
        """回答 JSON を読む。共有ロックで、書き込み中の中途半端な内容を読まないようにする。

        壊れている・存在しないときは例外を投げず空配列を返す（既存の挙動のまま）。
        """
        if not path.is_file():
            return []
        try:
            with path.open("r", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                try:
                    data = json.loads(f.read())
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except (ValueError, OSError):
            return []
        return data if isinstance(data, list) else []

    @classmethod
    def _row_item(cls, row: dict) -> str:
        """一覧に並ぶレポート 1 行。

        絞り込みは画面側（assets/index.js）が data-* を見て行う。
        未回答のまま日が経っているものは is-stale／is-rotten を付けて目立たせる。
        """
        classes = ["row"]
        if row["open"]:
            classes.append("is-open")
            if row["age_days"] >= STALE_DAYS:
                classes.append("is-stale")
            if row["age_days"] >= ROTTEN_DAYS:
                classes.append("is-rotten")
        if row["unread"]:
            classes.append("is-new")
        search_text = html.escape(" ".join((row["name"], row["project"], row["kind"])).lower())
        # ファイル名の頭の日付は題名から外し、下の行へ回す（題名を読みやすくする）
        stamp, title = NAME_DATE.match(row["name"]).groups() if NAME_DATE.match(row["name"]) else ("", row["name"])
        sub = "".join(
            f'<span class="kind">{html.escape(part)}</span>'
            for part in (row["project"], row["kind"], stamp)
            if part
        )
        return (
            f'<li class="{" ".join(classes)}" data-project="{html.escape(row["project"])}"'
            f' data-text="{search_text}"{" data-done=\'1\'" if row["done"] else ""}>'
            f'<a class="row-link" href="{html.escape(row["url"])}" title="{row["updated"]} 更新">'
            '<span class="dot" aria-hidden="true"></span>'
            '<span class="row-main">'
            f'<span class="row-title">{html.escape(title)}</span>'
            f'<span class="row-sub">{sub}</span>'
            "</span>"
            + cls._status_text(row)
            + f'<span class="row-age">{row["age"]}</span></a></li>'
        )

    @staticmethod
    def _status_text(row: dict) -> str:
        """レポート 1 件の回答状況。設問がなければその旨を出す。"""
        if not row["questions"]:
            return '<span class="row-state">設問なし</span>'
        if row["open"]:
            return f'<span class="row-state is-open">未回答 {row["open"]} / {row["questions"]}</span>'
        return f'<span class="row-state is-done">回答済 {row["questions"]}</span>'

    @staticmethod
    def _sidenav(active: str) -> str:
        """一覧・成果物ビューア共通の左サイドバー。"""
        try:
            open_total = sum(
                row["open"] for project in _list_reports() for row in project["rows"]
            )
        except Exception:  # noqa: BLE001（サイドバーの都合で本編を落とさない）
            open_total = 0
        report_badge = f' <span class="n">{open_total}</span>' if open_total else ""
        items = [
            f'<li><a class="nav-item{" is-here" if active == "reports" else ""}" href="/">'
            f"レポート{report_badge}</a></li>"
        ]
        if _doc_projects():
            items.append(
                f'<li><a class="nav-item{" is-here" if active == "docs" else ""}" href="/d/">'
                "成果物</a></li>"
            )
        return (
            '<nav class="sidenav"><p class="brand"><a href="/">report-hub</a></p>'
            f'<ul class="nav-items">{"".join(items)}</ul></nav>'
        )

    @classmethod
    def _page(cls, title: str, body: str) -> str:
        signature = _signature()
        return (
            "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(title)}</title>"
            "<link rel='icon' type='image/svg+xml' href='/assets/favicon-reports.svg'>"
            "<link rel='stylesheet' href='/assets/index.css'>"
            "<link rel='stylesheet' href='/assets/nav.css'>"
            f"</head><body class='has-nav' data-signature='{signature}'>"
            + cls._sidenav("reports")
            + "<div class='wrap'>"
            + body
            + "</div><script src='/assets/index.js'></script></body></html>"
        )

    def _render_index(self) -> str:
        """トップ。プロジェクトをまたいだ 1 本のリストに、答え待ちを上から並べる。

        並びは「未回答があるもの → 新着 → 更新の新しい順」。プロジェクトは
        見出しにせず左の絞り込みに置く（縦の階層を作らず、次に見るものを上に集める）。
        """
        projects = _list_reports()
        rows = sorted(
            (row for project in projects for row in project["rows"]),
            key=lambda r: (r["open"] > 0, r["unread"], r["mtime"]),
            reverse=True,
        )
        done_rows = sorted(
            (row for project in projects for row in project["done_rows"]),
            key=lambda r: r["mtime"],
            reverse=True,
        )
        open_total = sum(row["open"] for row in rows)
        unread_total = sum(1 for row in rows if row["unread"])

        # 左：答え待ちの総数と、プロジェクトの絞り込み
        filters = [
            f'<button class="f" data-project="" type="button">すべて'
            f'<span class="n">{len(rows)}</span></button>'
        ]
        for project in projects:
            open_mark = (
                f'<span class="n is-open">未回答 {project["open"]}</span>'
                if project["open"]
                else f'<span class="n">{len(project["rows"])}</span>'
            )
            name = html.escape(project["name"])
            filters.append(
                f'<button class="f" data-project="{name}" type="button">{name}{open_mark}</button>'
            )

        templates = _list_templates()
        templates_block = (
            '<p class="side-foot"><span class="head">テンプレート</span>'
            + "".join(f'<a href="/t/{t}.html">{t}</a>' for t in templates)
            + "</p>"
            if templates
            else ""
        )

        side = (
            '<aside class="side">'
            f'<div class="side-total{"" if open_total else " is-clear"}">'
            f'<span class="side-num">{open_total}</span>'
            '<span class="side-label">未回答の設問</span>'
            f'<span class="side-when">{datetime.now().strftime("%Y/%m/%d %H:%M")} 現在</span>'
            "</div>"
            '<nav class="filters"><span class="filters-head">プロジェクト</span>'
            + "".join(filters)
            + "</nav>"
            + templates_block
            + "</aside>"
        )

        # 右：検索・タブと、1 本のレポートリスト
        list_block = (
            f'<ul class="rows">{"".join(self._row_item(row) for row in rows)}</ul>'
            '<p class="empty" hidden>絞り込みに当てはまるレポートがない。</p>'
            if rows
            else '<p class="empty">まだレポートがありません。'
            "reports/&lt;プロジェクト&gt;/ に HTML を置いてください。</p>"
        )
        done_block = (
            f'<details class="done"><summary>完了 {len(done_rows)} 件</summary>'
            f'<ul class="rows">{"".join(self._row_item(row) for row in done_rows)}</ul></details>'
            if done_rows
            else ""
        )
        stream = (
            '<main class="stream">'
            '<div class="toolbar">'
            '<input class="q" type="search" placeholder="レポート名・プロジェクトで絞る（/）" '
            'aria-label="レポートを絞り込む">'
            '<div class="tabs" role="group" aria-label="表示の切り替え">'
            f'<button class="tab" data-tab="open" type="button">'
            f'未回答 {sum(1 for r in rows if r["open"])} 件</button>'
            f'<button class="tab" data-tab="new" type="button">新着 {unread_total} 件</button>'
            f'<button class="tab is-on" data-tab="all" type="button">すべて</button>'
            "</div></div>"
            + list_block
            + done_block
            + "</main>"
        )

        return self._page(
            "レポート一覧",
            "<header><h1>レポート</h1>"
            "<p class='lede'>答え待ちのものが上に来る。開いて確認事項に回答すると、"
            "同じ場所の <code>.answers.json</code> に保存される。</p></header>"
            f'<div class="inbox">{side}{stream}</div>',
        )

    def log_message(self, fmt: str, *args) -> None:
        """アクセスログは 1 行に収める（既定は日時が二重に出る）。"""
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"レポートサーバー起動: http://localhost:{PORT}/  （reports={REPORTS_DIR}）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
