"""`python -m tgdl update` 테스트(가짜 명령 실행기 사용). `python tests/test_update.py`"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgdl import selfupdate  # noqa: E402


class FakeRunner:
    """git/pip 호출을 흉내내고, 무엇을 불렀는지 기록한다.

    같은 명령이 여러 번 불릴 수 있으므로(갱신 전·후 커밋 확인) 값을 목록으로
    주면 순서대로 하나씩 돌려준다. 다 쓰면 마지막 값을 계속 쓴다.
    """

    def __init__(self, script: dict):
        self.script = {
            key: list(value) if isinstance(value, list) else [value]
            for key, value in script.items()
        }
        self.calls: list = []

    def __call__(self, args, cwd=None, capture_output=None, text=None,
                 timeout=None, check=None):
        self.calls.append(list(args))
        key = "pip" if "pip" in args else " ".join(args[1:3])
        queue = self.script.get(key) or [(0, "")]
        code, output = queue.pop(0) if len(queue) > 1 else queue[0]
        return subprocess.CompletedProcess(args, code, output, "")

    def called(self, needle: str) -> bool:
        return any(needle in " ".join(call) for call in self.calls)


class Capture:
    def __init__(self):
        self.lines: list = []

    def __call__(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def with_fake_repo(script, install_deps=True, git="/usr/bin/git"):
    """임시 폴더를 저장소로 위장해 run_update 를 실행한다."""
    out = Capture()
    runner = FakeRunner(script)
    original_root, original_git = selfupdate.repo_root, selfupdate.find_git
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".git").mkdir()  # git 으로 받은 폴더로 위장
        selfupdate.repo_root = lambda: root
        selfupdate.find_git = lambda: git
        try:
            code = selfupdate.run_update(
                out=out, runner=runner, install_deps=install_deps
            )
        finally:
            selfupdate.repo_root = original_root
            selfupdate.find_git = original_git
    return code, out, runner


def test_zip_folder_explains_how_to_clone():
    out = Capture()
    original = selfupdate.repo_root
    with tempfile.TemporaryDirectory() as tmp:
        selfupdate.repo_root = lambda: Path(tmp)  # .git 없음
        try:
            code = selfupdate.run_update(out=out, runner=FakeRunner({}))
        finally:
            selfupdate.repo_root = original
    assert code == 2
    assert "git clone" in out.text
    assert selfupdate.BRANCH in out.text


def test_missing_git_is_reported():
    code, out, _ = with_fake_repo({}, git=None)
    assert code == 2
    assert "git 을 찾을 수 없습니다" in out.text


def test_successful_update_lists_changes_and_installs():
    code, out, runner = with_fake_repo(
        {
            "rev-parse --abbrev-ref": (0, "claude/elegant-fermat-eyudcb"),
            "rev-parse --short": [(0, "aaa1111"), (0, "ccc3333")],  # 갱신 전 → 후
            "pull --ff-only": (0, "Updating aaa1111..ccc3333"),
            "log --oneline": (0, "bbb2222 속도 개선\nccc3333 진행률 수정"),
            "pip": (0, ""),
        }
    )
    assert code == 0
    assert "속도 개선" in out.text and "진행률 수정" in out.text
    assert "갱신을 마쳤습니다" in out.text
    assert runner.called("pull --ff-only")
    assert runner.called("pip install"), "필요한 모듈도 맞춰 설치해야 한다"


def test_already_up_to_date_says_so():
    code, out, runner = with_fake_repo(
        {
            "rev-parse --abbrev-ref": (0, "main"),
            "rev-parse --short": (0, "same123"),  # 전후 동일
            "pull --ff-only": (0, "Already up to date."),
        }
    )
    assert code == 0
    assert "이미 최신입니다" in out.text
    assert not runner.called("pip install"), "바뀐 게 없으면 설치도 건너뛴다"


def test_pull_conflict_gives_recovery_command():
    code, out, _ = with_fake_repo(
        {
            "rev-parse --abbrev-ref": (0, "claude/elegant-fermat-eyudcb"),
            "rev-parse --short": (0, "aaa1111"),
            "pull --ff-only": (1, "error: Your local changes would be overwritten"),
        }
    )
    assert code == 1
    assert "갱신하지 못했습니다" in out.text
    assert "git reset --hard" in out.text, "막혔을 때 빠져나갈 방법을 알려줘야 한다"


def test_dependency_install_failure_is_not_fatal():
    code, out, _ = with_fake_repo(
        {
            "rev-parse --abbrev-ref": (0, "main"),
            "rev-parse --short": [(0, "aaa1111"), (0, "bbb2222")],
            "pull --ff-only": (0, "updated"),
            "log --oneline": (0, "bbb2222 뭔가 변경"),
            "pip": (1, "네트워크 오류"),
        }
    )
    # 코드 갱신은 성공했으므로 실패로 처리하지 않는다
    assert code == 0
    assert "모듈 설치에 실패" in out.text


def test_real_repo_is_recognized():
    """이 저장소 자체는 git 으로 받은 폴더여야 한다."""
    assert selfupdate.repo_root().name != ""
    assert (selfupdate.repo_root() / "tgdl").is_dir()


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
