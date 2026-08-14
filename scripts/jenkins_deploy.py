#!/usr/bin/env python3
"""Jenkins 自动发布 CLI。

通过 Jenkins REST API 触发参数化构建、轮询进度、抓取控制台日志。
仅依赖 Python 标准库，无需安装第三方包。

用法：
    python jenkins_deploy.py --config .jenkins-deploy.json <command> [args]

命令：
    init     [--dir DIR]                        生成配置模板
    list                                         列出所有可发布任务
    show     NAME                                查看单个任务详情
    deploy   NAME [--branch B] [--yes] [--follow] [--no-wait]
                                                 触发构建并跟踪进度（--yes 才会真正触发）
    status   NAME                                查看最近一次构建状态
    console  NAME [--build N] [--tail LINES]     查看控制台日志
    watch    NAME [--build N] [--follow]         跟踪已有构建直到结束（不触发新构建）
    params   NAME                                查看 Job 已定义的构建参数
    stop     NAME [--build N]                    中止排队/运行中的构建

认证：Jenkins 用户名 + API Token（Basic Auth）。
CSRF：自动从 crumbIssuer 获取 crumb（若 Jenkins 关闭了 crumb 则自动跳过）。
"""
import argparse
import base64
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Windows 下 stdout/stderr 默认用 GBK，中文会乱码；强制 UTF-8 输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILENAME = "config.json"
DEFAULT_CONFIG = str(SKILL_DIR / CONFIG_FILENAME)


class JenkinsError(Exception):
    """Jenkins 调用失败，携带可读原因。"""


def fmt_duration(ms):
    """把毫秒格式化为 1m20s / 45s 之类。"""
    if ms is None:
        return "?"
    s = int(ms) // 1000
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m{s % 60:02d}s"


def log(msg=""):
    print(msg, flush=True)


