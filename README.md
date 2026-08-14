# jenkins-deploy 技能

通过 Jenkins REST API 触发参数化构建、实时跟踪进度、失败时自动抓取日志分析原因。所有可发布目标集中在一份配置文件里，按名称区分，CLI 只依赖 Python 标准库、无需 pip 安装。

---

## 目录

1. [功能特性](#功能特性)
2. [目录结构](#目录结构)
3. [前置要求](#前置要求)
4. [快速开始](#快速开始)
5. [配置文件](#配置文件)
6. [命令参考](#命令参考)
7. [典型工作流程](#典型工作流程)
8. [安全说明](#安全说明)
9. [常见错误排查](#常见错误排查)
10. [已知限制 / 待完善](#已知限制--待完善)

---

## 功能特性

- **触发构建**：通过 Jenkins REST API 触发参数化构建，支持分支覆盖与额外参数。
- **实时跟踪**：轮询排队状态与构建进度，估算剩余时间，`--follow` 可实时滚动日志。
- **安全闸门**：不带 `--yes` 的 `deploy` 永远是演练，不会真正触发；发布前打印环境/分支供确认。
- **失败分析**：构建失败时自动打印日志尾部，并将完整日志落盘为 `.log` 文件。
- **中止构建**：`stop` 命令可取消排队中的构建、或终止正在运行的构建（误发 / 卡死时用）。
- **查看 Job 参数**：`params` 命令列出 Job 已定义的参数（名称/类型/默认值/可选值），避免 `params` 的 key 对不上。
- **零依赖**：仅用 Python 标准库（`urllib` / `argparse`），跨平台（含 Windows GBK 控制台适配）。
- **多凭证 / 多环境**：一个配置文件可管理多个 Jenkins 账号（dev/test/prod）与多个发布任务。

---

## 目录结构

```
jenkins-deploy/
├── SKILL.md                      # 技能定义（触发词 + 工作流 + 速查）
├── config.json                   # 实际配置（含凭证，勿提交 git）
├── assets/
│   └── config.template.json      # 配置模板（init 时生成）
├── scripts/
│   └── jenkins_deploy.py         # 发布 CLI（纯标准库实现）
└── evals/
    └── evals.json                # 评估用例
```

---

## 前置要求

- Python 3（任意 3.x，脚本仅用标准库）。
- Jenkins 账号的 **API Token**：右上角用户名 → 配置（Configure）→ API Token → 生成。
- 目标 Job 必须是**参数化构建**（脚本通过 `buildWithParameters` 触发）。
- 运行环境需能访问 Jenkins 地址（通常需在公司内网 / VPN）。

---

## 快速开始

> **路径说明**：下面命令里的 `~` 是「用户主目录」的简写，只在 **Git Bash / macOS / Linux** 下有效。
> **Windows 系统请使用全路径**（cmd / PowerShell 都不会展开 `~`），例如：
>
> ```bash
> python C:/Users/userName/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py init
> ```
>
> 把 `userName` 换成你自己的用户名即可；后续所有命令同理，把 `~/.claude/...` 替换成上面的全路径。

```bash
# 1. 生成配置模板（默认生成到技能目录，可用 --dir 指定）
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py init

# 2. 编辑 config.json：填写 baseUrl、credentials、deploys

# 3. 列出所有可发布任务
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py list

# 4. 查看某个任务详情（发布前确认用）
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py show supply-center-test

# 5. 演练（不带 --yes，只打印不触发）
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py deploy supply-center-test

# 6. 真正发布
python ~/.claude/skills/jenkins-deploy/scripts/jenkins_deploy.py deploy supply-center-test --yes
```

---

## 配置文件

脚本会**自动定位** `~/.claude/skills/jenkins-deploy/config.json`，无需每次传 `--config`（也可用全局 `--config` 覆盖）。

### 顶层结构

```jsonc
{
  "jenkins": { ... },   // 全局 Jenkins 连接信息
  "deploys": [ ... ]    // 发布任务列表
}
```

### `jenkins` 字段

| 字段 | 类型 | 含义 | 获取方式 |
|------|------|------|----------|
| `baseUrl` | string | Jenkins 地址，如 `https://ci.test.ybm100.com` | 浏览器打开 Jenkins 首页，取地址栏 `https://` 到域名部分（不含 `/job/...`） |
| `timeout` | int | 单次请求超时秒数（默认 30） | 自定，一般保持默认 |
| `pollInterval` | int | 轮询间隔秒数（默认 10） | 自定，一般保持默认 |
| `logDir` | string | 失败日志保存目录，默认当前目录（空值/未配时） | 自定，如 `E:\\IDEA_Object\\JenkinsFailLog` |
| `credentials` | object | 凭证表，`名称 → { username, apiToken }`，可配多个 | 见下方「参数获取方式详解」 |

### `deploys[]` 字段

| 字段 | 类型 | 含义 | 获取方式 |
|------|------|------|----------|
| `name` | string | 任务名称，发布时用它指定（建议含环境，如 `supply-center-test`） | 自定 |
| `job` | string | Jenkins Job 路径，如 `job/INVT/job/INVT-BE-Open/job/ykq-supply-center` | 浏览器打开该 Job 页面，从地址栏 `/job/` 起原样抄 |
| `environment` | string | 环境标签，仅用于确认展示 | 自定（dev/test/prod） |
| `branch` | string | 分支，作为构建参数传入（参数名由 `branchParam` 决定） | 仓库分支名，如 `refs/heads/test` |
| `credential` | string | 引用 `jenkins.credentials` 里哪个凭证 | 填 `credentials` 里的 key |
| `branchParam` | string/null | 分支对应的构建参数名，默认 `branch`；Job 不接收分支参数则设为 `null` | 用 `params <name>` 命令查看 Job 实际参数名 |
| `params` | object | 额外构建参数（key-value），key 必须与 Job 定义的参数名一致 | 用 `params <name>` 命令查看参数名与可选值 |

### 参数获取方式详解

几个需要从 Jenkins / Git 侧获取、不能凭空填的参数，获取方式如下。

#### 1. API Token（`credentials[].apiToken`）

1. 浏览器登录 Jenkins。
2. 右上角用户名 → 配置（Configure）→ **API Token** 区块 → 添加新 Token → 生成后复制。
3. `username` 要填**登录 User ID**（ASCII，通常是拼音/英文 ID），不是显示名/全名；中文用户名会导致 Basic Auth 编码异常返回 401。

#### 2. Job 路径（`deploys[].job`）

1. 浏览器打开该 Job 的构建页。
2. 地址栏 `/job/` 之后的部分原样抄下。例如
   `https://ci.test.ybm100.com/job/INVT/job/INVT-BE-Open/job/ykq-supply-center`
   → 取 `job/INVT/job/INVT-BE-Open/job/ykq-supply-center`（去掉域名和末尾 `/`）。

#### 3. 分支参数名与额外参数 key（`branchParam` / `params`）

`branchParam` 和 `params` 的 key **必须与 Job 定义的参数名完全一致**，否则参数不生效。两种获取方式：

- **用命令查**（推荐）：`python jenkins_deploy.py params <name>` 会列出该 Job 所有构建参数（名称 / 类型 / 默认值 / 可选值）。
- **在 Jenkins 页面看**：Job 页 → 配置（Configure）→ 勾选「参数化构建」→ 查看每个「参数」的名字。

例如 `params <name>` 输出里若分支参数叫 `GIT_BRANCH`，则 `branchParam` 填 `"GIT_BRANCH"`；若 Job 没有分支参数，就设 `null`。

### 示例

```json
{
  "jenkins": {
    "baseUrl": "https://ci.test.ybm100.com",
    "timeout": 30,
    "pollInterval": 10,
    "credentials": {
      "deploy-test": { "username": "deployer", "apiToken": "xxxxx" }
    }
  },
  "deploys": [
    {
      "name": "supply-center-test",
      "job": "job/INVT/job/INVT-BE-Open/job/ykq-supply-center",
      "environment": "test",
      "branch": "refs/heads/test",
      "credential": "deploy-test",
      "branchParam": "GIT_BRANCH",
      "params": { "SRV_ENV": "test" }
    }
  ]
}
```

---

## 命令参考

所有命令都支持全局参数 `--config PATH`（需放在子命令之前）。

### `init`
生成配置模板。
```bash
python jenkins_deploy.py init [--dir DIR]
```
默认生成到技能目录；`--dir` 可指定其他目录。

### `list`
列出所有可发布任务（名称 / 环境 / 分支 / Job）。
```bash
python jenkins_deploy.py list
```

### `show`
查看单个任务详情（发布前确认用）。
```bash
python jenkins_deploy.py show <name>
```

### `deploy`
触发构建并跟踪进度。
```bash
python jenkins_deploy.py deploy <name> [--branch B] [--yes] [--follow] [--no-wait]
```

| 选项 | 含义 |
|------|------|
| `--branch B` | 覆盖配置里的分支 |
| `--yes` | 确认发布；**不传则仅演练打印，不真正触发** |
| `--follow` | 实时滚动输出控制台日志 |
| `--no-wait` | 触发后立即返回，不等待结果 |

### `status`
查看最近一次构建状态。
```bash
python jenkins_deploy.py status <name>
```

### `console`
查看控制台日志。
```bash
python jenkins_deploy.py console <name> [--build N] [--tail LINES]
```

| 选项 | 含义 |
|------|------|
| `--build N` | 指定构建号（默认 `lastBuild`） |
| `--tail LINES` | 仅显示最后 N 行 |

### `watch`
跟踪已有构建直到结束（**不触发新构建**）。用于 `deploy --no-wait` 之后在后台接收结果。
```bash
python jenkins_deploy.py watch <name> [--build N] [--follow]
```

| 选项 | 含义 |
|------|------|
| `--build N` | 指定构建号（默认 `lastBuild`） |
| `--follow` | 实时滚动输出控制台日志 |

> 建议用 Bash 后台方式运行 `watch`（`run_in_background: true`）：构建结束会打印 `✓ 成功` / `✗ 失败` 并退出，Claude 收到后台完成通知；期间前台空闲，可随时 `stop`。若构建已结束，`watch` 会立即返回当前结果、不会傻等。

### `params`
查看 Job 已定义的构建参数（名称 / 类型 / 默认值 / 可选值 / 说明），用于对齐 `branchParam` 与 `params` 的 key。
```bash
python jenkins_deploy.py params <name>
```

### `stop`
中止排队或运行中的构建。
```bash
python jenkins_deploy.py stop <name> [--build N]
```

| 选项 | 含义 |
|------|------|
| `--build N` | 只中止指定构建号；不传则取消排队 + 中止最近一次运行中的构建 |

> 注意：Ctrl-C 只是退出脚本，Jenkins 上的构建仍在继续，需用 `stop` 真正中止。

---

## 典型工作流程

```bash
# 1. 用户说「发布 supply-center 到 test」→ 匹配 name
python jenkins_deploy.py list

# 2. 发布前确认（必须）
python jenkins_deploy.py show supply-center-test
#    将「环境 / 分支 / Job」展示给用户，等确认

# 3. 确认后触发（非阻塞，立即返回并打印构建号 N）
python jenkins_deploy.py deploy supply-center-test --yes --no-wait

# 3b. 后台跟踪到结束（Bash run_in_background: true），构建结束自动收到结果
python jenkins_deploy.py watch supply-center-test --build N

# 4. 成功后查看结果 / 失败后看日志
python jenkins_deploy.py status supply-center-test
python jenkins_deploy.py console supply-center-test --tail 200
```

构建失败时，`deploy` 会自动打印日志尾部，并把完整日志保存为
`jenkins-console-<name>-<build>.log`（默认当前工作目录，可用 `jenkins.logDir` 指定）。

---

## 安全说明

- **`apiToken` 是敏感凭证**，`config.json` 不要提交到 git（已提供 `.gitignore` 排除）。
- **发布前必须二次确认**；不带 `--yes` 的 `deploy` 永远是安全演练。
- 凭证支持按任务隔离（dev/test/prod 用不同账号），避免测试凭证触及生产。

---

## 常见错误排查

| 现象 | 原因 | 处理 |
|------|------|------|
| HTTP 401/403 | `username`/`apiToken` 配错或过期 | 重新生成 API Token |
| 触发成功但参数不生效 | Job 未开「参数化构建」，或 `branchParam`/`params` 的 key 与 Job 参数名不一致 | 对齐参数名 / 勾选参数化构建 |
| 一直排队 | 节点被占用或离线 | 到 Jenkins 查看 executor / 节点状态 |
| `job` 路径 404 | Job 路径抄错 | 从浏览器地址栏 `/job/` 起原样抄 |
| 非参数化 Job 触发报 400 | 脚本只调 `buildWithParameters` | 当前版本需 Job 为参数化构建（见「待完善」） |

---

## 已知限制 / 待完善

1. **仅支持参数化 Job**：`trigger_build` 固定 POST 到 `buildWithParameters`，非参数化 Job 会返回 400。
2. `init` 的 argparse help 文案「默认当前目录」与实际行为（默认技能目录）不一致。

---

> 文档生成日期：2026-08-13
