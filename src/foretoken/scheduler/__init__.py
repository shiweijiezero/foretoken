"""D2/D4(P2)价值 / goodput 感知调度 —— 准入 / 排序 / 复用 vs 重算闸门。

接入(零 fork):自定义 scheduler_cls(qualname,docs/02 / 08:config/scheduler.py)。
goodput 控制环路把 KV 命中 <-> 投机长度 <-> 调度耦合(docs/11,可守空位)。

TODO(P2):继承 vLLM Scheduler 接口(sched/interface.py),加价值排序 + 复用 / 重算决策。
"""
