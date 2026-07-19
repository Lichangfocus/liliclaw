#!/usr/bin/env python3
"""liliclaw 内核适配层（纯 Python，跨平台）。

契约：每个内核提供两件事——
  available(ctx) -> (bool, note)   本机是否可用（只查装没装/钥匙在不在；「能运行」由真实连通测试把关）
  build(ctx, prompt) -> (argv, env)  无头运行一次的命令与环境

ctx: {"root", "secrets_dir", "proxy"}  代理为员工级配置。
原则：框架零模型、模型/端点/API key 一律在各 CLI 自己那层配置；
     二进制解析不依赖 PATH（常驻服务的环境里没有用户 shell 的 PATH）。
"""
import glob
import os
import shutil
import sys


def _which(cmd):
    return shutil.which(cmd) or ""


def _read_secret(path):
    try:
        with open(path, encoding="utf-8") as f:
            return "".join(ln for ln in f if not ln.strip().startswith("#")).strip()
    except Exception:
        return ""


# ---------- Claude Code CLI ----------

def _claude_bin():
    p = _which("claude")
    if p:
        return p
    g = sorted(glob.glob(os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude")))
    return g[-1] if g else ""


def _claude_token(secrets_dir):
    tok = _read_secret(os.path.join(secrets_dir, ".claude-oauth"))
    if tok:
        return tok.split()[0][:4096]
    # 兜底：从运行中的 Claude 桌面 App 子进程借当前 token（App 开着才有）
    try:
        import subprocess
        pids = subprocess.run(["pgrep", "-f", "claude-code/.*claude"],
                              capture_output=True, text=True, timeout=10).stdout.split()
        for pid in pids[:8]:
            env_out = subprocess.run(["ps", "eww", "-p", pid],
                                     capture_output=True, text=True, timeout=10).stdout
            for tokpart in env_out.split():
                if tokpart.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                    return tokpart.split("=", 1)[1]
    except Exception:
        pass
    return ""


def _claude_env_file(secrets_dir):
    """用户在 CLI 层配三方模型的入口（.claude-env，一行一个 KEY=VALUE），最后注入所以覆盖默认。"""
    out = {}
    try:
        with open(os.path.join(secrets_dir, ".claude-env"), encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if "=" in ln and not ln.startswith("#"):
                    k, v = ln.split("=", 1)
                    if k.replace("_", "").isalnum():
                        out[k] = v
    except Exception:
        pass
    return out


def claude_available(ctx):
    if not _claude_bin():
        return False, "没找到 claude 命令"
    if _claude_token(ctx["secrets_dir"]) or os.path.exists(os.path.join(ctx["secrets_dir"], ".claude-env")):
        return True, ""
    return False, "既没有订阅 token（claude setup-token → .claude-oauth）也没有 .claude-env"


def claude_build(ctx, prompt):
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
              "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION"):
        env.pop(k, None)
    if ctx.get("proxy"):
        env["HTTPS_PROXY"] = env["HTTP_PROXY"] = ctx["proxy"]
        env["NO_PROXY"] = "localhost,127.0.0.1,::1,.local"
    env["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
    env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
    tok = _claude_token(ctx["secrets_dir"])
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    env.update(_claude_env_file(ctx["secrets_dir"]))
    return [_claude_bin(), "-p", prompt, "--dangerously-skip-permissions"], env


# ---------- Kimi Code CLI（原生） ----------

def _kimi_bin():
    p = _which("kimi")
    if p:
        return p
    cand = os.path.expanduser("~/.kimi-code/bin/kimi")
    return cand if os.access(cand, os.X_OK) else ""


def kimi_available(ctx):
    return (True, "") if _kimi_bin() else (False, "没找到 kimi 命令（kimi login 后即用）")


def kimi_build(ctx, prompt):
    # 注意：kimi 的 -p 不能与 --yolo 同用，prompt 模式本身即非交互执行
    return [_kimi_bin(), "-p", prompt], dict(os.environ)


# ---------- 其余内核（实验性：装了即列，能否运行由连通测试把关） ----------

def _simple(cmd, args_maker):
    def available(ctx):
        return (True, "") if _which(cmd) else (False, f"没找到 {cmd} 命令")

    def build(ctx, prompt):
        return args_maker(_which(cmd), prompt), dict(os.environ)
    return available, build


pi_available, pi_build = _simple("pi", lambda b, p: [b, "--no-session", "-p", p])
codex_available, codex_build = _simple(
    "codex", lambda b, p: [b, "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", p])
gemini_available, gemini_build = _simple("gemini", lambda b, p: [b, "--yolo", "-p", p])
qwen_available, qwen_build = _simple("qwen", lambda b, p: [b, "--yolo", "-p", p])
opencode_available, opencode_build = _simple("opencode", lambda b, p: [b, "run", p])
aider_available, aider_build = _simple(
    "aider", lambda b, p: [b, "--yes-always", "--no-auto-commits", "--message", p])


ENGINES = {
    "claude-code": (claude_available, claude_build),
    "kimi": (kimi_available, kimi_build),
    "pi": (pi_available, pi_build),
    "codex": (codex_available, codex_build),
    "gemini": (gemini_available, gemini_build),
    "qwen": (qwen_available, qwen_build),
    "opencode": (opencode_available, opencode_build),
    "aider": (aider_available, aider_build),
}


def available(key, ctx):
    fn = ENGINES.get(key)
    return fn[0](ctx) if fn else (False, "没有这个内核的适配器")


def build(key, ctx, prompt):
    fn = ENGINES.get(key)
    if not fn:
        raise KeyError(key)
    return fn[1](ctx, prompt)


if __name__ == "__main__":
    # 命令行自检：python3 engines.py available <key> [proxy]
    import json
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    try:
        cfg = json.load(open(os.path.join(root, "liliclaw.json")))
    except Exception:
        cfg = {}
    ctx = {"root": root, "secrets_dir": cfg.get("secrets_dir") or root,
           "proxy": sys.argv[3] if len(sys.argv) > 3 else ""}
    if len(sys.argv) > 2 and sys.argv[1] == "available":
        ok, note = available(sys.argv[2], ctx)
        print("ok" if ok else f"unavailable: {note}")
        sys.exit(0 if ok else 1)
    print("用法: engines.py available <key> [proxy]")
