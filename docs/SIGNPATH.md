# 通过 SignPath Foundation 给 ShareBox 做公开可信签名

> 目标：让 Windows 把 `ShareBox-Setup.exe` 识别为可信发布者，减少 SmartScreen / “未知发布者”提示。
> 费用：开源项目免费。发布者显示为 **SignPath Foundation**，不是你的公司名。

## 1. 申请前必须满足

- 仓库 **公开**（GitHub / GitLab 均可），能匿名访问；
- 有 OSI 认可的开源许可证（本项目使用 **Apache-2.0**，见根目录 `LICENSE`）；
- 构建可复现：`scripts/build-windows.ps1` 能从公开源码构建出安装包；
- 不包含闭源二进制或需要授权码才能构建的组件；
- 项目处于维护状态（README、提交记录、Issue）。

## 2. 仓库里不能出现的东西

- Tailscale auth key、任何公司/租户密钥；
- `signing/` 下的自签名私钥 `.pfx`；
- 用户数据、日志、恢复码、`sharebox-data`。

`.gitignore` 已排除这些路径。密钥只能通过环境变量或运行时配置注入。

## 3. 申请步骤

1. 在 GitHub 创建公开仓库，推送源码（含 `LICENSE`、`README.md`、`scripts/`、`.github/`）。
2. 打开 <https://signpath.org/>，选择 **SignPath Foundation**，提交项目申请。
3. 申请里填写：
   - 项目名：ShareBox
   - 仓库地址
   - 许可证：Apache-2.0
   - 构建命令：`./scripts/build-windows.ps1`
   - 产物：`ShareBox-Setup.exe`
   - 维护者联系方式
4. 审核通常需要 1–2 周。通过后，SignPath 后台会给你：
   - Organization ID
   - Project slug
   - Signing policy slug
   - Artifact configuration（产物的签名规则）

## 4. CI 接入

在 GitHub 仓库设置里添加：

| 类型 | 名称 | 说明 |
| --- | --- | --- |
| Secret | `SIGNPATH_API_TOKEN` | SignPath 生成的 API Token |
| Secret | `SIGNPATH_ORGANIZATION_ID` | 组织 ID |
| Variable | `SIGNPATH_ENABLED` | 设为 `true` 才启用签名任务 |
| Variable | `SIGNPATH_PROJECT_SLUG` | 项目 slug |
| Variable | `SIGNPATH_POLICY_SLUG` | 签名策略 slug |
| Variable | `SIGNPATH_ARTIFACT_CONFIG` | 产物配置 slug |

`signpath-release.yml` 里已经留好签名步骤，把 SignPath 后台生成的 YAML 片段替换进去即可。

## 5. 需要签两个文件

安装包和主程序都要签，否则装完后双击 `ShareBox.exe` 仍可能提示未知发布者。

正确顺序：

1. 构建目录版程序；
2. **签 `dist/ShareBox/ShareBox.exe`**；
3. 用签名后的程序打 `payload.zip`；
4. 编译 `ShareBox-Setup.exe`；
5. **签 `ShareBox-Setup.exe`**；
6. 校验两个文件的 `Get-AuthenticodeSignature` 都是 `Valid`。

两次签名可以用同一个 SignPath 项目下的两个 artifact configuration。

## 6. 发布检查清单

- [ ] `LICENSE` 为 Apache-2.0
- [ ] 仓库无密钥、无 `.pfx`、无用户数据
- [ ] `scripts/build-windows.ps1` 在干净的 Windows runner 上成功
- [ ] `ShareBox.exe` 签名 Valid
- [ ] `ShareBox-Setup.exe` 签名 Valid
- [ ] 安装/卸载/启动冒烟测试通过
- [ ] Release notes 注明签名方式和版本

## 7. 备选方案

- **自签名**：见 `signing/`（不随公开仓库分发），适合内部电脑，每台需导入根证书。
- **Azure Trusted Signing**：约 $9.99/月，需企业验证。
- **OV/EV 证书**：约 $80–300/年，发布者显示你们公司名。
