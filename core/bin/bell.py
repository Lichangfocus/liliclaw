#!/usr/bin/env python3
"""liliclaw 调度网关 —— 整个系统里唯一常驻的东西，纯脚本、零 token 成本。

两种运行方式：
  python3 bell.py --daemon     常驻守护（launchd KeepAlive 拉起，产品的正常形态）：
                                 每 1 秒盯工作台留言（秒级强起运行进程）
                                 每 N 秒轮询飞书（默认 10 秒，配置 feishu_poll_seconds）
                                 每 30 秒检查时间表 / 定时计划 / 自我反思
  python3 bell.py [--dry-run]  单次检查（手动排查、cron 兜底用）

触发优先级：老板消息 > 时间表到点任务 > 定时计划 > 自我反思（空闲兜底）。
设计立场：会死的东西（模型运行）不许常驻；常驻的东西（本脚本）必须笨到不会死——
任何异常都吞掉继续循环，进程真死了 launchd KeepAlive 秒级拉活。
"""
import json, os, re, signal, sys, subprocess, datetime, shutil, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CFG_PATH = os.path.join(ROOT, "liliclaw.json")
try:
    CFG = json.load(open(CFG_PATH))
except Exception:
    sys.exit(0)  # 未启用（无配置）= 静默退出，永远不崩
def employees():
    emps = CFG.get("employees")
    if emps is None:  # v1 扁平配置兼容
        e = dict(CFG)
        e["id"] = "default"
        e["workspace"] = CFG.get("workspace") or CFG.get("instance") or "instances/default"
        emps = [e]
    return [e for e in emps if e.get("enabled", True)]


def emp_paths(emp):
    ws = emp.get("workspace") or "instances/default"
    ws = ws if os.path.isabs(ws) else os.path.join(ROOT, ws)
    inst = os.path.join(ws, ".liliclaw")
    return ws, inst, os.path.join(inst, "journal")


os.environ["TZ"] = CFG.get("timezone", "Asia/Shanghai")
try:
    time.tzset()
except Exception:
    pass


