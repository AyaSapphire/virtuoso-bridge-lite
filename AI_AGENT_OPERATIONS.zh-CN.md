# Virtuoso Bridge Lite：AI Agent 基本操作手册

> 适用目录：`D:\Commoditas\virtuoso-bridge-lite`  
> 适用环境：Windows 运行 Python/CLI，远端 Linux 运行 Cadence Virtuoso  
> 最近验证：2026-07-26

## 1. 本手册的目标

本仓库是连接 Windows AI Agent 与远端 Cadence Virtuoso 的 Python SDK/CLI。
它负责：

```text
AI Agent
  -> Windows Python / virtuoso-bridge CLI
  -> SSH 隧道（当前环境使用 Paramiko 密码认证）
  -> 远端 RAMIC daemon
  -> Virtuoso CIW / SKILL
```

当前仓库本身不是 MCP Server。Agent 应通过以下两种入口操作：

1. `virtuoso-bridge` CLI：状态检查、简单 SKILL、加载 `.il` 文件、截图等；
2. `VirtuosoClient` Python SDK：结构化查询、原理图/版图/Maestro/Spectre 工作流。

除非上层项目另外提供 MCP 包装，否则不要假定本仓库会注册 MCP tools。

## 2. 固定工作目录与运行时

所有 Windows 命令默认从仓库根目录执行：

```powershell
cd D:\Commoditas\virtuoso-bridge-lite
```

优先使用项目虚拟环境中的可执行文件，不依赖全局 PATH：

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\virtuoso-bridge.exe --help
```

用户级连接配置位于：

```text
%USERPROFILE%\.virtuoso-bridge\.env
```

禁止读取后打印、复制或提交其中的密码、主机地址、账户和指纹。Agent 只应确认所需键是否存在。

当前旧服务器使用的关键配置类型如下，实际值只能保存在用户级 `.env`：

```dotenv
VB_SSH_TRANSPORT=paramiko
VB_REMOTE_PASSWORD=...
VB_SSH_PORT=...
VB_SSH_HOST_KEY_SHA256=...
VB_REMOTE_HOST=...
VB_REMOTE_USER=...
VB_REMOTE_PORT=...
VB_LOCAL_PORT=...
```

不要把 Paramiko 升级到 4.x/5.x 后假定仍兼容旧服务器；本项目当前约束为：

```text
paramiko>=3.4,<4
```

## 3. 每次任务开始时的标准检查

第一条命令永远应是：

```powershell
.\.venv\Scripts\virtuoso-bridge.exe status
```

健康状态必须同时满足：

```text
[tunnel] running
[daemon] OK - connected to Virtuoso CIW
[spectre] OK
```

同时检查：

- `daemon user` 与 `tunnel user` 一致；
- `workdir` 是用户期望的 Virtuoso 设计工作目录；
- Virtuoso 版本和当前任务预期一致。

如果三项均正常，可用下面的无副作用探针确认 SKILL 往返：

```powershell
.\.venv\Scripts\virtuoso-bridge.exe eval '1+1'
```

成功结果的 JSON 应包含：

```json
{
  "status": "success",
  "output": "2"
}
```

Agent 不得因为 TCP 端口可连接就判定系统健康；必须看到 daemon 能返回 SKILL 结果。

## 4. 启动、停止与重启

启动或恢复 Windows 端隧道：

```powershell
.\.venv\Scripts\virtuoso-bridge.exe start
```

停止 Windows 端隧道：

```powershell
.\.venv\Scripts\virtuoso-bridge.exe stop
```

重启：

```powershell
.\.venv\Scripts\virtuoso-bridge.exe restart
```

注意：

- `stop` 主要停止 Windows 端 SSH 隧道，不等同于关闭 Virtuoso；
- Paramiko tunnel helper 是独立后台进程；
- 修改 `paramiko_password.py` 后，必须执行 `stop` 再 `start`，旧 helper 不会自动加载新代码；
- 不要直接按 PID 杀进程，除非已经确认 PID、监听端口和状态文件三者一致；
- 不要停止用户的 Virtuoso、Xvnc、license 或其他 EDA 进程。

## 5. Virtuoso CIW 需要人工配合时

如果 `status` 显示：

```text
[tunnel] running
[daemon] NO RESPONSE
```

先使用 `status` 输出中给出的实际路径，在 Virtuoso CIW 执行：

```lisp
load("<status 输出的 virtuoso_setup.il 路径>")
```

不要自行猜测或硬编码远端临时路径。

CIW 正常情况下会出现类似信息：

```text
[RAMIC Bridge ...] launching daemon
[RAMIC Bridge ...] ready
```

如果 CIW 提示已有 daemon 正在运行，且配置需要切换：

```lisp
RBStop()
load("<status 输出的 virtuoso_setup.il 路径>")
```

只有普通停止无法清除当前用户的残留 daemon 时，才考虑：

```lisp
RBStopAll()
```

`RBStopAll()` 属于应急操作。Agent 不应在没有确认 CIW 状态和进程归属时自动调用。

完成 CIW 操作后，回到 Windows PowerShell 再运行：

```powershell
.\.venv\Scripts\virtuoso-bridge.exe status
```

## 6. CLI 基本用法

### 6.1 执行单行 SKILL

```powershell
.\.venv\Scripts\virtuoso-bridge.exe eval 'getCurrentTime()'
```

### 6.2 执行含引号或多行的 SKILL

PowerShell 中优先通过标准输入传递，避免转义错误：

```powershell
@'
let((libs)
  libs = mapcar(lambda((lib) lib~>name) ddGetLibList())
  libs
)
'@ | .\.venv\Scripts\virtuoso-bridge.exe eval --stdin
```

### 6.3 加载本地 `.il` 文件

```powershell
.\.venv\Scripts\virtuoso-bridge.exe load .\path\to\script.il
```

在远程模式下，CLI 会自动上传文件再让 Virtuoso 加载。必须检查返回 JSON 中的：

- `status`
- `output`
- `errors`
- `warnings`
- `metadata`

### 6.4 查看窗口和截图

```powershell
.\.venv\Scripts\virtuoso-bridge.exe windows
.\.venv\Scripts\virtuoso-bridge.exe snapshot
.\.venv\Scripts\virtuoso-bridge.exe screenshot
```

涉及关闭窗口或 dismiss dialog 时，先列出目标并确认，避免关闭保存提示或仿真对话框。

### 6.5 查找 SKILL 文档

```powershell
.\.venv\Scripts\virtuoso-bridge.exe skill-find dbOpenCellViewByType
.\.venv\Scripts\virtuoso-bridge.exe skill-info dbOpenCellViewByType
.\.venv\Scripts\virtuoso-bridge.exe doc-search "schCheck"
```

不确定 SKILL 函数签名时，先查本机 Cadence 文档，不要依赖记忆猜参数。

## 7. Python SDK 基本用法

### 7.1 最小连接与只读查询

建议把临时代码保存为独立 `.py` 文件；简单探针也可以使用：

```python
from virtuoso_bridge import VirtuosoClient

