#!/usr/bin/env python3
"""liliclaw 云端工作台 KV 通道适配器（可选；用于 Vercel+Upstash 版远程工作台）。

用法:
  workbench.py check   # 门铃用：输出 wb:inbox 里待处理动作数（不消费），异常输出 0
  workbench.py pull    # 班次用：取出并清空 wb:inbox，逐行打印

凭证: <secrets_dir>/.wb-kv-creds，两行 KV_REST_API_URL=... / KV_REST_API_TOKEN=...
没有凭证或没配云端工作台时，check 恒输出 0，不影响其他部分。
"""
import os, sys, json, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
try:
    _cfg = json.load(open(os.path.join(ROOT, "liliclaw.json")))
except Exception:
    _cfg = {}
SECRETS = _cfg.get("secrets_dir") or ROOT


def creds():
    p = os.path.join(SECRETS, ".wb-kv-creds")
    if not os.path.exists(p):
        return None
    d = {}
    for ln in open(p, encoding="utf-8"):
        if "=" in ln:
            k, v = ln.strip().split("=", 1)
            d[k] = v
    if "KV_REST_API_URL" not in d or "KV_REST_API_TOKEN" not in d:
        return None
    return d["KV_REST_API_URL"], d["KV_REST_API_TOKEN"]


def kv(url, tok, cmd):
    req = urllib.request.Request(url, data=json.dumps(cmd).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + tok,
                                          "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    c = creds()
    if cmd == "check":
        if not c:
            print(0)
            return
        try:
            n = kv(c[0], c[1], ["LLEN", "wb:inbox"]).get("result") or 0
            print(int(n))
        except Exception:
            print(0)
    elif cmd == "pull":
        if not c:
            print("[workbench] 未配置 KV 凭证")
            return
        got = kv(c[0], c[1], ["LRANGE", "wb:inbox", 0, -1]).get("result") or []
        if got:
            kv(c[0], c[1], ["DEL", "wb:inbox"])
        for item in got:
            print(f"[工作台动作] {item}")
        if not got:
            print("[workbench] 无新动作")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
