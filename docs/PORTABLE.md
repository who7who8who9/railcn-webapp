# 便携版（U 盘 / 移动盘）

把项目连运行环境一起放到移动盘，插到任意 64 位 Windows 电脑双击就能用。
不需要目标机器装 Python，不改环境变量，不写注册表。

## 目录结构

```
<盘符>:\railcn-portable\
├── 启动 railcn.bat        双击启动
├── run.py                 启动逻辑（路径全部相对自身，不依赖盘符）
├── 使用说明.txt           给使用者看的
├── python\                便携 Python 3.13.12 + requests/openpyxl/bs4
├── app\                   项目本体（约 438 MB）
│   └── webapp\data\rail.db    数据库 160 MB
├── railcn-历史版本.bundle  git 全量历史，单文件 18 MB
└── wheels\                依赖 wheel 备份，重建环境时用
```

为什么 `.git` 不放进去：exFAT 默认 128 KB 簇，一个仓库几千个小对象文件
会占好几倍空间。需要版本管理时 `git clone railcn-历史版本.bundle app-git` 还原。

## 为什么能免安装

- `python\python313._pth` 里只写了 `python313.zip`、`.`、`Lib\site-packages`
  三个**相对**路径，解释器只认自己所在目录，与盘符、用户名、注册表都无关
- `run.py` 用 `os.path.dirname(os.path.abspath(__file__))` 定位自身
- 项目代码本身也全是 `__file__` 推导（`railcore.py`、`server.py`、`cnrail.py`）
- 依赖是 wheel 解压安装的，没有 pip 留下的 `.pth` 绝对路径记录

## 改哪一份（先看这条）

**代码只在 `D:\railcn` 改，移动盘上的 `app\` 永远是从开发机同步过去的副本。**

原因：

- 移动盘的 `app\` 没有 `.git`，改了没法 diff、没法回滚、也对不上提交历史
- 两边各改一份，最后只能整份覆盖，必然丢掉一边的改动
- `rail.db` 是 SQLite 二进制，没有行级合并这回事

数据（`rail.db`）规则相反：**谁在跑服务，就在谁那儿改**，但最后必须把
`rail.db` 拷回 `D:\railcn\webapp\data\`，让正本始终最新。

硬规则：同一段时间里只在一个地方动数据。两边同时改，就只能二选一。

## 从开发机更新代码到移动盘

在开发机（`D:\railcn`）改完、提交后，**双击移动盘根目录的 `更新到U盘.bat`**。
它和 `同步回D盘.bat` 是同一套逻辑，只是方向反过来：

- 只拷「开发机比移动盘新」的文件
- 移动盘上更新的文件会被跳过，并提示你先跑 `同步回D盘.bat` 把改动带回去
- 被覆盖的移动盘文件备份到 `app\.sync-backup\<时间戳>\`

没有 Python 环境时（比如在别人机器上），用 robocopy 也一样：

```bat
robocopy D:\railcn E:\railcn-portable\app /E /XD .git __pycache__ .workbuddy backup exports shots .sync-backup /XF rail.db rail.db-wal rail.db-shm rail.db.bak-* railways.geojson *.pyc /MT:8
```

这条**不碰 `rail.db`**，只更新代码和文档。确实要用开发机的数据覆盖移动盘
（比如开发机上新校对了一批），再去掉 `rail.db` 那三个 `/XF` 跑一次。

注意：

- **不要**加 `/MIR`，否则会把移动盘上独有的文件删掉
- 同步前两边都要退出服务，数据库被占用时文件拷不过去

## 从移动盘同步回开发机

在移动盘根目录双击 **`同步回D盘.bat`**：

1. 逐个比对两边文件的时间（留 5 秒容差，避开 exFAT 写入误差）
2. 只列出「移动盘比开发机新」的文件
3. 开发机更新的文件一律跳过——那边是正本，不能被移动盘的旧版本冲掉
4. 确认后复制，被覆盖的原文件备份到 `D:\railcn\.sync-backup\<时间戳>\`

命令行用法（在 `E:\railcn-portable` 目录下）：

```bat
python\python.exe sync_back.py                 :: 只预览，什么都不改
python\python.exe sync_back.py --apply         :: 同步代码和文档
python\python.exe sync_back.py --apply --db    :: 连 rail.db 一起带回去
python\python.exe sync_back.py --dst X:\railcn :: 开发机不在 D 盘时
```

要注意的：

- **数据库默认不动**，要带回去必须显式加 `--db`，因为它是整份覆盖
- 加 `--db` 时脚本会**先把来源库的 `-wal` 合并进主库**（`PRAGMA wal_checkpoint(TRUNCATE)`）
  再拷：服务是 WAL 模式跑的，最后几分钟的校对可能一个字节都还在 `-wal` 里，
  只拷 `rail.db` 会悄悄丢掉。输出里会写「已合并（回写 N 页）」，显示「库被占用」就先退服务
- 目标库那份在覆盖前也会先合并，备份下来的 `rail.db` 才是完整的
- 两边都改了同一个文件时，按时间判、晚的赢，早的那份会躺在备份目录里
- 内容相同只是时间不同的文件会自动跳过（逐字节比对过，不做无意义覆盖）
- 同步完到 `D:\railcn` 跑一次 `git status`，看清楚差异再提交

## 重建便携 Python 环境（万一把 python 目录弄坏了）

```bat
mkdir E:\railcn-portable\python
curl -L -o py.zip https://mirrors.huaweicloud.com/python/3.13.12/python-3.13.12-embed-amd64.zip
tar -xf py.zip -C E:\railcn-portable\python
```

然后新建 `python\python313._pth`，内容四行：

```
python313.zip
.
Lib\site-packages
import site
```

再把 `wheels\*.whl` 解压进 `python\Lib\site-packages\`（wheel 就是 zip，改名解压即可）。

## 已知限制

- 32 位 Windows 用不了（内置 64 位解释器）
- 同一份数据同时只能一台机器用，SQLite 不支持多点并发写
- `rail.db-wal` / `rail.db-shm` 是临时文件，非正常退出后会残留，
  下次启动自动恢复；报 `database is locked` 时删掉这两个文件即可
- 没有携带 `railways.geojson`（326 MB）和 `rail.db.bak-*`（750 MB），
  前者只在重建车站坐标库时需要，后者是过期备份
- 回归测试 `scripts\test_*.py` 依赖 playwright，没装浏览器内核，需要时单独装
