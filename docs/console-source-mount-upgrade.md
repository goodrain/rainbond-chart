# Console 源码覆盖的 Helm 升级迁移

标准 Chart 新装不会创建旧 VNC Python 源码 ConfigMap 覆盖，也不运行本迁移 Job。Console、API、operator 的开发版镜像在新建 Pod 时使用 `Always`，避免使用旧的同名开发镜像缓存；稳定版本继续使用配置的拉取策略。`Always` 不会自行重启已经运行且模板未变化的 Pod，通常应使用不同的构建标签或镜像摘要。

Helm 升级默认启用一次性源码迁移，控制项为 `Component.rbd_app_ui.sourceMountMigration.enabled`。Console 禁用时不生成相关资源。不要为通过校验而关闭迁移或清空整个 `volumeMounts` 列表。

## 执行顺序

1. **pre-upgrade**：使用实际 Ready Console Pod 的不可变镜像启动只读检查 init 容器，检查时不挂业务/PVC/源码卷或 ServiceAccount token。目标 Console 镜像的主容器核对旧、新原生文件，以及 CR 中旧源码挂载引用的不可变 ConfigMap 文件指纹，并保存最小备份。这个阶段不修改 CR。
2. Helm 更新本次发布的组件配置和目标镜像。
3. **post-upgrade**：再次核对 CR、旧来源身份、备份、目标镜像文件和实际 Pod 镜像摘要。只退休已确认冗余的源码挂载及不再被引用的来源卷，然后等待 operator 生成的新 Console Pod 就绪、使用目标镜像摘要且没有这些源码覆盖。

升级前后 Job 的目标镜像摘要必须相同，业务 Pod 最终也必须匹配该摘要。若开发标签相同、原 Pod 尚未更新，只有原运行镜像也已通过原生文件核对、所有活动 Pod Ready 且仍为该基线、CR 拉取策略仍为 `Always` 时，才允许由退休挂载本身触发滚动更新；最终仍必须验证新摘要。`IfNotPresent` 不适用此例外。

## 自动迁移范围

此迁移限定于经过代码审查的六文件旧 VNC 覆盖，不以现场 UID 或 ConfigMap 名称判断：

- `console/services/app_actions/app_manage.py`
- `console/views/app_overview.py`
- `console/urls/__init__.py`
- `console/utils/vm_vnc_proxy.py`
- `console/views/vm_vnc.py`
- `console/utils/vm_vnc_proxy_relay.py`

`files/migrate_console_source_mounts.py` 包含已审查的旧文件指纹与原生六文件指纹。旧运行镜像和目标镜像必须都匹配批准的原生指纹组；目标源码后续变化时，需要先复核兼容性再更新该有限目录，不能只凭版本号假设兼容。旧原生镜像不包含 VNC、文件存在未知定制、目录覆盖、来源不是不可变 ConfigMap 或身份不明时，自动迁移停止。

只处理匹配的只读单文件挂载。未知定制、共享仍在用的卷、SSL、PVC、迁移目录等保留，旧来源 ConfigMap 不删除。没有候选覆盖时直接跳过，也不拉取旧 Console 镜像检查。

## 权限与私有镜像

Job 使用独立命名空间 ServiceAccount：只能 get/patch `rbd-app-ui` CR、get/create ConfigMap、get/list Pod，以及 get 本 revision 的两个 Job 来确认剩余执行时间；不读取 Secret 内容，也不复用 operator 的集群管理员权限。原生检查 init 不挂 API token。

升级 Job 继承现有 Console Pod 的 `imagePullSecrets` **名称引用**。目标镜像换用不同私库时，可通过 `Component.rbd_app_ui.sourceMountMigration.imagePullSecrets` 传入标准 `[{name: existing-secret-name}]` 引用；Secret 应事先存在，不把凭据填进此字段。

## 失败、备份与回退

迁移的 JSON Patch 检查 CR UID、最新 resourceVersion、镜像及原始完整挂载/卷数组。等待镜像期间只变更 status 的新 resourceVersion 会重新读取；相关配置或来源身份变化则停止，不强制覆盖。

备份是按 release/revision 命名的不可变 ConfigMap，带 `app.kubernetes.io/component=console-source-migration-backup` 标签。只存退休来源引用、索引、镜像、对象身份及数组/文件指纹；不复制源码正文、Secret、环境变量或未知卷的私有配置。旧源码 ConfigMap 和该备份保留，供核查与恢复。

补丁响应丢失或新 Console 未就绪时，脚本检查实际 CR 状态；只有仍是本次迁移结果、原来源身份和内容仍有效时才恢复原挂载，保留当前目标镜像。并发配置变化、API 不可用、Job 被外部强杀或执行超时，可能需要依据保留备份人工恢复，不能保证自动补偿。Job 会在修改前预留滚动验证与恢复时间，不足时零修改退出。

**本迁移不承诺任意旧 Helm revision 的自动回滚。** 实际运行基线可能与旧 Helm revision 记录不同；成功迁移后回滚更老版本，须先核对该版本是否原生包含 VNC，并在需要时按备份及原来源恢复挂载。`helm --atomic` 也不能替代这种跨版本复核。不要盲目执行旧快照中的 resourceVersion 或去掉并发前置检查。

成功 Job 自动清理；失败 Job 保留最多一天，便于查看仅含状态的错误信息。少量固定名称的迁移 SA/Role/Binding/脚本 ConfigMap 在下一次升级前替换；卸载 release 时需另行清理这些 hook 资源。不要在验收前删除迁移备份或旧来源。

## 验证与打包

运行 `python3 -m unittest discover -s tests -v`（需要 PyYAML 和 Helm）、`helm lint .` 和 `helm package .`。再从实际 `.tgz` 分别渲染安装与 `--is-upgrade`，确认脚本完整、包中没有测试及字节码。真实环境还需验证 Job 拉取/调度、迁移后 Console Ready 和 VNC HTTP/WebSocket。

Chart 的 `version` 不等于镜像版本。发布构建必须按原 release 流程设置 `Cluster.installVersion`；仅给 `helm package` 传 `--version` 不会更新镜像。新装或升级本轮开发镜像时，须使用包含本轮 Go/Console 提交的实际镜像标签，不能把旧稳定镜像视作已包含新修复。
