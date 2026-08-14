---
name: jenkins-deploy
description: Use when the user wants to trigger, monitor, or troubleshoot a Jenkins build/deploy — phrases like "发布 X 到 prod", "部署", "发布/上线某个项目", "查看发布进度", "发布失败看原因", "Jenkins 自动发布", or anything about deploying a project to a dev/test/prod environment. The deploy targets are pre-configured in the skill's config.json with named tasks carrying project, branch, environment, and API credentials.
---

# Jenkins 自动发布

通过 Jenkins REST API 触发参数化构建、实时跟踪进度、失败时抓取日志分析原因。所有可发布目标集中在一个配置文件里，按名称区分。

## 脚本与配置位置

- 脚本：`~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py`
- 配置：`~/.claude/skills/jenkins-deploy/config.json`（脚本会**自动找到**这份配置，无需每次写 `--config`）

脚本仅用 Python 标准库，无需 pip 安装。Jenkins 侧需要账号的 API Token（右上角用户名 → 配置 → API Token）。

## 配置文件字段

| 字段 | 含义 |
|------|------|
| `jenkins.baseUrl` | Jenkins 地址，如 `https://ci.test.ybm100.com` |
| `jenkins.credentials` | 凭证表，`名称 → { username, apiToken }`，可配多个 |
| `jenkins.timeout` / `pollInterval` | 单次请求超时秒数 / 轮询间隔秒数 |
| `jenkins.logDir` | 失败日志保存目录，默认当前目录（空值/未配时） |
| `deploys[].name` | 任务名称，发布时用它指定 |
| `deploys[].job` | Jenkins Job 路径，如 `job/INVT/job/INVT-BE-Open/job/ykq-supply-center` |
| `deploys[].environment` | 环境标签，仅用于确认展示 |
| `deploys[].branch` | 分支，作为构建参数传入（参数名由 `branchParam` 决定） |
| `deploys[].credential` | 引用 `jenkins.credentials` 里的哪个凭证 |
| `deploys[].branchParam` | 分支对应的构建参数名，默认 `branch`；Job 不接收分支参数则设为 `null` |
| `deploys[].params` | 额外构建参数（key-value），key 必须与 Job 定义的参数名一致 |

## 工作流程

### 1. 明确发布目标

```bash
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py list
```

用户可能只说「发布 supply-center 到 test」，据此匹配到对应 `name`，有歧义用 `list` 结果确认。

### 2. 发布前确认（必须）

执行前打印将要发布的环境和分支，**明确征得用户同意**：

```bash
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py show <name>
```

把「环境 / 分支 / Job」展示给用户，等确认。**未确认前绝不带 `--yes` 执行 `deploy`。**

如需核对 Job 实际接收的参数名（`branchParam` / `params` 的 key 要与 Job 一致），用 `params <name>` 查看。

### 3. 触发并跟踪进度（默认非阻塞，可随时中止）

`deploy` 不带 `--no-wait` 时会进入轮询循环、一直阻塞到构建结束才返回。而 Claude 执行 Bash 命令是同步的——阻塞期间无法再执行 `stop`，用户中途喊停只能等构建跑完。因此**默认用 `--no-wait` 触发并立即返回**（它会等构建离开队列、拿到构建号后返回）：

```bash
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py deploy <name> --yes --no-wait
```

触发后会打印 `构建 # N 已开始`，记下这个构建号 `N`。

**重要：不要用 `deploy --follow` 来「盯着」构建。** `deploy` 命令永远是「触发 + 跟踪」一体，`--follow` 会**再触发一次新构建**（造成重复发布）。要盯结果，用下面独立的 `watch` 命令。

#### 接收发布结果（非阻塞 + 可随时停止）

想让 Claude 在构建结束时**自动收到结果**，同时中途还能响应 `stop`，就把 `watch` 用 **Bash 后台**方式跑（`run_in_background: true`）：

```bash
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py watch <name> --build N
```

- `watch` 只跟踪、**不触发**新构建；构建结束时打印 `✓ 成功` / `✗ 失败` 并退出，Claude 会收到后台任务完成通知（含失败时的日志尾部）。
- 因为它是后台进程，前台始终空闲，用户随时喊停都能立刻执行 `stop`。
- 若构建早已结束，`watch` 会立即打印当前结果并退出，不会傻等。

触发后按需查看进度（这两条都是即时返回、不阻塞）：

```bash
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py status <name>
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py console <name> --tail 50
```

- 覆盖分支用 `--branch`；不带 `--yes` 只是演练打印，不会真正发布。
- 绝不用前台阻塞式 `deploy`（即不带 `--no-wait` 也不进后台）。

### 4. 失败分析

`deploy` 失败时自动打印日志尾部并保存完整日志到 `jenkins-console-<name>-<build>.log`（默认当前目录，可用 `jenkins.logDir` 指定）。之后读取日志定位原因（编译/测试失败、拉代码失败、环境缺失、依赖/镜像问题等），用中文简洁说明原因 + 修复方向，附关键报错行，不贴整篇日志。

随时可用 `status <name>`、`console <name> [--tail N]` 查看。

### 5. 中止构建

触发后想中止（如误发、卡死），用 `stop` 取消排队或终止运行中的构建。因为上一步是 `--no-wait` 非阻塞触发，构建进行中用户随时喊停，`stop` 都能立刻执行（不再被阻塞的 deploy 卡住）。

```bash
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py stop <name>           # 取消排队 + 中止最近一次运行中的构建
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py stop <name> --build N # 只中止指定构建号
```

注意：Ctrl-C 只是退出脚本，Jenkins 上的构建仍在继续，必须用 `stop` 才能真正中止。`stop` 中止的是「Jenkins 构建 / 部署过程」，不是「已部署运行的服务进程」。

## 常用命令速查

```bash
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py init          # 生成配置模板
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py list
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py show <name>
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py deploy <name> --yes --no-wait [--branch B]   # 默认：触发即返回，可随时 stop
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py status <name>
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py console <name> [--build N] [--tail 200]
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py watch <name> [--build N] [--follow]   # 后台跟踪已有构建直到结束（不触发新构建）
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py params <name>              # 查看 Job 已定义的构建参数
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py stop <name> [--build N]   # 中止排队/运行中的构建
```

## 常见错误

- **401/403**：`username`/`apiToken` 配错或过期，重新生成 Token。
- **触发成功但参数不生效**：Job 没开「参数化构建」，或 `branchParam`/`params` 的 key 与 Job 参数名不一致。
- **一直排队**：节点被占用或离线。
- **`job` 路径 404**：从浏览器地址栏 `/job/` 到 Job 名原样抄。

## 安全注意

- `apiToken` 是敏感凭证，`config.json` 不要提交到 git。
- 发布前必须二次确认；不带 `--yes` 的 `deploy` 永远是安全演练。
