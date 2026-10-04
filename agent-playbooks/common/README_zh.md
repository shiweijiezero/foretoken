# 通用指导

存放所有任务手册共用的原则、Prompt、实验记录规则和证据要求。

使用[实验模板](experiment-template_zh.md)记录一项实验的固定信息，使用[迭代模板](iteration-template_zh.md)记录每一轮迭代。目录结构如下：

```text
results/<experiment-purpose>/
├── experiment.md
└── iterations/<sequence>-<short-name>/
    ├── record.md
    ├── context.json
    ├── changes/
    └── runs/
```

生成的 benchmark 结果、日志、profile 和 W&B 运行放在对应迭代目录中，或从记录中链接到原始位置。`changes/` 按仓库相对路径保存这一轮涉及的修改。
