# 后端与项目维护工具

均从项目根目录执行，使用已安装项目依赖的Python和Node。脚本不负责安装系统环境。后端启动入口为`backend/run.py`；前端构建工具位于`frontend/tools/`。本目录中`verify_all`协调前后端验证，`export_github_source`用于项目交付，二者属于项目维护工具，不是后端业务服务。

| 入口 | 用途 |
|---|---|
| `python backend/tools/verify_all.py` | 完整工程验证：源码凭证模式扫描、Python依赖导入、后端隔离全量测试、前端行为测试、类型检查和生产构建；非零退出码表示失败 |
| `python backend/tools/run_agri_checks.py tests` | 仅运行后端隔离测试；禁用部署配置与真实HTTP，默认产物位于`scratch/`的全新目录 |
| `node frontend/tools/build_agri_frontend.mjs` | 不读取部署环境文件，向全新目录生成Vite生产构建；可通过`AGRI_BUILD_SCRATCH`指定输出父目录 |
| `python backend/tools/export_github_source.py` | 向`dist/`新建带逐文件摘要的GitHub源码zip，不包含旧Git历史、本地运行数据、AI会话及历史构建/测试产物 |
| `python backend/tools/export_github_source.py --check` | 检查当前发布范围内文本的凭证模式，包含未跟踪源码，不依赖Git，也不生成zip |
| `python backend/run.py --initialize-encryption` | 首次本地启动并初始化独立配置加密密钥；后续启动省略初始化参数 |
| `python backend/tools/prepare_agri_restore.py --help` | 在全新独立目录校验并准备恢复副本 |
| `python backend/tools/upgrade_agri_copy.py --help` | 对已经核验的独立恢复副本执行编号升级 |
| `python backend/tools/import_region_reference_points.py` | 显式维护行政区划参考点；已有输出时拒绝覆盖，非日常启动步骤 |
| `python backend/tools/check_ai_config.py` | 可选的真实本地配置自检，会加载本地配置；不包含在默认隔离测试中 |

完整验证结果和日志在`scratch/verification-<UUID>/`。它们是本机运行产物，适合定位失败。根目录`AGENTS.md`（AI助手协作约定）以及研发资料`doc/`、`suggestions/`均为本地文件，不进入源码导出包且已被`.gitignore`忽略，由维护者自行保留或删除，不影响应用启动。

Windows也可运行`./backend/tools/verify_all.ps1 -PythonExe <Python路径>`；不传参数时使用当前PATH中的Python。

## 源码发布说明

`export_github_source.py`生成的zip包含当前源码、README、配置模板、界面截图及逐文件SHA-256清单，排除`AGENTS.md`、`doc/`、`suggestions/`、本地配置、运行数据、依赖和测试/构建临时产物。导出工具只生成本地包，不提交或推送。

停止Git跟踪某个文件不会清除它在旧提交中的内容；用源码包建立新仓库时需单独处理原仓库历史。若这些文件曾被旧仓库跟踪，`.gitignore`不会自动取消跟踪。

本地目录改名后从新根目录执行上述命令即可，默认数据路径根据源码位置计算；若本地环境或IDE配置使用了自定义绝对路径，需要同步调整。改名不替代数据备份和原加密密钥的保管。
