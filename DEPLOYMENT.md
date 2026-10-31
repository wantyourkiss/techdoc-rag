# 部署与公网入口

## 单进程与持久数据

Python 3.11，`uvicorn --workers 1`。同一目录只允许一个应用所有者；本地向量索引由持久快照在启动时重建，单纯备份内存索引没有意义。SQLite、原件、快照和导入日志应一起保留。密钥由环境变量或受保护的环境文件提供。

默认 Compose 使用命名卷 `rag_data` 和非 root 用户 10001；首次运行自动初始化目录。切勿执行 `docker compose down -v` 来更新已有系统。更新前停服备份，启动后检查 `/health`、认证与引用。

## HTTPS 代理示例

下面是通用 Caddy 示例，替换示例域名，按实际服务监听调整上游。容器上游为宿主机映射的回环端口；如果 Caddy 也在容器内，应配置受控的容器网络与服务地址。

```caddyfile
rag.example.com {
    request_body {
        max_size 9MiB
    }
    @blocked path /diagnostics /diagnostics/* /docs /docs/* /redoc /redoc/* /openapi.json
    respond @blocked 404
    reverse_proxy 127.0.0.1:8765 {
        flush_interval -1
        header_up X-Techdoc-Client-IP {http.request.remote.host}
        header_up X-Techdoc-Proxy-Auth {$TECHDOC_PROXY_AUTH_TOKEN}
        transport http {
            response_header_timeout 300s
        }
    }
}
```

使用可验证的浏览器证书。远程上游应采用 HTTPS 并验证来源证书；示例的 HTTP 上游只位于同主机回环。Basic 认证由应用处理，不要在代理中叠加不一致的第二套认证。不要缓存认证响应或自动重试 POST。

使用代理时，为应用和 Caddy 分别设置相同的至少 32 字符 `TECHDOC_PROXY_AUTH_TOKEN`，确保来源传递启用；缺少该配置时，应用只能按连接对端识别来源，多个访客可能共享代理地址的失败额度。该来源凭据不替代浏览器密码。

## IPv4 云入口 + IPv6 NAS 直连

当 NAS 只有公网 IPv6、访客可能只有 IPv4 时，可采用：

```text
IPv4 客户端 → 云主机 TCP 18443 → 云回环 18765
                                  ↓ SSH 反向隧道
IPv6 客户端 ─────────────────→ NAS HTTPS 18443 → RAG
```

同一域名的 A 指向云主机，AAAA 指向 NAS，两个入口使用相同的 HTTPS 端口。TLS 在 NAS 终止，云主机只透传 TCP，证书私钥无需复制到云主机。若使用标准 443，需要确认两端端口占用与防火墙规则。

NAS 的常驻客户端命令形态：

```bash
ssh -NT -o BatchMode=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/private/known_hosts \
  -i /private/tunnel_key \
  -R 127.0.0.1:18765:127.0.0.1:18443 tunnel-user@CLOUD_HOST
```

云端使用单独隧道账号：仅公钥认证、`AllowTcpForwarding remote`、`PermitListen 127.0.0.1:18765`、`PermitOpen none`、`MaxSessions 0`，禁止代理转发、TTY、X11 和 Unix socket 转发。authorized_keys 可同时限制为 `restrict,port-forwarding,permitlisten="127.0.0.1:18765"`。修改 SSH 前先验证配置，保留现有管理连接。

云端可用 systemd socket activation 的 `ListenStream=0.0.0.0:18443` 与 `systemd-socket-proxyd --connections-max=64 127.0.0.1:18765` 完成 TCP 透传；为 NAS 客户端设置心跳及 `Restart=always`。不向公网开放 18765，不使用 root 口令作常驻隧道凭据。

普通 socket-proxyd 会丢失客户端地址。启用按来源的认证防护前，应先保留来源；本项目提供 `scripts/ipv4_proxy.py`，由同一个 systemd socket 启动（ExecStart 为 Python 加脚本绝对路径）。它限制 64 个连接，向私有回环上游发送 PROXY v1，TLS 仍在 NAS 终止。

NAS Caddy 需支持内置 proxy_protocol 模块。全局配置示例：

```caddyfile
{
    servers {
        listener_wrappers {
            proxy_protocol {
                timeout 2s
                allow 127.0.0.1/32
                fallback_policy reject
            }
            tls
        }
    }
}
```

仅信任隧道的回环来源；未知来源发来的 PROXY 头必须拒绝，普通 IPv6 TLS 连接仍可使用。先验证并更新 NAS 配置，再切换云桥；回滚时先恢复云端普通 TCP 代理。

