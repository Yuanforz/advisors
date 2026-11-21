# advisors

[English](README.md)

让干活的编程 agent 像用自带子 agent 一样请教其他模型。

`advisors` 由一条命令和一个本机守护进程组成。干活的 agent（Claude Code，或者任何能跑 shell 命令的 agent）用它向配置好的顾问提问。顾问有两种接法：

- 通过 [pi](https://github.com/earendil-works/pi)（`pi --mode rpc`），可以接 Anthropic、Vertex AI、OpenAI 兼容接口，以及 pi 扩展加进来的服务商；
- 通过 [Codex CLI](https://github.com/openai/codex) 的 app-server。

调用方每次自己决定是前台等结果还是放后台。一轮在跑的时候，可以看进度、插话、停止。每一轮都会存到磁盘上。

```bash
advisors list                                        # 谁能用、额度、并发、能力
advisors ask gpt "这个函数有什么问题？" --files src/a.py --effort low
advisors ask --session gpt-1121-0712-ab12 "展开说第二点"
advisors status gpt-1121-0712-ab12:2                 # 阶段、最后一个后端事件、工具轨迹、正文末尾
advisors steer gpt-1121-0712-ab12:2 "只看错误处理"
advisors stop gpt-1121-0712-ab12:2
advisors log gpt-1121-0712-ab12
```

## 解决什么问题

在 agent 里找别的模型要第二意见，通常是拼一长串 `pi -p …` 或 `codex exec …`，然后干等，回答最后淹没在终端输出里。advisors 给 agent 提供了这些：

- **所有后端一个接口。** 不管顾问走 Codex 还是 pi，会话、档位、附件、工具限制的用法都一样。
- **每次调用自己选前台还是后台。** 放后台时，输出文件就是实时进度：阶段、工具调用，还有一行心跳，写明后端最后一次有动静是什么时候。结束时由调用方自己的工具发通知。没有 MCP，也没有固定超时替调用方做决定。
- **插话和停止。**
  - `steer` 把话送进正在跑的这一轮（Codex 用 `turn/steer`，pi 用 `steer`），并告诉你结果：已送达、排队，还是来不及了。
  - `stop` 或 SIGTERM 会干净地结束这一轮，之后会话还能接着用。
- **调用方绕不过的规则：**
  - 按组限制并发，超出的排队；
  - 付费顾问有熔断：每轮最多 40 次工具调用、30 分钟；
  - 被预算拦下、订阅额度用完，各有单独的退出码；
  - 幂等键保证重试不会重复扣钱。
- **记录。** 每一轮的题目、回答和元数据都存在 `~/.advisors/` 下。元数据包括请求、实际发出和后端确认的档位，耗时，token 或花费，以及剩余额度。
- **一个 skill**（`skill/SKILL.md`）：教 agent 什么时候该请教、怎么做盲审、怎么组织多家讨论。

设计见 [DESIGN.md](DESIGN.md)（英文）。

## 环境要求

- macOS 或 Linux，Python 3.12 及以上。只用标准库，在 3.13 和 3.14 上测过。
- 至少装好一个后端，并且已经登录：
  - `pi-rpc` 顾问需要 [pi](https://github.com/earendil-works/pi)，在 pi 1.4.0 上测过，要用的服务商先在 pi 里配好；
  - `codex-app-server` 顾问需要 [Codex CLI](https://github.com/openai/codex)，在 0.171.0 上测过。

advisors 用你自己的凭据驱动这些官方命令行工具，自己不碰任何 API key 或令牌。各家服务请按其条款使用。

## 安装

```bash
git clone https://github.com/Yuanforz/advisors.git && cd advisors
pipx install .            # 或 pip install --user .，也可以不装，直接用仓库里的 bin/advisors

mkdir -p ~/.advisors
cp examples/advisors.toml ~/.advisors/advisors.toml   # 然后编辑：每个顾问一张表
advisors list
```

在 Claude Code 里用，再装上 skill：

```bash
mkdir -p ~/.claude/skills/advisors
ln -s "$PWD/skill/SKILL.md" ~/.claude/skills/advisors/SKILL.md
```

[`pi-extensions/`](pi-extensions/README.md) 里有两个可选的 pi 扩展：

- **`read-many.ts`** 加一个 `read_many` 工具，一次读多个文件，每个文件的读法和 `read` 完全一样。有的服务商每一步工具调用都要走一次完整的模型请求，对它们最有用。要启用，在顾问的 `read_tools` 里加上 `"read_many"`。
- **`vertex-budget.ts`** 给 pi 的 google-vertex 服务商加硬性花费上限。advisors 能识别它的拦截（退出码 4）。顾问设了 `quota = "vertex-budget"` 时，`list` 里会显示剩余预算。

## 配置顾问

顾问表放在 `~/.advisors/advisors.toml`，也可以用 `$ADVISORS_CONFIG` 指定别的文件。每个字段的说明见 [`examples/advisors.toml`](examples/advisors.toml)。

`description` 很重要：`advisors list` 会把它显示给调用方的模型，模型靠它来挑顾问。所以要写清楚这个顾问擅长什么、花多少钱、什么时候别用。

守护进程发现文件变了会重新读取。改坏了会继续用上一份能用的表，并在 `advisors list` 里显示错误。

## 顾问能看到什么

- **能看到的内容：** 题目、`--files` 附上的文件；用 `--tools read`（默认）时，还有只读工具能读到的一切。
  - pi 的读取工具**不限**于工作目录。
  - Codex 的只读沙箱能读你这个用户能读的所有文件。
  - 所以要把顾问当成一个对你的账号有读权限的同事。
- **包括别家顾问的回答：** 它们存在 `~/.advisors/sessions/` 下，也读得到。做盲审时用 `--tools none`，材料用 `--files` 附上。
- **不往工作目录写东西：** 运行记录都在 `~/.advisors/` 下，目录权限 0700，socket 权限 0600。
- **Codex 顾问：** 用 `read-only` 沙箱和 `approvalPolicy: never`，服务端发来的批准请求一律拒绝。
- **pi 顾问：**
  - 只给 `read_tools` 里列出的工具；
  - 关掉 pi 的上下文文件、skill、提示模板和 MCP；
  - pi 扩展不关，所以预算拦截和自定义服务商照常工作。

## 插件

`~/.advisors/plugins/` 里的 Python 文件可以增加额度来源，用在 advisors 自己读不到额度的服务商上：

- 插件里定义 `QUOTA_SOURCES = {"名字": 异步函数}`；
- 顾问用 `quota = "名字:参数"` 来启用。

接口说明在 [`advisors/plugins.py`](advisors/plugins.py)。插件以你的权限在守护进程里运行。

## 测试

```bash
python3 -m unittest discover -s tests -t .
```

- 测试用 pi 的 `fauxProvider` 假模型和临时数据目录，不花钱。需要 `pi` 在 PATH 上，没有 pi 的测试会跳过。
- `tests/test_budget.py` 里有一组对照测试：向一个不存在的 Vertex 项目发一次请求，收到 403，不花钱。它需要联网和 Google 默认应用凭据，所以只在设了 `ADVISORS_NETWORK_TESTS=1` 时运行。
- `tests/manual/live_check.py ADVISOR` 用真实顾问依次测前台、后台、插话、停止和续接，会花真实额度或真钱。

## 状态

早期版本（0.1）。

**已经验证过的：**

- 零成本测试套件；
- Codex app-server 的握手；
- Codex 的 `turn/steer` 在同一轮里生效（实测一次）；
- Codex 额度用完时报 `quota_exhausted` 并给出恢复时间（用真实事件测过）；
- pi 的 `steer` 和 `abort`（在订阅制的 pi 服务商上测过）。

**还没有实测的：**

- Codex 的 `turn/interrupt`；
- 付费 pi 服务商（Anthropic、Vertex）在一轮中途插话。

## 许可证

MIT，见 [LICENSE](LICENSE)。
