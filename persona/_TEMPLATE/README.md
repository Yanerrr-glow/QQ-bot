# 新建一个人格包

这个目录是**脚手架**，`_` 前缀让它不被当成可用人格（`packs.list_packs()` 会跳过
`_` 与 `.` 开头的目录），所以你可以放心改它、也可以把它留在仓库里。

## 最快的做法

```powershell
# 1. 复制成新包（id 只用 ASCII 字母/数字/下划线/连字符）
Copy-Item -Recurse 'persona\_TEMPLATE' 'persona\packs\mybot'

# 2. 填三个地方
#    persona\packs\mybot\_pack.json   ← id / name / bot_name / wake_words
#    persona\packs\mybot\base.txt     ← 【它是谁】那一行，角色名与 bot_name 逐字一致
#    persona\packs\mybot\forbidden.txt / surface.txt

# 3. 检查能不能用（不通过就别切）
python '验证\_人格包检查.py' --pack mybot

# 4. 切过去（立即生效，不用重启）
#    群里：/人设 切换 mybot     控制台：「人格」页 → 人格包 → 切换
```

## 一个包由什么组成

| 文件 | 必需 | 作用 | 谁能写 |
|---|---|---|---|
| `_pack.json` | 是 | 身份元数据：显示名、别名、角色名、唤醒词、对主人的称谓 | 人工 |
| `base.txt` | 是 | **底层人设**（它是谁）。最硬的一层 | 人工，代码里**没有**写入口 |
| `forbidden.txt` | 是 | **禁止事项**（铁律）。自动迭代碰它会被丢弃 | 人工，代码里**没有**写入口 |
| `surface.txt` | 是 | **表层模板**（怎么说话）。只作首次播种 | 人工提供种子 |
| `traits.json` | 否 | 特质 / 通道 / 闸门关键词。缺了闸门回退内置默认表 | 人工 |
| `assets/` | 否 | 随包资源（角色卡、头像等）。运行时不读 | 人工 |

## 三条容易踩的坑

1. **角色名必须和 `_pack.json` 的 `bot_name` 逐字一致。**
   `chatlog._speaker()` 用「角色名 + QQ 号」判定聊天记录里哪句是机器人自己说的。
   对不上时它认不出自己刚说过的话，会对着自己上一轮接话，**而且不报任何错**。

2. **不要在正文里写全路径**（`persona/packs/xxx/base.txt`）。
   换包或搬迁之后它就指错了。要引用同包的文件就只写文件名（`forbidden.txt`）。

3. **不要在人格正文里写机制说明**（"你会把聊天记录分两档读"这类）。
   机制由代码按时机注入，写进正文会在换角色时丢失，也会和代码版本冲突。

## 内容怎么写：见规范

这个脚手架只给**形状**。每一份文件该写什么、不该写什么、条目要什么格式（哪些违反了会
**静默失效**）、改名时哪三处要一起改 —— 见
[`docs/人格包内容规范.md`](../../docs/人格包内容规范.md)。

那份规范里带「判据」的条目都由 `验证/_人格包检查.py` 检查；照本规范逐条写好的完整样本见
[`persona/packs/example/`](../packs/example/)。

## 运行数据落在哪

不是这里，是 `data/runtime/persona/<id>/`：

```
surface.txt      当前表层（自动迭代唯一会写的文件）
changelog.json   变更审计（可撤回）
candidates.json  待采纳候选池
signals.json     人设信号账本
eval.json        评估台结果
```

**按包隔离**：切到别的人格，这个包学到的东西原样保留；切回来接着用，
不会被覆盖，也不会串到别的角色身上。