def log(msg, jdir=None):
    jdir = jdir or (emp_paths(employees()[0])[2] if employees() else None)
    if not jdir:
        return
    os.makedirs(jdir, exist_ok=True)
    with open(os.path.join(jdir, "bell.log"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now().strftime('%F %T')} {msg}\n")


def heartbeat(now, jdir):
    os.makedirs(jdir, exist_ok=True)
    open(os.path.join(jdir, ".bell-heartbeat"), "w").write(now.isoformat())


def reload_cfg():
    """守护模式下定期重读配置——「一句话调教」会让 agent 改 liliclaw.json，改完这里自动生效。"""
    global CFG
    try:
        CFG = json.load(open(CFG_PATH))
    except Exception:
        pass


def lock_alive(jdir):
    lk = os.path.join(jdir, ".shift-lock")
    if not os.path.isdir(lk):
        return False
    try:
        pid = int(open(os.path.join(lk, "pid")).read().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        shutil.rmtree(lk, ignore_errors=True)
        log("清掉了死锁（上一次运行的进程已不在）", jdir)
        return False


def in_fail_backoff(emp, jdir):
    f = os.path.join(jdir, ".last-fail")
    if not os.path.exists(f):
        return False
    try:
        last = float(open(f).read().strip())
    except Exception:
        return False
    return (time.time() - last) < emp.get("fail_backoff_minutes", 15) * 60


def channel(name, cmd, inst=None):
    """跑一个通道适配器的 check，返回新条目数；任何异常都当 0。凭证按员工记忆目录隔离。"""
    try:
        env = dict(os.environ)
        if inst:
            env["LILICLAW_LARK_DIR"] = inst
        r = subprocess.run([sys.executable, os.path.join(ROOT, "core", "channels", name), cmd],
                           capture_output=True, text=True, timeout=30, env=env)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0


def local_inbox_new(jdir):
    f = os.path.join(jdir, "workbench-inbox.jsonl")
    try:
        return sum(1 for ln in open(f, encoding="utf-8") if ln.strip())
    except Exception:
        return 0


def due_shift(now, emp, jdir):
    for s in emp.get("shifts", []):
        if "weekday" in s and now.isoweekday() != s["weekday"]:
            continue
        hh, mm = map(int, s["at"].split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < at:
            continue
        stamp = now.strftime("%G-W%V") if "weekday" in s else now.strftime("%F")
        marker = os.path.join(jdir, ".shifts", f"{s['name']}-{stamp}")
        if os.path.exists(marker):
            continue
        return s, marker
    return None, None


def due_task(now, inst, jdir, dry=False):
    """时间表：TASKS.md 里带激活时间(at)的任务，到点就执行。
    防重复触发：同一任务触发后 30 分钟冷却（正常情况下运行会更新任务状态；
    若运行失败没更新，冷却期过后自动重试）。"""
    try:
        md = open(os.path.join(inst, "TASKS.md"), encoding="utf-8").read()
        m = re.search(r"```json\s*\n(.*?)\n```", md, re.S)
        tasks = json.loads(m.group(1)).get("tasks", []) if m else []
    except Exception:
        return None
    fired_dir = os.path.join(jdir, ".task-fired")
    for t in tasks:
        if t.get("status") != "todo" or t.get("owner") not in (None, "", "agent"):
            continue
        at = (t.get("at") or "").strip()
        if not at:
            continue
        try:
            due = datetime.datetime.fromisoformat(at)
        except Exception:
            continue
        if now < due:
            continue
        tid = str(t.get("id") or at)
        marker = os.path.join(fired_dir, re.sub(r"[^A-Za-z0-9_-]", "_", tid))
        if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 1800:
            continue
        if not dry:
            os.makedirs(fired_dir, exist_ok=True)
            open(marker, "w").write(now.isoformat())
        return t
    return None


def idle_minutes(now, jdir):
    """距上一次运行结束多少分钟（shifts.jsonl 的最后修改时间）。从未运行过 = 无穷大。"""
    f = os.path.join(jdir, "shifts.jsonl")
    try:
        if os.path.getsize(f) > 0:
            return (now.timestamp() - os.path.getmtime(f)) / 60
    except Exception:
        pass
    return float("inf")


def find_trigger(now, include, emp, dry=False):
    """按优先级为某名员工找本刻该触发什么。返回 (shift, marker, reason)。"""
    ws, inst, jdir = emp_paths(emp)
    ev = emp.get("events", {})

    # 优先级 1：老板消息（工作台留言 / 飞书 / 云端 KV）
    if "inbox" in include and ev.get("local_inbox", True):
        inbox = os.path.join(jdir, "workbench-inbox.jsonl")
        handoff = os.path.join(jdir, "inbox-processing.jsonl")
        n = local_inbox_new(jdir)
        stale = os.path.exists(handoff) and time.time() - os.path.getmtime(handoff) > 1800
        if n or stale:
            if n and not dry:
                # 机械交接：触发时把收件箱原子移交，天然防重复触发、防漏清
                try:
                    if os.path.exists(handoff):
                        with open(handoff, "a", encoding="utf-8") as out, open(inbox, encoding="utf-8") as src:
                            out.write(src.read())
                        os.remove(inbox)
                    else:
                        os.replace(inbox, handoff)
                    os.utime(handoff, None)
                except Exception:
                    pass
            shift = {"name": "event",
                     "goal": (f"处理老板的留言：读 {handoff}（每行一条 JSON；action=config 的是调教指令，"
                              "等同老板授权改运行计划/记忆规则）。逐条处理，全部处理完后删除该文件。")}
            return shift, None, (f"工作台有 {n} 条新留言" if n else "上次留言处理超时未完成，重新触发")
    if "feishu" in include and ev.get("feishu", True):
        n = channel("feishu.py", "check", inst)
        if n:
            return ({"name": "event", "goal": "处理老板的飞书新消息（最高优先级），处理完继续推进当下最重要的事"},
                    None, f"飞书有 {n} 条新消息")
    if "kv" in include and ev.get("workbench_kv", False):
        n = channel("workbench.py", "check", inst)
        if n:
            return ({"name": "event", "goal": "处理云端工作台的留言/勾选（用 channels/workbench.py pull 取出）"},
                    None, f"云端工作台有 {n} 条新动作")

    # 优先级 2：时间表到点任务
    if "task" in include:
        t = due_task(now, inst, jdir, dry=dry)
        if t:
            shift = {"name": "task",
                     "goal": (f"执行时间表到点任务「{t.get('title','')}」（id={t.get('id','')}）。"
                              + (f"背景：{t.get('note')}。" if t.get("note") else "")
                              + (f"验收标准：{t.get('accept')}。" if t.get("accept") else "")
                              + "完成后必须更新该任务的状态，并按结果给下一步任务排上激活时间(at)。")}
            return shift, None, f"时间表任务到点（{t.get('at','')} · {t.get('id','')}）"

    # 优先级 3：定时计划
    if "shift" in include:
        s, marker = due_shift(now, emp, jdir)
        if s:
            return s, marker, f"定时计划到点（{s['at']}）"

    # 优先级 4：自我反思（空闲兜底，永不停摆）；从未运行过 = 先跑一次初始化
    if "reflect" in include:
        rf = emp.get("reflection", {})
        if rf.get("enabled", True):
            gap = idle_minutes(now, jdir)
            thr = float(rf.get("idle_minutes", 5))
            needs_boot = os.path.exists(os.path.join(jdir, ".needs-bootstrap")) or gap == float("inf")
            if needs_boot:
                shift = {"name": "bootstrap",
                         "goal": ("初始化运行（工作台各板块的起点）：基于北极星目标，"
                                  "①在 STATE.md 的经营指标 JSON 块里定义 3-6 个重要指标（含中间指标，说明为什么）；"
                                  "②在 PLAN.md 搭出本周 OKR（1-2 个 O，每个 2-3 条可量化 KR）；"
                                  "③把首批任务拆进 TASKS.md 并排时间表(at)，其中至少 1 个学习任务(kind=learn，补齐目标所需的知识缺口)；"
                                  "④在 STATE.md 写清现状。若北极星还是空的，就把「向老板确认目标」建成 owner=lichang 任务并把能做的准备都做了。")}
                return shift, None, "首次运行：初始化经营指标 / OKR / 首批任务与学习计划"
            if gap >= thr:
                shift = {"name": "reflect",
                         "goal": ("自我反思（标准程序）：读 STATE 的目标、TASKS 队列、最近几条运行记录，回答"
                                  "「当下离目标最近的下一步是什么」。三选一并执行：①有能直接干的小事→立刻干掉并留痕；"
                                  "②该排期的→给对应任务补上激活时间(at)写进时间表；③确实在等外部→写清等什么、"
                                  "给对应任务标 waiting 并排一个复查时间(at)。反思不许空手收尾：时间表必须比反思前更明确。一时找不到活，就按 .liliclaw/AGENT.md 的反思菜单轮着扫（目标回溯/风险/机会/学习/决策复盘/资产盘点/体系自检）。")}
                return shift, None, f"空闲 {gap:.0f} 分钟，触发自我反思"
    return None, None, None


def fire(shift, marker, reason, emp, sync=True):
    ws, inst, jdir = emp_paths(emp)
    log(f"触发 → 运行[{shift['name']}]：{reason}", jdir)
    if marker:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        open(marker, "w").write(datetime.datetime.now().isoformat())
    env = dict(os.environ, LILICLAW_EMP=str(emp.get("id", "")))
    args = [sys.executable, os.path.join(HERE, "shift.py"), shift["name"], shift.get("goal", ""), reason or ""]
    if sync:
        subprocess.run(args, env=env)
    else:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env)


ALL_SOURCES = ["inbox", "feishu", "kv", "task", "shift", "reflect"]


def main_once(dry=False):
    now = datetime.datetime.now()
    for emp in employees():
        ws, inst, jdir = emp_paths(emp)
        heartbeat(now, jdir)
        if lock_alive(jdir) or in_fail_backoff(emp, jdir):
            continue
        shift, marker, reason = find_trigger(now, ALL_SOURCES, emp, dry=dry)
        if not shift:
            continue
        if dry:
            log(f"触发（dry-run）→ 运行[{shift['name']}]：{reason}", jdir)
            print(f"[bell] 员工[{emp.get('id')}] 将触发运行[{shift['name']}]，原因：{reason}")
            continue
        fire(shift, marker, reason, emp, sync=True)


def daemon():
    """常驻网关：留言秒级强起，飞书 N 秒，计划/时间表/反思 30 秒。"""
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # 自动回收运行子进程，长驻不留僵尸
    log(f"调度网关启动（pid={os.getpid()}），员工数={len(employees())}")
    i = 0
    cfg_mtime = 0
    while True:
        try:
            now = datetime.datetime.now()
            # 配置改了（工作台设置/调教指令）→ 即时重载，同步生效
            try:
                mt = os.path.getmtime(CFG_PATH)
                if mt != cfg_mtime:
                    cfg_mtime = mt
                    reload_cfg()
            except Exception:
                pass
            for emp in employees():
                ws, inst, jdir = emp_paths(emp)
                if i % 15 == 0:
                    heartbeat(now, jdir)
                if lock_alive(jdir) or in_fail_backoff(emp, jdir):
                    continue
                include = ["inbox"]  # 秒级：工作台留言
                fp = max(3, int(emp.get("feishu_poll_seconds", 10)))
                if i % fp == 0:
                    include.append("feishu")
                if i % 30 == 0:
                    include += ["kv", "task", "shift", "reflect"]
                shift, marker, reason = find_trigger(now, include, emp)
                if shift:
                    fire(shift, marker, reason, emp, sync=False)
        except Exception as e:
            try:
                log(f"守护循环异常（继续跑）: {e}")
            except Exception:
                pass
        time.sleep(1)
        i += 1


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        daemon()
    else:
        main_once(dry="--dry-run" in sys.argv)
