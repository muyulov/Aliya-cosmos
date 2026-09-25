# config 层 YAML 化重构 设计

日期：2026-09-25
状态：设计已确认，待实施

## 一、目标与范围

把配置层从「环境变量为中心」重构为「YAML 文件为中心」。

- `data/config/app.yaml` 是配置的唯一骨架，进仓库、可安全提交。
- `.env` 退化为只给 `app.yaml` 里的占位符喂值（密钥类字段）。
- 配置通路只有一条，不存在「谁覆盖谁」的优先级歧义。

非目标（本次不做）：按环境拆多文件、配置热重载、多配置源合并、`Vault` 等外部密钥后端、`log.dir` 路径锚定调整。

## 二、决策记录

| # | 议题 | 结论 | 被否决的方案与原因 |
| --- | --- | --- | --- |
| 1 | YAML 与既有机制的关系 | YAML 为骨架，`.env` 只存隐私信息（密钥） | 完全取代（部署无法注入）；双轨并存（来源有歧义） |
| 2 | 敏感字段如何体现 | YAML 写占位符 `${VAR}`，加载时插值 | YAML 完全不写该字段（配置结构不完整，看不出缺哪把钥匙） |
| 3 | 是否保留 `APP_*` 直接覆盖 | **不保留**，YAML 是唯一配置来源 | 两套并存（要同时查三处，排查成本高） |
| 4 | 文件布局 | 单文件 `data/config/app.yaml`，顶层按 `app:` / `log:` 分组 | 按环境拆文件（要先读配置才知道读哪个文件，鸡生蛋）；按模块拆文件（每加模块都要改合并逻辑） |
| 5 | 缺失处理 | 文件缺失 → 退默认值；占位符无值且无默认 → **fail fast** | 一律 fail fast（破坏「克隆即可跑」）；一律宽松（错误点离病因太远） |
| 6 | 路径解析 | 相对当前工作目录 `Path("data/config/app.yaml")` | 锚定项目根（把目录结构写死进代码）；环境变量指定路径（又引入「配置的配置」） |
| 7 | 实现与依赖 | 纯 `pydantic` + `PyYAML` + `python-dotenv`，**移除 `pydantic-settings`** | 保留 `pydantic-settings`（其核心能力 env 覆盖已被砍，只剩读文件）；手写 `.env` 解析（引号/注释等边界易踩坑） |
| 8 | 占位符语法 | `${VAR}` 与 `${VAR:默认值}` 两种 | 只支持 `${VAR}`（丢失带默认的部署覆盖能力）；完整 shell 表达式（YAML 变模板语言） |
| 9 | 静默降级提示 | `create_app` 启动日志加「配置源」字段 | 不提示（配合 CWD 相对路径会形成排查黑洞）；config 层直接打警告（新增 `config → logger` 依赖边，且加载早于日志装配） |
| 10 | 测试夹具构造方式 | `Settings.model_validate({"app": {...}, "log": {...}})` | 手搓嵌套模型实例（啰嗦、偏离 YAML 形态）；fixture 写临时 YAML 走真实加载（夹具职责过载，文件层另有专测） |

## 三、配置来源与加载流程

```text
core/main.py:  settings = get_settings()
                      │
                      ▼
                load_settings()
                      ├─ ① read_env()      .env（python-dotenv）+ os.environ → dict[str, str]
                      ├─ ② read_yaml()     data/config/app.yaml → dict；文件缺失 → {}
                      ├─ ③ interpolate()   递归展开 ${VAR} / ${VAR:默认值}
                      └─ ④ Settings.model_validate(raw)
```

**① 取值来源**：`.env` 与进程环境变量共同参与占位符取值，**进程环境变量优先**（部署时能顶掉文件里的本地值）。用 `python-dotenv` 的 `dotenv_values()` 读取，只取返回值、**不注入 `os.environ`**，不给进程留下全局副作用。

变量名**精确匹配**（大小写敏感）：去掉 `pydantic-settings` 后不再有变量名净化，`${App_Key}` 与 `${APP_KEY}` 视为两个变量。

`.env` 位于项目根、CWD 相对，与现状一致；缺失属正常，不报错也不提示。

