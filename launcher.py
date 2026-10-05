# -*- coding: utf-8 -*-
"""launcher.py —— 网页启动器：把终端里的双教练对谈搬进浏览器

跑法：
    .venv/bin/python launcher.py            # 起服务，并自动打开浏览器
    .venv/bin/python launcher.py --mock     # 默认勾上模拟模式：不花钱、不联网

**日常用法是双击桌面快捷方式**（见 make_shortcut.py），不用敲命令。快捷方式的
目标就是「起服务 + 开浏览器」两件事一次做完。

两件为「双击」做的事：
1. `_open_browser()` 走 WSL 互操作调 Windows 的 explorer.exe——WSL 里 webbrowser
   模块找的是 Linux 侧的图形浏览器，基本没装，指望不上。
2. `_existing_instance()` + `/health`：**重复双击不会起第二个服务**，只会把浏览器
   指到已经在跑的那个。少了这一步，第二次双击会顺延到 8776，于是你有两个各自
   为政的页面，而且新那个啥历史都没有。

**零新增依赖**，只用标准库。不做独立窗口（Tk）而做网页：中文排版交给浏览器，
比 Tk 省心一个量级，也不用给 venv 装 tkinter。

分层红线
--------
`launcher.py import fitness_duo.pipeline`，**pipeline 绝不 import launcher**。
所以 `uv run fitness-duo run` 在这台机器没有浏览器、没有 Windows 的情况下照样能跑。

线程模型（改这个文件之前先读完这三条）
--------------------------------------
    HTTP 线程池（ThreadingHTTPServer，每个连接一个线程）
       ├─ GET  /events   → 阻塞在自己的订阅队列上，逐条 SSE 写出
       └─ POST /stop     → stopped.set()

    daemon worker 线程：**同步**调用 pipeline.run_pipeline(...)
       └─ 各 hook → bus.publish(...)

1. **worker 线程里一个 HTTP 调用都不许发**——只往广播里放。
   （比起参考的 autogen 项目这里简单一档：CAMEL 的 step() 是同步的，
     不需要在 worker 里跑 asyncio。）
2. 跨线程的裸状态只有两个，都是单向「只写不读回」：`session.stopped`
   （threading.Event）和跑完一次性赋值的 `server.handbook_text`。
3. **worker 线程必须 daemon，而且永不 join**——join 会把「关掉页面」变成
   「等模型把这一轮跑完」。
"""

from __future__ import annotations

import argparse
import json
import queue
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fitness_duo import pipeline
from fitness_duo.config import MissingApiKey, build_backend
from fitness_duo.personas import COACH, NUTRITIONIST
from fitness_duo.topics import CLOSING_TOPIC, TOPICS

#: /health 里报的身份。启动时用它认「这个端口上是不是已经有一个我了」——
#: 双击图标两次是很常见的动作，不该因此起了两个服务、开出两个各自为政的页面。
_APP_ID = "camel-fitness-nutrition-duo-launcher"

#: 默认端口。**故意避开 8765**——那是 autogen-travel-agent 的 launcher 在用的，
#: 两个项目一起跑时单实例扫描会互相顺延，认错人。
DEFAULT_PORT = 8775

#: 说话人 -> 前端配色用的标识。前端按这个上色，不硬编中文名。
_SIDE = {NUTRITIONIST.name: "lin", COACH.name: "chen"}


# ---------------------------------------------------------------------------
# 1. 广播：worker 发一条，所有开着页面的浏览器各收一份
# ---------------------------------------------------------------------------

