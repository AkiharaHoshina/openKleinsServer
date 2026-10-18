# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)。
`0.x` 阶段视为「能玩，但不保证接口稳定」。

## [0.9.5] — 2026-10-18

### 修复

- **某些教程/演示关卡卡在结算界面** —— `CSStageFinishReq`(1076) 里如果解析不出
  `Stage`，旧代码**一个字节都不回** ⇒ 客户端永远等回包。现在会回一个
  「0 星、无掉落」的空结算，并在日志里喊出来。
- **马缇长街的三个货币（稀因/格/因）工作不正常** —— 「还原数据」只写
  `actor.diamond/gold/copper`，而顶栏读的是 `items[1/2/3]`，`load()` 末尾的
  `ACTOR_CURRENCY_MIRROR` 又用 items 反向覆盖 actor ⇒ `_options` 里的数值当场失效
  （基准档 items[2]=1000000 / `_options.gold`=10000 ⇒ 顶栏 100 万、日志 10000）。
  现在三币**同时**写 actor 和 items。
- **不吹霜卡池在国服不工作** —— 卡池每批只下发 5 个（客户端 LoopScrollRect 只有
  5~6 个格子，发多了 `ChangePool(下标)` 会崩），但服务端原来按 **id 顺序**切批 ⇒
  不吹霜池(31) 排在第 7 批，界面的「换一批」翻不到。现在先按客户端的
  `recruit_ui_config.sort_id` 排序（同档新池在前），不吹霜池落到**第 1 批**。
- **编队超过 10 人 / 连续打关后编队不继承** —— 客户端队列界面是写死的
  `for i = 1, 10`（`view_queue_normal_ui` proto#8）。服务端以前对 1060/1078
  不设上限，存档里可能塞进 >10 个成员 ⇒ 客户端只渲染前 10 个，两边状态分叉。
  现在 1060/1078/1055 全部按 `MAX_MEMBER`(10) 截断，越界槽位直接忽略并写日志。

### 变更

- **基准档三币统一 1,000,000** —— `baseline.json` 的 `_options` /
  `save.items[1..3]` / `save.actor` 与 `playerdata.CURRENCY_SEED` **四处一致**。
  ★ 只改一两处就会出现"日志和顶栏对不上"。
- `_syncfromrepo.py` 把 `baseline.json` 也纳入同步校验（它是运行期读取的文件，
  以前改完仓库版、工作副本还是旧档）。

- **限定卡池保底完全没生效** —— 两个错叠在一起：
  1. `draw()` 用 `pool_type == 2` 判断限定池，但配置里限定池（不吹霜 31）是
     **pool_type = 4** ⇒ 它连 UP 保底都没有，只能靠 2% 再除以池内角色数硬撞。
  2. `up_pity` 拿到**任意** SSR 就清零 ⇒ 70 抽的软保底先出 SSR 就把计数清了，
     **140 的硬保底永远到不了**（几百抽不出限定就是这么来的）。
  现在：`up_pity` 只在拿到**当期 UP** 时清零（140 硬保底），
  `ssr_pity` 在拿到任意 SSR 时清零（70 软保底），自然出 SSR 也是 50% UP / 50% 常驻。

