# pigeon

`pigeon` 让你在 `gpu_m` 执行 `pigeon <cmd...>`，把命令投递到 `cpu_m` 执行。
通信只使用共享目录（`pigeonCache`），不使用额外网络通道。

## 1. 环境准备

以下条件必须满足：

1. `gpu_m` 和 `cpu_m` 能访问同一个共享目录（例如 `/data/shared/pigeon-cache`）。
2. `gpu_m` 和 `cpu_m` 的工作代码目录路径一致（例如都能访问 `/data/project/repo`）。
3. 两台机器都有 Python 3.9+。

检查 Python 版本：

```bash
python3 --version
```

## 2. 安装命令（一次）

在仓库根目录执行：

```bash
cd /data/pxd-team/workspace/fyh/pigeon
python3 -m pip install --user -e .
```

把 `~/.local/bin` 加入 PATH（如果还没有）：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

确认命令可用：

```bash
pigeon --help
```

## 3. 写入全局配置（一次）

默认配置文件路径：`~/.config/pigeon/config.toml`
如果要跨 `gpu_m/cpu_m` 共享同一份配置，直接设置“当前配置文件路径”：

```bash
pigeon config path /data/shared/pigeon-config/config.toml
```

如果你习惯用环境变量，`PIGEON_CONFIG` 和上面的命令是同一个语义：

```bash
export PIGEON_CONFIG=/data/shared/pigeon-config/config.toml
```

首次初始化（如果文件不存在会创建并写入默认值）：

```bash
pigeon config init
```

不手动执行 `config init` 也可以：首次运行 `pigeon worker ...` 或 `pigeon <cmd...>` 时，也会自动创建同一份配置文件。
你也可以直接刷新并补齐配置缺失项：

```bash
pigeon config refresh
```

把当前配置路径切换到另一个文件：

```bash
pigeon config path /home/pgroup/pxd-team/workspace/fyh/pigeon/.pigeon.toml
```

说明：`--config` 参数已移除，统一改为 `pigeon config path <FILE>`。

查看当前生效路径：

```bash
pigeon config path
```

`pigeon config init` 首次创建时会写入这些默认值：

```toml
cache = "/tmp/pigeon-cache"
namespace = "$USER"
user = "$USER"

[worker]
max_jobs = 4
poll_interval = 0.05
debug = false
```

如果你已经设置了环境变量 `PIGEON_CACHE/PIGEON_NAMESPACE/PIGEON_USER/PIGEON_ROUTE/PIGEON_WORKER_ROUTE/PIGEON_WORKER_MAX_JOBS/PIGEON_WORKER_POLL_INTERVAL/PIGEON_WORKER_DEBUG`，初始化时会写入这些环境变量值。

写入最小可用配置（把路径和路由改成你的真实值）：

```bash
pigeon config set cache /data/shared/pigeon-cache
pigeon config set namespace fyh
pigeon config set route cpu-pool-a
```

如果你需要固定远端代理，写入 `remote_env`（远端优先级最高）：

```bash
pigeon config set remote_env.HTTPS_PROXY http://proxy.example:8080
pigeon config set remote_env.HTTP_PROXY http://proxy.example:8080
```

查看文件配置和生效值：

```bash
pigeon config show --effective
pigeon config refresh
```

## 4. 在 cpu_m 启动 worker

在 `cpu_m` 执行：

```bash
pigeon worker --route cpu-pool-a --max-jobs 4 --poll-interval 0.05
```

调试时打开 debug 日志（带颜色）：

```bash
pigeon worker --route cpu-pool-a --max-jobs 4 --poll-interval 0.05 --debug
```

worker 运行中会每秒自动重读一次配置文件（`route`/`worker.poll_interval`/`worker.debug`），修改共享配置后无需重启 worker。
如果你通过命令行显式传了 `--route`、`--poll-interval` 或 `--debug`，该项以命令行为准，不会被配置文件覆盖。

## 5. 在 gpu_m 执行命令

在 `gpu_m` 的任意项目目录执行：

```bash
cd /data/project/repo
pigeon pwd
pigeon ls
pigeon curl -I https://example.com
```

交互命令示例：

```bash
pigeon codex
```

复合 shell 命令示例：

```bash
pigeon 'read -p "name? " n; echo "hello $n"'
```

临时覆盖路由（本次命令有效）：

```bash
pigeon --route cpu-pool-b curl -I https://example.com
```

`pigeon` 在发起请求前会先检查是否有匹配 route 的活跃 worker。默认最多等待 3 秒，超时直接报错并退出（不会无限卡住）。
你可以按命令覆盖等待时间：

