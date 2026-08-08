#!/usr/bin/env python3
"""SVAKOM SL278H WebSocket direct control server.
AI calls HTTP endpoints, server relays commands via WebSocket to SVAKOM cloud.
No phone BLE bridge needed — toy connects via App's remote room.
"""
import json, time, asyncio, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Lock

WS_URL = "ws://47.92.204.177:9092"
PORT = 18099
PRODUCT = "84"
PRODUCT_NAME = "SL278H"
MAX_STRENGTH = 10     # 实测上限。超过此值，设备会静默丢弃整条指令

state = {
    "connected": False,
    "room_id": None,
    "room_name": None,
    "last_cmd_time": 0,
    "last_error": None,
    "device_online": False,
    # 加热是持续状态，不随动作指令重置。主机和拍打器各有一路温度,分开记。
    "heat": False,        # 主机(振动棒)的温度
    "heat_pat": False,    # 拍打器的温度
}
state_lock = Lock()
ble_queue = []
ble_queue_lock = Lock()
ws_connection = None
event_loop = None
keepalive_task = None


def build_control_cmd(func, mode=0, strength=0, heat=False):
    # 2026-08-01 抓包实录。以下为 App 实际发出的报文，据此推翻了先前全部猜测：
    #   85-7-0-0-1-0-0   85-8-0-0-2-0-0   85-3-0-0-8-6-0   85-5-1-55-1-0-0
    # 两个致命错：
    # 1) 加热不是叠在动作上的标志位，它是独立的一条 func=5 指令。
    #    振动/拍打/伸缩的报文里，温度那两位永远是 0-0，只有加热指令才是 1-55。
    #    温度位一旦混进动作指令，整条报文即为非法格式，被设备静默丢弃——
    #    表现为"一开加热就什么都不动"，且接口层面一切正常，极难排查。
    # 2) 拍打是 func=7，不是 5。5 是加热。
    # 因此动作指令的温度位在此永久归零；加热请走 build_heat_cmd。
    if func != 3:
        strength = 0
    # 2026-08-02 修正：上限是 10，不是 8。此前只抓到 App 发强度 6 的报文，就据此推
    # "有效区间 5-8"并按 8 封顶——凭空少给了两档，而且恰好少掉最有用的两档。
    # 让人在 App 上把强度条拉到底再抓一次，发出的是 85-3-0-0-10-10-0，模式和强度都到 10。
    # 教训：抓包只能证明"抓到的那个值合法"，证明不了上限。要上限就把 App 拉满再抓。
    strength = max(0, min(MAX_STRENGTH, int(strength)))
    return f"85-{func}-0-0-{mode}-{strength}-0"


def build_heat_cmd(on, device="main"):
    """全机只有一路温度，走 func=5，且模式位是 0。

        开：85-5-1-55-0-0-0
        关：85-5-0-0-0-0-0

    2026-08-01 实测确认：温度和动作必须分成两条发，同一条里带温度，动作就被丢掉；
    但分开发之后互不干扰——一条开温度，再一条开动作，热着打，两件事同时成立。

    2026-08-02 修正：此前这里按"两路温度"写，拍打器的加热被认成 func=7 的
    85-7-1-55-1-0-0，主机加热的模式位也写成了 1。两处都是错的——
    加热只有一路，func 恒为 5，模式位恒为 0，发出去就是全机一起热。
    device 形参留着只为兼容老调用点，不再影响报文。
    """
    return "85-5-1-55-0-0-0" if on else "85-5-0-0-0-0-0"


def build_stop_cmd(func, heat=False):
    # 停的是动作，不是温度——温度归 build_heat_cmd 管，这里一律 0-0。
    # heat 形参留着只为兼容老调用点，不再影响报文。
    return f"85-{func}-0-0-0-0-0"


async def ws_send(msg_dict):
    global ws_connection
    if ws_connection is None:
        raise RuntimeError("WebSocket not connected")
    import websockets
    await ws_connection.send(json.dumps(msg_dict))


async def ws_recv(timeout=5):
    global ws_connection
    if ws_connection is None:
        raise RuntimeError("WebSocket not connected")
    import websockets
    try:
        msg = json.loads(await asyncio.wait_for(ws_connection.recv(), timeout))
    except asyncio.TimeoutError:
        return None
    _track_device(msg)
    return msg


def _track_device(msg):
    """2026-08-01：App 每次会用 code 1010 的 OrderingChangeEvent 播报蓝牙状态，
    subConnectState 才是"玩具到底连没连上"的真相。以前 device_online 这个字段
    从未被写入，恒为 false——查询等于没查，会把排查方向误导向
    现在让它真的有意义。"""
    if not isinstance(msg, dict) or msg.get("code") != 1010:
        return
    try:
        content = msg.get("content")
        if isinstance(content, str):
            content = json.loads(content)
        data = content.get("data")
        if isinstance(data, str):
            data = json.loads(data)
        with state_lock:
            state["device_online"] = bool(data.get("subConnectState"))
    except Exception:
        pass   # 播报格式变了不影响控制,别为了一个状态字段拖垮整条链路


