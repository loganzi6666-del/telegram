"""받은 파일을 같은 와이파이의 휴대폰(아이폰/안드로이드)으로 옮기는 간단한 웹 서버.

아이폰 사파리는 동영상 링크를 누르면 그냥 재생해 버리므로, 여기서는
``Content-Disposition: attachment`` 를 붙여 '파일' 앱에 저장되도록 한다.
추가 설치가 필요 없고, 파이썬 기본 기능만 사용한다.
"""

from __future__ import annotations

import html
import http.server
import io
import os
import socket
import socketserver
import sys
import urllib.parse
from functools import partial
from pathlib import Path

PAGE_STYLE = """
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; font-family: -apple-system, BlinkMacSystemFont,
       "Segoe UI", "Malgun Gothic", sans-serif; background: #f5f5f7; color: #1d1d1f; }
h1 { font-size: 20px; margin: 0 0 4px; }
p.hint { margin: 0 0 16px; font-size: 14px; color: #6e6e73; line-height: 1.5; }
ul { list-style: none; margin: 0; padding: 0; }
li { background: #fff; border-radius: 12px; margin-bottom: 8px; }
a.row, span.row { display: flex; align-items: center; gap: 10px; padding: 14px 16px;
       text-decoration: none; color: inherit; font-size: 16px; word-break: break-all; }
span.row { color: #8e8e93; }
.name { flex: 1; }
.size { font-size: 13px; color: #6e6e73; white-space: nowrap; }
.empty { background: #fff; border-radius: 12px; padding: 16px; font-size: 15px;
       color: #6e6e73; }
"""


def human_size(num: float) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def lan_ip() -> str:
    """같은 공유기 안에서 접속할 때 쓰는 내 컴퓨터 주소."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # 실제로 보내지는 않는다
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "tgdl-serve"

    def log_message(self, fmt, *args) -> None:
        """실제로 파일이 전달된 경우만 한 줄 남긴다(차단된 요청은 찍지 않는다)."""
        status = str(args[1]) if len(args) > 1 else ""
        if status == "200" and getattr(self, "_attach", None):
            print(f"  → 휴대폰으로 전송: {self._attach}", file=sys.stderr, flush=True)

    def send_head(self):
        self._attach = None
        try:
            path = self.translate_path(self.path)
        except Exception:  # noqa: BLE001
            path = ""
        if path and os.path.isfile(path):
            self._attach = os.path.basename(path)
        return super().send_head()

    def end_headers(self) -> None:
        name = getattr(self, "_attach", None)
        if name:
            # 사파리가 재생 대신 '파일' 앱에 저장하도록 만든다.
            quoted = urllib.parse.quote(name, safe="")
            self.send_header(
                "Content-Disposition", f"attachment; filename*=UTF-8''{quoted}"
            )
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def list_directory(self, path):
        try:
            entries = sorted(
                os.scandir(path), key=lambda item: (not item.is_dir(), item.name.lower())
            )
        except OSError:
            self.send_error(404, "폴더를 읽을 수 없습니다")
            return None

        here = urllib.parse.unquote(self.path)
        rows = []
        if here not in ("/", ""):
            rows.append('<li><a class="row" href=".."><span class="name">⬆️ 상위 폴더</span></a></li>')

        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            link = urllib.parse.quote(name)
            safe = html.escape(name)
            if entry.is_dir():
                rows.append(
                    f'<li><a class="row" href="{link}/">'
                    f'<span class="name">📁 {safe}</span></a></li>'
                )
            elif name.endswith(".part"):
                size = human_size(entry.stat().st_size)
                rows.append(
                    f'<li><span class="row"><span class="name">⏳ {safe[:-5]}'
                    f' (받는 중)</span><span class="size">{size}</span></span></li>'
                )
            else:
                size = human_size(entry.stat().st_size)
                rows.append(
                    f'<li><a class="row" href="{link}">'
                    f'<span class="name">🎬 {safe}</span>'
                    f'<span class="size">{size}</span></a></li>'
                )

        body = "\n".join(rows) or '<li class="empty">이 폴더에는 받은 파일이 없습니다.</li>'
        page = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>받은 동영상</title><style>{PAGE_STYLE}</style></head>
<body>
<h1>받은 동영상</h1>
<p class="hint">파일 이름을 누르면 휴대폰의 <b>'파일'</b> 앱에 저장됩니다.<br>
저장한 뒤 파일 앱에서 공유 → <b>'비디오 저장'</b> 을 누르면 사진첩으로 들어갑니다.</p>
<ul>
{body}
</ul>
</body></html>"""

        encoded = page.encode("utf-8", "surrogateescape")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        return io.BytesIO(encoded)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_server(directory: str, port: int = 8000) -> int:
    """받은 파일 폴더를 같은 와이파이에 공개한다. Ctrl+C 로 종료."""
    folder = Path(directory).expanduser()
    folder.mkdir(parents=True, exist_ok=True)

    handler = partial(Handler, directory=str(folder))
    try:
        server = Server(("0.0.0.0", port), handler)
    except OSError as exc:
        print(
            f"{port} 번 포트를 열 수 없습니다 ({exc}).\n"
            f"다른 번호로 해보세요: python -m tgdl serve --port {port + 1}",
            file=sys.stderr,
        )
        return 2

    address = f"http://{lan_ip()}:{port}"
    print("=" * 52)
    print("  휴대폰(아이폰)에서 아래 주소를 사파리에 입력하세요")
    print()
    print(f"        {address}")
    print()
    print(f"  공개 중인 폴더: {folder}")
    print("  같은 와이파이에 연결돼 있어야 합니다.")
    print("  끝내려면 이 창에서 Ctrl+C 를 누르세요.")
    print("=" * 52)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 닫았습니다. 이제 다른 기기에서 접속할 수 없습니다.")
    finally:
        server.server_close()
    return 0
