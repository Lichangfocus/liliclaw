#!/usr/bin/env python3
"""liliclaw 本地站点服务器 —— 承载整个产品：安装向导、dashboard 配置、协同工作台。
零依赖（python 标准库），只绑 127.0.0.1。

  python3 site/server.py [port]     # 默认 8866，打开 http://localhost:8866
"""
import json, os, re, sys, glob, shutil, datetime, subprocess, platform, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "core", "bin"))
import engines as engines_mod  # noqa: E402
CFG_PATH = os.path.join(ROOT, "liliclaw.json")
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.liliclaw.bell.plist")
LABEL = "com.liliclaw.bell"

DEFAULT_EMP = {
    "name": "我的数字员工",
    "workspace": "instances/default",
    "timezone": "Asia/Shanghai",
    "secrets_dir": "",
    "proxy": "http://127.0.0.1:7890",
    "engines": ["claude-code"],
    "custom_engine": {"mode": "anthropic", "command": "", "base_url": "", "model": "", "proxy": "", "raw_args": []},
    "watchdog_minutes": 20,
    "fail_backoff_minutes": 15,
    "report_prefix": "【数字员工】",
    "shifts": [
        {"name": "morning", "at": "09:00", "goal": "早会：看数据和异常，定当日最重要的 1-3 件事并排进时间表"},
        {"name": "evening", "at": "21:00", "goal": "对外沟通与分发，准备需要真人处理的材料"},
        {"name": "wrapup", "at": "23:30", "goal": "复盘会：更新记忆、写当日日志、复盘三问、给明天排时间表"},
        {"name": "weekly", "at": "20:00", "weekday": 7, "goal": "周复盘：目标回溯、更新六维能力评分（涨分挂证据）、进化闭环自检——同类错误第二次出现=查规则为何没拦住"},
    ],
    "reflection": {"enabled": True, "idle_minutes": 5},
    "feishu_poll_seconds": 10,
    "events": {"feishu": True, "workbench_kv": False, "local_inbox": True},
    "report": {"feishu": False},
    "enabled": True,
}
DEFAULT_CFG = DEFAULT_EMP  # 兼容旧引用


EMP_KEYS = ["name", "workspace", "engines", "proxy", "shifts", "reflection",
            "feishu_poll_seconds", "events", "report", "report_prefix", "watchdog_minutes",
            "fail_backoff_minutes", "timezone", "enabled"]


def load_cfg():
    """读配置并自动迁移到 v2（employees 数组）。旧的单员工扁平结构原地包成一名员工。"""
    try:
        cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    except Exception:
        return None
    if "employees" not in cfg:
        emp = {k: cfg[k] for k in EMP_KEYS if k in cfg}
        emp["id"] = "default"
        emp.setdefault("enabled", True)
        emp["workspace"] = cfg.get("workspace") or cfg.get("instance") or "instances/default"
        cfg = {"version": 2, "secrets_dir": cfg.get("secrets_dir", ""), "employees": [emp]}
        try:
            save_cfg(cfg)
        except Exception:
            pass
    return cfg


def employees(cfg):
    return (cfg or {}).get("employees", [])


def get_emp(cfg, eid=None):
    emps = employees(cfg)
    if not emps:
        return None
    if eid:
        for e in emps:
            if e.get("id") == eid:
                return e
    return emps[0]


def save_cfg(cfg):
    tmp = CFG_PATH + ".tmp"
    json.dump(cfg, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_PATH)


def ws_dir(emp):
    """workspace = 员工干活的目录（可以是用户自己的项目仓库）。"""
    p = (emp or {}).get("workspace") or "instances/default"
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def inst_dir(emp):
    """记忆目录 = workspace/.liliclaw（隔离子目录，保证不碰用户自己的文件）。"""
    return os.path.join(ws_dir(emp), ".liliclaw")


def secrets_dir(cfg):
    return cfg.get("secrets_dir") or ROOT


def read(path, default=""):
    try:
        return open(path, encoding="utf-8").read()
    except Exception:
        return default


def sh(args, timeout=30):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


_claude_bin = None


