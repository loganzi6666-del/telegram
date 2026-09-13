"""`python -m tgdl update` — 최신 코드로 갱신한다.

git 으로 받은 폴더라면 `git pull` 로 코드를 갱신하고, 필요한 모듈도 맞춰 설치한다.
ZIP 으로 받은 폴더라면 그 사실을 알려주고 git clone 방법을 안내한다.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

BRANCH = "claude/elegant-fermat-eyudcb"
CLONE_HINT = (
    "이 폴더는 git 으로 받은 것이 아니어서 자동 갱신할 수 없습니다.\n"
    "한 번만 아래처럼 git 으로 다시 받아두면, 다음부터는\n"
    "`python -m tgdl update` 한 줄로 갱신됩니다.\n\n"
    f"  git clone -b {BRANCH} https://github.com/loganzi6666-del/telegram.git\n"
    "  cd telegram\n"
    "  python -m pip install -r requirements.txt"
)

Runner = Callable[..., subprocess.CompletedProcess]


def repo_root() -> Path:
    """이 패키지가 들어 있는 저장소 폴더."""
    return Path(__file__).resolve().parent.parent


def is_git_repo(root: Optional[Path] = None) -> bool:
    return ((root or repo_root()) / ".git").exists()


def find_git() -> Optional[str]:
    """git 실행 파일 경로(없으면 None). 테스트에서 갈아끼울 수 있게 분리."""
    return shutil.which("git")


def _run(runner: Runner, args: List[str], cwd: Path) -> Tuple[int, str]:
    """명령을 실행해 (종료코드, 출력) 을 돌려준다."""
    try:
        proc = runner(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, output


def run_update(
    out: Callable[[str], None] = print,
    runner: Runner = subprocess.run,
    install_deps: bool = True,
) -> int:
    """최신 코드를 받아온다. 0 이면 성공."""
    root = repo_root()

    if not is_git_repo(root):
        out(CLONE_HINT)
        return 2

    git = find_git()
    if not git:
        out("git 을 찾을 수 없습니다. https://git-scm.com 에서 설치한 뒤 다시 시도하세요.")
        return 2

    out(f"폴더: {root}")

    code, branch = _run(runner, [git, "rev-parse", "--abbrev-ref", "HEAD"], root)
    if code == 0 and branch:
        out(f"가지(branch): {branch}")

    code, before = _run(runner, [git, "rev-parse", "--short", "HEAD"], root)
    before = before if code == 0 else ""

    out("최신 코드를 받아옵니다…")
    code, output = _run(runner, [git, "pull", "--ff-only"], root)
    if code != 0:
        out("갱신하지 못했습니다:")
        out(f"  {output}")
        out(
            "\n내가 고친 내용이 남아 있어 충돌할 수 있습니다. 그대로 버려도 괜찮다면:\n"
            f"  git reset --hard origin/{BRANCH}\n"
            "  git pull"
        )
        return 1

    code, after = _run(runner, [git, "rev-parse", "--short", "HEAD"], root)
    after = after if code == 0 else ""

    if before and after and before == after:
        out("이미 최신입니다. 바뀐 것이 없습니다.")
        return 0

    if before and after:
        code, changes = _run(
            runner, [git, "log", "--oneline", f"{before}..{after}"], root
        )
        if code == 0 and changes:
            out("\n이번에 바뀐 내용:")
            for line in changes.splitlines()[:20]:
                out(f"  · {line}")
    else:
        out(output)

    if install_deps:
        out("\n필요한 모듈을 확인합니다…")
        code, output = _run(
            runner,
            [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
            root,
        )
        if code != 0:
            out("모듈 설치에 실패했습니다(프로그램은 그대로 동작할 수 있습니다):")
            out(f"  {output}")
        else:
            out("모듈 확인 완료.")

    out("\n✔ 갱신을 마쳤습니다. 이제 그대로 실행하세요: python -m tgdl")
    return 0
