# 任务队列（TASKS）

> 全部工作的唯一队列，包括分给真人搭档的活（owner 区分）。工作台直接渲染下面的 JSON 块。
> 字段：id / title / owner("agent"|"真人id") / status("doing"|"todo"|"waiting"|"done") / priority("P0"-"P2") / accept(验收标准) / tags(如 "需全工具","等外部信号") / note / **at(激活时间，ISO 如 "2026-07-17T15:00"——时间表机制：调度器到点自动执行该任务)** / **kind("work" 默认 | "learn" 学习任务——基于目标的知识获取，工作之外的成长机制，一样进时间表，学到的沉淀进 EXPERIENCE)**

```json
{
  "updated": "",
  "tasks": []
}
```
