# 冷链温区账本

用于冷库监管核对「已出具月报为何把某批货计入冷藏或冷冻」的网页应用。
出库事实按 UTC 月一次性冻结；温区认定是一份只追加的知识账本，每次整组更正
生成新的知识版号。补登过去生效的区间只影响新版汇总，任何旧版号仍返回它当初
的统计口径。

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在名为 `coldchain-data` 的卷中（容器内 `/data/coldchain.sqlite3`），
`docker compose restart` 或重新 `up` 之后旧版号与旧事实均可继续追查。
可通过环境变量改端口/密钥：

```bash
COLDCHAIN_PORT=9000 COLDCHAIN_SECRET='...' docker compose up --build -d
```

## 本地运行（不使用容器）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python run.py                       # http://127.0.0.1:8000
# 或
COLDCHAIN_DB=/tmp/cc.sqlite3 gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 run:app
```

## 使用流程

1. **上传月结**：上传单月出库 CSV。列为 `流水号,SKU,出库时刻,箱数`（也接受常见别名，
   如 `出库流水号 / 商品编码 / 出库时间 / 件数`）。时刻为 ISO-8601，可带 `Z` 或偏移量，
   统一换算为 UTC；文件必须同属一个 UTC 月份，流水号全局唯一，箱数为正整数。
   含引号的字段必须闭合：**未闭合的引号整份拒收**（否则其后的流水会被读成备注，
   造成少行却「校验通过」）；合法的跨行引号字段照常接收。
   校验失败时**一行都不会写入**；成功后该月事实永久固定，重复上传返回冲突。
2. **补齐坏月份**：若某月当初因未闭合引号被少读后冻结，可在上传页同时提交
   **当时的坏文件**与**修正版**，为该月生成一份补齐版（普通上传表单之外的另一组
   上传框，或 API 的 `bad_file` + `corrected_file`）。要求：
   - 坏文件确有未闭合的引号，且其 SHA-256 与该月冻结记录一致；
   - 修正版的原始字节只能在坏文件基础上**多出补上的闭引号**；
   - 修正版保留每笔已冻结流水的 SKU、UTC 出库时刻和箱数，并至少找回一笔同月流水；
   - 流水号与普通上传共用全局唯一约束；每月最多成功补齐一次；失败不留下任何新增
     流水或补齐版。
   原冻结事实不动：原链接与原 API 参数仍读取原冻结事实，补齐版须显式选择
   （月报页「事实口径」或 `/api/report?...&supplement=1`）。
3. **登记温区**：按 SKU 登记 `[生效, 失效)` 区间（起含止不含），失效留空表示持续有效。
   同一版号内同一 SKU 的区间不得重叠；整组提交即整组替换，并生成新知识版号。
4. **核对月报**：选择月份和知识版号（留空=最新版），查看各温区箱数、每笔对应流水
   及其依据区间，以及未归类流水。空档期间的出库自动列为未归类。存在补齐版的月份
   可同时选择「原冻结事实」或「补齐版」。

示例文件：`examples/2026-03.csv`（可用 `python examples/make_example.py` 重新生成）。

## 关键规则如何落实

| 要求 | 实现 |
| --- | --- |
| 上传成功后固定该月事实 | `months` 与 `shipments` 在同一 `BEGIN IMMEDIATE` 事务写入；数据库触发器禁止 `UPDATE/DELETE`；同月重复上传返回 409 |
| 未闭合引号整份拒收 | `unclosed_quote_line()` 按 csv 引号规则扫描，定位开启未闭合字段的行；普通上传与 `/api/months` 都在写入前拒绝 |
| 合法跨行引号照常接收 | 扫描器只在「字段起始处的引号」开启引用，`""` 视为转义，闭合后的换行正常分行 |
| 补齐只补不覆盖 | 补齐版写入独立的 `month_supplements` / `supplement_shipments`；`months`/`shipments` 原样保留，旧链接口径不变 |
| 坏文件必须原物 | 坏文件 SHA-256 必须等于 `months.source_sha256`，且确有未闭合引号 |
| 修正版只能多闭引号 | `_insert_only_quotes()` 逐字节比对：除新增的 `"` 外每个字节必须与坏文件相同 |
| 补齐保留已冻结流水 | 按流水号核对 SKU、UTC 时刻、箱数完全一致，并至少找回一笔同月新流水 |
| 流水号全局唯一 | `shipments.sequence_no` 与 `supplement_shipments.sequence_no` 各自唯一；普通上传与补齐在同一写事务内互查对方表 |
| 每月最多补齐一次 | `month_supplements.month` 唯一约束 + 事务内先查后写，失败整体回滚 |
| 补齐版显式选择 | `/report?supplement=1`、`/api/report?supplement=1` 才读补齐视图；省略参数读原冻结事实 |
| 补齐数据不可变 | 补齐两张表均有禁止 `UPDATE/DELETE` 的触发器，失败/重启后仍可分别查询 |
| 区间起含止不含 | 所有时点存为 UTC 微秒整数，判定 `valid_from_us <= t < valid_to_us` |
| 同版同 SKU 不重叠 | 应用层排序校验 + SQLite 触发器 `ranges_no_overlap` 双重保护 |
| 空档出库未归类 | `classify()` 返回空即为未归类，与已归类箱数之和恒等于月总箱数 |
| 每次整组更正生成新版号 | 新版本 = 其他 SKU 区间原样带入 + 目标 SKU 整组替换；版本与区间同事务写入 |
| 补登过去只改新版 | 版本只追加、不修改；查询按选定版号读取该版区间快照 |
| 更正/查询交错不混用新旧 | 写用 `BEGIN IMMEDIATE` 串行化；查询在一个读事务里同时取月份事实、版号与区间 |
| 非法/失败不产生半版 | 校验先于写入；任何异常整体回滚；版本号和区间在同一事务提交 |
| 重启后旧版可追查 | 全部状态在 SQLite（WAL，`synchronous=FULL`），容器卷持久化 |

