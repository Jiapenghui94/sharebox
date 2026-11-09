# tsnet 内嵌方案（2.2.0）

## 为什么

现在异地使用要求两台电脑都安装 Tailscale 客户端。目标是让 ShareBox 自带
Tailscale 能力：装完即用，用户不需要单独装客户端。

## 架构

```
ShareBox (Python)  --HTTP-->  127.0.0.1:1080 SOCKS5  --tsnet-->  100.x peer:8787
                                      ^
                                      |
                              sharebox-tsnet.exe
                                      |
                                 Tailscale tailnet
```

- `sharebox-tsnet.exe` 用 tsnet 加入 tailnet，拿到自己的 `100.x` 地址；
- 在本机回环地址开一个 SOCKS5 代理；
- ShareBox 的 HTTP 客户端对 `100.x` 目标走这个代理；
- 对端仍然是普通的 ShareBox 节点，监听 `8787`。

## 为什么不改系统路由

tsnet 是用户态网络栈，不需要安装 TUN 驱动、不需要管理员权限，也不会改动
系统路由表。代价是只有显式走代理的流量才会进入 tailnet。

## auth key 策略

### 方案 1：共用 reusable key（零操作）

- 一把 `tskey-auth-...`，带 `tag:sharebox`，可复用；
- 安装时写入 `%LOCALAPPDATA%\ShareBox\tsnet\authkey`（权限 0600）；
- sidecar 用 `SHAREBOX_TS_AUTHKEY_FILE` 读取；
- 风险：key 可被从安装包/配置中提取。必须配合 ACL 和轮换。

### 方案 2：每台设备交互登录（更安全）

- 不内置 key；
- 首次启动由 sidecar 输出登录 URL，用户在浏览器授权；
- 每台新设备需要一次人工操作。

当前先实现方案 1 的源码结构，key 不进入仓库；发布时从 GitHub Secret 注入。

## ACL 示例

```json
{
  "tagOwners": {
    "tag:sharebox": ["autogroup:admin"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["tag:sharebox"],
      "dst": ["tag:sharebox:8787"]
    }
  ]
}
```

只允许 `tag:sharebox` 之间访问 8787，不能访问 tailnet 里的其他设备。

## 构建与签名

- sidecar 源码在 `tsnet/`，由 GitHub Actions 的 `build-tsnet` 构建；
- **不要把预编译的 exe 提交到仓库**，否则 SignPath 会认为产物无法从源码复现；
- 2.2.0 打包时，安装器把 CI 产出的 `sharebox-tsnet.exe` 一起纳入 payload；
- 正式发布时 sidecar 也要签名（或至少随已签名的安装包分发）。

## 集成步骤（待做）

1. GitHub Secret 增加 `SHAREBOX_TS_AUTHKEY`（仅打包签名版时使用）；
2. 安装器把 key 写到 `%LOCALAPPDATA%\ShareBox\tsnet\authkey`；
3. ShareBox 启动时拉起 `sharebox-tsnet.exe`，读状态文件拿到 `100.x`；
4. HTTP 客户端对 `100.x` 走 SOCKS5；
5. 控制面板显示“Tailscale 已连接 / 未连接”；
6. 两台机器异地实测。