**② 文件读取**：`yaml.safe_load`，顶层必须是映射，否则按配置错误处理。文件不存在返回 `{}`，全部字段走默认值照样能启动。

**③ 插值**：见第四节。

**④ 校验**：`Settings.model_validate(raw)`，非法取值（如 `env: staging`、`port: 70000`）由 pydantic 的 `ValidationError` 兜住。

## 四、占位符规范

正则：`\$\{([A-Za-z_]\w*)(?::([^}]*))?\}`

| 写法 | 语义 |
| --- | --- |
| `${VAR}` | 无默认值。取不到就 `ConfigError` |
| `${VAR:默认值}` | 取不到时用 `默认值`（默认值原样作为字符串使用） |

规则：

- 只作用于 `str` 标量：递归遍历 dict 的 value 与 list 的 item，**dict 的 key 不插值**。非字符串标量原样保留。
- **单趟展开**，变量值里若含 `${...}` 不再二次展开；不支持 `$${VAR}` 转义（YAGNI）。
- 展开后一律是字符串，**类型转换交给 pydantic**。宽松模式下 `"8000"` → `int`、`"true"` → `bool` 均可。
- 递归时携带键路径（如 `app.api_key`），出错信息能直接定位到 YAML 里的哪一行字段。

示例：

```yaml
app:
  env: ${APP_ENV:dev}              # 无 APP_ENV 时用 "dev"
  api_key: ${DEEPSEEK_API_KEY}     # .env 里没有就启动失败（无默认值，fail fast）
```

## 五、错误处理

配置在 `core/main.py` 的 import 期加载，因此所有配置错误都是 **fail fast、进程退出**，与 `ServiceError` 同一风格。

| 场景 | 行为 |
| --- | --- |
| `.env` 不存在 | 正常，继续 |
| YAML 文件不存在 | 正常，退默认值 |
| YAML 语法错 | `ConfigError`，带文件路径与行列号（来自 `yaml.MarkedYAMLError`） |
| YAML 顶层不是映射 | `ConfigError` |
| 占位符无值且无默认 | `ConfigError`，带变量名与键路径 |
| 字段类型或取值非法 | pydantic `ValidationError` 直接冒泡 |

`ConfigError(Exception)` 定义在 `loader.py` 中，跟着抛它的代码走（与 `ServiceError` 定义在 `manager.py` 的既有风格一致）。它是配置期错误，**不是** `AppError`，不会被转成 4xx。

## 六、模块结构与改动清单

```text
core/
  config/
    __init__.py    对外出口（新增导出 ConfigError、CONFIG_FILE）
    settings.py    模型定义 + get_settings 单例（改造）
    loader.py      新增：ConfigError / read_env / read_yaml / interpolate / load_settings
data/
  config/
    app.yaml       新增，提交进仓库
```

`settings.py` 的变化：

- `AppSettings` / `LogSettings` / `Settings` 由 `BaseSettings` 换成 `pydantic.BaseModel`；`model_config` 只留 `populate_by_name=True`。
- `LogSettings.json_output` 继续用 `Field(alias="json")`——`json` 与 `BaseModel` 自带方法撞名，必须走别名。
- `Settings` 新增 `_source: str | None = PrivateAttr(default=None)` 与只读属性 `config_source`，取值三态：YAML 路径 / `内置默认值（未找到 data/config/app.yaml）` / `外部注入`（`_source` 为 `None`，即测试直接构造的实例）。
- `get_settings()` 保持 `@lru_cache(maxsize=1)`，签名不变。

`loader.py` 的函数签名（路径参数可注入，便于测试，无需 `chdir`）：

```python
CONFIG_FILE = Path("data/config/app.yaml")
ENV_FILE = Path(".env")

def read_env(env_file: Path = ENV_FILE) -> dict[str, str]: ...
def read_yaml(config_file: Path = CONFIG_FILE) -> tuple[dict[str, object], str]: ...
def interpolate(value: object, sources: dict[str, str], *, path: str = "") -> object: ...
def load_settings(config_file: Path = CONFIG_FILE, env_file: Path = ENV_FILE) -> Settings: ...
```

改动清单：

