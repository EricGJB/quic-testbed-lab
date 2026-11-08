# 第三版复现

需要 Linux/WSL2、Python 3.12、sudo、iproute2、ethtool、OpenSSL 开发库、CMake、C 编译器，以及根目录的 Python 依赖。JPEG 准备还需 Pillow，制图需 matplotlib。Testbed 和模型分别沿用[根目录说明](../README.md#复现)与[V2 素材目录](../v2/README.md)。

```bash
git clone https://github.com/private-octopus/picoquic.git
git -C picoquic checkout 37bb5d28aaea417649a6d4facc15f54de7d55829
V3_BIN="$PWD/gold-pipe-endpoint"
bash v3/build.sh "$PWD/picoquic" "$V3_BIN"
V3_RAW="$HOME/quic-v3"
V3_FX="$HOME/quic-v3-fixtures"
V3_EXTRA="$HOME/quic-v3-slow"
V3_RTT="$HOME/quic-v3-long-rtt"
sudo "$PY" v3/fixtures.py --assets "$ASSETS" --out "$V3_FX" \
  --llama "$LLAMA" --lib-path "$LLAMA_LIB_PATH"
sudo "$PY" v3/experiment.py --testbed "$TESTBED" --endpoint "$V3_BIN" \
  --fixtures "$V3_FX/fixtures.json" --out "$V3_RAW" --repeats 3
sudo "$PY" v3/experiment.py --testbed "$TESTBED" --endpoint "$V3_BIN" \
  --out "$V3_EXTRA" --repeats 3 --only A1load_slow
sudo "$PY" v3/experiment.py --testbed "$TESTBED" --endpoint "$V3_BIN" \
  --out "$V3_RTT" --repeats 3 --only B1_rtt_elastic B1_rtt_fast
"$PY" v3/report_data.py "$V3_RAW" "$V3_FX" results/v3 --extra "$V3_EXTRA" "$V3_RTT" --archive
"$PY" v3/plots.py results/v3
```

`fixtures.py` 在原 12 道题的四张图上扫描缩放率和 JPEG 质量，选择最接近 18、40 kB 的完整请求。模型先预热两个档，各题各档独立生成一次真实答案。随后串行跑传输，避免本地 GPU/CPU 推理干扰毫秒级队列观测。模型仅监听隔离收端命名空间内的回环地址。公开结果含请求哈希和逐题模型输出。

三个隔离命名空间和两对 veth 沿用前两版。原版 Testbed NetworkEmulator 在路由器双向 egress 各加 1 ms，B1 长 RTT 补测各加 6 ms。前向速率由 `relay.py` 按区间积分，每个 IP 数据报必须获得足够的服务字节才能出队。回程不整形速率。前向缓冲上限 256 个包，数据报按到达顺序释放。逐包记录预约出队和实际出队，用于检查调度误差。该设计让 5–10 ms 速率档位独立于 `tc` 子进程创建时间。relay 保留加密载荷，QUIC/TLS 仍由两端建立和维护。每个 run 的任务和档位由 `experiment.py` 固定种子生成。

端点用 picoquic BBRv1，每个任务走同一连接中的独立可靠双向 Stream。收端在 FIN 后返回一个字节作为传输完成回执。`upload_ms` 从实际任务提交计到收端完整接收，`token_ms` 到发端收到该回执。两者都不含模型计算。20 ms 成功率以全部任务为分母，缺失值保留。C1 的 `correct20` 把实测上传成功和同一请求的独立模型评分配对，表示在上传预算内可用的正确输入/输出组合。真实首字时间、无线频谱利用率和在线预测精度各需要独立测量。

| 证据 | 说明 |
|---|---|
| [逐任务数据](../results/v3/tasks.csv.gz) | 含计划与实际开始、上传和回执时延、窗口前 20 ms 队列、pacing 失配、应用档位。`retry=1` 为 C1 二次发送。 |
| [分组汇总](../results/v3/task-summary.csv) | 以场景和任务大小或信道状态分组。`matched20` 要求在 20 ms 内连续 3 次约 1 ms 采样的 pacing 目标均在容量 ±10% 内。`reserved_fill_pct` 是在预算内完整交付的应用字节占该预算内配置服务字节的比例。 |
| [逐轮指标](../results/v3/runs.json) 与 [A1 时序](../results/v3/A1-trace.csv) | A1 的稳态样本在共同时间区间 3–6 秒，pacing、窗口和带宽估计筛选在途非空采样。数组分位数顺序为 P05、P50、P95、P99。展示用 A1 时序每 10 ms 取一个状态样本并统计队列峰值。 |
| [B1/D1 时序](../results/v3/BD-curves.csv.gz) | 每次任务前 40 ms 按 1 ms 分桶，状态取桶首前最近的样本。`tx/rx` 是路由中继的入队与出队 IP 字节数，`queue` 是桶内队列峰值。 |
| [真实推理结果](../results/v3/model-results.csv) 与 [输入档位](../results/v3/fixtures.json) | 原始题标识、答案、正确性、每张图重编码后的组合请求字节数和 SHA-256。图片和权重由本地素材准备。 |
| [代表性原始证据](../results/v3/trace-evidence.tar.xz) | 四类用例选定轮次的双端 qlog、端点逐采样状态、发送和接收事件、路由器逐包事件，以及去除本地素材路径的任务和容量计划。 |
