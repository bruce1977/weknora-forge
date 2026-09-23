# FMQ — Forge Meta Query 语法参考

FMQ (Forge Meta Query) 是 WeKnora Forge 的元数据查询语言，用于 `POST /api/v2/knowledge/search` 的 `metas_query` 字段。

---

## 1. 语法总览

```
expr        := or
or          := and (("OR" | "||") and)*
and         := not (("AND" | "&&") not)*
not         := ("NOT" | "!") not | primary
primary     := "(" expr ")" | comparison
comparison  := field op operand | field "EXISTS" | field "NOT" "EXISTS"
operand     := literal | "(" literal ("," literal)* ")"
```

优先级从低到高：`OR` → `AND` → `NOT` → 比较表达式。

---

## 2. 比较运算符

| 运算符 | 别名 | 说明 | 示例 |
|--------|------|------|------|
| `=` | `==` | 等于 | `level = 3` |
| `!=` | `<>` | 不等于 | `status != 'draft'` |
| `>` | | 大于 | `chapter > 5` |
| `>=` | | 大于等于 | `level >= 3` |
| `<` | | 小于 | `chapter < 10` |
| `<=` | | 小于等于 | `level <= 4` |
| `CONTAINS` | `LIKE` | 子串匹配 / 集合包含 | `category CONTAINS 'db'` |
| `NOT CONTAINS` | | 取反的 CONTAINS | `tags NOT CONTAINS '内部'` |
| `IN` | | 值在列表中 | `level IN (2, 4)` |
| `NOT IN` | | 值不在列表中 | `level NOT IN (1, 2)` |
| `EXISTS` | | 字段存在且非空 | `draft EXISTS` |
| `NOT EXISTS` | | 字段不存在或为空 | `NOT expired EXISTS` |
| `STARTSWITH` | | 前缀匹配 | `title STARTSWITH 'AI'` |
| `ENDSWITH` | | 后缀匹配 | `title ENDSWITH '报告'` |
| `MATCHES` | `~` | 正则匹配（Python re） | `title MATCHES '^第\d+回'` |

> **裸字段名**（后面无运算符）等价于 `EXISTS`：`draft` 等同于 `draft EXISTS`。

---

## 3. 字面量

| 类型 | 语法 | 示例 |
|------|------|------|
| 字符串 | 单引号 `'...'` 或双引号 `"..."` | `'ai'`, `"数据库"` |
| 整数 | 直接写数字 | `3`, `100` |
| 浮点数 | 含小数点 | `3.14`, `0.5` |
| 布尔 | `true` / `false` | `enabled = true` |
| 空值 | `null` | `deleted_at = null` |

> 字符串内的转义用两个单引号：`'it''s'`。

---

## 4. 字段名

### 4.1 自定义元数据字段

直接写 key 名，支持嵌套路径（用 `.` 分隔）：

```
category = 'ai'
author.name = 'bruce'
config.max_retries >= 3
```

### 4.2 内置字段（`$` 前缀）

| 字段 | 类型 | 说明 |
|------|------|------|
| `$id` | string | 记录 ID |
| `$title` | string | 标题 |
| `$kb_id` | string | 知识库 ID |
| `$tag_id` | string | 标签 ID |
| `$type` | string | 记录类型 |
| `$file_type` | string | 文件类型 |
| `$source` | string | 来源 |
| `$parse_status` | string | 解析状态 |
| `$enable_status` | string | 启用状态 |
| `$created_at` | datetime | 创建时间 |
| `$updated_at` | datetime | 更新时间 |
| `$deleted_at` | datetime | 删除时间 |
| `$description` | string | 描述 |

### 4.3 含特殊字符的字段名

用引号包裹：

```
"author name" = 'bruce'
"retry-count" >= 3
```

---

## 5. 逻辑运算符

| 运算符 | 别名 | 说明 |
|--------|------|------|
| `AND` | `&&` | 逻辑与 |
| `OR` | `\|\|` | 逻辑或 |
| `NOT` | `!` | 逻辑非 |

括号 `()` 用于控制优先级：

```
(level >= 3 AND category = 'tech') OR tags CONTAINS 'urgent'
```

---

## 6. 类型自动推断

比较时，FMQ 按以下顺序尝试匹配类型：