def find_claude():
    global _claude_bin
    if _claude_bin is not None:
        return _claude_bin
    rc, out = sh(["bash", "-lc", "command -v claude"], 10)
    if rc == 0 and out.strip():
        _claude_bin = out.strip().splitlines()[-1]
        return _claude_bin
    g = sorted(glob.glob(os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude")))
    _claude_bin = g[-1] if g else ""
    return _claude_bin


def claude_token_status(cfg):
    sd = secrets_dir(cfg or {"secrets_dir": ""})
    if os.path.exists(os.path.join(sd, ".claude-oauth")):
        return "long", "长效 token（.claude-oauth）"
    rc, out = sh(["pgrep", "-f", "claude-code/.*claude"], 10)
    if rc == 0 and out.strip():
        return "harvest", "从运行中的 Claude 桌面 App 借（App 关了会失效；建议 claude setup-token 生成长效 token）"
    return "none", "没有：跑 claude setup-token 把 token 存进 .claude-oauth，或先打开 Claude 桌面 App"


def bell_loaded():
    rc, _ = sh(["launchctl", "list", LABEL], 10)
    return rc == 0


def engine_name(emp):
    return ((emp or {}).get("engines") or ["claude-code"])[0]


# 已知 agent CLI 候选池：按支持成熟度排序（rank 越小越推荐）。
# 原则：本产品只感知「装没装」，模型/端点/API key 一律在各 CLI 自己那层配置。
KERNELS = [
    {"key": "claude-code", "cli": "claude", "name": "Claude Code CLI", "rank": 1,
     "note": "稳定支持 · 订阅 token 直用；三方模型放 .claude-env"},
    {"key": "kimi", "cli": "kimi", "name": "Kimi Code CLI", "rank": 2,
     "note": "原生支持 · 无头 kimi -p，kimi login 登录后即用", "extra_path": "~/.kimi-code/bin/kimi"},
    {"key": "pi", "cli": "pi", "name": "pi coding agent", "rank": 3,
     "note": "实验性 · 无头 pi -p，模型在 pi 配置里"},
    {"key": "codex", "cli": "codex", "name": "OpenAI Codex CLI", "rank": 4,
     "note": "实验性 · 模型在 ~/.codex 里配"},
    {"key": "gemini", "cli": "gemini", "name": "Gemini CLI", "rank": 5,
     "note": "实验性 · 模型在 ~/.gemini 里配"},
    {"key": "qwen", "cli": "qwen", "name": "Qwen Code CLI", "rank": 6,
     "note": "实验性 · 模型在 qwen 配置里"},
    {"key": "opencode", "cli": "opencode", "name": "OpenCode CLI", "rank": 7,
     "note": "实验性 · opencode run 无头模式"},
    {"key": "aider", "cli": "aider", "name": "Aider CLI", "rank": 8,
     "note": "实验性 · aider --message 无头模式"},
]
_kernel_cache = None


def _scan_clis(clis):
    """在用户的真实 shell 环境里批量找命令。关键：macOS 用户的 PATH 大多配在 ~/.zshrc，
    所以先用用户默认 shell 的「登录 + 交互」模式扫（能读到 .zshrc），再用 bash -lc 补一遍，取并集。"""
    script = "for c in " + " ".join(clis) + '; do printf "%s=%s\\n" "$c" "$(command -v $c 2>/dev/null)"; done'
    valid = set(clis)
    found = {}
    shell = os.environ.get("SHELL") or "/bin/zsh"
    for cmd in ([shell, "-ilc", script], ["bash", "-lc", script]):
        rc, o = sh(cmd, 20)
        for ln in (o or "").splitlines():
            if "=" not in ln:
                continue
            c, p = ln.split("=", 1)
            c, p = c.strip(), p.strip()
            if c in valid and p and c not in found:
                found[c] = p
    return found


def detect_kernels(cfg, fresh=False):
    """扫描本机真实安装的 agent CLI，找到的都列出来并按 rank 给推荐。
    「扫描到」只代表装了；「能运行」由启用时的真实连通测试把关。"""
    global _kernel_cache
    if _kernel_cache is not None and not fresh:
        return _kernel_cache
    found = _scan_clis([k["cli"] for k in KERNELS])
    # PATH 之外的已知默认安装位兜底（比如 Kimi Code 装在 ~/.kimi-code/bin，只写进了 zshrc）
    for k in KERNELS:
        if k["cli"] not in found and k.get("extra_path"):
            p = os.path.expanduser(k["extra_path"])
            if os.access(p, os.X_OK):
                found[k["cli"]] = p
    out = []
    for k in sorted(KERNELS, key=lambda x: x["rank"]):
        path = found.get(k["cli"], "")
        if k["key"] == "claude-code" and not path:
            path = find_claude()
        item = {"key": k["key"], "name": k["name"], "note": k["note"], "found": bool(path), "path": path}
        if k["key"] == "claude-code" and path:
            kind, msg = claude_token_status(cfg)
            has_env = os.path.exists(os.path.join(secrets_dir(cfg or {"secrets_dir": ""}), ".claude-env"))
            item["auth_ok"] = kind != "none" or has_env
            item["auth"] = ("已配三方模型（.claude-env）" if has_env and kind == "none" else msg)
        out.append(item)
    rec = next((k["key"] for k in out if k["found"]), None)
    for k in out:
        k["recommended"] = k["key"] == rec
    _kernel_cache = out
    return out


def engine_real_test(key, timeout_s=90, proxy=None):
    """真实连通测试：让所选内核真跑一条最小指令。这是「扫描到」和「能运行」之间的那道门。"""
    cfg = load_cfg() or {}
    ctx = {"root": ROOT, "secrets_dir": cfg.get("secrets_dir") or ROOT, "proxy": proxy or ""}
    ok, note = engines_mod.available(key, ctx)
    if not ok:
        return False, note
    try:
        argv, env = engines_mod.build(key, ctx, "连通性测试：只回复两个字符：OK")
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, env=env)
        rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        rc, out = 1, "连通测试超时"
    except Exception as e:
        rc, out = 1, str(e)
    tail = "\n".join((out or "").strip().splitlines()[-4:])
    return rc == 0, (tail or f"exit={rc}")


_eng_cache = {"t": 0, "v": None}


def engine_available(emp, fresh=False):
    eng = engine_name(emp)
    if not fresh and _eng_cache["v"] and time.time() - _eng_cache["t"] < 60 and _eng_cache["v"][1] == eng:
        return _eng_cache["v"]
    cfg = load_cfg() or {}
    ctx = {"root": ROOT, "secrets_dir": cfg.get("secrets_dir") or ROOT, "proxy": (emp or {}).get("proxy") or ""}
    ok, note = engines_mod.available(eng, ctx)
    _eng_cache["v"] = (ok, eng, note)
    _eng_cache["t"] = time.time()
    return _eng_cache["v"]


def install(body):
    """创建一名数字员工：workspace 查重 → 内核真实连通门 → 建记忆 → 注册网关。"""
    cfg = load_cfg() or {"version": 2, "secrets_dir": "", "employees": []}
    emp = json.loads(json.dumps(DEFAULT_EMP))
    emp["name"] = (body.get("name") or emp["name"]).strip() or emp["name"]
    emp["report_prefix"] = f"【{emp['name']}】"
    base = re.sub(r"[^a-z0-9]+", "-", emp["name"].lower()).strip("-") or "emp"
    eid = base
    n = 1
    while any(e.get("id") == eid for e in employees(cfg)):
        n += 1
        eid = f"{base}-{n}"
    emp["id"] = eid
    ws = (body.get("workspace") or "").strip() or f"instances/{eid}"
    if not os.path.isabs(ws) and not ws.startswith("instances/"):
        ws = os.path.join("instances", ws)
    emp["workspace"] = ws
    ws_abs = ws if os.path.isabs(ws) else os.path.join(ROOT, ws)
    for e in employees(cfg):
        other = ws_dir(e)
        if os.path.abspath(other) == os.path.abspath(ws_abs):
            return {"ok": False, "error": f"这个 workspace 已被员工「{e.get('name')}」使用——两名员工共用一个 workspace 会互写记忆打架，换一个目录。"}
    try:
        if body.get("reflection_idle_minutes"):
            emp.setdefault("reflection", {"enabled": True})["idle_minutes"] = max(1, min(720, float(body["reflection_idle_minutes"])))
        if body.get("feishu_poll_seconds"):
            emp["feishu_poll_seconds"] = max(3, min(300, int(body["feishu_poll_seconds"])))
    except Exception:
        pass
    apply_engine(emp, body)
    # 启用门槛：所选内核必须真跑通一条最小指令，否则拒绝创建（不写任何东西）
    eng = engine_name(emp)
    ok, detail = engine_real_test(eng, proxy=emp.get("proxy", ""))
    if not ok:
        return {"ok": False, "error": f"内核 {eng} 连通测试没过，未启用。它自己报的错：\n{detail}\n先在该 CLI 里完成登录/配置（比如 pi 进入交互模式后 /login），再回来点启用。"}
    cfg["employees"] = employees(cfg) + [emp]
    save_cfg(cfg)
    # 建 workspace（记忆目录），并把启用时设定的必要内容写进去。
    # 若指定目录里已有 SOUL.md = 已有记忆的 workspace，直接接管、绝不覆盖。
    inst = inst_dir(emp)
    if not os.path.exists(os.path.join(inst, "SOUL.md")):
        if not os.path.exists(inst):
            shutil.copytree(os.path.join(ROOT, "core", "templates", "instance"), inst)
        else:
            for fn in os.listdir(os.path.join(ROOT, "core", "templates", "instance")):
                src = os.path.join(ROOT, "core", "templates", "instance", fn)
                dst = os.path.join(inst, fn)
                if not os.path.exists(dst):
                    (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)
        role = (body.get("role") or "").strip()
        goal = (body.get("goal") or "").strip()
        subs = {
            "{{NAME}}": emp["name"],
            "{{ROLE}}": (role if role.endswith(("。", "．", ".", "！", "!")) else role + "。") if role else "我的角色还没定，等老板一句话。",
            "{{GOAL}}": goal or "（老板还没定，第一班主动去问）",
            "{{DATE}}": datetime.datetime.now().strftime("%F %H:%M"),
        }
        for fn in ("SOUL.md", "STATE.md", "PLAN.md"):
            p = os.path.join(inst, fn)
            s = read(p)
            for k, v in subs.items():
                s = s.replace(k, v)
            open(p, "w", encoding="utf-8").write(s)
    jdir = os.path.join(inst, "journal")
    os.makedirs(jdir, exist_ok=True)
    open(os.path.join(jdir, "shifts.jsonl"), "a").close()
    open(os.path.join(jdir, ".needs-bootstrap"), "w").write("pending")
    # 预盖今天已过点的定时（避免创建瞬间补跑积压）
    now = datetime.datetime.now()
    d = os.path.join(jdir, ".shifts")
    os.makedirs(d, exist_ok=True)
    for s in emp.get("shifts", []):
        if "weekday" in s and now.isoweekday() != s["weekday"]:
            continue
        hh, mm = map(int, s["at"].split(":"))
        if now >= now.replace(hour=hh, minute=mm, second=0, microsecond=0):
            stamp = now.strftime("%G-W%V") if "weekday" in s else now.strftime("%F")
            open(os.path.join(d, f"{s['name']}-{stamp}"), "w").write("seeded-at-install")
    ensure_gateway()
    return {"ok": True, "id": emp["id"]}


def ensure_gateway():
    """注册/确保常驻网关在跑（全体员工共用一个守护，按各自锁与记忆互不干扰）。"""
    tpl = read(os.path.join(ROOT, "core", "launchd", "com.liliclaw.bell.plist.template"))
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    open(PLIST, "w").write(tpl.replace("{{ROOT}}", ROOT))
    sh(["launchctl", "unload", PLIST], 15)
    rc, out = sh(["launchctl", "load", PLIST], 15)
    return rc == 0


def apply_engine(emp, body):
    et = body.get("engine_type")
    if et in [k["key"] for k in KERNELS]:
        emp["engines"] = [et] if et == "claude-code" else [et, "claude-code"]
    if "proxy" in body:
        emp["proxy"] = (body.get("proxy") or "").strip()


def remove_employee(eid):
    """停用并移除一名员工（记忆目录完整保留，重建同 workspace 即可接管续命）。"""
    cfg = load_cfg()
    if not cfg:
        return {"ok": False, "error": "未启用"}
    before = len(employees(cfg))
    cfg["employees"] = [e for e in employees(cfg) if e.get("id") != eid]
    if len(cfg["employees"]) == before:
        return {"ok": False, "error": "没有这个员工"}
    save_cfg(cfg)
    if not cfg["employees"]:
        sh(["launchctl", "unload", PLIST], 15)
        if os.path.exists(PLIST):
            os.remove(PLIST)
    return {"ok": True}


def emp_status(emp):
    """员工卡片状态摘要：网关心跳 / 在岗 / 最近一次运行。"""
    jdir = os.path.join(inst_dir(emp), "journal")
    hb = os.path.join(jdir, ".bell-heartbeat")
    alive = round((datetime.datetime.now().timestamp() - os.path.getmtime(hb)) / 60, 1) if os.path.exists(hb) else None
    on_duty = False
    lk = os.path.join(jdir, ".shift-lock")
    if os.path.isdir(lk):
        try:
            os.kill(int(read(os.path.join(lk, "pid")).strip() or "0"), 0)
            on_duty = True
        except Exception:
            pass
    last = ""
    for ln in reversed(read(os.path.join(jdir, "shifts.jsonl")).splitlines()):
        if ln.strip():
            try:
                d = json.loads(ln)
                last = f"[{str(d.get('ts',''))[5:16].replace('T',' ')}] {d.get('summary','')[:60]}"
            except Exception:
                pass
            break
    return {"id": emp.get("id"), "name": emp.get("name"), "workspace": emp.get("workspace"),
            "engine": engine_name(emp), "enabled": emp.get("enabled", True),
            "bell_alive_min": alive, "on_duty": on_duty, "last_run": last,
            "has_lark": os.path.exists(os.path.join(inst_dir(emp), ".lark_creds")),
            "config": emp}


def setup_info():
    cfg = load_cfg()
    cb = find_claude()
    tok_kind, tok_msg = claude_token_status(cfg)
    return {
        "installed": bool(cfg) and bool(employees(cfg)),
        "bell_loaded": bell_loaded(),
        "checks": {
            "os": {"ok": platform.system() == "Darwin", "msg": f"{platform.system()} {platform.mac_ver()[0] or ''}（目前只支持 macOS 的 launchd，Linux 可用 cron 跑 bell.py）"},
            "python": {"ok": True, "msg": f"python3 {platform.python_version()}"},
            "claude_cli": {"ok": bool(cb), "msg": cb or "没找到 claude 命令（装 Claude Code CLI，或用检测到的其他 CLI 内核）"},
            "claude_token": {"ok": tok_kind != "none", "kind": tok_kind, "msg": tok_msg},
        },
        "kernels": detect_kernels(cfg),
        "defaults": DEFAULT_EMP,
        "employees": [emp_status(e) for e in employees(cfg)],
    }


# ---------- 工作台数据（沿用旧工作台的数据模型：WB / COLLAB_LIVE / LOOPLOG / NOW）----------

def json_block(md):
    m = re.search(r"```json\s*\n(.*?)\n```", md, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


def parse_experience(md):
    """从 EXPERIENCE.md「活跃规则」节抽规则条目。"""
    out = []
    sec = re.split(r"^## ", md, flags=re.M)
    for s in sec:
        if s.startswith("活跃规则"):
            for m in re.finditer(r"^###\s+(\S+)[ 	]*[🟢🟡]?\s*(.+)$", s, re.M):
                out.append({"id": m.group(1), "h": m.group(2).strip(), "s": ""})
            for m in re.finditer(r"^[-*]\s+(?:\*\*)?(L?\d+[^：:*]*)(?:\*\*)?[：:]\s*(.+)$", s, re.M):
                out.append({"id": m.group(1).strip(), "h": m.group(2).strip(), "s": ""})
    return out


def state_payload(eid=None):
    cfg = load_cfg()
    now = datetime.datetime.now()
    emp = get_emp(cfg, eid)
    if not cfg or not emp:
        return {"empty": True}
    inst = inst_dir(emp)
    jdir = os.path.join(inst, "journal")
    today = now.strftime("%F")

    shifts_log = []
    for ln in read(os.path.join(jdir, "shifts.jsonl")).splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            shifts_log.append(json.loads(ln))
        except Exception:
            pass

    # 对话回路：它对留言/调教指令的答复
    replies = []
    for ln in read(os.path.join(jdir, "replies.jsonl")).splitlines()[-20:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            replies.append(json.loads(ln))
        except Exception:
            pass
    replies.reverse()

    # 对话侧边栏：老板消息（发送时留档）+ 它的回复，按时间合并成一条线程
    chat = []
    for ln in read(os.path.join(jdir, "chat.jsonl")).splitlines()[-60:]:
        try:
            d = json.loads(ln)
            if d.get("action") == "chat-new":
                chat.append({"ts": d.get("ts", ""), "role": "divider", "text": "— 新对话 —"})
                continue
            chat.append({"ts": d.get("ts", ""), "role": "boss",
                         "text": d.get("note", ""), "config": d.get("action") == "config"})
        except Exception:
            pass
    for r in replies:
        chat.append({"ts": r.get("ts", ""), "role": "agent", "text": r.get("text", ""), "re": r.get("re", "")})
    chat.sort(key=lambda x: str(x.get("ts", "")))
    for _i in range(len(chat) - 1, -1, -1):
        if chat[_i].get("role") == "divider":
            chat = chat[_i:]
            break
    chat = chat[-40:]

    # 运行健康（新鲜度取最旧层的数据源）：失败退避中要在页面示警
    backoff_min = None
    lf = os.path.join(jdir, ".last-fail")
    if os.path.exists(lf):
        try:
            left = emp.get("fail_backoff_minutes", 15) - (now.timestamp() - float(read(lf).strip())) / 60
            if left > 0:
                backoff_min = round(left, 1)
        except Exception:
            pass

    # NOW：当班状态
    lock = os.path.join(jdir, ".shift-lock")
    NOW = {}
    if os.path.isdir(lock):
        try:
            os.kill(int(read(os.path.join(lock, "pid")).strip() or "0"), 0)
            nm = read(os.path.join(lock, "name")).strip() or "event"
            gl = read(os.path.join(lock, "goal")).strip()
            NOW = {"doing": f"运行中 · {nm}" + (f"：{gl}" if gl else ""),
                   "since": datetime.datetime.fromtimestamp(os.path.getmtime(lock)).strftime("%H:%M")}
        except Exception:
            NOW = {}

    # 门铃心跳 → 新鲜度守卫的数据源
    hb = os.path.join(jdir, ".bell-heartbeat")
    log_last = datetime.datetime.fromtimestamp(os.path.getmtime(hb)).isoformat() if os.path.exists(hb) else ""
    bell_min = round((now.timestamp() - os.path.getmtime(hb)) / 60, 1) if os.path.exists(hb) else None

    tasks = (json_block(read(os.path.join(inst, "TASKS.md"))) or {}).get("tasks", [])
    human = [t for t in tasks if t.get("owner") not in (None, "", "agent") and t.get("status") != "done"]
    COLLAB_LIVE = [{"id": t.get("id", ""), "title": t.get("title", ""),
                    "sub": t.get("note", "") or f"优先级 {t.get('priority','')}",
                    "accept": t.get("accept", ""), "status": "open"} for t in human]

    kind_map = {"wrapup": "inner", "weekly": "inner", "smoke": "watch"}
    LOOPLOG = [{"t": (x.get("ts", "")[:16]).replace("T", " "),
                "h": x.get("summary", "") + (("　→ " + x["next"]) if x.get("next") else ""),
                "shift": x.get("shift", ""),
                "k": kind_map.get(x.get("shift", ""), "work")} for x in reversed(shifts_log[-40:])]

    # 班表 → 日程
    sched_today, markers = [], os.path.join(jdir, ".shifts")
    done_names = set()
    for s in emp.get("shifts", []):
        if "weekday" in s and now.isoweekday() != s["weekday"]:
            continue
        stamp = now.strftime("%G-W%V") if "weekday" in s else today
        done = os.path.exists(os.path.join(markers, f"{s['name']}-{stamp}"))
        if done:
            done_names.add(s["name"])
        sched_today.append({**s, "done": done})

    days, week = {}, []
    for i in range(-14, 15):
        dd = now + datetime.timedelta(days=i)
        d = dd.strftime("%F")
        wd = "周" + "一二三四五六日"[dd.isoweekday() - 1]
        dshifts = [x for x in shifts_log if str(x.get("ts", "")).startswith(d)]
        done_items = [{"time": str(x.get("ts", ""))[11:16], "t": f"运行·{x.get('shift','')}",
                       "r": x.get("summary", "")} for x in dshifts]
        todo_items = []
        if i >= 0:
            for t in tasks:
                at = (t.get("at") or "")
                if at.startswith(d) and t.get("status") in ("todo", "waiting"):
                    todo_items.append({"tag": at[11:16] or "—",
                                       "t": ("📚 " if t.get("kind") == "learn" else "") + t.get("title", ""),
                                       "s": t.get("note", ""), "wait": t.get("status") == "waiting"})
            for s0 in emp.get("shifts", []):
                if "weekday" in s0 and dd.isoweekday() != s0["weekday"]:
                    continue
                if i == 0 and s0["name"] in done_names:
                    continue
                if i == 0 and s0["at"] <= now.strftime("%H:%M"):
                    continue
                todo_items.append({"tag": s0["at"], "t": f"定时·{s0['name']}", "s": s0.get("goal", ""), "wait": False})
            todo_items.sort(key=lambda x: x.get("tag", ""))
        th = (f"{len(dshifts)} 次运行" if dshifts else ("—" if i <= 0 else (f"{len(todo_items)} 项" if todo_items else "—")))
        week.append({"date": d, "wd": wd, "dd": d[5:], "th": th})
        days[d] = {"done": done_items, "todo": todo_items,
                   "note": "过去看已运行（结果先行），未来看排期（时间表任务 + 定时计划）。"}

    rhythm = []
    for s in sched_today:
        hh = int(s["at"].split(":")[0])
        rhythm.append({"lab": f"定时·{s['name']}", "t": s["at"] + (" · 每周日" if "weekday" in s else " · 每天"),
                       "d": s.get("goal", ""), "h": [hh, hh + 1]})

    abilities = json_block(read(os.path.join(inst, "ABILITIES.md"))) or {}
    experience = parse_experience(read(os.path.join(inst, "EXPERIENCE.md")))
    metrics = (json_block(read(os.path.join(inst, "STATE.md"))) or {}).get("metrics", [])
    plan = json_block(read(os.path.join(inst, "PLAN.md"))) or {}

    # 「接下来」时间轴：未来激活点（主动性的外显）——时间表任务 + 下一个定时点
    upcoming = []
    for t in tasks:
        at = (t.get("at") or "").strip()
        if at and t.get("status") in ("todo", "waiting") and at > now.strftime("%Y-%m-%dT%H:%M"):
            upcoming.append({"at": at.replace("T", " ")[5:16], "title": t.get("title", ""),
                             "kind": ("学习" if t.get("kind") == "learn" else ("等外部·复查" if t.get("status") == "waiting" else "任务")),
                             "owner": t.get("owner", "agent")})
    for s0 in emp.get("shifts", []):
        hh, mm = map(int, s0["at"].split(":"))
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if "weekday" in s0:
            d = (s0["weekday"] - now.isoweekday()) % 7
            if d == 0 and nxt <= now:
                d = 7
            nxt += datetime.timedelta(days=d)
        elif nxt <= now:
            nxt += datetime.timedelta(days=1)
        upcoming.append({"at": nxt.strftime("%m-%d %H:%M"), "title": f"定时·{s0['name']}：{s0.get('goal','')[:24]}", "kind": "定时", "owner": "agent"})
    upcoming.sort(key=lambda x: x["at"])
    upcoming = upcoming[:8]

    learn_tasks = [t for t in tasks if t.get("kind") == "learn"]

    soul = read(os.path.join(inst, "SOUL.md"))
    paras = [p.strip() for p in re.sub(r"^>.*$", "", soul, flags=re.M).split("\n\n")
             if p.strip() and not p.strip().startswith("#")]
    lead = paras[0] if paras else "账本里的 SOUL.md 还没写。"

    state_md = read(os.path.join(inst, "STATE.md"))
    ns_m = re.search(r"北极星[：:]\s*(.+)", state_md)
    ms_m = re.search(r"里程碑[：:]\s*(.+)", state_md)

    open_tasks = [t for t in tasks if t.get("status") != "done"]
    eng_ok, eng, _ = engine_available(emp)
    last = shifts_log[-1] if shifts_log else None

    WB = {
        "overviewLead": lead,
        "northStar": {"label": "北极星", "value": ns_m.group(1).strip() if ns_m else "—",
                      "goalTitle": "当前里程碑", "goalText": ms_m.group(1).strip() if ms_m else "（STATE.md 里还没写）",
                      "milestone": f"今天定时任务 {len(done_names)}/{len(sched_today)}"},
        "kpis": [
            {"k": "调度网关", "v": ("在线 · 实时监听" if (bell_min is not None and bell_min <= 1) else (f"{bell_min} 分钟前心跳" if bell_min is not None else "还没启动")), "accent": bell_min is not None and bell_min <= 5, "small": True},
            {"k": "内核", "v": ("✅ " if eng_ok else "❌ ") + eng, "small": True},
            {"k": "任务队列", "v": f"{len(open_tasks)} 开 · {len(human)} 件需要你", "small": True},
        ],
        "todayDate": today,
        "todaySummary": (f"最近一次运行（{last.get('shift','')}）：" + last.get("summary", "")) if last else "还没有运行记录。",
        "week": week, "days": days, "rhythm": rhythm,
        "experience": experience,
        "abilities": abilities,
        "metrics": metrics,
        "plan": plan,
        "upcoming": upcoming,
        "learnTasks": learn_tasks,
        "files": [
            {"n": "我是谁", "p": "SOUL.md"}, {"n": "铁律与决策边界", "p": "CONSTITUTION.md"},
            {"n": "目标与现状", "p": "STATE.md"}, {"n": "任务队列", "p": "TASKS.md"},
            {"n": "经验库", "p": "EXPERIENCE.md"}, {"n": "六维能力", "p": "ABILITIES.md"},
            {"n": "日志与交接", "p": "journal/"}, {"n": "接班协议", "p": "core/prompts/shift.md"},
        ],
    }
    return {"WB": WB, "COLLAB_LIVE": COLLAB_LIVE, "LOOPLOG": LOOPLOG, "NOW": NOW, "REPLIES": replies,
            "CHAT": chat, "BACKOFF_MIN": backoff_min, "DISPATCH": [],
            "SNAPSHOT_ISO": now.isoformat(), "LOG_LAST_ISO": log_last, "TODAY": today,
            "BELL_MIN": bell_min, "NAME": emp.get("name", "liliclaw"), "EMP": emp.get("id")}


# ---------- HTTP ----------

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

    def _json(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def _q(self, key):
        from urllib.parse import parse_qs, urlparse
        return (parse_qs(urlparse(self.path).query).get(key) or [""])[0]

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            self._send(200, read(os.path.join(HERE, "index.html")), "text/html; charset=utf-8")
        elif p == "/api/setup":
            self._send(200, json.dumps(setup_info(), ensure_ascii=False))
        elif p == "/api/state":
            self._send(200, json.dumps(state_payload(self._q("emp")), ensure_ascii=False))
        elif p == "/api/chat":
            cfg = load_cfg()
            emp = get_emp(cfg, self._q("emp"))
            if not emp:
                self._send(200, "[]")
                return
            jdir = os.path.join(inst_dir(emp), "journal")
            chat = []
            for ln in read(os.path.join(jdir, "chat.jsonl")).splitlines():
                try:
                    d = json.loads(ln)
                    if d.get("action") == "chat-new":
                        chat.append({"ts": d.get("ts", ""), "role": "divider", "text": "— 新对话 —"})
                    else:
                        chat.append({"ts": d.get("ts", ""), "role": "boss", "text": d.get("note", ""),
                                     "config": d.get("action") == "config"})
                except Exception:
                    pass
            for ln in read(os.path.join(jdir, "replies.jsonl")).splitlines():
                try:
                    r = json.loads(ln)
                    chat.append({"ts": r.get("ts", ""), "role": "agent", "text": r.get("text", "")})
                except Exception:
                    pass
            chat.sort(key=lambda x: str(x.get("ts", "")))
            self._send(200, json.dumps(chat, ensure_ascii=False))
        elif p == "/api/feishu/history":
            cfg = load_cfg()
            emp = get_emp(cfg, self._q("emp"))
            env = dict(os.environ)
            if emp:
                env["LILICLAW_LARK_DIR"] = inst_dir(emp)
            try:
                r = subprocess.run([sys.executable, os.path.join(ROOT, "core", "channels", "feishu.py"), "history"],
                                   capture_output=True, text=True, timeout=30, env=env)
                rc, out = r.returncode, r.stdout
            except Exception:
                rc, out = 1, "[]"
            try:
                self._send(200, out.strip() or "[]")
            except Exception:
                self._send(200, "[]")
        elif p == "/api/file":
            # 记忆文件只读查看器（白名单，绝不越出记忆目录）
            cfg = load_cfg()
            allow = {"SOUL.md", "CONSTITUTION.md", "STATE.md", "TASKS.md", "EXPERIENCE.md",
                     "ABILITIES.md", "PLAN.md", "AGENT.md"}
            name = self._q("name")
            emp = get_emp(cfg, self._q("emp"))
            if not emp or name not in allow:
                self._send(404, '{"error":"not found"}')
                return
            self._send(200, json.dumps({"name": name, "content": read(os.path.join(inst_dir(emp), name))},
                                       ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        p = self.path.split("?")[0]
        body = self._json()
        cfg = load_cfg()
        emp = get_emp(cfg, body.get("emp")) if cfg else None
        try:
            if p == "/api/install":
                self._send(200, json.dumps(install(body), ensure_ascii=False))
            elif p == "/api/employees/delete" and cfg:
                self._send(200, json.dumps(remove_employee(body.get("id") or ""), ensure_ascii=False))
            elif p == "/api/config" and emp:
                apply_engine(emp, body)
                if isinstance(body.get("shifts"), list):
                    ok = []
                    for s in body["shifts"]:
                        if re.match(r"^\d{2}:\d{2}$", str(s.get("at", ""))) and s.get("name"):
                            e = {"name": str(s["name"]).strip(), "at": s["at"], "goal": str(s.get("goal", "")).strip()}
                            if s.get("weekday"):
                                e["weekday"] = int(s["weekday"])
                            ok.append(e)
                    if ok:
                        emp["shifts"] = ok
                if body.get("timezone"):
                    emp["timezone"] = body["timezone"]
                if body.get("name"):
                    emp["name"] = body["name"].strip()
                    emp["report_prefix"] = f"【{emp['name']}】"
                if isinstance(body.get("reflection"), dict):
                    r = body["reflection"]
                    cur = emp.get("reflection", {"enabled": True, "idle_minutes": 5})
                    if "enabled" in r:
                        cur["enabled"] = bool(r["enabled"])
                    try:
                        if "idle_minutes" in r:
                            cur["idle_minutes"] = max(1, min(720, float(r["idle_minutes"])))
                    except Exception:
                        pass
                    emp["reflection"] = cur
                if body.get("feishu_poll_seconds"):
                    try:
                        emp["feishu_poll_seconds"] = max(3, min(300, int(body["feishu_poll_seconds"])))
                    except Exception:
                        pass
                save_cfg(cfg)
                self._send(200, '{"ok":true}')
            elif p == "/api/engine/test" and emp:
                eng = engine_name(emp)
                ok, detail = engine_real_test(eng, proxy=emp.get("proxy", ""))
                self._send(200, json.dumps({"ok": ok, "engine": eng, "detail": detail}, ensure_ascii=False))
            elif p == "/api/bell" and cfg:
                rc, out = sh(["python3", os.path.join(ROOT, "core", "bin", "bell.py"), "--dry-run"], 60)
                self._send(200, json.dumps({"ok": rc == 0, "out": out.strip() or "（静默：没有到点的计划、没有新消息）"}, ensure_ascii=False))
            elif p == "/api/run-shift" and emp:
                env = dict(os.environ, LILICLAW_EMP=emp.get("id", ""))
                args = [sys.executable, os.path.join(ROOT, "core", "bin", "shift.py"),
                        body.get("name") or "manual", body.get("goal") or "手动试跑：按协议走一遍，只做一件小事并留完整痕迹", "工作台手动触发"]
                if body.get("quiet", True):
                    args.append("--quiet")
                dbg = open(os.path.join(ROOT, "shift-spawn.log"), "ab")
                subprocess.Popen(args, stdout=dbg, stderr=dbg, stdin=subprocess.DEVNULL, env=env)
                self._send(200, '{"ok":true}')
            elif p == "/api/feishu/save" and emp:
                mode = body.get("mode")
                if mode == "webhook":
                    creds = {"mode": "webhook", "url": (body.get("url") or "").strip()}
                elif mode == "app":
                    creds = {"mode": "app", "app_id": (body.get("app_id") or "").strip(),
                             "app_secret": (body.get("app_secret") or "").strip()}
                    if (body.get("chat_id") or "").strip():
                        creds["chat_id"] = body["chat_id"].strip()
                else:
                    self._send(400, '{"ok":false,"error":"mode 应为 webhook 或 app"}')
                    return
                cp = os.path.join(inst_dir(emp), ".lark_creds")
                json.dump(creds, open(cp, "w", encoding="utf-8"), ensure_ascii=False)
                os.chmod(cp, 0o600)
                emp.setdefault("report", {})["feishu"] = True
                emp.setdefault("events", {})["feishu"] = True
                save_cfg(cfg)
                self._send(200, '{"ok":true}')
            elif p == "/api/feishu/test" and emp:
                env = dict(os.environ, LILICLAW_LARK_DIR=inst_dir(emp))
                try:
                    r = subprocess.run(["python3", os.path.join(ROOT, "core", "channels", "feishu.py"),
                                        "send", "通道测试", "liliclaw 飞书通道已接通。"],
                                       capture_output=True, text=True, timeout=30, env=env)
                    rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
                except Exception as _e:
                    rc, out = 1, str(_e)
                self._send(200, json.dumps({"ok": rc == 0, "out": out.strip()}, ensure_ascii=False))
            elif p == "/api/upload" and emp:
                import base64
                data = (body.get("data") or "")
                if "," in data:
                    data = data.split(",", 1)[1]
                try:
                    raw = base64.b64decode(data)
                except Exception:
                    self._send(400, '{"ok":false,"error":"图片解码失败"}')
                    return
                if len(raw) > 8 * 1024 * 1024:
                    self._send(400, '{"ok":false,"error":"图片超过 8MB"}')
                    return
                ext = "png" if raw[:8].startswith(b"\x89PNG") else "jpg"
                up = os.path.join(inst_dir(emp), "journal", "uploads")
                os.makedirs(up, exist_ok=True)
                fn = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "." + ext
                fp = os.path.join(up, fn)
                open(fp, "wb").write(raw)
                self._send(200, json.dumps({"ok": True, "path": fp}, ensure_ascii=False))
            elif p == "/api/chat-new" and emp:
                jdir = os.path.join(inst_dir(emp), "journal")
                os.makedirs(jdir, exist_ok=True)
                with open(os.path.join(jdir, "chat.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": datetime.datetime.now().isoformat(), "action": "chat-new"},
                                       ensure_ascii=False) + "\n")
                self._send(200, '{"ok":true}')
            elif p == "/api/instruct" and emp:
                # 用 agent 方式动态调教：老板一句话 → 写进收件箱（带 config 标记）→
                # 调度器 2 分钟内触发运行，agent 依据老板授权修改运行计划/记忆规则并汇报
                text = (body.get("text") or "").strip()
                if not text:
                    self._send(400, '{"ok":false}')
                    return
                jdir = os.path.join(inst_dir(emp), "journal")
                os.makedirs(jdir, exist_ok=True)
                rec = {"ts": datetime.datetime.now().isoformat(), "from": "dashboard",
                       "action": "config", "note": text}
                with open(os.path.join(jdir, "workbench-inbox.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                with open(os.path.join(jdir, "chat.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._send(200, '{"ok":true}')
            elif p == "/api/action" and emp:
                jdir = os.path.join(inst_dir(emp), "journal")
                os.makedirs(jdir, exist_ok=True)
                rec = {"ts": datetime.datetime.now().isoformat(), "from": "workbench",
                       "id": body.get("id", ""), "action": body.get("action", ""),
                       "note": body.get("note", "")}
                with open(os.path.join(jdir, "workbench-inbox.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if rec["note"]:
                    with open(os.path.join(jdir, "chat.jsonl"), "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._send(200, '{"ok":true}')
            else:
                self._send(404, "{}")
        except Exception as e:
            self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8866
    print(f"[liliclaw] http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
