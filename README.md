# WARP + Xray Google 分流

保留 Cloudflare WARP 官方客户端，在现有 Xray 服务端按域名分流 Google。适用于 Debian 12、13；更高版本需官方软件源支持。

```text
客户端 → VPS Xray ┬─ Google TCP → 本机 WARP SOCKS5 → Cloudflare
                 └─ 其他流量 → 原有路由
```

## 一键安装

服务器已有 curl 时执行：

```bash
curl -fsSL https://raw.githubusercontent.com/brian952700/warp-xray-debian/main/install.sh | sudo bash
```

root 用户也可以使用 `| bash`。极简系统先安装 curl：

```bash
sudo apt-get update && sudo apt-get install -y curl ca-certificates
```

如需先审阅脚本再运行：

```bash
curl -fsSL https://raw.githubusercontent.com/brian952700/warp-xray-debian/main/install.sh -o install-warp-xray.sh
less install-warp-xray.sh
sudo bash install-warp-xray.sh
```

安装器解析 main 的当前提交，从同一提交下载程序并核对 SHA256，安装到 `/usr/local/lib/warp-xray/releases/`，创建 `warp-xray` 命令。校验用于检查文件一致性，不是独立的发布签名。GitHub API 限流或网络错误会停止安装；不会自动切到不可信镜像。

只下载管理工具、不配置网络：

```bash
curl -fsSL https://raw.githubusercontent.com/brian952700/warp-xray-debian/main/install.sh | sudo bash -s -- --download-only
sudo warp-xray setup
```

## 设置向导

向导会询问：

1. 是否安装/复用 WARP、接受其条款并切换到本地代理模式。
2. 是否由面板/多文件管理；这类环境只导出待合并片段，不覆盖配置。
3. 普通单文件部署的 config 绝对路径、systemd 服务名、路由插入位置。
4. 是否补充缺失的嗅探配置、应用候选配置、清理已识别旧透明代理、启用健康检查。

应用前备份并通过当前 Xray 校验；应用时重启 Xray，有短暂断连。失败则尝试恢复原配置。已被其他程序修改的配置会拒绝覆盖。保留原默认出站、非 Google 路由及现有嗅探配置。

## 管理命令

```bash
sudo warp-xray status
sudo warp-xray setup
sudo warp-xray enable-health
sudo warp-xray disable-health
sudo warp-xray restore --plan /var/lib/warp-xray/plan-实际编号
```

连续三次代理连通性检查失败才恢复 warp-svc，恢复间隔至少 15 分钟；不会自动重注册、换 IP、重启 Xray 或重启服务器。健康检查运行日志：

```bash
sudo journalctl -u warp-xray-health.service -n 60 --no-pager
```

## 使用边界

- 仅处理通过 Xray 的流量，客户端原有连接参数通常不用改。
- 默认覆盖常见 Google/YouTube 域名，不保证全部产品和地区域名；可自定义清单。
- 匹配到的 UDP 会拒绝，不假设 WARP 本地 SOCKS 支持 UDP。浏览器通常可回退 TCP，需要 UDP 的应用可能失败。
- 不关闭 IPv6，不添加黑洞路由；完整双栈能力需实际检测。
- 纯 IP、ECH 或无法嗅探的请求可能漏匹配。Google 出口地区、验证码与可用性不作保证。
- 不擅自清空共享防火墙，不卸载可能被其他应用使用的 redsocks；持久化旧规则需另行检查。
- 有面板时必须通过面板保存出站/路由，避免面板覆盖文件。
- 仓库的自动化测试不等于真实 VPS 验证；首次部署请保留 SSH，并从客户端测试业务。

[完整操作与迁移说明](GUIDE.zh-CN.md) · [验证范围](VALIDATION.txt)
