---
name: opsed-chapters
description: >
  为本地动漫文件自动检测并写入片头（OP）/片尾（ED）章节，使 PotPlayer 进度条显示章节标记并可跳转。
  Use whenever the user mentions adding chapters to anime/video files, OP/ED detection, 片头片尾,
  章节, 跳过片头, PotPlayer chapters, or asks to process a folder of video files —
  even if they just say "给新的一季加章节", "给这个文件夹加章节" or "处理一下这个文件夹".
  Every run names exactly one folder supplied by the user; there is no default library and no
  whole-library mode, and without a path nothing runs. Drives the existing opsed CLI;
  never reimplement detection or container parsing.
---

# opsed-chapters — 动漫视频库 OP/ED 章节写入

所有检测、容器解析、校验逻辑**已经实现并调试通过**，就在下面这个项目里。
你的工作是调用它、核对它的输出、然后按用户要求写回。**不要重新实现任何算法，
不要解析 MP4/Matroska 盒结构，不要调整检测参数**——那样只会烧 token 并引入错误。

## 固定路径

下表除「项目根」外都是相对项目根的路径。**项目根 = 本文件所在仓库的根目录**
（即包含 `opsed/` 包与 `README.md` 的那一层），先确认它再执行任何命令。

| 项 | 路径 |
|---|---|
| 项目根（所有命令在这里执行）| 仓库根目录，用 `pwd` / `git rev-parse --show-toplevel` 确认 |
| Python 解释器（需已装 numpy + Pillow）| `.venv/Scripts/python.exe`（Windows）或 `.venv/bin/python`（Linux/macOS）|
| 运行目标 | **用户每次给出的文件夹路径**；没有默认值，不给路径就不执行 |
| ffmpeg / ffprobe | `tools/ffmpeg/bin/`，缺失时回落到 `PATH` |
| mkvpropedit / mkvmerge | `tools/mkvtoolnix/`，缺失时回落到 `PATH` |
| 检测报告（唯一事实来源，只含最近分析的文件夹）| `work/report.json` |
| 写回记录（回滚依据）| `work/manifest.jsonl` |
| 边界抽帧验证图 | `work/artifacts/*__boundaries.png` |
| 运行日志 | `work/logs/` |

先按项目根拼出绝对路径，再这样调用（必须先 cd 到项目根，`-m opsed.cli` 依赖它）：

```bash
cd "<项目根>" && ./.venv/Scripts/python.exe -m opsed.cli --library "<文件夹>" <子命令>
```

工具缺失时才需要准备（正常不会）：Windows 跑 `pwsh -File scripts/fetch_tools.ps1`；
Linux/macOS 直接用包管理器装 ffmpeg 与 mkvtoolnix。**不要**用 winget/pip 装系统级工具，
也不要把第三方二进制提交进仓库。

## 运行范围：一次一个文件夹（硬规则）

**每次运行都必须由用户给出文件夹路径，只处理那一个文件夹。** 没有默认库根，也不存在
「跑全库」：不给 `--library` 直接报错、什么都不执行；如果给出的文件夹里解析出**多个分组**
（说明指到了收藏夹或上层目录），`analyze` 会拒绝执行并列出分组名。

```bash
... -m opsed.cli --library "<文件夹>" scan         # 只列这个文件夹
... -m opsed.cli --library "<文件夹>" analyze      # 检测并写 report
... -m opsed.cli --library "<文件夹>" apply        # 干跑，不改文件
... -m opsed.cli --library "<文件夹>" apply --write
... -m opsed.cli --library "<文件夹>" verify
```

- `--library` 必须写在子命令**之前**（`-m opsed.cli --library X scan` 对，`... scan --library X` 错）。
- 只有 `clean`（清缓存）不需要路径，它不碰视频。
- **report.json 只保留最近一次分析的那个文件夹**：换文件夹 analyze 会自动重建（路径键是
  相对该文件夹的，跨文件夹不可复用）。要再给上一部番写回，就先重新 analyze 它。
- 文件夹里出现多个分组时，用 `--group <关键词>` 选其中一个，或把 `--library` 指到更里层。
  `--group` 是**子串**匹配，关键词要够独特——`--group 某番名` 可能同时命中 S1 与
  「[发布组] 某番名…第二季」。
- `apply` / `verify` / `rollback` 的路径来自 report 与 manifest（`--library` 只为命令形式
  统一）；`verify` 报 `! missing: <路径>` 表示文件已不在原位置，不是校验失败。
- 缓存不会跨运行残留（`analyze` 默认跑完就删），拆目录/改名也不会留下过期缓存；
  代价是每次 analyze 重新解码，约 2 秒/集。

## 工作流

### 1. 先看现状，不要先算