```bash
pigeon --wait-worker 0.5 curl -I https://example.com
```

## 6. 退出码对齐验证

`pigeon` 会使用远端命令的 exit code 退出。

示例：

```bash
pigeon 'exit 7'
echo $?
```

`echo $?` 会输出 `7`。

## 7. 配置路径规则

配置文件路径只有一个“当前值”：

1. `pigeon config path /path/to/file.toml` 会设置并持久化当前路径。
2. `PIGEON_CONFIG=/path/to/file.toml` 也表示当前路径（进程内生效，并会同步到持久化路径）。
3. 若两者都没设置过，默认 `~/.config/pigeon/config.toml`。

业务参数规则（统一以配置文件为准）：

1. 启动 `pigeon`/`pigeon worker`/`pigeon config show|init|refresh` 时，会先把环境变量同步写入当前配置文件。
2. 同步后，运行期读取配置时优先用配置文件值；只有配置文件缺失该项时才回退到环境变量。
3. client `route`：`--route` > `config.route`
4. worker `route`：`worker --route` > `config.worker.route` > `config.route`
5. 远端环境变量：`config.remote_env.*` 覆盖同名本地环境变量和 worker 环境变量

当前支持自动同步到配置文件的环境变量：

- `PIGEON_CACHE`
- `PIGEON_NAMESPACE`
- `PIGEON_USER`
- `PIGEON_ROUTE`
- `PIGEON_WORKER_ROUTE`
- `PIGEON_WORKER_MAX_JOBS`
- `PIGEON_WORKER_POLL_INTERVAL`
- `PIGEON_WORKER_DEBUG`

性能开关（默认快路径）：

- 默认 `append_jsonl` 不做每条 `fsync`，交互延迟更低。
- 如需最强落盘一致性，可设置 `PIGEON_APPEND_FSYNC=always`（会明显变慢）。

远端环境变量来源规则：

1. 命令执行时不会转发 `gpu_m` 当前 shell 的环境变量。
2. 基础环境来自 `cpu_m` worker 进程自身环境。
3. `config.remote_env.*` 会覆盖同名变量（用于显式注入代理等）。
4. 如需每次命令前静默加载 `~/.bashrc`（不输出任何提示文本），设置：

```bash
export PIGEON_SOURCE_BASHRC=1
```

## 8. 远端环境变量与 shell 展开

推荐写法（避免本地 shell 提前展开）：

```bash
pigeon 'HTTPS_PROXY="http://proxy.example:8080" echo $HTTPS_PROXY'
```

如果你已经把代理写在 `remote_env.HTTPS_PROXY`，直接执行：

```bash
pigeon 'echo $HTTPS_PROXY'
```

## 9. 多 CPU / 多 GPU 部署方式

目标：让每组 `gpu_m` 只投递到指定 `cpu_m` worker。

示例：

1. 在 CPU A 上启动：

```bash
pigeon worker --route cpu-pool-a
```

2. 在 CPU B 上启动：

```bash
pigeon worker --route cpu-pool-b
```

3. 在 GPU 组 A 上设置：

```bash
pigeon config set route cpu-pool-a
```

4. 在 GPU 组 B 上设置：

```bash
pigeon config set route cpu-pool-b
```

这样 worker 只会消费匹配自己 route 的会话。

## 10. 常用排障命令

查看配置文件路径：

```bash
pigeon config path
```

查看可配置键：

```bash
pigeon config keys
```

只看键名：

```bash
pigeon config keys --short
```

删除一个配置键：

```bash
pigeon config unset remote_env.HTTPS_PROXY
```

查看 worker 详细事件（启动/claim/锁/输入输出预览/退出码）：

```bash
pigeon worker --debug
```

只检查是否有可用 worker（不执行命令）：

```bash
pigeon --wait-worker 0.5 true
```

如果你需要临时使用另一份配置文件，先切换路径再执行命令：

```bash
pigeon config path /tmp/pigeon.toml
pigeon config show --effective
pigeon worker --route cpu-pool-a
pigeon curl -I https://example.com
```

## 11. 会话目录结构

`cache` 路径下会生成：

```text
<cache>/namespaces/<namespace>/sessions/<session_id>/
  request.json
  status.json
  stream.jsonl
  stdin.jsonl
  control.jsonl
  worker.claim

<cache>/namespaces/<namespace>/workers/<worker_host>-<worker_pid>.json
  # worker heartbeat (用于客户端判断是否有可用 worker)
```

同一 `cwd` 会使用锁文件串行执行：

```text
<cache>/namespaces/<namespace>/locks/<sha256(cwd)>.lock
```