class _Bus:
    """极简广播 + 历史回放。

    历史回放是为了**刷新页面不丢内容**：新订阅者一上来先把已有事件补给它。
    事件带一个**单调递增、永不重置**的 seq，前端用它去重，于是重连/重放不会
    把消息渲染两遍。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._history: list[dict] = []
        self._seq = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
            replay = list(self._history)
        for event in replay:
            q.put(event)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            self._seq += 1
            event = {**event, "seq": self._seq}
            self._history.append(event)
            subs = list(self._subs)
        for q in subs:
            q.put(event)

    def reset(self) -> None:
        """开始新的一轮：清掉历史，但 **seq 不重置**（否则前端去重会误杀新事件）。"""
        with self._lock:
            self._history.clear()


# ---------------------------------------------------------------------------
# 2. 一次 run 的状态
# ---------------------------------------------------------------------------

class _Session:
    """一次 run 的全部状态。每次点「开始」新建一个，用完即弃。"""

    def __init__(self, bus: _Bus, *, topics, rounds: int, mock: bool) -> None:
        self.bus = bus
        self.topics = topics
        self.rounds = rounds
        self.mock = mock
        self.stopped = threading.Event()
        self.finished = threading.Event()
        self.handbook = ""
        self.handbook_path: Path | None = None
        self._thread: threading.Thread | None = None

    # ---- hooks：全在 worker 线程里被 pipeline 调用 ----

    def _hooks(self) -> pipeline.PipelineHooks:
        bus = self.bus

        def run_start(total: int, rounds: int) -> None:
            bus.publish({
                "type": "run_start",
                "total": total,
                "rounds": rounds,
                "mock": self.mock,
                "topics": [{"key": t.key, "title": t.section_title} for t in self.topics],
            })

        def topic_start(index: int, total: int, topic) -> None:
            bus.publish({
                "type": "topic_start",
                "index": index,
                "total": total,
                "key": topic.key,
                "title": topic.section_title,
            })

        def turn(topic, t) -> None:
            bus.publish({
                "type": "turn",
                "topic": topic.key,
                "speaker": t.speaker,
                "side": _SIDE.get(t.speaker, "other"),
                "text": t.content,
            })

        def topic_done(topic, transcript) -> None:
            bus.publish({
                "type": "topic_done",
                "topic": topic.key,
                "turns": len(transcript.turns),
            })

        def section_start(topic, is_closing: bool) -> None:
            bus.publish({
                "type": "section_start",
                "topic": topic.key,
                "title": topic.section_title,
                "closing": is_closing,
            })

        def section_done(topic, is_closing: bool, markdown: str) -> None:
            # 只报「整理好了」，正文由 /handbook 单独取——免得大 payload 走 SSE
            bus.publish({
                "type": "section_done",
                "topic": topic.key,
                "title": topic.section_title,
                "closing": is_closing,
            })

        return pipeline.PipelineHooks(
            on_run_start=run_start,
            on_topic_start=topic_start,
            on_turn=turn,
            on_topic_done=topic_done,
            on_section_start=section_start,
            on_section_done=section_done,
        )

    # ---- 生命周期 ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        try:
            backend = build_backend(mock=self.mock)
            result = pipeline.run_pipeline(
                backend=backend,
                rounds=self.rounds,
                topics=self.topics,
                mock=self.mock,
                hooks=self._hooks(),
                should_stop=self.stopped.is_set,
            )
            self.handbook = result.handbook
            self.handbook_path = result.handbook_path
            self.bus.publish({
                "type": "run_done",
                "transcript": str(result.transcript_path),
                "handbook": str(result.handbook_path),
            })
        except pipeline.RunStopped:
            # 用户喊停：本轮作废，一个文件都没写。
            self.bus.publish({
                "type": "notice",
                "kind": "final",
                "text": "已停止。本轮作废，没有写出任何文件。",
            })
        except MissingApiKey as exc:
            # 这条不是堆栈，是一段给人看的操作指引，整段放进页面比截首行有用。
            self._fail(str(exc).strip(), full=False)
        except Exception as exc:                                    # noqa: BLE001
            self._fail(str(exc).strip())
        finally:
            self.finished.set()
            self.bus.publish({"type": "done"})

    def _fail(self, text: str, *, full: bool = True) -> None:
        """把错误放上页面；`full=True` 时完整内容留在终端（网页只放首行）。"""
        print(f"\n[运行失败]\n{text}\n", file=sys.stderr, flush=True)
        if full:
            first = text.splitlines()[0] if text else "未知错误"
            if len(first) > 300:
                first = first[:300] + "…"
            text = f"{first}（完整信息见终端）"
        self.bus.publish({"type": "notice", "kind": "warning", "text": text})

    def request_stop(self) -> None:
        """优雅停：pipeline 在下一个议题/轮次边界收手。

        最坏情况要等当前这轮 step() 跑完（它内部是两次模型调用，掐不断），
        所以页面文案写的是「会在当前这轮结束后停下」。
        """
        self.stopped.set()


# ---------------------------------------------------------------------------
# 3. HTTP
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "FitnessDuoLauncher"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args) -> None:      # 别把每条请求刷到终端
        pass

    # ---- 工具 ----

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _json(self, payload: dict) -> None:
        self._send(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send(self, blob: bytes, ctype: str, *, filename: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        if filename:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
        self.end_headers()
        self.wfile.write(blob)

    # ---- 路由 ----

    def do_GET(self) -> None:                       # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(_build_page(self.server), "text/html; charset=utf-8")
        elif path == "/health":
            # 只报身份，不碰 session——所以跑着一轮的时候它照样秒回，
            # 这正是「重复双击能不能认出自己人」需要的性质。
            session = self.server.session
            running = session is not None and not session.finished.is_set()
            self._json({"app": _APP_ID, "running": running})
        elif path == "/events":
            self._stream()
        elif path == "/handbook":
            session = self.server.session
            self._json({
                "markdown": session.handbook if session else "",
                "path": str(session.handbook_path) if session and session.handbook_path else "",
            })
        elif path == "/handbook.md":
            session = self.server.session
            if session is None or not session.handbook:
                # 消息必须是 ASCII：http.server 按 latin-1 编码状态行，中文会直接抛
                # UnicodeEncodeError，把 handler 干掉，而不是干净地返回 404。
                self.send_error(404, "No handbook yet")
                return
            name = session.handbook_path.name if session.handbook_path else "handbook.md"
            self._send(
                session.handbook.encode("utf-8"),
                "text/markdown; charset=utf-8",
                filename=name,
            )
        elif path == "/favicon.ico":
            self.send_response(204)                 # 浏览器总会来要，别让它 404 刷控制台
            self.end_headers()
        else:
            self.send_error(404)

    def do_POST(self) -> None:                      # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/start":
            current = self.server.session
            if current is not None and not current.finished.is_set():
                # 防呆：两个标签页各点一次「开始」，会把一次付费的跑变成两次。
                self._json({"ok": False, "why": "已经有一轮在跑了"})
                return

            body = self._body()
            keys = [str(k) for k in (body.get("topics") or [])]
            custom = [c for c in (body.get("custom") or []) if isinstance(c, dict)]
            try:
                rounds = max(1, min(6, int(body.get("rounds") or 3)))
            except (TypeError, ValueError):
                rounds = 3
            mock = bool(body.get("mock"))

            if not keys and not custom:
                self._json({"ok": False, "why": "至少要选一个议题（或者自己加一个）。"})
                return

            try:
                topics = pipeline.resolve_topics(keys, custom)
            except (KeyError, pipeline.InvalidTopic) as exc:
                self._json({"ok": False, "why": str(exc)})
                return

            self.server.bus.reset()

            session = _Session(self.server.bus, topics=topics, rounds=rounds, mock=mock)
            self.server.session = session
            session.start()
            self._json({"ok": True, "rounds": rounds, "mock": mock,
                        "topics": [t.key for t in topics]})
        elif path == "/stop":
            session = self.server.session
            if session is None:
                self._json({"ok": False, "why": "还没开始跑"})
                return
            session.request_stop()
            self._json({"ok": True})
        else:
            self.send_error(404)

    def _stream(self) -> None:
        """SSE。

        连接**不主动关闭**：EventSource 在连接断掉时会自动重连，如果我们在
        `done` 之后就断开，浏览器会重连、拿到历史、又立刻断开——变成无限重连循环。
        所以让连接一直挂着，靠前端的 seq 去重来处理重放。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = self.server.bus.subscribe()
        try:
            while True:
                event = q.get()
                chunk = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                     # 关页/刷新，正常现象
        finally:
            self.server.bus.unsubscribe(q)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, *, mock_default: bool = False) -> None:
        super().__init__(addr, handler)
        self.bus = _Bus()
        #: 当前（或最近一次）的 run。手册正文也挂在它身上——不再另存一份，
        #: 免得像刚才那样「worker 写了 session、路由读了 server」两边对不上。
        self.session: _Session | None = None
        self.mock_default = mock_default


