# sharebox-tsnet

一个很小的 Go sidecar：用 [tsnet](https://tailscale.com/kb/1244/tsnet) 加入
Tailscale tailnet，并在本机 `127.0.0.1:1080` 提供 SOCKS5 代理。
ShareBox 通过这个代理访问对端的 `100.x` 地址，用户不需要单独安装 Tailscale。

## 构建

```powershell
cd tsnet
go build -trimpath -ldflags "-s -w" -o sharebox-tsnet.exe .
```

要求 Go 1.23.1+，依赖 `tailscale.com v1.80.3`。

## 运行

```powershell
$env:SHAREBOX_TS_AUTHKEY = "tskey-auth-..."
$env:SHAREBOX_TS_HOSTNAME = "sharebox-abc123"
.\sharebox-tsnet.exe
```

启动成功后标准输出会打印一行 JSON：

```json
{"ready":true,"version":"v2.2.0","listen":"127.0.0.1:1080","ip":"100.x.y.z","hostname":"sharebox-abc123"}
```

## 环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `SHAREBOX_TS_AUTHKEY` | 二选一 | Tailscale auth key |
| `SHAREBOX_TS_AUTHKEY_FILE` | 二选一 | 从文件读取 auth key（推荐，避免出现在进程环境里） |
| `SHAREBOX_TS_HOSTNAME` | 否 | tailnet 里的设备名，默认 `sharebox-<随机>` |
| `SHAREBOX_TS_STATE` | 否 | 状态目录，默认 `%LOCALAPPDATA%\ShareBox\tsnet` |
| `SHAREBOX_TS_LISTEN` | 否 | 本地 SOCKS5 监听地址，默认 `127.0.0.1:1080` |
| `SHAREBOX_TS_STATUS_FILE` | 否 | 把就绪 JSON 写入该文件 |
| `SHAREBOX_TS_CONTROL_URL` | 否 | 自建 Headscale 时使用 |
| `SHAREBOX_TS_VERBOSE` | 否 | 设为 `1` 输出 tsnet 日志 |

## 安全说明

- **auth key 不编译进二进制**，只从环境变量/文件读取；仓库和 CI 日志里都不能出现。
- SOCKS5 只监听 `127.0.0.1`，**没有认证**：本机任何进程都能用它访问 tailnet。
  这是有意的本地信任模型；不要把监听地址改成 `0.0.0.0`。
- 共用一把 reusable key 时，必须在 Tailscale ACL 里用 `tag:sharebox` 限制
  只能访问 ShareBox 端口，并定期轮换。详见 `docs/TSNET.md`。
- 状态目录包含节点密钥，权限应为当前用户私有，不要提交到仓库。
