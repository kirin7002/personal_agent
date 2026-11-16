# 独立服务器部署

本部署保留服务器上的其他站点。网页服务运行在独立目录、独立用户进程和端口，Hermes仅监听本机回环地址。公网只经现有Caddy的一个新增域名进入。

当前布局：

- 代码：`/home/kirin/apps/personal_agent`，GitHub `kirin7002/personal_agent`。
- 入口：`https://agent.kirinxu.cn`，手机 `/mobile`。
- 网页上游：Docker主机网关 `172.17.0.1:18765`。
- 可信反向代理：当前Caddy的两个固定容器地址；不能配置为 `*`。
- Hermes：`127.0.0.1:8642`，不公开API Key或端口。
- Hermes源码固定到已验证的上游提交 `72c880370ff2e47dd587982ddded1b1ce90cfde4`，运行状态留在忽略的 `.runtime`。

## 配置

先准备项目虚拟环境、固定版本Hermes运行环境和私有 `.env`。服务器模式只在项目 `.env` 保存模型密钥，启动器向Hermes传递进程环境；Hermes home的 `.env` 仅包含启用标记。

固定源码的API适配器还需要 `aiohttp`；本次PM选择 `anthropic`、`mcp`、`web`、`sms` 依赖组，其中 `sms` 组只用于取得上游固定的 `aiohttp`，不启用短信功能或工具。可在Hermes源码目录运行：

```sh
HERMES_HOME=/home/kirin/apps/personal_agent/.runtime/hermes-home \
HERMES_RUNTIME_DIR=/home/kirin/apps/personal_agent/.runtime/hermes-tools \
python3 -m pm.cli install --extra anthropic --extra mcp --extra web --extra sms
```

然后执行：

```sh
.venv/bin/python deploy/configure_server.py \
  --origin https://agent.kirinxu.cn \
  --bind-host 172.17.0.1 --port 18765 \
  --trusted-proxy 172.22.0.3 --trusted-proxy 172.25.0.2
```

尚未配置模型凭据时可追加 `--web-only`，先完成网页与设备配对入口，模型功能明确显示未配置。

上述代理地址仅是本次服务器检测结果。迁移或重建Caddy容器后需重新确认，不要扩大到任意地址。Uvicorn只从这些代理接收原始客户端地址和HTTPS协议，公网请求不能借伪造的转发头成为本机可信设备。

服务单元模板只启动本项目服务。安装后先 `systemctl start`，本次不执行 `systemctl enable`。Caddy文件先备份，只增加 `Caddyfile.fragment` 中的新站点，通过 `caddy validate` 后再平滑reload；失败则恢复原配置。

## 首台设备

公网新设备默认未配对，不能访问任务、消息或调用模型。管理员通过SSH执行：

```sh
cd /home/kirin/apps/personal_agent
.venv/bin/python deploy/bootstrap_device.py
```

一次性配对码写入权限为600的 `.runtime/first-device.txt`，不会打印到日志。先在 `/mobile` 输入此码；同浏览器随后打开 `/` 即是电脑观察台。后续设备由已配对设备生成新码邀请。配对码五分钟有效，过期后重新运行命令。

不要提交 `.env`、设备数据库、私有连接说明或任何证书私钥。公网域名使用Caddy签发的受信HTTPS证书，不需要安装本地演示CA。

## 验证与回滚

部署后核对Git提交、内部健康、外部HTTPS、未配对访问拒绝、转发头伪造拒绝及真实模拟任务。只将“健康检查通过”称作启动成功；真实Hermes/S2S结果另行记录。

回滚时停止本项目两个服务，切回前一提交/备份，再启动；不要停止其他容器。Caddy备份留在服务器，不拷贝到公开仓库。