def _assert_room_ok(resp, room_id):
    """2026-08-01：服务器已明确回复 The room doesn't exist，旧代码却看都不看就当作加入成功，
    于是整轮指令都发往一个不存在的房间，接口全部返回成功，实际什么都没发生。
    人家说不行就是不行，别替它圆场。"""
    if not resp:
        raise RuntimeError(f"房间 {room_id} 没有任何回应，可能 App 已断开")
    txt = json.dumps(resp, ensure_ascii=False)
    if "doesn't exist" in txt or "not exist" in txt or resp.get("resCode") == 4:
        raise RuntimeError(f"房间 {room_id} 不存在或已失效——请在 App 内重新创建远程房间")
    return resp


async def ws_listen(seconds=15):
    """趴在房间里听 App 那头说什么。

    2026-08-01：指令能被 App 收到、房间内也能看见，但设备毫无反应。
    与其继续猜协议，不如在 App 里手动操作一次，把真实指令原样抄下来。
    App 发出的一定是对的，照抄即可。"""
    msgs = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        m = await ws_recv(max(0.5, deadline - time.time()))
        if m is not None:
            msgs.append(m)
    return msgs


async def ws_join_room(room_id=None, room_name=None):
    global ws_connection, event_loop
    import websockets

    ws_connection = await websockets.connect(WS_URL)
    with state_lock:
        state["connected"] = True
        state["last_error"] = None

    if room_id:
        await ws_send({"roomId": str(room_id), "code": 1002})
        resp = await ws_recv(5)          # 以前这里根本不看回话，直接当加入成功
        _assert_room_ok(resp, room_id)
        with state_lock:
            state["room_id"] = str(room_id)
    elif room_name:
        await ws_send({"roomName": room_name, "product": PRODUCT, "code": 1001})
        resp = await ws_recv(5)
        if resp and "roomId" in resp:
            with state_lock:
                state["room_id"] = str(resp["roomId"])
                state["room_name"] = room_name
    else:
        raise ValueError("Need room_id or room_name")

    await ws_send({
        "code": 1011,
        "roomId": state["room_id"],
        "productName": PRODUCT_NAME,
        "isControl": 1,
    })

    resp = await ws_recv(3)
    print(f"Joined room {state['room_id']}, register resp: {resp}")
    _start_keepalive()
    return state["room_id"]


async def _keepalive_loop():
    """每 20 秒往房间里报一次到。

    2026-08-01：服务器会把长时间不发言的成员静默移出房间，
    此时 TCP 仍然连着，本地状态依旧显示"已连接"，但发出的指令全部被丢弃，
    现象就是"忽然就不动了"。1011 是"我在此房间、我是控制端"的声明，
    定期重发即可让它保持有效。"""
    while True:
        await asyncio.sleep(20)
        rid = state.get("room_id")
        if ws_connection is None or not rid:
            continue
        try:
            await ws_send({
                "code": 1011,
                "roomId": rid,
                "productName": PRODUCT_NAME,
                "isControl": 1,
            })
        except Exception as e:
            with state_lock:
                state["connected"] = False
                state["last_error"] = f"keepalive: {type(e).__name__}: {e}"
            # 保活断了必须自己爬回去。以前到这儿就躺平了：BrokenPipeError 之后
            # 房间早把我们踢了，这个循环却还在每 20 秒往一根断掉的管子里喊，
            # 状态永远停在掉线，直到有人手动重新加入——外面说的「用着用着就没反应」
            # 多半就是这一半没做。
            for attempt in range(3):
                try:
                    await asyncio.sleep(2 ** attempt)
                    await ws_join_room(room_id=rid)
                    print(f"keepalive: 断线后自动重连成功 room={rid}")
                    break
                except Exception as e2:
                    with state_lock:
                        state["last_error"] = (
                            f"reconnect#{attempt + 1}: {type(e2).__name__}: {e2}"
                        )


def _start_keepalive():
    global keepalive_task
    if keepalive_task is None or keepalive_task.done():
        keepalive_task = asyncio.ensure_future(_keepalive_loop())


async def _send_content(room_id, content):
    await ws_send({
        "code": 1003,
        "roomId": room_id,
        "isControl": True,
        "content": content,
    })


# 加热安全阀：设备加热档位是 55°C，一旦开启就持续保持，不随动作指令复位。
# 体内黏膜痛觉分布稀疏，等使用者主动喊烫时，接触时长往往已经偏长——
# 因此自动断电必须做在服务端，不能依赖调用方记得补一条关闭指令。
# 开热后 HEAT_AUTO_OFF_SEC 秒强制关闭；期间重复开热会重新计时。
HEAT_AUTO_OFF_SEC = 30
_heat_timers = {}