- **战斗里的编队和外面选的编队完全不是一个东西** —— 反汇编
  `logic_chapter.StageStartResBack`(proto#10) 最后一句是
  `scene_mgr.GoBattle(..., StartData.BattleUID, StartData.RoleList)`
  ⇒ **战斗队列就是 `StartData.RoleList`**。服务端以前把 `d["roles"]`
  （全部已拥有角色）全发过去，根本没看请求里的 `GroupIdx` ⇒ 战斗里出现的是
  所有角色（用户截图里 15 个），而不是编队里的那 10 个。
  现在按 `CSStageStartReq.GroupIdx`（0-based）只发该编队的成员、保持槽位顺序；
  编队为空时退回第一个非空编队，全空才退回全部角色（兜底不崩）。

- **教程关的锁定教学角色是空的** —— 教程战斗的队列同样来自 `StartData.RoleList`，
  但教学角色（本渠道是 1030 / 1040 / 1045 / 1046）玩家**可能还没拥有**，
  编队里也没有他们 ⇒ 客户端锁定的教学角色没有数据、教程推不下去。
  现在从 `dataconfig_guide_config` 推导：
  - `gtype=15 (g_enter_battle_scene)` 的 `params_int` = **教程关卡 id**
  - `gtype=35 (g_battle_lock_roles)` / `gtype=30 (g_put_on_role)` = **锁定角色**
  教程关的 `StartData.RoleList` 会补上这些角色的数据（没拥有就用最小可用数据合成）。
  ★ 推导用的是**当前渠道自己的表**，STEAM 版同样适用；表读不到时集合为空、
    行为退回到改动前（不会把 Steam 弄坏）。
  ★ 客户端侧把 `dataconfig_guide_config` 加进了 PROACTIVE 清单，首次运行就会 dump。

### 自检

- 新增 `_selftest_baseline_currency.py`（16 项）：三处货币一致 / 重置后 items 与
  actor 都是 1,000,000 / 再 load 一次仍一致。
- 新增 `_selftest_stagequeue.py`（10 项）：RoleList 只含指定编队 /
  教程关自动补锁定角色 / 普通关不塞教程角色。
- 新增 `_selftest_pity.py`（11 项）：pool_type=4 识别为限定 / 第 140 抽必出 UP /
  常驻 SSR 不清 up_pity / 140 连抽必见 UP / 常驻池 70 保底。

### 客户端改动（需要重新打补丁 / 覆盖 Win\\lua）

- `inject_recruit.py`：限定池的 `paint_type`（`cardpool_restrict_*`）与原版
  `ChangePool` 不兼容 —— 不吹霜池一选中就 `attempt to index a nil value`。
  已在查表层把这类池的面板换成通用面板（角色/名称/时间仍用本池的）。

## [0.9.4] — 2026-10-18

### 修复

- **改名失败并弹出「今日已签到」** —— 两个错叠在一起：
  1. **长度算法不对**：客户端 `logic_create_role.characters`（proto#7）按
     `min(UTF-8 字节数, 2)` 计宽（ASCII=1、汉字=2），服务端却用 Python `len()`
     （汉字=1）⇒ 三个汉字的名字「大星奕」（客户端眼里宽度 6，合法）被判成长度 3。
     现在 `playerdata._name_width()` 与客户端算法一致，上限按文案放开到 14。
  2. **错误码撞了文案**：客户端的 `ret_code` **就是语言 id**（`Utils.GetText(ret)`）。
     `RET_NAME_INVALID` 原来是 122 —— 而 122 的文案是「今日已签到」。
     现在改成 203（请输入名字）/ 206（请保证名字长度为4到14个字符）。

### 自检

- 新增 `_selftest_rename.py`（15 项）：宽度算法 / 三个汉字改名成功 /
  空名字与过短的错误码 / 没道具回 54 / 错误码与配置表文案一一对得上。

## [0.9.3] — 2026-10-17

### 修复

- **「角色工具」页在全新数据目录下仍然全空** —— 0.9.2 加了自动生成，但生成发生在
  **渠道确定之前**：服务端先起、客户端后连，启动那会儿 `state\client_channel.json`
  还不存在、配置表一张都读不到 ⇒ 索引生成失败，之后不再重试。
  现在 `fakeserver` 打开这一页前会先按最新状态重设渠道目录；`bizserver` 在
  **渠道切换成功时**补生成一次；生成失败也会把「渠道/目录」写进日志。
- **月卡买了不生效（一直显示「未订购」）** —— 根因是 `playerdata` 的配置表索引缓存
  `_item_by_id()` 在"配置表还没 dump"时被建成了**空表**并永久缓存 ⇒ 月卡礼包
  4029 查不到 `func=8` ⇒ 没有被当场开掉，天数靠 `load()` 兜底才加，且**不推 1246**，
  客户端 `GetSignInfo()` 永远是登录时的旧值。
  - 空表不再缓存；新增 `playerdata.reset_cfg_caches()`，渠道切换时清空**全部**
    由配置表派生的缓存（道具/卡池/掉落/任务/邮件/关卡… 共 19 个）。
  - `load()` 兜底折算天数后会要求业务服补推 `1246`（`consume_pending_sign_push`）。
  - **第二张月卡也真的加 30 天**（以前只在"没有天数"时折算，第二张被静默吞掉）。

### 自检

- 新增 `_selftest_mc.py`（12 项）：空配置表不缓存 / 切渠道后索引恢复 /
  礼包当场开掉 / 连买两张 +60 / 兜底折算会补推 1246。
- `_selftest_honor.py` / `_selftest_sweep.py`：`dataconfig_honor`、
  `dataconfig_module_unlock_config` 已不在主动 dump 清单里，没 dump 时**跳过**
  对照而不是报失败。

## [0.9.2] — 2026-10-17

### 修复

- **角色名索引改为自动生成** —— 以前必须手动跑 `python tools\make_roles_index.py`；
  不生成时「角色工具」页**整个是空的**（不是老注释说的"退化成只显示角色 id"）。
  现在 `bizserver` 启动时生成，`fakeserver` 打开那一页发现缺文件也会现场补。
- **活动切换页补回两条线** —— `turn_activity.json` 不存在时用的兜底清单只有
  卡门 / 睡眠两条，**情绪线（20001）和初霜（80001）直接不显示**。已补齐；
  初霜渲染成「不可切换」（它是章节线，设成当前回合活动会让主页活动入口整个消失）。
- **`cfgdata.cfg_report()` 改成按渠道计数** —— 渠道化之后它还在只数平面目录，
  明明有 67 张表却报「找到 0 张 / ！！没找到配置表」。
- `_selftest_switch.py` 不再假定 `turn_activity.json` 必须存在（清空数据目录后自检会崩）。

### 新增

- **`tools\kleinstools.py`** —— 打包成 `kleinstools.exe` 随 release 分发（**不需要 Python**）：
  `roles` 重建角色名索引 / `cfg` 配置表体检 / `save` 存档摘要。
- release 包现在带 `tools\`（全部工具源码 + 上面这个 exe + 三个 spec）。

### 变更 —— 运行期数据搬出程序目录（**破坏性**）

- 存档、配置表、日志、状态标记全部改到
  **`%USERPROFILE%\Saved Games\freeKleins\`**（`saves\ resources\ state\ logs\`）。
  程序目录现在只剩「程序 + 只读的作者配置」，**可以整包分发**。
- 新增 `KLEINS_DATA_ROOT` 环境变量（换掉整个数据根）；其余 `KLEINS_*` 的默认值
  跟着改成数据目录下的对应文件。
- `paths.py` 新增 `data_root()` / `data_dir()` / `data_path()` / `migrate_legacy()`。
  **首次启动会自动把旧版本遗留在程序目录里的文件搬过去**（幂等，只搬一次；
  会识别 Windows junction，不会把链接本体搬走）。
- `bizdata.json`（正式服回包抓取）与 `turn_activity.json` 归为运行期数据，不再入库。

### 新增 —— 按渠道隔离的配置表

- 客户端补丁把配置表导出到 `resources\cfg\<渠道>\`，服务端**优先读当前渠道那份**。
  以前所有渠道共用一个目录，先导出的会永久占位 ⇒ 服务端照它的表下发 id，
  另一个渠道的客户端查不到就崩。
- **动态渠道识别**：HTTP 层从请求里提取 `subPlatType` 写入 `state\client_channel.json`，
  业务服在每个请求开头据此切换配置表目录（无需重启）。
- **新鲜度标记**：客户端写 `_meta.txt = <渠道>|<版本号>`；对不上就**全量重导出**
  （先清旧表，不会出现内容翻倍）。
- `cfgdata.py` 的输出多了一行「渠道来源」，启动横幅能看出当前用的是哪个渠道、依据是什么。

## [0.9.1] — 2026-10-16

首个整理出版的版本。从开发用的工作目录里把**属于本项目的资产**提取出来独立成库。

### 新增

- 独立的服务端仓库，去掉了所有硬编码的绝对路径 —— 现在整目录可以搬到任何位置
  （新增 `paths.py`，所有运行期文件都相对「exe/脚本自身所在目录」，且全部可用环境变量覆盖）。
- `cfgdata.py` 支持**配置表目录自动发现**：`KLEINS_CFG_DIR` → 本地 `cfg\` →
  上一级的 `_kleinscfg\cfg\` → 上两级。全部找不到时**不崩**，但会在日志和控制台
  打印明确指引。
- `python cfgdata.py` —— 配置表体检，打印找到多少张表、缺哪几张。
- `tools\make_roles_index.py` —— 用你自己的配置表生成角色名索引。
- `tools\build_release.ps1` —— 一键重打包，带「服务端是否在跑」的前置检查。
- 两份 .bat 现在同时支持 exe 与源码两种跑法。

### 变更

- `settings.json`（运行期状态）不再入库，改带一份种子 `settings.default.json`。
- `roles_index.json` 不再入库（含游戏文案），改由工具从配置表现场生成。
- 文档里过时的路径与数量做了标注。

### 已知限制

- 仅支持这一版客户端（Unity 2020.3.47f1 / IL2CPP / LuaJIT 2.1.0-beta3）。
- 部分界面刻意禁用（见 `docs/08_待办与已知限制.md`）。
