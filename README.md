<p align="center">
  <img alt="自由旅舍 FreeKleins —— Server" src="assets/banner.png" width="100%">
</p>

# openKleinsServer

**《环行旅舍》本地假服务端** —— 让这款已停运的 Unity 手游在你的机器上重新跑起来。

本仓库只包含**服务端程序与文档**。它**不含任何游戏素材、美术、音频或配置数据**，
需要你自备一份合法客户端，并用 **[openKleinsPatcher](https://github.com/AkiharaHoshina/openKleinsPatcher)** 打上补丁。

> 《环行旅舍》(Kleins) 的一切权利归原权利人所有。本项目出于**互操作性研究 / 数字保存**目的，
> 与该游戏的开发或运营方无任何关联。详见 [NOTICE.md](NOTICE.md)。

---

## 它是什么

游戏客户端本来要连两个服务器：

| 端口 | 层 | 协议 | 本仓库的实现 |
|---|---|---|---|
| **10300** | HTTP 层 | REST（登录、公告、版本、配置下发） | `fakeserver.py` |
| **10301** | 业务层 | 裸 TCP + Protobuf + XXTEA（`CSPKG`） | `bizserver.py` |

两个都是**纯 Python 标准库**写的，**没有任何第三方依赖**。
玩家存档是一个 `player.json`，单客户端独占（本服刻意锁死了多开会话）。

> ★★ **运行期数据不在本目录里。** 存档、配置表、日志、状态标记全部落在
> **`%USERPROFILE%\Saved Games\freeKleins\`** 下（见下面的目录结构）。
> 所以本仓库可以**整包分发** —— 里面没有任何游戏资产，也不会带上别人的存档。
> 第一次启动时服务端会把旧版本遗留在程序目录里的那些文件**自动搬过去**（幂等，只搬一次）。

---

## 快速开始

> ★★★ **顺序：先给客户端打补丁 → 再启动服务端 → 最后启动游戏。**
>
> **顺序反了会出问题**：服务端先开着、拿**没打过补丁**的客户端去连，客户端可能会提示
> **网络问题** —— 它手里的服务端地址还是官方那个，早就没了。这是**已知问题**。
> 用 openKleinsPatcher 把客户端补丁过之后就不必担心了：地址被改成本机，什么时候开服都行。
>
> 下面「前置」第 3 条说的就是这件事。

### 0. 前置

- Windows
- 一份已停运客户端的合法副本（Untiy 2020.3.47f1 那个中文版）
- 已经用 **openKleinsPatcher** 给客户端打好补丁

### 1. 拿到配置表（**只需做一次，但要先起服务端**）

配置表是游戏**运行时**的数据，服务端自己变不出来 —— 它必须由打过补丁的客户端导出一份。

1. 用 openKleinsPatcher 给客户端打补丁（补丁集里的 `cfg-dump` 模块负责这一步）
2. **先启动服务端**（见下一步），**再启动游戏**。进到登录页/大厅即可。
   配置表会自动写入 `%USERPROFILE%\Saved Games\freeKleins\resources\cfg\<渠道>\`
3. 之后服务端会自动去那里找，**不用你手动拷贝**

> ★★ 第 2 步的顺序不能反：**渠道目录是服务端建的**（客户端是 Lua，创建不了目录）。
> 服务端在收到客户端第一个请求时就知道渠道（HTTP 请求里带着 `subPlatType`），
> 于是替它把 `resources\cfg\<渠道>\` 建出来，并把渠道记进 `state\client_channel.json`。

> 想放到别处也行：设环境变量 `KLEINS_CFG_DIR` 指过去，或整个数据根用 `KLEINS_DATA_ROOT` 换掉。
> 一条命令体检：
> ```
> python cfgdata.py
> ```
> 会打印「程序目录 / 数据目录 / 配置表目录 / 渠道子目录 / 找到多少张表 / 缺哪几张」。

**没配置表会怎样？** 服务端**不会崩**，但关卡列表、抽卡卡池、商店、任务等会全部是空的。
启动时日志和控制台会明确提示。

#### 为什么按渠道分目录？

不同渠道（国服 `bingkolo-Win` / Steam `steam-GL`）的客户端**数据表并不一样**。
以前所有客户端共用一个表目录，先导出的那个会"永久占位" —— 服务端照它的表下发 id，
另一个客户端查不到就崩（实测踩过四次：道具 5133、头像 64/65、卡池 35~40、角色图鉴）。

现在每个渠道自己一份，服务端**优先读当前渠道那份**。客户端还会写一个
`_meta.txt = <渠道>|<版本号>`；哪天渠道或版本对不上，**下次启动会自动全量重导出**。

> 第一次启动会有一阵卡顿（要写一百多 MB）—— 这是预期内的，每个「渠道 + 版本」只付一次。

### 2. 启动服务端

双击 **`启动服务端.bat`**（会自动跳过已经在监听的那个端口）。

它会优先用同目录下的 `bizserver.exe` / `fakeserver.exe`；
没有 exe 就退回 `python bizserver.py` / `python fakeserver.py`（需要 PATH 里有 Python）。

看到这两行就成了：

```
[OK] HTTP 层  127.0.0.1:10300
[OK] 业务层  127.0.0.1:10301
```

### 3. 启动游戏

用**打过补丁的**客户端。客户端里的服务端地址已经被补丁改写成 `127.0.0.1`，
所以不需要改 hosts、不需要管理员权限、不需要自签证书。

关闭服务端：双击 **`停止服务端.bat`**。

---

## 目录结构

```
openKleinsServer/                    ← 只放程序 + 只读配置，可以整包分发
├─ 启动服务端.bat 停止服务端.bat   ← 双击这两个
│
├─ bizserver.py       业务层 TCP 10301（CSPKG 协议）
├─ fakeserver.py      HTTP 层 10300
├─ playerdata.py      存档读写 + 绝大部分业务逻辑（最大的一个文件）
├─ bizproto.py        Protobuf / 变长整数 / XXTEA 编解码
├─ cfgdata.py         配置表读取（带 pickle 缓存）
├─ paths.py           统一的路径解析（★ 程序目录 / 用户数据目录分开，见下）
│
├─ routes.json        HTTP 路由表          ┐
├─ notice.json        公告文本             │ 全部**热读**：
├─ notice_page.html   公告正文 HTML 模板   │ 改完直接生效，
├─ baseline.json      「还原数据」基准档   │ 不用重打包
├─ settings.default.json  设置种子         ┘
│
├─ release/           ★ 打包产物（不进 git）
├─ docs/              完整技术档案（见下）
└─ tools/             构建与自检工具（不参与运行）

%USERPROFILE%\Saved Games\freeKleins\  ← 运行期数据全在这儿
├─ saves\      player.json、player.json.bak_*（存档 + 自动备份）
├─ resources\  cfg\<渠道>\（配置表）、roles_index.json、bizdata.json
├─ state\      local_name.txt、turn_activity.json、settings.json、
│              do_*.flag（跨进程标记）、client_channel.json（当前渠道）
└─ logs\       bizserver.log、probe.log
```

## 环境变量

所有路径都能覆盖，方便把状态隔离到别处：

| 变量 | 默认 | 说明 |
|---|---|---|
| `KLEINS_DATA_ROOT` | `%USERPROFILE%\Saved Games\freeKleins` | ★ 整个数据根目录 |
| `KLEINS_PLAYER_JSON` | `<root>\saves\player.json` | 玩家存档 |
| `KLEINS_CFG_DIR` | `<root>\resources\cfg` | 配置表目录 |
| `KLEINS_LOCAL_NAME_FILE` | `<root>\state\local_name.txt` | 登录页显示的用户名 |
| `KLEINS_TURN_ACT_JSON` | `<root>\state\turn_activity.json` | 回合活动线路 |
| `KLEINS_ROLES_INDEX` | `<root>\resources\roles_index.json` | 角色 id→名字（可选） |
| `KLEINS_LOGOUT_FLAG` 等 | `<root>\state\do_*.flag` | 各种跨进程操作标记 |
| `KLEINS_CFG_CLIENT` | 自动判定 | 强制指定用哪个渠道的配置表目录 |

## 可选：角色名索引

「角色工具」页面要显示角色名。名字来自游戏文案，所以**不随本仓库分发**。
想让它显示名字，配置表就位后跑一次：

```
python tools\make_roles_index.py
```

不跑也不影响启动，那一页会退化成只显示角色 id。

---

## 自己重新打包 exe

需要 Python **3.8.6** + PyInstaller **3.6**：

```
pip install pyinstaller==3.6
powershell -File tools\build_release.ps1
```

产物在 `release\`。脚本会先检查**服务端没在跑**（否则报 `WinError 5 拒绝访问`）。

验证打出来的 exe 里确实有新代码：

```
python tools\_verify_exe.py
```

---

## 文档

`docs/` 是这个项目完整的技术档案，边做边记的：

| 文件 | 内容 |
|---|---|
| `01_客户端画像与二进制补丁.md` | 客户端技术栈、唯一必要的二进制补丁 |
| `02_HTTP层_10300.md` | 响应信封、路由匹配规则 |
| `03_业务层_10301_CSPKG.md` | 帧结构、加密、命令号 |
| `04_服务端实现_存档与配置.md` | 模块分工、存档结构 |
| `05_客户端Lua覆盖清单.md` | 客户端补丁机制与清单 |
| `06_逆向工具链.md` | 自建工具（LuaJIT 字节码反汇编器等） |
| `07_踩坑与结论.md` | 硬结论与踩过的坑 |
| `08_待办与已知限制.md` | **最详细的一份**：已完成 / 待复测 / 已知限制 |

> 档案里的路径还写着开发时的 `D:\KleinsPatch\_re\...`，对照阅读时注意。

---

## 已知限制

- **只支持这一版客户端**。换版本（不同 IL2CPP build / 不同 Lua 字节码）大概率不工作。
- 部分功能是**刻意禁用**的（好友、战术回顾、舍友访募、战斗通行证、交易等），
  点进去会提示「该功能已禁用（本地服务端）」。
- 单客户端独占：同一账号第二个连接会被踢。
- **顺序反了会提示网络问题**：服务端先开着、拿**没打过补丁**的客户端去连，客户端可能提示网络问题
  —— 它手里的地址还是官方那个，早就没了。**先给客户端打补丁、再开服**就没有这个问题。
- 未尽事项见 `docs/08_待办与已知限制.md`。

## 许可

代码 **AGPL-3.0**，见 [LICENSE](LICENSE)。游戏相关内容见 [NOTICE.md](NOTICE.md)。

★ 该许可**只覆盖本仓库的原创源码**，不覆盖《环行旅舍》及其任何资源。
★ AGPL 允许商用，但要求：改动必须以同样许可开源；
  **通过网络提供服务时，也必须向使用者提供源码**。想拿去做私服赚钱，就得把全部改动公开。
