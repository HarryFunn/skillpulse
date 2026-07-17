# SkillGuard

[![CI](https://github.com/HarryFunn/skillguard/actions/workflows/ci.yml/badge.svg)](https://github.com/HarryFunn/skillguard/actions/workflows/ci.yml)

[English](README.md) | **中文**

面向 Agent 技能库的版本管理与退化检测工具。

Agent 的技能库会**腐烂**。今天还能用的 skill，会在 API 改版、网页重构或底层模型
更换后悄悄失效。大多数工具靠**代理信号**判断陈旧——上游版本号、git 提交、文件哈希
——却从不关心这个 skill 是否**还能跑通**。

SkillGuard 换个思路，盯住**执行流**：持续统计每个 skill 版本的真实成功率，用统计
检验标记显著退化，并管理一套安全的**修复 → 灰度 → 晋升/回滚**闭环——让坏掉的 skill
不会被静默覆盖，坏的修复也不会一次性放量到全部流量。

## 有何不同

- **基于结果判定退化，而非元数据。** 用双比例 z 检验，把 skill 近期成功率和它自己的
  长期基线对比，抓住 version-diff 类工具漏掉的静默失效。
- **根因归因。** 只知道 skill 坏了还不够，得知道**该怎么办**。SkillGuard 把每次退化
  归因到四类原因之一——环境漂移、模型更换、任务漂移、技能内在缺陷——并各自映射到
  不同的推荐动作。
- **修复走灰度门，不盲改。** 修复后的版本先进入 canary 灰度试用（流量占比可配），
  只有在最小试验数内成功率达标才晋升；否则回滚，旧版本继续服务。
- **完整审计轨迹。** 每一次状态变更都记录在案，任何版本为何被标记、修复、晋升或退役
  都可追溯。
- **零依赖。** 纯标准库 + SQLite，既可当库用，也可当 CLI 用。

## 安装

```bash
pip install -e .          # 在仓库根目录执行
```

## 快速上手（CLI）

```bash
# 注册一个 skill（首个版本自动激活）
skillguard add scraper --name "抓取页面标题" --content-file skill.txt

# agent 每次运行 skill 后，把执行结果记进来
skillguard record scraper --ok
skillguard record scraper --fail --error "SelectorNotFound"

# 一眼看整个技能库的健康度
skillguard status

# 运行退化检测，退化的 skill 会被标记
skillguard doctor

# 对退化做根因归因，给出推荐动作
skillguard attribute scraper

# 创建修复版本 -> 进入 canary 灰度试用
skillguard repair scraper --content-file fixed_skill.txt

# canary 承接足够流量后，决定它的命运
skillguard evaluate scraper      # -> promoted | rejected | pending

# 查看版本树与审计事件
skillguard history scraper
```

## 快速上手（作为库）

```python
from skillguard import SkillStore, LifecycleManager, ExecutionRecord

store = SkillStore("skills.db")
mgr = LifecycleManager(store)

store.add_skill("scraper", "抓取页面标题", content="selector = 'head > title'")
mgr.activate_initial("scraper")

# agent 运行时记录结果
store.record_execution(ExecutionRecord("scraper", 1, success=True))

# 定期扫描退化
flagged = mgr.scan()

# 修复退化的 skill（把你自己的 LLM / 人工修复接进 repair_fn）
if flagged:
    mgr.repair("scraper", repair_fn=lambda old, reasons: fix(old, reasons))

# 路由调用：canary 分到一部分流量，其余走现役版本
version = mgr.route("scraper")

# canary 试验数够了以后，决定晋升/回滚
mgr.evaluate_probation("scraper")
```

## 退化是怎么检测的

一个 skill 版本在以下任一条件成立时被标记为 **DEGRADED**：

1. **近期 vs 基线下跌** —— 对近期窗口成功率与长期基线做单侧双比例 z 检验，超过
   `z_threshold`（默认 1.645 ≈ 95% 置信）。抓突发的环境失效。
2. **EWMA 跌破下限** —— 指数加权成功率低于 `ewma_floor`。抓缓慢退化。
3. **陈旧** —— 超过 `stale_after_days` 没有执行。默认只告警；`stale_is_degraded`
   打开时才判为退化。

所有阈值在 `HealthConfig` 里；灰度行为在 `ProbationConfig` 里。

## 根因归因

skill 被标记后，`Attributor` 从执行流的可解释信号（变点陡峭度、主导错误签名、
模型迁移、任务分布外）判断它**为什么**退化，并推荐动作：

- **环境漂移** —— 突变式失效、单一主导错误、模型与任务不变
  → **修复** skill 以适配新环境。
- **模型更换** —— 失败集中在健康期从未见过的模型上
  → **重新验证**；修复更可能是 prompt/模型适配，而非改 skill 逻辑。
- **任务漂移** —— 失败集中在健康期从未见过的任务类型上
  → **收窄触发范围**；skill 是被用在分布外场景，而非坏了。
- **技能缺陷** —— 全程 flaky 且无外部解释
  → **重写** 而非打补丁。

归因需要执行记录上的可选字段 `model` 和 `task_tag`
（`skillguard record ... --model <m> --tag <t>`）。阈值在 `AttributionConfig` 里。

## 生命周期状态机

```
candidate ──activate/promote──► active ──检测到退化──► degraded
    ▲                             │                        │
    │                             │ repair()               │ repair()
    └─────────── probation ◄──────┴────────────────────────┘
                    │
        晋升（达标）──► active   （旧版本 ──► retired）
        回滚（不达标）──► rejected（现役版本继续服务）
```

## 跑一下 demo

```bash
python -m demo.simulate
```

模拟一个 scraper skill 在目标站点改版后失效的全过程：SkillGuard 检测到下跌、
标记、把修复版本纳入 canary 试用、验证通过后晋升——并打印完整审计轨迹。

## 导入你的真实使用数据

不用合成数据，直接把 SkillGuard 指向 agent 的本地会话日志。它会把每个工具/skill
调用与其结果配对记录，没见过的 skill 自动注册。

```bash
# Claude Code 会话记录
skillguard ingest ~/.claude/projects --format claude

# Codex rollout
skillguard ingest ~/.codex/sessions --format codex

# 然后分析你自己的历史
skillguard status
skillguard doctor
skillguard attribute <skill_id>
```

成败判定启发式：Claude Code 中 `tool_result` 带 `is_error: true` 记为失败；
Codex 中工具输出的 `exit_code` 非零或带 `error` 字段记为失败。会捕获 `model` 和
`task_tag`（来自会话的 cwd）以支撑归因。没有对应结果的调用视为不完整而跳过。

## 运行测试

```bash
pip install -e ".[dev]"
pytest
```

## 许可证

MIT
