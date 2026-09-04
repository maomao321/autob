# GitHub PR 合并后自动部署到 UCloud

此流程面向 UCloud 云主机上的 CentOS Stream 9（64 位）。PR 合并到 `master` 后，
GitHub Actions 会上传后端发布包，以独立版本目录安装、切换 systemd 服务并检查
`http://127.0.0.1:8765/health`。若重启或健康检查失败，会自动切回上一个版本。

## 1. 配置 UCloud 网络

- 云主机需要能访问 Python 包索引以及应用所使用的外部 API。
- 安全组只需向可信来源开放 SSH 端口（默认 22）。后端默认仅监听
  `127.0.0.1:8765`，不要直接向公网开放 8765。
- 远程使用前端时，优先使用 HTTPS 反向代理；临时管理可使用 SSH 隧道：

  ```bash
  ssh -L 8765:127.0.0.1:8765 autoquant@服务器公网IP
  ```

## 2. 首次初始化服务器

先将 `packaging/deploy` 目录传到服务器，然后以 root 身份运行：

```bash
sudo bash packaging/deploy/bootstrap-centos-stream-9.sh
```

脚本会安装 Python 3.11 和 curl，创建 `autoquant` 用户、发布目录、systemd 单元，
以及仅允许重启 `autoquant.service` 的最小 sudo 规则。它不会启动服务，首次自动部署
成功后服务才会启动。

为部署用户安装一把专用 SSH 公钥（不要复用个人私钥）：

```bash
sudo install -d -m 700 -o autoquant -g autoquant /home/autoquant/.ssh
sudoedit /home/autoquant/.ssh/authorized_keys
sudo chown autoquant:autoquant /home/autoquant/.ssh/authorized_keys
sudo chmod 600 /home/autoquant/.ssh/authorized_keys
```

如需用环境变量提供服务凭据，编辑以下文件；不要将真实值提交到 GitHub：

```bash
sudoedit /etc/autoquant/autoquant.env
```

示例内容：

```dotenv
AUTOQUANT_API_TOKEN=请替换为足够长的随机令牌
# AUTOQUANT_RESTORE_REAL=1
# BINANCE_API_KEY=...
# BINANCE_API_SECRET=...
# OPENAI_API_KEY=...
# DEEPSEEK_API_KEY=...
# DASHSCOPE_API_KEY=...
```

默认没有设置 `AUTOQUANT_RESTORE_REAL=1`，因此服务器重启不会自动恢复实盘策略。

## 3. 配置 GitHub Environment

在仓库 `Settings → Environments` 新建 `production`，限制只允许 `master` 部署，
并配置以下 Environment secrets：

| Secret | 内容 |
| --- | --- |
| `UCLOUD_SSH_HOST` | UCloud 云主机公网 IP 或域名 |
| `UCLOUD_SSH_PORT` | SSH 端口，例如 `22` |
| `UCLOUD_SSH_USER` | 固定为 `autoquant` |
| `UCLOUD_SSH_PRIVATE_KEY` | 与服务器公钥匹配的专用私钥全文 |
| `UCLOUD_SSH_KNOWN_HOSTS` | 经核验的服务器 SSH host key 记录 |

可在可信网络中执行下面命令取得 host key 候选值，然后与 UCloud 控制台或服务器上的
`/etc/ssh/ssh_host_ed25519_key.pub` 指纹核对后再保存：

```bash
ssh-keyscan -p 22 服务器公网IP
```

若 SSH 端口不是 22，known_hosts 中的主机名通常是 `[主机]:端口`。工作流启用了
严格 host key 校验，不会自动接受未知服务器。

## 4. 触发与验证

- PR 合并到 `master` 会自动触发 `.github/workflows/deploy-production.yml`。
- `Actions → Deploy backend to UCloud → Run workflow` 可手动重部署 `master`。
- 部署互斥执行，不会同时修改生产环境。
- 服务器上的业务数据位于 `/home/autoquant/.autoquant`，发布版本位于
  `/opt/autoquant/releases`，代码更新不会覆盖业务数据。

部署后可在服务器检查：

```bash
systemctl status autoquant.service
curl --fail http://127.0.0.1:8765/health
journalctl -u autoquant.service -n 100 --no-pager
```

如需人工回滚，把 `/opt/autoquant/current` 重新指向之前的发布目录，再重启服务。
自动部署在健康检查失败时会完成同样的回滚操作。
