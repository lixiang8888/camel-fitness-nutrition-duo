"""launcher 里**纯逻辑**部分的测试。

只测确定性、不需要人眼的部分：事件总线、单实例识别、页面注入。
「起服务 + 开浏览器 + 线程」那条整链路不做 pytest（脆且没必要），
靠手工端到端点一遍——见 README 的「网页版」一节。
"""

from __future__ import annotations

import http.client
import http.server
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

import launcher


# ---------------------------------------------------------------------------
# 事件总线
# ---------------------------------------------------------------------------

def test_bus_publish_reaches_subscriber_with_increasing_seq():
    bus = launcher._Bus()
    q = bus.subscribe()
    bus.publish({"type": "a"})
    bus.publish({"type": "b"})

    first = q.get_nowait()
    second = q.get_nowait()
    assert (first["type"], second["type"]) == ("a", "b")
    assert second["seq"] > first["seq"]


def test_bus_replays_history_to_late_subscriber():
    """刷新页面后要能补回已发生的内容，否则一按 F5 对话就空了。"""
    bus = launcher._Bus()
    bus.publish({"type": "a"})
    bus.publish({"type": "b"})

    q = bus.subscribe()
    assert [q.get_nowait()["type"] for _ in range(2)] == ["a", "b"]


def test_bus_reset_clears_history_but_keeps_seq():
    """reset 只清新一轮的历史，**不回退 seq**。

    回退了的话，前端按 seq 去重就会把新一轮的事件当成旧的丢掉——页面会一片空白。
    """
    bus = launcher._Bus()
    bus.publish({"type": "old"})
    seen = bus.subscribe().get_nowait()["seq"]

    bus.reset()
    q = bus.subscribe()
    assert q.empty(), "reset 之后不该再回放旧事件"

    bus.publish({"type": "new"})
    assert q.get_nowait()["seq"] > seen


def test_bus_unsubscribe_stops_delivery():
    bus = launcher._Bus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.publish({"type": "a"})
    assert q.empty()


# ---------------------------------------------------------------------------
# 单实例识别
# ---------------------------------------------------------------------------

