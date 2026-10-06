# 通用指导

[English](README.md) | 简体中文

用[实验模板](experiment-template_zh.md)说明优化目标和比较方法，用[迭代模板](iteration-template_zh.md)记录每轮尝试及其结论。

```text
results/<goal>/<motivation>/
├── notes/experiment.md
└── iterations/<name>/
    ├── notes/iteration.md
    └── runs/<run>/
        ├── generated/
        └── artifacts/
```

`notes/` 由用户或智能体维护。命令、源码快照、时间戳和状态由程序记录在 `generated/` 中；评测配置、指标和性能剖析文件保存在 `artifacts/` 中。说明直接引用这些文件，不重复抄写字段。

每轮只选择能回答当前问题的负载。另行记录查找参考、修改代码、准备环境和分析结果花费的时间，注明估算值，实测耗时直接引用对应记录。
