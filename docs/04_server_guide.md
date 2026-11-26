# 服务器使用指南：从首次登录到结果回传

更新：**2026-11-26（Asia/Shanghai）**。适用项目：`INNOCUITY/chinadebt`。
本指南依据硬件截图、项目代码和2026-11-26实际SSH检查维护。已验证密码登录、目录权限、
GitHub连通性；已安装独立Python环境并从服务器抓取、逐日核验和冻结数据。本轮 PPO 入口及运行条件见第 17 节。保留原有Host配置，也可直接使用
`ssh research-server`；密码只在交互提示中输入，不写入文件。
代码目录为 `/srv/ficc/china_debt`，数据、环境和运行产物集中放在 `/srv/ficc`。Mac不负责下载数据集。

这是一份持续维护的操作手册。每次新增训练入口、配置字段、输出文件、恢复方式，都应同步
更新本文件、`scripts/server/`、相关测试与 `docs/code.md`，不要只在聊天里追加命令。

阅读顺序：首次部署按第4–8节；日常运行/下载按第9–11节；算力与训练接入按第12节；
网络受限、清理和验证范围看第13–16节。标注“Mac”的命令在本地执行，标注“服务器”的
命令在SSH登录后执行；不要把两端路径混用。

文中的 `research-server` 与 `/srv/ficc` 均为示例。使用本机 SSH 别名和实际项目目录替换。

## 1. 先认识这台服务器

- 连接名称固定为 **`research-server`**，在Mac终端直接 `ssh research-server`。保留已有Host配置，
  不改服务器hostname，不另建连接别名；截图中的系统提示符不影响这个连接入口。
- CPU：**2×Intel Xeon Platinum 8358P，2.60GHz**；每路32物理核、每核2线程，合计
  **64物理核／128逻辑CPU**。NUMA node0=`0–31,64–95`，node1=`32–63,96–127`。
- GPU：截图为 **8×NVIDIA A800-SXM4-80GB**，每卡81920MiB；不是此前按假设规划的
  40GB。合计容量不表示单任务可自动使用640GB连续显存。
- 主存：约1,031,668MiB，即约1007GiB／约1TiB。截图无swap。
- 驱动：**570.124.06**；`nvidia-smi` 显示 CUDA 12.8。这表示驱动支持能力，不能据此
  认定已经安装系统 CUDA Toolkit、nvcc 或 PyTorch。
- 截图时 CPU idle约91%，但这是历史瞬时值，不是分配给本项目的配额。本计划不以截图中
  GPU 满载情况限制容量规划；实际运行使用获分配的设备，不修改系统驱动或停止已有任务。

资源起点：一项任务1个采样进程、BLAS/OMP各1线程；先测单任务，再2/4/8项并行。
八卡训练优先按seed/fold分任务，通常每任务先预留4个可用CPU核，8任务约32核；不要把
128线程当成128物理核。GPU/NUMA对应关系先读 `nvidia-smi topo -m`，不猜GPU编号映射。

## 2. 当前能运行什么，不能运行什么

当前已实现：数据、曲线、定价、账本/约束、基线策略、归因、风险、ML Ridge/小树、成本后
评估、RL环境与rollout、真实成交example、统一验收。单卡 PPO 入口见第 17 节。

本轮新增的真实入口均在 `scripts/server/`：

- `bootstrap.sh`：无conda安装项目独立环境。
- `server.sh doctor`：采集环境信息，不做GPU训练。
- `data.sh pack/unpack/verify`：冻结数据传输、校验，标准库即可运行。
- `server.sh data-check`：硬校验曲线、真实FDR007、版本指针及研究窗口。
- `server.sh smoke`：example、跨入口对拍、协议quick。
- `server.sh check`：ruff、格式检查、默认pytest，保留JUnit。
- `server.sh models`：全量ML→2 lot主配置成本→1 lot探索成本→完整协议冒烟。
- `server.sh acceptance-buyhold` / `acceptance-rolling` / `acceptance-extended`：三套验收。
- `server.sh research`：六阶段pipeline、曲线诊断、策略、对账、归因、长样本、窗口、风险、
  quick成本研究、lot暴露解释；这是较重的整套研究复现。
- `server.sh offline` / `fetch`：在隔离副本内离线重建／在线抓取，最后仍硬校验融资。
- `server.sh full`：依次运行smoke、check、models、三套acceptance、research；任一失败
  即停止，已完成任务的结果包保留。不会安装环境、刷新数据或训练RL。
- `install_torch.sh`：显式指定版本及官方wheel索引后安装torch，可选。

训练入口为 `scripts/train_rl.py`。旧Makefile里 `backtest`、`sensitivity`、
`report` 等部分目标仍指向尚未实现的文件，`make risk` 也不是已支持的pipeline stage；
当前风险入口是 `scripts/run_risk.py` / `make risk-report`。封装只调用已核对的入口。

## 3. 固定目录与会话配置

