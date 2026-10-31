# TechDoc RAG

单机、单用户的技术资料知识库工作区。支持知识库管理、资料版本、问答、可核对原文的引用、笔记、概览、批量导入与会话历史。桌面采用分栏工作区，手机采用可展开资料抽屉。

这是最小部署源码：不含语料、数据库、模型权重、密钥、证书私钥、历史补丁和评测原始数据。运行时文件与本项目 2026-10-31 修复版本一致。

## 直接运行（Python 3.11）

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-minimal.txt -c constraints-runtime.txt
export TECHDOC_DATA_DIR="$PWD/runtime_data"
export TECHDOC_KNOWLEDGE_MODE=1
export TECHDOC_DEPLOYMENT_MODE=local
python -m uvicorn techdoc.api:app --host 127.0.0.1 --port 8765 --workers 1
```

打开 `http://127.0.0.1:8765/`。这个最小配置使用 BM25，仅适合本机直接访问；匿名 local 模式拒绝非回环客户端和代理转发头。Windows 使用 `.venv\Scripts\Activate.ps1` 与 PowerShell 的 `$env:变量名='值'` 设置环境。

## 容器运行

```bash
cp .env.example .env
# 编辑 .env，填入自己的登录账号、随机长口令；需要模型生成时填写三个 LLM 变量
docker compose up -d --build
```

容器默认使用认证模式，监听宿主机 `127.0.0.1:8765`，数据持久化到命名卷。首次启动在页面创建知识库，再导入自己的资料。默认 BM25 不需要本地模型；生成回答需要完整配置兼容 Chat Completions 的接口。

### BGE-M3 + BGE-reranker-v2-m3

准备完整、可信的本地模型目录，并在 `.env` 填写 `BGE_M3_HOST_DIR` 和 `RERANKER_HOST_DIR`：

```bash
docker compose -f compose.yaml -f compose.dense.yaml up -d --build
```

Dense 镜像安装 CPU PyTorch；模型目录只读挂载，不自动下载。启用 Hybrid、INT8、隔离评分进程、增量暂存索引。x86 主机使用 oneDNN；其他架构需要核对 PyTorch 量化支持并调整配置。模型和语料规模决定内存需求；冷问题仍可能耗时数十秒。

LLM 示例模型名为 `deepseek-v4.1-flash`。`LLM_BASE_URL` 填提供商要求的兼容 API 基址（通常包含 `/v1`），`LLM_API_KEY` 使用自己的密钥；不要将 `.env` 提交到 Git。

## 公网与双栈

公网必须保持 `TECHDOC_DEPLOYMENT_MODE=public` 并设置两个浏览器凭据，缺少任意一项会启动失败。HTTP Basic 必须置于可信 HTTPS 后；共享账号不提供多用户数据隔离。

公网代理应关闭 SSE 缓冲，允许长请求，保持原 Authorization，避免自动重放 POST。单文件上限 8 MiB，代理请求体上限可设 9 MiB；一次多选仍逐文件提交。示例见 [部署说明](DEPLOYMENT.md)。

## 备份与更新

停止所有使用同一数据目录的应用进程后执行：

```bash
python -m scripts.backup_workspace backup /path/to/runtime_data /path/to/backup.zip
python -m scripts.backup_workspace restore /path/to/backup.zip /path/to/new-empty-directory
python -m scripts.audit_catalog --data-dir /path/to/runtime_data
```

容器命名卷请先停服并挂载到维护容器执行上述命令。不要用空卷覆盖旧数据，不要并行启动两个进程使用同一工作区。已有旧版快照目录需要离线迁移，启动不会静默改造非空历史数据。

## 验证与边界

公开仓库保留最小部署、合成回归子集及 GitHub Actions 验证流程；完整工程测试和真实语料不在本仓库。最小 CPU 镜像已经在 CI 与云主机实际构建并通过容器内启动、认证、导入和原文读取检查。基础镜像固定到官方摘要，constraints-runtime.txt 固定已验证的最小运行依赖；Dense 镜像及真实模型挂载尚需单独验收。

```bash
python scripts/smoke_minimal.py
python -m scripts.evaluate_regression --output-dir /tmp/rag-regression
# 需要安装 pytest，以下测试不调用外部模型
python -m pytest tests -q
```

统一回归包括 80 条模型替身回答合同用例、3 条实际 BM25 合成定位用例和 6 条定位／引用关联合同，记录源码身份与策略，输出 JSON/Markdown 并在错误时返回非零退出码。未测的真实 Dense 质量、真实生成准确率和人工 Gold 明确留空。

资料名、PDF 物理页和章节定位目前只做影子观察：解析已选资料中的精确名称，比较现有候选的元数据顺序，不改变正式检索、模型输入和答案，不增加检索或模型评分。普通“第几页”、印刷页码、多文件与多个页码的配对均不猜测；未进入候选池的目标页也不会由影子观察补回。相关诊断不代表已验证章节结构。

包含多个明确问号或分号的问题，可在回答下展开“按问题核对原文”。关联仅使用本次回答已有的有效引用，依据文字重合供查阅；未关联不等于没有答案，关联也不证明该部分已回答正确。旧回答保留原版本引用，撤销资料后隐藏失效关联。

模型限额、频率、网络、认证与格式异常分别显示；“只查原文”会保护已有草稿，回填原问题并切换模式，由用户确认发送。系统不会因模型额度不足静默更换模型或发布未审核草稿。

隔离重排 worker 异常后，当前任务失败结束，后续请求可受控重建；初始退避 2 秒，5 分钟内最多 3 次恢复尝试，不自动重放生成请求。恢复仍占用全局推理预算，并核对模型身份，取消时先清理子进程。正常关闭不重启。公网 `/health` 只返回状态，不可用时为 503；详细 worker 状态保留在受认证的 capabilities 内。

`LLM_TIMEOUT_S` 默认 30 秒，可设 30～90 秒，非法值启动失败。此值作用于每次供应商请求，生成、复核和重试的累计耗时会更长。认证失败按来源限流：60 秒内 20 次带凭据的失败后返回 429；普通未登录挑战不计入，成功请求不清零失败窗口。代理链需要可靠来源信息，切勿信任客户端自带的转发头；双栈隧道说明见 DEPLOYMENT.md。

长期维护先运行 `python -m scripts.maintain_workspace /path/to/runtime_data` 查看报告。需要整理时停服并指定 `--apply --backup /outside/data/before.zip`；仅清理超过保留期的终态中间进度事件，保留终态 SSE、任务、幂等回执、消息、回答、引用和全部原件。默认 30 天，最少 7 天。不会自动 VACUUM 或物理删除资料；业务历史和回执暂不自动过期。

引用逐字核验和模型复核不等于事实保证。针对明确数值绑定、单位、否定、条件和范围矛盾有保守自动防护；未知改写仍可能出错，关键结论应核对引用。48 条模型替身对抗用例通过，不代表真实模型准确率或独立人工 Gold。解析以文本为主，不保证扫描件 OCR、复杂图表或图片事实。物理历史回收、资源自动重排队和多租户功能未提供。