# ---------------------------------------------------------------------------
# 4. 浏览器与单实例
# ---------------------------------------------------------------------------

def _existing_instance(port: int) -> bool:
    """这个端口上已经有一个我们自己起的启动器吗？

    认的是 /health 里的 _APP_ID，而不是「端口通不通」——不然会把别人占用的
    端口误判成自己人，然后把浏览器指到一个不相干的页面上。
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.6) as resp:
            return json.loads(resp.read()).get("app") == _APP_ID
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


def _open_browser(url: str) -> None:
    """尽力在**用户的**浏览器里打开这个 URL。

    WSL 里 `webbrowser` 模块基本指望不上——它找的是 Linux 侧的图形浏览器，多半
    没装。真正管用的是走 WSL 互操作去调 Windows 的 `explorer.exe`：它拿 URL 当
    参数时会用 Windows 的**默认浏览器**打开。所以顺序是 Windows 优先、webbrowser 兜底。
    """
    for argv in (["explorer.exe", url], ["cmd.exe", "/c", "start", "", url]):
        exe = shutil.which(argv[0])
        if not exe:
            continue
        try:
            # explorer.exe 成功时也常常返回非 0，所以不 check；超时兜住卡死的情况。
            subprocess.run(
                [exe, *argv[1:]],
                check=False, timeout=10,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except (OSError, subprocess.SubprocessError):
            continue
    try:
        webbrowser.open(url)
    except Exception:                                    # noqa: BLE001
        pass


def _build_page(server: _Server) -> bytes:
    boot = json.dumps(
        {
            "topics": [
                {"key": t.key, "title": t.section_title, "fixed": False} for t in TOPICS
            ] + [
                {"key": CLOSING_TOPIC.key, "title": CLOSING_TOPIC.section_title, "fixed": True}
            ],
            "defaultRounds": 3,
            "mockDefault": server.mock_default,
        },
        ensure_ascii=False,
    )
    return _PAGE.replace("__BOOT__", boot).encode("utf-8")


# ---------------------------------------------------------------------------
# 5. 页面（内嵌，不搞静态文件目录）
# ---------------------------------------------------------------------------

_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>健身饮食实战手册</title>
<style>
  :root {
    --bg: #f6f7f9;      --panel: #ffffff;   --fg: #1f2328;
    --muted: #59636e;   --border: #d8dee4;  --accent: #1f6feb;
    --accent-hi: #1a5fd0; --warn: #9a6700;  --bad: #b42318;
    --lin: #0969da;     --chen: #1a7f37;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 14px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  }
  .wrap { max-width: 960px; margin: 0 auto; padding: 20px 16px 32px; }
  header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 16px; }
  h1 { font-size: 17px; font-weight: 600; margin: 0; letter-spacing: .01em; white-space: nowrap; }
  #status { margin-left: auto; font-size: 13px; color: var(--muted);
            text-align: right; max-width: 70%; white-space: pre-wrap; }
  #status.warn { color: var(--warn); }
  #status.bad { color: var(--bad); }
  #status.live { color: var(--accent); }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
  .card + .card { margin-top: 12px; }
  .pad { padding: 12px 14px; }
  .topics { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-bottom: 10px; }
  .topics label { font-size: 13px; cursor: pointer; display: flex; align-items: center; gap: 5px; }
  .topics label.fixed { color: var(--muted); cursor: default; }
  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .row .spacer { flex: 1; }
  select, button {
    font: inherit; border: 1px solid var(--border); border-radius: 6px;
    padding: 7px 14px; background: var(--panel); color: var(--fg); cursor: pointer;
  }
  button:hover:not(:disabled) { background: var(--bg); }
  button:disabled { opacity: .45; cursor: default; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover:not(:disabled) { background: var(--accent-hi); }
  .hint { font-size: 12.5px; color: var(--muted); margin-top: 8px; }
  .hint.cost { color: var(--warn); }
  #progress { font-size: 13px; color: var(--accent); min-height: 20px; }
  #notice { font-size: 13px; white-space: pre-wrap; }
  #notice:empty { display: none; }
  #notice.warn { color: var(--warn); }
  #notice.bad { color: var(--bad); }
  .tabs { display: flex; gap: 4px; margin-bottom: -1px; }
  .tab {
    padding: 8px 16px; border: 1px solid var(--border); border-bottom: none;
    border-radius: 8px 8px 0 0; background: var(--bg); cursor: pointer; font-size: 13px;
  }
  .tab.on { background: var(--panel); font-weight: 600; }
  .pane { display: none; }
  .pane.on { display: block; }
  #chat { height: calc(100vh - 400px); min-height: 220px; overflow-y: auto; padding: 6px 14px 14px; }
  .topic-head {
    font-size: 13px; font-weight: 600; color: var(--muted); margin: 14px 0 4px;
    padding-top: 10px; border-top: 1px solid var(--border);
  }
  .topic-head:first-child { border-top: 0; margin-top: 4px; padding-top: 0; }
  .msg { padding: 8px 0 2px; white-space: pre-wrap; }
  .msg .src { display: block; font-size: 12px; letter-spacing: .04em; margin-bottom: 2px; font-weight: 600; }
  .msg.lin .src { color: var(--lin); }
  .msg.chen .src { color: var(--chen); }
  .empty { color: var(--muted); padding: 22px 0; text-align: center; }
  #hb { padding: 4px 18px 18px; max-height: calc(100vh - 340px); overflow-y: auto; }
  #hb .mockwarn {
    background: #fff8e5; border: 1px solid #f0d9a0; border-radius: 6px;
    padding: 8px 12px; margin: 10px 0; font-size: 13px; color: var(--warn);
  }
  #hb h1 { font-size: 20px; margin: 18px 0 8px; white-space: normal; }
  #hb h2 { font-size: 17px; margin: 20px 0 8px; padding-top: 8px; border-top: 1px solid var(--border); }
  #hb h3 { font-size: 14.5px; margin: 14px 0 6px; }
  #hb h4 { font-size: 13.5px; margin: 12px 0 4px; }
  #hb p { margin: 8px 0; }
  #hb ul, #hb ol { margin: 6px 0; padding-left: 22px; }
  #hb li { margin: 3px 0; }
  #hb ul.check { list-style: none; padding-left: 4px; }
  #hb ul.check input { margin-right: 7px; vertical-align: 1px; }
  #hb blockquote {
    margin: 8px 0; padding: 4px 12px; border-left: 3px solid var(--border); color: var(--muted);
  }
  #hb hr { border: 0; border-top: 1px solid var(--border); margin: 16px 0; }
  #hb pre { white-space: pre-wrap; word-wrap: break-word; font-size: 13px; }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
         background: var(--muted); margin-right: 6px; vertical-align: 1px; }
  /* 跑一轮要等模型，几十秒没有任何变化最容易让人以为卡死了。
     所以运行中的那个点必须**一直在动**——它是「页面还活着」最直接的信号。 */
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .18; } }
  .dot.live { background: var(--accent); animation: pulse 1.1s ease-in-out infinite; }
  .dot.warn { background: var(--warn); animation: pulse 1.1s ease-in-out infinite; }
  .dot.bad { background: var(--bad); }
  #runline { font-size: 13px; min-height: 21px; gap: 16px; }
  #activity { color: var(--accent); }
  /* 跳动的省略号只在**真的在干活**时出现。跑完还留着「完成···」会让人以为没结束，
     正好和这个功能想解决的问题相反。 */
  #activity.busy::after {
    content: ""; display: inline-block; width: 1em; text-align: left;
    animation: dots 1.4s steps(4, end) infinite;
  }
  @keyframes dots { 0% { content: ""; } 25% { content: "·"; }
                    50% { content: "··"; } 75% { content: "···"; } }
  #counter, #elapsed { color: var(--muted); font-variant-numeric: tabular-nums; }
  .mine .tag {
    font-size: 11px; background: #eef2f7; color: var(--muted);
    border-radius: 3px; padding: 1px 5px; margin-left: 4px;
  }
  .rm { border: 0; background: none; color: var(--muted); cursor: pointer;
        padding: 0 2px; font-size: 13px; line-height: 1; }
  .rm:hover { color: var(--bad); }
  #addform { display: none; margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border); }
  #addform.on { display: block; }
  #addform label { display: block; font-size: 12.5px; color: var(--muted); margin: 9px 0 3px; }
  #addform input, #addform textarea {
    width: 100%; font: inherit; color: var(--fg); background: var(--panel);
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 9px; outline: none;
  }
  #addform textarea { resize: vertical; min-height: 52px; }
  #addform input:focus, #addform textarea:focus { border-color: var(--accent); }
  #addform .err { color: var(--bad); font-size: 12.5px; margin-top: 6px; min-height: 17px; }
  #addform .row { margin-top: 4px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>健身饮食实战手册</h1>
    <span id="status"><span class="dot" id="dot"></span><span id="statustext">就绪</span></span>
  </header>

  <div class="card pad">
    <div class="topics" id="topics"></div>
    <div class="topics mine" id="mine"></div>
    <div class="row">
      <button id="toggleAdd">＋ 添加议题</button>
      <span class="spacer"></span>
      <span style="font-size:13px">每个议题</span>
      <select id="rounds">
        <option value="1">1 轮</option>
        <option value="2">2 轮</option>
        <option value="3" selected>3 轮</option>
        <option value="4">4 轮</option>
        <option value="5">5 轮</option>
        <option value="6">6 轮</option>
      </select>
      <label style="font-size:13px;display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="checkbox" id="mock"> 模拟模式（不花钱）
      </label>
      <button class="primary" id="start">开始</button>
      <button id="stop" disabled>停止</button>
    </div>

    <div id="addform">
      <label>标题 —— 会成为手册里这一节的标题</label>
      <input id="f-title" maxlength="60" placeholder="例如：六、外食党的早餐怎么吃">
      <label>要讨论什么（必填）—— 两位教练会围绕它吵</label>
      <textarea id="f-brief" maxlength="400"
        placeholder="例如：早上时间紧、只能买着吃的人，怎么在 5 分钟内凑够 30g 蛋白质？"></textarea>
      <label>本轮要收敛出什么（选填，留空用默认）</label>
      <input id="f-goal" maxlength="200" placeholder="留空则：收敛出读者可以直接照做的结论">
      <label>营养师的开场方案（选填，留空按议题描述自动生成）</label>
      <textarea id="f-opening" maxlength="1000"
        placeholder="留空即可。想指定他从哪个立场切入时再填。"></textarea>
      <div class="err" id="f-err"></div>
      <div class="row">
        <button class="primary" id="f-add">加入议题</button>
        <button id="f-cancel">取消</button>
        <span class="spacer"></span>
        <span class="hint" id="f-count"></span>
      </div>
    </div>

    <div class="hint" id="modehint"></div>
  </div>

  <div class="card pad">
    <div class="row" id="runline">
      <span id="activity">就绪</span>
      <span class="spacer"></span>
      <span id="counter"></span>
      <span id="elapsed"></span>
    </div>
    <div id="progress"></div>
    <div id="notice"></div>
  </div>

  <div class="tabs">
    <div class="tab on" id="tab-chat">对话</div>
    <div class="tab" id="tab-hb">手册</div>
  </div>
  <div class="card">
    <div class="pane on" id="pane-chat">
      <div id="chat"><div class="empty" id="empty">选好议题和轮数，点「开始」。</div></div>
    </div>
    <div class="pane" id="pane-hb">
      <div id="hb"><div class="empty">跑完才有手册。</div></div>
    </div>
  </div>
</div>

<script>
const BOOT = __BOOT__;
const SIDE_NAME = { lin: "营养师", chen: "教练" };
const $ = (id) => document.getElementById(id);
let lastSeq = -1;          // 去重：重连/重放时同一事件不会渲染两遍

// ---- 配置区 ----
const box = $("topics");
for (const t of BOOT.topics) {
  const lab = document.createElement("label");
  if (t.fixed) { lab.className = "fixed"; }
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.value = t.key;
  cb.checked = true;
  if (t.fixed) { cb.dataset.fixed = "1"; cb.disabled = true; }
  lab.appendChild(cb);
  lab.appendChild(document.createTextNode(t.title + (t.fixed ? "（自动追加）" : "")));
  box.appendChild(lab);
}
$("rounds").value = String(BOOT.defaultRounds);
$("mock").checked = !!BOOT.mockDefault;

function selectedTopics() {
  return [...box.querySelectorAll("input:checked")].map((cb) => cb.value);
}
function updateHint() {
  const on = $("mock").checked;
  $("modehint").className = on ? "hint" : "hint cost";
  $("modehint").textContent = on
    ? "模拟模式：不调用 DeepSeek、不联网、不花钱。产出是占位内容，每条回复几乎一样，仅用来验证流程能跑通。"
    : "真实模式：会调用 DeepSeek 生成内容，按用量计费。议题数和轮数越多越贵，建议先用模拟模式试。";
}

// ---- 自定义议题 ----
// localStorage 只用来「刷新别把刚写的东西弄丢」，不是数据源：真正跑的时候是随
// /start 一起把内容发给后端，后端不落盘，产物里只有跑出来的手册。
const STORE = "fitness-duo-custom-topics";
let customTopics = [];
try {
  const saved = JSON.parse(localStorage.getItem(STORE) || "[]");
  if (Array.isArray(saved)) {
    customTopics = saved.filter((t) => t && t.title && t.brief);
  }
} catch (e) { customTopics = []; }

function saveCustom() {
  try { localStorage.setItem(STORE, JSON.stringify(customTopics)); } catch (e) { /* 隐私模式等，忽略 */ }
}
function selectedCustom() {
  return [...$("mine").querySelectorAll("input:checked")]
    .map((cb) => customTopics[Number(cb.value)])
    .filter(Boolean);
}
function renderMine() {
  const host = $("mine");
  host.innerHTML = "";
  customTopics.forEach((t, i) => {
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = String(i); cb.checked = true;
    cb.dataset.custom = "1";
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(t.title));
    const tag = document.createElement("span");
    tag.className = "tag"; tag.textContent = "自定义";
    lab.appendChild(tag);
    const rm = document.createElement("button");
    rm.type = "button"; rm.className = "rm"; rm.textContent = "✕"; rm.title = "删掉这个议题";
    rm.onclick = (e) => {
      e.preventDefault();
      customTopics.splice(i, 1);
      saveCustom(); renderMine();
    };
    lab.appendChild(rm);
    host.appendChild(lab);
  });
}

function showAddForm(on) {
  $("addform").className = on ? "on" : "";
  $("toggleAdd").textContent = on ? "收起" : "＋ 添加议题";
  if (on) {
    $("f-title").focus();
  } else {
    ["f-title", "f-brief", "f-goal", "f-opening"].forEach((id) => { $(id).value = ""; });
    $("f-err").textContent = ""; $("f-count").textContent = "";
  }
}
$("toggleAdd").onclick = () => showAddForm(!$("addform").className);
$("f-cancel").onclick = () => showAddForm(false);

$("f-add").onclick = () => {
  const title = $("f-title").value.trim();
  const brief = $("f-brief").value.trim();
  if (!title) { $("f-err").textContent = "标题不能空。"; return; }
  if (!brief) { $("f-err").textContent = "「要讨论什么」不能空——不然两位教练没有可吵的东西。"; return; }
  // 长度上限与后端 pipeline.MAX_* 保持一致，前端先挡一道，省得跑到一半才报错
  customTopics.push({
    title: title, brief: brief,
    goal: $("f-goal").value.trim(),
    opening: $("f-opening").value.trim(),
  });
  saveCustom(); renderMine(); showAddForm(false);
};
["f-title", "f-brief"].forEach((id) => {
  $(id).addEventListener("input", () => {
    $("f-count").textContent =
      "标题 " + $("f-title").value.trim().length + "/60 · 议题描述 " + $("f-brief").value.trim().length + "/400";
  });
});
renderMine();

// ---- 状态与渲染 ----
function setStatus(text, tone) {
  $("statustext").textContent = text;
  $("status").className = tone || "";
  $("dot").className = "dot" + (tone ? " " + tone : "");
}
function setRunning(on) {
  $("start").disabled = on;
  $("stop").disabled = !on;
  $("rounds").disabled = on;
  $("mock").disabled = on;
  $("toggleAdd").disabled = on;
  ["f-title", "f-brief", "f-goal", "f-opening", "f-add", "f-cancel"]
    .forEach((id) => { $(id).disabled = on; });
  // 收尾议题是固定勾选、永远禁用的，别在停止时把它一起放开。
  box.querySelectorAll("input").forEach((cb) => {
    cb.disabled = on || cb.dataset.fixed === "1";
  });
  // 自定义议题的勾选框和删除按钮也一起锁住
  $("mine").querySelectorAll("input, button").forEach((el) => { el.disabled = on; });
}
function notice(text, tone) {
  $("notice").className = tone || "";
  $("notice").textContent = text || "";
}

// ---- 运行状态：让「没卡死」这件事看得见 ----
// 一次真实对谈要等模型几十秒，期间不会有任何新内容。如果页面只是静静停着，
// 用户第一反应就是「卡死了」。所以给三样一直在动的东西：
//   1. 脉冲的状态点（见 .dot.live 的动画）
//   2. 逐秒走的计时器
//   3. 发言进度 N/M —— 每个议题的发言条数是固定的，总数在开跑前就能算出来
let timerId = null, startedAt = 0, turnsDone = 0, turnsTotal = 0, nextSpeaker = "";

function tickElapsed() {
  if (!startedAt) { return; }
  const s = Math.floor((Date.now() - startedAt) / 1000);
  const m = Math.floor(s / 60);
  $("elapsed").textContent = "已用 " + (m ? m + " 分 " + (s % 60) + " 秒" : s + " 秒");
}
function setActivity(text, busy) {
  $("activity").textContent = text || "";
  // busy 才挂上跳动的省略号，空闲/完成时是静止的
  $("activity").className = busy ? "busy" : "";
}
function updateCounter() {
  $("counter").textContent = turnsTotal ? "发言 " + turnsDone + "/" + turnsTotal : "";
}
function beginRun(total, rounds) {
  turnsTotal = total * (1 + rounds * 2);   // 1 条开场 + 每轮「教练问、营养师答」
  turnsDone = 0;
  nextSpeaker = SIDE_NAME.lin;
  startedAt = Date.now();
  clearInterval(timerId);
  timerId = setInterval(tickElapsed, 1000);
  tickElapsed();
  updateCounter();
}
function endRun() {
  clearInterval(timerId);
  timerId = null;
  startedAt = 0;
}
function appendTurn(ev) {
  const empty = $("empty");
  if (empty) { empty.remove(); }
  const el = document.createElement("div");
  el.className = "msg " + ev.side;
  const s = document.createElement("span");
  s.className = "src";
  s.textContent = ev.speaker + " · " + (ev.side === "lin" ? "循证派" : "实战派");
  el.appendChild(s);
  el.appendChild(document.createTextNode(ev.text));
  const chat = $("chat");
  const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
  chat.appendChild(el);
  if (atBottom) { chat.scrollTop = chat.scrollHeight; }
}
function appendTopicHead(ev) {
  const empty = $("empty");
  if (empty) { empty.remove(); }
  const el = document.createElement("div");
  el.className = "topic-head";
  el.textContent = "[" + ev.index + "/" + ev.total + "] " + ev.title;
  $("chat").appendChild(el);
  $("chat").scrollTop = $("chat").scrollHeight;
}

// ---- 事件 ----
function handle(ev) {
  if (ev.seq <= lastSeq) { return; }
  lastSeq = ev.seq;

  if (ev.type === "run_start") {
    $("progress").textContent = "";
    notice("");
    beginRun(ev.total, ev.rounds);
    setActivity("准备中", true);
    setStatus("运行中", "live");
  } else if (ev.type === "topic_start") {
    appendTopicHead(ev);
    // 每个议题都是营养师先开口（init_chat 的开场白）
    nextSpeaker = SIDE_NAME.lin;
    setActivity("正在生成 " + nextSpeaker + " 的发言", true);
    $("progress").textContent = "[" + ev.index + "/" + ev.total + "] " + ev.title;
    setStatus("对谈中", "live");
  } else if (ev.type === "turn") {
    appendTurn(ev);
    turnsDone += 1;
    updateCounter();
    // 一轮里是「教练先说，营养师后答」，所以下一位总是对面的那位
    nextSpeaker = ev.side === "lin" ? SIDE_NAME.chen : SIDE_NAME.lin;
    setActivity("正在生成 " + nextSpeaker + " 的发言", true);
  } else if (ev.type === "section_start") {
    setActivity(ev.closing ? "正在整理分歧备忘与自检清单" : "正在把这一节整理成手册", true);
    setStatus("整理中", "live");
    $("progress").textContent += ev.closing ? "　→ 整理附录中" : "　→ 整理成章节中";
  } else if (ev.type === "notice") {
    notice(ev.text, ev.kind === "warning" ? "bad" : "");
    if (ev.kind === "warning") { setStatus("出错了", "bad"); }
    setActivity("");
  } else if (ev.type === "run_done") {
    $("progress").textContent = "✓ 完成：" + ev.handbook;
    setStatus("完成", "");
    setActivity("完成");
    endRun();
    loadHandbook();
  } else if (ev.type === "done") {
    setRunning(false);
    endRun();
    // 停止/出错时别把「正在生成…」留在页面上，那会一直骗人
    if ($("activity").textContent.indexOf("正在") === 0) { setActivity(""); }
  }
}

function connect() {
  const es = new EventSource("/events");
  es.onmessage = (e) => handle(JSON.parse(e.data));
}

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {})
  });
  return r.json();
}

$("start").onclick = async () => {
  const keys = selectedTopics();
  const mine = selectedCustom();
  if (!keys.length && !mine.length) {
    notice("至少要选一个议题，或者用「＋ 添加议题」自己加一个。", "bad");
    return;
  }

  $("chat").innerHTML = "";
  $("hb").innerHTML = '<div class="empty">跑完才有手册。</div>';
  lastSeq = -1;
  notice("");
  $("progress").textContent = "";
  $("counter").textContent = "";
  $("elapsed").textContent = "";
  showAddForm(false);
  setActivity("启动中", true);
  setRunning(true);
  pickTab("chat");

  const res = await post("/start", {
    topics: keys,
    custom: mine,
    rounds: parseInt($("rounds").value, 10),
    mock: $("mock").checked
  });
  if (!res.ok) {
    setRunning(false);
    setActivity("");
    notice(res.why || "启动失败", "bad");
  }
};

$("stop").onclick = async () => {
  setStatus("停止中…（会在当前这轮结束后收手）", "warn");
  await post("/stop");
};

// ---- 手册 ----
function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function inline(s) {
  return esc(s)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}
// 极简 markdown：只覆盖 digest.py 实际产出的语法。
// 先转义再做行内替换——正文是模型生成的，不转义就是给自己开 HTML 注入。
function mdToHtml(md) {
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push("</" + list + ">"); list = null; } };
  for (const raw of md.split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { closeList(); continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      closeList();
      out.push("<h" + m[1].length + ">" + inline(m[2]) + "</h" + m[1].length + ">");
    } else if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      closeList(); out.push("<hr>");
    } else if ((m = line.match(/^\s*[-*]\s+\[([ xX])\]\s+(.*)$/))) {
      if (list !== "ul") { closeList(); out.push('<ul class="check">'); list = "ul"; }
      const on = m[1].toLowerCase() === "x" ? " checked" : "";
      out.push('<li><input type="checkbox" disabled' + on + ">" + inline(m[2]) + "</li>");
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push("<li>" + inline(m[1]) + "</li>");
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push("<li>" + inline(m[1]) + "</li>");
    } else if ((m = line.match(/^>\s?(.*)$/))) {
      closeList(); out.push("<blockquote>" + inline(m[1]) + "</blockquote>");
    } else {
      closeList(); out.push("<p>" + inline(line) + "</p>");
    }
  }
  closeList();
  return out.join("\n");
}
let hbRaw = "";
async function loadHandbook() {
  const res = await (await fetch("/handbook")).json();
  hbRaw = res.markdown || "";
  if (!hbRaw.trim()) { return; }
  const warn = $("mock").checked
    ? '<div class="mockwarn">模拟模式下这份「手册」是占位内容：没有真实营养学结论，' +
      '而且整理环节同样走的是模拟后端，所以它没有标题层级——不是渲染坏了。</div>'
    : "";
  $("hb").innerHTML = warn + mdToHtml(hbRaw) +
    '<p style="margin-top:18px"><button id="dl">下载 .md</button> ' +
    '<button id="raw">查看原文</button></p>';
  $("dl").onclick = () => { window.location = "/handbook.md"; };
  $("raw").onclick = () => {
    $("hb").innerHTML = warn + '<pre>' + esc(hbRaw) + "</pre>" +
      '<p style="margin-top:18px"><button id="dl">下载 .md</button> ' +
      '<button id="raw2">看渲染版</button></p>';
    $("dl").onclick = () => { window.location = "/handbook.md"; };
    $("raw2").onclick = loadHandbook;
  };
}

// ---- Tab ----
function pickTab(which) {
  const chat = which === "chat";
  $("tab-chat").className = "tab" + (chat ? " on" : "");
  $("tab-hb").className = "tab" + (chat ? "" : " on");
  $("pane-chat").className = "pane" + (chat ? " on" : "");
  $("pane-hb").className = "pane" + (chat ? "" : " on");
}
$("tab-chat").onclick = () => pickTab("chat");
$("tab-hb").onclick = () => pickTab("hb");

$("mock").onchange = updateHint;
updateHint();
connect();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 6. 入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="健身饮食双教练对谈的网页启动器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  .venv/bin/python launcher.py            # 起服务，然后开浏览器\n"
            "  .venv/bin/python launcher.py --mock     # 默认勾上模拟模式，不花钱\n"
            "\n"
            "跑起来后在页面里选议题、点「开始」。想生成真实内容记得把「模拟模式」\n"
            "取消勾选，并在 .env 里配好 DEEPSEEK_API_KEY。\n"
        ),
    )
    parser.add_argument("--mock", action="store_true",
                        help="页面上的「模拟模式」默认勾上（不调用真实模型）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"端口，默认 {DEFAULT_PORT}（被占用就往后顺延）")
    parser.add_argument("--no-browser", action="store_true", help="不要试着自动开浏览器")
    args = parser.parse_args(argv)

    # 已经有一个在跑？把浏览器指过去就完事，**不要再起第二个**。
    # 双击图标两次、或者服务在后台开着又点了一次，都会走到这里。
    for port in range(args.port, args.port + 11):
        if _existing_instance(port):
            url = f"http://localhost:{port}/"
            print(f"启动器已经在跑了，直接把浏览器指过去：\n\n    {url}\n", flush=True)
            if not args.no_browser:
                _open_browser(url)
            return 0

    server = None
    for port in range(args.port, args.port + 11):
        try:
            server = _Server(("127.0.0.1", port), _Handler, mock_default=args.mock)
            break
        except OSError:
            continue
    if server is None:
        print(f"\n{args.port}–{args.port + 10} 都被占用了，用 --port 换一个。", file=sys.stderr)
        return 2

    port = server.server_address[1]
    url = f"http://localhost:{port}/"
    # flush 是必须的：stdout 不是 TTY 时（`| tee`、后台跑）Python 会块缓冲，
    # 不 flush 的话这段话会一直卡在缓冲区里，用户看不到 URL 就只能干等。
    print(f"\n{'=' * 72}\n健身饮食手册启动器已就绪：\n\n    {url}\n", flush=True)
    print("正在打开浏览器……没反应就手动复制上面这个地址。" if not args.no_browser
          else "（--no-browser：不自动开浏览器，手动复制上面这个地址。）", flush=True)
    if args.mock:
        print("（--mock：页面上的「模拟模式」默认已勾选，不花钱。）", flush=True)
    print(f"{'=' * 72}\n关掉这个窗口、或者按 Ctrl-C，都会停掉服务。", flush=True)

    if not args.no_browser:
        # 0.4 秒是给 serve_forever 让路。socket 在 _Server(...) 里就已经 bind+listen 了，
        # 所以浏览器这会儿连上来只是进 backlog 等一会儿，不会被拒。
        threading.Timer(0.4, _open_browser, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