```text
/srv/ficc/china_debt/                                Git代码主体，clone/pull均在这里
  exports/                          小型依赖冻结清单（bootstrap自动生成）
/srv/ficc/                    数据盘根目录，不作为Git仓库
  .venv-server-20261126-v2/         当前已通过完整检查的Python环境
  .venv-server/                     保留的初始环境
  session.env                       每次登录source的本项目路径配置
  datasets/frozen-20261126/data/     服务器抓取并冻结后的完整数据
  cache/                            pip/uv缓存和可选uv管理的Python
  exports/                          数据快照包及后台启动日志
  runs/<时间-任务-随机ID>/
    manifest.json                   源码/data散列、启动commit、实际命令
    environment.json                Python、硬件、驱动与环境信息
    requirements.freeze.txt         本次依赖版本
    status.json                     running/succeeded/failed/interrupted
    logs/                           逐步骤stdout与stderr
    work/                           独立代码副本及data副本，全部位于数据盘
    reports/                        本轮报告
    analysis_bundle.tar.gz          下载给Mac分析的小于完整工作目录的结果包
    analysis_bundle.tar.gz.sha256   压缩包校验值
    BUNDLE_MANIFEST.json            结果文件散列
```

数据盘中允许存在各run的代码副本，日常维护的**代码主体始终为 `/srv/ficc/china_debt`**。
每次任务新建run，运行中不改其work/data；不要把数据或结果包提交Git。有任务运行时
不要升级共享venv；需要升级时等待任务结束或另建版本环境。

## 4. 登录服务器并准备数据盘

```bash
# Mac终端：沿用现有Host名称，不修改SSH配置
ssh research-server
```

以下在**服务器终端**执行：

```bash
whoami
hostname
cat /etc/os-release
python3 --version
git --version
mkdir -p /srv/ficc
test -w /srv/ficc
df -h "$HOME" /srv/ficc
mkdir -p /srv/ficc/datasets /srv/ficc/runs /srv/ficc/exports /srv/ficc/cache/tmp
```

若没有写权限，先解决该专属目录的权限；不要静默回退到家目录下载大文件。
第一次部署时创建下列配置文件；**已有文件则先查看，勿覆盖自己的版本**：

```bash
if [ ! -e /srv/ficc/session.env ]; then
  cat > /srv/ficc/session.env <<'ENV'
export FICC_VENV=/srv/ficc/.venv-server-20261126-v2
export FICC_DATA_DIR=/srv/ficc/datasets/frozen-20261126/data
export FICC_RUNS_DIR=/srv/ficc/runs
export PIP_CACHE_DIR=/srv/ficc/cache/pip
export UV_CACHE_DIR=/srv/ficc/cache/uv
export UV_PYTHON_INSTALL_DIR=/srv/ficc/cache/python
export UV_PYTHON_BIN_DIR=/srv/ficc/tools
export TMPDIR=/srv/ficc/cache/tmp
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
ENV
fi
source /srv/ficc/session.env
```

每次新SSH会话或新tmux会话都执行 `source /srv/ficc/session.env`。
`server.sh`读取FICC_VENV、FICC_DATA_DIR和FICC_RUNS_DIR；显式`--runs-dir`可覆盖运行目录。
**FICC_DATA_DIR必须指向完整data子目录，不能设为 `/srv/ficc` 根目录**，否则会把
环境、缓存、runs一起当数据复制，封装也会拒绝runs位于data内的递归布局。

## 5. 首次获取代码／以后更新（服务器终端）

2026-11-26首次部署采用rsync经SSH传输：包含本地有效工作文件与Git历史bundle，排除
Mac环境、data和缓存。新项目origin指向原GitHub HTTPS地址；不改全局Git认证、已有SSH
配置或其他仓库。仓库公开后可直接fetch/pull。上传保留本地未提交状态，不伪装clean；
本次迁移检查显式使用`--allow-dirty`并记录完整文件散列，后续正式发布前需提交并同步这些变更。
以下clone命令只用于尚未建立仓库的情况。

代码放在 `/srv/ficc/china_debt`。使用已有GitHub认证方式，默认HTTPS：

```bash
cd /srv/ficc
git clone https://github.com/INNOCUITY/chinadebt.git china_debt
cd "/srv/ficc/china_debt"
git status --short
git log -1 --oneline
```

仓库现已公开，HTTPS clone/fetch无需GitHub认证；服务器SSH登录是另一条认证链。
本项目不改全局Git凭据、现有SSH密钥或Host配置，也不把token写入URL或脚本。

**若 `/srv/ficc/china_debt` 已有仓库，跳过clone**；若是非空非Git目录，先检查内容，不能直接覆盖。
每轮更新代码：

```bash
cd "/srv/ficc/china_debt"
source /srv/ficc/session.env
git status --short
git switch main
git pull --ff-only
git log -1 --oneline
test -f scripts/server/server.sh
```

