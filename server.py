#!/usr/bin/env python3
"""レポート閲覧・回答サーバー（ローカル専用）。

AI が出した調査結果・作業計画・実施結果の HTML をブラウザで開き、
ページ内の確認事項にその場で回答するための小さなサーバー。
回答は reports/<プロジェクト>/<名前>.answers.json に溜まり、AI はそれを読む。

置き場所はプロジェクトの外（本ディレクトリ）。複数プロジェクトを横断して使う。

  reports/
    wbs_site/2026-08-05_main-commits.html
    wbs_site/2026-08-05_main-commits.answers.json   ← 回答（このサーバーが書く）
    ordering_ops/...

一覧では、各レポートに未回答の設問がいくつ残っているか、前回開いたあとに
更新されたか（新着）を出す。開いた時刻は reports/.read.json に記録する。

URL:
  GET  /                                  レポート一覧（答え待ちを上に集めた 1 本のリスト）
  GET  /r/<プロジェクト>/<名前>.html        レポート本体（開いた時刻を記録する）
  GET  /r/<プロジェクト>/                   一覧のそのプロジェクトの位置へ戻す（302）
  GET  /t/<名前>.html                      テンプレート（複製して使う雛形）
  GET  /assets/<ファイル>                   共通の css / js
  GET  /api/answers/<プロジェクト>/<名前>   回答の取得（再読み込み時の復元用・AI もここから読む）
  POST /api/answers/<プロジェクト>/<名前>   回答の保存（同じ設問は上書き）

ローカル専用のため認証は持たない。待ち受けは 127.0.0.1 のみ。
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlparse

# 技術フィード（feedlib.py）。並行して作られているため、無い／壊れていても
# 一覧・レポート機能は止めたくない。import できなければフィード関連だけ 503 にする。
try:
    import feedlib
except Exception:  # noqa: BLE001（何が起きても既存機能は落とさない）
    feedlib = None

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

# 技術フィードの group ごとの表示名・並び（フロント担当と合意済みの固定リスト）
FEED_GROUPS = [
    ("official", "Anthropic 公式"),
    ("jp", "国内"),
    ("community", "海外"),
    ("github", "リリース"),
]


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


def _parse_iso(text: str) -> datetime | None:
    """ISO 8601 の日時文字列を datetime に。壊れていれば None。"""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _feed_when(text: str) -> tuple[str, str]:
    """フィード記事の日時表示。相対表記（当日は分・時間、それ以降は日）と、
    title 属性用の絶対表記（YYYY-MM-DD HH:MM）を返す。壊れた日時は空にする。
    """
    dt = _parse_iso(text)
    if dt is None:
        return "", ""
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    secs = (now - dt).total_seconds()
    if secs < 60:
        rel = "たった今"
    elif secs < 3600:
        rel = f"{int(secs // 60)}分前"
    elif secs < 86400:
        rel = f"{int(secs // 3600)}時間前"
    elif secs < 86400 * 7:
        rel = f"{int(secs // 86400)}日前"
    else:
        rel = dt.strftime("%m/%d")
    return rel, dt.strftime("%Y/%m/%d %H:%M")


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

        # /feed … 技術フィードのページ本体
        if parts == ["feed"]:
            if feedlib is None:
                self._send_text(503, "技術フィードは準備中です")
                return
            self._send_html(200, self._render_feed())
            return

        # /feed/taste … 評価の傾向を見るページ
        if parts == ["feed", "taste"]:
            if feedlib is None:
                self._send_text(503, "技術フィードは準備中です")
                return
            try:
                overview = feedlib.taste_overview()
            except Exception as exc:  # noqa: BLE001（フィード側の不具合で全体を落とさない）
                self._send_text(503, f"傾向の取得に失敗しました: {exc}")
                return
            self._send_html(200, self._render_taste(overview))
            return

        # /api/feed/signature ・ /api/feed/status
        if parts[:2] == ["api", "feed"] and len(parts) == 3 and parts[2] in ("signature", "status"):
            if feedlib is None:
                self._send_json(503, {"detail": "技術フィードは準備中です"})
                return
            if parts[2] == "signature":
                self._send_json(200, {"signature": feedlib.signature()})
            else:
                self._send_json(200, feedlib.status())
            return

        # /api/feed/excluded … 不要にした語の一覧
        if parts == ["api", "feed", "excluded"]:
            if feedlib is None:
                self._send_json(503, {"detail": "技術フィードは準備中です"})
                return
            try:
                words = feedlib.excluded_words()
            except Exception as exc:  # noqa: BLE001（フィード側の不具合で全体を落とさない）
                self._send_json(503, {"detail": str(exc)})
                return
            self._send_json(200, {"words": words})
            return

        # /api/feed/item/<item_id> … 記事の中身（ダイアログ用）。既定では本文を含めない
        if parts[:3] == ["api", "feed", "item"] and len(parts) == 4:
            if feedlib is None:
                self._send_json(503, {"ok": False, "error": "技術フィードは準備中です"})
                return
            item_id = parts[3]
            if not SAFE_NAME.match(item_id):
                self._send_json(404, {"ok": False, "error": "記事がありません"})
                return
            try:
                detail = feedlib.item_detail(item_id)
            except Exception as exc:  # noqa: BLE001（フィード側の不具合で全体を落とさない）
                self._send_json(503, {"ok": False, "error": str(exc)})
                return
            if not detail:
                self._send_json(404, {"ok": False, "error": "記事がありません"})
                return
            detail = dict(detail)
            body = str(detail.get("body") or "")
            query = parse_qs(urlparse(self.path).query)
            include_body = query.get("body", ["0"])[0] == "1"
            if include_body:
                detail["body"] = body
            else:
                detail["body"] = ""
                detail["body_len"] = len(body)
            self._send_json(200, detail)
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

        if parts[:2] == ["api", "feed"]:
            self._handle_feed_post(parts)
            return

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

        answers = self._read_answers(answers_path)
        # 同じ設問への回答は最新の 1 件だけ残す（言い直しができるように）
        replaced = {e["qa_id"] for e in saved}
        answers = [a for a in answers if a.get("qa_id") not in replaced]
        answers.extend(saved)

        answers_path.parent.mkdir(parents=True, exist_ok=True)
        answers_path.write_text(json.dumps(answers, ensure_ascii=False, indent=2) + "\n", "utf-8")
        self._send_json(200, {"saved": saved, "count": len(answers)})

    # -------------------------------------------------------------- 技術フィード
    def _handle_feed_post(self, parts: list[str]) -> None:
        """/api/feed/refresh・read・pin。既存の /api/answers と同じ作法で読む。"""
        if feedlib is None:
            self._send_json(503, {"ok": False, "error": "技術フィードは準備中です"})
            return
        if len(parts) != 3 or parts[2] not in ("refresh", "read", "pin", "rate", "rate-note", "excluded", "body"):
            self._send_json(404, {"ok": False, "error": "見つかりません"})
            return

        action = parts[2]
        if action == "refresh":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._send_json(200, feedlib.start_refresh())
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send_json(400, {"ok": False, "error": "本文が JSON ではありません"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "error": "本文が JSON ではありません"})
            return

        try:
            if action == "read":
                item_id = str(payload.get("id") or "").strip()
                if not item_id:
                    self._send_json(400, {"ok": False, "error": "id が必要です"})
                    return
                ok = feedlib.set_read(item_id, bool(payload.get("read")))
                self._send_json(200, {"ok": bool(ok)})
                return
            if action == "pin":
                item_id = str(payload.get("id") or "").strip()
                if not item_id:
                    self._send_json(400, {"ok": False, "error": "id が必要です"})
                    return
                ok = feedlib.set_pinned(item_id, bool(payload.get("pinned")))
                self._send_json(200, {"ok": bool(ok)})
                return
            if action == "rate":
                item_id = str(payload.get("id") or "").strip()
                if not item_id:
                    self._send_json(400, {"ok": False, "error": "id が必要です"})
                    return
                value = payload.get("value")
                if value not in (1, -1, 0):
                    self._send_json(400, {"ok": False, "error": "value は 1 / -1 / 0 のいずれかです"})
                    return
                note = str(payload.get("note") or "")
                ok = feedlib.set_rating(item_id, value, note)
                self._send_json(200, {"ok": bool(ok)})
                return
            if action == "rate-note":
                item_id = str(payload.get("id") or "").strip()
                if not item_id:
                    self._send_json(400, {"ok": False, "error": "id が必要です"})
                    return
                note = str(payload.get("note") or "")
                ok = feedlib.set_rating_note(item_id, note)
                self._send_json(200, {"ok": bool(ok)})
                return
            if action == "body":
                item_id = str(payload.get("id") or "").strip()
                if not item_id:
                    self._send_json(400, {"ok": False, "error": "id が必要です"})
                    return
                result = feedlib.fetch_body(item_id)
                self._send_json(200, result)
                return
            if action == "excluded":
                word = str(payload.get("word") or "").strip()
                if not word:
                    self._send_json(400, {"ok": False, "error": "word が必要です"})
                    return
                act = payload.get("action")
                if act == "add":
                    result = feedlib.add_excluded_word(word)
                    self._send_json(200, {"ok": True, **result})
                    return
                if act == "remove":
                    result = feedlib.remove_excluded_word(word)
                    self._send_json(200, {"ok": True, **result})
                    return
                self._send_json(400, {"ok": False, "error": "action は add / remove のいずれかです"})
                return
            self._send_json(404, {"ok": False, "error": "見つかりません"})
        except Exception as exc:  # noqa: BLE001（フィード側の不具合で全体を落とさない）
            self._send_json(400, {"ok": False, "error": str(exc)})

    def _render_feed(self) -> str:
        """技術フィードのページ本体。SSR で初期表示ぶんを組み立てる。"""
        items = feedlib.load_items()
        state = feedlib.load_state()
        status = feedlib.status()
        read_map = state.get("read") if isinstance(state.get("read"), dict) else {}
        pinned_map = state.get("pinned") if isinstance(state.get("pinned"), dict) else {}

        # 並びは新着順だけで決める。既読・あとで読むで順番を変えない（絞り込みで見分ける）。
        def sort_key(item: dict) -> float:
            dt = _parse_iso(item.get("published_at", ""))
            return -(dt.timestamp() if dt else 0.0)

        ordered = sorted(items, key=sort_key)

        last_fetched = _parse_iso(status.get("last_fetched_at") or "")
        last_fetched_label = last_fetched.strftime("%Y/%m/%d %H:%M") if last_fetched else "まだ"
        meta = (
            f"最終取得 {last_fetched_label} ／ 未読 {status.get('unread', 0)}"
            f" ／ 要約待ち {status.get('pending_summary', 0)} ／ いいね {status.get('liked', 0)}"
        )
        pending_digest = status.get("pending_digest", 0)
        if pending_digest:
            meta += f" ／ 読みどころ待ち {pending_digest}"
        if status.get("last_error"):
            meta += "／ 前回の取得にエラーあり"

        try:
            rated_map = state.get("rated") if isinstance(state.get("rated"), dict) else {}
        except Exception:  # noqa: BLE001
            rated_map = {}

        items_html = "".join(self._feed_item(item, read_map, pinned_map, rated_map) for item in ordered)

        tabs = (
            '<div class="tabs">'
            f'<button class="tab" data-tab="unread">未読 <span class="n">{status.get("unread", 0)}</span></button>'
            f'<button class="tab" data-tab="pinned">あとで読む <span class="n">{status.get("pinned", 0)}</span></button>'
            f'<button class="tab" data-tab="liked">いいね <span class="n">{status.get("liked", 0)}</span></button>'
            f'<button class="tab is-on" data-tab="all">すべて <span class="n">{status.get("total", 0)}</span></button>'
            "</div>"
        )
        groups = (
            '<div class="groups"><button class="group is-on" data-group="all">全部</button>'
            + "".join(
                f'<button class="group" data-group="{gid}">{html.escape(label)}</button>'
                for gid, label in FEED_GROUPS
            )
            + "</div>"
        )

        try:
            roles = feedlib.load_roles()
        except Exception:  # noqa: BLE001
            roles = []
        role_counts: dict[str, int] = {}
        for item in items:
            item_roles = item.get("roles", [])
            if not isinstance(item_roles, list):
                continue
            for r in item_roles:
                key = str(r)
                role_counts[key] = role_counts.get(key, 0) + 1
        def _role_button(r: dict) -> str:
            rid = str(r.get("id") or "")
            label = str(r.get("label") or "")
            n = role_counts.get(rid, 0)
            disabled = " disabled" if n == 0 else ""
            return (
                f'<button class="role" data-role="{html.escape(rid)}"{disabled}>'
                f'{html.escape(label)} <span class="n">{n}</span></button>'
            )

        role_buttons_parts: list[str] = []
        seen_topic = False
        for r in roles:
            if not isinstance(r, dict):
                continue
            kind = str(r.get("kind") or "role")
            if kind == "topic" and not seen_topic:
                seen_topic = True
                role_buttons_parts.append('<span class="role-sep" aria-hidden="true"></span>')
            role_buttons_parts.append(_role_button(r))

        roles_html = (
            '<div class="roles">'
            f'<button class="role is-on" data-role="all">全部 <span class="n">{len(items)}</span></button>'
            + "".join(role_buttons_parts)
            + "</div>"
        )

        try:
            backlog = feedlib.rating_backlog()
        except Exception:  # noqa: BLE001
            backlog = 0
        nudge_html = (
            f'<p id="taste-nudge" class="nudge">評価が {backlog} 件たまった。傾向をまとめ直せる。</p>'
            if backlog >= 10
            else ""
        )

        try:
            words = feedlib.excluded_words()
        except Exception:  # noqa: BLE001
            words = []
        excluded_html = ""
        if words:
            word_spans = "".join(
                f'<span class="word">{html.escape(str(w))}'
                f'<button class="drop" data-word="{html.escape(str(w))}" aria-label="{html.escape(str(w))} を外す">×</button></span>'
                for w in words
            )
            excluded_html = f'<div class="excluded"><span class="label">不要にした語</span>{word_spans}</div>'

        body = (
            '<div class="wrap">'
            '<header class="feed-head"><h1>技術フィード</h1>'
            '<p class="taste-link"><a href="/feed/taste">傾向を見る</a></p>'
            f'<p class="meta">{html.escape(meta)}</p>'
            '<button id="refresh" class="refresh">更新</button>'
            '<p id="refresh-msg" class="refresh-msg" hidden></p>'
            + nudge_html
            + "</header>"
            '<nav class="filters">'
            + tabs
            + groups
            + roles_html
            + '<input type="search" id="q" placeholder="絞り込み">'
            '<button id="exclude-add" class="exclude-add">この語を不要にする</button>'
            "</nav>"
            + excluded_html
            + f'<ul class="items">{items_html}</ul>'
            '<p class="empty" hidden>該当する記事がない。</p>'
            "</div>"
        )
        return (
            "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>技術フィード</title>"
            "<link rel='icon' type='image/svg+xml' href='/assets/favicon-feed.svg'>"
            "<link rel='stylesheet' href='/assets/feed.css'>"
            "<link rel='stylesheet' href='/assets/nav.css'>"
            "</head><body class='feed-page has-nav'>"
            + self._sidenav("feed")
            + body
            + "<script src='/assets/feed.js'></script></body></html>"
        )

    @staticmethod
    def _feed_item(item: dict, read_map: dict, pinned_map: dict, rated_map: dict | None = None) -> str:
        """フィード記事 1 件ぶんの <li>。"""
        item_id = str(item.get("id") or "")
        is_read = item_id in read_map
        is_pinned = item_id in pinned_map
        rated_map = rated_map or {}
        rating_entry = rated_map.get(item_id) if isinstance(rated_map, dict) else None
        rating = 0
        if isinstance(rating_entry, dict):
            try:
                rating = int(rating_entry.get("v") or 0)
            except (TypeError, ValueError):
                rating = 0
        roles = item.get("roles", [])
        if not isinstance(roles, list):
            roles = []
        roles_attr = " ".join(str(r) for r in roles)
        body_status = str(item.get("body_status") or "none")
        digest_status = str(item.get("digest_status") or "none")
        group = str(item.get("group") or "")
        source_label = str(item.get("source_label") or "")
        title = str(item.get("title") or "")
        url = str(item.get("url") or "")
        summary = str(item.get("summary") or "")
        excerpt = str(item.get("excerpt") or "")
        summary_status = str(item.get("summary_status") or "")
        via = str(item.get("via") or "")
        rel, absolute = _feed_when(str(item.get("published_at") or ""))

        title_html = html.escape(title)
        if url.startswith("http://") or url.startswith("https://"):
            title_link = f'<a class="title" href="{html.escape(url)}" target="_blank" rel="noopener">{title_html}</a>'
        else:
            title_link = f'<span class="title">{title_html}</span>'

        when_html = f'<span class="when" title="{html.escape(absolute)}">{html.escape(rel)}</span>' if rel else ""
        summary_html = f'<p class="summary">{html.escape(summary)}</p>' if summary else ""
        excerpt_html = f'<p class="excerpt">{html.escape(excerpt)}</p>'

        acts = [
            f'<button class="act read">{"未読に戻す" if is_read else "既読にする"}</button>',
            f'<button class="act pin">{"あとで読むを外す" if is_pinned else "あとで読む"}</button>',
            (
                f'<button class="act like emoji" title="{"いいねを外す" if rating == 1 else "いいね"}"'
                f' aria-label="{"いいねを外す" if rating == 1 else "いいね"}">👍</button>'
            ),
            (
                f'<button class="act nope emoji" title="{"不要を外す" if rating == -1 else "不要"}"'
                f' aria-label="{"不要を外す" if rating == -1 else "不要"}">👎</button>'
            ),
        ]
        if summary_status != "done":
            acts.append('<span class="flag pending">要約待ち</span>')
        if via == "ai":
            acts.append('<span class="flag ai">AI が拾った</span>')

        return (
            f'<li class="item" data-id="{html.escape(item_id)}" data-group="{html.escape(group)}"'
            f' data-read="{1 if is_read else 0}" data-pinned="{1 if is_pinned else 0}" data-via="{html.escape(via)}"'
            f' data-rating="{rating}" data-roles="{html.escape(roles_attr)}"'
            f' data-body="{html.escape(body_status)}" data-digest="{html.escape(digest_status)}">'
            '<div class="line">'
            f'<span class="pill {html.escape(group)}">{html.escape(source_label)}</span>'
            + title_link
            + when_html
            + "</div>"
            + summary_html
            + excerpt_html
            + f'<div class="acts">{"".join(acts)}</div>'
            "</li>"
        )

    def _render_taste(self, overview: dict) -> str:
        """/feed/taste の本体。feedlib.taste_overview() の中身を並べるだけ。

        中身が空の節は出さない。全部空なら「まだ評価がない」の 1 行だけにする。
        """

        def fmt(text: object) -> str:
            dt = _parse_iso(str(text or ""))
            return dt.strftime("%Y/%m/%d %H:%M") if dt else ""

        liked = int(overview.get("liked", 0) or 0)
        disliked = int(overview.get("disliked", 0) or 0)
        total = int(overview.get("total", 0) or 0)
        backlog = int(overview.get("rating_backlog", 0) or 0)

        meta = f"いいね {liked} ／ 不要 {disliked} ／ 記事 {total}"
        if backlog:
            meta += f" ／ まとめ直してから {backlog} 件の評価"

        sections: list[str] = []

        notes = overview.get("notes") or []
        if isinstance(notes, list) and notes:
            stamp = fmt(overview.get("notes_updated_at"))
            stamp_html = f'<p class="stamp">{html.escape(stamp)} に更新</p>' if stamp else ""
            items = "".join(f"<li>{html.escape(str(n))}</li>" for n in notes)
            sections.append(
                '<section class="taste-block"><h2>好みのメモ</h2>'
                + stamp_html
                + f'<ul class="notes">{items}</ul></section>'
            )

        def tally_table(rows: list) -> str:
            body = "".join(
                f'<tr><td>{html.escape(str(r.get("label", "")))}</td>'
                f'<td class="num good">{int(r.get("like", 0) or 0)}</td>'
                f'<td class="num bad">{int(r.get("dislike", 0) or 0)}</td></tr>'
                for r in rows
                if isinstance(r, dict)
            )
            return (
                '<table class="tally"><thead><tr><th>{}</th><th>いいね</th><th>不要</th></tr></thead>'
                f"<tbody>{body}</tbody></table>"
            )

        sources = overview.get("sources") or []
        if isinstance(sources, list) and sources:
            sections.append(
                '<section class="taste-block"><h2>出典ごとの当たり外れ</h2>'
                + tally_table(sources).format("出典")
                + "</section>"
            )

        roles = overview.get("roles") or []
        if isinstance(roles, list) and roles:
            sections.append(
                '<section class="taste-block"><h2>立場ごとの当たり外れ</h2>'
                + tally_table(roles).format("立場")
                + "</section>"
            )

        reasons = overview.get("reasons") or []
        if isinstance(reasons, list) and reasons:
            items = "".join(self._taste_reason(r, fmt) for r in reasons if isinstance(r, dict))
            sections.append(
                '<section class="taste-block"><h2>不要にした理由</h2>'
                f'<ul class="reasons">{items}</ul></section>'
            )

        liked_items = overview.get("liked_items") or []
        if isinstance(liked_items, list) and liked_items:
            items = "".join(self._taste_row(it, fmt) for it in liked_items if isinstance(it, dict))
            sections.append(
                '<section class="taste-block"><h2>いいねした記事</h2>'
                f'<ul class="rated">{items}</ul></section>'
            )

        disliked_items = overview.get("disliked_items") or []
        no_reason = [
            it for it in disliked_items if isinstance(it, dict) and not str(it.get("note") or "").strip()
        ]
        if no_reason:
            items = "".join(self._taste_row(it, fmt) for it in no_reason)
            sections.append(
                '<section class="taste-block"><h2>不要にした記事（理由なし）</h2>'
                f'<ul class="rated">{items}</ul></section>'
            )

        words = overview.get("excluded_words") or []
        if isinstance(words, list) and words:
            items = "".join(f"<li>{html.escape(str(w))}</li>" for w in words)
            sections.append(
                '<section class="taste-block"><h2>不要にした語</h2>'
                f'<ul class="words">{items}</ul></section>'
            )

        retired = overview.get("retired") or []
        if isinstance(retired, list) and retired:
            items = "".join(self._taste_retired_row(it, fmt) for it in retired if isinstance(it, dict))
            sections.append(
                '<section class="taste-block"><h2>消えた記事に残っている評価</h2>'
                f'<ul class="rated">{items}</ul></section>'
            )

        if not sections:
            sections.append(
                '<p class="empty">まだ評価がない。技術フィードで 👍 や 👎 を付けると、ここに傾向が出る。</p>'
            )

        body = (
            '<div class="wrap">'
            '<header class="taste-head">'
            '<p class="back"><a href="/feed">← 技術フィード</a></p>'
            "<h1>いま分かっている傾向</h1>"
            f'<p class="meta">{html.escape(meta)}</p>'
            "</header>"
            + "".join(sections)
            + "</div>"
        )
        return (
            "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>技術フィードの傾向</title>"
            "<link rel='icon' type='image/svg+xml' href='/assets/favicon-taste.svg'>"
            "<link rel='stylesheet' href='/assets/feed.css'>"
            "<link rel='stylesheet' href='/assets/nav.css'>"
            "</head><body class='taste-page has-nav'>"
            + self._sidenav("taste")
            + body
            + "</body></html>"
        )

    @staticmethod
    def _taste_link(url: str, text_html: str, cls: str = "title") -> str:
        """URL が http(s) のときだけリンクにする。text_html は呼び出し側で escape 済み。"""
        if url.startswith("http://") or url.startswith("https://"):
            return f'<a class="{cls}" href="{html.escape(url)}" target="_blank" rel="noopener">{text_html}</a>'
        return f'<span class="{cls}">{text_html}</span>'

    @classmethod
    def _taste_reason(cls, item: dict, fmt) -> str:
        title = html.escape(str(item.get("title") or ""))
        url = str(item.get("url") or "")
        source_label = html.escape(str(item.get("source_label") or ""))
        when = html.escape(fmt(item.get("at")))
        from_html = f"{source_label} ／ {when}" if when else source_label
        note = html.escape(str(item.get("note") or ""))
        return (
            '<li class="reason">'
            + cls._taste_link(url, title)
            + f'<span class="from">{from_html}</span>'
            + (f'<p class="note">{note}</p>' if note else "")
            + "</li>"
        )

    @classmethod
    def _taste_row(cls, item: dict, fmt) -> str:
        title = html.escape(str(item.get("title") or ""))
        url = str(item.get("url") or "")
        source_label = html.escape(str(item.get("source_label") or ""))
        when = html.escape(fmt(item.get("at")))
        from_html = f"{source_label} ／ {when}" if when else source_label
        return f'<li class="row">{cls._taste_link(url, title)}<span class="from">{from_html}</span></li>'

    @classmethod
    def _taste_retired_row(cls, item: dict, fmt) -> str:
        try:
            v = int(item.get("v") or 0)
        except (TypeError, ValueError):
            v = 0
        if v == 1:
            mark = '<span class="mark good">いいね</span>'
        elif v == -1:
            mark = '<span class="mark bad">不要</span>'
        else:
            mark = ""
        title = html.escape(str(item.get("title") or ""))
        url = str(item.get("url") or "")
        source_label = html.escape(str(item.get("source_id") or ""))
        when = html.escape(fmt(item.get("at")))
        from_html = f"{source_label} ／ {when}" if when else source_label
        note = html.escape(str(item.get("note") or ""))
        return (
            '<li class="row">'
            + mark
            + cls._taste_link(url, title)
            + f'<span class="from">{from_html}</span>'
            + (f'<p class="note">{note}</p>' if note else "")
            + "</li>"
        )

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
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text("utf-8"))
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
        """3 画面（/・/feed・/feed/taste）共通の左サイドバー。

        feedlib が import できないときはフィード系の項目を出さない
        （既存のフィード系ルートと同じ考え方）。
        """
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
        if feedlib is not None:
            try:
                unread = feedlib.status().get("unread", 0)
            except Exception:  # noqa: BLE001
                unread = 0
            feed_badge = f' <span class="n">{unread}</span>' if unread else ""
            items.append(
                f'<li><a class="nav-item{" is-here" if active == "feed" else ""}" href="/feed">'
                f"技術フィード{feed_badge}</a></li>"
            )
            items.append(
                '<li><a class="nav-item'
                + (" is-here" if active == "taste" else "")
                + '" href="/feed/taste">傾向</a></li>'
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

        feed_link = ""
        if feedlib is not None:
            try:
                unread = feedlib.status().get("unread", 0)
            except Exception:  # noqa: BLE001（フィード側の不具合で一覧を落とさない）
                unread = 0
            badge = f" <code>{unread}</code>" if unread else ""
            feed_link = f'<p class="lede"><a href="/feed">技術フィード{badge}</a></p>'

        return self._page(
            "レポート一覧",
            "<header><h1>レポート</h1>"
            + feed_link
            + "<p class='lede'>答え待ちのものが上に来る。開いて確認事項に回答すると、"
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
