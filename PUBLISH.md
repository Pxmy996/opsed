# 发布清单（给仓库所有者看，开源后可删）

这个目录是**已清干净、可以直接开源**的版本。发布前只剩两处你要自己填：

1. **`LICENSE`** — 把 `<YOUR NAME>` 换成 `Pxmy996`（或你的真名）。
2. 想改许可证的话（当前是 MIT），同时改 `LICENSE`、`pyproject.toml` 的 `license`
   和 `README.md` 末尾的「许可」一节。

`pyproject.toml` 里的仓库地址已经填成 `https://github.com/Pxmy996/opsed`，
如果你打算用别的仓库名，记得一并改掉 `Homepage` / `Repository` / `Issues`。

然后：

```bash
cd opsed
git init -b main
git add .
git status          # 确认没有 tools/ work/ .venv/ config/series.json 被带进来
git commit -m "Initial public release"
git remote add origin git@github.com:Pxmy996/opsed.git
git push -u origin main
```

## 名字

项目名 **opsed** = OP + ED，读作 "op-sed"，正好是这个工具干的事：把片头片尾标成章节。
`chapterskip` 这个名字已经被一个 mpv 脚本占用
（<https://mynixos.com/nixpkgs/package/mpvScripts.chapterskip>），所以没有采用。

包名与命令名一致：

| 用途 | 名字 |
|---|---|
| GitHub 仓库 / PyPI 包名 | `opsed` |
| 导入包 | `import opsed` |
| 模块入口 | `python -m opsed.cli` |
| 安装后的命令 | `opsed` |
| AI 技能名 | `opsed-chapters` |

## 这个版本相较工作副本做了什么

**移除（无用产物 / 隐私）**

| 移除项 | 原因 |
|---|---|
| `.venv/`（78 MB） | 本机虚拟环境，任何人都该自己建 |
| `tools/ffmpeg/`、`tools/mkvtoolnix/`（526 MB） | 第三方二进制，各有自己的许可；改为 `scripts/fetch_tools.ps1` 下载，见 `tools/README.md` |
| `work/report.json`、`work/manifest.jsonl` | 含**你自己的文件名、目录结构与绝对路径** |
| `work/logs/`、`work/tmp/`、`work/cache/` | 运行日志与缓存，且日志里含本地路径 |
| `work/artifacts/*.png`、`frames/` | 边界抽帧图是版权动画画面（约 12 MB） |
| `opsed/__pycache__/` | 编译产物 |

**改写（去掉能反推出你库的信息）**

| 位置 | 改动 |
|---|---|
| `opsed/cli.py` | 报错示例里的 `C:\Users\<你>\Videos\某番` → `/path/to/某番` |
| `.agents/skills/opsed-chapters/SKILL.md` | 项目根绝对路径、`C:\Users\<你>\Videos\...` 调用示例 → 通用写法；具体番名/发布组名 → 「某番名」「某组」 |
| `README.md` | 同上；实测数据只保留数值（时长差异等），去掉作品名与发布组名 |
| `config/series.json` | 你原来那份 8 个分组的追番映射表 → `{}`；另附 `config/series.example.json` 模板，且该文件已加进 `.gitignore` |

**新增（开源门面）**

`.gitignore`、`.gitattributes`、`LICENSE`(MIT)、`requirements.txt`、`pyproject.toml`、
`tools/README.md`、`scripts/fetch_tools.ps1`、`config/series.example.json`、
`.github/workflows/ci.yml`（仅做「能编译、CLI 能加载」的冒烟检查，不需要媒体文件）。

## 发布前自查

```bash
# 敏感串扫描（应无输出；把 <你的用户名> 和 <番名> 换成真实值再跑）
grep -rInE '<你的用户名>|[A-Za-z]:\\\\Users|<番名>|<发布组名>' .

# 大文件扫描（应无 > 1 MB 的文件）
find . -type f -size +1M -not -path './.git/*'

# 乱码扫描（应无 U+FFFD）
grep -rInP '\xef\xbf\xbd' .
```

源码总量约 160 KB（不含 `.git`）。