```bash
... -m opsed.cli --library "<文件夹>" scan    # 分组/集号/时长/是否已有章节（约 2 秒）
... -m opsed.cli --library "<文件夹>" show    # 打印 report.json 里的检测结果
```

`report.json` 是缓存，且只属于最近 analyze 的那个文件夹。检测代码没改、文件夹没变时
**直接用它**，不要重跑 analyze；加了新集才需要 analyze（默认不留特征缓存，会整组重新解码，
约 2 秒/集）。

### 2. 需要重新检测时

```bash
... -m opsed.cli --library "<文件夹>" analyze    # 只算这个文件夹，不动别的
```

每组约 1 分钟是**检测本身**（纯计算，无解码），解码另算约 2 秒/集。默认不保留缓存，
所以每次 analyze 都会重新解码一遍。未覆盖的集会**自动再跑 1–2 轮检测**（不额外解码，
见「已知局限」第 3 条）。
跑完后读 `show` 的输出和 `work/artifacts/*__boundaries.png`（每组一张，
画的是 OP 起点/终点、ED 起点/终点前后 ±3 秒的画面，用于人工确认边界没判错）。

**缓存默认不留。** `analyze` 跑完会删掉本次用到的特征与 PCM，日志打印
`cache: dropped N file(s), freed ...`：report 是唯一事实来源，后面的 `apply` / `verify`
都不读缓存。只有同一批文件要反复调参重跑时才值得加 `--keep-cache`。

- `work/cache/feat/*.npy` 键名来自**相对路径**（如 `S01_01_1080p.mkv.npy`，
  键本身就是你库里的文件名，不要外传）：
  写入章节不会让它失效，改名 / 移动 / 拆目录会（键变了）。
- 历史堆积（换机器、以前用过 `--keep-cache`）用 `clean` 清：`clean` 删 pcm + feat，
  `clean --all` 连 AniSkip 缓存和边界验证图一起删，`clean --dry-run` 只看体积。
- **别删** `work/report.json`（apply 的依据）与 `work/manifest.jsonl`（verify/rollback 的唯一依据）。

**过期缓存只在目录里有历史 `.npy` 时才会咬人**（用过 `--keep-cache`，或换机器带来）：
`compute()` 看到缓存文件就直接复用，**不检查代码是否变过**——曾经因此出现
「同一份视频两次分析结果不同」。判断方法：两次 `analyze` 结果不一致而数据没变，
就是缓存过期，先 `clean` 或加 `--recompute` 重算。

### 3. 写回：永远先干跑

```bash
... -m opsed.cli apply --group <词>                # 干跑，只打印会写什么，不改文件
... -m opsed.cli apply --group <词> --write       # 真正写入
```

- **干跑是默认行为**。不加 `--write` 不会碰任何文件，所以先干跑没有成本。
- `--group` 可重复，按**子串**匹配分组名（中文也行）。子串会误伤同名的番：
  关键词要够独特（`--group 某番名` 这类子串会连带命中它的第二季），
  用发布组名或季度词，或先 `scan` 看准名字。
- 已有章节的文件会被自动跳过（幂等），加 `--force` 才覆盖——但那会**替换掉原有章节**。
  发布组自带章节的文件（例如某集自带 6 个章节）也走这条，按文件夹批量跑时它们
  会静默跳过，这是设计行为，不是漏检。
- 用户可能不想给看过的番加章节——**先问清范围再写**，写回是要改用户文件的。
- 章节方案：`--chapters default`（OP/正片/ED/预告，推荐）、`minimal`（只 OP/ED）、
  `full`（额外在 0 秒加「前情」）。
- `--out-dir <目录>` 可改为输出到新目录、原文件不动。

### 4. 写完必须验证

```bash
... -m opsed.cli verify           # 回读每个已写文件，比对章节与流参数
```

`verify` 输出每行 `ok <文件名> [OP@.., 正片@.., ED@..]`。任何非 ok 行都要查明原因，
**不要**为了凑通过而放宽校验。逐流 MD5 与 chpl 比对已内置在写入流程里，
任一不过该文件会保持原样并记为 FAILED——这是设计行为，不是需要绕过的障碍。
`! missing: <路径>` 是另一回事：那个文件已不在库里（被用户删掉或改名），不是章节校验失败。

**验证止损（重要）**：怀疑边界不对时，允许做的只有两件事——
看 `work/artifacts/*__boundaries.png`（该图就是为此生成的），以及读 report 里同角色
各集的起点/时长分布与 `notes`。**不要自己写帧匹配 / 音频匹配脚本去反推检测内部，
更不要整集解码**：4K HEVC 整片解码是小时级成本（一个「扫 5 集」的探针就能跑十几分钟，
而且产出不了可落地的结论——真问题在代码里，不在帧里）。需要帧级验证时报给用户，
由用户决定是否值得做。

### 5. 撤销