class Jenkins:
    def __init__(self, base_url, username, api_token, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        raw = f"{username}:{api_token}".encode()
        self.auth_header = f"Basic {base64.b64encode(raw).decode()}"
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        self._crumb = None  # (field, value)

    @staticmethod
    def _quote(path):
        # 保留 '/'（Jenkins 目录层级），其余（空格等）编码。
        return urllib.parse.quote(path, safe="/")

    def _url(self, path):
        return f"{self.base_url}/{self._quote(path.lstrip('/'))}"

    def _request(self, path, method="GET", query=None, headers=None):
        url = self._url(path)
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", self.auth_header)
        if self._crumb:
            req.add_header(self._crumb[0], self._crumb[1])
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            resp = self.opener.open(req, timeout=self.timeout)
            return resp, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:800]
            hint = ""
            if e.code in (401, 403):
                hint = "（认证失败：请检查 config 里的 username / apiToken 是否正确）"
            raise JenkinsError(f"HTTP {e.code} {e.reason} — {url}\n{body}{hint}")
        except urllib.error.URLError as e:
            raise JenkinsError(f"无法连接 Jenkins：{e.reason}\n请检查 baseUrl 是否可达、是否在公司网络内。")

    def _json(self, path):
        _, body = self._request(path)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise JenkinsError(f"解析 Jenkins 响应失败（{path}）：{e}")

    def ensure_crumb(self):
        # 部分 Jenkins 关闭了 CSRF，此时 crumbIssuer 返回 404，直接忽略。
        try:
            resp, body = self._request("crumbIssuer/api/json")
            data = json.loads(body)
            field = data.get("crumbRequestField", "Jenkins-Crumb")
            value = data.get("crumb")
            if value:
                self._crumb = (field, value)
        except JenkinsError:
            pass

    def trigger_build(self, job, params):
        """触发参数化构建，返回 queue item id 或 ('build', number)。"""
        self.ensure_crumb()
        resp, _ = self._request(f"{job}/buildWithParameters", method="POST", query=params)
        if resp.status not in (200, 201):
            raise JenkinsError(f"触发失败，HTTP {resp.status}")
        location = resp.headers.get("Location") or resp.headers.get("location")
        if not location:
            raise JenkinsError("触发成功但未返回 Location，无法跟踪队列。")
        return self._parse_location(location)

    @staticmethod
    def _parse_location(location):
        parts = [p for p in urllib.parse.urlparse(location).path.split("/") if p]
        if "item" in parts and parts.index("item") + 1 < len(parts):
            return ("queue", parts[parts.index("item") + 1])
        if parts and parts[-1].isdigit():
            return ("build", parts[-1])
        raise JenkinsError(f"无法解析 Jenkins 返回的 Location：{location}")

    def wait_queue(self, queue_id, poll, timeout=300):
        start = time.time()
        while time.time() - start < timeout:
            data = self._json(f"queue/item/{queue_id}/api/json")
            if data.get("cancelled"):
                raise JenkinsError("构建在排队时被取消。")
            exe = data.get("executable")
            if exe and exe.get("number"):
                return exe["number"]
            why = data.get("why") or "排队中"
            log(f"  排队中…（{why}）")
            time.sleep(poll)
        raise JenkinsError("等待构建离开队列超时。")

    def build_status(self, job, number):
        return self._json(f"{job}/{number}/api/json")

    def job_parameters(self, job):
        """返回 Job 已定义的构建参数（未开启参数化构建则为空列表）。"""
        data = self._json(f"{job}/api/json")
        params = []
        for prop in data.get("property") or []:
            defs = prop.get("parameterDefinitions")
            if not defs:
                continue
            for d in defs:
                cls = (d.get("_class") or "").split(".")[-1]
                if cls.endswith("ParameterDefinition"):
                    cls = cls[: -len("ParameterDefinition")]
                default = ""
                dv = d.get("defaultParameterValue")
                if dv and dv.get("value") is not None:
                    default = str(dv["value"])
                params.append({
                    "name": d.get("name", "?"),
                    "type": cls or d.get("type", "?"),
                    "default": default,
                    "description": d.get("description") or "",
                    "choices": d.get("choices") or [],
                })
        return params

    def console_text(self, job, number, start=None):
        if start is None:
            path = f"{job}/{number}/consoleText"
            _, body = self._request(path)
            return body.decode(errors="replace")
        path = f"{job}/{number}/logText/progressiveText"
        _, body = self._request(path, query={"start": start})
        return body.decode(errors="replace")

    def stop_build(self, job, number):
        """中止正在运行的构建（POST .../stop，成功返回 302 或 200）。"""
        self.ensure_crumb()
        resp, _ = self._request(f"{job}/{number}/stop", method="POST")
        if resp.status not in (200, 201, 202, 302):
            raise JenkinsError(f"中止构建失败，HTTP {resp.status}")

    def cancel_queue(self, job):
        """取消该 Job 所有排队中的构建项，返回取消数量。"""
        self.ensure_crumb()
        try:
            data = self._json("queue/api/json")
        except JenkinsError:
            return 0
        job_suffix = job.rstrip("/")
        cancelled = 0
        for item in data.get("items", []):
            task = item.get("task") or {}
            url = (task.get("url") or "").rstrip("/")
            if url.endswith(job_suffix):
                qid = item.get("id")
                if qid is None:
                    continue
                try:
                    self._request(f"queue/item/{qid}/cancelQueue", method="POST")
                    cancelled += 1
                except JenkinsError:
                    continue
        return cancelled


def load_config(path):
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise JenkinsError(
            f"找不到配置文件：{cfg_path.resolve()}\n"
            f"请先运行 `python jenkins_deploy.py --config {path} init` 生成模板并填写。"
        )
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise JenkinsError(f"配置文件 JSON 格式错误：{e}")
    jenkins = cfg.get("jenkins") or {}
    if not jenkins.get("baseUrl"):
        raise JenkinsError("配置文件缺少 jenkins.baseUrl。")
    deploys = cfg.get("deploys") or []
    return cfg, jenkins, deploys


def resolve_task(deploys, name):
    for t in deploys:
        if t.get("name") == name:
            return t
    raise JenkinsError(f"没有名为「{name}」的发布任务。可用任务见 `list` 命令。")


def get_client(cfg, jenkins, task):
    cred_name = task.get("credential")
    creds = jenkins.get("credentials") or {}
    cred = creds.get(cred_name)
    if not cred:
        raise JenkinsError(
            f"发布任务「{task.get('name')}」引用了未知凭证「{cred_name}」，"
            f"请在 jenkins.credentials 里配置。"
        )
    if not cred.get("username") or not cred.get("apiToken"):
        raise JenkinsError(f"凭证「{cred_name}」缺少 username 或 apiToken。")
    username = cred["username"]
    if any(ord(c) > 127 for c in username):
        log("⚠ 提示：username 含非 ASCII 字符（如中文）。Jenkins 的 Basic Auth 通常只认 ASCII 登录 User ID，")
        log("   中文用户名会变成 ?? 导致 401，请改用「登录 User ID」（不是显示名/全名）。")
    return Jenkins(
        jenkins["baseUrl"],
        username,
        cred["apiToken"],
        timeout=int(jenkins.get("timeout", 30)),
    )


