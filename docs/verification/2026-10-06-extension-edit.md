# v2 extension content edit candidate

2026-10-06，Windows/Python 3.11 合成数据。`managed-package-tools-v2` 仅允许按已安装、SHA-256 固定的外部 Schema，改变自定义 Zone 中**已存在**的顶层字段；类型、成员集合、包引用和共享实例作用域固定。资源和递归工作集继续沿原 journal 的预览→确认、undo/redo、save/export 走，不在候选阶段写原包。

独立工具定向 `fwtools_tests.test_extension_content` 与 `fwtools_tests.test_package_cli` 47/47；覆盖了无 profile、错目标、缺字段、错误旧值、Schema 拒绝、重复提交、undo/redo、保存与导出。随后补充原始工作集的结构保真检查：即使新增/删除的可选字段均符合外部 Schema，也不能绕过工具补丁 API 修改成员集合。该修复后的正式 `tools/test-core.py --facetwire-schema-root <固定 FacetWire schema>` 全部 194/194，通过逐文件 1650/1650 语句、674/674 分支精确 100%。Pillow 消费会话专项 9/9 另记，不把工具测试当 Renderer 视觉证明。没有真实用户文档、真实模型或网络外发。