with VirtuosoClient.from_env(timeout=30, log_to_ciw=False) as client:
    ping = client.execute_skill("1+1", timeout=10)
    if not ping.ok or ping.output.strip() != "2":
        raise RuntimeError(ping)

    libraries = client.library.list()
    print(libraries)
```

运行：

```powershell
.\.venv\Scripts\python.exe .\path\to\script.py
```

### 7.2 查询 library

```python
with VirtuosoClient.from_env() as client:
    print(client.library.list())
    print(client.library.get("test"))
```

当前用户准备过名为 `test` 的测试 library，但 Agent 每次仍应先通过
`client.library.list()` 验证它存在，不要把历史状态当成永久事实。

仓库也提供现成示例：

```powershell
.\.venv\Scripts\python.exe .\examples\01_virtuoso\basic\04_list_library_cells.py
.\.venv\Scripts\python.exe .\examples\01_virtuoso\basic\04_list_library_cells.py test
```

### 7.3 读取原理图

```python
with VirtuosoClient.from_env(timeout=60) as client:
    data = client.schematic.read(
        "test",
        "CELL_NAME",
        include_positions=True,
        param_filters=None,
        timeout=120,
    )
    print(data)
```

`client.schematic.read()` 可返回拓扑、pins、nets、instances、参数以及可选坐标。
优先理解结构化读取结果，而不是仅依赖截图判断连线。

参考实现：

```text
examples/01_virtuoso/schematic/11_read_schematic_unified.py
examples/01_virtuoso/schematic/02_read_connectivity.py
examples/01_virtuoso/schematic/03_read_instance_params.py
```

## 8. 写操作的安全规则

### 8.1 `create()` 与 `modify()` 的区别

```python
client.schematic.create(lib, cell)
```

会以 Cadence `"w"` 模式重建目标 schematic，可能覆盖已有 cellview。只有用户明确允许新建或覆盖时才能使用。

```python
client.schematic.modify(lib, cell)
```

以 `"a"` 模式打开现有 schematic，适合增量修改。

不要使用含糊的旧 `edit()` 接口；明确选择 `create()` 或 `modify()`。

### 8.2 推荐写操作顺序

1. 读取并记录修改前状态；
2. 确认 library、cell、view 和工艺绑定；
3. 在测试 library/cell 上执行最小修改；
4. 放置器件并设置参数；
5. 按实例端口或网络语义创建连线，避免只靠坐标猜测；
6. 创建 pins/net labels；
7. 执行 `schCheck`；
8. 保存；
9. 重新读取 schematic，验证 instances、nets、pins 和参数；
10. 必要时让用户在 GUI 中做最终视觉确认。

`SchematicEditor` context manager 在无 Python 异常退出时会追加 `schCheck` 和 `dbSave`：

```python
with client.schematic.modify("test", "CELL_NAME") as sch:
    sch.add(...)
