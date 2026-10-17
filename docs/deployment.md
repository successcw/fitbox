# 部署说明

这些是待填写的模板，不是安装器。先按 README 配置 Python 环境和依赖；树莓派和 Mac 各自使用本机路径。不会替你修改网络、sudoers、防火墙或启动服务。不要将私人运行目录或原始健康数据放进公开仓库。

## 1. 准备配置与路径

树莓派建议普通用户运行采集器。假设公开仓库位于 `~/public-fitbox`，采集数据保存在 `~/fitdata`。两台机器分别准备私有配置：

```sh
mkdir -p "$HOME/.config/fitbox"
touch "$HOME/.config/fitbox/config.env"
chmod 600 "$HOME/.config/fitbox/config.env"
```

按仓库配置示例填入 `FITBOX_*` 变量。该文件需同时兼容 systemd EnvironmentFile 和 POSIX shell：使用 `NAME='value'`，不要写 `export`，路径必须绝对路径，不使用 `~` 或 `$HOME` 展开。不要在值内放命令。Mac 的启动脚本会读取这个文件，systemd 则通过 `EnvironmentFile` 读取。

每台机器复制本机需要的模板到仓库内的 `deploy/local/`（不提交）。将以下占位符替换为**该机器**的真实值：

| 占位符 | 示例含义 |
|---|---|
| `__USER__` | 树莓派普通登录用户名 |
| `__HOME__` | 该用户的绝对主目录 |
| `__PROJECT__` | 公开仓库的绝对路径，不是 `fitbox/` 子目录 |
| `__PYTHON__` | 已安装依赖的 Python 绝对路径，通常为仓库 `.venv/bin/python` |

在 Mac 上也替换 `run-watch.sh`，并将 plist 中脚本路径改到替换后的 `deploy/local/run-watch.sh`。路径若含 XML 特殊字符，需要对 plist 正确转义；systemd 的 ExecStart 路径若含空格，需要加双引号。推荐部署路径不含空格。安装前检查没有遗留 `__USER__` 等占位符。

## 2. 树莓派网络与权限

推荐树莓派用有线网络连接家庭路由器，Wi-Fi 专门连 ezShare 热点。此时先用 NetworkManager 手工连好卡；卡连接设置 `ipv4.never-default yes`，保证默认路由仍走有线网。`--net auto` 检测到 `eth0` 已连接时不会自动配置 Wi-Fi，需要你先完成这一步。网卡名称不为 `eth0` 时先检查代码的自动选择逻辑，并明确使用合适的模式。

只有一个 Wi-Fi 网卡时，采集会暂时离开家庭网，Tailscale/SSH 也可能断开。`--net nm` 路径先创建独立 systemd 恢复计时器，再切换热点，结束后恢复原连接。首次验证应在能通过有线网络或本地终端恢复的条件下操作。

**单 Wi-Fi 自动切换不是无权限运行。** 当前代码调用 `sudo -n nmcli`、`sudo -n systemd-run`、`sudo -n systemctl`。运行用户必须已具有相应非交互权限；如果不能创建恢复计时器，采集应停止而不是冒险切网。`systemd-run` 能执行 root 命令，给它宽泛免密权限等同于授予高权限，不能将模板机械粘贴到 sudoers。本仓库不附宽泛 sudoers 规则；由机器管理员审核权限，或采用有线网加固定卡连接的 `--net none` 方案。

公开版仅使用 CPAP 任务自身的独立恢复计时器，不包含自动重启网络监控。如果机器上另有网络监控服务，先确认它不会在预期离网时重置 Wi-Fi 或重启机器。

## 3. 树莓派服务

先创建数据目录：

```sh
mkdir -p "$HOME/fitdata/cpap" "$HOME/fitdata/health" "$HOME/fitbox"
```

Health 接收器默认只绑定回环地址。远程接收需在私有配置中设置：

```sh
FITBOX_HEALTH_BIND='填写树莓派的 Tailscale IPv4 地址'
FITBOX_HEALTH_TOKEN_FILE='/填写绝对主目录/fitbox/health.token'
```

