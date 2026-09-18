# opsed — 给指定文件夹里的动漫写入 OP/ED 章节

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/Pxmy996/opsed/actions/workflows/ci.yml/badge.svg)](https://github.com/Pxmy996/opsed/actions/workflows/ci.yml)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#快速上手)

**opsed** = OP + ED：纯本地检测片头（OP）/片尾（ED），并把章节**无损**写进 MP4 / MKV，
让 PotPlayer 进度条上出现章节标记、一键跳过片头片尾。

为**指定的一个文件夹**自动检测片头（OP）和片尾（ED）位置，并把章节信息**无损**写入 MP4 / MKV，
使 PotPlayer 进度条上出现章节标记、可通过章节跳转快速跳过片头片尾。

每次运行都要给出文件夹路径（`--library`），**没有默认库根、也没有「跑全库」这回事**：
不给路径就不执行；如果给的目录里含多个分组，`analyze` 会拒绝执行并列出分组名。

全程**不重新编码**：MP4 走 `-c copy` 重封装，MKV 用 `mkvpropedit` 原地改文件头。
音视频码流字节不变（写回时会用 MD5 逐流校验，见下）。

纯本地检测，基于「同一季里 OP/ED 的音频逐集相同」这一观察，不需要任何参照音频、
不上传任何文件。唯一的联网功能是可选的 `bench` 交叉核对，`--no-network` 可完全关掉。

---

## 快速上手

```bash
# 1. 依赖：Python 3.10+，以及 ffmpeg / ffprobe / mkvpropedit / mkvmerge
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt         # Linux/macOS

# 外部工具：Windows 用脚本下载到 tools/，Linux/macOS 用包管理器装好即可
pwsh -File scripts/fetch_tools.ps1

# 2. 每次运行都必须指定一个文件夹——没有默认路径，不给就不执行。
#    只处理这一个文件夹；指向含多个分组的目录时 analyze 会拒绝执行。
LIB="D:/Anime/某番"

# 3. 分析这个文件夹（只读，不改任何视频；25 集约 2–3 分钟）
#    跑完会自动删掉本次的特征与解码缓存——report.json 才是后续步骤的依据
.venv/Scripts/python.exe -m opsed.cli --library "$LIB" scan
.venv/Scripts/python.exe -m opsed.cli --library "$LIB" analyze

# 4. 查看结果 / 与外部数据交叉核对
.venv/Scripts/python.exe -m opsed.cli --library "$LIB" show
.venv/Scripts/python.exe -m opsed.cli --library "$LIB" bench --no-network

# 5. 写入章节（先干跑，确认后加 --write）
.venv/Scripts/python.exe -m opsed.cli --library "$LIB" apply               # 干跑
.venv/Scripts/python.exe -m opsed.cli --library "$LIB" apply --write

# 6. 回读验证 / 回滚
.venv/Scripts/python.exe -m opsed.cli --library "$LIB" verify
.venv/Scripts/python.exe -m opsed.cli --library "$LIB" rollback --write

# 7. 清缓存（analyze 跑完已自动删掉本次的，这里清历史堆积；只有 clean 不需要路径）
.venv/Scripts/python.exe -m opsed.cli clean --dry-run      # 只看体积
.venv/Scripts/python.exe -m opsed.cli clean                # 真删：pcm + feat
```

装成包之后也可以直接用 `opsed` 命令（等价于上面的 `python -m opsed.cli`）：

```bash
pip install -e .
opsed --library "$LIB" analyze
```

> **给 AI 助手用**：`.agents/skills/opsed-chapters/SKILL.md` 是一份技能说明，
> 描述了命令流程、安全闸门、已踩过的坑与检测局限。支持 Agent Skills 的助手会自动加载它，
> 不需要重新读代码推导方案；但它必须先向用户要到文件夹路径。人工操作按下文即可。

常用开关：

| 开关 | 作用 |
|---|---|
| `--library <目录>` | **必需**（除 `clean` 外）：要处理的文件夹，一次一个。没有默认值，不给就不执行；指向含多个分组的目录时 `analyze` 拒绝执行并列出分组名。`report.json` 只保留最近分析的那个文件夹，换文件夹会自动重建 |
| `--group <关键词>` | 在一个文件夹内还有多个分组时选定其一（子串匹配，可重复）|
| `--limit N` | 最多处理 N 个文件 |
| `--chapters minimal\|default\|full` | 章节方案，见下 |
| `--force` | 覆盖已有章节的文件 |
| `--out-dir <目录>` | MP4 输出到新目录，原文件不动 |
| `--no-network` | 完全离线，不查 AniSkip |
| `--keep-cache` | `analyze` 保留这批文件的特征与解码缓存（默认跑完即删，只在同一批要反复调参重跑时才值得）|
| `--no-split` | 关闭「对未覆盖的集自动再分组」（默认开启；复现旧行为或做对照时才用）|
| `clean [--dry-run] [--all]` | 清分析缓存；`--all` 连 AniSkip 缓存和边界验证图一起删。不动 `report.json` / `manifest.jsonl` |

章节方案：

- `minimal` — `OP`、`ED` 两个章节
- `default` — `OP`、`正片`、`ED`、`预告`（推荐；「正片」可一键跳过片头，「预告」可一键跳过片尾）
- `full` — 在 0 秒处额外加「前情」标记

---

## 效果

检测完会为每个分组画一张边界抽帧图（`work/artifacts/<分组>__boundaries.png`），
把 OP 起点/终点、ED 起点/终点前后 ±3 秒的画面拼在一起，用来人工确认边界没判错：

<!-- 有截图后取消下面这行注释，并把图片存为 docs/screenshot.png -->
<!-- ![边界抽帧验证图](docs/screenshot.png) -->

写入后 PotPlayer 进度条上会出现 `OP` / `正片` / `ED` / `预告` 章节标记，可直接跳转。
（截图待补：边界图含动画画面，是否随仓库分发由你自己决定。）

---

## 检测原理

同一季里 OP/ED 的**音频逐集相同**，而剧情音频逐集不同。所以把一集当作时间轴，
找出它与其他集共享的音频段落即可定位 OP/ED。纯本地、不需要下载任何参照音频。

1. **抽音频** — `ffmpeg -vn -ac 1 -ar 16000`，只解码音频不解码视频；多进程并行。
2. **特征** — 32 段 log-mel，20 ms 步长；每个频带做 15 秒滑窗 z-score，
   并给局部标准差设下限，避免静音段被放大成噪声。
3. **粗定位** — 参考集与每集做 FFT 互相关，找出共享音频所在的时间平移；
   再用 6 秒归一化滑窗把平移转成参考集时间轴上的具体区间。按区间聚类，
   即得到「重复出现的段落」及其所包含的集号集合。
4. **多轮迭代** — 每轮挑一个**尚未被覆盖**的集作为新参考集重跑。
   这能自动发现片头/片尾**中途更换**的情况（同一季有两首不同的 ED 时，
   用其中一个做模板匹配不到另一个）。一轮下来仍有集数没被覆盖时，`analyze`
   会把这些集（或对半拆分后的单元）再跑一遍同一套检测：接受门槛是「覆盖 30%
   的集数、下限 3 集」，单元变小后少数派版本就能达标。这条再分组路径全程沿用
   同一套阈值与精修，因此不会放宽检出严格度。
5. **精修边界** — 用集间对齐后的共识模板，逐集求出锚点；再计算
   「跨集相干度」随偏移的变化：

   ```
   coherence = (‖Σ_j x_j‖² − Σ_j‖x_j‖²) / ((N−1)·Σ_j‖x_j‖²)
   ```

   它等于所有集对的平均归一化相关系数：共享段落内约等于 1，剧情处约等于 0。
   该统计量是比值，天然免疫音量与频谱尺度差异。边界取相干度降到平台值 80% 处。
6. **时长一致化** — 同一版本的 OP/ED 画面逐集相同，长度是常量。以检测到的起点为准
   （起点锐利可靠），长度改用**该版本自己那批集**的 25 分位数：边界走查只会「越界」
   不会「不足」（序列之后的素材若也逐集相同，会把停止点推后），误差是单侧的。
   版本划分**不按时长接近程度，而按簇的集集合是否相交**：同一首歌从不同参考集重复发现
   时集合重叠，合并为一个版本；集合不相交就是中途换了 OP/ED，各算各的时长。
   实测长番里两版 ED 可差 4 秒、两版 OP 可差 8 秒：按时长接近程度合并会把长的那一版压短，
   所以这里必须按集集合划分。

不做画面场景检测：库里大半是 4K HEVC，整片解码是小时级成本，而音频路径便宜两个数量级。
画面只用于最后的人工抽帧验证（`work/artifacts/*__boundaries.png`）。

---

## 安全设计

写回前每个文件都会经过校验，**任何一项不通过就保留原文件**：

1. 输出写到目标目录下的临时文件，校验通过后才 `os.replace` 原子替换。
2. 逐流 **MD5 比对**（`ffmpeg -map 0:N -c copy -f md5`）：音视频码流字节级一致，
   证明没有重编码、没有丢帧。
3. 流结构比对：编码、分辨率、声道、语言、顺序全部一致。
   ffmpeg 写 MP4 章节时会额外加一条 QuickTime 章节轨（表现为一条 `data` 流），
   这是预期的，允许新增；丢失或改变音视频流则判定失败。
4. 时长比对（容差 1 秒）。
5. **chpl 章节回读**（MP4）：直接解析 Nero `chpl` 盒，与写入值逐条比对。
   注意 ffprobe 会优先读 ffmpeg 同时写出的 QuickTime 章节轨，而当首章不从 0 开始时
   该读回值的第一章起点是错误的假象，所以这里以 `chpl` 为准。
6. 每个文件的结果（含前后哈希、章节、耗时）追加写入 `work/manifest.jsonl`，
   `rollback` 依据它无损移除章节。

`--out-dir` 模式下原文件完全不动，章节版写到镜像目录。

---

## 已知局限

- **ED 终点是精度最弱的一项**。部分作品的 ED 歌曲会在片尾预告下继续播放，
  且预告画面在此处也逐集相同，因此音视频都无法精确定位 ED *画面* 的结束点，
  误差通常 0–8 秒，表现为「预告」章节标记比真实预告起点略晚。
  OP 的起止点与 ED 起点精度在 ±1–2 秒。

- **部分剧集没有可检测的 OP/ED**。本地检测依赖「同季音频逐集相同」这一前提。
  有些季只有约一半集数含一段十几秒的开场（相关系数 0.72–0.86），
  或者某角色只有 1–2 集成对，这些分组不做写入，以免猜错位置。
  报告 `notes` 会写明原因：例如开场片段长 11–14 秒，被 `seg_min_dur = 15 秒` 的窗口
  挡掉；或 ED 只有 2 集共享，低于 3 集下限。两者都是结构性限制——放宽窗口会把
  5–12 秒的共享残段（前情、预告）认成 OP。

- **前情提要**：部分版本的「前情提要」与 OP 音频连续，检测会把 OP 章节点在
  前情提要之后，即章节覆盖循环播放的 OP 本身；若需要单独标记前情提要，
  用 `--chapters full` 补充 0 秒处的标记。

- AniSkip 公开数据只用于交叉核对（`bench`），不作为写入依据。它对部分剧集
  自相矛盾（同一集有人标 0–130 秒、有人标 41–131 秒；同一角色的 ED 时长在不同
  提交里能在 50–95 秒之间摇摆），且冷门作品常常没有数据。

- **一季中途更换 OP/ED（整季两版）会自动适配**。接受门槛是「覆盖 30% 的集数、下限 3 集」，
  所以长番里只覆盖少数集的版本会被整批否决、相关集数报成未检出。现在 `analyze` 会对这些
  未覆盖的集再跑一遍同一套检测（某个角色整组没检出时把单元对半），门槛在子单元里退回
  3 集下限，因此换版通常一次 `analyze` 就能覆盖。仍然检不出的集会写进报告 `notes`
  （注明 `reported, nothing written`），`apply` 自动跳过，不会猜位置。
  **OP 在每集里的位置不同不影响检测**（互相关在任意偏移上找共享段），**版本不同**才会；
  3–5 集的尾巴可手动拆成独立子目录再跑一次，少于 3 集则无法检出。
  换版后每个版本用自己的长度：报告 `notes` 会写明 `N versions kept separate (…)`，
  每条 op/ed 记录还带 `variant` 字段标明它属于哪一版（`via` 字段则标明它是否来自子单元分析）。

- 分组按「文件所在目录」划分，集号由文件名正则解析，支持 `E01`、`第01集`、`[29] [1080p]`
  （含 `[46v2]`）、`01 4K` 等写法；解析不出的文件在 `scan` / `show` 里集号为空。若目录结构
  不是「一剧一目录」需要调整，请改 `opsed/library.py` 的 `parse_episode` 或先用 `scan` 核对。
  移动或拆分目录不影响已写入的章节（章节存在文件里），分析缓存默认不保留，也无须为它做任何处理。

---

## 模块

| 文件 | 职责 |
|---|---|
| `opsed/util.py` | 路径、工具发现、子进程与日志封装 |
| `opsed/library.py` | 扫描、按目录分组、集号解析、ffprobe |
| `opsed/features.py` | 抽音频、log-mel、滑窗归一化、缓存 |
| `opsed/detect.py` | 互相关粗定位、聚类、多轮参考集、相干度精修、时长一致化、未覆盖集自动再分组 |
| `opsed/chapters.py` | FFMETADATA1 与 Matroska XML 章节文件生成 |
| `opsed/inspect.py` | 抽帧、接触表、`chpl` 解析、视频重复度 |
| `opsed/aniskip.py` | AniSkip 交叉核对（只读 GET，本地缓存） |
| `opsed/apply.py` | 写回、逐流 MD5 校验、manifest、回滚 |
| `opsed/cli.py` | 命令行入口 |

产物：`work/report.json`（检测报告）、`work/manifest.jsonl`（写回记录）、
`work/artifacts/*__boundaries.png`（边界抽帧验证图）、`work/logs/`（运行日志）。
分析缓存（`work/cache/`：解码 PCM 约 45 MB/集、特征约 8 MB/集）**默认不保留**——
`analyze` 跑完即删，`clean` 清历史堆积。代价是下次分析同一批文件要重新解码，
约 2 秒/集；检测本身的开销不受影响。

> 这些产物全部在 `.gitignore` 里：`report.json` / `manifest.jsonl` 含你自己的文件名与
> 本地路径，公开仓库时不要提交。

---

## 目录结构

```
opsed/
├── opsed/                    # 全部源码（唯一需要读的目录）
├── config/
│   ├── series.json           # 可选，local，已被 git 忽略
│   └── series.example.json   # 模板：分组名 -> AniList/MAL 标题，供 bench 用
├── scripts/fetch_tools.ps1   # 下载 ffmpeg / MKVToolNix 到 tools/（Windows）
├── tools/                    # 外部二进制放这里，内容不入版本控制
├── work/                     # 运行时产物（报告、manifest、缓存、日志），git 忽略
├── .agents/skills/           # 可选的 AI 助手技能说明
├── LICENSE                   # MIT
└── README.md
```

## 配置 `bench`（可选）

`bench` 要把本地分组对上 MyAnimeList 的条目才能查 AniSkip，对不上就跳过。
复制 `config/series.example.json` 为 `config/series.json` 并按需填写：

```json
{
  "我的番 S01": { "title": "My Anime Season 1", "mal_id": 12345, "season": 1 }
}
```

键匹配分组名（精确命中优先，否则取「键是分组名子串」的第一个），
`title` 会在联网时去 AniList 解析成 MAL id，`mal_id` 填了就跳过解析。

## 外部工具与许可

`ffmpeg` / `ffprobe`（FFmpeg）与 `mkvpropedit` / `mkvmerge`（MKVToolNix）是独立的第三方
程序，**不在本仓库内**，各自遵循自己的许可（LGPL/GPL）。本项目只以子进程方式调用它们。
自行分发副本时请一并保留其许可文件，详见 `tools/README.md`。

## 许可

本项目源码以 [MIT](LICENSE) 许可发布。
