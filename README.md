# pigeon

`pigeon` 是一个只通过共享目录通信的“命令借网执行”工具：

- 在 `gpu_m` 执行：`pigeon <cmd...>`
- 命令实际在 `cpu_m` 的 `pigeon worker` 上运行
- 双方共享同一路径文件系统，命令在发起时的 `cwd` 执行并直接产生产物

## 设计目标覆盖

- 执行等价：worker 在请求中的绝对 `cwd` 执行命令
- 默认交互：实时输出流、stdin 多轮输入、Ctrl-C 转发
- 会话并发：每次执行一个唯一 `session_id`，多会话互相隔离
- 同 cwd 串行：worker 对 `cwd` 做文件锁，同目录自动排队
- 退出码一致：客户端读取 worker 状态并以相同 exit code 退出
- 可配置缓存目录：必须设置 `PIGEON_CACHE`，命名空间由 `PIGEON_NAMESPACE` 控制

## 运行方式

离线环境建议直接使用仓库内脚本（无需 `pip install`）：

```bash
./bin/pigeon --help
```

如果要全局命令，可把 `bin/` 加到 `PATH`。

## 配置

至少需要能解析到 `cache`。推荐使用配置文件，环境变量作为覆盖。

### 1) 配置文件（推荐）

支持优先级顺序：

1. `--config /path/to/pigeon.toml`
2. `PIGEON_CONFIG=/path/to/pigeon.toml`
3. 默认路径 `/home/pgroup/pxd-team/workspace/fyh/pigeon/.pigeon.toml`
4. 兜底兼容：当前目录 `./.pigeon.toml` 或 `./pigeon.toml`

示例：

```bash
cat > .pigeon.toml <<'TOML'
cache = "/shared/path/pigeonCache"
namespace = "demo"
user = "fyh"

[worker]
max_jobs = 4
poll_interval = 0.2
debug = false

[remote_env]
HTTPS_PROXY = "http://proxy.example:8080"
HTTP_PROXY = "http://proxy.example:8080"
MY_TOKEN = "xxx"
TOML
```

### 2) 环境变量（覆盖配置文件默认值）

```bash
export PIGEON_CONFIG=/shared/path/pigeon.toml
export PIGEON_CACHE=/shared/path/pigeonCache
export PIGEON_NAMESPACE=my_team_or_user
export PIGEON_USER=my_user
```

说明：
- `cache/namespace/user`：环境变量优先于配置文件。
- `remote_env`：由配置文件注入到远端命令环境，优先级最高（覆盖本地同名环境变量和 worker 自身环境）。
- `worker.*`：作为 `pigeon worker` 默认参数，可被命令行参数覆盖。

目录结构示例：

```text
$PIGEON_CACHE/
  namespaces/
    <namespace>/
      sessions/
        <session_id>/
          request.json
          status.json
          stream.jsonl
          stdin.jsonl
          control.jsonl
          worker.claim
      locks/
        <sha256(cwd)>.lock
```

## 使用

在 `cpu_m` 启动 worker：

```bash
./bin/pigeon worker --max-jobs 4
```

需要排查交互问题时，可开启 worker debug：

```bash
./bin/pigeon worker --max-jobs 4 --debug
```

`--debug` 会打印关键节点（会话 claim、锁等待/获取、命令开始/结束）以及 stdin/stdout/stderr 的字节预览（hex + 文本）。
支持终端颜色高亮，不同语义（队列/锁/输入/输出/信号/成功/失败）使用不同颜色，便于快速定位问题。

在 `gpu_m` 执行命令（默认会按 `bash -lc` 执行）：

```bash
./bin/pigeon curl -I https://example.com
./bin/pigeon codex
./bin/pigeon 'read -p "name? " n; echo "hello $n"'
```

默认会在 worker 侧按 `bash -lc "<你的命令>"` 执行，所以常用场景直接 `pigeon <cmd...>` 即可，不再需要手写 `bash -lc`。
如果你已经写了 `pigeon bash -lc ...`，也继续兼容。

客户端会把 stdout/stderr（PTY 合流）实时打印到本地终端，并支持 Ctrl-C 转发。

关于环境变量展开：
- 对 `remote_env` 中的变量，`pigeon` 会在多参数模式下尽量修正本地 shell 的提前展开（例如 `pigeon HTTPS_PROXY=... echo $HTTPS_PROXY`），让结果更接近远端执行直觉。
- 需要完全按原始 shell 字符串控制时，仍建议单引号整体传入：`pigeon 'HTTPS_PROXY=... echo $HTTPS_PROXY'`。

## 本机模拟联调（单机）

开两个终端，使用同一个 `PIGEON_CACHE`：

终端 A（模拟 cpu_m）：

```bash
export PIGEON_CACHE=/tmp/pigeon-cache
export PIGEON_NAMESPACE=demo
./bin/pigeon worker --max-jobs 4
```

终端 B（模拟 gpu_m）：

```bash
export PIGEON_CACHE=/tmp/pigeon-cache
export PIGEON_NAMESPACE=demo
./bin/pigeon 'pwd; echo hi; sleep 1; echo done'
```

## 注意事项

- 仅依赖共享目录，不使用额外网络通道/数据库
- 若共享文件系统不支持跨机文件锁，需要替换为你们集群可用的锁方案
- 当前默认把客户端环境变量带到 worker 并覆盖同名变量