| 文件 | 动作 |
| --- | --- |
| `core/config/settings.py` | 换基类、去 env 源配置、加 `_source` / `config_source` |
| `core/config/loader.py` | 新增，约 60 行 |
| `core/config/__init__.py` | 补导出 `ConfigError`、`CONFIG_FILE` |
| `core/main.py` | 启动日志那处 `log.info` 加 `配置源=settings.config_source` 字段；打日志的是入口层，不新增层间依赖 |
| `pyproject.toml` | 依赖：删 `pydantic-settings`，加 `PyYAML>=6.0`、`python-dotenv>=1.0` |
| `data/config/app.yaml` | 新增并提交 |
| `.env.example` | 改写为「密钥清单」式说明 |
| `README.md` | 配置章节重写 |
| `tests/conftest.py` | `test_settings` 改为 `Settings.model_validate({...})` |
| `core/main.py`、`core/service/**`、其余测试 | **不改** |

## 七、`data/config/app.yaml` 初版内容

与模型默认值一致，**不引入任何无默认值的占位符**，保证克隆后无 `.env` 也能直接启动。

```yaml
app:
  app_name: aliya-cosmos
  env: dev
  debug: true

log:
  level: INFO
  json: false
  dir: logs
  rotation: "00:00"           # 必须加引号，见第九节
  retention: 7 days
  compression: zip

# 密钥类字段不进 YAML 明文，以占位符声明后由 .env 提供：
# api_key: ${DEEPSEEK_API_KEY}
```

`AppSettings.app_name` 字段名**保持不变**（YAML 里写作 `app.app_name`）。改名属与本次重构无关的改动，不纳入。

## 八、测试策略

新增 `tests/test_config_loader.py`，通过传入 `tmp_path` 下的文件路径构造用例（不 `chdir`）。

| 类别 | 用例 |
| --- | --- |
| 来源 | 文件缺失 → 全默认值且 `config_source` 为「内置默认值」；正常 YAML → 字段被覆盖、来源为路径 |
| 插值 | `${VAR}` 取 `.env` 值；进程环境变量优先于 `.env`；`${VAR:默认}` 取默认；嵌套 dict / list 内递归展开；dict 的 key 不插值；单趟展开（值里的 `${}` 不再展开） |
| 转型 | `json: true` 走别名生效；插值得到的 `"true"` 能被 pydantic 宽松转成 bool |
| 错误 | `${VAR}` 无值无默认 → `ConfigError` 且消息含变量名与键路径；YAML 语法错 → `ConfigError` 带行列号；顶层是 list → `ConfigError`；`env: staging` → `ValidationError` |
| 单例 | `cache_clear()` 后按新文件重载 |

现有测试除 `conftest.py` 的构造方式外无需改动，用作「重构未破坏契约」的回归证明。

## 九、破坏性变更与风险

**破坏性变更**：`.env` 里原有的 `APP_*` 变量（含 `APP_APP__PORT` 这类双下划线写法）**全部失效**，改由 YAML + 占位符承担。需在 README 与提交信息中写明。

**风险与已规避项**：

- **YAML 1.1 数字陷阱**：PyYAML 兼容 YAML 1.1，`00:00` 会被解析成整数 `0`，故 `rotation` 必须写成 `"00:00"`。同理需警惕形如 `12:34` 的值。
- **CWD 相对路径 + 缺失退默认**：从错误目录启动会静默使用默认值。已用「配置源」启动日志（第二节 #9）覆盖该风险。
- **插值后仍是字符串**：pydantic 宽松模式能处理 `int` / `bool` 等常见转换，但 `str` 字段不会被数字填充（宽松模式不做 `int → str`），写错会在启动期就报错，符合预期。

## 十、验收标准

1. `uv run pytest` 全绿、`uv run ruff check .` 干净、basedpyright 无新增诊断。
2. 给 `data/config/app.yaml` 的某字段写上 `${VAR:默认}` 后，改 YAML 默认值或设环境变量 `VAR` 都能生效。
3. 删掉 `data/config/app.yaml` 仍能启动，启动日志显示「配置源=内置默认值」。
4. 给某字段写上无默认值的占位符、`.env` 里不给值，启动即报错并指出变量名。
