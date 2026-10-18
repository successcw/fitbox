# Fitbox 部署代理手册

这份文档是写给 **AI agent** 的，不是给人从头读到尾的教程。目标：让代理在用户的 Mac 和树莓派上把 fitbox 部署到“能采集、能出日报、能自我验证”的状态；缺少硬件、权限或凭据时，明确停下来，不猜测、不假装成功。

## 给用户的复制提示词

把下面这段和仓库地址一起发给你的 AI：

```text
请阅读 https://github.com/successcw/fitbox/blob/main/AGENTS.md，按阶段为我部署 fitbox。
先只做信息收集和只读预检，把需要我提供的内容列出来；我确认后再继续。
不要向我索要 token 明文，不要修改呼吸机治疗参数，不要格式化 SD 卡，
不要把 8787 端口暴露到公网，也不要在未验证时声称部署成功。
```

---

## 0. 代理契约

你必须：

- 按阶段执行；每个阶段结束运行验证命令，并保留真实输出。
- 只把“命令返回成功且输出符合预期”视为通过。
- 遇到 `FAIL`、权限不足、硬件型号不符或用户未提供的信息，停止并报告。
- 把密钥写入用户本机的配置文件（权限 `600`），不要回显、不要写进聊天记录、不要提交 Git。
- 优先使用有线网络 + 固定连卡的方案；只有用户明确同意时才配置自动切网。
- 在真实发送 Telegram 消息前，取得用户明确同意。

你不得：

- 修改呼吸机压力、模式或任何治疗设置。
- 格式化、删除或写入 SD 卡；采集器只读卡。
- 关闭 SSH 主机密钥校验，或把 `StrictHostKeyChecking` 设为 `no`。
- 把健康接收端口绑定到 `0.0.0.0` 并暴露到公网。
- 删除用户已有的 `数据/`、`呼吸机/`、`runtime/` 或原始导出。
- 用“应该可以”“看起来正常”代替验证输出。

---

## 1. Phase 0：收集信息

一次性向用户索取下表；没有的项写“未提供”，不要自行编造。

| 变量 | 含义 | 示例/默认 |
|---|---|---|
| `PI_HOST` | 树莓派地址（局域网或 Tailscale） | `fitbox.local` / `100.x.y.z` |
| `PI_USER` | 树莓派登录用户 | `pi` |
| `PI_PROJECT` | 树莓派上的仓库绝对路径 | `/home/pi/fitbox` |
| `PI_HOME` | 该用户主目录 | `/home/pi` |
| `MAC_PROJECT` | Mac 上的仓库绝对路径 | `/Users/me/fitbox` |
| `FITBOX_TIMEZONE` | IANA 时区 | `Asia/Shanghai` |
| `FITBOX_DATA_ROOT` | 私有数据根目录 | `~/fitdata` 或仓库下 `runtime/` |
| `HOME_CONNECTION` | 树莓派家庭 Wi-Fi 的 NetworkManager 连接名 | 仅 `--net nm` 需要 |
| `CHROME_BIN` | Chrome/Chromium 可执行文件 | macOS 默认路径可留空 |
| Telegram | bot token 与 chat id | 由用户写入配置，不要贴在聊天里 |
| 硬件 | 呼吸机型号、卡固件、体脂秤型号 | 例如 ResMed + ezShare `LZ1001EDPG` |

同时确认：

- 树莓派是 64 位 Raspberry Pi OS / Debian / Ubuntu，带 systemd、NetworkManager、Bluetooth。
- 树莓派是否有线联网；只有一块 Wi-Fi 网卡时，连卡热点会短暂离网。
- Mac 是否安装 Chrome/Chromium、Python 3.12+，以及能否 SSH 到树莓派。
- 是否使用体脂秤、Apple Watch、Tailscale。未使用的模块可以跳过，但要在报告里写明。

**停止条件**：用户没有提供 `PI_HOST`、`PI_USER`、Telegram 凭据或时区时，不要开始安装。

---

## 2. Phase 1：只读预检

### 2.1 取代码

在 Mac 和树莓派分别执行：

```sh
git clone https://github.com/successcw/fitbox.git "$HOME/fitbox"
cd "$HOME/fitbox"
python3 --version    # 必须 >= 3.12
```

### 2.2 预检（配置前）

在每台机器上：

```sh
python3 scripts/preflight.py --role auto --skip-ssh
```

