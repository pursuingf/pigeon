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

必须：

```bash
export PIGEON_CACHE=/shared/path/pigeonCache
```

可选（默认使用 `$USER`）：

```bash
export PIGEON_NAMESPACE=my_team_or_user
```

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

在 `gpu_m` 执行命令：

```bash
./bin/pigeon curl -I https://example.com
./bin/pigeon bash -lc 'read -p "name? " n; echo "hello $n"'
```

客户端会把 stdout/stderr（PTY 合流）实时打印到本地终端，并支持 Ctrl-C 转发。

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
./bin/pigeon bash -lc 'pwd; echo hi; sleep 1; echo done'
```

## 注意事项

- 仅依赖共享目录，不使用额外网络通道/数据库
- 若共享文件系统不支持跨机文件锁，需要替换为你们集群可用的锁方案
- 当前默认把客户端环境变量带到 worker 并覆盖同名变量
