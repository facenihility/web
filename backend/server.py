# -*- coding: utf-8 -*-
"""
工业智算网 · 通算协同调度平台 —— 后端服务（Python 标准库，零第三方依赖）

    python -m backend.run            # 或  python backend/server.py
    默认监听 http://127.0.0.1:8787

职责
    1. 托管前端页面（/ 为后端驱动版，/classic 为原始纯前端版）
    2. 服务端权威仿真：双轨（AI 协同调度 / 静态规则基线）按真实时间推进
    3. REST 控制与查询：场景 / 任务强度 / 速度 / 暂停 / 重置 / 指标 / 任务 / 日志
    4. SSE 实时推流：/api/stream（10Hz 状态快照，前端据此渲染）
    5. 批量实验与报告落盘：/api/experiment、/api/report、/api/runs
"""

from __future__ import annotations

import json
import os
import posixpath
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):        # 允许 `python backend/server.py` 直接运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend import engine, experiment, hardware, models
    from backend.hub import Hub, catalog
else:
    from . import engine, experiment, hardware, models
    from .hub import Hub, catalog

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC_DIR = os.path.join(HERE, "web")
APP_HTML = os.path.join(ROOT, "工业智算网-通算协同调度仿真平台-后端版.html")
CLASSIC_HTML = os.path.join(ROOT, "工业智算网-通算协同调度仿真平台.html")

MIME = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".ico": "image/x-icon", ".woff2": "font/woff2",
}

HUB = Hub()
HW_TRANSPORTS = None
EXP_STATE = {"running": False, "startedAt": None, "scene": None, "done": 0, "total": 0,
             "lastId": None, "error": None}
EXP_LOCK = threading.Lock()


# =========================================================================
# 批量实验（可异步）
# =========================================================================
def _run_experiment(body):
    with EXP_LOCK:
        if EXP_STATE["running"]:
            return None, "已有实验在运行中"
        EXP_STATE.update({"running": True, "startedAt": time.time(), "error": None,
                          "done": 0, "scene": None})
    try:
        scenes = body.get("scenes") or list(engine.SCENES.keys())
        EXP_STATE["total"] = len(scenes)
        res = experiment.run_batch(
            scenes=scenes,
            duration_ms=float(body.get("durationMs", 120000)),
            dt=float(body.get("dtMs", engine.STEP_MS)),
            seed=int(body.get("seed", engine.SEED)),
            label=body.get("label"),
        )
        EXP_STATE["lastId"] = res["id"]
        return res, None
    except Exception as exc:                      # noqa: BLE001
        EXP_STATE["error"] = "%s: %s" % (type(exc).__name__, exc)
        return None, EXP_STATE["error"]
    finally:
        with EXP_LOCK:
            EXP_STATE["running"] = False


