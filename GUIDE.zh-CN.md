# WARP 官方客户端 + Xray 自分流

面向 Debian 12/13 的现有 Xray 服务器。保留官方 WARP 客户端；不再使用 redsocks、Google IP 段重定向、IPv6 黑洞路由或全系统 IPv4 优先设置。未来 Debian 版本只有在官方软件源提供相应发行版时才能新装 WARP，不冒用旧发行版软件源。

本工具尚未在真实 Debian VPS 上运行验证。已做配置转换与安全边界的本地测试；部署时仍需当前 Xray 自检、服务启动检查及客户端实测。不会保证 Google 无验证码、IP 地区或长期连通。

## 文件与适用范围

- `warp-xray.sh`：启动入口，需和 `warp-xray.py` 放在同一目录。
- `warp-xray.py`：主体，仅使用 Python 3 标准库。
- 只处理通过本机 Xray 的流量，不影响服务器其他程序的出站。
- 自动应用只支持由 systemd 直接运行、进程命令行明确加载单个 JSON 文件的 Xray。面板、容器、多文件目录、JSONC、重复 JSON 字段、非 Xray 主进程等不自动覆盖。
- **有面板时，无论 Xray 进程如何启动，都应使用 export，由面板保存配置。** 不能仅因 prepare 能识别文件就绕过面板，否则面板可能覆盖修改。
- 不安装或升级 Xray，不改变客户端连接信息，不重置 WARP 注册、许可，不自动换 IP，不修改 VPS 入站监听。
- 配置备份含有现有密钥，只存放在 root 可访问的计划目录中，不要公开上传。

## 1. 上传并检查环境

将 `.sh` 和 `.py` 上传到服务器同一目录，例如 `/root/warp-xray/`，进入该目录。

```bash
sudo apt-get update
sudo apt-get install -y python3 curl ca-certificates
bash warp-xray.sh --help
systemctl cat xray.service
```

最后一条用于确认启动方式和配置路径，服务名可能不同。`export` 的配置输出目录权限受系统已有目录权限影响，建议指定新目录。

## 2. 安装/复用并设置官方 WARP 代理

```bash
sudo bash warp-xray.sh install-warp
sudo bash warp-xray.sh status
```

本命令代表接受 WARP 条款并允许切换到 proxy 模式、设置端口 40000；会影响原先使用 WARP 全局模式的其他服务。如果已安装客户端，则复用现有版本，不自动升级。首次安装使用 Cloudflare 官方 APT 源和签名校验。不能在官方源不支持当前代号时继续安装。

`status` 分别使用自动、IPv4 目标、IPv6 目标进行真实 SOCKS 请求。`warp=on`/`plus` 才视为健康。IPv6 测试失败不能声称完整双栈已可用；也不能用 `curl -6 -x socks5h://127.0.0.1:40000` 测 IPv6 出口，因为这会限制本地 SOCKS 连接的地址族。

## 3A. 普通单文件 Xray：生成候选配置

下面路径只是例子，请替换成实际配置路径和服务名：

```bash
sudo bash warp-xray.sh prepare \
  --config /usr/local/etc/xray/config.json \
  --service xray.service
```

如果已有路由规则，脚本会显示各规则序号和出站标签，然后要求明确插入位置。规则从 0 编号；`--rule-index N` 表示在原第 N 条规则前插入。例如：

```bash
sudo bash warp-xray.sh prepare \
  --config /usr/local/etc/xray/config.json \
  --service xray.service \
  --rule-index 2 \
  --add-sniffing
```

**2 仅是示例，不是通用正确值。** 必须让 API 路由、安全拦截等需要优先的规则留在前面，让原有兜底规则留在 Google 分流后面。插入位置决定哪些原有规则会被 Google 分流优先匹配；不要不看原规则就填 0。

默认出站和其他规则顺序均保留。新增 SOCKS 出站追加到列表末尾，避免意外成为默认出站。不改变已有 DNS 配置和 domainStrategy。

`--add-sniffing` 仅对尚无 sniffing 字段的常见代理入站补充 HTTP/TLS/QUIC 嗅探，并设 routeOnly；已有嗅探设置不覆盖。客户端传递域名时通常能直接匹配；纯 IP、ECH、无法嗅探或未列入清单的 Google 流量仍可能不匹配。已有禁用嗅探的配置需人工检查，脚本不会偷偷启用它。旧内核若不支持所加字段，Xray 自检将阻止应用。

