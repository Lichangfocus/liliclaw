#!/usr/bin/env python3
"""liliclaw 运行执行器（纯 Python，跨平台）——执行一次运行：
拿锁 → 渲染接手提示词（渐进式披露）→ 按员工内核顺序选引擎 → 看门狗 → 痕迹检查 → 放锁。

用法: shift.py <运行名> [目标] [触发原因] [--quiet]
员工上下文来自环境变量 LILICLAW_EMP（缺省取配置里第一名员工）。
逻辑与已验证的 shift.sh 一一对应，这是它的跨平台替身。
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import engines  # noqa: E402

os.environ.setdefault("LANG", "en_US.UTF-8")
os.environ.setdefault("LC_ALL", "en_US.UTF-8")


def load_emp():
    cfg = json.load(open(os.path.join(ROOT, "liliclaw.json"), encoding="utf-8"))
    emps = cfg.get("employees")
    if emps is None:  # v1 兼容
        e = dict(cfg)
        e["id"] = "default"
        e["workspace"] = cfg.get("workspace") or cfg.get("instance") or "instances/default"
        emps = [e]
    eid = os.environ.get("LILICLAW_EMP") or ""
    emp = next((x for x in emps if x.get("id") == eid), emps[0] if emps else None)
    if not emp:
        sys.exit("[shift] 没有任何员工")
    return cfg, emp


LOAD_PLANS = {
    "bootstrap": ("初始化运行，全量加载：SOUL.md、CONSTITUTION.md、STATE.md、TASKS.md、PLAN.md、"
                  "EXPERIENCE.md、AGENT.md（生效组合契约，含初始化协议）。这是工作台各板块的起点，认真读。"),
    "reflect": ("本次是高频轻量运行。必读（且只读）：STATE.md（目标与现状）、TASKS.md（任务与时间表）、"
                "journal/shifts.jsonl 最后 5 行。按需：EXPERIENCE.md 的「活跃规则」节（要做业务取舍时）、"
                "CONSTITUTION.md（触到决策边界时）。不要通读 SOUL/journal 全文，反思要快、要省。"),
    "event": ("必读：SOUL.md、STATE.md、TASKS.md、journal/shifts.jsonl 最后 5 行，以及触发本次运行的消息文件。"
              "按需：EXPERIENCE.md 活跃规则（做业务判断时）、CONSTITUTION.md（触到决策边界或收到调教指令时必读）、"
              "journal/ 当日日志（需要更多上下文时）。"),
    "task": ("必读：SOUL.md、STATE.md、TASKS.md（重点看要执行的那条任务）、journal/shifts.jsonl 最后 5 行。"
             "按需：EXPERIENCE.md 活跃规则、CONSTITUTION.md（触到决策边界时必读）、当日 journal。"),
    "wrapup": ("复盘运行，全量加载：SOUL.md、CONSTITUTION.md、STATE.md、TASKS.md、EXPERIENCE.md 全文、"
               "当日 journal 全文、journal/shifts.jsonl 今日全部条目。"),
    "weekly": ("周复盘，全量加载：SOUL.md、CONSTITUTION.md、STATE.md、TASKS.md、EXPERIENCE.md 全文、"
               "ABILITIES.md（本次要更新六维评分，涨分必须挂证据）、本周 journal 与运行总结。"),
}
DEFAULT_PLAN = ("标准加载：SOUL.md、STATE.md、TASKS.md、EXPERIENCE.md 的「活跃规则」节、"
                "昨日与今日 journal 尾部、journal/shifts.jsonl 最后 5 行。按需：CONSTITUTION.md（触到决策边界时必读）。")


def render_prompt(name, goal, reason, ws, inst, emp, quiet):
    tpl = open(os.path.join(ROOT, "core", "prompts", "shift.md"), encoding="utf-8").read()
    feishu = os.path.join(ROOT, "core", "channels", "feishu.py")
    if quiet or not (emp.get("report") or {}).get("feishu", True):
        report = "本班是静默/测试班：不要发任何飞书消息。"
    else:
        report = (f'发运行汇报：python3 {feishu} send "汇报·{name}" '
                  f'"<本次结果（结果先行）/ 下一步计划 / 需要真人搭档的事（没有就写无）>"')
    sub = {
        "{{LOAD_PLAN}}": LOAD_PLANS.get(name, DEFAULT_PLAN),
        "{{INSTANCE_DIR}}": inst,
        "{{WORKSPACE}}": ws,
        "{{SHIFT_NAME}}": name,
        "{{SHIFT_GOAL}}": goal or "处理触发事件并推进当下最重要的事",
        "{{REASON}}": reason,
        "{{NOW}}": datetime.datetime.now().strftime("%Y-%m-%d %H:%M %A"),
        "{{FEISHU}}": f"python3 {feishu}",
        "{{CONFIG_PATH}}": os.path.join(ROOT, "liliclaw.json"),
        "{{REPORT_LINE}}": report,
    }
    for k, v in sub.items():
        tpl = tpl.replace(k, v)
    return tpl


def line_count(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def main():
    args = [a for a in sys.argv[1:] if a != "--quiet"]
    quiet = "--quiet" in sys.argv[1:]
    name = args[0] if args else "event"
    goal = args[1] if len(args) > 1 else ""
    reason = args[2] if len(args) > 2 else "手动触发"

    cfg, emp = load_emp()
    ws = emp.get("workspace") or "instances/default"
    ws = ws if os.path.isabs(ws) else os.path.join(ROOT, ws)
    inst = os.path.join(ws, ".liliclaw")
    jdir = os.path.join(inst, "journal")
    os.makedirs(jdir, exist_ok=True)
    log_path = os.path.join(jdir, "shift-runs.log")
    shifts_jsonl = os.path.join(jdir, "shifts.jsonl")
    lock = os.path.join(jdir, ".shift-lock")

    def log(msg):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")

    # ---- 单写者锁：同一时刻每名员工只有一个运行实例 ----
    try:
        os.mkdir(lock)
    except FileExistsError:
        log("[shift] 已有运行在岗，退出")
        return 0
    open(os.path.join(lock, "pid"), "w").write(str(os.getpid()))
    open(os.path.join(lock, "name"), "w").write(name)
    open(os.path.join(lock, "goal"), "w").write(goal)

    rc, used = 1, ""
    try:
        log(f"===== {datetime.datetime.now():%F %T} 运行[{name}] 开始（{reason}）=====")
        prompt = render_prompt(name, goal, reason, ws, inst, emp, quiet)
        before = line_count(shifts_jsonl)
        watch_min = int(emp.get("watchdog_minutes", 20) or 20)
        ctx = {"root": ROOT, "secrets_dir": cfg.get("secrets_dir") or ROOT, "proxy": emp.get("proxy") or ""}
        # 员工级凭证目录（飞书等通道在提示词里被内核调用时读它）
        os.environ["LILICLAW_LARK_DIR"] = inst

        for eng in (emp.get("engines") or ["claude-code"]):
            ok, note = engines.available(eng, ctx)
            if not ok:
                log(f"[shift] 引擎 {eng} 不可用（{note}），试下一个")
                continue
            used = eng
            log(f"[shift] 脑={eng}（看门狗 {watch_min} 分钟）")
            argv, env = engines.build(eng, ctx, prompt)
            env["LILICLAW_LARK_DIR"] = inst
            try:
                with open(log_path, "a", encoding="utf-8") as out:
                    r = subprocess.run(argv, cwd=ws, env=env, stdout=out, stderr=out,
                                       stdin=subprocess.DEVNULL, timeout=watch_min * 60)
                rc = r.returncode
            except subprocess.TimeoutExpired:
                log(f"[shift] ⏱ 看门狗超时（{watch_min} 分钟），已终止")
                rc = 143
            break

        if not used:
            log("[shift] 所有引擎都不可用")
            open(os.path.join(jdir, ".last-fail"), "w").write(str(datetime.datetime.now().timestamp()))
            if not quiet:
                _notify_fail(inst, "⚠️ 数字员工无法上班",
                             "所有内核都不可用。去工作台「设置」测试内核，或完成对应 CLI 的登录。")
            return 1

        # ---- 痕迹检查（铁律：没写运行总结 = 白跑）----
        after = line_count(shifts_jsonl)
        if after <= before:
            log(f"[shift] ⚠️ 痕迹检查失败：引擎退出(exit={rc})但没写运行总结")
            open(os.path.join(jdir, ".last-fail"), "w").write(str(datetime.datetime.now().timestamp()))
            with open(shifts_jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "shift": name,
                    "summary": f"（框架代笔）本次运行异常：引擎 exit={rc} 且未留总结，已进入失败退避",
                    "next": "查 journal/shift-runs.log 定位原因"}, ensure_ascii=False) + "\n")
            if not quiet:
                _notify_fail(inst, f"⚠️ 运行[{name}]异常结束",
                             f"引擎跑了但没留总结（exit={rc}），已退避 {emp.get('fail_backoff_minutes', 15)} 分钟。")
        else:
            try:
                os.remove(os.path.join(jdir, ".last-fail"))
            except FileNotFoundError:
                pass
            if name == "bootstrap":
                try:
                    os.remove(os.path.join(jdir, ".needs-bootstrap"))
                except FileNotFoundError:
                    pass
        log(f"===== {datetime.datetime.now():%F %T} 运行[{name}] 结束 exit={rc} 脑={used} =====")
        return rc
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def _notify_fail(inst, title, body):
    try:
        env = dict(os.environ, LILICLAW_LARK_DIR=inst)
        subprocess.run([sys.executable, os.path.join(ROOT, "core", "channels", "feishu.py"),
                        "send", title, body], capture_output=True, timeout=30, env=env)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