1. **数字** — 如果两侧都能转为数字，按数值比较
2. **日期** — 如果一侧是日期字符串（ISO 8601、`YYYY-MM-DD`、`YYYY年MM月DD日` 等），按时间比较
3. **字符串** — 兜底按字符串比较

```
# chapter 存储为数字 5，比较也是数字
chapter = 5          ✓ 数字比较

# created_at 存储为 ISO 日期字符串
created_at >= '2026-01-01'   ✓ 日期比较
```

---

## 7. 运算符行为详解

### CONTAINS（包含）

- **字符串值**：子串匹配 `'abc' CONTAINS 'b'` → true
- **列表值**：列表中是否存在匹配项 `tags CONTAINS 'ai'`
- **字典值**：key 是否存在 `config CONTAINS 'timeout'`

### IN（成员）

值是否在右侧列表中：

```
level IN (1, 2, 3)
category IN ('ai', 'db', 'infra')
```

### EXISTS / NOT EXISTS

检查字段是否存在且非空。以下值视为空：

- `null`
- 空字符串 `''`
- 空列表 `[]`
- 空字典 `{}`

```
draft EXISTS         # draft 字段存在且非空
NOT draft EXISTS     # draft 字段不存在或为空
```

---

## 8. 常用查询模式

### 精确匹配

```
hashcode = 'aaa111'
novel = '风雪江湖'
part = 1 AND chapter = 5
```

### 范围查询（连续区间）

```
# 第3回到第6回
chapter >= 3 AND chapter <= 6

# 2026年的文章
created_at >= '2026-01-01' AND created_at <= '2026-12-31'
```

### 多条件组合

```
part = 1 AND novel = '风雪江湖' AND chapter IN (1, 3, 5)
level >= 3 AND category = 'ai' AND NOT draft EXISTS
```

### 正则匹配

```
# 匹配"第N回"格式
title MATCHES '^第\d+回'

# 匹配邮箱
author MATCHES '^[a-zA-Z0-9]+@[a-zA-Z0-9]+\.[a-z]+$'
```

### 存在性检查

```
deleted_at NOT EXISTS     # 未删除的记录
expired EXISTS            # 有失效时间的记录
```

---

## 9. 与搜索 API 配合

`POST /api/v2/knowledge/search` 的完整请求体：

```json
{
  "kb_ids": ["kb-00000001"],
  "metas_query": "part = 1 AND chapter >= 3 AND chapter <= 6",
  "title": "风雪",
  "tags": ["武侠"],
  "page": 1,
  "page_size": 20,
  "case_insensitive": false,
  "return_content": false
}
```

| 字段 | 说明 |
|------|------|
| `kb_ids` | 限制在哪些知识库内搜索（必填，且需当前 API KEY 有权限访问） |
| `metas_query` | FMQ 表达式（必填） |
| `title` | 标题子串搜索（可选，与 metas_query 是 AND 关系） |
| `tags` | 按标签名过滤（可选，多个标签之间是 OR 关系） |
| `page` / `page_size` | 分页 |
| `case_insensitive` | 是否忽略大小写 |
| `return_content` | `true` 时每条结果额外带 `content` 正文字段（默认 `false`） |

> 响应 `items[].metas` 返回每条命中的**完整** custom_metadata（与查询条件无关）。
> `return_content: true` 时 `items[].content` 优先取 `knowledges.metadata->>'content'`，
> 为空则按 `chunk_index` 拼接 `chunks.content`（`chunk_type='text'`）。

---

## 10. 完整示例

```
# 查找"风雪江湖"第1篇中，第3回到第6回
part = 1 AND novel = '风雪江湖' AND chapter >= 3 AND chapter <= 6

# 查找所有未删除且已发布的技术文档
$deleted_at NOT EXISTS AND $parse_status = 'completed' AND category = 'tech'

# 查找 level >= 4 的 AI 或数据库文章
level >= 4 AND (category = 'ai' OR category = 'db')

# 查找标题以"第"开头且包含数字的章节
title STARTSWITH '第' AND title MATCHES '第\d+回'

# 查找有标签、未删除、且 level 在特定范围内的记录
tags EXISTS AND $deleted_at NOT EXISTS AND level IN (3, 4, 5)
```