成功后输出一个 `/var/lib/warp-xray/plan-...` 目录。里面包含原始备份、候选配置和哈希清单。候选配置必须通过当前 Xray 二进制自检；旧 SOCKS 格式不通过时会尝试新版格式。

```bash
# 本地查看候选文件；不要将含密钥的配置公开发送
sudo less /var/lib/warp-xray/plan-实际编号/candidate.json

# 应用会短暂重启 Xray，中断现有连接
sudo bash warp-xray.sh apply --plan /var/lib/warp-xray/plan-实际编号
```

应用前再次检查配置是否被其他程序改动、候选哈希、Xray 自检和 WARP 实际连通。通过后原子替换配置，保留权限与所有者。新服务启动失败时恢复原 Xray 配置并重启。服务启动成功不等于业务端到端成功，仍需客户端测试。

## 3B. 面板或多文件配置：导出配置片段

```bash
sudo bash warp-xray.sh export --output /root/warp-xray-fragments
```

将导出的两个出站合并到面板的出站设置，将两条路由合并到面板路由中，并正确安排优先级。**这些文件是片段，不是完整配置，不要覆盖原 config。** 如当前 Xray 要求新版 SOCKS 字段，用：

```bash
sudo bash warp-xray.sh export \
  --output /root/warp-xray-fragments-new \
  --socks-style current
```

由面板保存、执行完整配置检查和重启。不要同时再用 prepare/apply。面板场景的旧脚本清理需要按下方清理边界人工操作，因为本工具不会伪造一个自动应用计划。

## 4. 清理原 warp-g.sh 的透明代理

普通单文件模式下，确认新配置已应用，再执行：

```bash
sudo bash warp-xray.sh cleanup-legacy --plan /var/lib/warp-xray/plan-实际编号
```

只识别本次提供的旧脚本特征。清理前备份旧文件与 iptables 状态，不执行未知的旧脚本：

- 停用并移除旧 `warp-google.service`，避免开机重建透明代理。
- 删除精确的 OUTPUT → WARP_GOOGLE 跳转及该专用链，不清空整个防火墙。
- 删除旧脚本添加的 `2607:f8b0::/32` IPv6 黑洞路由。
- 删除 `/etc/gai.conf` 中旧脚本写入的精确行 `precedence ::ffff:0:0/96  100`，其他行保留。若这个相同行原本就是你手动设置的，需要事后根据需要恢复。
- 移除识别到的旧 `/usr/local/bin/warp-google` 和旧管理命令 `warp`，防止误操作重新接管网络。
- 不全局 pkill、不卸载 redsocks、不覆盖或删除其配置，因为可能有其他服务使用。

如果你曾使用 netfilter-persistent 等工具保存旧规则，还需检查其持久化文件，避免重启后恢复旧链；本工具不擅自重写共享持久化防火墙文件。清理结果需重启后复核。

面板用户可按上述边界人工清理，保留原文件备份，尤其先停用旧服务。**直接运行旧脚本的 stop 或 systemctl stop 旧服务，会执行其全局 pkill redsocks，可能影响其他 redsocks 用途。** 不清楚用途时先不要运行旧 stop，检查服务并清除 ExecStop 后再停用。也可以先把当前面板类型和相关配置发来，针对该环境补充迁移流程。

## 5. 启用健康检查

```bash
sudo bash warp-xray.sh enable-health
systemctl list-timers warp-xray-health.timer
journalctl -u warp-xray-health.service -n 60 --no-pager
```

复制主体脚本到 `/usr/local/lib/warp-xray/`，创建健康检查服务和计时器。约每分钟用 Cloudflare HTTPS trace 验证本地 SOCKS 数据路径，连续三次失败才重启 warp-svc 并请求连接；两次恢复至少间隔 15 分钟。不检查 Google 验证码，不删除注册，不重启 Xray 或服务器。仅使用一个检测站点，站点故障也可能触发恢复，因此有失败阈值和冷却时间。

计时器只负责代理数据路径检测与恢复；WARP 开机自连仍需通过一次服务器重启验证。不要在 WARP 不通时自动回退直连 Google，以免出口意外变化。

暂停自动恢复后再手动断开 WARP，否则健康检查会重新连接：

```bash
sudo bash warp-xray.sh disable-health
```

## 域名、UDP、IPv6 和测试范围

默认域名清单覆盖常见 Google 搜索/API/静态资源、YouTube、部分广告域名及几个地区域名，不代表所有 Google 产品、国家后缀或所有 Google Cloud 用户。它不是 Google ASN 全量路由，也不会自动更新远端规则集。