预期：没有 `FAIL`；`WARN` 需要逐条解释。此阶段允许 `config file: not found`。

### 2.3 创建配置

两台机器分别：

```sh
mkdir -p "$HOME/.config/fitbox"
cp config.example.env "$HOME/.config/fitbox/config.env"
chmod 600 "$HOME/.config/fitbox/config.env"
```

让用户自己编辑该文件，或由你写入占位值后再让用户替换。配置必须是 `NAME='value'`，不要 `export`，不要命令替换，路径用绝对路径。

Mac 至少需要：

```sh
FITBOX_HOSTS='<PI_HOST>'
FITBOX_SSH_USER='<PI_USER>'
FITBOX_TIMEZONE='<FITBOX_TIMEZONE>'
TELEGRAM_BOT_TOKEN='<由用户填写>'
TELEGRAM_CHAT_ID='<由用户填写>'
# 可选
FITBOX_DATA_ROOT='/absolute/private/data'
CHROME_BIN='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

树莓派至少需要：

```sh
FITBOX_TIMEZONE='<FITBOX_TIMEZONE>'
# 使用健康接收器时
FITBOX_HEALTH_BIND='127.0.0.1'          # 或树莓派的 Tailscale 地址
FITBOX_HEALTH_TOKEN_FILE='/home/<PI_USER>/fitbox/health.token'
# 只有 --net nm 需要
FITBOX_HOME_CONNECTION='<家庭 Wi-Fi 连接名>'
```

生成健康接收 token（不要打印到聊天）：

```sh
mkdir -p "$HOME/fitbox"
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$HOME/fitbox/health.token"
chmod 600 "$HOME/fitbox/health.token"
```

### 2.4 预检（配置后）

在 Mac：

```sh
python3 scripts/preflight.py --role mac --require-config
```

在树莓派：

```sh
python3 scripts/preflight.py --role pi --require-config
```

**门**：必须 `OK`，或只剩已解释的可选 `WARN`。出现 `FAIL` 时停止，不要继续装服务。

---

## 3. Phase 2：安装依赖与自测

两台机器分别：

```sh
cd "$HOME/fitbox"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
pip install -r requirements.txt
python -m unittest discover -s fitbox -p 'test_*.py'
python examples/render_demo.py --html-only
```

预期：

- 单元测试全部通过（当前 109 项）。
- 输出 `runtime/demo/数据/健康/晨报/demo.html`。
- 没有访问真实设备、没有发送 Telegram。

---

## 4. Phase 3：树莓派采集

### 4.1 先只读探测

```sh
cd "$PI_PROJECT"
python3 fitbox/cpap_pull.py --probe
python3 fitbox/cpap_pull.py --list
python3 fitbox/cpap_pull.py --dest "$HOME/fitdata/cpap" --dry-run --net auto
```

预期：打印卡版本、目录条目；`--dry-run` 不写文件。若返回登录页、欢迎页、404 或空列表，先读：

- `skills/ezshare-cpap-read/SKILL.md`
- `skills/ezshare-cpap-read/references/protocol.md`

不要因为 HTTP 200 就认为拿到了 EDF；旧固件可能用 200 返回欢迎页。

### 4.2 网络方案二选一

**推荐：有线 + 固定连卡**

```sh
# 用户手动把 Wi-Fi 连到 ezShare；有线保留家庭网络
python3 fitbox/cpap_pull.py --dest "$HOME/fitdata/cpap" --net none --verify
```

**自动切网：`--net nm`**

需要用户已配置免密 sudo：

```sh
sudo -n true
sudo -n nmcli --version
sudo -n systemd-run --version
```

任一失败就停止，改用有线方案或让用户配置权限；不要要求把宽泛 sudoers 直接粘进系统。

### 4.3 安装 systemd 单元

在仓库内生成 `deploy/local/`（不提交）：

```sh
mkdir -p deploy/local
for unit in fit-cpap.service fit-cpap.timer fit-health.service fit-scale.service; do
  sed -e "s|__USER__|$PI_USER|g" \
      -e "s|__HOME__|$PI_HOME|g" \
      -e "s|__PROJECT__|$PI_PROJECT|g" \
      -e "s|__PYTHON__|$PI_PROJECT/.venv/bin/python|g" \
      "deploy/systemd/$unit" > "deploy/local/$unit"