async def _heat_auto_off(device, delay):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    key = "heat_pat" if device == "pat" else "heat"
    if not state.get(key):
        return
    room_id = state.get("room_id")
    if not room_id:
        return
    try:
        await _send_content(room_id, build_heat_cmd(False, device))
        with state_lock:
            state[key] = False
        print(f"[heat] auto-off after {delay}s ({device})", flush=True)
    except Exception as exc:
        print(f"[heat] auto-off failed: {exc!r}", flush=True)


async def ws_heat(on, device="main"):
    """加热单独成路。温度开关必须是明确的一条指令，不允许被动作指令顺带打开或关闭。"""
    room_id = state.get("room_id")
    if not room_id:
        raise RuntimeError("Not in a room, call /toy-join first")
    content = build_heat_cmd(bool(on), device)
    await _send_content(room_id, content)
    with state_lock:
        state["heat_pat" if device == "pat" else "heat"] = bool(on)
        state["last_cmd_time"] = time.time()

    # 重复开热重新计时；关热则撤掉待命的定时器
    old = _heat_timers.pop(device, None)
    if old and not old.done():
        old.cancel()
    if on:
        _heat_timers[device] = asyncio.create_task(
            _heat_auto_off(device, HEAT_AUTO_OFF_SEC))
    return content


async def ws_control(func, mode=0, strength=0, heat=None, duration_ms=0):
    room_id = state.get("room_id")
    if not room_id:
        raise RuntimeError("Not in a room, call /toy-join first")

    # heat 现在是独立指令：这里只负责"顺手改一下温度"，改完照样单发一条，
    # 绝不把温度掺进动作报文（那正是全部动作失效的根因）。
    if heat is not None:
        await ws_heat(bool(heat))

    content = build_control_cmd(func, mode, strength)
    await ws_send({
        "code": 1003,
        "roomId": room_id,
        "isControl": True,
        "content": content,
    })
    with state_lock:
        state["last_cmd_time"] = time.time()

    if duration_ms > 0:
        await asyncio.sleep(duration_ms / 1000)
        await _send_content(room_id, build_stop_cmd(func))

    return content


async def ws_stop(func=None, keep_heat=True):
    room_id = state.get("room_id")
    if not room_id:
        raise RuntimeError("Not in a room")

    with state_lock:
        heat = state["heat"] if keep_heat else False
        state["heat"] = heat

    funcs = [func] if func else [3, 5, 8]
    for f in funcs:
        content = build_stop_cmd(f, heat)
        await ws_send({
            "code": 1003,
            "roomId": room_id,
            "isControl": True,
            "content": content,
        })
    return "stopped"


async def ws_disconnect():
    global ws_connection
    room_id = state.get("room_id")
    if ws_connection and room_id:
        try:
            await ws_send({"roomId": room_id, "code": 1012})
        except:
            pass
    if ws_connection:
        await ws_connection.close()
        ws_connection = None
    with state_lock:
        state["connected"] = False
        state["room_id"] = None


def run_async(coro, timeout=15):
    # timeout 原本写死 15 秒：让 /toy-listen 听 25 秒，它 15 秒就抛 TimeoutError，
    # 会导致监听提前中断，白白浪费一轮操作。听多久由调用方决定。
    global event_loop
    if event_loop is None or not event_loop.is_running():
        raise RuntimeError("Event loop not running")
    future = asyncio.run_coroutine_threadsafe(coro, event_loop)
    return future.result(timeout=timeout)