出现修改或分叉时先保留并处理，不执行reset --hard清场。正式任务拒绝dirty checkout；
开发自测才用`--allow-dirty`。本指南和脚本必须先从Mac提交推送，服务器pull后才可使用；
未推送文件不会自动同步。本步骤不下载行情，Mac也不需要准备行情压缩包。

## 6. 无conda配置Python环境

实际服务器为Ubuntu 24.04.3、系统Python 3.12.3，但缺少ensurepip且未安装uv。
本机应优先按6.2准备用户级Python，再执行bootstrap；不要直接假定6.1的venv可创建。

### 6.1 系统Python已经≥3.11

```bash
# 服务器：环境在数据盘，代码在数据盘专属目录
cd "/srv/ficc/china_debt"
source /srv/ficc/session.env
export PYTHON_BIN=python3
bash scripts/server/bootstrap.sh
"$FICC_VENV/bin/python" --version
"$FICC_VENV/bin/python" -m pip check
bash scripts/server/server.sh doctor
```

脚本创建 `$FICC_VENV` 指向的独立环境（当前为数据盘的`.venv-server-20261126-v2`）、安装项目及dev依赖、输出 `exports/server-requirements.freeze.txt`；
不安装conda、不改系统Python、不安装GPU驱动。可用 `PYTHON_BIN=python3.11` 明确解释器。
日常用脚本无需 `source activate`；手动调试可 `source "$FICC_VENV/bin/activate"`，结束 `deactivate`。

### 6.2 系统Python太旧，或没有venv，且没有sudo

