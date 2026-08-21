#!/usr/bin/env python3
"""report-hub の運用ルールを、指定した階層の CLAUDE.md から効かせる。

report-hub は clone しただけでは効かない。「成果物は HTML レポートで出す」という
運用ルールを AI が読む CLAUDE.md に入れて、はじめてその通りに動く。
どの階層の CLAUDE.md に入れるかは使う人が決める（全プロジェクトに効かせるなら
~/.claude、特定の組織配下だけなら ~/ghq/github.com/<org> など）。

  python3 bin/setup-claudemd.py status --target ~/.claude       書き込む中身と差分
  python3 bin/setup-claudemd.py apply  --target ~/.claude --yes 書き込む
  python3 bin/setup-claudemd.py install-skill [--yes]           /report-hub-setup を使えるようにする

書き込むのは既定では**ルール本体そのものではなく、それを読み込む数行**（link）。
本体は repo の rules/report-hub.md にあり、CLAUDE.md からの読み込み（@ 記法）で参照する。
こうしておくと、ルールを直したときに各マシンの CLAUDE.md を書き換えて回らずに済む
（配った先は git pull するだけ）。

読み込みに対応していない道具で使うときだけ `--mode embed` を使う。本文をそのまま
貼るので更新のたびに入れ直しが要る。

どちらの場合も、既に別のルールが書かれていても壊さないよう、マーカーで挟んだ区画だけを
入れ替える（無ければ末尾へ足す）。二重に貼られない。
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
REPO_DIR = Path(__file__).resolve().parent.parent
SOURCE = REPO_DIR / "rules" / "report-hub.md"

BEGIN = "<!-- report-hub:begin この区画は report-hub が書く。ルールを直すなら repo の rules/report-hub.md -->"
END = "<!-- report-hub:end -->"

# ルール本文の中で置き場所を指す書き方。これを実際の場所へ読み替える
PLACEHOLDER = "<report-hub>"

SKILL_DIR = REPO_DIR / "skills" / "report-hub-setup"
SKILL_LINK = HOME / ".claude" / "skills" / "report-hub-setup"


def _repo_as_tilde() -> str:
    """このマシンでの report-hub の場所。ホーム配下なら `~/…` の形で返す。"""
    try:
        return "~/" + str(REPO_DIR.relative_to(HOME))
    except ValueError:
        return str(REPO_DIR)


def _resolve_target(raw: str) -> Path:
    """--target をこのマシンの CLAUDE.md のパスに直す。"""
    path = Path(raw).expanduser()
    if path.is_dir() or path.suffix != ".md":
        path = path / "CLAUDE.md"
    return path


def _body_link() -> str:
    """ルール本体を読み込む数行。中身は repo 側に置いたまま参照する。"""
    here = _repo_as_tilde()
    return (
        "# 成果物は HTML レポートで出す（report-hub）\n\n"
        "調査結果・作業計画・実施結果・レビュー・確認事項は、チャットの箇条書きで済ませず\n"
        "report-hub の HTML レポートとして出す。運用ルールの本体は次の 1 行で読み込む。\n\n"
        f"@{here}/rules/report-hub.md\n\n"
        f"- ルール本文にある `{PLACEHOLDER}` は、このマシンでは `{here}` を指す。\n"
        "- ルールを直すときは読み込み先（`rules/report-hub.md`）を直す。このファイルは書き換えなくてよい。\n"
        f"- 配布を受けた側は `cd {here} && git pull` だけで最新のルールになる。\n"
    )


def _body_embed() -> str:
    """ルール本文をそのまま貼る形。読み込みに対応していない道具向け。"""
    body = SOURCE.read_text("utf-8").strip()
    here = _repo_as_tilde()
    return body.replace(PLACEHOLDER, here)


def _block(mode: str) -> str:
    """書き込む区画（マーカー込み）。"""
    body = _body_link() if mode == "link" else _body_embed()
    return f"{BEGIN}\n{body.strip()}\n{END}\n"


def _merged(target: Path, mode: str) -> str:
    """書き込んだ後の CLAUDE.md の全文。"""
    block = _block(mode)
    current = target.read_text("utf-8") if target.is_file() else ""
    if BEGIN in current and END in current:
        head, rest = current.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        return head + block + tail.lstrip("\n")
    if not current.strip():
        return block
    return current.rstrip("\n") + "\n\n" + block


def cmd_status(args: argparse.Namespace) -> int:
    target = _resolve_target(args.target)
    current = target.read_text("utf-8") if target.is_file() else ""
    print(f"ルール本体: {SOURCE}")
    print(f"書き込む先: {target}（{'あり' if target.is_file() else 'なし'}）")
    print(f"書き方　　: {args.mode}"
          + ("（本体は repo 側のまま。読み込む数行だけ書く）" if args.mode == "link"
             else "（本文をそのまま貼る。更新のたびに入れ直しが要る）"))
    print(f"置き場所　: {PLACEHOLDER} → {_repo_as_tilde()}")
    after = _merged(target, args.mode)
    if current == after:
        print("\n差分なし。すでに最新。")
        return 0
    print()
    sys.stdout.writelines(difflib.unified_diff(
        current.splitlines(True), after.splitlines(True),
        fromfile="現状/CLAUDE.md", tofile="書き込んだ後/CLAUDE.md", n=2,
    ))
    print(f"\n（{'区画を入れ替える' if BEGIN in current else '区画を足す'}）")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    target = _resolve_target(args.target)
    current = target.read_text("utf-8") if target.is_file() else ""
    after = _merged(target, args.mode)
    if current == after:
        print("差分なし。何もしない。")
        return 0
    if not args.yes:
        print("--yes を付けると書き込む。先に status で中身を確かめること。")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    if current:
        backup = target.with_name(f"{target.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        shutil.copyfile(target, backup)
        print(f"控え: {backup}")
    target.write_text(after, "utf-8")
    print(f"書き込んだ: {target}")
    print("効くのは次にセッションを開いたときから。いまの会話には自動では読み込まれない。")
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    """clone した repo の skills/report-hub-setup を ~/.claude/skills から参照させる。

    スキルは ~/.claude/skills に入って初めて `/report-hub-setup` で呼べる。
    実体は repo 側に置いたまま symlink で参照させるので、repo を更新すればスキルも新しくなる。
    """
    print(f"スキルの実体: {SKILL_DIR}")
    print(f"入れる先　　: {SKILL_LINK}")
    if SKILL_LINK.is_symlink() and SKILL_LINK.resolve() == SKILL_DIR:
        print("\nすでに入っている。何もしない。")
        return 0
    if SKILL_LINK.exists() or SKILL_LINK.is_symlink():
        print("\n同じ名前のものが既にある。中身を確かめて、要らなければ自分で退けること。")
        return 1
    if not args.yes:
        print("\n--yes を付けると symlink を張る。")
        return 0
    SKILL_LINK.parent.mkdir(parents=True, exist_ok=True)
    SKILL_LINK.symlink_to(SKILL_DIR)
    print("入れた。Claude Code を開き直すと /report-hub-setup で呼べる。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text, func in (
        ("status", "差分を見る", cmd_status),
        ("apply", "指定した CLAUDE.md へ書き込む", cmd_apply),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--target", required=True,
                       help="CLAUDE.md を置く階層（ディレクトリ）か CLAUDE.md そのもの")
        p.add_argument("--mode", choices=("link", "embed"), default="link",
                       help="link: ルール本体を読み込む数行だけ書く（既定）／embed: 本文をそのまま貼る")
        if name == "apply":
            p.add_argument("--yes", action="store_true", help="実際に書き込む")
        p.set_defaults(func=func)
    p = sub.add_parser("install-skill", help="/report-hub-setup を ~/.claude/skills から使えるようにする")
    p.add_argument("--yes", action="store_true", help="実際に symlink を張る")
    p.set_defaults(func=cmd_install_skill)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