# =========================================================================
# 请求处理
# =========================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "IndustrialAIComputeNet/1.0"
    protocol_version = "HTTP/1.1"
    quiet = False

    # ---------- 基础工具 ----------
    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Cache-Control", "no-store")

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.cors()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_text(self, text, status=200, ctype="text/plain; charset=utf-8", filename=None):
        data = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self.cors()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_file(self, path):
        if not os.path.isfile(path):
            self.send_json({"ok": False, "error": "文件不存在: %s" % os.path.basename(path)}, 404)
            return
        with open(path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(path)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.cors()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _safe_join(base, rel):
        rel = urllib.parse.unquote(rel)
        rel = posixpath.normpath(rel).lstrip("/")
        if rel.startswith("..") or os.path.isabs(rel):
            return None
        full = os.path.normpath(os.path.join(base, rel))
        if not full.startswith(os.path.normpath(base)):
            return None
        return full

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.cors()
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        q = urllib.parse.parse_qs(u.query)
        try:
            if p in ("/", "/index.html", "/app"):
                return self.send_file(APP_HTML if os.path.exists(APP_HTML) else CLASSIC_HTML)
            if p in ("/classic", "/legacy", "/原版"):
                return self.send_file(CLASSIC_HTML)
            if p == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.cors()
                return self.end_headers()
            if p.startswith("/static/"):
                full = self._safe_join(STATIC_DIR, p[len("/static/"):])
                if not full:
                    return self.send_json({"ok": False, "error": "非法路径"}, 400)
                return self.send_file(full)
            if p.startswith("/api/"):
                return self.api_get(p, q)
            return self.send_json({"ok": False, "error": "未找到: %s" % p}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:                  # noqa: BLE001
            self.send_json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        body = self.read_json()
        try:
            if p == "/api/control":
                action = body.get("action")
                if not action:
                    return self.send_json({"ok": False, "error": "缺少 action"}, 400)
                try:
                    st = HUB.control(action, body.get("value"))
                except ValueError as exc:
                    return self.send_json({"ok": False, "error": str(exc)}, 400)
                HUB._publish(force=True)
                return self.send_json({"ok": True, "action": action, "state": st})
            if p == "/api/config":
                for action in ("scene", "rate", "speed", "pause", "view", "seed"):
                    if action in body:
                        HUB.control(action, body[action])
                HUB._publish(force=True)
                return self.send_json({"ok": True, "state": HUB.state()})
            if p == "/api/reset":
                HUB.reset()
                return self.send_json({"ok": True, "state": HUB.state()})
            if p == "/api/experiment":
                async_flag = str(body.get("async", "")).lower() in ("1", "true", "yes")
                if async_flag:
                    with EXP_LOCK:
                        if EXP_STATE["running"]:
                            return self.send_json({"ok": False, "error": "已有实验在运行中"}, 409)
                    threading.Thread(target=_run_experiment, args=(body,), daemon=True).start()
                    return self.send_json({"ok": True, "status": "running"}, 202)
                res, err = _run_experiment(body)
                if err:
                    return self.send_json({"ok": False, "error": err}, 409)
                return self.send_json({"ok": True, "run": res})
            if p == "/api/model":
                mid = body.get("id") or body.get("model")
                try:
                    st = HUB.control("model", mid)
                except ValueError as exc:
                    return self.send_json({"ok": False, "error": str(exc)}, 400)
                HUB._publish(force=True)
                return self.send_json({"ok": True, "state": st,
                                       "model": models.MODELS[mid]})
            if p == "/api/nodes/weights":
                try:
                    st = HUB.set_weights(weights=body.get("weights"),
                                         locks=body.get("locks"),
                                         reset=bool(body.get("reset")))
                except ValueError as exc:
                    return self.send_json({"ok": False, "error": str(exc)}, 400)
                return self.send_json({"ok": True, "weights": st})
            # ---------------- 硬件在环 ----------------
            if p == "/api/hw/register":
                try:
                    r = HUB.hw.register(body)
                except ValueError as exc:
                    return self.send_json({"ok": False, "error": str(exc)}, 400)
                return self.send_json({"ok": True, **r})
            if p == "/api/hw/telemetry":
                r = HUB.hw.ingest(body, source="http")
                return self.send_json({"ok": r["rejected"] == 0, **r},
                                      200 if r["accepted"] or not r["rejected"] else 400)
            if p == "/api/hw/ack":
                try:
                    r = HUB.hw.ack(body.get("deviceId"), body.get("commandId"),
                                   bool(body.get("ok", True)), body.get("result"))
                except ValueError as exc:
                    return self.send_json({"ok": False, "error": str(exc)}, 400)
                return self.send_json({"ok": True, **r})
            if p == "/api/hw/mode":
                try:
                    mode = HUB.hw.set_mode(body.get("mode") or body.get("value"))
                except ValueError as exc:
                    return self.send_json({"ok": False, "error": str(exc)}, 400)
                if mode == "hil":
                    info = HUB.apply_hw()
                else:
                    for s in HUB.sims.values():
                        for n in s.nodes:
                            n.hwOverride = False
                        for l in s.links:
                            l.hwOverride = False
                    info = {"applied": False, "mode": mode}
                return self.send_json({"ok": True, "mode": mode, "apply": info})
            if p == "/api/hw/command":
                cmd = HUB.hw.push_command(body.get("deviceId"), body.get("type", "ping"),
                                          body.get("payload"), int(body.get("ttlMs", 8000)))
                if cmd is None:
                    return self.send_json({"ok": False, "error": "设备未注册"}, 404)
                return self.send_json({"ok": True, "command": cmd})
            if p == "/api/hw/broadcast":
                out = HUB.hw.broadcast(body.get("type", "config"), body.get("payload"),
                                       body.get("role"))
                return self.send_json({"ok": True, "sent": out})
            if p == "/api/hw/reset":
                with HUB.hw.lock:
                    HUB.hw.devices.clear()
                    HUB.hw.events.clear()
                return self.send_json({"ok": True, "cleared": True})
            return self.send_json({"ok": False, "error": "未找到: %s" % p}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:                  # noqa: BLE001
            self.send_json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    # ---------- GET 接口 ----------
    def api_get(self, p, q):
        def one(key, default=None):
            v = q.get(key)
            return v[0] if v else default

        if p == "/api/health":
            st = HUB.state()
            return self.send_json({
                "ok": True, "service": "工业智算网通算协同调度后端",
                "version": self.server_version, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "engine": {"stepMs": engine.STEP_MS, "tScale": engine.TSCALE,
                           "seed": engine.SEED, "scenes": list(engine.SCENES.keys())},
                "sim": st, "experiment": dict(EXP_STATE),
            })
        if p == "/api/catalog" or p == "/api/scenes":
            return self.send_json({"ok": True, "catalog": catalog()})
        if p == "/api/state":
            if one("lite") in ("1", "true"):
                return self.send_json({"ok": True, "state": HUB.state()})
            return self.send_json({"ok": True, "snapshot": HUB.snapshot()})
        if p == "/api/snapshot":
            return self.send_json(HUB.snapshot())
        if p == "/api/metrics":
            return self.send_json({"ok": True, "metrics": HUB.metrics()})
        if p == "/api/tasks":
            return self.send_json({"ok": True, "data": HUB.tasks(
                int(one("limit", 40)), one("mode", "ai"))})
        if p == "/api/logs":
            return self.send_json({"ok": True, "data": HUB.logs(
                int(one("limit", 12)), one("mode", "ai"))})
        if p == "/api/stream":
            if one("once") in ("1", "true"):
                return self.send_json({"ok": True, "snapshot": HUB.snapshot()})
            return self.sse()
        if p == "/api/report":
            fmt = one("format", "txt")
            if fmt == "json":
                return self.send_json({"ok": True, "run": experiment.latest_report("json")})
            txt = experiment.latest_report("txt")
            return self.send_text(txt, ctype="text/plain; charset=utf-8",
                                  filename="通算协同调度实验报告.txt" if one("download") else None)
        if p == "/api/runs":
            return self.send_json({"ok": True, "runs": experiment.list_runs(int(one("limit", 30)))})
        if p.startswith("/api/runs/"):
            rid = p[len("/api/runs/"):]
            run = experiment.get_run(rid)
            if run is None:
                return self.send_json({"ok": False, "error": "记录不存在"}, 404)
            return self.send_json({"ok": True, "run": run})
        if p == "/api/experiment":
            return self.send_json({"ok": True, "experiment": dict(EXP_STATE)})
        if p == "/api/models":
            return self.send_json({"ok": True, "models": models.catalog(),
                                   "default": models.DEFAULT})
        if p == "/api/model":
            m = models.MODELS[HUB.model_id]
            return self.send_json({"ok": True, "active": HUB.model_id, "model": m})
        if p == "/api/nodes/weights":
            st = HUB.node_weight_state()
            st["nodes"] = [{"id": n["id"], "name": n["name"], "tag": n["tag"], "cap": n["cap"]}
                           for n in engine.NODE_DEFS]
            return self.send_json({"ok": True, "state": st})
        # ---------------- 硬件在环 ----------------
        if p == "/api/hw/health":
            h = HUB.hw.health()
            if HW_TRANSPORTS:
                h["transports"] = HW_TRANSPORTS.info()
            return self.send_json({"ok": True, "hw": h})
        if p == "/api/hw/devices":
            snap = HUB.hw.snapshot(sims=HUB.sims)
            if HW_TRANSPORTS:
                snap["transports"] = HW_TRANSPORTS.info()
            return self.send_json({"ok": True, **snap})
        if p == "/api/hw/schema":
            return self.send_json({"ok": True, "schema": HUB.hw.schema()})
        if p == "/api/hw/commands":
            dev_id = one("deviceId") or one("device")
            if not dev_id:
                return self.send_json({"ok": False, "error": "缺少 deviceId"}, 400)
            wait = min(60.0, max(0.0, float(one("wait", 0) or 0)))
            cmds = HUB.hw.poll_commands(dev_id, wait_ms=wait * 1000.0)
            if cmds is None:
                return self.send_json({"ok": False, "error": "设备未注册"}, 404)
            return self.send_json({"ok": True, "deviceId": dev_id, "commands": cmds,
                                   "serverTimeMs": int(time.time() * 1000)})
        return self.send_json({"ok": False, "error": "未找到: %s" % p}, 404)

    # ---------- SSE ----------
    def sse(self):
        q = HUB.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.cors()
        self.end_headers()

        def chunk(payload):
            if not payload:
                return
            data = payload.encode("utf-8")
            self.wfile.write(("%X\r\n" % len(data)).encode("ascii"))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        try:
            chunk("retry: 2000\n\n")
            chunk("event: hello\ndata: %s\n\n" % json.dumps(
                {"ok": True, "scene": HUB.scene, "stepMs": engine.STEP_MS}, ensure_ascii=False))
            while True:
                try:
                    snap = q.get(timeout=15)
                except Exception:
                    chunk(": ping\n\n")
                    continue
                chunk("event: state\ndata: %s\n\n" % HUB.dumps(snap))
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            HUB.unsubscribe(q)
            self.close_connection = True


# =========================================================================
# 启动
# =========================================================================
class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """客户端断开（SSE 常见）时不要在控制台刷异常栈。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def serve(host="127.0.0.1", port=8787, quiet=False, scene="normal", speed=1.5,
          model_id=None, hw_mode="off", udp_port=None, tcp_port=None, hw_host=None):
    global HW_TRANSPORTS
    Handler.quiet = quiet
    HUB.control("scene", scene)
    HUB.control("speed", speed)
    if model_id:
        HUB.control("model", model_id)
    HUB.hw.set_mode(hw_mode)
    HUB.start()
    if udp_port is None:
        udp_port = int(os.environ.get("DSH_HW_UDP", "8790"))
    if tcp_port is None:
        tcp_port = int(os.environ.get("DSH_HW_TCP", "8791"))
    if udp_port or tcp_port:
        HW_TRANSPORTS = hardware.Transports(
            HUB.hw, host=hw_host or ("127.0.0.1" if host in ("127.0.0.1", "localhost") else "0.0.0.0"),
            udp_port=udp_port, tcp_port=tcp_port).start()
    httpd = QuietThreadingHTTPServer((host, port), Handler)
    return httpd


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    host, port, quiet = "127.0.0.1", 8787, False
    scene, speed, model_id, hw_mode = "normal", 1.5, None, "off"
    udp_port, tcp_port = None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-p", "--port") and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2; continue
        if a in ("-H", "--host") and i + 1 < len(argv):
            host = argv[i + 1]; i += 2; continue
        if a in ("-s", "--scene") and i + 1 < len(argv):
            scene = argv[i + 1]; i += 2; continue
        if a == "--speed" and i + 1 < len(argv):
            speed = float(argv[i + 1]); i += 2; continue
        if a in ("-m", "--model") and i + 1 < len(argv):
            model_id = argv[i + 1]; i += 2; continue
        if a == "--hw-mode" and i + 1 < len(argv):
            hw_mode = argv[i + 1]; i += 2; continue
        if a == "--udp-port" and i + 1 < len(argv):
            udp_port = int(argv[i + 1]); i += 2; continue
        if a == "--tcp-port" and i + 1 < len(argv):
            tcp_port = int(argv[i + 1]); i += 2; continue
        if a in ("-q", "--quiet"):
            quiet = True; i += 1; continue
        if a in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        i += 1

    httpd = serve(host, port, quiet, scene, speed, model_id, hw_mode, udp_port, tcp_port)
    real_port = httpd.server_address[1]
    base = "http://%s:%d" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, real_port)
    tr = HW_TRANSPORTS.info() if HW_TRANSPORTS else {}
    print("工业智算网 · 通算协同调度后端已启动")
    print("  前端页面 : %s/" % base)
    print("  原始页面 : %s/classic" % base)
    print("  健康检查 : %s/api/health" % base)
    print("  实时推流 : %s/api/stream" % base)
    print("  指标接口 : %s/api/metrics" % base)
    print("  模型接口 : %s/api/models" % base)
    print("  硬件在环 : %s/api/hw/schema   设备清单 %s/api/hw/devices" % (base, base))
    if tr:
        print("            UDP %s / TCP %s（遥测上行）；HTTP %s/api/hw/telemetry"
              % ("开启" if tr["udp"]["up"] else "关闭", "开启" if tr["tcp"]["up"] else "关闭", base))
    print("  Ctrl+C 停止")
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        httpd.shutdown()
        httpd.server_close()
        if HW_TRANSPORTS:
            HW_TRANSPORTS.stop()
        HUB.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