def build_params(task, branch_override):
    branch = branch_override or task.get("branch")
    if not branch:
        raise JenkinsError(f"任务「{task.get('name')}」未配置 branch。")
    params = dict(task.get("params") or {})
    param_name = task.get("branchParam", "branch")
    if param_name is not None:
        params[param_name] = branch
    return params, branch


def resolve_log_dir(jenkins):
    """失败日志保存目录：jenkins.logDir 为空/未配时默认当前目录。"""
    return (jenkins.get("logDir") or "").strip() or "."


def cmd_init(args):
    target = Path(args.dir).resolve() if args.dir else SKILL_DIR
    target.mkdir(parents=True, exist_ok=True)
    dest = target / CONFIG_FILENAME
    if dest.exists():
        log(f"已存在，不覆盖：{dest}")
        return
    template = Path(__file__).resolve().parent.parent / "assets" / "config.template.json"
    if not template.exists():
        raise JenkinsError(f"缺少配置模板：{template}")
    dest.write_bytes(template.read_bytes())
    log(f"已生成配置模板：{dest}")
    log("请填写 jenkins.baseUrl、credentials、deploys 后使用。")


def cmd_list(cfg, jenkins, deploys):
    if not deploys:
        log("尚未配置任何发布任务。")
        return
    log(f"Jenkins: {jenkins.get('baseUrl')}")
    log("-" * 60)
    log(f"{'名称':<24} {'环境':<10} {'分支':<14} {'Job'}")
    log("-" * 60)
    for t in deploys:
        log(f"{t.get('name','?'):<24} {t.get('environment','?'):<10} {t.get('branch','?'):<14} {t.get('job','?')}")


def cmd_show(cfg, jenkins, deploys, name):
    t = resolve_task(deploys, name)
    log(f"任务名称 : {t.get('name')}")
    log(f"环境     : {t.get('environment')}")
    log(f"分支     : {t.get('branch')}")
    log(f"Job      : {t.get('job')}")
    log(f"凭证     : {t.get('credential')}")
    if t.get("params"):
        log(f"额外参数 : {json.dumps(t.get('params'), ensure_ascii=False)}")


def cmd_deploy(args):
    cfg, jenkins, deploys = load_config(args.config)
    task = resolve_task(deploys, name=args.name)
    params, branch = build_params(task, args.branch)
    env = task.get("environment", "?")
    job = task.get("job")

    # —— 发布前确认：打印将要发布的环境 / 分支 ——
    log("=" * 60)
    log("即将发布：")
    log(f"  名称  : {task.get('name')}")
    log(f"  环境  : {env}")
    log(f"  分支  : {branch}")
    log(f"  Job   : {job}")
    if params:
        log(f"  参数  : {json.dumps(params, ensure_ascii=False)}")
    log("=" * 60)
    if not args.yes:
        log("【未确认】本次为演练，未真正触发。确认无误后加 --yes 重新执行。")
        return

    client = get_client(cfg, jenkins, task)
    poll = int(jenkins.get("pollInterval", 10))

    log("触发构建…")
    kind, ref = client.trigger_build(job, params)
    if kind == "queue":
        number = client.wait_queue(ref, poll)
    else:
        number = ref

    log(f"构建 # {number} 已开始。")
    if args.no_wait:
        log(f"已退出跟踪，可稍后用 `status {args.name}` 查看。")
        return

    _watch(client, job, number, poll, follow=args.follow, name=args.name,
           log_dir=resolve_log_dir(jenkins))


