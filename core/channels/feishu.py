#!/usr/bin/env python3
"""liliclaw 飞书通道适配器（纯标准库，自包含）。

用法:
  feishu.py send "标题" ["正文"]   # 发消息给真人搭档
  feishu.py recv                   # 拉新消息并推进已读游标（班次里用）
  feishu.py peek                   # 只看不标已读
  feishu.py check                  # 门铃用：只输出新消息条数（不推进游标、不打印内容），异常输出 0
  feishu.py test                   # 发一条测试消息

凭证: <secrets_dir>/.lark_creds（secrets_dir 来自 liliclaw.json，缺省为框架根目录）
  单向: {"mode":"webhook","url":"https://open.feishu.cn/open-apis/bot/v2/hook/..."}
  双向: {"mode":"app","app_id":"cli_xxx","app_secret":"xxx","chat_id":"oc_xxx(可选)"}
状态: <secrets_dir>/.lark_state.json（自动维护 chat_id 与已读游标）
"""
import sys, os, json, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
try:
    _cfg = json.load(open(os.path.join(ROOT, "liliclaw.json")))
except Exception:
    _cfg = {}
SECRETS = os.environ.get("LILICLAW_LARK_DIR") or _cfg.get("secrets_dir") or ROOT
CREDS = os.path.join(SECRETS, ".lark_creds")
STATE = os.path.join(SECRETS, ".lark_state.json")
BASE = "https://open.feishu.cn/open-apis"
PREFIX = _cfg.get("report_prefix", "【数字员工】")


def _die(msg):
    sys.exit(f"[feishu] {msg}")


def _creds():
    if not os.path.exists(CREDS):
        _die(f"缺凭证 {CREDS}")
    return json.load(open(CREDS))


def _state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {}


def _save_state(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), ensure_ascii=False)
    os.replace(tmp, STATE)
    try:
        os.chmod(STATE, 0o600)
    except Exception:
        pass


