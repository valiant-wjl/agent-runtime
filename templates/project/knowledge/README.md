# knowledge/

本目录放项目贴身文档，由项目 owner 手工维护，Claude 按需加载（不主动通读）。

## 推荐文件

- `architecture.md` — 模块划分、调用链、关键设计决策
- `api-contracts.md` — 对外接口契约 / 协议
- `common-pitfalls.md` — 已知坑、误用模式、调试经验
- `recent-incidents.md` — 近期故障回顾
- `<其他项目专属文档>` — 按需添加

## 写入规则

- 由项目 owner 手工维护或通过 `/save` 命令写入（`/save` 由 meta 层定义，见 meta/AGENTS.md）
- Claude 不自动覆写
- 重要决策同步到 EVERGREEN.md "近 6 个月关键变更" 章节