def _watch(client, job, number, poll, follow, name, log_dir=None):
    start = time.time()
    offset = 0
    while True:
        status = client.build_status(job, number)
        building = status.get("building", False)
        result = status.get("result")
        elapsed_ms = status.get("duration") if not building else None
        est = status.get("estimatedDuration")
        wall = int(time.time() - start)

        if building:
            pct = f"{min(int(wall / max(est / 1000, 1) * 100), 99)}%" if est else ""
            bar = _bar(pct)
            line = f"  [{fmt_duration(wall * 1000)}" + (f" / ~{fmt_duration(est)} {pct}{bar}" if est else "") + "] 构建中…"
            log(f"\r{line}", )
            if follow:
                txt = client.console_text(job, number, start=offset)
                if txt:
                    sys.stdout.write(txt)
                    sys.stdout.flush()
                    offset += len(txt.encode())
            time.sleep(poll)
            continue

        # 结束
        print("\r" + " " * 60 + "\r", end="", flush=True)
        duration = status.get("duration")
        log(f"{'✓ 成功' if result == 'SUCCESS' else '✗ ' + (result or 'UNKNOWN')}  构建 #{number}  ({fmt_duration(duration)})")
        if result != "SUCCESS":
            log("")
            log("=" * 60)
            log("构建失败，抓取控制台日志分析：")
            log("=" * 60)
            _dump_console(client, job, number, name, tail=80, log_dir=log_dir)
        else:
            log(f"详情：{status.get('url', '')}")
        return


def _bar(pct):
    if not pct:
        return ""
    n = int(int(pct.rstrip("%")) / 10)
    return " [" + "#" * n + "-" * (10 - n) + "]"


def cmd_status(args):
    cfg, jenkins, deploys = load_config(args.config)
    task = resolve_task(deploys, args.name)
    client = get_client(cfg, jenkins, task)
    job = task.get("job")
    status = client.build_status(job, "lastBuild")
    number = status.get("number")
    log(f"任务: {task.get('name')}  Job: {job}")
    log(f"最近构建 # {number}")
    log(f"  结果    : {status.get('result') or ('构建中' if status.get('building') else '未知')}")
    log(f"  是否进行 : {status.get('building')}")
    log(f"  耗时    : {fmt_duration(status.get('duration'))}")
    log(f"  链接    : {status.get('url', '')}")


def cmd_console(args):
    cfg, jenkins, deploys = load_config(args.config)
    task = resolve_task(deploys, args.name)
    client = get_client(cfg, jenkins, task)
    job = task.get("job")
    number = args.build or "lastBuild"
    _dump_console(client, job, number, task.get("name"), tail=args.tail or 0,
                  log_dir=resolve_log_dir(jenkins))


