# 专题问答的补测与分档分析

沿用[仓库根目录的复现环境](../README.md#复现)，使用同一 6G-Testbed 提交和 aioquic 1.2.0。下例中的 `PHASE` 可指向已有第一版持续流的原始目录。没有缓存时，先用原 profile 复测三个驻留时间。所有试验顺序执行。

```bash
LOSS="$HOME/quic-qa-loss"
PHASE="$HOME/quic-qa-phase"
OUT="$HOME/quic-qa-tables"
sudo "$PY" run.py --testbed "$TESTBED" --out "$LOSS" --workload bulk \
  --repeats 3 --suite qa/loss_profiles.yaml --endpoint qa/loss_probe.py
sudo "$PY" run.py --testbed "$TESTBED" --out "$PHASE" --workload bulk \
  --repeats 3 --only rate10ms rate20ms rate100ms
sudo "$PY" qa/analyze.py --loss "$LOSS" --phase "$PHASE" --out "$OUT"
```

补测有 8 个配置，每格 3 轮，每轮计量 5.8 秒。诊断端点记录原生判丢调用和窗口前后值。各组都用 CUBIC，仅改变判丢参数。qlog 用来核对判丢总数，并在接收端按 1-RTT 包号查找原包。正态抖动和随机丢包由内核抽样，复测中的包号及精确数值会变化。[运行环境与源文件哈希](../results/qa/loss-environment.json) · [补测参数](loss_profiles.yaml)

| 文件 | 数据口径 |
|---|---|
| [loss-events.csv](../results/qa/loss-events.csv) | 每条记录对应一个被判丢的 1-RTT 包。`number`、`time`、`both` 对应当时满足的规则。`rescheduled` 表示来自常规双阈值扫描之外的恢复调用。收端到达时间为空表示接收 qlog 中没找到同号原包。一次调用可以判丢多个包，窗口前后值在这些包上重复。时间相对工作负载起点。 |
| [loss-runs.csv](../results/qa/loss-runs.csv) / [loss-summary.csv](../results/qa/loss-summary.csv) | 每轮与每格中位数。`received_time` 等只统计判丢但原包实到的包。`cwnd_cuts` 按实际使窗口减小的调用次数统计。 |
| [phase-runs.csv](../results/qa/phase-runs.csv) / [phase-summary.csv](../results/qa/phase-summary.csv) | 把连续两次实际切换完成之间定义为一档。对每轮各档分别累计抓包 IP 字节和按序交付应用字节，再除以实际驻留时间。发端、收端、应用三列各自按事件时刻归档。数据可以跨档排队或等待补齐，因此三列不能视为同一批包的利用率。报告使用收端 IP 速率定位档位的交付差异，整体应用吞吐沿用同一切换窗口的[第一版汇总](../results/transport_summary.csv)。 |