```

这不取代操作后的结构化读取验证。

### 8.3 不允许默认执行的操作

未经用户明确授权，不要：

- 删除或覆盖 library/cell/cellview；
- 对生产设计使用 `schematic.create()`；
- 调用 `RBStopAll()`；
- 关闭 Virtuoso/Xvnc/license 进程；
- 任意执行来源不明的 SKILL、shell 或 `.il` 文件；
- 修改 Cadence 安装目录、系统 Python、glibc、OpenSSL 或 `/tools`；
- 把密码、主机地址、账户、指纹、license 信息写入源码、日志、测试或文档；
- 仅凭截图认定连线和器件参数正确。

## 9. Agent 推荐工作流

### 阶段 A：建立事实

```text
status
-> 1+1 探针
-> 读取 library/cell/schematic
-> 明确用户目标与允许修改的对象
```

### 阶段 B：最小修改

- 优先结构化 Python API；
- API 不足时才生成受控 SKILL；
- 多行 SKILL 使用 `eval --stdin` 或本地 `.il` + `load`；
- 每批操作保持小而可验证。

### 阶段 C：验证

- 检查 CLI/Python 返回状态；
- 重新读取对象；
- 检查 `schCheck`；
- 对仿真任务保留输入网表、日志、输出数据和指标；
- GUI 只作为补充确认，不作为唯一证据。

### 阶段 D：汇报

至少报告：

- 操作的 library/cell/view；
- 执行了哪些查询或修改；
- 返回状态和验证结果；
- `schCheck`/保存是否成功；
- 是否仍需人工 GUI 确认；
- 遇到的警告、超时、兼容性问题。

不要在汇报中粘贴 `.env` 内容。

## 10. 常见故障决策表

### `[tunnel] NOT running`

```powershell
.\.venv\Scripts\virtuoso-bridge.exe start
```

若失败，检查：

- 用户级 `.env` 是否存在所需键；
- Paramiko 是否仍为兼容版本；
- 主机指纹是否匹配；
- SSH 22 端口是否可达。

不要回退到明文命令行密码，也不要关闭主机指纹校验。

### `[tunnel] running`，但 `[daemon] NO RESPONSE`

按顺序检查：

1. 使用 `status` 给出的 setup 路径在 CIW 执行 `load(...)`；
2. 查看 CIW 是否出现 `launching` 和 `ready`；
3. 若提示已有 daemon，先 `RBStop()` 再加载；
4. 仅在确认是当前用户残留时使用 `RBStopAll()`；
5. 回到 Windows 重新运行 `status` 和 `eval '1+1'`。

### 端口可连接，但 `eval '1+1'` 返回空响应

这是协议层异常，不是“健康”。检查：

- Windows helper 是否仍运行旧代码；
- 是否在修改传输层后执行过 `stop` + `start`；
- `ParamikoPasswordTransport._relay` 的半关闭回归测试是否通过。

### 修改源码后状态没有变化

后台 helper 不会热重载：

```powershell
.\.venv\Scripts\virtuoso-bridge.exe stop
.\.venv\Scripts\virtuoso-bridge.exe start
```

### SKILL 超时

- 先用 `1+1` 区分连接问题和具体命令问题；
- 增大 `--timeout` 或 Python API 的 `timeout`；
- 检查 CIW 是否有阻塞对话框；
- 不要对同一失败命令无限重试；
- 记录触发超时的 SKILL 和 CIW 反馈。

## 11. 修改本仓库代码后的验证

先检查工作树，保留用户已有改动：

```powershell
git status --short
```

最小语法检查：

```powershell
.\.venv\Scripts\python.exe -m py_compile path\to\changed_file.py
```

Paramiko/SSH 相关定向回归：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_paramiko_password_transport.py `
  tests\test_tunnel_profiles.py `
  tests\test_ssh_control_master.py -q
```

完整测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

本仓库部分旧测试带有 POSIX 路径、`/bin/sh` 或 Windows 符号链接权限假设。
若完整测试失败，应报告具体测试文件和原因；不要笼统地把所有失败都归因于 Windows。

传输层修改还必须做真实闭环：

```text
start
-> status
-> eval '1+1'
-> stop
-> 确认本地监听端口关闭且 helper 无残留
-> start
-> status
```

## 12. 安全与凭据

- 密码只从用户级 `.env` 读取；
- 不在命令行参数中传密码；
- 不打印 transport 对象内部字段或进程环境；
- 不把密码写入状态文件；
- 保持 SSH 主机 SHA256 指纹校验；
- `.env` ACL 应限制为当前 Windows 用户可访问；
- 不提交 `.env`、私钥、日志或包含连接信息的临时文件；
- 诊断时只输出键名、布尔状态、PID 和端口归属，不输出 secret 值。

## 13. 交付前最终检查

Agent 在宣称任务完成前至少确认：

```text
[ ] status 显示 tunnel running
[ ] status 显示 daemon OK
[ ] daemon user 与 tunnel user 一致
[ ] eval '1+1' 返回 2
[ ] 目标对象已重新读取验证
[ ] 写操作已 schCheck 并保存
[ ] 没有覆盖非目标设计
[ ] 没有泄露 .env 或密码
[ ] 没有遗留临时 helper、补丁、脚本或远端测试文件
[ ] 已向用户报告剩余人工确认项
```

如任何一项无法确认，Agent 应明确报告“不确定/未验证”，而不是推断成功。