def _dump_console(client, job, number, name, tail=0, log_dir=None):
    text = client.console_text(job, number)
    if tail and tail > 0:
        lines = text.splitlines()
        shown = "\n".join(lines[-tail:]) if len(lines) > tail else text
        if shown:
            log(shown)
            if len(lines) > tail:
                log(f"\n…（共 {len(lines)} 行，仅显示最后 {tail} 行）")
    else:
        log(text)
    # 保存完整日志
    safe = f"jenkins-console-{name}-{number}.log".replace("/", "_")
    out_dir = Path(log_dir or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / safe
    out_path.write_text(text, encoding="utf-8")
    log(f"\n完整日志已保存：{out_path}")


def cmd_watch(args):
    """跟踪已有构建直到结束，不触发新构建。用于 deploy --no-wait 之后接收结果。"""
    cfg, jenkins, deploys = load_config(args.config)
    task = resolve_task(deploys, args.name)
    client = get_client(cfg, jenkins, task)
    job = task.get("job")
    poll = int(jenkins.get("pollInterval", 10))

    status = client.build_status(job, args.build or "lastBuild")
    number = status.get("number")
    building = status.get("building", False)

    if not building:
        result = status.get("result")
        log(f"{'✓ 成功' if result == 'SUCCESS' else '✗ ' + (result or 'UNKNOWN')}  构建 #{number}  ({fmt_duration(status.get('duration'))})")
        log(f"详情：{status.get('url', '')}")
        return

    log(f"跟踪构建 #{number} …（可随时用 `stop {args.name}` 中止）")
    _watch(client, job, number, poll, follow=args.follow, name=args.name,
           log_dir=resolve_log_dir(jenkins))


def cmd_params(args):
    cfg, jenkins, deploys = load_config(args.config)
    task = resolve_task(deploys, args.name)
    client = get_client(cfg, jenkins, task)
    job = task.get("job")
    params = client.job_parameters(job)
    if not params:
        log(f"Job {job} 没有定义任何构建参数（可能未开启「参数化构建」）。")
        return
    log(f"Job: {job}")
    log(f"共 {len(params)} 个参数：")
    log("-" * 78)
    log(f"{'名称':<22} {'类型':<14} {'默认值':<22} 说明")
    log("-" * 78)
    for p in params:
        default = p["default"]
        if p["choices"]:
            default = f"{default}（可选: {', '.join(p['choices'])}）"
        log(f"{p['name']:<22} {p['type']:<14} {default:<22} {p['description']}")


def cmd_stop(args):
    cfg, jenkins, deploys = load_config(args.config)
    task = resolve_task(deploys, args.name)
    client = get_client(cfg, jenkins, task)
    job = task.get("job")

    if args.build:
        number = args.build
        status = client.build_status(job, number)
        if not status.get("building"):
            log(f"构建 #{number} 未在运行（result={status.get('result')}），无需中止。")
            return
        client.stop_build(job, number)
        log(f"已发送中止请求：{task.get('name')} 构建 #{number}")
        return

    # 未指定 --build：先取消排队，再中止最近一次运行中的构建
    cancelled = client.cancel_queue(job)
    log(f"已取消 {cancelled} 个排队中的构建项。" if cancelled else "队列中没有该任务的待执行项。")

    status = client.build_status(job, "lastBuild")
    if status.get("building"):
        number = status.get("number")
        client.stop_build(job, number)
        log(f"已发送中止请求：{task.get('name')} 构建 #{number}")
    else:
        log("最近一次构建未在运行，无需中止。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Jenkins 自动发布 CLI")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"配置文件路径（默认 {DEFAULT_CONFIG}）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="生成配置模板")
    p_init.add_argument("--dir", help="生成到指定目录（默认当前目录）")

    sub.add_parser("list", help="列出所有可发布任务")

    p_show = sub.add_parser("show", help="查看单个任务详情")
    p_show.add_argument("name")

    p_deploy = sub.add_parser("deploy", help="触发构建并跟踪进度")
    p_deploy.add_argument("name")
    p_deploy.add_argument("--branch", help="覆盖配置里的分支")
    p_deploy.add_argument("--yes", action="store_true", help="确认发布（不传则仅演练打印）")
    p_deploy.add_argument("--follow", action="store_true", help="实时跟随输出控制台日志")
    p_deploy.add_argument("--no-wait", action="store_true", help="触发后不等待，立即返回")

    p_status = sub.add_parser("status", help="查看最近构建状态")
    p_status.add_argument("name")

    p_console = sub.add_parser("console", help="查看控制台日志")
    p_console.add_argument("name")
    p_console.add_argument("--build", help="指定构建号（默认 lastBuild）")
    p_console.add_argument("--tail", type=int, help="仅显示最后 N 行")

    p_watch = sub.add_parser("watch", help="跟踪已有构建直到结束（不触发新构建）")
    p_watch.add_argument("name")
    p_watch.add_argument("--build", help="指定构建号（默认 lastBuild）")
    p_watch.add_argument("--follow", action="store_true", help="实时跟随输出控制台日志")

    p_params = sub.add_parser("params", help="查看 Job 已定义的构建参数")
    p_params.add_argument("name")

    p_stop = sub.add_parser("stop", help="中止排队或运行中的构建")
    p_stop.add_argument("name")
    p_stop.add_argument("--build", help="指定要中止的构建号（默认取消排队 + 中止最近一次运行中的构建）")

    args = parser.parse_args(argv)
    if args.command == "init":
        cmd_init(args)
        return
    if args.command == "list":
        cfg, jenkins, deploys = load_config(args.config)
        cmd_list(cfg, jenkins, deploys)
        return
    if args.command == "show":
        cfg, jenkins, deploys = load_config(args.config)
        cmd_show(cfg, jenkins, deploys, args.name)
        return
    if args.command == "deploy":
        cmd_deploy(args)
        return
    if args.command == "status":
        cmd_status(args)
        return
    if args.command == "console":
        cmd_console(args)
        return
    if args.command == "watch":
        cmd_watch(args)
        return
    if args.command == "params":
        cmd_params(args)
        return
    if args.command == "stop":
        cmd_stop(args)
        return


if __name__ == "__main__":
    try:
        main()
    except JenkinsError as e:
        log(f"\n[错误] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log("\n已中断。构建仍在 Jenkins 上继续，可用 status/console 查看，或用 stop 中止。")
        sys.exit(130)
