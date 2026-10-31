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
        transport http {
            response_header_timeout 300s
        }
    }
}
```

使用可验证的浏览器证书。远程上游应采用 HTTPS 并验证来源证书；示例的 HTTP 上游只位于同主机回环。Basic 认证由应用处理，不要在代理中叠加不一致的第二套认证。不要缓存认证响应或自动重试 POST。

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

分别强制 IPv4 / IPv6 验证：证书可信、匿名 401、登录后页面与引用可读、SSE 中间事件及时到达、NAS 隧道重启后恢复。DNS、ECS 安全组和家庭 IPv6 防火墙均要按入口放行。

证书续期和 IPv6 DDNS 需要单独维护。手动 DNS-01 证书不会自动续期；IPv4 隧道无需依赖家庭公网 IPv6 地址固定。

官方参考：[OpenSSH 服务端配置](https://man.openbsd.org/sshd_config)、[systemd-socket-proxyd](https://www.freedesktop.org/software/systemd/man/latest/systemd-socket-proxyd.html)、[Caddy 反向代理](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。