done
grep -R '__USER__\|__HOME__\|__PROJECT__\|__PYTHON__' deploy/local && echo '占位符未替换，停止' && exit 1
```

如果使用有线方案（`--net none`），先把 `deploy/local/fit-cpap.service` 里的 `--net auto` 改成 `--net none`，再安装。

安装并启动接收器：

```sh
sudo install -m 644 deploy/local/fit-cpap.service deploy/local/fit-cpap.timer \
  deploy/local/fit-health.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fit-health.service
curl -fsS -H "X-Fit-Token: $(cat "$HOME/fitbox/health.token")" \
  http://127.0.0.1:8787/health
```

预期：`ok`；错误 token 应为 `403`。如果 `FITBOX_HEALTH_BIND` 不是回环地址，把 curl 地址换成该地址。

默认数据目录是 `$HOME/fitdata`。如果修改 `FITBOX_DATA_ROOT`，需要同步修改 systemd 模板里的 `--dest` 和 `ReadWritePaths`，否则服务会读写到旧路径。

首次手动采集：

```sh
sudo systemctl start fit-cpap.service
journalctl -u fit-cpap.service -n 60 --no-pager
```

预期：成功；或明确输出“今天已取得并核验昨夜数据，这一档跳过”。两者都算正常，但必须能看到原因。确认家庭网络已恢复（`nmcli con show --active` / SSH 仍可连）后再继续。

启用定时器：

```sh
sudo systemctl enable --now fit-cpap.timer
systemctl list-timers fit-cpap.timer
```

### 4.4 体脂秤（可选）

仅当型号匹配 `AFU-BH-TZ-B1` 时：

```sh
sudo install -m 644 deploy/local/fit-scale.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fit-scale.service
journalctl -u fit-scale.service -n 40 --no-pager
```

不匹配的秤不要假装支持；记录为“未使用”。

---

## 5. Phase 4：Mac 同步与日报

### 5.1 SSH 门

```sh
ssh -o BatchMode=yes -o ConnectTimeout=8 "$PI_USER@$PI_HOST" true
```

预期退出码 0。失败时检查主机密钥、密钥登录、Tailscale/局域网；不要关闭主机校验。

### 5.2 安装 LaunchAgent

```sh
cd "$MAC_PROJECT"
mkdir -p deploy/local "$HOME/.local/state/fitbox"
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__PROJECT__|$MAC_PROJECT|g" \
    -e "s|__PYTHON__|$MAC_PROJECT/.venv/bin/python|g" \
    -e "s|deploy/launchd/run-watch.sh|deploy/local/run-watch.sh|g" \
    deploy/launchd/dev.fitbox.watch.plist > deploy/local/dev.fitbox.watch.plist
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__PROJECT__|$MAC_PROJECT|g" \
    -e "s|__PYTHON__|$MAC_PROJECT/.venv/bin/python|g" \
    deploy/launchd/run-watch.sh > deploy/local/run-watch.sh