```bash
... -m opsed.cli rollback --group <词>          # 干跑，只列出会撤销哪些
... -m opsed.cli rollback --group <词> --write  # 真正撤销
```

依据 `manifest.jsonl`。MP4 是再做一次无章节的 `-c copy`（并 `-map -0:d` 去掉章节轨，
使流结构回到初始的 2 条流），MKV 是 `mkvpropedit --chapters ''`。
回滚同样会做逐流 MD5 校验，与写入时记录的原始哈希比对——**「回滚无损」是被校验的，
不是假设的**（实测往返三轮后媒体码流与最初原始文件逐字节相同）。

`manifest.jsonl` 是**追加式日志**，同一个文件会出现多次（失败的尝试、成功、回滚）。
程序按「每个路径的最后一条记录」判定当前状态，所以历史记录是正常的，不要手工清理它，
也不要把多出来的旧记录当成异常。`verify` 和 `rollback` 都支持 `--group` 过滤。

## 绝对不要做（每一条都真实踩过坑）

- **不要用 ffprobe 的 chapters 输出来校验 MP4 章节。** ffmpeg 写 MP4 章节时会同时写
  `chpl` 盒和一条 QuickTime 章节轨，ffprobe 优先读后者，而首章不从 0 开始时
  该轨读回的第一章起点会显示成 0——这是假象，`chpl` 里的值才是对的。
  用 `opsed/inspect.py` 的 `read_chpl()`（`apply`/`verify` 已经在用）。
- **不要用裸 `-map_metadata 1` 加章节。** 那会把原文件的全局标签清空。
  必须是 `-map 0 -map_metadata 0 -map_chapters 1 -c copy`（`apply.py` 已封装）。
- **不要把新增的 `data` 流当成校验失败。** MP4 写章节必然多出一条 `bin_data`
  章节轨，`diff_signature()` 已放行它；丢失或改变音视频流才会判 FAILED。
- **不要因为总字节数变化就怀疑丢数据。** 用 `du -sh` 看到的是磁盘分配块数不是文件大小；
  正确做法是按 manifest 里 `before.size`/`after.size` 逐文件求和（实测 +0.013%），
  以及比对 `stream_hashes_before`/`stream_hashes_after`（逐流 MD5）。
  容器内的索引/布局会变，但**媒体码流不会变**——这才是无损的定义。
- **不要假设 `--group` 存在于所有子命令**（它只存在于 analyze/show/apply/verify/rollback；
  不带子命令过滤时报错会在写之前发生，属于安全失败）。传参前可先 `--help` 确认。
- **不要在 `mkvpropedit` 之前做不可恢复的操作假设。** 它在校验之前就已改完文件，
  若之后任何代码抛异常，文件已改但 manifest 没记录（本项目真实发生过）。
  出现这种情况：直接 `probe` 该文件确认章节内容与 `report.json` 一致，
  然后手工补一条 manifest 记录，让 `verify`/`rollback` 重新覆盖它。
- **不要相信 `--limit` 会限制尝试次数以外的东西**——它按尝试数计数，
  全部失败时不会提前退出，这在修复前曾导致一次意外批量执行。
- **不要跳过干跑直接 `--write`。**
- **不要写帧匹配 / 音频匹配探针，也不要整集解码来「验证」边界**（见 §4 的验证止损）：
  那是小时级成本，而且真问题在代码里、不在帧里。
- **不要碰库里的非视频文件。** 若发现 `.exe`、`.mkv1`、`.crdownload` 等一律忽略并提醒用户，
  不要重命名、不要删除、不要当成媒体解析。
- Bash 在 plan mode 下只允许只读命令；真正写文件前需要退出 plan mode。

## 检测前提与已知局限（向用户解释时用）

原理：同一季的 OP/ED **音频逐集相同**，剧情音频逐集不同，所以把一集当时间轴找
「与许多集共享的音频段」。由此推出它**做不了**的事：

1. **每集不同的 ED 检测不到。** 有些季（实测两组：14 集与 10 集）就是这种：
   前者仅约半数集数含 14 秒开场（相关系数 0.72–0.86），后者最强候选只有 1 集成对。
   这类分组应判定为不可检出，不要强行写入。**`notes` 会给出确切原因**：
   开场片段只有 11–14 秒，落在 `Params.seg_min_dur = 15 秒` 之外被丢弃；
   或它的 ED 只有 2 集共享，低于 `member_min = 3`。两者都是结构性限制，不是参数没调好。
   **不要去调 `seg_min_dur`**：其他组的共享短残段（前情、预告，5–12 秒）正是被这个
   下限挡掉的，放宽会把它们当成 OP。若用户坚持要，选项是手动指定固定时间直接生成
   章节文件，或实现「相邻集结尾/预告匹配」的新模式（工作量大，先报价）。
