<p align="center">
  <img src="images/skillpulse-banner.png" alt="SkillPulse" width="760" />
</p>

<h3 align="center">面向 Agent Skill 的运行监测与安全生命周期管理</h3>

<p align="center">
  基于真实执行结果检测退化、分析原因，并在外部候选版本晋升前完成离线回放与灰度验证。
</p>

<p align="center">
  <a href="https://github.com/HarryFunn/skillpulse/actions/workflows/ci.yml"><img src="https://github.com/HarryFunn/skillpulse/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-059669.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB.svg" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/runtime_dependencies-0-2F3337.svg" alt="零运行时依赖" />
</p>

<p align="center">
  <a href="README.md">English</a> | <strong>简体中文</strong>
</p>

---

SkillPulse 根据真实执行记录持续评估每个 Agent Skill 版本的运行表现。执行结果既可以手工记录，也可以从 Langfuse 和 Phoenix 自动同步。SkillPulse 通过统计方法识别显著退化，区分环境变化、模型切换、任务分布变化和 Skill 自身缺陷，并管理一套完整的 **检测 → 归因 → 提交候选版本 → 离线回放 → 灰度验证 → 晋升/回滚** 流程。

与仅依赖上游版本、Git 提交或文件哈希的工具不同，SkillPulse 关注的是 Skill 在实际运行中是否仍然有效。

SkillPulse 本身目前提供 Python 库和 CLI，不包含 Web 前端；仓库里的截图是终端输出。
Langfuse 和 Phoenix 各自保留 Web 界面，用于查看原始 trace。

## 核心能力

- **基于真实执行结果检测退化**：使用双样本比例 z 检验，对比 Skill 的近期成功率与历史基线，识别仅靠版本号、Git 提交或文件哈希无法发现的运行质量下降。
- **分析退化原因**：根据成功率变化、错误类型、模型信息和任务标签，将问题归因于环境变化、模型切换、任务分布变化或 Skill 自身缺陷，并给出相应的处理建议。
- **分阶段验证候选版本**：候选版本先接受历史样本回放；通过后才进入 probation 并承接少量线上调用。
- **支持安全回滚**：候选版本验证失败时保留原有现役版本，避免未经验证的修改影响全部调用。
- **完整记录生命周期**：Skill 的创建、退化标记、候选版本提交、晋升、拒绝和退役等操作都会写入审计日志。
- **复用现有可观测数据**：将 Langfuse 的 trace/score 和 Phoenix 的根 span/annotation 转换为幂等的 `SkillRun`，无需建立另一套相互割裂的数据孤岛。
- **轻量且无运行时依赖**：基于 Python 标准库和 SQLite 实现，可作为 Python 库使用，也可通过命令行操作。

## 安装

在仓库根目录执行：

```bash
pip install -e .
```

如需运行测试：

```bash
pip install -e ".[dev]"
```

## CLI 快速开始

```bash
# 注册 Skill，v1 自动成为现役版本
skillpulse add scraper --name "抓取页面标题" --content-file skill.txt

# 记录完整 Skill 执行的最终结果
skillpulse run-record scraper --ok --run-id run-001
skillpulse run-record scraper --fail --run-id run-002 \
  --error "SelectorNotFound" --model claude-sonnet --tag web-scraping

skillpulse status
skillpulse doctor
skillpulse attribute scraper

# 提交候选版本；此时不能承接线上流量
skillpulse repair scraper --content-file candidate-skill.txt

# replay-results.json 的格式为 {"历史 run_id": true/false}
skillpulse replay scraper 2 --results replay-results.json

# 离线回放通过后，candidate 才会进入 probation
skillpulse evaluate scraper      # promoted | rejected | pending

# 输出机器可读的完整报告
skillpulse report --output report.json
```

## 从 Langfuse 或 Phoenix 导入执行结果

适配层读取每条 trace 的最终/根操作，关联 trace 级与根操作级评价证据，映射到
已注册的 Skill 版本，再写入一条幂等的 `SkillRun`。子 span 仍保留在可观测平台中：某个子操作成功，
并不等于完整 Skill 成功。

<p align="center">
  <img src="images/observability-sync.png" alt="Langfuse 与 Phoenix 同步演示" width="920" />