def _http(method, url, token=None, body=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            _die(f"HTTP {e.code} {url}")
    except urllib.error.URLError as e:
        _die(f"网络失败: {e}")


def _token(c):
    st = _state()
    if st.get("token") and st.get("token_exp", 0) > time.time() + 60:
        return st["token"]
    out = _http("POST", f"{BASE}/auth/v3/tenant_access_token/internal",
                body={"app_id": c["app_id"], "app_secret": c["app_secret"]})
    if out.get("code") != 0:
        _die(f"取 token 失败: {out}")
    st["token"] = out["tenant_access_token"]
    st["token_exp"] = time.time() + int(out.get("expire", 7200))
    _save_state(st)
    return st["token"]


def _resolve_chat(c, token):
    if c.get("chat_id"):
        return c["chat_id"]
    st = _state()
    if st.get("chat_id"):
        return st["chat_id"]
    out = _http("GET", f"{BASE}/im/v1/chats?page_size=100", token=token)
    if out.get("code") != 0:
        _die(f"列群失败: {out}")
    items = out.get("data", {}).get("items", [])
    if not items:
        _die("机器人不在任何群里，先把它拉进一个群。")
    if len(items) > 1:
        names = ", ".join(f'{i.get("name","?")}={i["chat_id"]}' for i in items)
        _die(f"机器人在多个群，请在 .lark_creds 里指定 chat_id。候选: {names}")
    cid = items[0]["chat_id"]
    st["chat_id"] = cid
    _save_state(st)
    return cid


def _app_recv(c, advance=True):
    token = _token(c)
    cid = _resolve_chat(c, token)
    st = _state()
    last = int(st.get("last_ts", 0))
    url = (f"{BASE}/im/v1/messages?container_id_type=chat&container_id={cid}"
           f"&sort_type=ByCreateTimeDesc&page_size=20")
    out = _http("GET", url, token=token)
    if out.get("code") != 0:
        _die(f"拉取失败: {out}")
    items = out.get("data", {}).get("items", [])
    fresh, newest = [], last
    for m in items:
        ts = int(m.get("create_time", 0))
        newest = max(newest, ts)
        if ts <= last:
            continue
        if m.get("sender", {}).get("sender_type") == "app":
            continue
        if m.get("msg_type") != "text":
            fresh.append((ts, f"[{m.get('msg_type')} 非文本消息，暂不解析]"))
            continue
        try:
            txt = json.loads(m.get("body", {}).get("content", "{}")).get("text", "")
        except Exception:
            txt = m.get("body", {}).get("content", "")
        if txt.strip():
            fresh.append((ts, txt.strip()))
    fresh.sort()
    if advance and newest > last:
        st["last_ts"] = newest
        _save_state(st)
    return [t for _, t in fresh]


def send(title, body=""):
    c = _creds()
    text = PREFIX + title + (("\n\n" + body) if body else "")
    if c.get("mode") == "webhook":
        out = _http("POST", c["url"], body={"msg_type": "text", "content": {"text": text}})
        if out.get("code", 0) not in (0, None):
            _die(f"发送失败: {out}")
    elif c.get("mode") == "app":
        token = _token(c)
        cid = _resolve_chat(c, token)
        out = _http("POST", f"{BASE}/im/v1/messages?receive_id_type=chat_id", token=token,
                    body={"receive_id": cid, "msg_type": "text",
                          "content": json.dumps({"text": text}, ensure_ascii=False)})
        if out.get("code") != 0:
            _die(f"发送失败: {out}")
    else:
        _die(f"未知 mode: {c.get('mode')}")
    print("[feishu] 已发送 ✓")


def recv(advance=True):
    c = _creds()
    if c.get("mode") != "app":
        _die("webhook 单向模式收不了消息")
    msgs = _app_recv(c, advance=advance)
    if not msgs:
        print("[feishu] 无新消息")
        return
    for m in msgs:
        print(f"【真人搭档】{m}")


def history():
    """工作台「飞书通道」用：拉最近 30 条双向消息（不动已读游标），输出 JSON。任何失败输出 []。"""
    import datetime
    try:
        c = _creds()
        if c.get("mode") != "app":
            print("[]"); return
        token = _token(c)
        cid = _resolve_chat(c, token)
        url = (f"{BASE}/im/v1/messages?container_id_type=chat&container_id={cid}"
               f"&sort_type=ByCreateTimeDesc&page_size=30")
        out = _http("GET", url, token=token)
        items = out.get("data", {}).get("items", []) or []
        msgs = []
        for m in items:
            ts = int(m.get("create_time", 0)) / 1000
            role = "agent" if m.get("sender", {}).get("sender_type") == "app" else "boss"
            if m.get("msg_type") != "text":
                txt = f"[{m.get('msg_type')}]"
            else:
                try:
                    txt = json.loads(m.get("body", {}).get("content", "{}")).get("text", "")
                except Exception:
                    txt = ""
            if txt:
                msgs.append({"ts": datetime.datetime.fromtimestamp(ts).isoformat(timespec="minutes"),
                             "role": role, "text": txt})
        msgs.sort(key=lambda x: x["ts"])
        print(json.dumps(msgs, ensure_ascii=False))
    except SystemExit:
        print("[]")
    except Exception:
        print("[]")


def check():
    """门铃专用：只打印新消息数量，绝不推进游标、绝不抛异常。"""
    try:
        c = _creds()
        if c.get("mode") != "app":
            print(0)
            return
        print(len(_app_recv(c, advance=False)))
    except SystemExit:
        print(0)
    except Exception:
        print(0)


a = sys.argv[1:]
if not a:
    sys.exit(__doc__)
cmd = a[0]
if cmd == "send":
    send(a[1] if len(a) > 1 else "通知", a[2] if len(a) > 2 else "")
elif cmd == "recv":
    recv(True)
elif cmd == "peek":
    recv(False)
elif cmd == "check":
    check()
elif cmd == "history":
    history()
elif cmd == "test":
    send("通道测试", "liliclaw 飞书通道已接通。")
else:
    sys.exit(__doc__)