chmod +x deploy/local/run-watch.sh
# plist 里的脚本路径改到 deploy/local/run-watch.sh
plutil -lint deploy/local/dev.fitbox.watch.plist
cp deploy/local/dev.fitbox.watch.plist "$HOME/Library/LaunchAgents/"
launchctl bootout "gui/$(id -u)/dev.fitbox.watch" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/dev.fitbox.watch.plist"
launchctl print "gui/$(id -u)/dev.fitbox.watch" | head -30
tail -n 30 "$HOME/.local/state/fitbox/watch.log"
```

预期：日志出现 `connected via <PI_HOST>`，没有反复重连。

### 5.3 渲染自测

```sh
python3 examples/render_demo.py
```

预期：生成合成示例 PNG/PDF；不会发送 Telegram。若 Chrome 失败，设置 `CHROME_BIN` 后重试。

---

## 6. Phase 5：iPhone / Health Auto Export

AI 不能代替用户操作 iPhone UI。让用户完成，然后检查树莓派索引。

在 Health Auto Export 中创建三条 REST API 自动化，URL 为：

```text
http://<PI_TAILSCALE_IP>:8787/
```

请求头：

```text
X-Fit-Token: <health.token 内容>
```

| 自动化 | 范围 | 汇总 | 用途 |
|---|---|---|---|
| 健康 | 过去 7 天全部健康指标 | 开启 | 昨天的步数、能量、活动、趋势基线 |
| 睡眠 | 今天全部健康指标 | 关闭 | 今天睡眠分段、HRV/静息心率采样 |
| 锻炼 | 过去 7 天锻炼 | 不需要 | 昨天具体锻炼项目 |

三条都手动跑一次，然后在树莓派检查：

```sh
tail -n 5 "$HOME/fitdata/health.jsonl"
ls -lt "$HOME/fitdata/health" | head
```

预期：索引增加，JSON 文件落盘。没有收到时，先查 Tailscale、URL、token 和 iOS 后台刷新，不要改采集器。

---

## 7. Phase 6：首次真实闭环与验收

1. 树莓派手动采集一次；确认 `cpap.jsonl` 有新行、EDF 完整、没有 `.part`。
2. Mac watcher 拉取；检查 `数据/健康/daily.json` 出现昨夜呼吸机和今天睡眠。
3. 生成日报（默认只生成，不发）：

```sh
cd "$MAC_PROJECT"
python3 - <<'PY'
import sys
sys.path.insert(0, 'fitbox')
import health_daily
print('sent=', health_daily.morning_report())
PY
```

4. 用户明确同意后，才强制发一条测试日报：

```sh
python3 - <<'PY'
import sys
sys.path.insert(0, 'fitbox')
import health_daily
print('sent=', health_daily.morning_report(force=True))
PY
```

验收必须全部满足：

- [ ] 预检 `OK`，没有未解释的 `FAIL`。
- [ ] 单元测试全过；合成示例 PNG/PDF 能生成。
- [ ] 树莓派能读到卡、EDF 校验通过、网络已恢复。
- [ ] `fit-cpap.timer` 已启用，`list-timers` 有下一次时间。
- [ ] 健康接收器 `/health` 返回 `ok`，错误 token 返回 `403`。
- [ ] Mac watcher 已加载，日志有连接记录。
- [ ] Telegram 收到一次真实日报（用户同意的前提下），`reported.json` 的 `channel` 为 `image`。
- [ ] 没有提交或上传 `数据/`、`呼吸机/`、`runtime/`、`config.local.env`。

---

## 8. 故障决策树

| 现象 | 先查 | 不要做 |
|---|---|---|
| 扫不到 `ez Share` | `nmcli device wifi list`、信号、卡是否在呼吸机内 | 不要把卡拔到电脑上“验证” |
| `--probe` 返回 HTML/登录页 | 固件版本、`--legacy-scan`、协议文档 | 不要因 HTTP 200 判定成功 |
| 下载 404 | 先用直接路径；看 `Journal.dat` 的实际文件名 | 不要改卡目录结构 |
| EDF 校验失败 | 治疗是否结束、文件是否仍在写、重跑一次 | 不要删除 `.part` 以外的旧数据 |
| 采集后 SSH 断 | `nmcli con show --active`、恢复计时器、日志 | 不要反复重启机器 |
| 健康推送 403 | token 文件与请求头是否一致 | 不要把 token 发到聊天/issue |
| 健康推送 413 | 导出体积、是否一次全量 | 不要放宽到无限大 |
| Telegram 失败 | bot token、chat id、网络 | 不要连续轰炸重试 |
| Chrome 失败 | `CHROME_BIN`、`--version`、是否有浏览器配置冲突 | 不要退回纯文本就宣称图片成功 |
| launchd 不跑 | `launchctl print`、日志、路径 | 不要同时留两套 watcher |
| 日报缺呼吸机 | `cpap.jsonl`、`STR.edf`、时区/治疗日 | 不要用 0 代替缺失 |
| HAE 没数据 | Tailscale、URL、header、iOS 后台刷新 | 不要改接收器来“兼容”错误请求 |

---

## 9. 完成后的汇报格式

```text
阶段：Phase 0-6
完成：
- 树莓派：<主机> / <路径> / <服务状态>
- Mac：<路径> / <watcher 状态>
- 数据：<最近一条采集时间> / <文件数>
验证：
- preflight：OK（或列出 WARN）
- tests：109 passed
- /health：ok；错误 token：403
- 日报：channel=image，发送时间 <...>
未完成/阻塞：
- <具体命令、错误输出、需要用户做的事>
用户需自行完成：
- iPhone 上的三条 HAE 自动化（如未做）
- <其他>
```

不要把 token、chat id、真实健康数值或原始文件内容写进汇报。