可提供自定义纯域名清单，每行一个，不带 `domain:`、协议或通配符；`#` 开头为注释。`--domains 文件` 同时适用于 prepare 和 export，**替换整个默认清单**。脚本主体的 DOMAINS 常量是完整默认列表。

```bash
sudo bash warp-xray.sh prepare \
  --config /usr/local/etc/xray/config.json \
  --rule-index 2 --domains /root/google-domains.txt
```

匹配的 TCP 进入 WARP；匹配的所有 UDP 被拒绝，避免未经验证就依赖官方本地代理转发 UDP。支持回退的浏览器通常会改用 TCP HTTPS；需要 UDP 的应用可能失败。没有匹配到域名的 QUIC 不保证被拦截，必须在实际客户端测试，而不是声称全部 UDP 已受控。

不强制关闭 IPv6、不改变 VPS 监听地址、不更改系统 DNS。IPv4/IPv6 的 IP 目标只要能匹配相应路由并且 WARP 代理支持该目标，都可以通过该出站。双栈能力应实测，不承诺单凭配置实现完整双栈。

迁移后至少检查：

1. 客户端能连接 VPS；Google 搜索和实际使用的 Google 产品正常。
2. 非 Google 网站沿用原路由；必要时查看 Xray 路由日志确认出站标签。
3. 开启 QUIC 的浏览器能够回退、纯 IP 请求行为符合预期。
4. `status` 各项结果及客户端 IPv4/IPv6 目标测试。
5. 重启后旧 WARP_GOOGLE 链没有重建，WARP 代理与计时器正常。

服务器 `curl https://google.com` 是服务器直连测试，不是 Xray 入站分流测试；`status` 则仅测试 WARP SOCKS 数据路径，也不能证明 Xray 规则匹配正确。

## 恢复与故障收集

### 新增：重复安装检测和完整卸载

新版会在询问插入位置前识别已有/残留的 WARP 分流并跳过重复安装。配置路径必须指向 JSON 文件，填写目录时会给出明确提示。

更新已安装的管理工具（不重新配置 Xray）：

```bash
curl -fsSL https://raw.githubusercontent.com/brian952700/warp-xray-debian/main/install.sh | sudo bash -s -- --download-only
```

完整卸载并恢复原 config：

```bash
sudo warp-xray uninstall
# 若有多个备份而无法唯一确定，指定原始计划：
sudo warp-xray uninstall --plan /var/lib/warp-xray/plan-实际编号
```

输入 UNINSTALL 确认后，先校验备份与实际服务、恢复原 config 并重启 Xray，成功后再清理健康检查、WARP 软件包、本地注册数据、软件源/密钥及工具。归档保留在 `/var/backups/warp-xray/时间编号/`，包括原始计划、卸载前 config 和阶段记录。原配置无法启动时尝试换回当前分流配置，不继续卸载。

config 已有后续修改或缺少有效备份时拒绝覆盖；不会卸载 Xray，不自动清理共享依赖/redsocks，不删除系统 journal，不自动恢复旧透明代理。未知旧规则或其他依赖 WARP 的跟踪配置会阻止完整卸载。面板/多文件模式需在面板中自行恢复，不支持自动恢复导出的片段。

### 只恢复配置、不卸载 WARP

```bash
sudo bash warp-xray.sh restore --plan /var/lib/warp-xray/plan-实际编号
```

只恢复该计划的 Xray 原始配置。如果当前配置已被面板/其他操作修改，或备份哈希不符，会拒绝覆盖，需人工合并。不会自动恢复旧透明代理、全局 WARP 模式、系统包或源，也不会卸载 WARP。清理前文件位于计划内 legacy 子目录，必要时人工审阅恢复。

```bash
warp-cli status
sudo journalctl -u warp-svc -n 100 --no-pager
sudo journalctl -u xray.service -n 100 --no-pager
sudo journalctl -u warp-xray-health.service -n 100 --no-pager
sudo warp-diag
```

日志和诊断包可能含公网 IP、域名等信息，分享前脱敏。

## 依据

- Cloudflare 官方软件源（Debian 12/bookworm、13/trixie）：https://pkg.cloudflareclient.com/
- WARP Linux：https://developers.cloudflare.com/warp-client/get-started/linux/
- Xray 路由：https://xtls.github.io/en/config/routing.html
- Xray SOCKS 出站：https://xtls.github.io/en/config/outbounds/socks.html