使用uv安装Python；先source第4节配置，Python安装目录和下载缓存均在数据盘。按[uv安装文档](https://docs.astral.sh/uv/getting-started/installation/)
下载官方安装脚本，阅读后执行：

```bash
mkdir -p /srv/ficc/tools /srv/ficc/cache/tmp
curl -LsSf https://astral.sh/uv/install.sh -o /srv/ficc/tools/uv-install.sh
less /srv/ficc/tools/uv-install.sh
UV_UNMANAGED_INSTALL=/srv/ficc/tools sh /srv/ficc/tools/uv-install.sh
export UV_PYTHON_BIN_DIR=/srv/ficc/tools
/srv/ficc/tools/uv python install 3.11
export PYTHON_BIN="$(/srv/ficc/tools/uv python find 3.11)"
bash scripts/server/bootstrap.sh
```

uv用于提供Python，这个项目仍使用标准venv；不是必须引入另一套包管理逻辑。
若服务器不能联网，先通过机构镜像／管理员提供Python与wheel，见第14节；不要在服务器
复制Mac的解释器或venv。venv设计上不可跨机器直接搬运，见[Python官方说明](https://docs.python.org/3/library/venv.html)。

### 6.3 Torch/CUDA是可选的后续步骤

当前smoke、ML/成本、研究验收全部可用CPU；不安装torch也能做完。未来训练启用时，
570驱动可选择匹配的CUDA12.x wheel，不直接安装默认可能采用CUDA13的最新版。
先核对下方官方版本表，再显式填写可用的精确版本及对应wheel索引；本轮未安装Torch，
不把旧文档候选版本当作实测依赖锁：

```bash
: "${TORCH_VERSION:?先依据官方版本表设置精确版本}"
: "${TORCH_INDEX_URL:?先设置与该版本和驱动兼容的官方wheel索引}"
export TORCH_VERSION TORCH_INDEX_URL
bash scripts/server/install_torch.sh
```

版本来源：[PyTorch官方版本表](https://pytorch.org/get-started/previous-versions/)；驱动兼容
范围见[NVIDIA说明](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)。
首次成功后固定精确版本，不每次更新至latest。无视觉/音频任务，不额外安装torchvision/torchaudio。

获得可用GPU后，可显式做一次很小的运算检查（GPU编号按分配修改）：

```bash
CUDA_VISIBLE_DEVICES=0 "$FICC_VENV/bin/python" -c \
  'import torch; x=torch.ones(8,device="cuda:0"); print(torch.__version__,torch.version.cuda,torch.cuda.get_device_name(0),x.sum().item())'
```

设置可见物理GPU后，进程内编号从0重新开始。不要为本项目自动升级驱动、启用MIG或修改
他人的环境。若使用Slurm/容器，以调度器提供的可见设备为准，不覆盖其分配。

### 6.4 本机已实测环境（2026-11-26）

初始环境位于 `/srv/ficc/.venv-server`，Python 3.11.18、uv 0.13.10、
AKShare 1.20.70，`pip check`通过。首次完整检查发现项目遗漏tabulate依赖，已在pyproject补齐，
并另建`.venv-server-20261126-v2`（保留原版本+tabulate0.10.0）进行重验；session.env现已指向v2（724 passed / 1 skipped / 1 deselected）。不要在任务运行中升级它正在使用的venv。系统Python缺ensurepip，所以采用数据盘内uv管理的解释器。
依赖锁保存在 `/srv/ficc/exports/environment-20261126.freeze.txt`，
v2为同目录`environment-v2-20261126.freeze.txt`；本次状态与run清单见[实跑记录](05_server_execution_20261126.md)。
本机pip访问默认PyPI超时，安装时在本项目session.env中设置
`PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`，没有改全局pip配置。
环境运行任务时不得升级；未来需变化则另建版本环境并重新验收。

## 7. 默认在服务器直接抓取数据

服务器可以联网，Mac存储不足，因此**不在Mac下载/打包完整数据集**。行情、原始xlsx、
资金数据和版本快照都在服务器数据盘。Git仓库不含行情，也没有另一个Kaggle数据集要下载。

### 7.1 安装资金数据接口并在线抓取

```bash
# 服务器，已经完成第4–6节
cd "/srv/ficc/china_debt"
source /srv/ficc/session.env
"$FICC_VENV/bin/python" -m pip install akshare
"$FICC_VENV/bin/python" -m pip check
"$FICC_VENV/bin/python" -m pip freeze --exclude-editable > /srv/ficc/exports/server-requirements.freeze.txt
bash scripts/server/server.sh fetch --data-end 2026-11-05 --ipv4-only
```

第一次FICC_DATA_DIR尚不存在，封装从空数据目录开始抓取，**输出位于新run的work/data**，
不会直接写入FICC_DATA_DIR。正常结束会打印RUN_DIR、STATE=succeeded及DATA_DIR。
可按第9节用tmux执行fetch，避免断开SSH中断下载。

截点继续固定2026-11-05，以便对照已有研究；文档更新日期2026-11-26不代表自动追到当天。
未来更新行情要显式改`--data-end`，另存新快照。官网可能修订历史数据，即使截点相同，
新抓取的SHA也可能不同；首次回传必须说明这是服务器新抓版本，不宣称与Mac逐值一致。

### 7.2 确认成功，再冻结为长期输入

下面把`实际fetch-run-id`替换为上一步打印的run目录名；不要猜“最新目录”或选其他任务。

```bash
FICC_FETCH_RUN="/srv/ficc/runs/实际fetch-run-id"
cat "$FICC_FETCH_RUN/status.json"
"$FICC_VENV/bin/python" -c 'import json,sys; s=json.load(open(sys.argv[1])); assert s["state"] == "succeeded", s' "$FICC_FETCH_RUN/status.json"
```

**只有上面的成功检查通过后**再执行冻结命令：

```bash
bash scripts/server/data.sh pack /srv/ficc/exports/data-frozen-20261126.tar.gz \
  --data-dir "$FICC_FETCH_RUN/work/data"
cd /srv/ficc/exports
sha256sum -c data-frozen-20261126.tar.gz.sha256
cd "/srv/ficc/china_debt"
bash scripts/server/data.sh unpack /srv/ficc/exports/data-frozen-20261126.tar.gz \
  /srv/ficc/datasets/frozen-20261126
bash scripts/server/data.sh verify /srv/ficc/datasets/frozen-20261126
source /srv/ficc/session.env
bash scripts/server/server.sh data-check
```

pack/unpack拒绝覆盖已有包/快照，防止覆盖成功版本。若上次已成功冻结，直接verify/data-check；
要产生新版本则换新快照名，并同步session.env中的FICC_DATA_DIR。以上压缩包、原始下载、
解包和SHA校验全在服务器，**无需把data包下载回Mac**。

### 7.3 抓取失败与离线重建

在线资金接口需要AKShare；不可达时项目可降级使用已有的
`raw_spike/repo_frr_fdr_hist.csv`。首次空目录没有该缓存，不能靠“离线”得到真实资金数据。
如果只抓到曲线，最终data-check会失败：先看fetch/data_verify日志，修复网络/资金来源后
新建run重试，不用常数融资替代正式数据。

失败后原run的work/data会保留。需要利用已下载缓存时可显式：

```bash
bash scripts/server/server.sh fetch --data-dir /srv/ficc/runs/实际失败run-id/work/data --data-end 2026-11-05
```

这会复制缓存到新run再抓取，不覆盖旧失败记录。该命令中的路径必须对应真实失败目录。
已有完整冻结快照后，可在服务器缓存上离线重建：

```bash
source /srv/ficc/session.env
bash scripts/server/server.sh offline
```

offline同样输出新run/work/data；要采用新构建结果也按7.2冻结。不要在唯一数据副本还在
fetch/offline的work内时删除这个run。缺少原始缓存时离线命令无法补全数据。

### 7.4 2026-11-26实际抓取问题与数据验收

- 官方站点默认连接超时而IPv4成功，`fetch --ipv4-only`仅在抓取子进程限制urllib3地址族，
  不改系统DNS/IPv6。它不会改变数据解析；参数写入运行manifest。
- AKShare新接口为`interest_rate_repo_hist_cm`，适配器优先调用它并兼容旧`repo_rate_hist`。
  月度接口实测漏掉2011-01-31，现相邻分片回看一天、重复日先核验全部FR/FDR值再去重，
  冲突直接失败；不请求截止日之后数据。不能用行数接近代替逐日比对。
- 曲线67472行/6204日/11期限；资金5453行，截至2026-11-05。与既有本地研究数据在共同
  截止内逐日数值按小数10位哈希一致。旧资金5454行多的是2026-11-06，未纳入新快照。
- 25个官方xlsx中2026文件原始SHA变化（线上包含新日期），截止内曲线产物SHA完全一致。
  2022-12-31、2023-12-31无FDR007发布值，旧数据也缺；沿用最近已知利率，不伪造观测。
- 冻结快照48文件，tree SHA `191c78bb2b3dfb78d19890965eaba3e0125227fe5dcb62c06b74122f1f493139`。
  `raw_spike`兼容CSV来自新抓取parquet的显式导出，`DERIVED_FIXTURE_PROVENANCE.json`
  标注输入/输出SHA及派生身份；用于旧测试/离线资金输入，不冒称原Phase0原始下载。

### 7.5 data-check检查范围

读取current版本并检查哈希；processed与current数据须一致；2019-01-31初始曲线至
2025-12-31研究区间的所需期限完整；真实FDR007覆盖研究窗口。不自动补2Y或修改异常点。
完整data含raw/raw_spike/processed/metadata/versions，只复制两个parquet不一定可复现。
检查数据来源与后续模型结果必须一起看，不能把在线新版本引起的变化当作代码回归。

## 8. 第一次运行的最短顺序

以下在**服务器仓库根目录**执行，先完成环境安装、服务器抓取并冻结数据、clean checkout：

```bash
cd "/srv/ficc/china_debt"
source /srv/ficc/session.env
bash scripts/server/server.sh doctor
bash scripts/server/server.sh data-check
bash scripts/server/server.sh smoke
```

每条命令立即打印 `RUN_DIR`、`RUNNER_PID`，逐步显示日志路径。真正成功条件为：命令退出0，
`status.json.state=succeeded`，相应业务报告也通过验收。存在文件或GPU有利用率都不代表成功。
smoke中的example应有真实成交/持仓/成本；R74之前的“0成交也通过”不再是验收口径。
doctor的成功仅表示完成盘点；`environment.json`中不可用工具或非零探测结果仍需检查，
不等于CUDA已可用或金融验收通过。实际子进程的线程配置另存于manifest的
`effective_runtime_environment`，与启动shell的环境记录区分。

验证smoke后再运行：

```bash
bash scripts/server/server.sh check
bash scripts/server/server.sh models
bash scripts/server/server.sh acceptance-buyhold
bash scripts/server/server.sh acceptance-rolling
bash scripts/server/server.sh acceptance-extended
```

需要整套研究复现时再 `bash scripts/server/server.sh research`；全部当前流程一键跑：

```bash
bash scripts/server/server.sh full
```

full含较重的拟合和回测，不是日常每次pull后都必须运行。第一次Linux验收建议完整运行；
以后按改动范围选择check/smoke/models等。PPO 使用独立 mode。

## 9. 断开SSH也能运行：tmux优先

若服务器已安装tmux：

```bash
tmux new -s ficc-run
cd "/srv/ficc/china_debt"
source /srv/ficc/session.env
bash scripts/server/server.sh models
```

按 `Ctrl-b` 后按 `d` 脱离，不中断任务。重新连接：

```bash
tmux ls
tmux attach -t ficc-run
```

无tmux可用nohup（日志名每次不同）：

```bash
cd "/srv/ficc/china_debt"
source /srv/ficc/session.env
mkdir -p /srv/ficc/exports
FICC_LAUNCH_LOG="/srv/ficc/exports/launch-$(date +%Y%m%d-%H%M%S).log"
nohup bash scripts/server/server.sh models > "$FICC_LAUNCH_LOG" 2>&1 < /dev/null &
echo "launcher PID=$! log=$FICC_LAUNCH_LOG"
tail -f "$FICC_LAUNCH_LOG"
```

外层日志会打印工作目录和步骤日志；具体细节用 `tail -f <RUN_DIR>/logs/<step>.log`。
`status.json` 每个阶段开始/结束更新，不是每秒进度；日志暂时安静不等于进程挂死。

```bash
# 将下面路径替换成真实 RUN_DIR
FICC_RUN="/srv/ficc/runs/实际run-id"
cat "$FICC_RUN/status.json"
tail -n 80 "$FICC_RUN/logs/ml_baseline.log"
```

取消当前任务：前台按Ctrl-C，或对打印的**RUNNER_PID**执行 `kill -TERM <PID>`。封装停止
自己启动的子进程组、写interrupted状态并尽量打包部分结果；不使用pkill python。
full模式应取消正在运行子任务的RUNNER_PID，不能只杀外层bash让子任务悬空。
kill -9、断电无法保证收尾；这种情况下running状态不代表仍在运行，需核对本人的进程后
保留日志，并新建run重试，不手工将状态改为succeeded。

## 10. 结果自动保存和下载到Mac

run结束，无论成功还是业务步骤失败，都会尝试生成 `analysis_bundle.tar.gz` 与 `.sha256`。
包含：状态/命令/退出码/时间、环境与依赖、完整源码/输入散列、每步日志、报告及example
小模型文件。**不包含data、.git、venv、工作副本源码**。

输入报告 `ml_dataset.csv/ml_oos_predictions.csv` 在某些测试任务中是种子数据，已在
manifest.seed_reports标明；不要把check任务里的种子报告解释成本次重新训练结果。
models任务会真实重建它们。内层报告里的repo_state可能因生成报告而dirty；外层
manifest.source_commit/source_repo_state和源码散列标识本次启动源码，二者用途不同。

```bash
# Mac：替换成服务器打印的真实 run-id
cd "$(git rev-parse --show-toplevel)"
FICC_RUN_ID=实际run-id
ssh research-server "du -h /srv/ficc/runs/$FICC_RUN_ID/analysis_bundle.tar.gz"
df -h .
```

先看结果包大小与Mac剩余空间。下载并解压需要同时容纳压缩包和解压文件；完整数据集
继续留在服务器。空间足够时再执行：

```bash
mkdir -p "results_from_server/$FICC_RUN_ID"
rsync -avP "research-server:/srv/ficc/runs/$FICC_RUN_ID/analysis_bundle.tar.gz" \
  "results_from_server/$FICC_RUN_ID/"
rsync -avP "research-server:/srv/ficc/runs/$FICC_RUN_ID/analysis_bundle.tar.gz.sha256" \
  "results_from_server/$FICC_RUN_ID/"
cd "results_from_server/$FICC_RUN_ID"
shasum -a 256 -c analysis_bundle.tar.gz.sha256
tar -xzf analysis_bundle.tar.gz
cat status.json
```

rsync的 `-P` 保留部分文件，网络中断后重跑同一命令。没有rsync时可用scp下载，仍做SHA校验。
如果本轮full有多个run，各自下载对应包；不要仅下载最后一个research包就遗漏模型/验收结果。

若Mac连完整结果包也放不下，先只取元数据和日志，分析后再按需下载具体汇总报告：

```bash
# Mac；沿用上面设置的FICC_RUN_ID，留在本地项目根目录执行
cd "$(git rev-parse --show-toplevel)"
mkdir -p "results_from_server/$FICC_RUN_ID"
for name in status.json manifest.json environment.json BUNDLE_MANIFEST.json; do
  rsync -avP "research-server:/srv/ficc/runs/$FICC_RUN_ID/$name" "results_from_server/$FICC_RUN_ID/"
done
rsync -avP "research-server:/srv/ficc/runs/$FICC_RUN_ID/logs/" "results_from_server/$FICC_RUN_ID/logs/"
```

这种方式是**部分下载**，交给我时说明未取reports，不能声称已校验完整结果包。原始结果包
和SHA仍保留服务器，避免为省本地空间丢掉可复核证据。

给我分析时提供本地目录的**绝对路径**，例如：

> 请审查 `results_from_server/<run-id>`。
> 本轮目标为首次Linux基线复现；请先看status/manifest及环境，再比较净值、订单、成本和终止义务。

不要仅发收益截图。失败也下载完整失败包，先定位第一个非零步骤。若归档因磁盘满没完成，
直接rsync该run的status、manifest、environment和logs目录；保留报错，不把缺文件当成零成本。
可以在任务已结束且磁盘恢复后重打包：

```bash
# 服务器，只允许终态run
"$FICC_VENV/bin/python" scripts/server/ops.py results "$FICC_RUN"
```

## 11. 每一轮的固定操作清单

1. Mac完成代码与测试、提交推送；服务器无运行中的共享环境升级，pull --ff-only并确认commit。
2. 明确本轮目的：环境验收、代码回归、模型对比、吞吐测试四者不要混写。
3. 指定冻结数据目录，校验data包SHA。依赖变化时更新独立环境并重新冻结版本。
4. 选择一个真实mode，在tmux中运行；记录RUN_DIR。正式实验不用--allow-dirty。
5. 看退出码、status、业务验收报告；异常先诊断，不通过删日志或改阈值凑PASS。
6. 下载结果包，核验SHA，保留上轮结果目录用于对照，交给我分析。
7. 根据分析决定下一步：修代码→新commit新run；改经济配置→新协议/配置指纹；只重跑→
   保留相同输入并新run-id。不能覆盖旧结果后仍沿用旧结论。

运行时网络只在clone远端更新/安装/fetch需要。普通run从本机Git checkout克隆，不联网拉
远端；研究阶段不会自动git pull或刷新数据。禁止把依赖更新、数据更新和策略改变同时做成
一个无法区分原因的对照实验。

## 12. 单卡与八卡的资源使用

### 当前CPU流程

当前模式大部分时间在NumPy/SciPy、定价和Python账本。GPU空闲不是失败。
默认各数学库线程1，避免多进程×多线程过度订阅；需要对照时显式设置并记录：

```bash
OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 NUMEXPR_NUM_THREADS=2 \
  bash scripts/server/server.sh models
```

先1、2、4项独立run测吞吐，不以核心总数直接启动128项。当前full是顺序运行，避免未经
验证的高并发。任务彼此数据/报告隔离，但争用同一CPU、磁盘和venv；新增并发前看RSS与I/O。

### GPU训练接入后

首版仍小型64×64 actor/critic，约20维输入时共约1.1万参数；80GB不是需要用满的目标。
一张卡一个seed/fold任务，8卡逐步增加并发；若CPU推理更快，可CPU采集、GPU批量更新。
选GPU物理编号后进程内使用cuda:0；用 `nvidia-smi topo -m`、`lscpu -e=CPU,CORE,SOCKET,NODE`
查看拓扑后才决定是否taskset/numactl，调度器已有绑定时不另行覆盖。

当前不提供假的 `train.sh`。训练模块交付时必须一次性补齐：

- trainer/collector/checkpoint真正入口及 `--config/--seed/--device/--out-dir/--resume` 契约；
- server.sh新mode、单卡sh与八卡任务清单，所有脚本调用同一训练器；
- 原始动作/log-prob/投影/拒单与五日区间reward对齐；末端费用只扣一次；
- checkpoint包含模型/优化器/随机状态/归一化/步数/数据协议指纹；首版仅更新边界恢复；
- 固定seed连续训练与断点恢复对拍；真实成交率/死区率/风控替代率；
- 共用22个walk-forward split、5个预登记seed、连续OOS账本；2 lot主、1 lot稳健性；
- 日收益时间相关性区间估计，seed不当独立市场样本，2019–2025仍是已研究样本。

详细训练实现与验收顺序见 `docs/01_project_plan.md` §12.41；其中早期40GB是假设方案，
本机资源以本指南80GB截图为准。没有训练器时就先完成环境、数据和金融结果移植。

## 13. 常见故障与处置

- **Permission denied (publickey)**：区分服务器SSH与GitHubSSH，检查用户名/端口/密钥对应。
  不把私钥或token发到聊天；可提供去掉敏感信息的错误文本。
- **git pull拒绝／工作区dirty**：先看git status；别在主checkout直接跑会覆盖reports的旧
  命令。封装使用独立副本，默认不会污染主checkout。
- **SSL/HTTP2错误**：先重试或确认代理/网络；可单次使用 `git -c http.version=HTTP/1.1 pull --ff-only`。
  不全局禁用sslVerify。失败的pull不应继续声称运行的是新版本。
- **Python<3.11 / ensurepip缺失**：用用户级uv提供解释器，或请管理员安装对应venv组件。
- **pip无网络**：使用机构允许的镜像或Linux wheelhouse，不拿Mac二进制包替代。
- **No module named akshare**：CPU离线冻结数据不依赖它；只有在线资金抓取需要。若已有
  真实FDR007且data-check通过，可继续；没有真实资金数据则修复输入。
- **current.json指向Mac绝对路径／缺versions**：检查服务器抓取与冻结目录，恢复完整data快照。不要随手删除pointer绕过校验。
- **数据不新鲜提示**：冻结研究的2026-11-05截点与“今天行情”不同；解释清楚，不能静默追新。
- **未知券或0成交example**：确认已拉取R74之后代码；真实example应成交。下载失败包分析。
- **nvidia-smi正常但torch不可用**：检查当前Python是不是 `"$FICC_VENV/bin/python"`、wheel
  是不是CPU版、CUDA_VISIBLE_DEVICES/调度器分配；不要先重装驱动。
- **长时间无输出**：查看本人进程和对应阶段日志；用elapsed时间与profile对照，不重复启动。
- **退出137/进程被杀**：可能为OOM或调度器限制；看stderr/作业记录、减少并发，保留run。
- **断点恢复不支持**：当前研究脚本失败后新run从头重跑；不要把已有CSV当成完成checkpoint。

## 14. 仅网络临时故障时的备用方案

当前服务器可联网，本节不是正常部署前置。若临时断网，优先用服务器已有冻结数据；
必要时从其他有空间的授权Linux机器传输完整数据快照，沿用data.sh的pack/unpack/verify。
Mac仍不承担完整数据下载。代码可通过Git bundle或机构镜像传输，仍记录commit。
Python/依赖须从**相同Linux架构和Python小版本**的联网机器准备wheelhouse：

```bash
# 在匹配的联网Linux环境，使用已验收的环境冻结文件
python -m pip download --dest wheelhouse -r server-requirements.freeze.txt
# 离线服务器已有对应Python/venv后
"$FICC_VENV/bin/python" -m pip install --no-index --find-links wheelhouse -r server-requirements.freeze.txt
"$FICC_VENV/bin/python" -m pip install --no-deps --no-build-isolation -e .
```

同时准备构建需要的setuptools/wheel；若依赖含源码包，要在匹配Linux环境先构建wheel。
Torch CUDA包体积较大，单独保留下载来源和SHA，不从Mac打包整个venv。
源码传输可在Mac `git bundle create exports/chinadebt.bundle --all`，服务器用
`git clone /路径/chinadebt.bundle "/srv/ficc/china_debt"`；之后首次转回远端需显式设置正确origin。

## 15. 保存、清理和后续维护

优先保留：冻结data压缩包＋SHA、源码commit/归档、每轮analysis_bundle＋SHA、环境冻结
文件、正式checkpoint（未来）、验证结果。不把本次生成时commit改写成后续报告commit。

清理前确认任务结束、结果已下载且校验、输入数据有独立备份。最大的通常是run/work和
逐日明细，可以只清理指定已归档run的work，保留manifest/logs/reports/bundle；**fetch/offline
任务的work/data可能是唯一新数据，先另行打包**。不要对整个项目执行未知来源清理命令。

每轮服务器改动更新记录：日期、硬件/驱动变化、Python/依赖、源码commit、data SHA、
协议/seed/split、命令、run-id、成功或失败、结果下载位置、下一步结论。所有“未运行”项目
明确写未运行，不沿用旧机器或旧提交的PASS。

## 16. 交付与验证范围

2026-11-26已实际完成服务器安装、在线抓取与核验、离线重建、smoke、22折模型及双执行带成本回放。
全量检查及三套验收的最终状态见[本次实跑记录](05_server_execution_20261126.md)。单卡 PPO 操作见第 17 节。

以下是历史验证范围，不能用于替代本次run的状态：

2026-11-18更新：当时固定ssh research-server、~/A代码与/srv/ficc环境/数据/运行目录（现已被项目目录约定覆盖）；
在线抓取改为主流程，冻结也在服务器完成。run.py新增FICC_RUNS_DIR环境变量默认值，
data.sh支持FICC_VENV。本轮make lint及服务器工具9项测试通过，涵盖数据盘独立runs的
失败归档与主代码目录不生成runs。未连接服务器、未实跑在线抓取。

以下为2026-11-12的历史验证记录：

本轮新增本指南、server脚本及传输/失败隔离测试。已在Mac用真实冻结数据跑隔离smoke：
data-check、真实example、跨入口、协议quick完成，生成可校验结果包；原正式reports不变。
单元测试覆盖数据包往返/篡改、非法tar成员、结果包排除原始数据、步骤失败立即停止和
原checkout不变。新增服务器测试与既有local example测试合计9项通过，`make lint`通过。
真实data包打包→新目录解包→校验通过；以导入数据在隔离副本离线重建并再次校验通过。
本轮未重跑完整pytest与三套acceptance；Linux安装、SSH传输、A800运算、research/full
与RL训练不在此次已运行范围。已有金融报告未改写。

## 17. 第 0 折 PPO 工程验收

迁移基线提交为 `0715823`。服务器入口先核验冻结数据树，再在独立工作副本内执行。
使用独立的 Python 3.11 训练环境，保留 v2 全量依赖锁，另安装 torch 2.9.1+cu126。
在训练 checkout 中加载项目路径，选择已分配的一张 GPU，执行以下命令。

```bash
source /srv/ficc/session.env
export FICC_PYTHON=/srv/ficc/.venv-ppo-cu126/bin/python
bash scripts/server/server.sh ppo-profile
# CUDA_VISIBLE_DEVICES 由任务启动器设置为获分配的单卡。
bash scripts/server/server.sh ppo-train
# 从已有的第一轮更新继续，结果写入新的 run。
bash scripts/server/server.sh ppo-resume --checkpoint /absolute/path/to/update-1.pt
```

窗口、执行带、预留 seed 与超参数见 `config/experiments/ppo_smoke.json`。
第 0 折前 20 个交易日仅用于拟合市场特征的均值和标准差，从第 21 日的公共决策网格开始交易。
九个市场特征各配一个缺失指示，四腿面值按券的发行期限汇总。输入另含滞后现金、净值、
基准久期与上一提议。每个更新采一条完整轨迹，执行四次全批次 PPO-Clip，gamma=1。
训练两次更新。在第二次更新后重载第一次的 checkpoint，对拍轨迹、损失、参数、Adam 与随机状态。
本次工程验收只用训练窗。后续验证和测试窗已在配置中登记。

`ppo-profile` 顺序测 CPU 1/2/4 进程的同一 21 日、四动作回放，记录各进程阶段耗时，
比较整条轨迹散列。聚合吞吐含子进程启动时间，单进程内部时钟分别记录采样、推理、更新、
主要产物写盘。GPU 的固定动作回放和实际 PPO 在 `ppo-train` 中顺序执行。
结果见 run 的 `reports/ppo*/`。每次更新独立保存轨迹、指标和原子写入的 checkpoint。
逐日轨迹包含动作归属、真实成交与终止费用。恢复指纹涵盖实际市场输入、观测编码、归一化、
配置、提交及主要运行环境。每个 run 的完整依赖锁在 `requirements.freeze.txt`。