应用与 Caddy 单独配置同一个至少 32 字符的 `TECHDOC_PROXY_AUTH_TOKEN`，保存在受保护的环境配置中。Caddy reverse_proxy 必须覆盖而非追加：`header_up X-Techdoc-Client-IP {http.request.remote.host}` 和 `header_up X-Techdoc-Proxy-Auth {$TECHDOC_PROXY_AUTH_TOKEN}`。该 token 只用于来源声明，不替代浏览器认证。Caddy 若通过环境读取它，需给 Caddy 服务单独配置环境；不要将整个模型服务密钥文件共享给代理。应用只接受匹配 token 的来源声明，任意客户端 X-Forwarded-For 均不能用于绕过限流。

### 单一代理凭据来源与轮换

systemd 部署建议将代理 token 单独存放在 `/private/proxy-auth.env`，内容仅一行 `TECHDOC_PROXY_AUTH_TOKEN=...`，所有者限定为运行服务的账号，文件权限 0600。应用与 Caddy 的服务配置都添加 `EnvironmentFile=/private/proxy-auth.env`；应用原环境文件中移除重复条目，Caddyfile 使用上述环境占位符，保留 0600。Caddy 仅加载这份代理配置，不读取模型和浏览器凭据文件。Compose 部署需通过已有环境映射把同一来源传入容器。

轮换在维护窗口进行：确认没有未完成任务，备份受保护配置；停止应用与公网代理；原子更新这一个私密文件并保持 0600；在加载新 token 的环境中验证 Caddy 配置；启动两个服务并等待模型就绪；检查两个实际进程读取的代理值一致（只输出比较结果，不输出值），再执行三入口验收。失败则恢复旧文件、重新启动两个服务并复验。仅改文件不会改变已运行进程的环境。

### 限流容量的可用性取舍

认证失败状态最多保留 4096 个来源，过期后可释放。容量满时，尚未记录的来源会先收到 429，即使随后提供的密码本来正确；已有来源仍按各自失败窗口检查。这是防止状态无界增长的 fail-closed 取舍，可能影响正常用户，不是“所有情况下来源都互不影响”的保证。来源洪泛即可触发，不以代理 token 泄露为前提；公网代理覆盖客户端自带来源头。不要在生产上用来源洪泛测试容量。

### 三入口发布验收

本项目的 LAN HTTPS + 公网双栈部署使用以下发布门禁，替换地址和私有证书路径；浏览器凭据从受保护环境读取，不写进命令参数：

```bash
python -m scripts.verify_entrypoints \
  --lan-url https://LAN_HOST:8765 \
  --public-url https://rag.example.com:18443 \
  --lan-ca /private/lan-ca.pem \
  --output /private/entrypoint-health.json
```

LAN、IPv4、IPv6 必须分别通过证书验证、匿名 401 和 Basic 挑战、正确认证后的最小 `status=ready` 响应；每个返回的地址都检查，公网地址不得误用回环、私网或 IPv4-mapped IPv6。探测使用指定地址族的原始 HTTPS 连接，不经过环境 HTTP 代理，也不跟随重定向。任一失败返回非零退出码。

本项目 NAS 发布编排在更新版本指针前调用相同门禁，并将 `entrypoints_verified` 和分入口结果写入发布记录；静态资源身份另用 `public_assets_verified` 表示。通用最小包提供上述 CLI 和发布记录辅助函数，其他部署编排需显式接入，Compose 启动不会自动配置三入口。发布记录写入失败会恢复原版本指针及原结果文件。CI 使用隔离测试验证失败门禁，不保存 NAS 或公网登录凭据；真实三入口由发布机执行。健康探测也不能代替代理 token 的进程一致性检查或所有用户网络的可达性测试。

分别强制 IPv4 / IPv6 验证：证书可信、匿名 401、登录后页面与引用可读、SSE 中间事件及时到达、NAS 隧道重启后恢复。DNS、ECS 安全组和家庭 IPv6 防火墙均要按入口放行。

证书续期和 IPv6 DDNS 需要单独维护。手动 DNS-01 证书不会自动续期；IPv4 隧道无需依赖家庭公网 IPv6 地址固定。

官方参考：[OpenSSH 服务端配置](https://man.openbsd.org/sshd_config)、[systemd-socket-proxyd](https://www.freedesktop.org/software/systemd/man/latest/systemd-socket-proxyd.html)、[Caddy 反向代理](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。
