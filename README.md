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
如果要跨 `gpu_m/cpu_m` 共享同一份配置，先设置：

```bash
export PIGEON_CONFIG_DIR=/data/shared/pigeon-config
```

设置后默认配置文件会变成：`/data/shared/pigeon-config/config.toml`

首次初始化（如果文件不存在会创建并写入默认值）：

```bash
pigeon config init
```

不手动执行 `config init` 也可以：首次运行 `pigeon worker ...` 或 `pigeon <cmd...>` 时，也会自动创建同一份配置文件。
你也可以直接刷新并补齐配置缺失项：

```bash
pigeon --config /home/pgroup/pxd-team/workspace/fyh/pigeon/.pigeon.toml
```

这条命令会同时把该文件设为“当前全局配置路径”（后续不带 `--config` 的命令都会用它）。

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
poll_interval = 0.2
debug = false
```

如果你已经设置了环境变量 `PIGEON_CACHE/PIGEON_NAMESPACE/PIGEON_USER/PIGEON_ROUTE/PIGEON_WORKER_ROUTE`，初始化时会优先使用这些环境变量值。

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
pigeon worker --route cpu-pool-a --max-jobs 4 --poll-interval 0.2
```

调试时打开 debug 日志（带颜色）：

```bash
pigeon worker --route cpu-pool-a --max-jobs 4 --poll-interval 0.2 --debug
```

worker 运行中会每秒自动重读一次配置文件（`route`/`worker.poll_interval`/`worker.debug`），修改共享配置后无需重启 worker。
如果你通过命令行显式传了 `--route`、`--poll-interval` 或 `--debug`，该项以命令行为准，不会被配置文件覆盖。

## 5. 在 gpu_m 执行命令

在 `gpu_m` 的任意项目目录执行：

```bash
cd /data/project/repo
pigeon pwd
pigeon ls --color=auto
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

## 7. 配置优先级（明确规则）

配置文件路径优先级：

1. `--config /path/to/file.toml`
2. `PIGEON_CONFIG=/path/to/file.toml`
3. `PIGEON_DEFAULT_CONFIG=/path/to/default.toml`
4. `PIGEON_CONFIG_DIR=/path/to/dir`（实际文件是 `/path/to/dir/config.toml`）
5. `~/.config/pigeon/active_config_path` 指向的路径（由 `pigeon --config FILE` 或 `pigeon config use FILE` 设置）
6. 默认 `~/.config/pigeon/config.toml`

业务参数优先级：

1. `cache`：`PIGEON_CACHE` > `config.cache`
2. `namespace`：`PIGEON_NAMESPACE` > `config.namespace` > `config.user` > `$USER` > `default`
3. client `route`：`--route` > `PIGEON_ROUTE` > `config.route`
4. worker `route`：`worker --route` > `PIGEON_WORKER_ROUTE` > `PIGEON_ROUTE` > `config.worker.route` > `config.route`
5. 远端环境变量：`config.remote_env.*` 覆盖同名本地环境变量和 worker 环境变量

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

如果你需要使用非默认配置文件，所有命令都可加 `--config`：

```bash
pigeon config --config /tmp/pigeon.toml show --effective
pigeon worker --config /tmp/pigeon.toml --route cpu-pool-a
pigeon --config /tmp/pigeon.toml curl -I https://example.com
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