并发更正采用「基准版号」乐观并发控制：提交时必须声明所依据的版本号，若已不是
最新版则返回 409 要求刷新重试，因此不会把两次不同更正悄悄合并或覆盖。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/months` | 已固定月份列表（含 `supplement_id` 等补齐信息） |
| POST | `/api/months` | multipart 上传 `file`（可选表单字段 `month` 做校验）；补齐改为同时上传 `bad_file` + `corrected_file` |
| GET | `/api/versions` | 知识版号列表 |
| POST | `/api/versions` | `{"sku","intervals":[{"zone","valid_from","valid_to"}],"reason","base_version"}` |
| GET | `/api/versions/<n>` | 某版全部 SKU 区间 |
| GET | `/api/report?month=YYYY-MM&version=<n>&supplement=1` | 月报（省略 `version` 用最新版；省略 `supplement` 读原冻结事实） |
| GET | `/api/audit` | 最近 200 条审计记录 |
| GET | `/healthz` | 健康检查 |

`base_version` 为 `null` 仅允许用于空账本创建 v1；否则必须是当前最新版号。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：月结冻结与重复拒绝、CSV 各类非法输入、未闭合引号整份拒收与合法跨行引号、
补齐版的 SHA 校验/字节校验/已冻结流水核对/每月一次/并发唯一/重启后分别查询、
起含止不含边界、空档未归类、重叠/倒序区间拒绝、补登不改旧版、基准版号冲突、
并发更正串行化、查询快照一致、重启后旧版可追查、数据库层不可变触发器等。

## 目录

```
coldchain/
  app.py        Flask 路由与页面/API
  facts.py      月结 CSV 校验、冻结与补齐
  knowledge.py  温区版本、区间校验、分类与报表
  db.py         SQLite schema、触发器、事务
  timeutil.py   UTC 解析与规范化
  templates/    页面
tests/          单元与并发测试
examples/       示例月结 CSV
Dockerfile, docker-compose.yml
```