在该 token 文件中保存随机长密钥，文件权限为 `600`。不要把密钥写进模板或提交 Git。iPhone 与 Pi 必须加入你的 Tailscale 网络，Health Auto Export 使用 `http://树莓派的Tailscale地址:8787/`，请求头 `X-Fit-Token` 填同一密钥。仅绑定 Tailscale 地址，不向公网开放 8787 端口；Tailnet ACL 应限制可访问设备。保留每日汇总 JSON 自动化，另外建立一条只选 Sleep Analysis、关闭汇总的 JSON 自动化，保留分段起止时间。两条发送到同一地址。

确认占位符均替换之后，在**树莓派**安装所需服务：

```sh
sudo install -m 644 deploy/local/fit-cpap.service /etc/systemd/system/fit-cpap.service
sudo install -m 644 deploy/local/fit-cpap.timer /etc/systemd/system/fit-cpap.timer
sudo install -m 644 deploy/local/fit-health.service /etc/systemd/system/fit-health.service
sudo systemctl daemon-reload
sudo systemctl start fit-health.service
sudo systemctl start fit-cpap.service
journalctl -u fit-health.service -u fit-cpap.service -n 100 --no-pager
```

首次手动采集通过、文件有效且网络恢复之后，才启用开机接收和定时采集：

```sh
sudo systemctl enable fit-health.service
sudo systemctl enable --now fit-cpap.timer
systemctl list-timers fit-cpap.timer
```

时间取树莓派系统时区：每日 07:30 主采集、21:30 补漏，周末增加 08:00 / 09:00，最多随机延迟 3 分钟。`--if-missing` 让当天成功后跳过后续任务。旧卡模板使用 `--legacy-scan --days 2 --verify`；更换卡或呼吸机后需先验证，不保证所有固件通用。

体脂秤为可选项：确认型号与协议匹配，配置个人参数与筛选范围，安装 `bleak` 并确认用户可以访问 BlueZ 后，再安装、启动 `fit-scale.service`。若 Bluetooth 被屏蔽，先用系统工具诊断；公开模板不直接修改 `/sys`。

## 4. Mac 同步与日报

Mac 配置 SSH 用户、Pi 地址和通知参数；先手动完成 SSH 主机指纹核验与密钥登录，确保 `ssh -o BatchMode=yes 用户@地址 true` 成功。不要通过禁用主机验证绕过登录问题。安装 Chrome 用于报告 PNG/PDF 渲染；Mac 需开机、登录且能够访问 Pi 和 Telegram，才会生成和发送日报。

准备日志目录，再安装**替换过占位符**的 plist：

```sh
mkdir -p "$HOME/.local/state/fitbox" "$HOME/Library/LaunchAgents"
plutil -lint deploy/local/dev.fitbox.watch.plist
cp deploy/local/dev.fitbox.watch.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/dev.fitbox.watch.plist"
launchctl print "gui/$(id -u)/dev.fitbox.watch"
tail -n 50 "$HOME/.local/state/fitbox/watch.log"
```

更新已加载服务时，先 `launchctl bootout "gui/$(id -u)/dev.fitbox.watch"`，再 bootstrap。不要同时保留两套 watcher 指向同一数据目录，以免重复同步或发送。

## 5. 验收

- Pi 手动采集结束后家庭网络恢复，EDF 严格校验通过；再次运行不会重复下载。
- 正确 token 的 Health 推送得到成功响应，错误 token 被拒绝；原始 JSON 和索引均落盘。
- Mac 能拉取 Pi 的增量数据，逐段睡眠与汇总互不覆盖。
- 先查看本地报告，确认设备实测、手表估计和个人趋势的区别，再开启个人 Telegram 通知。
- 次日验证 timer 执行日志和日报到达。首次安装成功不等于定时任务已验证。

部署命令会修改本机服务配置，请在自己的目标机器执行；本文没有自动执行任何安装操作。