2. **ED 终点是最弱边界，可能偏晚 0–8 秒。** 多个版本把 ED 歌曲延续到片尾预告下面，
   且预告画面在这几秒也逐集相同（实测两集同位置画面差异仅 3/255），
   所以音视频都无法定位 ED *画面* 的精确结束。「预告」标记因此可能略晚。
   OP 起止与 ED 起点精度 ±1–2 秒。用户只要精准时可建议 `--chapters minimal`。
3. **同一季有多个片头/片尾版本时（中途换 OP/ED），`analyze` 会自动适配。** OP/ED 在每集
   里的**位置**不同不是问题（互相关本来就能在任意偏移上找共享段），**版本**不同才是：
   这时整季不存在统一模板。接受门槛是 `max(member_min=3, 30% × (n−1))`，所以长番里
   只覆盖少数集的版本会被整批否决、那些集报成未检出。`analyze` 现在会自动**再分组**：
   对未覆盖的集重跑同一套检测（`Params.adapt`，`adapt_min_episodes=6`，
   `adapt_max_depth=2`），门槛在子单元里退回 3 集下限；某个角色整组都没检出时则把
   单元对半再跑。**全程沿用同一套阈值与精修，不是放宽检出严格度。**

   - **症状**：`show` 里某角色检出率偏低（如 12/25），而 `bench` 显示外部数据更全。
   - **先读 `notes`**：候选被丢弃的三条静默路径现在都记原因（片段不在 15–240 秒窗口内、
     成员数不足、精修拿不到边界）。note 只在对应角色没检全时出现，健康分组不刷屏。
   - **残留就是残留**：子单元再跑仍无结果的集保持未检出，notes 里写明
     `... reported, nothing written`，`apply` 会跳过它们。**不要**为它们猜位置或放宽阈值。
   - **3–5 集的尾巴仍需手动拆目录**：低于 `adapt_min_episodes=6` 工具不会自动再拆，
     但把那几集放进独立子目录后 ≥3 集仍可检出（门槛回到 3 集）。1–2 集在任何情况下
     都检不出（`detect_group` 对 `n < 3` 直接返回），只能报给用户，不写章节。
   - **不要**改 `seg_min_dur` / `member_frac` / `agree_*` 来「救」检出率——那些下限正是
     挡 5–12 秒共享残段用的，放宽会把剧情段落误判成 OP/ED。
   - **`notes` 里的 `op: 2 versions kept separate (13 ep @ 86.06s; 11 ep @ 93.94s)` 是正常信息**：
     这一季中途换了 OP/ED（或两半是不同编辑），已按版本各算各的时长——这是正确行为，不是
     告警。每个版本的长度取「该版本自己那批集」的 25 分位数；若某版本只覆盖极少数集，
     它会保持未检出并在 notes 里标明，不要替它猜位置。
   - `--no-split` 可关掉自动再分组（复现旧行为或做对照时用）。
4. **前情提要**：有些版本的前情提要与 OP 音频连续。检测会把 OP 章节点在
   前情提要之后，所以点「OP」即跳过前情提要；
   想单独标 0 秒用 `--chapters full`。
5. **AniSkip 只用于交叉核对（`bench`），不作为写入依据。** 它对部分剧集自相矛盾
   （同一集有人标 0–130 秒、有人标 41–131 秒），且冷门作品多数季无数据。

## 常见请求 → 命令

| 用户说 | 执行 |
|---|---|
| （任何请求） | **先拿到文件夹路径**：用户没给就问，不要猜、不要退回默认路径 |
| 「给这个文件夹加章节」 | `--library "<路径>" analyze`（若 report 不是它）→ `apply` 干跑 → 确认 → `apply --write` → `verify` |
| 「只处理某一季/某个子目录」 | `--library` 指到那个子目录；里面仍有多组时再加 `--group <关键词>` |
| 「某季中途换了 OP/ED」（一季两版） | 直接 analyze（会自动再分组）；只有 3–5 集的尾巴需要手动拆目录 |
| 「只要 OP 和 ED 两个标记」 | 加 `--chapters minimal` |
| 「撤销这个文件夹的章节」 | `--library "<路径>" rollback --write`（可加 `--group`） |
| 「新的一季/新几集加进来了」 | `--library "<路径>" analyze` → `apply --write` → `verify` |
| 「换个文件夹」 | 换 `--library` 的值；report 会自动重建为该文件夹 |
| 「章节名/数量不满意」 | 换 `--chapters`，需重写时先 `--force` |
| 「检测结果准不准」 | 看 `work/artifacts/*__boundaries.png` 与 report 的 notes；`bench --no-network` 交叉核对 |

## 深入阅读（仅当上面不够时）

- 项目根 `README.md` — 算法原理、参数、模块职责
- `opsed/detect.py` 顶部 docstring — 三遍检测的设计与理由
- `opsed/apply.py` — 校验流程与回滚实现