</p>

截图来自 `python -m demo.integrations`。这是一个离线契约演示，使用本地 API
fixture 驱动真实 HTTP 客户端、映射层、CLI 和 SQLite 存储；无需平台账号，也不会向
外部发送数据。

同步前先注册 Skill。映射既可以通过 CLI 为整个项目提供默认值，也可以由每条根
trace 上的命名空间字段提供。

### Langfuse

SkillPulse 使用当前的
[Observations API v2](https://langfuse.com/docs/api-and-data-platform/features/observations-api)
和 [Scores API v3](https://langfuse.com/changelog/2026-06-10-scores-v3-api)。
项目密钥通过环境变量提供，不会出现在命令行参数中。

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
# 自托管或其他云区域可覆盖：
export LANGFUSE_BASE_URL=https://cloud.langfuse.com

skillpulse --db skills.db sync langfuse \
  --skill-id support-answer \
  --success-score correctness \
  --success-threshold 0.8 \
  --since 24h
```

### Phoenix

SkillPulse 使用 Phoenix 当前的项目级
[根 span](https://arize.com/docs/phoenix/sdk-api-reference/rest-api/api-reference/spans/list-spans-with-simple-filters-no-dsl)
以及 [trace](https://arize.com/docs/phoenix/sdk-api-reference/rest-api/api-reference/annotations/get-trace-annotations-for-a-list-of-trace_ids)
与 [根 span annotation](https://arize.com/docs/phoenix/sdk-api-reference/rest-api/api-reference/annotations/get-span-annotations-for-a-list-of-span_ids)
REST API。自托管实例未启用认证时，可以不设置 bearer token。

```bash
export PHOENIX_BASE_URL=http://localhost:6006
export PHOENIX_API_KEY=...

skillpulse --db skills.db sync phoenix \
  --project support-production \
  --skill-id support-answer \
  --success-score correctness \
  --success-threshold 0.8 \
  --since 24h
```

### 使用 Docker Desktop 运行真实 provider 测试

默认测试套件完全离线。下面的可选测试会部署真实服务，分别在两个 provider 中创建
一条合成根 trace 和 `correctness` 评价，通过生产适配器同步到临时 SQLite 数据库，
再同步一次验证幂等去重。

已验证的基线是 Langfuse `3.222.0`（官方源码提交
`d70f258e8230b20c548bb74a3b272c1f30cc097f`）和 Phoenix `19.2.0`
（`arizephoenix/phoenix@sha256:e90f05fc04d7507a948128ddd701d89ad1816a1b6fcccfb9feebd0f77d5e86a3`）。
Docker Desktop 会直接在 macOS 上运行这些 Linux 容器，不需要额外安装一个 Linux 系统。

创建被 Git 忽略的本地运行目录，并只下载 Langfuse 官方部署文件：

```bash
mkdir -p integration-runtime/langfuse
curl -fsSL \
  https://raw.githubusercontent.com/langfuse/langfuse/d70f258e8230b20c548bb74a3b272c1f30cc097f/docker-compose.yml \
  -o integration-runtime/langfuse/docker-compose.yml
curl -fsSL \
  https://raw.githubusercontent.com/langfuse/langfuse/d70f258e8230b20c548bb74a3b272c1f30cc097f/packages/shared/clickhouse/scripts/dev-tables.sh \
  -o integration-runtime/langfuse/dev-tables.sh
cd integration-runtime/langfuse
```

创建 `.env`，写入仅供本地测试的无交互初始化配置：

```dotenv
NEXTAUTH_URL=http://localhost:3000
NEXTAUTH_SECRET=skillpulse-local-nextauth-secret-2026
SALT=skillpulse-local-salt-2026
ENCRYPTION_KEY=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
TELEMETRY_ENABLED=false
LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN=true
LANGFUSE_MIGRATION_V4_WRITE_MODE=events_only
LANGFUSE_MIGRATION_V4_NATIVE_OTEL_BEHAVIOUR=direct
LANGFUSE_INIT_ORG_ID=skillpulse-local-org
LANGFUSE_INIT_ORG_NAME=SkillPulse Local Integration
LANGFUSE_INIT_PROJECT_ID=skillpulse-local-project
LANGFUSE_INIT_PROJECT_NAME=SkillPulse Local Integration
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=lf_pk_skillpulse_local_test_2026
LANGFUSE_INIT_PROJECT_SECRET_KEY=lf_sk_skillpulse_local_test_2026
LANGFUSE_INIT_USER_EMAIL=skillpulse-local@example.test
LANGFUSE_INIT_USER_NAME=SkillPulse Local
LANGFUSE_INIT_USER_PASSWORD=SkillPulseLocalTest2026
```

创建 `docker-compose.override.yml`，让当前 v4 预览配置同时传入 web 和 worker：

```yaml
services:
  langfuse-worker:
    environment:
      LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN: "true"
      LANGFUSE_MIGRATION_V4_WRITE_MODE: events_only
      LANGFUSE_MIGRATION_V4_NATIVE_OTEL_BEHAVIOUR: direct
  langfuse-web:
    environment:
      LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN: "true"
      LANGFUSE_MIGRATION_V4_WRITE_MODE: events_only
      LANGFUSE_MIGRATION_V4_NATIVE_OTEL_BEHAVIOUR: direct
```

用固定版本的 Langfuse 官方脚本初始化预览 event 表，再启动完整服务：

```bash
docker compose up -d --wait postgres clickhouse redis minio
docker compose cp dev-tables.sh clickhouse:/tmp/dev-tables.sh
docker compose exec -T \
  -e CLICKHOUSE_MIGRATION_URL=clickhouse://localhost:9000 \
  -e CLICKHOUSE_USER=clickhouse \
  -e CLICKHOUSE_PASSWORD=clickhouse \
  -e CLICKHOUSE_DB=default \
  clickhouse bash /tmp/dev-tables.sh
docker compose up -d --wait
curl -fsS http://127.0.0.1:3000/api/public/health
cd ../..
```

从固定摘要的官方镜像启动 Phoenix：

```bash
docker run -d --name skillpulse-phoenix-real \
  -p 6006:6006 -p 4317:4317 \
  -e PHOENIX_WORKING_DIR=/mnt/data \
  -v skillpulse_phoenix_data:/mnt/data \
  arizephoenix/phoenix@sha256:e90f05fc04d7507a948128ddd701d89ad1816a1b6fcccfb9feebd0f77d5e86a3
curl -fsS http://127.0.0.1:6006/healthz
```

回到仓库根目录运行真实集成测试：

```bash
export LANGFUSE_PUBLIC_KEY=lf_pk_skillpulse_local_test_2026
export LANGFUSE_SECRET_KEY=lf_sk_skillpulse_local_test_2026
export LANGFUSE_BASE_URL=http://127.0.0.1:3000
export PHOENIX_BASE_URL=http://127.0.0.1:6006
SKILLPULSE_REAL_INTEGRATION=1 python -m pytest -q \
  tests/integration/real/test_real_providers.py
```

provider 源码、测试密钥、数据库和容器运行数据只存在于被忽略的
`integration-runtime/` 边界或 Docker volume 中，不属于安装包，也不会进入提交；
Git 只跟踪可复现的播种器、断言和本部署教程。

### 映射规则

当一个 provider 项目只对应一个 Skill 时，CLI 默认值最方便。一个项目包含多个
Skill 时，可以省略 `--skill-id`/`--version`，改为在 Langfuse 根 observation
metadata 或 Phoenix 根 span attributes 中写入命名空间字段。

| SkillRun 字段 | 解析顺序 |
| --- | --- |
| `skill_id` | `--skill-id`，然后是 `skillpulse.skill_id`（必填；未知 Skill 会跳过） |
| `version` | `--version`、`skillpulse.version`，然后是已注册的现役版本 |
| `success` | `--success-score` 指定的评价、`skillpulse.success`、provider 根状态 |
| `task_tag` | `skillpulse.task_tag`，然后是 trace/根 span 名称 |
| `model`、输入、输出、session | 从 provider 根字段或 attributes 标准化读取 |

`--success-score` 是严格规则：某条 trace 缺少指定评价时会被跳过，不会静默退回
到 span 状态。布尔值和分类评价使用 pass/fail 标签；数值评价使用
`--success-threshold`。导入时会保留 provider metadata 和所有已读取的评价证据，
方便后续审计。同名评价同时存在于两个层级时，使用更新时间最新的一条。

同步使用 cursor 分页，并把 checkpoint 保存在同一个 SQLite 文件中。只有完整轮询
窗口全部成功后才推进 checkpoint；下一轮会从高水位向前重叠 10 分钟，再通过稳定
provider ID 去重，从而兼顾延迟到达的数据和幂等性。首次同步默认读取过去 24 小时。
可通过 `--since 7d`、ISO-8601 时间或 epoch 秒覆盖，也可用 `--no-checkpoint`
执行一次性读取。

统一的 Python API 位于 `skillpulse.integrations`：

```python
from skillpulse.integrations import (
    LangfuseSource, MappingConfig, RunMapper, RunSynchronizer,
)

result = RunSynchronizer(
    store,
    RunMapper(MappingConfig(
        skill_id="support-answer",
        success_score="correctness",
        success_threshold=0.8,
    )),
).sync(LangfuseSource(), since=None)
```

## 作为 Python 库使用

```python
from skillpulse import LifecycleManager, SkillRun, SkillStore

store = SkillStore("skills.db")
manager = LifecycleManager(store)
store.add_skill("scraper", "抓取页面标题", content="selector = 'head > title'")
manager.activate_initial("scraper")

store.record_skill_run(SkillRun(
    run_id="run-001",
    skill_id="scraper",
    version=1,
    success=True,
    input_data={"url": "https://example.test"},
))

if manager.scan():
    candidate = manager.repair(
        "scraper",
        repair_fn=lambda old, reasons: generate_candidate(old, reasons),
    )
    replay = manager.replay(
        "scraper", candidate.version,
        replay_fn=lambda candidate_content, historical_run: replay_in_sandbox(
            candidate_content, historical_run.input_data),
    )
    if replay.passed:
        version = manager.route("scraper")  # candidate 此时才有资格承接灰度流量
```

## 退化检测机制

SkillPulse 综合使用以下三类信号评估 Skill 版本的运行状态。

### 1. 近期成功率显著下降

将近期窗口的成功率与长期历史基线进行单侧双样本比例 z 检验。当统计量超过 `z_threshold` 时，判定近期表现出现显著下降。

默认阈值为 `1.645`，对应约 95% 的单侧置信水平。这类信号适合识别 API、页面结构或外部服务突然变化造成的集中失败。

### 2. EWMA 成功率低于下限

使用指数加权移动平均（EWMA）提高近期执行结果的权重。当 EWMA 低于 `ewma_floor` 时，将 Skill 标记为退化。

该指标用于识别缓慢发生的质量下降，也能发现长期表现不稳定的 Skill。

### 3. 长期未验证

如果某个版本超过 `stale_after_days` 没有执行，SkillPulse 会将其报告为长期未验证。默认情况下，该信号只产生提示；将 `stale_is_degraded` 设为 `True` 后，也可直接将其视为退化。

检测参数通过 `HealthConfig` 配置，灰度验证参数通过 `ProbationConfig` 配置。

## 退化原因分析

检测到退化后，`Attributor` 会结合执行记录中的可解释信号判断原因，包括成功率变化幅度、主要错误类型、模型变化和任务分布变化。

### 环境变化（`environment_drift`）

典型特征：成功率突然下降、失败集中于同一种错误，同时模型与任务类型保持不变。

建议：检查 API、网页结构、数据格式或外部依赖是否发生变化，并据此修复 Skill。

### 模型切换（`model_change`）

典型特征：失败主要发生在历史健康阶段未使用过的新模型上。

建议：优先重新验证提示词、工具调用格式和模型兼容性，而不是直接修改 Skill 的业务逻辑。

### 任务分布变化（`task_drift`）

典型特征：失败主要来自历史健康阶段没有覆盖过的新任务类型。

建议：调整 Skill 的描述或触发条件，限制适用范围；必要时为新任务创建独立 Skill。

### Skill 自身缺陷（`skill_defect`）

典型特征：不存在清晰的突发变化，Skill 在较长时间内持续出现不同类型的失败，也无法由模型或任务变化解释。

建议：重新设计或重写 Skill，而不是继续叠加局部补丁。

为了提高归因质量，建议在记录执行结果时提供 `model` 和 `task_tag`：

```bash
skillpulse record scraper \
  --fail \
  --error "SelectorNotFound" \
  --model claude-sonnet \
  --tag web-scraping
```

归因阈值通过 `AttributionConfig` 配置。

## 两级候选验证

```text
active ──检测到退化──► degraded ──外部提交候选──► candidate
                                             │
                                       离线历史回放
                                       ├── 未通过 ──► candidate
                                       └── 通过 ──► probation
                                                      │
                                                线上灰度验证
                                                ├── 通过 ──► active（旧版本 retired）
                                                └── 未通过 ──► rejected
```

离线回放同时计算两个指标：历史失败样本的 `fix_rate`，以及历史成功样本的
`regression_rate`。只有两项均达到阈值，candidate 才能进入 probation 并承接线上灰度流量。

各状态含义：

- `candidate`：新建但尚未验证的版本。
- `probation`：正在接受灰度验证的版本。
- `active`：当前现役版本。
- `degraded`：已检测到运行质量下降的版本。
- `retired`：已被新版本替换的历史版本。
- `rejected`：未通过灰度验证的版本。

## 运行演示

```bash
python -m demo.simulate
```

演示脚本模拟如下流程：

1. 页面标题抓取 Skill 在初始阶段保持正常。
2. 目标网站调整 HTML 结构，旧选择器开始持续失败。
3. SkillPulse 检测到成功率显著下降并归因为环境变化。
4. 提交候选版本，候选版本暂不承接线上流量。
5. 回放历史成功/失败样本，验证修复率和回归率。
6. 回放通过后进入 probation，并接受线上灰度验证。
7. 达到成功率要求后晋升，旧版本转为退役状态。
8. 输出完整的生命周期审计记录。

## ToolCall 与 SkillRun

SkillPulse 明确区分两层执行数据：

- `ToolCall`：Agent 会话中的单次工具调用。
- `SkillRun`：一次完整 Skill 执行的最终结果，其中可以包含多个 ToolCall。

Claude Code 和 Codex 的本地日志主要暴露工具调用，因此 `ingest` 只导入 ToolCall。
它不会把一次工具调用成功等同于整个 Skill 成功，也不会把工具名自动注册为 Skill。

```bash
skillpulse ingest ~/.claude/projects --format claude
skillpulse ingest ~/.codex/sessions --format codex
```

导入过程支持幂等执行。SkillPulse 根据会话文件路径和原始 call ID 生成稳定标识，
重复导入同一日志不会产生重复数据。CLI 会分别报告 `added`、`duplicates`、
`skipped` 和 `files`。没有匹配结果的调用会作为不完整记录跳过。

完整 Skill 的最终结果应通过 `run-record` 或 Python API `record_skill_run()` 单独记录。
通过可重复使用的 `--tool-call-id <稳定标识>` 参数，可以把已导入的 ToolCall 关联到该
SkillRun。退化检测、原因归因、离线回放和 probation 评估均使用 SkillRun，而不是原始工具调用结果。

Langfuse 和 Phoenix 适配器遵守同一边界：它们从 trace 根操作和最终评价证据创建
一条 `SkillRun`，不会把任意子 span 名自动注册为 Skill，也不会把子 span 状态当作
完整 Skill 的最终结果。

## 运行测试

```bash
pytest
```

当前测试覆盖：

- Skill 和版本的创建、激活与存储。
- z 检验、EWMA 与长期未验证检测。
- 退化标记与审计记录。
- ToolCall 与 SkillRun 的分层存储及幂等写入。
- 离线回放的修复率、回归率和 probation 准入门。
- 修复版本的灰度路由、晋升和拒绝。
- 环境变化、模型切换、任务分布变化和 Skill 缺陷归因。
- Claude Code 与 Codex 日志解析、重复导入和统计结果。
- Langfuse/Phoenix API 映射、评价优先级、分页、checkpoint 和重复同步。
- JSON 报告 schema 与旧数据库自动迁移。

## 许可证

本项目采用 [MIT License](LICENSE)。
