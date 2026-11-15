# 服务器部署状态

日期：2026-11-15。

- GitHub：`kirin7002/personal_agent`，分支 `main`。
- 独立目录：`/home/kirin/apps/personal_agent`。
- 电脑入口：`https://agent.kirinxu.cn/`。
- 手机入口：`https://agent.kirinxu.cn/mobile`。
- 网页服务：已启动；公网HTTPS、健康检查、桌面和手机HTML均返回200。
- 访问隔离：未配对设备不能访问任务、对话和私有配置；伪造转发头仍不能成为本机可信设备。
- 本机与服务器应用回归：各654项通过。
- Hermes固定源码、独立Python和必要工具链：已准备。
- 模型功能：尚未配置服务器模型凭据，未启用，不宣称模型链路已部署验证完成。
- 首台设备：管理员运行 `deploy/bootstrap_device.py`，配对码只写入私有文件，五分钟有效。
- 现有Caddy配置已私有备份，仅增加独立域名路由；未替换其他站点。
- 本次没有启用系统开机自启。

模型密钥、SSH凭据、设备数据库、私有首次配对说明均不在Git中。配置模型后还需启动Hermes服务，并验证真实模型、S2S与模拟车控闭环。
