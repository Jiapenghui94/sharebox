# 安全说明

## 报告问题

如果你发现安全漏洞，请不要公开提交 Issue。请通过私下渠道联系维护者，并附上复现步骤。

## 密钥与数据

- Tailscale auth key、设备令牌、公司验证码等**不会**写入仓库；
- 所有密钥通过运行时配置或环境变量注入；
- `signing/`（自签名私钥）、`sharebox-data/`、`sharebox-client/` 已在 `.gitignore` 中排除；
- 公开仓库中的发布产物不应包含任何用户数据。

## 签名

- 内部构建可以使用自签名证书，但只应在受控电脑上导入根证书；
- 公开分发建议使用 SignPath Foundation（开源项目免费）或 OV/EV 证书；
- 详见 `docs/SIGNPATH.md`。

## 支持范围

- Windows 10/11 64 位；
- Python 3.12（源码运行）。