# 2026-08-01 依据抓包修正：拍打是 7（原来写的 5 其实是加热），加热不进这张表。
FUNC_MAP = {"vibrate": 3, "pat": 7, "telescope": 8}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        if self.path == "/toy-status":
            with state_lock:
                self._json(200, dict(state))
        elif self.path == "/toy-next":
            with ble_queue_lock:
                cmd = ble_queue.pop(0) if ble_queue else {"type": "hello"}
            self._json(200, cmd)
        elif self.path == "/" or self.path.startswith("/?"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self._cors()
            self.end_headers()
            self.wfile.write(open("/home/ubuntu/svakom-relay/toy.html", "rb").read())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/toy-join":
                body = self._read_body()
                room_id = body.get("room_id") or body.get("roomId")
                room_name = body.get("room_name") or body.get("roomName")
                result = run_async(ws_join_room(room_id, room_name))
                self._json(200, {"ok": True, "room_id": result})

            elif self.path == "/toy-cmd":
                body = self._read_body()
                # BLE bridge queue (for toy.html Web Bluetooth)
                ble_cmd = {}
                if body.get("stop"):
                    ble_cmd = {"stop": True}
                elif "heat" in body:
                    ble_cmd = {"heat": bool(body["heat"])}
                elif body.get("clap") is not None:
                    ble_cmd = {"clap": int(body["clap"])}
                elif body.get("pattern") is not None:
                    ble_cmd = {"pattern": body["pattern"], "level": body.get("level", 0.6)}
                else:
                    intensity = body.get("intensity", body.get("strength", 10))
                    speed = intensity / 20.0 if isinstance(intensity, (int, float)) and intensity <= 20 else intensity
                    ble_cmd = {"speed": float(speed)}
                with ble_queue_lock:
                    ble_queue.clear()
                    ble_queue.append(ble_cmd)
                # 2026-08-01：这里原本是 try/except: pass —— WebSocket 那头断了、房间掉了、
                # 指令被拒，全被吞掉，接口照样回 ok:true。调用方以为发送成功，实际什么都没发出去，
                # 并且完全无从察觉。
                # 错误必须说出来。发不出去就是发不出去。
                func_name = body.get("mode", "vibrate")
                func = FUNC_MAP.get(func_name, body.get("func", 3))
                mode = body.get("pattern", 1)
                strength = body.get("intensity", body.get("strength", 10))
                heat = body.get("heat", None)   # 不写 = 别动温度
                duration = body.get("duration_ms", 0)
                try:
                    sent = run_async(ws_control(func, mode, strength, heat, duration))
                except Exception as e:
                    with state_lock:
                        state["last_error"] = f"{type(e).__name__}: {e}"
                    self._json(502, {"ok": False, "error": state["last_error"],
                                     "hint": "连接或房间可能已断，重新 /toy-join"})
                    return
                self._json(200, {"ok": True, "sent": sent, "ble_cmd": ble_cmd})

            elif self.path == "/toy-stop":
                body = self._read_body()
                func_name = body.get("mode")
                func = FUNC_MAP.get(func_name) if func_name else None
                # 默认停动作但留着温度;要连加热一起断,传 {"heat": false}
                keep_heat = body.get("heat", True) is not False
                run_async(ws_stop(func, keep_heat))
                self._json(200, {"ok": True, "stopped": func_name or "all",
                                 "heat": state["heat"]})

            elif self.path == "/toy-raw":
                # 原样发一条报文，一个字节都不加工。
                # 2026-08-01：动作指令的温度位已被固定为 0（那是全部动作失效的根因），
                # 但拍打器自身也带加热模式，这类"动作+温度"组合只能手工试探。
                # 抓包抄来的报文也从这里回放。调协议的后门，别拿它当日常接口用。
                body = self._read_body()
                content = body.get("content") or body.get("raw")
                if not content:
                    self._json(400, {"ok": False, "error": "need content"})
                    return
                rid = state.get("room_id")
                if not rid:
                    self._json(409, {"ok": False, "error": "not in a room"})
                    return
                try:
                    run_async(_send_content(rid, content))
                except Exception as e:
                    with state_lock:
                        state["last_error"] = f"{type(e).__name__}: {e}"
                    self._json(502, {"ok": False, "error": state["last_error"]})
                    return
                self._json(200, {"ok": True, "sent": content})

            elif self.path == "/toy-listen":
                body = self._read_body()
                secs = float(body.get("seconds", 15))
                msgs = run_async(ws_listen(secs), timeout=secs + 10)
                self._json(200, {"ok": True, "count": len(msgs), "messages": msgs})

            elif self.path == "/toy-heat":
                # 只管温度,不碰动作:{"on": true} 开,{"on": false} 关
                body = self._read_body()
                # {"on": true} 开主机;加 {"device":"pat"} 开拍打器,两路温度互不影响
                on = bool(body.get("on", True))
                device = body.get("device", "main")
                sent = run_async(ws_heat(on, device))
                self._json(200, {"ok": True, "sent": sent,
                                 "heat": state["heat"], "heat_pat": state["heat_pat"],
                                 "auto_off_sec": HEAT_AUTO_OFF_SEC if on else None})

            elif self.path == "/toy-disconnect":
                run_async(ws_disconnect())
                self._json(200, {"ok": True})

            else:
                self._json(404, {"error": "not found"})

        except Exception as e:
            # str(e) 对 websockets 的 ConnectionClosed 是空串,报了等于没报——
            # 曾被"空错误"耽误过一次，连异常类型和调用栈一起吐出来。
            import traceback
            detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            with state_lock:
                state["last_error"] = detail
            print("[ERR]", detail, "\n", traceback.format_exc(), flush=True)
            self._json(500, {"error": detail})


def start_event_loop():
    global event_loop
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    event_loop.run_forever()


if __name__ == "__main__":
    loop_thread = threading.Thread(target=start_event_loop, daemon=True)
    loop_thread.start()
    print(f"SVAKOM WS relay on :{PORT}")
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