@pytest.fixture
def live_server():
    """在随机端口起一个真的在 serve 的 _Server。"""
    server = launcher._Server(("127.0.0.1", 0), launcher._Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, port
    finally:
        server.shutdown()
        server.server_close()


def _get(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as resp:
        return resp.read().decode("utf-8")


def _post(port: int, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_existing_instance_false_when_port_free():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # socket 关掉了，端口是空的
    assert launcher._existing_instance(port) is False


def test_existing_instance_recognises_our_server(live_server):
    _, port = live_server
    assert launcher._existing_instance(port) is True


def test_existing_instance_ignores_foreign_server():
    """别人占了端口也不能被认成自己人——认的是 /health 里的身份，不是端口通不通。"""
    foreign = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    port = foreign.server_address[1]
    thread = threading.Thread(target=foreign.serve_forever, daemon=True)
    thread.start()
    try:
        assert launcher._existing_instance(port) is False
    finally:
        foreign.shutdown()
        foreign.server_close()


# ---------------------------------------------------------------------------
# 页面与路由
# ---------------------------------------------------------------------------

def test_health_reports_app_id(live_server):
    _, port = live_server
    assert json.loads(_get(port, "/health"))["app"] == launcher._APP_ID


def test_page_has_boot_injected(live_server):
    _, port = live_server
    html = _get(port, "/")

    assert "__BOOT__" not in html, "占位符没被替换掉"
    assert "健身饮食实战手册" in html
    boot = json.loads(html.split("const BOOT = ", 1)[1].split(";\n", 1)[0])
    keys = [t["key"] for t in boot["topics"]]
    assert keys[-1] == "closing"
    assert boot["topics"][-1]["fixed"] is True, "收尾议题要固定勾选"
    assert boot["mockDefault"] is False, "模拟模式默认关（用户指定）"


def test_page_boot_reflects_mock_flag():
    server = launcher._Server(("127.0.0.1", 0), launcher._Handler, mock_default=True)
    try:
        html = launcher._build_page(server).decode("utf-8")
        assert '"mockDefault": true' in html
    finally:
        server.server_close()


def test_handbook_empty_before_any_run(live_server):
    _, port = live_server
    assert json.loads(_get(port, "/handbook"))["markdown"] == ""


def test_handbook_download_404_before_any_run(live_server):
    _, port = live_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(port, "/handbook.md")
    assert exc.value.code == 404


def test_quick_empty_before_any_run(live_server):
    _, port = live_server
    assert json.loads(_get(port, "/quick"))["markdown"] == ""


def test_quick_download_404_before_any_run(live_server):
    """速查版是另一份文件，得有自己的下载口——它才是「一页纸」那份。"""
    _, port = live_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(port, "/quick.md")
    assert exc.value.code == 404


def test_handbook_and_quick_do_not_get_swapped(live_server):
    """两条路由只差一个开关，最容易出的错是把两份内容对调。

    网页上读到的和下载到的必须是同一份——不然「页面上是速查版、下下来是完整版」
    这种事没人会立刻发现。
    """
    from pathlib import Path

    server, port = live_server

    class _Done:
        finished = threading.Event()
        handbook = "# 完整手册\n正文甲"
        handbook_path = Path("/tmp/handbook-1-fat_loss.md")
        quick = "# 速查版\n正文乙"
        quick_path = Path("/tmp/quick-1-fat_loss.md")

    server.session = _Done()

    assert json.loads(_get(port, "/handbook"))["markdown"] == _Done.handbook
    assert json.loads(_get(port, "/quick"))["markdown"] == _Done.quick
    assert json.loads(_get(port, "/quick"))["path"].endswith("quick-1-fat_loss.md")

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/quick.md", timeout=3) as resp:
        assert resp.read().decode("utf-8") == _Done.quick
        assert "quick-1-fat_loss.md" in resp.headers["Content-Disposition"]


def test_unknown_path_404(live_server):
    _, port = live_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(port, "/nope")
    assert exc.value.code == 404


def test_page_has_topic_adder(live_server):
    """网页上要能自己加议题：添加表单和它的入口都得在。"""
    _, port = live_server
    html = _get(port, "/")
    for el in ('id="toggleAdd"', 'id="addform"', 'id="f-title"', 'id="f-brief"',
               'id="f-goal"', 'id="f-opening"', 'id="f-add"', 'id="mine"'):
        assert el in html, f"页面缺少 {el}"


def test_page_has_profile_form(live_server):
    """网页上要能填目标和身体情况：卡片、表单和它的入口都得在。"""
    _, port = live_server
    html = _get(port, "/")
    for el in ('id="profileCard"', 'id="toggleProfile"', 'id="pform"',
               'id="p-goals"', 'id="p-crowds"', 'id="p-goaltext"', 'id="p-crowdtext"',
               'id="p-sex"', 'id="p-age"', 'id="p-height"', 'id="p-weight"',
               'id="p-training"', 'id="p-constraints"', 'id="p-medical"', 'id="p-notes"',
               'id="p-bonus"', 'id="p-preview"', 'id="p-clear"', 'id="p-err"'):
        assert el in html, f"页面缺少 {el}"


def test_boot_carries_goal_and_crowd_presets(live_server):
    """预设表的单一真相源在 profile.py，页面只是渲染它。"""
    _, port = live_server
    boot = json.loads(_get(port, "/").split("const BOOT = ", 1)[1].split(";\n", 1)[0])

    goals = {g["key"]: g for g in boot["goals"]}
    assert {"fat_loss", "muscle_gain", "recomp", "maintain"} <= set(goals)
    # 用户点名要的两个方向
    assert "减脂" in goals["fat_loss"]["label"]
    assert "增肌" in goals["muscle_gain"]["label"]
    # 每个现实目标都要带一条专属议题，前端才有的可点
    for key, goal in goals.items():
        if key == "custom":
            assert goal["bonus"] is None
        else:
            assert goal["bonus"]["title"] and goal["bonus"]["brief"], f"{key} 的专属议题不完整"
            assert goal["bonus"]["opening"], "专属议题也要有开场白，否则对话没有种子"

    assert {"office", "student"} <= {c["key"] for c in boot["crowds"]}

    # 原有键的形状不能被这次改动动到
    assert [t["key"] for t in boot["topics"]][-1] == "closing"
    assert boot["defaultRounds"] == 3


def test_page_has_run_status(live_server):
    """跑起来要看得见「没卡死」：当前动作、逐秒计时、发言进度、剩余时间都得在。"""
    _, port = live_server
    html = _get(port, "/")
    for el in ('id="activity"', 'id="elapsed"', 'id="counter"', 'id="runline"', 'id="eta"'):
        assert el in html, f"页面缺少 {el}"
    assert "@keyframes pulse" in html, "运行中的状态点要有动画"
    # 没有剩余时间估算的话，「慢」和「卡死」在页面上长得一模一样
    assert "etaText" in html, "页面缺少剩余时间的估算函数"


def test_page_offers_both_versions(live_server):
    """手册页要同时交付两份：速查版在上、完整版在下，各有各的下载按钮。

    速查版不能藏进第二个 tab 或折叠块——它就是给「不想读完整版」的人准备的，
    多一次点击就等于没做。
    """
    _, port = live_server
    html = _get(port, "/")
    for s in ('"/quick"', '"/quick.md"', "下载速查版", "下载完整版"):
        assert s in html, f"页面缺少 {s}"
    assert "正在把整本手册压成一页速查" in html, "压缩这一步也要有状态提示"


# ---------------------------------------------------------------------------
# keep-alive 连接不能被没读完的请求体污染
# ---------------------------------------------------------------------------

def test_early_return_does_not_corrupt_the_keep_alive_connection(live_server):
    """请求体必须在**任何提前返回之前**读掉。

    protocol_version 是 HTTP/1.1，连接默认复用。没读完的 body 会留在 socket 上，
    和下一个请求的请求行粘在一起——服务端把「方法」解析成一段 JSON，回一个 501：

        501 Unsupported method ('{"topics":[...]}GET')

    于是这条连接上排队的 /handbook（跑完自动拉手册那一下）拿到的是垃圾：
    跑成功了，手册页却一片空白。
    """
    server, port = live_server

    # 造一个「永远在跑」的 session，让 /start 稳定地走「已经有一轮在跑了」那条提前返回
    class _NeverFinishes:
        finished = threading.Event()      # 永不 set
        handbook = ""
        handbook_path = None

        def request_stop(self) -> None:
            pass

    server.session = _NeverFinishes()

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        def call(method: str, path: str, payload: str | None = None):
            conn.request(method, path, payload, {"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")

        body = json.dumps({"topics": ["baseline"], "rounds": 1, "mock": True})

        status, text = call("POST", "/start", body)
        assert status == 200 and "已经有一轮在跑了" in text

        # 同一条连接上接着发，必须还能正常应答
        assert call("GET", "/health")[0] == 200

        # /stop 以前压根不读 body，每一次点「停止」都会留下残渣
        assert call("POST", "/stop", "{}")[0] == 200
        assert call("GET", "/health")[0] == 200

        # 未知路径的 POST 也要先把 body 读掉再回 404
        assert call("POST", "/nope", body)[0] == 404
        assert call("GET", "/health")[0] == 200
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /start 与自定义议题
# ---------------------------------------------------------------------------

def test_start_accepts_custom_topic(live_server):
    _, port = live_server
    res = _post(port, "/start", {
        "topics": [],
        "custom": [{"title": "六、外食党的早餐", "brief": "买着吃怎么凑蛋白质"}],
        "rounds": 1,
        "mock": True,
    })
    assert res["ok"] is True
    assert res["topics"] == ["custom-1", "closing"]


def test_start_puts_custom_after_builtin(live_server):
    _, port = live_server
    res = _post(port, "/start", {
        "topics": ["baseline"],
        "custom": [{"title": "甲", "brief": "甲议题"}],
        "rounds": 1,
        "mock": True,
    })
    assert res["topics"] == ["baseline", "custom-1", "closing"]


def test_start_keeps_closing_last_for_real_page_payload(live_server):
    """回归：网页发过来的 topics 里**包含** closing（那个固定勾选、禁用的框），
    自定义议题加进来之后收尾议题曾被挤到倒数第二。这里用页面真实的载荷形状测。
    """
    _, port = live_server
    res = _post(port, "/start", {
        "topics": ["baseline", "training_day", "goal", "reality", "supplements", "closing"],
        "custom": [{"title": "六、外食党的早餐", "brief": "买着吃怎么凑蛋白质"}],
        "rounds": 1,
        "mock": True,
    })
    assert res["ok"] is True
    assert res["topics"][-1] == "closing"
    assert res["topics"][-2] == "custom-1"
    assert res["topics"].count("closing") == 1


def test_start_rejects_custom_topic_without_brief(live_server):
    _, port = live_server
    res = _post(port, "/start", {
        "topics": ["baseline"],
        "custom": [{"title": "没写内容"}],
        "rounds": 1,
        "mock": True,
    })
    assert res["ok"] is False
    assert "要讨论什么" in res["why"]


def test_start_rejects_empty_selection(live_server):
    _, port = live_server
    res = _post(port, "/start", {"topics": [], "rounds": 1, "mock": True})
    assert res["ok"] is False
    assert "至少" in res["why"]


def test_start_rejects_unknown_builtin_key(live_server):
    _, port = live_server
    res = _post(port, "/start", {"topics": ["不存在的议题"], "rounds": 1, "mock": True})
    assert res["ok"] is False
    assert "未知议题" in res["why"]


# ---------------------------------------------------------------------------
# /start 与读者档案
# ---------------------------------------------------------------------------

def test_start_accepts_profile_and_echoes_summary(live_server):
    _, port = live_server
    res = _post(port, "/start", {
        "topics": ["baseline"],
        "rounds": 1,
        "mock": True,
        "profile": {
            "goal_key": "fat_loss", "sex": "男", "age": 32,
            "height_cm": 175, "weight_kg": 82,
        },
    })
    assert res["ok"] is True
    # 响应里原有的键一个都不能少
    assert res["topics"] == ["baseline", "closing"]
    assert res["rounds"] == 1
    assert res["mock"] is True
    assert "减脂减重" in res["profile"]
    assert "BMI 26.8" in res["profile"]


def test_start_without_profile_still_works(live_server):
    """没带 profile 键的老载荷要照常跑——响应里 profile 是空串，不是 None。"""
    _, port = live_server
    res = _post(port, "/start", {"topics": ["baseline"], "rounds": 1, "mock": True})
    assert res["ok"] is True
    assert res["profile"] == ""


def test_start_with_empty_profile_is_the_same_as_none(live_server):
    """网页会永远带一个 profile 键（哪怕全空），不能因此走出一条不同的路径。"""
    _, port = live_server
    res = _post(port, "/start", {
        "topics": ["baseline"], "rounds": 1, "mock": True,
        "profile": {"goal_key": "", "age": "", "notes": ""},
    })
    assert res["ok"] is True
    assert res["profile"] == ""


@pytest.mark.parametrize("profile, keyword", [
    ({"age": 999}, "年龄"),
    ({"height_cm": 10}, "身高"),
    ({"goal_key": "减脂"}, "未知"),
    ({"goal_key": "custom"}, "其他"),
])
def test_start_rejects_bad_profile(live_server, profile, keyword):
    _, port = live_server
    res = _post(port, "/start", {
        "topics": ["baseline"], "rounds": 1, "mock": True, "profile": profile,
    })
    assert res["ok"] is False
    assert keyword in res["why"]


def test_bad_profile_does_not_wipe_the_previous_run(live_server):
    """档案校验必须在 bus.reset() 之前。

    否则填错一个年龄，上一轮跑出来的整场对话就从页面上消失了——
    而这跟用户想改的那一个数字毫无关系。
    """
    server, port = live_server
    server.bus.publish({"type": "turn", "text": "上一轮的内容"})

    res = _post(port, "/start", {
        "topics": ["baseline"], "rounds": 1, "mock": True, "profile": {"age": 999},
    })
    assert res["ok"] is False

    # 历史还在：新订阅者一上来照样能补回上一轮
    assert server.bus.subscribe().get_nowait()["text"] == "上一轮的内容"
